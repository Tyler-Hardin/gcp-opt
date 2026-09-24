"""Query helpers that answer concrete sizing/cost questions from the catalog.

These are deliberately solver-free: for a fixed machine type and disk kind the
cheapest feasible size is the analytic inverse of the performance formula, and
"max bandwidth for a budget" is a monotone cost inversion.  Rich multi-variable
problems (several disk kinds sharing one IOPS budget, mixed fleets) belong in
cvxopt; :mod:`gcp_opt.export` packages the candidate rows for that.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

from gcp_opt.catalog import Catalog
from gcp_opt.errors import InfeasibleTargetError, UnmodeledDiskKindError
from gcp_opt.models import DiskKind, DiskOption, PriceSku, Scope
from gcp_opt.performance import required_size_gib, saturation_size_gib
from gcp_opt.units import DecimalLike, to_decimal

DEFAULT_DISK_KINDS: tuple[DiskKind, ...] = (
    DiskKind.PD_STANDARD,
    DiskKind.PD_BALANCED,
    DiskKind.PD_SSD,
)

#: Practical ceiling for inverting tiered pricing; ~9.3 PiB.
_MAX_SEARCH_GIB = Decimal(10**7)

ThroughputMetric = Literal["read", "write", "balanced"]


@dataclass(frozen=True)
class Requirement:
    """Performance and capacity targets for a single instance."""

    min_total_size_gib: Decimal = Decimal(0)
    min_read_mibps: Decimal | None = None
    min_write_mibps: Decimal | None = None
    min_read_iops: Decimal | None = None
    min_write_iops: Decimal | None = None
    max_monthly_cost_usd: Decimal | None = None

    @classmethod
    def build(
        cls,
        *,
        min_total_size_gib: DecimalLike = 0,
        min_read_mibps: DecimalLike | None = None,
        min_write_mibps: DecimalLike | None = None,
        min_read_iops: DecimalLike | None = None,
        min_write_iops: DecimalLike | None = None,
        max_monthly_cost_usd: DecimalLike | None = None,
    ) -> Requirement:
        """Construct a requirement with explicit unit-suffixed inputs."""
        return cls(
            min_total_size_gib=to_decimal(min_total_size_gib),
            min_read_mibps=to_decimal(min_read_mibps) if min_read_mibps is not None else None,
            min_write_mibps=to_decimal(min_write_mibps) if min_write_mibps is not None else None,
            min_read_iops=to_decimal(min_read_iops) if min_read_iops is not None else None,
            min_write_iops=to_decimal(min_write_iops) if min_write_iops is not None else None,
            max_monthly_cost_usd=(
                to_decimal(max_monthly_cost_usd) if max_monthly_cost_usd is not None else None
            ),
        )


def max_affordable_size(
    sku: PriceSku,
    budget: DecimalLike,
    *,
    max_size_gib: Decimal = _MAX_SEARCH_GIB,
) -> Decimal:
    """Largest whole-GiB size whose monthly cost does not exceed ``budget``.

    Uses bisection because tiered pricing (Standard PD's free first tier) makes the
    close-form inversion piecewise.  Cost is monotone non-decreasing in size.
    """
    budget_value = to_decimal(budget)
    if budget_value <= 0:
        return Decimal(0)
    positive = [tier.unit_price for tier in sku.tiers if tier.unit_price > 0]
    if not positive:
        return max_size_gib
    high = min(
        max_size_gib,
        (budget_value / min(positive)).to_integral_value(rounding=ROUND_FLOOR),
    )
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
    return low


def min_cost_option(
    catalog: Catalog,
    machine_types: list[str],
    *,
    requirement: Requirement,
    region: str | None = None,
    disk_kinds: tuple[DiskKind, ...] = DEFAULT_DISK_KINDS,
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
        disk_kinds: disk kinds to consider.
        scope: zonal or regional.
        allow_us_list_price: accept US list prices for a non-US region.
        require_known_limit: skip machines whose VM-level disk ceiling is unknown.

    Raises:
        InfeasibleTargetError: if no configuration satisfies the requirement.
    """
    best: DiskOption | None = None
    rejections: list[str] = []

    for machine_type in machine_types:
        info = catalog.machine_info(machine_type)
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
            if (
                info is not None
                and info.maximum_total_size_gib is not None
                and size > info.maximum_total_size_gib
            ):
                rejections.append(
                    f"{machine_type}/{disk_kind}: needs {size} GiB but the VM allows "
                    f"{info.maximum_total_size_gib} GiB"
                )
                continue
            if info is not None and info.maximum_persistent_disks == 0:
                rejections.append(f"{machine_type}/{disk_kind}: machine allows no disks")
                continue

            option = catalog.disk_option(
                machine_type,
                disk_kind,
                size,
                region=region,
                scope=scope,
                allow_us_list_price=allow_us_list_price,
            )
            if (
                requirement.max_monthly_cost_usd is not None
                and option.monthly_cost_usd > requirement.max_monthly_cost_usd
            ):
                rejections.append(
                    f"{machine_type}/{disk_kind}: costs {option.monthly_cost_usd} > "
                    f"{requirement.max_monthly_cost_usd}"
                )
                continue
            if best is None or option.monthly_cost_usd < best.monthly_cost_usd:
                best = option

    if best is None:
        summary = "; ".join(rejections[:5]) or "no candidates"
        raise InfeasibleTargetError(
            f"no single-instance configuration satisfies the requirement ({summary})",
            target="min_cost_option",
            limit_kind="catalog",
        )
    return best


def max_throughput_option(
    catalog: Catalog,
    machine_types: list[str],
    *,
    budget_usd: DecimalLike,
    min_total_size_gib: DecimalLike = 0,
    metric: ThroughputMetric = "read",
    region: str | None = None,
    disk_kinds: tuple[DiskKind, ...] = DEFAULT_DISK_KINDS,
    scope: Scope = Scope.ZONAL,
    allow_us_list_price: bool = False,
    require_known_limit: bool = True,
) -> DiskOption:
    """Find the option with the highest throughput within a monthly disk budget.

    Throughput rises monotonically with size until the VM or disk-type ceiling, so
    each candidate simply takes the largest affordable size.

    Raises:
        InfeasibleTargetError: if nothing fits the budget and minimum size.
    """
    budget = to_decimal(budget_usd)
    min_size = to_decimal(min_total_size_gib)
    best: DiskOption | None = None
    best_score = Decimal(-1)
    rejections: list[str] = []

    for machine_type in machine_types:
        info = catalog.machine_info(machine_type)
        for disk_kind in disk_kinds:
            try:
                model = catalog.disk_model(disk_kind, scope)
            except UnmodeledDiskKindError:
                continue
            limit = catalog.limit_for(machine_type, disk_kind, scope)
            if limit is None and require_known_limit:
                rejections.append(f"{machine_type}/{disk_kind}: no documented VM disk ceiling")
                continue
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

            # Throughput stops improving at the saturation size; buy only up to it
            # (or up to what the budget allows, whichever is smaller).
            saturation = saturation_size_gib(
                model, limit, write=metric == "write", throughput=True
            )
            target = min_size
            if saturation is not None:
                target = max(target, saturation)
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
