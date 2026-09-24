"""Query-layer tests: the two motivating questions plus inversion helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from gcp_opt import units
from gcp_opt.catalog import Catalog
from gcp_opt.errors import InfeasibleTargetError
from gcp_opt.models import DiskKind, PriceSku, PriceTier, SourceRef
from gcp_opt.query import (
    Requirement,
    max_affordable_size,
    max_throughput_option,
    min_cost_option,
    min_replicas,
    scale_out,
    top_min_cost_options,
    top_throughput_options,
)

_TEN_TB_GIB = units.tb_to_gib(10)
_TEN_GBPS_MIBPS = units.gb_per_s_to_mibps(10)


def test_single_instance_cannot_reach_10_gbps(catalog: Catalog) -> None:
    """A single VM's Persistent Disk tops out at 1,200 MiB/s (~1.26 GB/s)."""
    requirement = Requirement.build(
        min_total_size_gib=_TEN_TB_GIB, min_read_mibps=_TEN_GBPS_MIBPS
    )
    with pytest.raises(InfeasibleTargetError, match="no single-instance"):
        min_cost_option(catalog, catalog.machine_names(), requirement=requirement)


def test_cheapest_for_1_1_gbps_and_10tb_is_standard_pd(catalog: Catalog) -> None:
    requirement = Requirement.build(
        min_total_size_gib=_TEN_TB_GIB, min_read_mibps=units.gb_per_s_to_mibps("1.1")
    )
    option = min_cost_option(
        catalog,
        [name for name in catalog.machine_names() if name.startswith("n2-")],
        requirement=requirement,
    )
    assert option.disk_kind is DiskKind.PD_STANDARD
    assert option.size_gib >= _TEN_TB_GIB
    assert option.read_mibps >= requirement.min_read_mibps  # type: ignore[operator]
    assert option.monthly_cost_usd < Decimal(500)


def test_max_bandwidth_under_budget_spends_no_more_than_budget(catalog: Catalog) -> None:
    option = max_throughput_option(
        catalog,
        [name for name in catalog.machine_names() if name.startswith("n2-")],
        budget_usd=2000,
        min_total_size_gib=_TEN_TB_GIB,
        metric="read",
    )
    assert option.monthly_cost_usd <= Decimal(2000)
    assert option.size_gib >= _TEN_TB_GIB
    # Provisioned Extreme PD beats the 1,200 MiB/s Balanced/SSD ceiling, and the
    # disk-type read cap is 4,000 MiB/s.
    assert option.disk_kind is DiskKind.PD_EXTREME
    assert Decimal(1200) < option.read_mibps <= Decimal(4000)


def test_four_gbps_is_solvable_with_provisioned_extreme(catalog: Catalog) -> None:
    """4 GB/s (3,814.7 MiB/s) fits the 4,000 MiB/s Extreme PD ceiling on one VM."""
    target = units.gb_per_s_to_mibps(4)
    requirement = Requirement.build(min_total_size_gib=_TEN_TB_GIB, min_read_mibps=target)
    option = min_cost_option(catalog, catalog.machine_names(), requirement=requirement)

    assert option.disk_kind is DiskKind.PD_EXTREME
    assert option.read_mibps >= target
    assert option.size_gib >= _TEN_TB_GIB
    assert option.provisioned_iops is not None
    assert option.provisioned_iops > 0
    assert option.capacity_monthly_cost_usd is not None
    assert option.provisioned_iops_monthly_cost_usd is not None
    assert option.monthly_cost_usd == (
        option.capacity_monthly_cost_usd + option.provisioned_iops_monthly_cost_usd
    )
    # 3,814.7 MiB/s / 0.25 MiB/s-per-IOPS = 15,258.789 provisioned IOPS.
    assert option.provisioned_iops == target / Decimal("0.25")


def test_ten_gbps_still_exceeds_the_extreme_ceiling(catalog: Catalog) -> None:
    requirement = Requirement.build(
        min_total_size_gib=_TEN_TB_GIB, min_read_mibps=_TEN_GBPS_MIBPS
    )
    with pytest.raises(InfeasibleTargetError, match="no single-instance"):
        min_cost_option(catalog, catalog.machine_names(), requirement=requirement)


def test_max_bandwidth_infeasible_when_budget_below_minimum_size(catalog: Catalog) -> None:
    with pytest.raises(InfeasibleTargetError):
        max_throughput_option(
            catalog, ["n2-standard-8"], budget_usd=1, min_total_size_gib=_TEN_TB_GIB
        )


def test_max_affordable_size_respects_budget_and_free_tier() -> None:
    sku = PriceSku(
        sku_id="test",
        description="Standard provisioned space",
        resource_family="Storage",
        usage_type="OnDemand",
        usage_unit="gibibyte hour",
        service_regions=("us-central1",),
        currency_code="USD",
        tiers=(
            PriceTier(start_usage_amount=Decimal(0), unit_price=Decimal(0)),
            PriceTier(start_usage_amount=Decimal(30), unit_price=Decimal("0.000054795")),
        ),
        source=SourceRef(url="test://price"),
    )
    # $0.04/GiB-month -> $100 buys ~2,500 GiB (plus the free 30).
    size = max_affordable_size(sku, Decimal(100))
    assert sku.cost_for(size) <= Decimal(100)
    assert sku.cost_for(size + 1) > Decimal(100)
    assert max_affordable_size(sku, 0) == 0


def test_scale_out_multiplies_totals(catalog: Catalog) -> None:
    option = catalog.disk_option("n2-standard-8", DiskKind.PD_SSD, 1000)
    aggregate = scale_out(option, 4)
    assert aggregate.total_size_gib == Decimal(4000)
    assert aggregate.total_read_mibps == option.read_mibps * 4
    assert aggregate.total_monthly_disk_cost_usd == option.monthly_cost_usd * 4
    assert "VM instance cost excluded" in aggregate.cost_note


def test_min_replicas(catalog: Catalog) -> None:
    option = catalog.disk_option("n2-standard-8", DiskKind.PD_SSD, 1000)
    replicas = min_replicas(
        option, min_total_size_gib=_TEN_TB_GIB, min_read_mibps=_TEN_GBPS_MIBPS
    )
    # Need ceil(9313.2/1000)=10 replicas for capacity and ceil(9536.7/720)=14 for bandwidth.
    assert replicas == 14
    aggregate = scale_out(option, replicas)
    assert aggregate.total_size_gib >= _TEN_TB_GIB
    assert aggregate.total_read_mibps >= _TEN_GBPS_MIBPS
    assert scale_out(option, replicas - 1).total_read_mibps < _TEN_GBPS_MIBPS


def test_scale_out_rejects_zero_replicas(catalog: Catalog) -> None:
    option = catalog.disk_option("n2-standard-8", DiskKind.PD_SSD, 1000)
    with pytest.raises(ValueError, match="replicas must be"):
        scale_out(option, 0)


def test_top_throughput_options_returns_a_distinct_frontier(catalog: Catalog) -> None:
    options = top_throughput_options(
        catalog,
        [name for name in catalog.machine_names() if name.startswith("n2-")],
        budget_usd=3000,
        top=5,
        min_total_size_gib=_TEN_TB_GIB,
    )
    assert 1 < len(options) <= 5
    scores = [option.read_mibps for option in options]
    assert scores == sorted(scores, reverse=True)
    # Distinct outcomes: no two rows are the same disk at the same cost.
    keys = {
        (option.disk_kind, option.size_gib, option.provisioned_iops, option.monthly_cost_usd)
        for option in options
    }
    assert len(keys) == len(options)


def test_top_throughput_options_top_one_matches_single_best(catalog: Catalog) -> None:
    machines = [name for name in catalog.machine_names() if name.startswith("n2-")]
    single = max_throughput_option(
        catalog, machines, budget_usd=2000, min_total_size_gib=_TEN_TB_GIB
    )
    top = top_throughput_options(
        catalog, machines, budget_usd=2000, min_total_size_gib=_TEN_TB_GIB, top=1
    )
    assert top == [single]


def test_top_min_cost_options_sorted_and_distinct(catalog: Catalog) -> None:
    options = top_min_cost_options(
        catalog,
        [name for name in catalog.machine_names() if name.startswith("n2-")],
        requirement=Requirement.build(
            min_total_size_gib=_TEN_TB_GIB,
            min_read_mibps=units.gb_per_s_to_mibps("1.1"),
        ),
        top=4,
    )
    assert 1 < len(options) <= 4
    costs = [option.monthly_cost_usd for option in options]
    assert costs == sorted(costs)
    assert len({option.disk_kind for option in options}) == len(options)


def test_top_rejects_out_of_range(catalog: Catalog) -> None:
    options = top_min_cost_options(
        catalog,
        ["n2-standard-8"],
        requirement=Requirement.build(min_total_size_gib="1TB"),
        top=0,
    )
    assert len(options) == 1  # clamped to at least one


def test_provisioned_frontier_rows_differ_only_by_iops(catalog: Catalog) -> None:
    """Two Extreme rows for one machine share everything but provisioned IOPS."""
    options = top_throughput_options(
        catalog,
        ["n2-highcpu-64"],
        budget_usd=3000,
        top=3,
        min_total_size_gib=_TEN_TB_GIB,
    )
    assert len(options) >= 2
    best, second = options[0], options[1]
    assert (best.machine_type, best.disk_kind, best.size_gib) == (
        second.machine_type,
        second.disk_kind,
        second.size_gib,
    )
    assert best.provisioned_iops is not None
    assert second.provisioned_iops is not None
    assert best.provisioned_iops > second.provisioned_iops
    assert best.read_mibps > second.read_mibps
    assert best.monthly_cost_usd > second.monthly_cost_usd
