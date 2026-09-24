"""Compute Engine API client for the ``machineTypes`` resource.

Endpoint: ``GET https://compute.googleapis.com/compute/v1/projects/{project}/zones/{zone}/machineTypes/{machineType}``

Scope note (verified against the public discovery document, revision 20260910):
``MachineType`` exposes ``guestCpus``, ``memoryMb``, ``maximumPersistentDisks`` and
``maximumPersistentDisksSizeGb``.  It does **not** expose any disk IOPS or
throughput field, and there is no ``capabilities`` block.  Per-VM disk
performance ceilings therefore come from the documentation snapshot, while this
client supplies the machine shape and the disk-count/size ceilings.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from gcp_opt import constants
from gcp_opt.errors import ApiError
from gcp_opt.http import JsonTransport, UrllibJsonTransport
from gcp_opt.models import MachineTypeInfo, SourceRef


def _family_of(name: str) -> str:
    return name.split("-", 1)[0]


def _zone_from_url(value: str | None) -> str | None:
    if not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]


def parse_machine_type(raw: Mapping[str, Any]) -> MachineTypeInfo:
    """Convert a Compute API ``MachineType`` object into :class:`MachineTypeInfo`."""
    name = str(raw.get("name", ""))
    if not name:
        raise ApiError("machine type payload is missing 'name'")

    memory_mb = raw.get("memoryMb")
    memory_gb = Decimal(memory_mb) / Decimal(1024) if memory_mb is not None else None

    size_raw = raw.get("maximumPersistentDisksSizeGb")
    total_size_gib: Decimal | None = None
    if size_raw is not None:
        try:
            total_size_gib = Decimal(str(size_raw))
        except InvalidOperation as error:  # pragma: no cover - defensive
            raise ApiError(f"invalid maximumPersistentDisksSizeGb {size_raw!r}") from error

    return MachineTypeInfo(
        name=name,
        family=_family_of(name),
        guest_cpus=int(raw["guestCpus"]) if raw.get("guestCpus") is not None else None,
        memory_gb=memory_gb,
        maximum_persistent_disks=(
            int(raw["maximumPersistentDisks"])
            if raw.get("maximumPersistentDisks") is not None
            else None
        ),
        maximum_total_size_gib=total_size_gib,
        zone=_zone_from_url(raw.get("zone")),
        architecture=raw.get("architecture"),
        is_shared_cpu=raw.get("isSharedCpu"),
        source=SourceRef(
            url="https://cloud.google.com/compute/docs/reference/rest/v1/machineTypes/get",
            note="Compute Engine API machineTypes resource.",
        ),
    )


class ComputeMachineTypeClient:
    """Minimal client for the Compute Engine ``machineTypes`` collection."""

    def __init__(
        self,
        *,
        project: str,
        access_token: str,
        transport: JsonTransport | None = None,
        base_url: str = constants.COMPUTE_API_BASE_URL,
    ) -> None:
        if not project:
            raise ValueError("project is required")
        if not access_token:
            raise ValueError("access_token is required")
        self._project = project
        self._access_token = access_token
        self._transport = transport or UrllibJsonTransport()
        self._base_url = base_url.rstrip("/")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def get_machine_type(self, zone: str, name: str) -> MachineTypeInfo:
        """Fetch one machine type from a zone."""
        url = f"{self._base_url}/projects/{self._project}/zones/{zone}/machineTypes/{name}"
        return parse_machine_type(self._transport.get_json(url, headers=self._headers))

    def list_machine_types(self, zone: str, *, page_size: int = 500) -> list[MachineTypeInfo]:
        """List every machine type in one zone, following pagination."""
        url = f"{self._base_url}/projects/{self._project}/zones/{zone}/machineTypes"
        return self._collect(url, page_size=page_size, nested=False)

    def aggregated_list(self, *, page_size: int = 500) -> list[MachineTypeInfo]:
        """List machine types across all zones, de-duplicated by name."""
        url = f"{self._base_url}/projects/{self._project}/aggregated/machineTypes"
        return self._collect(url, page_size=page_size, nested=True)

    def _collect(self, url: str, *, page_size: int, nested: bool) -> list[MachineTypeInfo]:
        seen: dict[str, MachineTypeInfo] = {}
        token: str | None = None
        while True:
            params = {"maxResults": str(page_size)}
            if token:
                params["pageToken"] = token
            payload = self._transport.get_json(url, params=params, headers=self._headers)
            if nested:
                for scope in payload.get("items", {}).values():
                    for raw in scope.get("machineTypes", []):
                        info = parse_machine_type(raw)
                        seen.setdefault(info.name, info)
            else:
                for raw in payload.get("items", []):
                    info = parse_machine_type(raw)
                    seen.setdefault(info.name, info)
            token = payload.get("nextPageToken")
            if not token:
                break
        return sorted(seen.values(), key=lambda info: info.name)
