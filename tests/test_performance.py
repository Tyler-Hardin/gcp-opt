"""Performance-model tests, including golden checks against Google's own tables.

The golden test parses ``tests/fixtures/doc_size_tables.json`` (generated from the
Persistent Disk performance page) and asserts that the formulas in
:mod:`gcp_opt.constants` reproduce every documented per-size row.  If Google
changes a scaling constant, this test fails and forces a deliberate update.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gcp_opt import constants
from gcp_opt.errors import InfeasibleTargetError
from gcp_opt.models import DiskKind, MachineTypeDiskLimit, Scope, SourceRef
from gcp_opt.performance import (
    achievable_performance,
    required_provisioned_iops,
    required_size_gib,
    saturation_size_gib,
)

_NUMBER_RE = re.compile(r"-?[0-9][0-9,]*(?:\.[0-9]+)?")


def _number(cell: str) -> Decimal:
    match = _NUMBER_RE.search(cell)
    assert match is not None, f"no number in {cell!r}"
    return Decimal(match.group(0).replace(",", ""))


def _limits(
    *,
    read_iops: int = 100_000,
    write_iops: int = 100_000,
    read_mibps: int = 1200,
    write_mibps: int = 1200,
) -> MachineTypeDiskLimit:
    return MachineTypeDiskLimit(
        machine_type="test-type",
        family="test",
        family_id="test",
        disk_kind=DiskKind.PD_SSD,
        scope=Scope.ZONAL,
        max_read_iops=Decimal(read_iops),
        max_write_iops=Decimal(write_iops),
        max_read_mibps=Decimal(read_mibps),
        max_write_mibps=Decimal(write_mibps),
        source=SourceRef(url="test://limits"),
    )


def _fixture_tables(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload["tables"])


@pytest.mark.parametrize("kind", ["ssd", "balanced", "standard"])
def test_formulas_reproduce_documented_zonal_size_tables(kind: str, fixtures_dir: Path) -> None:
    tables = [
        table
        for table in _fixture_tables(fixtures_dir / "doc_size_tables.json")
        if table["scope"] == "zonal" and table["kind"] == kind
    ]
    assert tables, f"no zonal {kind} table in fixture"
    table = tables[0]
    headers = [str(header) for header in table["headers"]]
    model = constants.ZONAL_DISK_MODELS[DiskKind(f"pd-{kind}")]

    combined_iops = any("read/write" in header for header in headers)
    combined_throughput = any("read/write" in header for header in headers)

    checked = 0
    for raw_row in table["rows"]:
        row = [str(cell) for cell in raw_row]
        size = _number(row[0])
        values = {
            header.lower(): _number(cell)
            for header, cell in zip(headers[1:], row[1:], strict=False)
        }
        envelope = achievable_performance(model, size, machine_limit=None)
        pairs: list[tuple[str, Decimal, Decimal]] = []
        if combined_iops:
            pairs.append(("read_iops", values["maximum (read/write) iops"], envelope.read_iops))
            pairs.append(("write_iops", values["maximum (read/write) iops"], envelope.write_iops))
        elif "maximum iops" in values:
            pairs.append(("read_iops", values["maximum iops"], envelope.read_iops))
            pairs.append(("write_iops", values["maximum iops"], envelope.write_iops))
        else:
            pairs.append(("read_iops", values["maximum read iops"], envelope.read_iops))
            pairs.append(("write_iops", values["maximum write iops"], envelope.write_iops))
        if combined_throughput:
            pairs.append(
                (
                    "read_mibps",
                    values["maximum (read/write) throughput (mib/s)"],
                    envelope.read_mibps,
                )
            )
            pairs.append(
                (
                    "write_mibps",
                    values["maximum (read/write) throughput (mib/s)"],
                    envelope.write_mibps,
                )
            )
        else:
            pairs.append(
                ("read_mibps", values["maximum read throughput (mib/s)"], envelope.read_mibps)
            )
            pairs.append(
                ("write_mibps", values["maximum write throughput (mib/s)"], envelope.write_mibps)
            )

        for name, documented, computed in pairs:
            # Google's own tables round sub-unit values inconsistently (e.g. the
            # Standard PD 3,334 GiB row lists 2,500 IOPS for 2,500.5 and 400 MiB/s
            # for 400.08), so allow a one-unit / 0.1 MiB/s absolute tolerance.
            tolerance = Decimal(1) if name.endswith("iops") else Decimal("0.1")
            assert abs(computed - documented) <= tolerance, (
                f"{kind} {size} GiB {name}: documented {documented}, computed {computed}"
            )
        checked += 1
    assert checked >= 10


def test_documented_500gib_ssd_example_from_docs() -> None:
    # Docs example: a 500 GiB SSD PD reaches 0.48 * 500 + 240 = 480 MiB/s.
    model = constants.ZONAL_DISK_MODELS[DiskKind.PD_SSD]
    envelope = achievable_performance(model, 500)
    assert envelope.read_mibps == Decimal("480")
    assert envelope.write_mibps == Decimal("480")
    assert envelope.read_iops == Decimal(21000)


def test_balanced_baseline_offsets_are_not_the_wrong_ones() -> None:
    # Regression guard for the widely copied but wrong "baseline_iops 1200/3000".
    balanced = constants.ZONAL_DISK_MODELS[DiskKind.PD_BALANCED]
    ssd = constants.ZONAL_DISK_MODELS[DiskKind.PD_SSD]
    assert balanced.iops_base_read == Decimal(3000)
    assert balanced.throughput_mibps_base_read == Decimal(140)
    assert ssd.iops_base_read == Decimal(6000)
    assert ssd.throughput_mibps_base_read == Decimal(240)


def test_instance_limit_wins_over_disk_scaling() -> None:
    model = constants.ZONAL_DISK_MODELS[DiskKind.PD_SSD]
    # A 4-vCPU N2 SSD ceiling is 240 MiB/s, far below 0.48 * 4000 + 240.
    envelope = achievable_performance(
        model, 4000, machine_limit=_limits(read_mibps=240, write_mibps=240)
    )
    assert envelope.read_mibps == Decimal(240)
    assert envelope.binding.read_mibps.value == "instance_machine_type"
    assert envelope.binding.instance_bound is True


def test_type_cap_wins_when_machine_ceiling_is_higher() -> None:
    model = constants.ZONAL_DISK_MODELS[DiskKind.PD_SSD]
    envelope = achievable_performance(
        model, 4000, machine_limit=_limits(read_mibps=100_000, write_mibps=100_000)
    )
    # 0.48 * 4000 + 240 = 2160, capped at the disk-type 1200.
    assert envelope.read_mibps == Decimal(1200)
    assert envelope.binding.read_mibps.value == "disk_type_cap"


def test_required_size_is_inverse_of_achievable() -> None:
    model = constants.ZONAL_DISK_MODELS[DiskKind.PD_SSD]
    target = achievable_performance(model, 1000).read_mibps
    size = required_size_gib(model, read_mibps=target)
    assert size <= Decimal(1000)
    assert achievable_performance(model, size).read_mibps >= target


def test_required_size_rejects_target_above_instance_cap() -> None:
    model = constants.ZONAL_DISK_MODELS[DiskKind.PD_SSD]
    with pytest.raises(InfeasibleTargetError) as info:
        required_size_gib(model, read_mibps=Decimal(5000), machine_limit=_limits(read_mibps=1200))
    assert info.value.limit_kind == "instance_machine_type"


def test_saturation_size_is_where_growth_stops() -> None:
    model = constants.ZONAL_DISK_MODELS[DiskKind.PD_BALANCED]
    limit = _limits(read_mibps=1200, write_mibps=1200)
    saturation = saturation_size_gib(model, limit, throughput=True)
    assert saturation is not None
    at = achievable_performance(model, saturation, machine_limit=limit).read_mibps
    beyond = achievable_performance(model, saturation * 2, machine_limit=limit).read_mibps
    assert at == beyond


@given(
    st.decimals(min_value=Decimal(1), max_value=Decimal(100_000), places=2),
    st.decimals(min_value=Decimal(1), max_value=Decimal(100_000), places=2),
)
def test_achievable_is_monotonic_in_size(small: Decimal, large: Decimal) -> None:
    low, high = sorted((small, large))
    model = constants.ZONAL_DISK_MODELS[DiskKind.PD_BALANCED]
    low_env = achievable_performance(model, low)
    high_env = achievable_performance(model, high)
    assert high_env.read_mibps >= low_env.read_mibps
    assert high_env.read_iops >= low_env.read_iops
    assert high_env.write_mibps >= low_env.write_mibps


def test_negative_size_rejected() -> None:
    model = constants.ZONAL_DISK_MODELS[DiskKind.PD_SSD]
    with pytest.raises(ValueError, match="non-negative"):
        achievable_performance(model, Decimal(-1))


def test_provisioned_iops_disk_requires_iops() -> None:
    model = constants.ZONAL_DISK_MODELS[DiskKind.PD_EXTREME]
    with pytest.raises(ValueError, match="provisioned"):
        achievable_performance(model, 1000)
    envelope = achievable_performance(model, 1000, provisioned_iops=10_000)
    assert envelope.read_iops == Decimal(10_000)
    assert envelope.read_mibps == Decimal(2500)  # 10,000 * 256 KiB/s


def test_required_provisioned_iops_from_throughput() -> None:
    model = constants.ZONAL_DISK_MODELS[DiskKind.PD_EXTREME]
    # 4,000 MiB/s needs 16,000 IOPS at 0.25 MiB/s per IOPS.
    assert required_provisioned_iops(model, read_mibps=Decimal(4000)) == Decimal(16000)


def test_required_provisioned_iops_from_iops_and_minimum() -> None:
    model = constants.ZONAL_DISK_MODELS[DiskKind.PD_EXTREME]
    assert required_provisioned_iops(model, read_iops=Decimal(5000)) == Decimal(5000)
    # No targets -> the documented 2,500 IOPS floor.
    assert required_provisioned_iops(model) == Decimal(2500)
    # A tiny throughput request is still raised to the floor.
    assert required_provisioned_iops(model, read_mibps=Decimal(100)) == Decimal(2500)


def test_required_provisioned_iops_respects_caps() -> None:
    model = constants.ZONAL_DISK_MODELS[DiskKind.PD_EXTREME]
    with pytest.raises(InfeasibleTargetError) as info:
        required_provisioned_iops(model, read_mibps=Decimal(4001))  # type cap
    assert info.value.limit_kind == "disk_type_cap"

    with pytest.raises(InfeasibleTargetError) as info:
        required_provisioned_iops(
            model,
            read_mibps=Decimal(3000),
            machine_limit=_limits(read_mibps=2500, read_iops=120000),
        )
    assert info.value.limit_kind == "instance_machine_type"

    with pytest.raises(ValueError, match="size-scaled"):
        required_provisioned_iops(
            constants.ZONAL_DISK_MODELS[DiskKind.PD_SSD], read_iops=Decimal(10)
        )
