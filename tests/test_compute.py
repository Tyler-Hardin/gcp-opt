"""Compute Engine machineTypes client tests (no network)."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from gcp_opt.compute import ComputeMachineTypeClient, parse_machine_type
from gcp_opt.errors import ApiError


class FakeTransport:
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


_FULL = {
    "kind": "compute#machineType",
    "name": "n2-standard-8",
    "zone": "https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a",
    "guestCpus": 8,
    "memoryMb": 32768,
    "maximumPersistentDisks": 128,
    "maximumPersistentDisksSizeGb": "263168",
    "architecture": "X86_64",
    "isSharedCpu": False,
}


def test_parse_machine_type_normalizes_units() -> None:
    info = parse_machine_type(_FULL)
    assert info.name == "n2-standard-8"
    assert info.family == "n2"
    assert info.guest_cpus == 8
    assert info.memory_gb == Decimal(32)
    assert info.maximum_persistent_disks == 128
    # Google returns the size field as a string of gibibytes (257 TiB).
    assert info.maximum_total_size_gib == Decimal(263168)
    assert info.zone == "us-central1-a"
    assert info.architecture == "X86_64"


def test_parse_machine_type_tolerates_missing_optional_fields() -> None:
    info = parse_machine_type({"name": "e2-micro", "guestCpus": 2})
    assert info.maximum_persistent_disks is None
    assert info.maximum_total_size_gib is None
    assert info.zone is None


def test_parse_machine_type_requires_name() -> None:
    with pytest.raises(ApiError, match="missing 'name'"):
        parse_machine_type({"guestCpus": 2})


def test_get_machine_type_builds_correct_url() -> None:
    transport = FakeTransport([_FULL])
    client = ComputeMachineTypeClient(project="my-proj", access_token="TOKEN", transport=transport)
    info = client.get_machine_type("us-central1-a", "n2-standard-8")
    assert info.name == "n2-standard-8"
    call = transport.calls[0]
    assert call["url"].endswith("/projects/my-proj/zones/us-central1-a/machineTypes/n2-standard-8")
    assert call["headers"]["Authorization"] == "Bearer TOKEN"


def test_list_machine_types_follows_pagination() -> None:
    transport = FakeTransport(
        [
            {"items": [{"name": "n2-standard-2", "guestCpus": 2}], "nextPageToken": "t2"},
            {"items": [{"name": "n2-standard-4", "guestCpus": 4}]},
        ]
    )
    client = ComputeMachineTypeClient(project="p", access_token="T", transport=transport)
    infos = client.list_machine_types("us-central1-a")
    assert [info.name for info in infos] == ["n2-standard-2", "n2-standard-4"]
    assert transport.calls[1]["params"]["pageToken"] == "t2"


def test_aggregated_list_dedupes_across_zones() -> None:
    transport = FakeTransport(
        [
            {
                "items": {
                    "zones/us-central1-a": {
                        "machineTypes": [{"name": "n2-standard-8", "guestCpus": 8}]
                    },
                    "zones/us-central1-b": {
                        "machineTypes": [{"name": "n2-standard-8", "guestCpus": 8}]
                    },
                }
            }
        ]
    )
    client = ComputeMachineTypeClient(project="p", access_token="T", transport=transport)
    infos = client.aggregated_list()
    assert [info.name for info in infos] == ["n2-standard-8"]


def test_client_requires_project_and_token() -> None:
    with pytest.raises(ValueError, match="project is required"):
        ComputeMachineTypeClient(project="", access_token="T", transport=FakeTransport([]))
    with pytest.raises(ValueError, match="access_token is required"):
        ComputeMachineTypeClient(project="p", access_token="", transport=FakeTransport([]))
