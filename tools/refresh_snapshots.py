"""Regenerate the committed snapshots from official Google Cloud sources.

Usage::

    poetry run python tools/refresh_snapshots.py

Writes:

* ``src/gcp_opt/data/machine_type_disk_limits.json`` -- per-machine-type disk ceilings
  scraped from the Persistent Disk performance documentation.
* ``src/gcp_opt/data/machine_types.json`` -- vCPU/memory/disk-count ceilings scraped
  from the machine-family documentation (a live Compute API refresh supersedes this).
* ``src/gcp_opt/data/disk_prices.json`` -- US list prices scraped from the disk pricing
  page, used only as a bootstrap until the Cloud Billing Catalog API is called.
* ``tests/fixtures/doc_size_tables.json`` -- documented per-size tables used as golden
  test fixtures.

This is a dev tool: it never runs at import time and is not part of the runtime path.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import doc_parse  # noqa: E402  (path set above)
from gcp_opt import constants, units  # noqa: E402
from gcp_opt.models import (  # noqa: E402
    DiskKind,
    MachineTypeDiskLimit,
    MachineTypeInfo,
    MachineTypeLimitTable,
    PriceBook,
    PriceSku,
    PriceTier,
    Provenance,
    Scope,
    SkuRole,
    SnapshotKind,
    SourceMethod,
    SourceRef,
    VCpuDiskLimit,
)
from gcp_opt.snapshot import DATA_DIR, GENERATOR, build_snapshot, write_snapshot  # noqa: E402

PERF_DOC_URL = constants.DISK_PERFORMANCE_DOC_URL
PRICING_DOC_URL = constants.DISK_PRICING_DOC_URL
MACHINE_DOC_URLS = (
    "https://docs.cloud.google.com/compute/docs/general-purpose-machines",
    "https://docs.cloud.google.com/compute/docs/compute-optimized-machines",
    "https://docs.cloud.google.com/compute/docs/memory-optimized-machines",
    "https://docs.cloud.google.com/compute/docs/accelerator-optimized-machines",
    "https://docs.cloud.google.com/compute/docs/storage-optimized-machines",
)
USER_AGENT = "gcp-opt-snapshot-refresh/0.1 (+https://cloud.google.com/compute/docs/disks/performance)"


def fetch(url: str) -> str:
    """Download a docs page as text."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return bytes(response.read()).decode("utf-8", errors="replace")


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


# --------------------------------------------------------------------------- #
# Machine-type disk limits
# --------------------------------------------------------------------------- #
def refresh_machine_type_limits(html: str) -> Path:
    """Parse and write per-machine-type and per-vCPU disk ceilings."""
    machine_records, vcpu_records = doc_parse.parse_machine_series_limits(html)
    total = len(machine_records) + len(vcpu_records)
    if total < 150:
        raise SystemExit(
            f"refusing to write machine-type limits: only {total} rows parsed "
            "(expected several hundred); the docs markup probably changed"
        )
    source = constants.perf_doc_machine_table_source()

    def limits(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "family": str(record["family"]),
            "family_id": str(record["family_id"]),
            "disk_kind": DiskKind(str(record["disk_kind"])),
            "scope": Scope.ZONAL,
            "max_read_iops": Decimal(str(record["max_read_iops"])),
            "max_write_iops": Decimal(str(record["max_write_iops"])),
            "max_read_mibps": Decimal(str(record["max_read_mibps"])),
            "max_write_mibps": Decimal(str(record["max_write_mibps"])),
            "source": source,
        }

    machine_limits = [
        MachineTypeDiskLimit(machine_type=str(record["machine_type"]), **limits(record))
        for record in machine_records
    ]
    vcpu_limits = [
        VCpuDiskLimit(
            vcpu_label=str(record["vcpu_label"]),
            vcpus_min=int(str(record["vcpus_min"])),
            vcpus_max=int(str(record["vcpus_max"])) if record["vcpus_max"] is not None else None,
            **limits(record),
        )
        for record in vcpu_records
    ]
    table = MachineTypeLimitTable(
        machine_type_limits=tuple(machine_limits), vcpu_limits=tuple(vcpu_limits)
    )
    provenance = Provenance(
        method=SourceMethod.DOCS_SCRAPE,
        source_url=PERF_DOC_URL,
        retrieved_at=_now(),
        generator=GENERATOR,
        notes=(
            "Per-machine-type and per-vCPU Persistent Disk ceilings. The Compute Engine "
            "machineTypes API does not expose disk IOPS/throughput, so this table is the "
            "authoritative source for the VM bottleneck."
        ),
    )
    path = DATA_DIR / "machine_type_disk_limits.json"
    write_snapshot(
        build_snapshot(table, kind=SnapshotKind.MACHINE_TYPE_LIMITS, provenance=provenance), path
    )
    print(
        f"wrote {path.relative_to(REPO_ROOT)} "
        f"({len(machine_limits)} machine rows, {len(vcpu_limits)} vCPU rows)"
    )
    return path


# --------------------------------------------------------------------------- #
# Machine shapes
# --------------------------------------------------------------------------- #
def refresh_machine_types(pages: dict[str, str]) -> Path:
    """Parse and write machine shapes from the machine-family docs pages."""
    merged: dict[str, dict[str, Any]] = {}
    for url, html in pages.items():
        for record in doc_parse.parse_machine_specs(html):
            name = str(record["name"])
            target = merged.setdefault(
                name, {"name": name, "family": record.get("family"), "urls": []}
            )
            target["urls"].append(url)
            for key, value in record.items():
                if key in {"name", "family"} or value is None:
                    continue
                target[key] = value
    if len(merged) < 50:
        raise SystemExit(
            f"refusing to write machine types: only {len(merged)} parsed from {len(pages)} pages"
        )
    infos: list[MachineTypeInfo] = []
    for name, record in sorted(merged.items()):
        tib = record.get("maximum_total_size_tib")
        maximum_total = (
            units.tib_to_gib(tib) if isinstance(tib, (int, str, Decimal)) else None
        )
        urls = record.get("urls")
        first_url = urls[0] if isinstance(urls, list) and urls else MACHINE_DOC_URLS[0]
        infos.append(
            MachineTypeInfo(
                name=name,
                family=str(record["family"]) if record.get("family") else None,
                guest_cpus=int(str(record["guest_cpus"])) if record.get("guest_cpus") else None,
                memory_gb=Decimal(str(record["memory_gb"])) if record.get("memory_gb") else None,
                maximum_persistent_disks=(
                    int(str(record["maximum_persistent_disks"]))
                    if record.get("maximum_persistent_disks")
                    else None
                ),
                maximum_total_size_gib=maximum_total,
                source=SourceRef(
                    url=str(first_url),
                    note=(
                        "Scraped machine family docs; refresh with the Compute API "
                        "for exact values."
                    ),
                ),
            )
        )
    provenance = Provenance(
        method=SourceMethod.DOCS_SCRAPE,
        source_url=MACHINE_DOC_URLS[0],
        retrieved_at=_now(),
        generator=GENERATOR,
        notes="Machine shapes scraped from machine-family docs; the Compute API is authoritative.",
    )
    path = DATA_DIR / "machine_types.json"
    write_snapshot(
        build_snapshot(infos, kind=SnapshotKind.MACHINE_TYPES, provenance=provenance), path
    )
    print(f"wrote {path.relative_to(REPO_ROOT)} ({len(infos)} rows)")
    return path


# --------------------------------------------------------------------------- #
# Bootstrap prices
# --------------------------------------------------------------------------- #
_PRICE_LABELS: dict[str, tuple[DiskKind, Scope, SkuRole]] = {
    "standard provisioned space": (DiskKind.PD_STANDARD, Scope.ZONAL, SkuRole.CAPACITY),
    "balanced provisioned space": (DiskKind.PD_BALANCED, Scope.ZONAL, SkuRole.CAPACITY),
    "ssd provisioned space": (DiskKind.PD_SSD, Scope.ZONAL, SkuRole.CAPACITY),
    "extreme provisioned space": (DiskKind.PD_EXTREME, Scope.ZONAL, SkuRole.CAPACITY),
    "extreme provisioned iops": (DiskKind.PD_EXTREME, Scope.ZONAL, SkuRole.PROVISIONED_IOPS),
    "regional standard provisioned space": (DiskKind.PD_STANDARD, Scope.REGIONAL, SkuRole.CAPACITY),
    "regional balanced provisioned space": (DiskKind.PD_BALANCED, Scope.REGIONAL, SkuRole.CAPACITY),
    "regional ssd provisioned space": (DiskKind.PD_SSD, Scope.REGIONAL, SkuRole.CAPACITY),
    "hyperdisk balanced provisioned space": (
        DiskKind.HYPERDISK_BALANCED,
        Scope.ZONAL,
        SkuRole.CAPACITY,
    ),
    "hyperdisk balanced provisioned iops": (
        DiskKind.HYPERDISK_BALANCED,
        Scope.ZONAL,
        SkuRole.PROVISIONED_IOPS,
    ),
    "hyperdisk balanced provisioned throughput": (
        DiskKind.HYPERDISK_BALANCED,
        Scope.ZONAL,
        SkuRole.PROVISIONED_THROUGHPUT,
    ),
    "hyperdisk extreme provisioned space": (
        DiskKind.HYPERDISK_EXTREME,
        Scope.ZONAL,
        SkuRole.CAPACITY,
    ),
    "hyperdisk extreme provisioned iops": (
        DiskKind.HYPERDISK_EXTREME,
        Scope.ZONAL,
        SkuRole.PROVISIONED_IOPS,
    ),
    "hyperdisk throughput provisioned space": (
        DiskKind.HYPERDISK_THROUGHPUT,
        Scope.ZONAL,
        SkuRole.CAPACITY,
    ),
    "hyperdisk throughput provisioned throughput": (
        DiskKind.HYPERDISK_THROUGHPUT,
        Scope.ZONAL,
        SkuRole.PROVISIONED_THROUGHPUT,
    ),
}

_GIB_HOUR_MARKER = "/ 1 gibibyte hour"
_HOUR_MARKER = "/ 1 hour"


def _price_unit(cell: str) -> str | None:
    """Return the billing unit a pricing cell is quoted in."""
    lowered = cell.lower()
    if _GIB_HOUR_MARKER in lowered:
        return "gibibyte hour"
    if _HOUR_MARKER in lowered:
        return "hour"
    return None


def _tiers_from_price_cell(cell: str) -> tuple[PriceTier, ...]:
    """Extract hourly price tiers from a pricing-table cell.

    Standard PD is listed as a free first tier plus a paid tier; everything else is a
    single flat rate.  ``$0.000054795 / 1 gibibyte hour`` becomes one flat tier and
    ``$0.000089041 / 1 hour`` (Extreme provisioned IOPS) likewise.
    """
    if _price_unit(cell) is None:
        return ()
    prices: list[Decimal] = []
    for fragment in cell.lower().split("$")[1:]:
        number = fragment.strip().split("/")[0].strip().split()[0]
        try:
            prices.append(Decimal(number.replace(",", "")))
        except InvalidOperation:
            continue
    if not prices:
        return ()
    if len(prices) == 1:
        return (PriceTier(start_usage_amount=Decimal(0), unit_price=prices[0]),)
    # Tiered listing (Standard PD: free up to 30 GiB, then the paid price).
    return (
        PriceTier(start_usage_amount=Decimal(0), unit_price=prices[0]),
        PriceTier(start_usage_amount=Decimal(30), unit_price=prices[-1]),
    )


def parse_disk_prices(
    html: str,
) -> list[tuple[str, tuple[DiskKind, Scope, SkuRole], str, tuple[PriceTier, ...]]]:
    """Find provisioned-space / provisioned-IOPS price rows in the disk pricing page."""
    results: list[tuple[str, tuple[DiskKind, Scope, SkuRole], str, tuple[PriceTier, ...]]] = []
    for block in doc_parse.parse_blocks(html):
        if block.kind != "table" or not block.headers:
            continue
        if doc_parse.normalize_header(block.headers[0]) != "type":
            continue
        for row in block.rows:
            if len(row) < 2:
                continue
            label = doc_parse.normalize_header(row[0])
            mapping = _PRICE_LABELS.get(label)
            if mapping is None:
                continue
            unit = _price_unit(row[1])
            tiers = _tiers_from_price_cell(row[1])
            if unit is not None and tiers:
                results.append((row[0], mapping, unit, tiers))
    return results


def refresh_prices(html: str) -> Path:
    """Parse and write the bootstrap US list prices."""
    parsed = parse_disk_prices(html)
    if len(parsed) < 5:
        raise SystemExit(
            f"refusing to write prices: only {len(parsed)} provisioned-space rows parsed"
        )
    source = SourceRef(
        url=PRICING_DOC_URL,
        note=(
            "US list price scraped from the disk pricing page. Regional prices differ "
            "(e.g. Sao Paulo, Tokyo); run `python -m gcp_opt refresh-prices --region ...` "
            "with the Cloud Billing Catalog API for exact regional rates."
        ),
    )
    skus = tuple(
        PriceSku(
            sku_id=f"docs-us-list::{kind}::{scope}::{role}",
            description=label,
            resource_family="Storage",
            usage_type="OnDemand",
            usage_unit=unit,
            service_regions=("us-central1",),
            currency_code="USD",
            tiers=tiers,
            source=source,
            role=role,
        )
        for label, (kind, scope, role), unit, tiers in parsed
    )
    provenance = Provenance(
        method=SourceMethod.DOCS_SCRAPE,
        source_url=PRICING_DOC_URL,
        retrieved_at=_now(),
        generator=GENERATOR,
        notes="Bootstrap US list prices; use the Cloud Billing Catalog API for regional rates.",
    )
    book = PriceBook(
        currency_code="USD", region="us-central1", skus=skus, provenance=provenance
    )
    path = DATA_DIR / "disk_prices.json"
    write_snapshot(build_snapshot(book, kind=SnapshotKind.PRICES, provenance=provenance), path)
    print(f"wrote {path.relative_to(REPO_ROOT)} ({len(skus)} SKUs)")
    return path


# --------------------------------------------------------------------------- #
# Golden size-table fixtures
# --------------------------------------------------------------------------- #
def refresh_size_tables(html: str) -> Path:
    """Write the documented per-size tables as a test fixture."""
    tables = doc_parse.parse_size_tables(html)
    if len(tables) < 4:
        raise SystemExit(f"refusing to write size-table fixture: only {len(tables)} tables parsed")
    path = REPO_ROOT / "tests" / "fixtures" / "doc_size_tables.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "source_url": PERF_DOC_URL,
                "retrieved_at": _now().isoformat(),
                "tables": tables,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {path.relative_to(REPO_ROOT)} ({len(tables)} tables)")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-machine-pages", action="store_true")
    args = parser.parse_args()

    print(f"fetching {PERF_DOC_URL}")
    perf_html = fetch(PERF_DOC_URL)
    refresh_machine_type_limits(perf_html)
    refresh_size_tables(perf_html)

    print(f"fetching {PRICING_DOC_URL}")
    refresh_prices(fetch(PRICING_DOC_URL))

    if not args.skip_machine_pages:
        pages: dict[str, str] = {}
        for url in MACHINE_DOC_URLS:
            try:
                print(f"fetching {url}")
                pages[url] = fetch(url)
            except Exception as error:
                print(f"  skipped ({error})", file=sys.stderr)
        if pages:
            refresh_machine_types(pages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
