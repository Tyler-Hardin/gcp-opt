"""Catalog tests: machine-ceiling resolution, joins, pricing and provenance."""

from __future__ import annotations

from decimal import Decimal

import pytest

from gcp_opt import constants
from gcp_opt.catalog import Catalog
from gcp_opt.dataset import Dataset
from gcp_opt.errors import PriceUnavailableError, UnmodeledDiskKindError
from gcp_opt.models import DiskKind, Scope, SkuRole, SnapshotKind


def test_bundled_snapshots_load_and_verify(catalog: Catalog) -> None:
    assert catalog.dataset.limits.machine_type_limits
    assert catalog.dataset.limits.vcpu_limits
    assert len(catalog.machine_names()) > 100
    for kind in SnapshotKind:
        if kind is SnapshotKind.DISK_PERFORMANCE:
            continue
        assert kind in catalog.dataset.provenance


def test_explicit_machine_type_ceiling(catalog: Catalog) -> None:
    limit = catalog.limit_for("a2-ultragpu-1g", DiskKind.PD_BALANCED)
    assert limit is not None
    assert limit.max_read_iops == Decimal(15000)
    assert limit.max_read_mibps == Decimal(800)


def test_vcpu_range_ceiling_resolution(catalog: Catalog) -> None:
    # n2-standard-8 has 8 vCPUs -> the documented 8-15 vCPU row.
    limit = catalog.limit_for("n2-standard-8", DiskKind.PD_BALANCED)
    assert limit is not None
    assert limit.max_read_iops == Decimal(15000)
    assert limit.max_write_iops == Decimal(15000)
    assert limit.max_read_mibps == Decimal(800)

    # n2-standard-64 lands in the unbounded "64 or more" row.
    big = catalog.limit_for("n2-standard-64", DiskKind.PD_BALANCED)
    assert big is not None
    assert big.max_read_iops == Decimal(80000)


def test_unknown_machine_has_no_ceiling(catalog: Catalog) -> None:
    assert catalog.limit_for("does-not-exist-8", DiskKind.PD_SSD) is None


def test_disk_option_joins_cost_and_performance(catalog: Catalog) -> None:
    option = catalog.disk_option("n2-standard-8", DiskKind.PD_SSD, 1000)
    assert option.monthly_cost_usd == Decimal("170.000210000")
    assert option.cost_per_gib_month == Decimal("0.170000210000")
    # IOPS capped by the 8-15 vCPU VM ceiling; throughput by the size formula.
    assert option.read_iops == Decimal(15000)
    assert option.read_mibps == Decimal("720.00")
    assert option.instance_bound is True
    assert option.instance_limit_known is True


def test_disk_option_standard_free_tier(catalog: Catalog) -> None:
    option = catalog.disk_option("n2-standard-8", DiskKind.PD_STANDARD, 30)
    assert option.monthly_cost_usd == 0
    option_31 = catalog.disk_option("n2-standard-8", DiskKind.PD_STANDARD, 31)
    assert option_31.monthly_cost_usd == Decimal("0.000054795") * 730


def test_price_region_mismatch_requires_opt_in(catalog: Catalog) -> None:
    with pytest.raises(PriceUnavailableError, match="no price snapshot"):
        catalog.disk_option("n2-standard-8", DiskKind.PD_SSD, 100, region="southamerica-east1")
    option = catalog.disk_option(
        "n2-standard-8",
        DiskKind.PD_SSD,
        100,
        region="southamerica-east1",
        allow_us_list_price=True,
    )
    assert option.region == "southamerica-east1"
    assert "US list price" in option.price_note


def test_hyperdisk_has_no_size_scaling_model(catalog: Catalog) -> None:
    with pytest.raises(UnmodeledDiskKindError):
        catalog.disk_model(DiskKind.HYPERDISK_BALANCED)


def test_bundled_prices_match_published_us_list_prices(catalog: Catalog) -> None:
    expectations = {
        DiskKind.PD_STANDARD: Decimal("0.04"),
        DiskKind.PD_BALANCED: Decimal("0.10"),
        DiskKind.PD_SSD: Decimal("0.17"),
        DiskKind.PD_EXTREME: Decimal("0.125"),
    }
    for kind, expected in expectations.items():
        sku = catalog.price_sku(kind)
        assert sku is not None
        # Standard PD's first 30 GiB/month are free, so use its marginal tier.
        marginal = sku.tiers[-1].unit_price * 730
        assert marginal.quantize(Decimal("0.001")) == expected


def test_dataset_defaults_are_documented(catalog: Catalog) -> None:
    info = catalog.machine_info("n2-standard-8")
    assert info is not None
    assert info.maximum_persistent_disks == constants.DEFAULT_MAX_PERSISTENT_DISKS
    assert info.maximum_total_size_gib == constants.DEFAULT_MAX_TOTAL_SIZE_GIB


def test_scope_is_zonal_for_bundled_limits(catalog: Catalog) -> None:
    assert all(limit.scope is Scope.ZONAL for limit in catalog.dataset.limits.machine_type_limits)


def test_dataset_load_is_repeatable() -> None:
    first = Dataset.load_bundled()
    second = Dataset.load_bundled()
    assert first.limits == second.limits
    assert first.prices == second.prices


def test_extreme_requires_provisioned_iops(catalog: Catalog) -> None:
    with pytest.raises(PriceUnavailableError, match="provisioned"):
        catalog.disk_option("n2-standard-64", DiskKind.PD_EXTREME, 9313)


def test_extreme_cost_is_capacity_plus_provisioned_iops(catalog: Catalog) -> None:
    option = catalog.disk_option(
        "n2-standard-64", DiskKind.PD_EXTREME, 9313, provisioned_iops=16000
    )
    assert option.provisioned_iops == Decimal(16000)
    assert option.provisioned_iops_monthly_cost_usd == Decimal("0.000089041") * 730 * 16000
    assert option.capacity_monthly_cost_usd is not None
    assert option.monthly_cost_usd == (
        option.capacity_monthly_cost_usd + option.provisioned_iops_monthly_cost_usd
    )
    # 16,000 provisioned IOPS * 256 KiB/s = 4,000 MiB/s, capped by the VM/type.
    assert option.read_mibps == Decimal(4000)


def test_bundled_book_has_extreme_iops_sku(catalog: Catalog) -> None:
    sku = catalog.price_sku(DiskKind.PD_EXTREME, role=SkuRole.PROVISIONED_IOPS)
    assert sku is not None
    assert sku.usage_unit == "hour"
    assert sku.tiers[-1].unit_price == Decimal("0.000089041")
