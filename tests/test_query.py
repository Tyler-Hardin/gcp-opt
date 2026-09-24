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
    # The documented per-instance PD read ceiling is 1,200 MiB/s.
    assert option.read_mibps == Decimal(1200)


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
