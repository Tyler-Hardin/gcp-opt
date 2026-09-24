"""Reading and writing hash-verified snapshot files.

A snapshot is a pydantic :class:`~gcp_opt.models.Snapshot` envelope plus a
``payload_sha256`` digest of the canonical JSON payload.  Loading a snapshot
recomputes the digest, so a hand-edited or corrupted data file fails loudly
instead of silently feeding wrong numbers to the optimizer.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from gcp_opt.errors import SnapshotIntegrityError, SnapshotNotFoundError
from gcp_opt.models import PayloadT, Provenance, Snapshot, SnapshotKind

#: Directory holding the committed bootstrap snapshots shipped with the package.
DATA_DIR: Path = Path(__file__).resolve().parent / "data"

#: Human-readable generator string stamped into every snapshot this package writes.
GENERATOR: str = "gcp-opt"


def canonical_payload(payload: Any) -> Any:  # noqa: ANN401 - recursive JSON normalizer
    """Normalize a payload into JSON primitives for stable hashing."""
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    if isinstance(payload, (list, tuple)):
        return [canonical_payload(item) for item in payload]
    if isinstance(payload, dict):
        return {str(key): canonical_payload(value) for key, value in payload.items()}
    return payload


def payload_digest(payload: Any) -> str:  # noqa: ANN401 - accepts any JSON payload
    """Return the SHA-256 hex digest of a payload's canonical JSON form."""
    blob = json.dumps(
        canonical_payload(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_snapshot(
    payload: Any,  # noqa: ANN401 - accepts any JSON payload
    *,
    kind: SnapshotKind,
    provenance: Provenance,
) -> Snapshot[Any]:
    """Wrap a payload in a :class:`Snapshot` envelope with a computed digest."""
    return Snapshot[Any](
        kind=kind,
        provenance=provenance,
        payload_sha256=payload_digest(payload),
        payload=payload,
    )


def write_snapshot(snapshot: Snapshot[Any], path: Path) -> None:
    """Write a snapshot to ``path`` as pretty, deterministic JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")


def read_snapshot(
    path: Path,
    adapter: TypeAdapter[Snapshot[PayloadT]],
) -> Snapshot[PayloadT]:
    """Load and integrity-check a snapshot.

    Args:
        path: snapshot JSON file.
        adapter: a ``TypeAdapter(Snapshot[PayloadType])`` producing typed payloads.

    Raises:
        SnapshotNotFoundError: if ``path`` does not exist.
        SnapshotIntegrityError: if the recomputed payload digest differs.
    """
    if not path.exists():
        raise SnapshotNotFoundError(f"snapshot not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    snapshot = adapter.validate_python(raw)
    expected = payload_digest(snapshot.payload)
    if expected != snapshot.payload_sha256:
        raise SnapshotIntegrityError(
            f"{path}: payload digest mismatch "
            f"(expected {snapshot.payload_sha256}, computed {expected}); "
            "regenerate the snapshot with `python -m gcp_opt refresh` instead of editing it"
        )
    return snapshot
