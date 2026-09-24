"""Verified, sourced constants that Google does *not* expose through an API.

The linear disk-scaling rules (``IOPS/GiB``, ``MiB/s/GiB``, baseline offsets) are
published only as documentation tables and formulas, so they are pinned here with
a source reference on every model.  Do not edit a number here without updating
its ``source`` and the golden tests in ``tests/test_documented_values.py``.

Primary sources (retrieved 2026-09-24):

* Disk scaling formulas and per-type caps --
  https://cloud.google.com/compute/docs/disks/performance
* Machine-type disk ceilings -- same page, section
  "Persistent Disk performance limits by machine series".
* Disk capacity prices -- https://cloud.google.com/compute/disks-image-pricing and
  the Cloud Billing Catalog API
  (``services/6F81-5844-456A/skus``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from gcp_opt.models import (
    DiskKind,
    DiskPerformanceModel,
    Scope,
    SourceMethod,
    SourceRef,
)

# --------------------------------------------------------------------------- #
# API endpoints and identifiers
# --------------------------------------------------------------------------- #

#: Cloud Billing Catalog service id for Compute Engine.
#: Cross-checked against the Apache libcloud GCE price scraper
#: (``contrib/scrape-gce-prices.py``, ``GCE_SERVICE_ID``).
COMPUTE_ENGINE_SERVICE_ID: Final[str] = "6F81-5844-456A"

BILLING_CATALOG_BASE_URL: Final[str] = "https://cloudbilling.googleapis.com/v1"
COMPUTE_API_BASE_URL: Final[str] = "https://compute.googleapis.com/compute/v1"

#: Maximum page size accepted by ``services.skus.list``.
BILLING_MAX_PAGE_SIZE: Final[int] = 5000

DISK_PERFORMANCE_DOC_URL: Final[str] = "https://cloud.google.com/compute/docs/disks/performance"
MACHINE_TYPES_DOC_URL: Final[str] = "https://cloud.google.com/compute/docs/general-purpose-machines"
DISK_PRICING_DOC_URL: Final[str] = "https://cloud.google.com/compute/disks-image-pricing"

# --------------------------------------------------------------------------- #
# Documented defaults (fallbacks when the Compute API is unavailable)
# --------------------------------------------------------------------------- #

#: Most machine types allow 128 attached Persistent Disks.
DEFAULT_MAX_PERSISTENT_DISKS: Final[int] = 128
#: Most machine types allow 257 TiB (= 263,168 GiB) of total attached disk.
DEFAULT_MAX_TOTAL_SIZE_GIB: Final[Decimal] = Decimal(263168)

# --------------------------------------------------------------------------- #
# Source references
# --------------------------------------------------------------------------- #

_PERF_DOC_TYPE_TABLE = SourceRef(
    url=f"{DISK_PERFORMANCE_DOC_URL}#zonal_pd_limits",
    note=(
        "Tables 'IOPS limits by type for zonal Persistent Disk' and "
        "'Throughput limits by type for zonal Persistent Disk'."
    ),
)
_PERF_DOC_SIZE_FORMULA = SourceRef(
    url=f"{DISK_PERFORMANCE_DOC_URL}#size_price_performance",
    note="Formulas under 'Performance limits by size for zonal ... Persistent Disk'.",
)
_PERF_DOC_MACHINE_TABLE = SourceRef(
    url=f"{DISK_PERFORMANCE_DOC_URL}#machine-type-disk-limits",
    note="Section 'Persistent Disk performance limits by machine series'.",
)


def perf_doc_machine_table_source() -> SourceRef:
    """Return the source reference for the machine-series performance tables."""
    return _PERF_DOC_MACHINE_TABLE


# --------------------------------------------------------------------------- #
# The static scaling matrix (the thing the web agent tried to hand-write)
# --------------------------------------------------------------------------- #
#
# Documented formulas (x = combined GiB of all volumes of that type on the VM):
#
#   pd-standard  Read IOPS   MIN(instance_limit, 0.75x)         cap   7,500
#                Write IOPS  MIN(instance_limit, 1.5x)          cap  15,000
#                Throughput  MIN(instance_limit, 0.12x)         cap 1,200 read / 400 write
#   pd-balanced  IOPS        MIN(instance_limit, 6x + 3,000)    cap  80,000
#                Throughput  MIN(instance_limit, 0.28x + 140)   cap 1,200
#   pd-ssd       IOPS        MIN(instance_limit, 30x + 6,000)   cap 100,000
#                Throughput  MIN(instance_limit, 0.48x + 240)   cap 1,200
#   pd-extreme   IOPS provisioned 2,500..120,000; throughput = IOPS * 256 KiB/s
#
# NOTE for reviewers: the widely repeated "baseline_iops: 1200 balanced / 3000 ssd"
# snippet is WRONG.  The documented offsets are +3,000 (balanced) and +6,000 (ssd)
# and apply to *both* IOPS and throughput (throughput offsets 140 / 240 MiB/s).

ZONAL_DISK_MODELS: Final[dict[DiskKind, DiskPerformanceModel]] = {
    DiskKind.PD_STANDARD: DiskPerformanceModel(
        disk_kind=DiskKind.PD_STANDARD,
        scope=Scope.ZONAL,
        iops_per_gib_read=Decimal("0.75"),
        iops_per_gib_write=Decimal("1.5"),
        throughput_mibps_per_gib_read=Decimal("0.12"),
        throughput_mibps_per_gib_write=Decimal("0.12"),
        max_read_iops=Decimal(7500),
        max_write_iops=Decimal(15000),
        max_read_mibps=Decimal(1200),
        max_write_mibps=Decimal(400),
        iops_shared_between_directions=True,
        throughput_shared_between_directions=True,
        source=_PERF_DOC_TYPE_TABLE,
    ),
    DiskKind.PD_BALANCED: DiskPerformanceModel(
        disk_kind=DiskKind.PD_BALANCED,
        scope=Scope.ZONAL,
        iops_per_gib_read=Decimal(6),
        iops_per_gib_write=Decimal(6),
        iops_base_read=Decimal(3000),
        iops_base_write=Decimal(3000),
        throughput_mibps_per_gib_read=Decimal("0.28"),
        throughput_mibps_per_gib_write=Decimal("0.28"),
        throughput_mibps_base_read=Decimal(140),
        throughput_mibps_base_write=Decimal(140),
        max_read_iops=Decimal(80000),
        max_write_iops=Decimal(80000),
        max_read_mibps=Decimal(1200),
        max_write_mibps=Decimal(1200),
        iops_shared_between_directions=True,
        throughput_shared_between_directions=False,
        source=_PERF_DOC_SIZE_FORMULA,
    ),
    DiskKind.PD_SSD: DiskPerformanceModel(
        disk_kind=DiskKind.PD_SSD,
        scope=Scope.ZONAL,
        iops_per_gib_read=Decimal(30),
        iops_per_gib_write=Decimal(30),
        iops_base_read=Decimal(6000),
        iops_base_write=Decimal(6000),
        throughput_mibps_per_gib_read=Decimal("0.48"),
        throughput_mibps_per_gib_write=Decimal("0.48"),
        throughput_mibps_base_read=Decimal(240),
        throughput_mibps_base_write=Decimal(240),
        max_read_iops=Decimal(100000),
        max_write_iops=Decimal(100000),
        max_read_mibps=Decimal(1200),
        max_write_mibps=Decimal(1200),
        iops_shared_between_directions=True,
        throughput_shared_between_directions=False,
        source=_PERF_DOC_SIZE_FORMULA,
    ),
    DiskKind.PD_EXTREME: DiskPerformanceModel(
        disk_kind=DiskKind.PD_EXTREME,
        scope=Scope.ZONAL,
        max_read_iops=Decimal(120000),
        max_write_iops=Decimal(120000),
        max_read_mibps=Decimal(4000),
        max_write_mibps=Decimal(3000),
        iops_shared_between_directions=True,
        throughput_shared_between_directions=False,
        provisioned_iops=True,
        provisioned_iops_min=Decimal(2500),
        provisioned_iops_max=Decimal(120000),
        # "throughput scales ... at a rate of 256 KiB/s of throughput per I/O"
        throughput_mibps_per_provisioned_iop=Decimal("0.25"),
        source=_PERF_DOC_TYPE_TABLE,
    ),
}

#: Hyperdisk is provisioned-performance based, not size-scaling based, and is
#: documented on a different page.  It is intentionally *not* modeled here yet;
#: callers that request it get an explicit error instead of invented numbers.
UNMODELED_DISK_KINDS: Final[frozenset[DiskKind]] = frozenset(
    {
        DiskKind.HYPERDISK_BALANCED,
        DiskKind.HYPERDISK_EXTREME,
        DiskKind.HYPERDISK_THROUGHPUT,
    }
)

# --------------------------------------------------------------------------- #
# Cloud Billing SKU matching
# --------------------------------------------------------------------------- #
#
# Catalog SKU descriptions have varied over time ("Balanced PD Capacity" in the
# catalog vs "Balanced provisioned space" on the pricing page), so match on a
# tuple of lowercase substrings and exclude non-capacity SKUs explicitly.

DISK_SKU_MATCH: Final[dict[DiskKind, tuple[str, ...]]] = {
    DiskKind.PD_STANDARD: ("standard pd capacity", "standard provisioned space"),
    DiskKind.PD_BALANCED: ("balanced pd capacity", "balanced provisioned space"),
    DiskKind.PD_SSD: (
        "ssd pd capacity",
        "ssd backed pd capacity",
        "ssd provisioned space",
    ),
    DiskKind.PD_EXTREME: ("extreme pd capacity", "extreme provisioned space"),
    DiskKind.HYPERDISK_BALANCED: ("hyperdisk balanced provisioned space",),
    DiskKind.HYPERDISK_EXTREME: ("hyperdisk extreme provisioned space",),
    DiskKind.HYPERDISK_THROUGHPUT: ("hyperdisk throughput provisioned space",),
}

#: Substrings that mark a SKU as *not* plain disk capacity.
SKU_EXCLUDE_SUBSTRINGS: Final[tuple[str, ...]] = (
    "snapshot",
    "instant",
    "replication",
    "recycle",
    "multi-writer",
    "local ssd",
    "local storage",
    "asynchronous",
    "storage pool",
    "high availability",
)

#: Provisioned-IOPS SKUs (Extreme PD and Hyperdisk).  ``pd-extreme`` charges for
#: provisioned IOPS in addition to capacity.
DISK_IOPS_SKU_MATCH: Final[dict[DiskKind, tuple[str, ...]]] = {
    DiskKind.PD_EXTREME: ("extreme provisioned iops",),
    DiskKind.HYPERDISK_EXTREME: ("hyperdisk extreme provisioned iops",),
    DiskKind.HYPERDISK_BALANCED: ("hyperdisk balanced provisioned iops",),
}

#: Provisioned-throughput SKUs (Hyperdisk only).
DISK_THROUGHPUT_SKU_MATCH: Final[dict[DiskKind, tuple[str, ...]]] = {
    DiskKind.HYPERDISK_BALANCED: ("hyperdisk balanced provisioned throughput",),
    DiskKind.HYPERDISK_THROUGHPUT: ("hyperdisk throughput provisioned throughput",),
}

#: Marker distinguishing regional (replicated) disk SKUs.
REGIONAL_SKU_MARKER: Final[str] = "regional"

SOURCE_METHOD_DOCS: Final[SourceMethod] = SourceMethod.DOCS_SCRAPE
