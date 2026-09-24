"""Export candidate rows for an external optimizer (cvxopt, MILP, spreadsheet).

The dataset is deliberately shipped as an explicit candidate matrix: each row is a
concrete (machine type, disk kind, size) with its monthly cost and achievable
read/write IOPS and throughput.  Selecting a subset subject to shared budgets is a
MILP over binary row-selection variables; a continuous LP relaxation over per-kind
sizes is also possible.  Either way the matrix below is the input.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from gcp_opt.catalog import Catalog
from gcp_opt.models import DiskKind, DiskOption, Scope
from gcp_opt.units import DecimalLike

#: Stable column order for exported candidate matrices.
COLUMNS: tuple[str, ...] = (
    "machine_type",
    "region",
    "disk_kind",
    "scope",
    "size_gib",
    "monthly_cost_usd",
    "read_iops",
    "write_iops",
    "read_mibps",
    "write_mibps",
    "instance_bound",
    "instance_limit_known",
    "price_sku_id",
)

#: Columns an objective/constraint vector should use (numeric only).
NUMERIC_COLUMNS: tuple[str, ...] = (
    "monthly_cost_usd",
    "size_gib",
    "read_iops",
    "write_iops",
    "read_mibps",
    "write_mibps",
)


@dataclass(frozen=True)
class CandidateMatrix:
    """Row-oriented candidate table plus its options in row order."""

    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    options: tuple[DiskOption, ...]

    def numeric_columns(self) -> dict[str, list[float]]:
        """Return the numeric columns as float lists (for cvxopt/numpy)."""
        index = {name: position for position, name in enumerate(self.columns)}
        result: dict[str, list[float]] = {}
        for name in NUMERIC_COLUMNS:
            position = index[name]
            result[name] = [float(str(row[position])) for row in self.rows]
        return result


def candidate_matrix(
    catalog: Catalog,
    machine_types: Iterable[str],
    sizes_gib: Iterable[DecimalLike],
    *,
    region: str | None = None,
    disk_kinds: Sequence[DiskKind] = (
        DiskKind.PD_STANDARD,
        DiskKind.PD_BALANCED,
        DiskKind.PD_SSD,
    ),
    scope: Scope = Scope.ZONAL,
    allow_us_list_price: bool = False,
) -> CandidateMatrix:
    """Build the candidate matrix across machines, kinds and sizes."""
    options: list[DiskOption] = []
    for machine_type in machine_types:
        options.extend(
            catalog.disk_options(
                machine_type,
                sizes_gib,
                region=region,
                disk_kinds=disk_kinds,
                scope=scope,
                allow_us_list_price=allow_us_list_price,
            )
        )
    rows = tuple(tuple(_cell(option, column) for column in COLUMNS) for option in options)
    return CandidateMatrix(columns=COLUMNS, rows=rows, options=tuple(options))


def _cell(option: DiskOption, column: str) -> object:
    value = getattr(option, column)
    return str(value) if isinstance(value, Decimal) else value


def write_csv(matrix: CandidateMatrix, path: Path) -> None:
    """Write the candidate matrix as CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(matrix.columns)
        writer.writerows(matrix.rows)


def write_json(matrix: CandidateMatrix, path: Path) -> None:
    """Write the candidate matrix as JSON (exact decimal strings preserved)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [dict(zip(matrix.columns, row, strict=True)) for row in matrix.rows]
    path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
