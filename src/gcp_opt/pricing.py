"""Cloud Billing Catalog API client and disk-SKU price resolution.

Endpoint: ``GET https://cloudbilling.googleapis.com/v1/services/{serviceId}/skus``

The Compute Engine service id is :data:`~gcp_opt.constants.COMPUTE_ENGINE_SERVICE_ID`.
Authentication is either an API key (``?key=...``) or an OAuth bearer token; the
transport is injected so the client is fully testable offline.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from gcp_opt import constants
from gcp_opt.errors import ApiAuthError, ApiError
from gcp_opt.http import JsonTransport, UrllibJsonTransport
from gcp_opt.models import (
    DiskKind,
    PriceBook,
    PriceSku,
    PriceTier,
    Provenance,
    Scope,
    SkuRole,
    SourceMethod,
    SourceRef,
)

_BILLING_SOURCE = SourceRef(
    url="https://cloud.google.com/billing/docs/reference/rest/v1/services.skus/list",
    note="Cloud Billing Catalog API v1 (public, undiscounted on-demand list prices).",
)


def money_to_decimal(money: Mapping[str, Any]) -> Decimal:
    """Convert a Google ``Money`` object to an exact :class:`~decimal.Decimal`.

    Google encodes money as ``{"units": "0", "nanos": 136986}``; ``1e-9`` of a unit
    is exactly representable in decimal, so prices stay exact.
    """
    units = Decimal(str(money.get("units", "0") or "0"))
    nanos = Decimal(str(money.get("nanos", 0) or 0))
    return units + nanos / Decimal(10**9)


def parse_sku(raw: Mapping[str, Any]) -> PriceSku:
    """Parse one SKU JSON object into a :class:`PriceSku`.

    Raises:
        ApiError: if the SKU lacks the fields this package relies on.
    """
    try:
        category = raw["category"]
        pricing_info = raw["pricingInfo"][0]
        expression = pricing_info["pricingExpression"]
    except (KeyError, IndexError, TypeError) as error:
        raise ApiError(f"malformed SKU payload: {error}") from error

    tiers: list[PriceTier] = []
    for rate in expression.get("tieredRates", []):
        tiers.append(
            PriceTier(
                start_usage_amount=Decimal(str(rate.get("startUsageAmount", 0) or 0)),
                unit_price=money_to_decimal(rate.get("unitPrice", {})),
            )
        )
    if not tiers:
        raise ApiError(f"SKU {raw.get('skuId')!r} has no tieredRates")

    return PriceSku(
        sku_id=str(raw.get("skuId", "")),
        description=str(raw.get("description", "")),
        resource_family=str(category.get("resourceFamily", "")),
        usage_type=str(category.get("usageType", "")),
        usage_unit=str(expression.get("usageUnit", "")),
        service_regions=tuple(str(region) for region in raw.get("serviceRegions", [])),
        currency_code=str(pricing_info.get("currencyCode", "USD")),
        tiers=tuple(tiers),
        source=_BILLING_SOURCE,
        role=sku_role_of(str(raw.get("description", ""))),
    )


def sku_role_of(description: str) -> SkuRole:
    """Classify a SKU description as capacity, provisioned IOPS or throughput."""
    text = description.lower()
    if "provisioned iops" in text:
        return SkuRole.PROVISIONED_IOPS
    if "provisioned throughput" in text:
        return SkuRole.PROVISIONED_THROUGHPUT
    return SkuRole.CAPACITY


_ROLE_PATTERNS: dict[SkuRole, dict[DiskKind, tuple[str, ...]]] = {
    SkuRole.CAPACITY: constants.DISK_SKU_MATCH,
    SkuRole.PROVISIONED_IOPS: constants.DISK_IOPS_SKU_MATCH,
    SkuRole.PROVISIONED_THROUGHPUT: constants.DISK_THROUGHPUT_SKU_MATCH,
}


def classify_sku(description: str, scope: Scope) -> tuple[DiskKind, SkuRole] | None:
    """Map a SKU description to ``(disk kind, price role)``, or ``None``.

    Handles the two historical description styles ("Balanced PD Capacity" in the
    catalog and "Balanced provisioned space" on the pricing page), keeps Hyperdisk
    SKUs from matching plain-PD patterns, and separates provisioned-performance
    SKUs (Extreme PD IOPS) from capacity SKUs.
    """
    text = description.lower()
    if any(marker in text for marker in constants.SKU_EXCLUDE_SUBSTRINGS):
        return None
    is_regional = constants.REGIONAL_SKU_MARKER in text
    if scope is Scope.REGIONAL and not is_regional:
        return None
    if scope is Scope.ZONAL and is_regional:
        return None

    role = sku_role_of(description)
    is_hyperdisk = "hyperdisk" in text
    for kind, patterns in _ROLE_PATTERNS[role].items():
        if ("hyperdisk" in kind.value) is not is_hyperdisk:
            continue
        if any(pattern in text for pattern in patterns):
            return kind, role
    return None


def classify_disk_sku(description: str, scope: Scope) -> DiskKind | None:
    """Map a SKU description to a capacity :class:`DiskKind`, or ``None``.

    Provisioned-IOPS/throughput SKUs return ``None``; use :func:`classify_sku` to
    see those.
    """
    result = classify_sku(description, scope)
    if result is None or result[1] is not SkuRole.CAPACITY:
        return None
    return result[0]


class BillingCatalogClient:
    """Thin client for the public Cloud Billing Catalog API v1."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        access_token: str | None = None,
        transport: JsonTransport | None = None,
        base_url: str = constants.BILLING_CATALOG_BASE_URL,
    ) -> None:
        if not api_key and not access_token:
            raise ValueError("BillingCatalogClient needs an api_key or an access_token")
        self._api_key = api_key
        self._access_token = access_token
        self._transport = transport or UrllibJsonTransport()
        self._base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"} if self._access_token else {}

    def _params(self) -> dict[str, str]:
        return {"key": self._api_key} if self._api_key else {}

    def list_services(self, page_size: int = 200) -> list[dict[str, Any]]:
        """List all billable services (used to discover service ids)."""
        params = {**self._params(), "pageSize": str(page_size)}
        return list(self._paginate(f"{self._base_url}/services", params, item_key="services"))

    def iter_skus(
        self,
        *,
        service_id: str = constants.COMPUTE_ENGINE_SERVICE_ID,
        currency_code: str = "USD",
    ) -> Iterator[dict[str, Any]]:
        """Yield every raw SKU for a service, following ``nextPageToken``."""
        params = {
            **self._params(),
            "currencyCode": currency_code,
            "pageSize": str(constants.BILLING_MAX_PAGE_SIZE),
        }
        yield from self._paginate(
            f"{self._base_url}/services/{service_id}/skus", params, item_key="skus"
        )

    def _paginate(
        self, url: str, params: dict[str, str], *, item_key: str
    ) -> Iterator[dict[str, Any]]:
        token: str | None = None
        while True:
            page_params = dict(params)
            if token:
                page_params["pageToken"] = token
            payload = self._transport.get_json(url, params=page_params, headers=self._headers())
            yield from payload.get(item_key, [])
            token = payload.get("nextPageToken")
            if not token:
                return

    def fetch_disk_price_book(
        self,
        *,
        region: str,
        scope: Scope = Scope.ZONAL,
        currency_code: str = "USD",
        service_id: str = constants.COMPUTE_ENGINE_SERVICE_ID,
    ) -> PriceBook:
        """Fetch and filter disk-capacity SKUs for one region.

        Raises:
            ApiAuthError: if the catalog rejects the credentials.
        """
        skus: list[PriceSku] = []
        for raw in self.iter_skus(service_id=service_id, currency_code=currency_code):
            if str(raw.get("category", {}).get("resourceFamily", "")) != "Storage":
                continue
            regions = raw.get("serviceRegions", [])
            if region not in regions:
                continue
            parsed = parse_sku(raw)
            if classify_sku(parsed.description, scope) is None:
                continue
            skus.append(parsed)
        if not skus:
            raise ApiAuthError(
                f"no disk-capacity SKUs found for region {region!r}; "
                "check the region name, credentials, and that the Cloud Billing API is enabled"
            )
        return PriceBook(
            currency_code=currency_code,
            region=region,
            skus=tuple(skus),
            provenance=Provenance(
                method=SourceMethod.CLOUD_BILLING_CATALOG,
                source_url=f"{self._base_url}/services/{service_id}/skus",
                retrieved_at=datetime.now(UTC),
                generator="gcp-opt",
                notes="Live Cloud Billing Catalog v1 prices (undiscounted on-demand).",
            ),
        )


def find_price(
    book: PriceBook,
    disk_kind: DiskKind,
    scope: Scope = Scope.ZONAL,
    role: SkuRole = SkuRole.CAPACITY,
) -> PriceSku | None:
    """Return the SKU for a disk kind, scope and price role, or ``None`` if absent."""
    for sku in book.skus:
        if classify_sku(sku.description, scope) == (disk_kind, role):
            return sku
    return None
