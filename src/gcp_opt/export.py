"""Export candidate rows for an external optimizer (cvxopt, MILP, spreadsheet).

Each row is a concrete configuration: machine shape (vCPU, memory, network, disk
ceilings) plus an optional disk (kind, size, provisioned IOPS), with monthly cost
components and achievable disk performance.  Selecting a subset subject to shared
budgets is a MILP; a continuous LP relaxation over per-kind sizes is also possible.
Either way the matrix below is the input.
"""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from gcp_opt.catalog import Catalog
from gcp_opt.models import ConfigOption, DiskKind, MachineTypeInfo, Scope
from gcp_opt.units import DecimalLike

#: Stable column order for exported configuration matrices.
COLUMNS: tuple[str, ...] = (
    # machine
    "machine_type",
    "family",
    "guest_cpus",
    "memory_gb",
    "network_egress_gbps",
    "network_tier1_egress_gbps",
    "maximum_persistent_disks",
    "maximum_total_size_gib",
    # disk
    "disk_kind",
    "scope",
    "size_gib",
    "provisioned_iops",
    "read_iops",
    "write_iops",
    "read_mibps",
    "write_mibps",
    "instance_bound",
    "instance_limit_known",
    # cost
    "machine_monthly_cost_usd",
    "disk_monthly_cost_usd",
    "capacity_monthly_cost_usd",
    "provisioned_iops_monthly_cost_usd",
    "monthly_cost_usd",
    "cost_basis",
    "price_sku_id",
)

#: Columns an objective/constraint vector should use (numeric only).
NUMERIC_COLUMNS: tuple[str, ...] = (
    "guest_cpus",
    "memory_gb",
    "network_egress_gbps",
    "maximum_persistent_disks",
    "maximum_total_size_gib",
    "size_gib",
    "provisioned_iops",
    "machine_monthly_cost_usd",
    "disk_monthly_cost_usd",
    "capacity_monthly_cost_usd",
    "provisioned_iops_monthly_cost_usd",
    "monthly_cost_usd",
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
    options: tuple[ConfigOption, ...]

    def numeric_columns(self) -> dict[str, list[float]]:
        """Return the numeric columns as float lists (``nan`` for missing values)."""
        index = {name: position for position, name in enumerate(self.columns)}
        result: dict[str, list[float]] = {}
        for name in NUMERIC_COLUMNS:
            position = index[name]
            result[name] = [_as_float(row[position]) for row in self.rows]
        return result


def _as_float(value: object) -> float:
    if value is None or value == "":
        return math.nan
    return float(str(value))


def _config_cells(option: ConfigOption) -> dict[str, object]:
    machine = option.machine
    disk = option.disk
    return {
        "machine_type": machine.name,
        "family": machine.family,
        "guest_cpus": machine.guest_cpus,
        "memory_gb": machine.memory_gb,
        "network_egress_gbps": machine.network_egress_gbps,
        "network_tier1_egress_gbps": machine.network_tier1_egress_gbps,
        "maximum_persistent_disks": machine.maximum_persistent_disks,
        "maximum_total_size_gib": machine.maximum_total_size_gib,
        "disk_kind": disk.disk_kind.value if disk else None,
        "scope": disk.scope.value if disk else None,
        "size_gib": disk.size_gib if disk else None,
        "provisioned_iops": disk.provisioned_iops if disk else None,
        "read_iops": disk.read_iops if disk else None,
        "write_iops": disk.write_iops if disk else None,
        "read_mibps": disk.read_mibps if disk else None,
        "write_mibps": disk.write_mibps if disk else None,
        "instance_bound": disk.instance_bound if disk else None,
        "instance_limit_known": disk.instance_limit_known if disk else None,
        "machine_monthly_cost_usd": option.machine_monthly_cost_usd,
        "disk_monthly_cost_usd": disk.monthly_cost_usd if disk else None,
        "capacity_monthly_cost_usd": disk.capacity_monthly_cost_usd if disk else None,
        "provisioned_iops_monthly_cost_usd": (
            disk.provisioned_iops_monthly_cost_usd if disk else None
        ),
        "monthly_cost_usd": option.monthly_cost_usd,
        "cost_basis": option.cost_basis.value,
        "price_sku_id": disk.price_sku_id if disk else None,
    }


def _matrix_from_options(options: Sequence[ConfigOption]) -> CandidateMatrix:
    rows = tuple(
        tuple(_normalize(cells[column]) for column in COLUMNS)
        for cells in (_config_cells(option) for option in options)
    )
    return CandidateMatrix(columns=COLUMNS, rows=rows, options=tuple(options))


def _normalize(value: object) -> object:
    return str(value) if isinstance(value, Decimal) else value


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
    provisioned_iops: DecimalLike | None = None,
    allow_us_list_price: bool = False,
) -> CandidateMatrix:
    """Build the machine+disk candidate matrix across machines, kinds and sizes."""
    options: list[ConfigOption] = []
    for machine_type in machine_types:
        for kind in disk_kinds:
            for size in sizes_gib:
                options.append(
                    catalog.config_option(
                        machine_type,
                        region=region,
                        disk_kind=kind,
                        size_gib=size,
                        scope=scope,
                        provisioned_iops=provisioned_iops,
                        allow_us_list_price=allow_us_list_price,
                    )
                )
    return _matrix_from_options(options)


def machine_matrix(
    catalog: Catalog,
    machine_types: Iterable[MachineTypeInfo],
    *,
    region: str | None = None,
) -> CandidateMatrix:
    """Build a machine-only candidate matrix (no disk columns populated)."""
    options = [
        catalog.machine_only_option(info.name, region=region) for info in machine_types
    ]
    return _matrix_from_options(options)


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
