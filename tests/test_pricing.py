"""Billing Catalog client and SKU classification tests (no network)."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from gcp_opt.errors import ApiAuthError
from gcp_opt.models import DiskKind, Scope, SkuRole
from gcp_opt.pricing import (
    BillingCatalogClient,
    classify_disk_sku,
    classify_machine_sku,
    classify_sku,
    find_price,
    money_to_decimal,
    parse_sku,
)


class FakeTransport:
    """Returns queued JSON payloads and records the requests it received."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {"url": url, "params": dict(params or {}), "headers": dict(headers or {})}
        )
        if not self._responses:
            raise AssertionError("FakeTransport ran out of responses")
        return self._responses.pop(0)


def _sku(
    sku_id: str,
    description: str,
    regions: list[str],
    nanos: int,
    *,
    units: str = "0",
    family: str = "Storage",
    usage_unit: str = "gibibyte hour",
) -> dict[str, Any]:
    return {
        "skuId": sku_id,
        "description": description,
        "category": {
            "serviceDisplayName": "Compute Engine",
            "resourceFamily": family,
            "usageType": "OnDemand",
        },
        "serviceRegions": regions,
        "pricingInfo": [
            {
                "currencyCode": "USD",
                "pricingExpression": {
                    "usageUnit": usage_unit,
                    "tieredRates": [
                        {"startUsageAmount": 0, "unitPrice": {"units": units, "nanos": nanos}}
                    ],
                },
            }
        ],
    }


def test_money_to_decimal_is_exact() -> None:
    assert money_to_decimal({"units": "1", "nanos": 500_000_000}) == Decimal("1.5")
    assert money_to_decimal({"nanos": 136_986}) == Decimal("0.000136986")
    assert money_to_decimal({}) == Decimal(0)


def test_parse_sku_maps_fields() -> None:
    parsed = parse_sku(_sku("ABC-123", "Balanced PD Capacity", ["us-central1"], 136_986))
    assert parsed.sku_id == "ABC-123"
    assert parsed.tiers[0].unit_price == Decimal("0.000136986")
    assert parsed.service_regions == ("us-central1",)
    assert parsed.monthly_cost_per_gib() == Decimal("0.000136986") * 730


@pytest.mark.parametrize(
    ("description", "scope", "expected"),
    [
        ("Balanced PD Capacity", Scope.ZONAL, DiskKind.PD_BALANCED),
        ("SSD backed PD Capacity", Scope.ZONAL, DiskKind.PD_SSD),
        ("Standard PD Capacity", Scope.ZONAL, DiskKind.PD_STANDARD),
        ("Balanced provisioned space", Scope.ZONAL, DiskKind.PD_BALANCED),
        ("Regional Balanced PD Capacity", Scope.ZONAL, None),
        ("Regional Balanced PD Capacity", Scope.REGIONAL, DiskKind.PD_BALANCED),
        ("Hyperdisk Balanced provisioned space", Scope.ZONAL, DiskKind.HYPERDISK_BALANCED),
        ("Hyperdisk Throughput provisioned space", Scope.ZONAL, DiskKind.HYPERDISK_THROUGHPUT),
        ("Hyperdisk Throughput provisioned throughput", Scope.ZONAL, None),
        ("Balanced PD Snapshot data storage", Scope.ZONAL, None),
        ("SSD backed PD Capacity (Regional)", Scope.REGIONAL, DiskKind.PD_SSD),
    ],
)
def test_classify_disk_sku(description: str, scope: Scope, expected: DiskKind | None) -> None:
    assert classify_disk_sku(description, scope) is expected


def test_fetch_disk_price_book_filters_and_paginates() -> None:
    page_one = {
        "skus": [
            _sku("BAL", "Balanced PD Capacity", ["us-central1"], 136_986),
            _sku("GPU", "Nvidia GPU", ["us-central1"], 1, family="Compute"),
            _sku("SNAP", "Balanced PD Snapshot", ["us-central1"], 5),
        ],
        "nextPageToken": "page2",
    }
    page_two = {
        "skus": [
            _sku("SSD", "SSD backed PD Capacity", ["us-central1"], 232_877),
            _sku("OTHER-REGION", "Balanced PD Capacity", ["europe-west1"], 136_986),
        ]
    }
    transport = FakeTransport([page_one, page_two])
    client = BillingCatalogClient(api_key="KEY", transport=transport)
    book = client.fetch_disk_price_book(region="us-central1")

    assert {sku.sku_id for sku in book.skus} == {"BAL", "SSD"}
    assert book.region == "us-central1"
    # Pagination: second request carries the page token and the API key.
    assert transport.calls[1]["params"]["pageToken"] == "page2"
    assert transport.calls[0]["params"]["key"] == "KEY"


def test_access_token_uses_authorization_header() -> None:
    transport = FakeTransport([{"skus": [_sku("BAL", "Balanced PD Capacity", ["us-central1"], 1)]}])
    client = BillingCatalogClient(access_token="TOKEN", transport=transport)
    client.fetch_disk_price_book(region="us-central1")
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer TOKEN"
    assert "key" not in transport.calls[0]["params"]


def test_empty_result_raises_auth_error() -> None:
    transport = FakeTransport([{"skus": []}])
    client = BillingCatalogClient(api_key="KEY", transport=transport)
    with pytest.raises(ApiAuthError):
        client.fetch_disk_price_book(region="us-central1")


def test_client_requires_credentials() -> None:
    with pytest.raises(ValueError, match="api_key or an access_token"):
        BillingCatalogClient(transport=FakeTransport([]))


def test_tiered_standard_price_first_30_gib_free() -> None:
    raw = _sku("STD", "Standard PD Capacity", ["us-central1"], 54_795)
    raw["pricingInfo"][0]["pricingExpression"]["tieredRates"] = [
        {"startUsageAmount": 0, "unitPrice": {"units": "0", "nanos": 0}},
        {
            "startUsageAmount": 30,
            "unitPrice": {"units": "0", "nanos": 54_795},
        },
    ]
    parsed = parse_sku(raw)
    assert parsed.cost_for(Decimal(30)) == 0
    assert parsed.cost_for(Decimal(31)) == Decimal("0.000054795") * 730
    with pytest.raises(ValueError, match="2 price tiers"):
        parsed.flat_hourly_price()


def test_find_price_returns_none_when_absent() -> None:
    transport = FakeTransport([{"skus": [_sku("BAL", "Balanced PD Capacity", ["us-central1"], 1)]}])
    book = BillingCatalogClient(api_key="K", transport=transport).fetch_disk_price_book(
        region="us-central1"
    )
    assert find_price(book, DiskKind.PD_BALANCED) is not None
    assert find_price(book, DiskKind.PD_SSD) is None


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Extreme provisioned space", (DiskKind.PD_EXTREME, SkuRole.CAPACITY)),
        ("Extreme provisioned IOPS", (DiskKind.PD_EXTREME, SkuRole.PROVISIONED_IOPS)),
        (
            "Hyperdisk Balanced provisioned space",
            (DiskKind.HYPERDISK_BALANCED, SkuRole.CAPACITY),
        ),
        (
            "Hyperdisk Balanced provisioned IOPS",
            (DiskKind.HYPERDISK_BALANCED, SkuRole.PROVISIONED_IOPS),
        ),
        (
            "Hyperdisk Throughput provisioned throughput",
            (DiskKind.HYPERDISK_THROUGHPUT, SkuRole.PROVISIONED_THROUGHPUT),
        ),
    ],
)
def test_classify_sku_separates_roles(
    description: str, expected: tuple[DiskKind, SkuRole]
) -> None:
    assert classify_sku(description, Scope.ZONAL) == expected


def test_classify_disk_sku_ignores_provisioned_performance() -> None:
    # classify_disk_sku is the capacity-only view.
    assert classify_disk_sku("Extreme provisioned IOPS", Scope.ZONAL) is None
    assert classify_disk_sku("Extreme provisioned space", Scope.ZONAL) is DiskKind.PD_EXTREME


def test_parse_sku_records_role() -> None:
    assert parse_sku(_sku("X", "Extreme provisioned IOPS", ["us-central1"], 89_041)).role is (
        SkuRole.PROVISIONED_IOPS
    )


def test_price_book_keeps_capacity_and_iops_skus() -> None:
    transport = FakeTransport(
        [
            {
                "skus": [
                    _sku("EX-SPACE", "Extreme provisioned space", ["us-central1"], 171_233),
                    _sku("EX-IOPS", "Extreme provisioned IOPS", ["us-central1"], 89_041),
                ]
            }
        ]
    )
    book = BillingCatalogClient(api_key="K", transport=transport).fetch_disk_price_book(
        region="us-central1"
    )
    capacity = find_price(book, DiskKind.PD_EXTREME, role=SkuRole.CAPACITY)
    iops = find_price(book, DiskKind.PD_EXTREME, role=SkuRole.PROVISIONED_IOPS)
    assert capacity is not None
    assert capacity.sku_id == "EX-SPACE"
    assert iops is not None
    assert iops.sku_id == "EX-IOPS"


def _instance_sku(
    description: str, regions: list[str], nanos: int, *, usage: str = "OnDemand"
) -> dict[str, Any]:
    raw = _sku("SKU-" + description[:8], description, regions, nanos, family="Compute")
    raw["category"]["usageType"] = usage
    return raw


@pytest.mark.parametrize(
    ("description", "known", "expected"),
    [
        ("N2 Instance Core running in Americas", {"n2", "n2d"}, ("n2", "core")),
        ("N2D AMD Instance Core running in Americas", {"n2", "n2d"}, ("n2d", "core")),
        ("n1 predefined instance ram", {"n1"}, ("n1", "ram")),
        ("Memory-optimized Instance Ram", {"m1", "m2", "m3"}, ("m1", "ram")),
        ("Compute optimized core", {"c2"}, ("c2", "core")),
        ("N2 Custom Instance Core", {"n2"}, None),
        ("Premium image core", {"n2"}, None),
        ("N2 Instance Core running in Americas", {"c3"}, None),
        ("N2 sole tenancy core", {"n2"}, None),
    ],
)
def test_classify_machine_sku(
    description: str, known: set[str], expected: tuple[str, str] | None
) -> None:
    assert classify_machine_sku(description, known) == expected


def test_fetch_machine_family_prices() -> None:
    transport = FakeTransport(
        [
            {
                "skus": [
                    _instance_sku(
                        "N2 Instance Core running in Americas", ["us-central1"], 31_611_000
                    ),
                    _instance_sku(
                        "N2 Instance Ram running in Americas", ["us-central1"], 4_237_000
                    ),
                    _instance_sku(
                        "N2D AMD Instance Core running in Americas", ["us-central1"], 27_000_000
                    ),
                    _instance_sku("Memory-optimized Instance Core", ["us-central1"], 50_000_000),
                    _instance_sku(
                        "N2 Instance Core running in Americas", ["europe-west1"], 35_000_000
                    ),
                    _instance_sku("N2 Custom Instance Core", ["us-central1"], 1),
                    _instance_sku(
                        "N2 Instance Core running in Americas",
                        ["us-central1"],
                        999,
                        usage="Preemptible",
                    ),
                    _instance_sku("Premium image core", ["us-central1"], 1),
                ]
            }
        ]
    )
    client = BillingCatalogClient(api_key="K", transport=transport)
    prices = client.fetch_machine_family_prices(
        region="us-central1", known_families={"n2", "n2d", "m1", "m2", "m3"}
    )
    by_family = {price.family: price for price in prices}
    assert by_family["n2"].core_hourly_usd == Decimal("0.031611")
    assert by_family["n2"].ram_gib_hourly_usd == Decimal("0.004237")
    assert by_family["n2d"].core_hourly_usd == Decimal("0.027")
    assert by_family["m1"].core_hourly_usd == Decimal("0.05")
    # 64 vCPU + 64 GiB at n2 rates -> hourly sum, then 730-hour month.
    assert by_family["n2"].hourly_for(vcpus=64, memory_gib=Decimal(64)) == (
        Decimal("0.031611") * 64 + Decimal("0.004237") * 64
    )


def test_fetch_machine_family_prices_errors_when_region_absent() -> None:
    transport = FakeTransport(
        [{"skus": [_instance_sku("N2 Instance Core", ["europe-west1"], 1)]}]
    )
    client = BillingCatalogClient(api_key="K", transport=transport)
    with pytest.raises(ApiAuthError, match="no VM core/RAM SKUs"):
        client.fetch_machine_family_prices(region="us-central1", known_families={"n2"})
