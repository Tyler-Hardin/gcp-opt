"""Join disk models, machine-type ceilings and prices into optimizer-ready rows."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from gcp_opt import constants
from gcp_opt.dataset import Dataset
from gcp_opt.errors import PriceUnavailableError, UnmodeledDiskKindError
from gcp_opt.models import (
    DiskKind,
    DiskOption,
    DiskPerformanceModel,
    MachineTypeDiskLimit,
    MachineTypeInfo,
    PriceSku,
    Scope,
)
from gcp_opt.performance import achievable_performance
from gcp_opt.pricing import find_price
from gcp_opt.units import DecimalLike, to_decimal

#: Sentinel used when ranking unbounded vCPU ranges by width.
_UNBOUNDED_WIDTH = 10**9


class Catalog:
    """A read-only view that resolves machine ceilings and prices for disk options."""

    def __init__(self, dataset: Dataset) -> None:
        self._dataset = dataset
        self._machine_limits = {
            (limit.machine_type, limit.disk_kind, limit.scope): limit
            for limit in dataset.limits.machine_type_limits
        }

    @property
    def dataset(self) -> Dataset:
        """The underlying :class:`~gcp_opt.dataset.Dataset`."""
        return self._dataset

    # -- machines ---------------------------------------------------------
    def machine_info(self, name: str) -> MachineTypeInfo | None:
        """Return machine shape info, or ``None`` if unknown."""
        return self._dataset.machine_types.get(name)

    def machine_names(self) -> list[str]:
        """All machine type names in the dataset, sorted."""
        return sorted(self._dataset.machine_types)

    def limit_for(
        self,
        machine_type: str,
        disk_kind: DiskKind,
        scope: Scope = Scope.ZONAL,
    ) -> MachineTypeDiskLimit | None:
        """Resolve the per-VM disk ceiling for a machine type.

        Resolution order: an explicit per-machine-type row, then the machine's
        family vCPU range applied to its ``guestCpus``.  Returns ``None`` when the
        machine type (or its vCPU count) is unknown.
        """
        direct = self._machine_limits.get((machine_type, disk_kind, scope))
        if direct is not None:
            return direct

        info = self._dataset.machine_types.get(machine_type)
        if info is None or info.guest_cpus is None:
            return None
        family_id = info.family or machine_type.split("-", 1)[0]
        candidates = [
            limit
            for limit in self._dataset.limits.vcpu_limits
            if limit.family_id == family_id
            and limit.disk_kind == disk_kind
            and limit.scope == scope
            and limit.covers(info.guest_cpus)
        ]
        if not candidates:
            return None
        chosen = min(
            candidates,
            key=lambda limit: (
                (limit.vcpus_max - limit.vcpus_min)
                if limit.vcpus_max is not None
                else _UNBOUNDED_WIDTH
            ),
        )
        # Normalize to a MachineTypeDiskLimit so callers see one shape.
        return MachineTypeDiskLimit(
            machine_type=machine_type,
            family=chosen.family,
            family_id=chosen.family_id,
            disk_kind=chosen.disk_kind,
            scope=chosen.scope,
            max_read_iops=chosen.max_read_iops,
            max_write_iops=chosen.max_write_iops,
            max_read_mibps=chosen.max_read_mibps,
            max_write_mibps=chosen.max_write_mibps,
            source=chosen.source,
        )

    # -- prices -----------------------------------------------------------
    def price_sku(
        self,
        disk_kind: DiskKind,
        *,
        region: str | None = None,
        scope: Scope = Scope.ZONAL,
        allow_us_list_price: bool = False,
    ) -> PriceSku | None:
        """Return the capacity SKU for a disk kind, honoring the region.

        The bundled bootstrap price book holds US list prices.  Requesting a
        different region raises unless ``allow_us_list_price`` is set, so a
        São Paulo query cannot silently use US prices.

        Raises:
            PriceUnavailableError: if the region is not covered and fallback is off.
        """
        book = self._dataset.prices
        requested = region or book.region
        sku = find_price(book, disk_kind, scope)
        if sku is None:
            return None
        if requested != book.region and not allow_us_list_price:
            raise PriceUnavailableError(
                f"no price snapshot for region {requested!r}; the bundled book covers "
                f"{book.region!r}. Run `python -m gcp_opt refresh-prices --region "
                f"{requested}` with Cloud Billing credentials, or pass "
                "--allow-us-list-price to accept US list prices."
            )
        return sku

    def disk_model(self, disk_kind: DiskKind, scope: Scope = Scope.ZONAL) -> DiskPerformanceModel:
        """Return the verified size-scaling model for a disk kind.

        Raises:
            UnmodeledDiskKindError: for Hyperdisk kinds (provisioned-performance).
        """
        if scope is not Scope.ZONAL or disk_kind in constants.UNMODELED_DISK_KINDS:
            raise UnmodeledDiskKindError(
                f"no size-scaling performance model for {disk_kind} ({scope}); "
                "Hyperdisk and regional disks are provisioned/covered separately"
            )
        return constants.ZONAL_DISK_MODELS[disk_kind]

    # -- options ----------------------------------------------------------
    def disk_option(
        self,
        machine_type: str,
        disk_kind: DiskKind,
        size_gib: DecimalLike,
        *,
        region: str | None = None,
        scope: Scope = Scope.ZONAL,
        provisioned_iops: DecimalLike | None = None,
        allow_us_list_price: bool = False,
    ) -> DiskOption:
        """Build one priced, performance-bounded option row."""
        size = to_decimal(size_gib)
        model = self.disk_model(disk_kind, scope)
        limit = self.limit_for(machine_type, disk_kind, scope)
        sku = self.price_sku(
            disk_kind, region=region, scope=scope, allow_us_list_price=allow_us_list_price
        )
        if sku is None:
            raise PriceUnavailableError(f"no capacity price found for {disk_kind} ({scope})")

        envelope = achievable_performance(
            model, size, machine_limit=limit, provisioned_iops=provisioned_iops
        )
        book = self._dataset.prices
        effective_region = region or book.region
        note = sku.source.note or ""
        if effective_region != book.region:
            note = f"US list price used for {effective_region} (book region {book.region})"

        return DiskOption(
            machine_type=machine_type,
            region=effective_region,
            disk_kind=disk_kind,
            scope=scope,
            size_gib=size,
            monthly_cost_usd=sku.cost_for(size),
            read_iops=envelope.read_iops,
            write_iops=envelope.write_iops,
            read_mibps=envelope.read_mibps,
            write_mibps=envelope.write_mibps,
            instance_bound=envelope.binding.instance_bound,
            instance_limit_known=limit is not None,
            binding=envelope.binding,
            price_sku_id=sku.sku_id,
            price_note=note,
        )

    def disk_options(
        self,
        machine_type: str,
        sizes_gib: Iterable[DecimalLike],
        *,
        region: str | None = None,
        disk_kinds: Sequence[DiskKind] = (
            DiskKind.PD_STANDARD,
            DiskKind.PD_BALANCED,
            DiskKind.PD_SSD,
        ),
        scope: Scope = Scope.ZONAL,
        allow_us_list_price: bool = False,
    ) -> list[DiskOption]:
        """Build options for a machine type across disk kinds and sizes."""
        options: list[DiskOption] = []
        for kind in disk_kinds:
            for size in sizes_gib:
                options.append(
                    self.disk_option(
                        machine_type,
                        kind,
                        size,
                        region=region,
                        scope=scope,
                        allow_us_list_price=allow_us_list_price,
                    )
                )
        return options


def load_catalog() -> Catalog:
    """Load the bundled snapshots and return a :class:`Catalog`."""
    return Catalog(Dataset.load_bundled())
