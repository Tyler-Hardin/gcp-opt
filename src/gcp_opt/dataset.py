"""Load the committed snapshots into a typed, integrity-checked dataset."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from gcp_opt.models import (
    MachineTypeInfo,
    MachineTypeLimitTable,
    PriceBook,
    Provenance,
    Snapshot,
    SnapshotKind,
)
from gcp_opt.snapshot import DATA_DIR, read_snapshot

_LIMITS_ADAPTER = TypeAdapter(Snapshot[MachineTypeLimitTable])
_MACHINE_TYPES_ADAPTER = TypeAdapter(Snapshot[list[MachineTypeInfo]])
_PRICES_ADAPTER = TypeAdapter(Snapshot[PriceBook])

DEFAULT_LIMITS_PATH = DATA_DIR / "machine_type_disk_limits.json"
DEFAULT_MACHINE_TYPES_PATH = DATA_DIR / "machine_types.json"
DEFAULT_PRICES_PATH = DATA_DIR / "disk_prices.json"


@dataclass(frozen=True)
class Dataset:
    """The three snapshots the catalog joins, plus their provenance."""

    limits: MachineTypeLimitTable
    machine_types: Mapping[str, MachineTypeInfo]
    prices: PriceBook
    provenance: Mapping[SnapshotKind, Provenance]

    @classmethod
    def load(
        cls,
        *,
        limits_path: Path = DEFAULT_LIMITS_PATH,
        machine_types_path: Path = DEFAULT_MACHINE_TYPES_PATH,
        prices_path: Path = DEFAULT_PRICES_PATH,
    ) -> Dataset:
        """Load and integrity-check the three snapshot files.

        Raises:
            SnapshotNotFoundError: if a file is missing.
            SnapshotIntegrityError: if a file's payload does not match its digest.
        """
        limits_snapshot = read_snapshot(limits_path, _LIMITS_ADAPTER)
        machine_snapshot = read_snapshot(machine_types_path, _MACHINE_TYPES_ADAPTER)
        price_snapshot = read_snapshot(prices_path, _PRICES_ADAPTER)
        return cls(
            limits=limits_snapshot.payload,
            machine_types={info.name: info for info in machine_snapshot.payload},
            prices=price_snapshot.payload,
            provenance={
                SnapshotKind.MACHINE_TYPE_LIMITS: limits_snapshot.provenance,
                SnapshotKind.MACHINE_TYPES: machine_snapshot.provenance,
                SnapshotKind.PRICES: price_snapshot.provenance,
            },
        )

    @classmethod
    def load_bundled(cls) -> Dataset:
        """Load the snapshots shipped inside the installed package."""
        return cls.load()
