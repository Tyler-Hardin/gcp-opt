"""Snapshot hashing, tamper detection and round-tripping."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from gcp_opt.errors import SnapshotIntegrityError, SnapshotNotFoundError
from gcp_opt.models import (
    MachineTypeInfo,
    Provenance,
    Snapshot,
    SnapshotKind,
    SourceMethod,
    SourceRef,
)
from gcp_opt.snapshot import build_snapshot, payload_digest, read_snapshot, write_snapshot

_ADAPTER = TypeAdapter(Snapshot[list[MachineTypeInfo]])


def _provenance() -> Provenance:
    return Provenance(
        method=SourceMethod.MANUAL,
        source_url="test://source",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        generator="tests",
    )


def _infos() -> list[MachineTypeInfo]:
    return [
        MachineTypeInfo(
            name="n2-standard-8",
            family="n2",
            guest_cpus=8,
            memory_gb=None,
            source=SourceRef(url="test://mt"),
        )
    ]


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "snap.json"
    snapshot = build_snapshot(_infos(), kind=SnapshotKind.MACHINE_TYPES, provenance=_provenance())
    write_snapshot(snapshot, path)
    loaded = read_snapshot(path, _ADAPTER)
    assert loaded.payload == _infos()
    assert loaded.provenance.method is SourceMethod.MANUAL


def test_digest_is_order_independent_for_keys() -> None:
    assert payload_digest({"a": 1, "b": 2}) == payload_digest({"b": 2, "a": 1})


def test_tampering_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "snap.json"
    snapshot = build_snapshot(_infos(), kind=SnapshotKind.MACHINE_TYPES, provenance=_provenance())
    write_snapshot(snapshot, path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["payload"][0]["guest_cpus"] = 999  # silent edit
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SnapshotIntegrityError, match="digest mismatch"):
        read_snapshot(path, _ADAPTER)


def test_missing_snapshot_raises(tmp_path: Path) -> None:
    with pytest.raises(SnapshotNotFoundError):
        read_snapshot(tmp_path / "nope.json", _ADAPTER)
