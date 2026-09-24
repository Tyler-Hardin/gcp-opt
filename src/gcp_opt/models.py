"""Typed domain model for the GCP disk optimizer data layer.

Every object that crosses a boundary (HTTP response, committed snapshot file,
CLI argument) is validated by :mod:`pydantic` here.  Models are frozen so a
snapshot handed to the optimizer cannot be mutated accidentally mid-solve.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from gcp_opt import units

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class DiskKind(StrEnum):
    """Persistent Disk / Hyperdisk families this package understands."""

    PD_STANDARD = "pd-standard"
    PD_BALANCED = "pd-balanced"
    PD_SSD = "pd-ssd"
    PD_EXTREME = "pd-extreme"
    HYPERDISK_BALANCED = "hyperdisk-balanced"
    HYPERDISK_EXTREME = "hyperdisk-extreme"
    HYPERDISK_THROUGHPUT = "hyperdisk-throughput"


class Scope(StrEnum):
    """Whether a disk is zonal or regional (replicated)."""

    ZONAL = "zonal"
    REGIONAL = "regional"


class SkuRole(StrEnum):
    """Which part of a disk's cost a SKU prices.

    Persistent Disks are priced by capacity alone.  Extreme PD (and Hyperdisk) also
    charge for *provisioned* performance, which is a separate SKU.
    """

    CAPACITY = "capacity"
    PROVISIONED_IOPS = "provisioned_iops"
    PROVISIONED_THROUGHPUT = "provisioned_throughput"


class SnapshotKind(StrEnum):
    """Kinds of committed snapshot files."""

    DISK_PERFORMANCE = "disk-performance"
    MACHINE_TYPE_LIMITS = "machine-type-disk-limits"
    MACHINE_TYPES = "machine-types"
    PRICES = "prices"
    MACHINE_PRICES = "machine-prices"


class SourceMethod(StrEnum):
    """How a record or snapshot was obtained."""

    CLOUD_BILLING_CATALOG = "cloud_billing_catalog_v1"
    COMPUTE_MACHINE_TYPES = "compute_machine_types_v1"
    DOCS_SCRAPE = "google_cloud_docs_scrape"
    MANUAL = "manual"


class LimitSource(StrEnum):
    """Which constraint produced a performance ceiling."""

    INSTANCE = "instance_machine_type"
    DISK_MODEL = "disk_size_linear_model"
    DISK_TYPE_CAP = "disk_type_cap"


# --------------------------------------------------------------------------- #
# Provenance / snapshot envelopes
# --------------------------------------------------------------------------- #


class SourceRef(BaseModel):
    """Where a single fact came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    note: str | None = None


class Provenance(BaseModel):
    """Where a whole snapshot file came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: SourceMethod
    source_url: str
    retrieved_at: datetime
    generator: str
    notes: str | None = None


PayloadT = TypeVar("PayloadT")


class Snapshot(BaseModel, Generic[PayloadT]):
    """A versioned, hash-verified bundle of records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    kind: SnapshotKind
    provenance: Provenance
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: PayloadT


# --------------------------------------------------------------------------- #
# Disk performance model (the "static constants" that Google does not expose)
# --------------------------------------------------------------------------- #


class DiskPerformanceModel(BaseModel):
    """Linear size-scaling rules plus hard caps for one disk type.

    The achievable performance of a Persistent Disk of size ``x`` GiB is::

        IOPS       = MIN(instance_limit, per_gib * x + base, type_cap)
        Throughput = MIN(instance_limit, per_gib * x + base, type_cap)

    which mirrors the formulas published on
    https://cloud.google.com/compute/docs/disks/performance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    disk_kind: DiskKind
    scope: Scope

    iops_per_gib_read: Decimal = Decimal(0)
    iops_per_gib_write: Decimal = Decimal(0)
    iops_base_read: Decimal = Decimal(0)
    iops_base_write: Decimal = Decimal(0)

    throughput_mibps_per_gib_read: Decimal = Decimal(0)
    throughput_mibps_per_gib_write: Decimal = Decimal(0)
    throughput_mibps_base_read: Decimal = Decimal(0)
    throughput_mibps_base_write: Decimal = Decimal(0)

    max_read_iops: Decimal | None = None
    max_write_iops: Decimal | None = None
    max_read_mibps: Decimal | None = None
    max_write_mibps: Decimal | None = None

    min_size_gib: Decimal | None = None
    max_size_gib: Decimal | None = None

    #: Read and write IOPS share one budget for *all* PD types (half-duplex).
    iops_shared_between_directions: bool = True
    #: Standard PD shares read+write throughput; Balanced/SSD do not.
    throughput_shared_between_directions: bool = False

    #: Extreme PD is provisioned by IOPS rather than by capacity.
    provisioned_iops: bool = False
    provisioned_iops_min: Decimal | None = None
    provisioned_iops_max: Decimal | None = None
    #: Throughput obtained per provisioned IOPS (Extreme PD: 256 KiB/s per IO).
    throughput_mibps_per_provisioned_iop: Decimal | None = None

    source: SourceRef

    def scaled_iops(self, size_gib: Decimal, *, write: bool) -> Decimal:
        """Return the size-scaled IOPS before the instance/type caps apply."""
        per_gib = self.iops_per_gib_write if write else self.iops_per_gib_read
        base = self.iops_base_write if write else self.iops_base_read
        return per_gib * size_gib + base

    def scaled_mibps(self, size_gib: Decimal, *, write: bool) -> Decimal:
        """Return the size-scaled throughput before the instance/type caps apply."""
        per_gib = (
            self.throughput_mibps_per_gib_write if write else self.throughput_mibps_per_gib_read
        )
        base = self.throughput_mibps_base_write if write else self.throughput_mibps_base_read
        return per_gib * size_gib + base

    def type_cap_iops(self, *, write: bool) -> Decimal | None:
        """Return the per-disk-type IOPS cap, if any."""
        return self.max_write_iops if write else self.max_read_iops

    def type_cap_mibps(self, *, write: bool) -> Decimal | None:
        """Return the per-disk-type throughput cap, if any."""
        return self.max_write_mibps if write else self.max_read_mibps


class BindingConstraints(BaseModel):
    """Which constraint produced each ceiling of a :class:`PerformanceEnvelope`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_iops: LimitSource
    write_iops: LimitSource
    read_mibps: LimitSource
    write_mibps: LimitSource

    @property
    def instance_bound(self) -> bool:
        """True if the machine type is the binding limit in any direction."""
        return LimitSource.INSTANCE in {
            self.read_iops,
            self.write_iops,
            self.read_mibps,
            self.write_mibps,
        }


class PerformanceEnvelope(BaseModel):
    """Achievable read/write performance for one concrete disk configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_iops: Decimal
    write_iops: Decimal
    read_mibps: Decimal
    write_mibps: Decimal
    binding: BindingConstraints


# --------------------------------------------------------------------------- #
# Machine types
# --------------------------------------------------------------------------- #


class MachineTypeDiskLimit(BaseModel):
    """Per-machine-type ceiling on the aggregate of all PDs of one disk type.

    These are *not* returned by the Compute Engine ``machineTypes`` API; they are
    published as tables in the Persistent Disk performance documentation and are
    captured here from that documentation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    machine_type: str
    family: str
    family_id: str
    disk_kind: DiskKind
    scope: Scope
    max_read_iops: Decimal
    max_write_iops: Decimal
    max_read_mibps: Decimal
    max_write_mibps: Decimal
    source: SourceRef


class VCpuDiskLimit(BaseModel):
    """Per-vCPU-count ceiling for machine families documented that way.

    C2/C3/N1/N2/N2D/E2/T2A/T2D/Z3 publish one row per vCPU count or range instead
    of one row per machine type.  The catalog resolves a concrete machine type by
    looking up its family and ``guestCpus`` against these ranges.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    family: str
    family_id: str
    vcpu_label: str
    vcpus_min: int
    #: ``None`` means unbounded (the docs' "64 or more" rows).
    vcpus_max: int | None
    disk_kind: DiskKind
    scope: Scope
    max_read_iops: Decimal
    max_write_iops: Decimal
    max_read_mibps: Decimal
    max_write_mibps: Decimal
    source: SourceRef

    def covers(self, vcpus: int) -> bool:
        """True if ``vcpus`` falls inside this row's range."""
        return vcpus >= self.vcpus_min and (self.vcpus_max is None or vcpus <= self.vcpus_max)


class MachineTypeLimitTable(BaseModel):
    """Both shapes of the published machine-series performance tables."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    machine_type_limits: tuple[MachineTypeDiskLimit, ...]
    vcpu_limits: tuple[VCpuDiskLimit, ...]


class MachineTypeInfo(BaseModel):
    """Machine shape and capability ceilings.

    ``guest_cpus``/``memory_gb``/``maximum_persistent_disks``/
    ``maximum_total_size_gib`` come from the Compute Engine ``machineTypes`` API
    (or the docs fallback).  ``maximum_total_size_gib`` is normalized to GiB:
    Google's API field is named ``maximumPersistentDisksSizeGb`` but returns
    gibibytes (257 TiB is returned as ``263168``).

    ``network_egress_gbps``/``network_tier1_egress_gbps`` come from the machine
    family documentation, because the Compute API does **not** expose network
    bandwidth on ``MachineType``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    family: str | None = None
    guest_cpus: int | None = None
    memory_gb: Decimal | None = None
    maximum_persistent_disks: int | None = None
    maximum_total_size_gib: Decimal | None = None
    network_egress_gbps: Decimal | None = None
    network_tier1_egress_gbps: Decimal | None = None
    zone: str | None = None
    architecture: str | None = None
    is_shared_cpu: bool | None = None
    source: SourceRef

    @property
    def memory_gib(self) -> Decimal | None:
        """Memory in GiB (the docs' ``Memory (GB)`` values are GiB-priced binary)."""
        return self.memory_gb


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #


class PriceTier(BaseModel):
    """One tier of a Cloud Billing SKU price.

    ``unit_price`` is the price per ``usage_unit`` (e.g. per GiB-hour) that
    applies from ``start_usage_amount`` up to the next tier's start.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_usage_amount: Decimal = Decimal(0)
    unit_price: Decimal


class PriceSku(BaseModel):
    """A Cloud Billing Catalog SKU relevant to disk capacity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sku_id: str
    description: str
    resource_family: str
    usage_type: str
    usage_unit: str
    service_regions: tuple[str, ...]
    currency_code: str
    tiers: tuple[PriceTier, ...]
    source: SourceRef
    role: SkuRole = SkuRole.CAPACITY

    def cost_for(self, quantity: Decimal, *, hours: Decimal = units.HOURS_PER_MONTH) -> Decimal:
        """Return the cost of ``quantity`` usage units held for ``hours`` hours.

        Handles tiered pricing (for example Standard PD's first-30-GiB-free tier).
        """
        total = Decimal(0)
        ordered = sorted(self.tiers, key=lambda tier: tier.start_usage_amount)
        for index, tier in enumerate(ordered):
            start = tier.start_usage_amount
            if quantity <= start:
                break
            end = ordered[index + 1].start_usage_amount if index + 1 < len(ordered) else None
            upper = quantity if end is None else min(quantity, end)
            total += (upper - start) * tier.unit_price * hours
        return total

    def monthly_cost_per_gib(self) -> Decimal:
        """Return the on-demand cost of 1 GiB for one 730-hour month."""
        return self.cost_for(Decimal(1))

    def flat_hourly_price(self) -> Decimal:
        """Return the per-unit hourly price, rejecting genuinely tiered SKUs.

        Raises:
            ValueError: if the SKU has more than one price tier (use
                :meth:`cost_for` for tiered SKUs such as Standard PD, whose first
                30 GiB per month are free).
        """
        if len(self.tiers) != 1:
            raise ValueError(
                f"SKU {self.sku_id} has {len(self.tiers)} price tiers; "
                "use cost_for() instead of flat_hourly_price()"
            )
        return self.tiers[0].unit_price


class PriceBook(BaseModel):
    """All disk-capacity prices fetched for one currency and region."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    currency_code: str
    region: str
    skus: tuple[PriceSku, ...]
    provenance: Provenance


class DiskOption(BaseModel):
    """One concrete, priced, performance-bounded disk configuration.

    This is the row an optimizer consumes: cost plus the achievable read/write
    IOPS and throughput after intersecting the size-scaled disk model with the
    machine-type ceiling.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    machine_type: str
    region: str
    disk_kind: DiskKind
    scope: Scope
    size_gib: Decimal
    monthly_cost_usd: Decimal
    read_iops: Decimal
    write_iops: Decimal
    read_mibps: Decimal
    write_mibps: Decimal
    #: True when the machine-type ceiling is the binding limit in some direction.
    instance_bound: bool
    #: False when no machine-type ceiling was found for this machine type.
    instance_limit_known: bool
    binding: BindingConstraints
    price_sku_id: str
    price_note: str
    #: Provisioned IOPS for provisioned-performance disks (``pd-extreme``), else None.
    provisioned_iops: Decimal | None = None
    #: Cost of capacity alone (equals ``monthly_cost_usd`` for non-provisioned disks).
    capacity_monthly_cost_usd: Decimal | None = None
    #: Cost of provisioned IOPS alone, or None for non-provisioned disks.
    provisioned_iops_monthly_cost_usd: Decimal | None = None

    @property
    def cost_per_gib_month(self) -> Decimal:
        """Effective blended cost per GiB per month."""
        if self.size_gib == 0:
            return Decimal(0)
        return self.monthly_cost_usd / self.size_gib


# --------------------------------------------------------------------------- #
# Machine pricing (optional layer) and combined configuration options
# --------------------------------------------------------------------------- #


class MachinePrice(BaseModel):
    """On-demand hourly price of one machine type in one region.

    Instance pricing is an **optional** layer: it is not in the bundled bootstrap
    because Google's VM pricing page is rendered client-side.  Populate it with
    ``python -m gcp_opt refresh-machine-prices`` (Billing Catalog) or your own
    source; when it is absent the optimizer reports ``cost_basis="disk_only"``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    machine_type: str
    region: str
    hourly_usd: Decimal
    source: SourceRef

    def monthly_usd(self, hours: Decimal = units.HOURS_PER_MONTH) -> Decimal:
        """Return the 730-hour monthly price."""
        return self.hourly_usd * hours


class CostBasis(StrEnum):
    """Which components a :class:`ConfigOption` cost figure includes."""

    MACHINE_AND_DISK = "machine_and_disk"
    DISK_ONLY = "disk_only"
    MACHINE_ONLY = "machine_only"
    UNKNOWN = "unknown"


class Objective(StrEnum):
    """A single axis to optimize over.

    Capability axes (``max_vcpus``/``max_memory``/``max_network``/``max_disk_size``)
    answer "what is the biggest X I can get"; cost and throughput axes answer the
    classic sizing questions.
    """

    MIN_COST = "min_cost"
    MAX_VCPUS = "max_vcpus"
    MAX_MEMORY = "max_memory"
    MAX_NETWORK = "max_network"
    MAX_DISK_SIZE = "max_disk_size"
    MAX_DISK_READ = "max_disk_read"
    MAX_DISK_WRITE = "max_disk_write"
    MAX_DISK_BALANCED = "max_disk_balanced"
    MAX_DISK_IOPS = "max_disk_iops"


#: Objectives that require a disk to be attached.
DISK_OBJECTIVES: frozenset[Objective] = frozenset(
    {
        Objective.MAX_DISK_SIZE,
        Objective.MAX_DISK_READ,
        Objective.MAX_DISK_WRITE,
        Objective.MAX_DISK_BALANCED,
        Objective.MAX_DISK_IOPS,
        Objective.MIN_COST,
    }
)


class ConfigOption(BaseModel):
    """A machine, optionally with one attached disk configuration.

    This is the generalized optimizer row: the machine contributes vCPU, memory,
    network bandwidth and disk-count/size ceilings; the disk contributes capacity,
    IOPS and throughput; ``monthly_cost_usd`` sums whichever components are priced.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    machine: MachineTypeInfo
    disk: DiskOption | None = None
    machine_monthly_cost_usd: Decimal | None = None
    monthly_cost_usd: Decimal | None = None
    cost_basis: CostBasis = CostBasis.UNKNOWN
    #: Human-readable note about how the cost was assembled (or why it is partial).
    cost_note: str = ""

    @property
    def machine_type(self) -> str:
        """Machine type name."""
        return self.machine.name

    @property
    def guest_cpus(self) -> int | None:
        """The vCPU count."""
        return self.machine.guest_cpus

    @property
    def memory_gb(self) -> Decimal | None:
        """Memory in GiB (GCP prices memory per GB but reports binary values)."""
        return self.machine.memory_gb

    @property
    def network_egress_gbps(self) -> Decimal | None:
        """Default (or maximum) egress bandwidth in Gbps."""
        return self.machine.network_egress_gbps

    @property
    def disk_kind(self) -> DiskKind | None:
        """Attached disk kind, if any."""
        return self.disk.disk_kind if self.disk else None

    @property
    def size_gib(self) -> Decimal | None:
        """Attached disk capacity in GiB, if any."""
        return self.disk.size_gib if self.disk else None

    @property
    def read_mibps(self) -> Decimal | None:
        """Achievable disk read throughput, if a disk is attached."""
        return self.disk.read_mibps if self.disk else None

    @property
    def write_mibps(self) -> Decimal | None:
        """Achievable disk write throughput, if a disk is attached."""
        return self.disk.write_mibps if self.disk else None

    @property
    def read_iops(self) -> Decimal | None:
        """Achievable disk read IOPS, if a disk is attached."""
        return self.disk.read_iops if self.disk else None

    @property
    def write_iops(self) -> Decimal | None:
        """Achievable disk write IOPS, if a disk is attached."""
        return self.disk.write_iops if self.disk else None

    @property
    def disk_monthly_cost_usd(self) -> Decimal | None:
        """Disk-only monthly cost, if a disk is attached."""
        return self.disk.monthly_cost_usd if self.disk else None
