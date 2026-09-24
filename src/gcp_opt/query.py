"""Query helpers that answer concrete sizing/cost questions from the catalog.

These are deliberately solver-free: for a fixed machine type and disk kind the
cheapest feasible size (or provisioned-IOPS level) is the analytic inverse of the
performance formula, and "max bandwidth for a budget" is a monotone cost inversion.
Rich multi-variable problems (several disk kinds sharing one IOPS budget, mixed
fleets) belong in cvxopt; :mod:`gcp_opt.export` packages the candidate rows for that.

Two families of disks are handled:

* **size-scaled** (``pd-standard``, ``pd-balanced``, ``pd-ssd``): performance grows
  with capacity, so the cheapest feasible size is ``required_size_gib``.
* **provisioned** (``pd-extreme``): performance is bought directly, so the cheapest
  solution provisions the minimum IOPS that meets the targets
  (:func:`~gcp_opt.performance.required_provisioned_iops`) at the minimum capacity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

from gcp_opt.catalog import Catalog
from gcp_opt.errors import (
    InfeasibleTargetError,
    PriceUnavailableError,
    UnmodeledDiskKindError,
)
from gcp_opt.models import (
    ConfigOption,
    DiskKind,
    DiskOption,
    DiskPerformanceModel,
    MachineTypeDiskLimit,
    MachineTypeInfo,
    Objective,
    PriceSku,
    Scope,
    SkuRole,
)
from gcp_opt.performance import (
    required_provisioned_iops,
    required_size_gib,
    saturation_size_gib,
)
from gcp_opt.units import (
    HOURS_PER_MONTH,
    DecimalLike,
    parse_size_gib,
    parse_throughput_mibps,
    to_decimal,
)

#: Disk kinds whose performance scales with capacity (safe for candidate export).
SIZE_SCALED_DISK_KINDS: tuple[DiskKind, ...] = (
    DiskKind.PD_STANDARD,
    DiskKind.PD_BALANCED,
    DiskKind.PD_SSD,
)

#: Every disk kind this package can model, including provisioned ``pd-extreme``.
ALL_MODELED_DISK_KINDS: tuple[DiskKind, ...] = (*SIZE_SCALED_DISK_KINDS, DiskKind.PD_EXTREME)

#: Backwards-compatible alias for the size-scaled set.
DEFAULT_DISK_KINDS: tuple[DiskKind, ...] = SIZE_SCALED_DISK_KINDS

#: Practical ceiling for inverting tiered pricing; ~9.3 PiB.
_MAX_SEARCH_GIB = Decimal(10**7)

ThroughputMetric = Literal["read", "write", "balanced"]


def _as_size(value: DecimalLike) -> Decimal:
    """Coerce a size, accepting human strings such as ``"10TB"``."""
    return parse_size_gib(value) if isinstance(value, str) else to_decimal(value)


def _as_bandwidth(value: DecimalLike) -> Decimal:
    """Coerce a throughput, accepting human strings such as ``"1.2GBps"``."""
    return parse_throughput_mibps(value) if isinstance(value, str) else to_decimal(value)


@dataclass(frozen=True)
class Requirement:
    """Constraints across every optimization axis.

    Machine axes (vCPU, memory, network) and disk axes (capacity, IOPS,
    throughput) are all optional; leave an axis as ``None`` to ignore it.
    """

    # -- machine axes --
    min_vcpus: int | None = None
    max_vcpus: int | None = None
    min_memory_gib: Decimal | None = None
    max_memory_gib: Decimal | None = None
    min_network_gbps: Decimal | None = None
    max_network_gbps: Decimal | None = None

    # -- disk axes --
    min_total_size_gib: Decimal = Decimal(0)
    min_read_mibps: Decimal | None = None
    min_write_mibps: Decimal | None = None
    min_read_iops: Decimal | None = None
    min_write_iops: Decimal | None = None

    # -- cost --
    max_monthly_cost_usd: Decimal | None = None

    @classmethod
    def build(
        cls,
        *,
        min_vcpus: int | None = None,
        max_vcpus: int | None = None,
        min_memory_gib: DecimalLike | None = None,
        max_memory_gib: DecimalLike | None = None,
        min_network_gbps: DecimalLike | None = None,
        max_network_gbps: DecimalLike | None = None,
        min_total_size_gib: DecimalLike = 0,
        min_read_mibps: DecimalLike | None = None,
        min_write_mibps: DecimalLike | None = None,
        min_read_iops: DecimalLike | None = None,
        min_write_iops: DecimalLike | None = None,
        max_monthly_cost_usd: DecimalLike | None = None,
    ) -> Requirement:
        """Construct a requirement with explicit unit-suffixed inputs."""
        return cls(
            min_vcpus=min_vcpus,
            max_vcpus=max_vcpus,
            min_memory_gib=(_as_size(min_memory_gib) if min_memory_gib is not None else None),
            max_memory_gib=(_as_size(max_memory_gib) if max_memory_gib is not None else None),
            min_network_gbps=(
                to_decimal(min_network_gbps) if min_network_gbps is not None else None
            ),
            max_network_gbps=(
                to_decimal(max_network_gbps) if max_network_gbps is not None else None
            ),
            min_total_size_gib=_as_size(min_total_size_gib),
            min_read_mibps=(
                _as_bandwidth(min_read_mibps) if min_read_mibps is not None else None
            ),
            min_write_mibps=(
                _as_bandwidth(min_write_mibps) if min_write_mibps is not None else None
            ),
            min_read_iops=to_decimal(min_read_iops) if min_read_iops is not None else None,
            min_write_iops=to_decimal(min_write_iops) if min_write_iops is not None else None,
            max_monthly_cost_usd=(
                to_decimal(max_monthly_cost_usd) if max_monthly_cost_usd is not None else None
            ),
        )

    @property
    def disk_required(self) -> bool:
        """True when any disk axis is constrained."""
        return (
            self.min_total_size_gib > 0
            or any(
                value is not None
                for value in (
                    self.min_read_mibps,
                    self.min_write_mibps,
                    self.min_read_iops,
                    self.min_write_iops,
                )
            )
        )

    @property
    def machine_required(self) -> bool:
        """True when any machine axis is constrained."""
        return any(
            value is not None
            for value in (
                self.min_vcpus,
                self.max_vcpus,
                self.min_memory_gib,
                self.max_memory_gib,
                self.min_network_gbps,
                self.max_network_gbps,
            )
        )


def _min_ok(value: Decimal | int | None, minimum: Decimal | int | None) -> bool:
    if minimum is None:
        return True
    return value is not None and value >= minimum


def _max_ok(value: Decimal | int | None, maximum: Decimal | int | None) -> bool:
    if maximum is None:
        return True
    # An unknown value cannot be shown to violate a ceiling, so allow it.
    return value is None or value <= maximum


def machine_satisfies(info: MachineTypeInfo, requirement: Requirement) -> tuple[bool, str | None]:
    """Check a machine against the machine-axis constraints.

    Returns ``(ok, reason)``; ``reason`` is a short human explanation when rejected.
    A ``min`` constraint fails when the machine's value is unknown (we cannot prove
    it meets the floor); a ``max`` constraint passes when unknown.
    """
    checks = (
        ("vcpus", info.guest_cpus, requirement.min_vcpus, requirement.max_vcpus),
        ("memory_gib", info.memory_gb, requirement.min_memory_gib, requirement.max_memory_gib),
        (
            "network_gbps",
            info.network_egress_gbps,
            requirement.min_network_gbps,
            requirement.max_network_gbps,
        ),
    )
    for name, value, minimum, maximum in checks:
        if not _min_ok(value, minimum):
            return False, f"{name}={value} below minimum {minimum}"
        if not _max_ok(value, maximum):
            return False, f"{name}={value} above maximum {maximum}"
    return True, None


MachineMetric = Literal["vcpus", "memory_gib", "network_gbps"]

_MACHINE_OBJECTIVE_METRIC: dict[Objective, MachineMetric] = {
    Objective.MAX_VCPUS: "vcpus",
    Objective.MAX_MEMORY: "memory_gib",
    Objective.MAX_NETWORK: "network_gbps",
}


def machine_metric_value(info: MachineTypeInfo, metric: MachineMetric) -> Decimal | None:
    """Return a machine's value on one metric axis, or ``None`` if unknown."""
    if metric == "vcpus":
        return Decimal(info.guest_cpus) if info.guest_cpus is not None else None
    if metric == "memory_gib":
        return info.memory_gb
    return info.network_egress_gbps


def _metric_sort_key(info: MachineTypeInfo, metric: MachineMetric) -> Decimal:
    """Sort key for a machine metric; unknown values sort below any real value."""
    value = machine_metric_value(info, metric)
    return value if value is not None else Decimal(-1)


def max_affordable_quantity(
    sku: PriceSku,
    budget: DecimalLike,
    *,
    cap: Decimal,
    step: Decimal = Decimal(1),
) -> Decimal:
    """Largest whole-``step`` quantity whose monthly cost does not exceed ``budget``.

    Works for both capacity (quantity in GiB) and provisioned IOPS (quantity in
    IOPS).  Uses bisection because tiered pricing (Standard PD's free first tier)
    makes the closed-form inversion piecewise; cost is monotone in quantity.
    """
    budget_value = to_decimal(budget)
    if budget_value <= 0 or cap <= 0:
        return Decimal(0)
    positive = [tier.unit_price for tier in sku.tiers if tier.unit_price > 0]
    if not positive:
        return cap
    high = min(cap, (budget_value / min(positive)).to_integral_value(rounding=ROUND_FLOOR))
    if high <= 0:
        return Decimal(0)
    low = Decimal(0)
    while low < high:
        midpoint = ((low + high) / 2).to_integral_value(rounding=ROUND_CEILING)
        if midpoint <= low:
            midpoint = low + 1
        if sku.cost_for(midpoint) <= budget_value:
            low = midpoint
        else:
            high = midpoint - 1
    if step > 1:
        low = (low // step) * step
    return low


def max_affordable_size(
    sku: PriceSku,
    budget: DecimalLike,
    *,
    max_size_gib: Decimal = _MAX_SEARCH_GIB,
) -> Decimal:
    """Largest whole-GiB capacity whose monthly cost does not exceed ``budget``."""
    return max_affordable_quantity(sku, budget, cap=max_size_gib)


def _minimum_size(model: DiskPerformanceModel, requirement: Requirement) -> Decimal:
    floor = model.min_size_gib or Decimal(0)
    return max(requirement.min_total_size_gib, floor)


def _fits_machine(
    catalog: Catalog, machine_type: str, size_gib: Decimal
) -> tuple[bool, str | None]:
    info = catalog.machine_info(machine_type)
    if info is None:
        return True, None
    if info.maximum_total_size_gib is not None and size_gib > info.maximum_total_size_gib:
        return False, f"needs {size_gib} GiB but the VM allows {info.maximum_total_size_gib} GiB"
    if info.maximum_persistent_disks == 0:
        return False, "machine allows no disks"
    return True, None


def min_cost_option(
    catalog: Catalog,
    machine_types: list[str],
    *,
    requirement: Requirement,
    region: str | None = None,
    disk_kinds: tuple[DiskKind, ...] = ALL_MODELED_DISK_KINDS,
    scope: Scope = Scope.ZONAL,
    allow_us_list_price: bool = False,
    require_known_limit: bool = True,
) -> DiskOption:
    """Find the cheapest single-instance option meeting every requirement.

    Args:
        catalog: joined data view.
        machine_types: machine types to consider.
        requirement: capacity/performance/cost targets.
        region: region for pricing (defaults to the price book's region).
        disk_kinds: disk kinds to consider (defaults to all modeled kinds).
        scope: zonal or regional.
        allow_us_list_price: accept US list prices for a non-US region.
        require_known_limit: skip machines whose VM-level disk ceiling is unknown.

    Raises:
        InfeasibleTargetError: if no configuration satisfies the requirement.
    """
    best: DiskOption | None = None
    best_total = Decimal("Infinity")
    rejections: list[str] = []

    for machine_type in machine_types:
        info = catalog.machine_info(machine_type)
        if info is not None:
            machine_ok, machine_why = machine_satisfies(info, requirement)
            if not machine_ok:
                rejections.append(f"{machine_type}: {machine_why}")
                continue
        elif requirement.machine_required:
            rejections.append(
                f"{machine_type}: unknown machine cannot satisfy machine constraints"
            )
            continue
        for disk_kind in disk_kinds:
            try:
                model = catalog.disk_model(disk_kind, scope)
            except UnmodeledDiskKindError:
                continue
            limit = catalog.limit_for(machine_type, disk_kind, scope)
            if limit is None and require_known_limit:
                rejections.append(f"{machine_type}/{disk_kind}: no documented VM disk ceiling")
                continue

            provisioned_iops: Decimal | None = None
            if model.provisioned_iops:
                try:
                    provisioned_iops = required_provisioned_iops(
                        model,
                        read_iops=requirement.min_read_iops,
                        write_iops=requirement.min_write_iops,
                        read_mibps=requirement.min_read_mibps,
                        write_mibps=requirement.min_write_mibps,
                        machine_limit=limit,
                    )
                except InfeasibleTargetError as error:
                    rejections.append(f"{machine_type}/{disk_kind}: {error}")
                    continue
                size = _minimum_size(model, requirement)
            else:
                try:
                    size = required_size_gib(
                        model,
                        read_iops=requirement.min_read_iops,
                        write_iops=requirement.min_write_iops,
                        read_mibps=requirement.min_read_mibps,
                        write_mibps=requirement.min_write_mibps,
                        machine_limit=limit,
                    )
                except InfeasibleTargetError as error:
                    rejections.append(f"{machine_type}/{disk_kind}: {error}")
                    continue
                size = max(size, requirement.min_total_size_gib)

            fits, why = _fits_machine(catalog, machine_type, size)
            if not fits:
                rejections.append(f"{machine_type}/{disk_kind}: {why}")
                continue

            try:
                option = catalog.disk_option(
                    machine_type,
                    disk_kind,
                    size,
                    region=region,
                    scope=scope,
                    provisioned_iops=provisioned_iops,
                    allow_us_list_price=allow_us_list_price,
                )
            except PriceUnavailableError as error:
                rejections.append(f"{machine_type}/{disk_kind}: {error}")
                continue
            # Include the machine price in the ranking when the snapshot has one;
            # otherwise this is a pure disk-cost comparison (cost_basis=disk_only).
            hourly = catalog.machine_hourly_price(machine_type, region)
            total = option.monthly_cost_usd + (
                hourly * HOURS_PER_MONTH if hourly is not None else Decimal(0)
            )
            if (
                requirement.max_monthly_cost_usd is not None
                and total > requirement.max_monthly_cost_usd
            ):
                rejections.append(
                    f"{machine_type}/{disk_kind}: costs {total} > "
                    f"{requirement.max_monthly_cost_usd}"
                )
                continue
            if best is None or total < best_total:
                best, best_total = option, total

    if best is None:
        summary = "; ".join(rejections[:5]) or "no candidates"
        raise InfeasibleTargetError(
            f"no single-instance configuration satisfies the requirement ({summary})",
            target="min_cost_option",
            limit_kind="catalog",
        )
    return best


def _provisioned_budget_option(
    catalog: Catalog,
    machine_type: str,
    disk_kind: DiskKind,
    model: DiskPerformanceModel,
    requirement: Requirement,
    *,
    budget: Decimal,
    metric: ThroughputMetric,
    region: str | None,
    scope: Scope,
    allow_us_list_price: bool,
) -> DiskOption | None:
    """Best provisioned-IOPS option for ``pd-extreme`` within a budget.

    Buys the minimum capacity, then spends the remaining budget on as many
    provisioned IOPS as the VM and disk-type ceilings allow for ``metric``.
    """
    limit = catalog.limit_for(machine_type, disk_kind, scope)
    capacity_sku = catalog.price_sku(
        disk_kind,
        region=region,
        scope=scope,
        role=SkuRole.CAPACITY,
        allow_us_list_price=allow_us_list_price,
    )
    iops_sku = catalog.price_sku(
        disk_kind,
        region=region,
        scope=scope,
        role=SkuRole.PROVISIONED_IOPS,
        allow_us_list_price=allow_us_list_price,
    )
    if capacity_sku is None or iops_sku is None:
        return None
    size = _minimum_size(model, requirement)
    fits, _ = _fits_machine(catalog, machine_type, size)
    if not fits:
        return None
    capacity_cost = capacity_sku.cost_for(size)
    if capacity_cost > budget:
        return None

    per_iop = model.throughput_mibps_per_provisioned_iop or Decimal(0)
    if per_iop <= 0:
        return None

    directions = ("read", "write") if metric == "balanced" else (metric,)
    caps: list[Decimal] = []
    if model.provisioned_iops_max is not None:
        caps.append(model.provisioned_iops_max)
    for direction in directions:
        if limit is not None:
            caps.append(limit.max_read_iops if direction == "read" else limit.max_write_iops)
        type_mibps = model.max_read_mibps if direction == "read" else model.max_write_mibps
        if type_mibps is not None:
            caps.append(type_mibps / per_iop)
        if limit is not None:
            instance_mibps = (
                limit.max_read_mibps if direction == "read" else limit.max_write_mibps
            )
            caps.append(instance_mibps / per_iop)
    effective_cap = min(caps) if caps else Decimal(10**7)

    affordable = max_affordable_quantity(iops_sku, budget - capacity_cost, cap=effective_cap)
    iops = affordable
    if model.provisioned_iops_min is not None:
        iops = max(iops, model.provisioned_iops_min)
    if effective_cap > 0:
        iops = min(iops, effective_cap)
    option = catalog.disk_option(
        machine_type,
        disk_kind,
        size,
        region=region,
        scope=scope,
        provisioned_iops=iops,
        allow_us_list_price=allow_us_list_price,
    )
    return option if option.monthly_cost_usd <= budget else None


def max_throughput_option(
    catalog: Catalog,
    machine_types: list[str],
    *,
    budget_usd: DecimalLike,
    min_total_size_gib: DecimalLike = 0,
    metric: ThroughputMetric = "read",
    region: str | None = None,
    disk_kinds: tuple[DiskKind, ...] = ALL_MODELED_DISK_KINDS,
    scope: Scope = Scope.ZONAL,
    allow_us_list_price: bool = False,
    require_known_limit: bool = True,
    requirement: Requirement | None = None,
) -> DiskOption:
    """Find the option with the highest throughput within a monthly disk budget.

    Throughput rises monotonically with size until the VM or disk-type ceiling, so
    each size-scaled candidate takes the largest useful affordable size.  For
    provisioned disks the budget is split: minimum capacity first, then as many
    provisioned IOPS as the remainder affords.

    Raises:
        InfeasibleTargetError: if nothing fits the budget and minimum size.
    """
    budget = to_decimal(budget_usd)
    base = requirement or Requirement()
    floor = Requirement.build(min_total_size_gib=min_total_size_gib)
    effective = replace(
        base,
        min_total_size_gib=max(base.min_total_size_gib, floor.min_total_size_gib),
    )
    min_size = effective.min_total_size_gib
    best: DiskOption | None = None
    best_score = Decimal(-1)
    rejections: list[str] = []

    for machine_type in machine_types:
        info = catalog.machine_info(machine_type)
        if info is not None:
            machine_ok, machine_why = machine_satisfies(info, effective)
            if not machine_ok:
                rejections.append(f"{machine_type}: {machine_why}")
                continue
        elif effective.machine_required:
            rejections.append(
                f"{machine_type}: unknown machine cannot satisfy machine constraints"
            )
            continue
        for disk_kind in disk_kinds:
            try:
                model = catalog.disk_model(disk_kind, scope)
            except UnmodeledDiskKindError:
                continue
            limit = catalog.limit_for(machine_type, disk_kind, scope)
            if limit is None and require_known_limit:
                rejections.append(f"{machine_type}/{disk_kind}: no documented VM disk ceiling")
                continue

            if model.provisioned_iops:
                option = _provisioned_budget_option(
                    catalog,
                    machine_type,
                    disk_kind,
                    model,
                    effective,
                    budget=budget,
                    metric=metric,
                    region=region,
                    scope=scope,
                    allow_us_list_price=allow_us_list_price,
                )
                if option is None:
                    rejections.append(f"{machine_type}/{disk_kind}: no affordable option")
                    continue
            else:
                sku = catalog.price_sku(
                    disk_kind, region=region, scope=scope, allow_us_list_price=allow_us_list_price
                )
                if sku is None:
                    continue
                affordable = max_affordable_size(sku, budget)
                if info is not None and info.maximum_total_size_gib is not None:
                    affordable = min(affordable, info.maximum_total_size_gib)
                if affordable < min_size:
                    rejections.append(
                        f"{machine_type}/{disk_kind}: affordable {affordable} GiB < "
                        f"required {min_size} GiB"
                    )
                    continue
                # Throughput stops improving at the saturation size; buy only up to
                # it (or up to what the budget allows, whichever is smaller).
                saturation = saturation_size_gib(
                    model, limit, write=metric == "write", throughput=True
                )
                target = max(min_size, saturation) if saturation is not None else min_size
                size = min(affordable, target)
                if size < min_size:
                    continue
                option = catalog.disk_option(
                    machine_type,
                    disk_kind,
                    size,
                    region=region,
                    scope=scope,
                    allow_us_list_price=allow_us_list_price,
                )
                if option.monthly_cost_usd > budget:  # guard against rounding
                    continue

            score = _score(option, metric)
            if best is None or score > best_score or (
                score == best_score and option.monthly_cost_usd < best.monthly_cost_usd
            ):
                best, best_score = option, score

    if best is None:
        summary = "; ".join(rejections[:5]) or "no candidates"
        raise InfeasibleTargetError(
            f"no configuration fits a ${budget}/month disk budget ({summary})",
            target="max_throughput_option",
            limit_kind="budget",
        )
    return best


def _score(option: DiskOption, metric: ThroughputMetric) -> Decimal:
    if metric == "read":
        return option.read_mibps
    if metric == "write":
        return option.write_mibps
    return min(option.read_mibps, option.write_mibps)


@dataclass(frozen=True)
class AggregateOption:
    """An option replicated across instances.

    Only *disk* cost is summed; VM instance cost is not part of this dataset, so
    this is a lower bound on a real fleet's cost.
    """

    per_instance: DiskOption
    replicas: int
    total_size_gib: Decimal
    total_read_mibps: Decimal
    total_write_mibps: Decimal
    total_read_iops: Decimal
    total_write_iops: Decimal
    total_monthly_disk_cost_usd: Decimal
    cost_note: str = "disk capacity cost only; VM instance cost excluded"


def scale_out(option: DiskOption, replicas: int) -> AggregateOption:
    """Replicate one option across ``replicas`` instances."""
    if replicas < 1:
        raise ValueError(f"replicas must be >= 1, got {replicas}")
    count = Decimal(replicas)
    return AggregateOption(
        per_instance=option,
        replicas=replicas,
        total_size_gib=option.size_gib * count,
        total_read_mibps=option.read_mibps * count,
        total_write_mibps=option.write_mibps * count,
        total_read_iops=option.read_iops * count,
        total_write_iops=option.write_iops * count,
        total_monthly_disk_cost_usd=option.monthly_cost_usd * count,
    )


def min_replicas(
    option: DiskOption, *, min_total_size_gib: Decimal, min_read_mibps: Decimal
) -> int:
    """Smallest replica count whose aggregate meets the given totals.

    Raises:
        InfeasibleTargetError: if the option's bands are zero, making scaling useless.
    """
    needed = 1
    if option.size_gib > 0:
        size_replicas = (
            to_decimal(min_total_size_gib) / option.size_gib
        ).to_integral_value(rounding=ROUND_CEILING)
        needed = max(needed, int(size_replicas))
    if option.read_mibps > 0:
        bandwidth_replicas = (
            to_decimal(min_read_mibps) / option.read_mibps
        ).to_integral_value(rounding=ROUND_CEILING)
        needed = max(needed, int(bandwidth_replicas))
    elif min_read_mibps > 0:
        raise InfeasibleTargetError(
            "per-instance read throughput is zero; scaling out cannot add bandwidth",
            target="min_replicas",
            limit_kind="disk_model",
        )
    return max(1, needed)


# --------------------------------------------------------------------------- #
# Generalized, objective-driven selection
# --------------------------------------------------------------------------- #
DiskMetric = Literal["read", "write", "balanced", "iops", "size"]


def _cost_or_inf(option: ConfigOption | DiskOption) -> Decimal:
    cost = option.monthly_cost_usd
    return cost if cost is not None else Decimal("Infinity")


def _machine_tiebreak(option: ConfigOption) -> tuple[Decimal, Decimal]:
    """Prefer cheaper, then smaller machines. Missing prices sort last."""
    cpus = option.guest_cpus if option.guest_cpus is not None else 10**9
    return (_cost_or_inf(option), Decimal(cpus))


def best_machine(
    catalog: Catalog,
    machine_types: list[str],
    *,
    objective: Objective,
    requirement: Requirement | None = None,
    region: str | None = None,
) -> ConfigOption:
    """Find the machine maximizing an axis (vCPU, memory or network).

    Machine-axis constraints in ``requirement`` (and ``max_monthly_cost_usd`` when
    machine prices are available) are honored.  Ties break toward the cheaper,
    then smaller, machine.

    Raises:
        ValueError: if ``objective`` is not a machine objective.
        InfeasibleTargetError: if no machine satisfies the constraints.
    """
    metric = _MACHINE_OBJECTIVE_METRIC.get(objective)
    if metric is None:
        raise ValueError(f"{objective} is not a machine objective")
    base = requirement or Requirement()
    best: ConfigOption | None = None
    best_score = Decimal(-1)
    best_tie: tuple[Decimal, Decimal] | None = None
    rejections: list[str] = []

    for machine_type in machine_types:
        info = catalog.machine_info(machine_type)
        if info is None:
            rejections.append(f"{machine_type}: unknown machine")
            continue
        ok, why = machine_satisfies(info, base)
        if not ok:
            rejections.append(f"{machine_type}: {why}")
            continue
        score = machine_metric_value(info, metric)
        if score is None:
            rejections.append(f"{machine_type}: no {metric} data")
            continue
        option = catalog.machine_only_option(machine_type, region=region)
        if (
            base.max_monthly_cost_usd is not None
            and option.monthly_cost_usd is not None
            and option.monthly_cost_usd > base.max_monthly_cost_usd
        ):
            rejections.append(
                f"{machine_type}: costs {option.monthly_cost_usd} > "
                f"{base.max_monthly_cost_usd}"
            )
            continue
        tie = _machine_tiebreak(option)
        if (
            best is None
            or score > best_score
            or (score == best_score and best_tie is not None and tie < best_tie)
        ):
            best, best_score, best_tie = option, score, tie

    if best is None:
        summary = "; ".join(rejections[:5]) or "no candidates"
        raise InfeasibleTargetError(
            f"no machine satisfies the constraints ({summary})",
            target=f"best_machine:{objective}",
            limit_kind="catalog",
        )
    return best


def _disk_metric_value(option: DiskOption, metric: DiskMetric) -> Decimal:
    if metric == "read":
        return option.read_mibps
    if metric == "write":
        return option.write_mibps
    if metric == "balanced":
        return min(option.read_mibps, option.write_mibps)
    if metric == "iops":
        return max(option.read_iops, option.write_iops)
    return option.size_gib


def _saturation_for(
    option_model: DiskPerformanceModel, limit: MachineTypeDiskLimit | None, metric: DiskMetric
) -> Decimal | None:
    """Saturation size for a disk metric, or ``None`` when it keeps growing."""
    if metric == "size":
        return None
    if metric in {"read", "write", "balanced"}:
        write = metric == "write"
        return saturation_size_gib(option_model, limit, write=write, throughput=True)
    # iops
    return saturation_size_gib(option_model, limit, write=False, throughput=False)


def max_disk_metric_option(
    catalog: Catalog,
    machine_types: list[str],
    *,
    objective: Objective,
    budget_usd: DecimalLike,
    requirement: Requirement | None = None,
    region: str | None = None,
    disk_kinds: tuple[DiskKind, ...] = ALL_MODELED_DISK_KINDS,
    scope: Scope = Scope.ZONAL,
    allow_us_list_price: bool = False,
    require_known_limit: bool = True,
) -> DiskOption:
    """Maximize one disk axis (size, read/write throughput, IOPS) within a budget.

    Raises:
        ValueError: if ``objective`` is not a disk metric objective.
        InfeasibleTargetError: if nothing fits.
    """
    metric_map: dict[Objective, DiskMetric] = {
        Objective.MAX_DISK_SIZE: "size",
        Objective.MAX_DISK_READ: "read",
        Objective.MAX_DISK_WRITE: "write",
        Objective.MAX_DISK_IOPS: "iops",
    }
    metric = metric_map.get(objective)
    if metric is None:
        raise ValueError(f"{objective} is not a disk metric objective")

    budget = to_decimal(budget_usd)
    base = requirement or Requirement()
    best: DiskOption | None = None
    best_score = Decimal(-1)
    rejections: list[str] = []

    for machine_type in machine_types:
        info = catalog.machine_info(machine_type)
        if info is not None:
            machine_ok, machine_why = machine_satisfies(info, base)
            if not machine_ok:
                rejections.append(f"{machine_type}: {machine_why}")
                continue
        elif base.machine_required:
            rejections.append(f"{machine_type}: unknown machine cannot satisfy machine constraints")
            continue
        for disk_kind in disk_kinds:
            try:
                model = catalog.disk_model(disk_kind, scope)
            except UnmodeledDiskKindError:
                continue
            limit = catalog.limit_for(machine_type, disk_kind, scope)
            if limit is None and require_known_limit:
                rejections.append(f"{machine_type}/{disk_kind}: no documented VM disk ceiling")
                continue
            try:
                option = _max_disk_candidate(
                    catalog,
                    machine_type,
                    disk_kind,
                    model,
                    limit,
                    metric=metric,
                    base=base,
                    budget=budget,
                    info=info,
                    region=region,
                    scope=scope,
                    allow_us_list_price=allow_us_list_price,
                )
            except (InfeasibleTargetError, PriceUnavailableError) as error:
                rejections.append(f"{machine_type}/{disk_kind}: {error}")
                continue
            if option is None:
                continue
            score = _disk_metric_value(option, metric)
            if best is None or score > best_score or (
                score == best_score and option.monthly_cost_usd < best.monthly_cost_usd
            ):
                best, best_score = option, score

    if best is None:
        summary = "; ".join(rejections[:5]) or "no candidates"
        raise InfeasibleTargetError(
            f"no configuration fits a ${budget}/month disk budget ({summary})",
            target=f"max_disk_metric:{metric}",
            limit_kind="budget",
        )
    return best


def _max_disk_candidate(
    catalog: Catalog,
    machine_type: str,
    disk_kind: DiskKind,
    model: DiskPerformanceModel,
    limit: MachineTypeDiskLimit | None,
    *,
    metric: DiskMetric,
    base: Requirement,
    budget: Decimal,
    info: MachineTypeInfo | None,
    region: str | None,
    scope: Scope,
    allow_us_list_price: bool,
) -> DiskOption | None:
    """Build the best disk for one (machine, kind) under ``metric`` and ``budget``."""
    machine_cap = info.maximum_total_size_gib if info is not None else None

    if model.provisioned_iops:
        # Provisioned disks: performance is bought, capacity is separate.
        try:
            provisioned = required_provisioned_iops(
                model,
                read_iops=base.min_read_iops,
                write_iops=base.min_write_iops,
                read_mibps=base.min_read_mibps,
                write_mibps=base.min_write_mibps,
                machine_limit=limit,
            )
        except InfeasibleTargetError:
            raise
        iops_sku = catalog.price_sku(
            disk_kind,
            region=region,
            scope=scope,
            role=SkuRole.PROVISIONED_IOPS,
            allow_us_list_price=allow_us_list_price,
        )
        capacity_sku = catalog.price_sku(
            disk_kind,
            region=region,
            scope=scope,
            role=SkuRole.CAPACITY,
            allow_us_list_price=allow_us_list_price,
        )
        if iops_sku is None or capacity_sku is None:
            raise PriceUnavailableError(f"missing SKUs for {disk_kind}")
        if metric == "size":
            remaining = budget - iops_sku.cost_for(provisioned)
            if remaining < 0:
                return None
            size = max_affordable_size(capacity_sku, remaining)
            size = max(size, base.min_total_size_gib)
            if machine_cap is not None:
                size = min(size, machine_cap)
            if size < base.min_total_size_gib:
                return None
        else:
            size = max(base.min_total_size_gib, model.min_size_gib or Decimal(0))
            if machine_cap is not None and size > machine_cap:
                return None
            capacity_cost = capacity_sku.cost_for(size)
            if capacity_cost > budget:
                return None
            cap = _provisioned_iops_cap(model, limit, metric)
            affordable = max_affordable_quantity(iops_sku, budget - capacity_cost, cap=cap)
            provisioned = max(provisioned, min(affordable, cap))
        option = catalog.disk_option(
            machine_type,
            disk_kind,
            size,
            region=region,
            scope=scope,
            provisioned_iops=provisioned,
            allow_us_list_price=allow_us_list_price,
        )
        return option if option.monthly_cost_usd <= budget else None

    capacity_sku = catalog.price_sku(
        disk_kind, region=region, scope=scope, allow_us_list_price=allow_us_list_price
    )
    if capacity_sku is None:
        return None
    try:
        required = required_size_gib(
            model,
            read_iops=base.min_read_iops,
            write_iops=base.min_write_iops,
            read_mibps=base.min_read_mibps,
            write_mibps=base.min_write_mibps,
            machine_limit=limit,
        )
    except InfeasibleTargetError:
        raise
    required = max(required, base.min_total_size_gib)

    affordable = max_affordable_size(capacity_sku, budget)
    if machine_cap is not None:
        affordable = min(affordable, machine_cap)
    if required > affordable:
        return None
    if metric == "size":
        size = affordable
    else:
        saturation = _saturation_for(model, limit, metric)
        target = max(required, saturation) if saturation is not None else required
        size = min(affordable, target)
        if size < required:
            return None
    option = catalog.disk_option(
        machine_type, disk_kind, size, region=region, scope=scope,
        allow_us_list_price=allow_us_list_price,
    )
    return option if option.monthly_cost_usd <= budget else None


def _direction_iops_cap(
    model: DiskPerformanceModel,
    limit: MachineTypeDiskLimit | None,
    *,
    write: bool,
) -> Decimal:
    """Largest useful provisioned level for one direction, honoring every cap."""
    per_iop = model.throughput_mibps_per_provisioned_iop or Decimal(0)
    type_iops = model.max_write_iops if write else model.max_read_iops
    type_mibps = model.max_write_mibps if write else model.max_read_mibps
    caps: list[Decimal] = []
    if model.provisioned_iops_max is not None:
        caps.append(model.provisioned_iops_max)
    if type_iops is not None:
        caps.append(type_iops)
    if limit is not None:
        caps.append(limit.max_write_iops if write else limit.max_read_iops)
        instance_mibps = limit.max_write_mibps if write else limit.max_read_mibps
        if instance_mibps is not None and per_iop > 0:
            caps.append(instance_mibps / per_iop)
    if type_mibps is not None and per_iop > 0:
        caps.append(type_mibps / per_iop)
    return min(caps) if caps else Decimal(10**7)


def _provisioned_iops_cap(
    model: DiskPerformanceModel, limit: MachineTypeDiskLimit | None, metric: DiskMetric
) -> Decimal:
    """Largest useful provisioned-IOPS level for a metric.

    Direction-aware: a read objective is not capped by the write ceiling.  For the
    combined ``iops`` metric the level maximizing ``max(read, write)`` is the larger
    of the two per-direction caps.
    """
    if metric == "size":
        return Decimal(0)
    read_cap = _direction_iops_cap(model, limit, write=False)
    if metric == "read":
        return read_cap
    write_cap = _direction_iops_cap(model, limit, write=True)
    if metric == "write":
        return write_cap
    return max(read_cap, write_cap)


def optimize(
    catalog: Catalog,
    machine_types: list[str],
    *,
    objective: Objective,
    requirement: Requirement | None = None,
    region: str | None = None,
    disk_kinds: tuple[DiskKind, ...] = ALL_MODELED_DISK_KINDS,
    scope: Scope = Scope.ZONAL,
    allow_us_list_price: bool = False,
    require_known_limit: bool = True,
    budget_usd: DecimalLike | None = None,
) -> ConfigOption:
    """Answer any supported question with one call.

    Machine objectives (``MAX_VCPUS``/``MAX_MEMORY``/``MAX_NETWORK``) return a
    machine (with a disk attached only if the requirement constrains disk axes).
    Disk objectives and ``MIN_COST`` return a full machine + disk configuration.

    Raises:
        ValueError: if a disk objective has no budget.
        InfeasibleTargetError: if nothing satisfies the requirement.
    """
    base = requirement or Requirement()
    effective_budget = (
        base.max_monthly_cost_usd if base.max_monthly_cost_usd is not None else budget_usd
    )

    if objective in _MACHINE_OBJECTIVE_METRIC:
        if not base.disk_required:
            return best_machine(
                catalog, machine_types, objective=objective, requirement=base, region=region
            )
        # With a disk required, walk machines best-first and take the first that can
        # also host a suitable disk (the largest-memory machine may have no
        # documented disk ceiling at all, for example).
        metric = _MACHINE_OBJECTIVE_METRIC[objective]
        ordered = sorted(
            (
                info
                for name in machine_types
                if (info := catalog.machine_info(name)) is not None
                and machine_satisfies(info, base)[0]
            ),
            key=lambda info: _metric_sort_key(info, metric),
            reverse=True,
        )
        rejections: list[str] = []
        for info in ordered:
            try:
                disk = min_cost_option(
                    catalog,
                    [info.name],
                    requirement=base,
                    region=region,
                    disk_kinds=disk_kinds,
                    scope=scope,
                    allow_us_list_price=allow_us_list_price,
                    require_known_limit=require_known_limit,
                )
            except InfeasibleTargetError as error:
                rejections.append(f"{info.name}: {error}")
                continue
            hourly = catalog.machine_hourly_price(info.name, region)
            machine_cost = hourly * HOURS_PER_MONTH if hourly is not None else None
            return catalog.assemble_config(info, disk=disk, machine_monthly_cost_usd=machine_cost)
        summary = "; ".join(rejections[:5]) or "no candidates"
        raise InfeasibleTargetError(
            f"no machine satisfies both the {objective} objective and the disk "
            f"requirement ({summary})",
            target=f"optimize:{objective}",
            limit_kind="catalog",
        )

    if objective is Objective.MIN_COST:
        disk = min_cost_option(
            catalog,
            machine_types,
            requirement=base,
            region=region,
            disk_kinds=disk_kinds,
            scope=scope,
            allow_us_list_price=allow_us_list_price,
            require_known_limit=require_known_limit,
        )
        return catalog.wrap_disk_option(disk)

    if effective_budget is None:
        raise ValueError(f"{objective} requires a budget (set max_monthly_cost_usd or budget_usd)")
    disk = max_disk_metric_option(
        catalog,
        machine_types,
        objective=objective,
        budget_usd=effective_budget,
        requirement=base,
        region=region,
        disk_kinds=disk_kinds,
        scope=scope,
        allow_us_list_price=allow_us_list_price,
        require_known_limit=require_known_limit,
    )
    return catalog.wrap_disk_option(disk)
