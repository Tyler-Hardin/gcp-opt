"""Offline tests for the docs parser used when regenerating snapshots."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

import doc_parse


@pytest.fixture(scope="module")
def snippet(fixtures_dir: Path) -> str:
    return (fixtures_dir / "perf_snippet.html").read_text(encoding="utf-8")


def test_parse_blocks_extracts_headings_and_tables(snippet: str) -> None:
    blocks = doc_parse.parse_blocks(snippet)
    kinds = [block.kind for block in blocks]
    assert "heading" in kinds
    assert "table" in kinds
    headings = [block.text for block in blocks if block.kind == "heading"]
    assert "T1 Instances" in headings


def test_footnote_superscript_is_not_glued_to_the_number(snippet: str) -> None:
    _machine_rows, vcpu_rows = doc_parse.parse_machine_series_limits(snippet)
    # "800<sup>1</sup>" must parse as 800, not 8001.
    row = next(r for r in vcpu_rows if r["family_id"] == "t1" and r["vcpu_label"] == "8-15")
    assert row["max_write_mibps"] == Decimal(800)
    assert row["max_read_mibps"] == Decimal(800)
    row64 = next(r for r in vcpu_rows if r["vcpu_label"] == "64 or more")
    assert row64["vcpus_min"] == 64
    assert row64["vcpus_max"] is None
    assert row64["max_write_mibps"] == Decimal(1200)


def test_disk_kind_heading_suffix_is_normalized(snippet: str) -> None:
    # The second pd-ssd heading has devsite's de-duplication suffix "pd-ssd_1".
    _, vcpu_rows = doc_parse.parse_machine_series_limits(snippet)
    assert any(row["disk_kind"] == "pd-ssd" for row in vcpu_rows)


def test_machine_type_keyed_rows_are_separated(snippet: str) -> None:
    machine_rows, _ = doc_parse.parse_machine_series_limits(snippet)
    row = next(r for r in machine_rows if r["machine_type"] == "t1-megagpu-8g")
    assert row["family_id"] == "t1"
    assert row["max_read_iops"] == Decimal(60000)


def test_example_family_is_excluded(snippet: str) -> None:
    machine_rows, vcpu_rows = doc_parse.parse_machine_series_limits(snippet)
    families = {str(r["family"]) for r in machine_rows + vcpu_rows}
    assert "Example" not in families


def test_parse_vcpu_label_cases() -> None:
    assert doc_parse.parse_vcpu_label("4") == (4, 4)
    assert doc_parse.parse_vcpu_label("2-7") == (2, 7)
    assert doc_parse.parse_vcpu_label("8 to 14") == (8, 14)
    assert doc_parse.parse_vcpu_label("64 or more") == (64, None)
    assert doc_parse.parse_vcpu_label("e2-medium1") is None


def test_parse_size_tables_classifies_scope_and_kind(snippet: str) -> None:
    tables = doc_parse.parse_size_tables(snippet)
    pairs = {(table["scope"], table["kind"]) for table in tables}
    assert ("zonal", "ssd") in pairs
    assert ("regional", "balanced") in pairs
    zonal_ssd = next(t for t in tables if t["scope"] == "zonal" and t["kind"] == "ssd")
    rows = cast("list[list[str]]", zonal_ssd["rows"])
    assert rows[0][0] == "10"


def test_parse_machine_specs_merges_tables(snippet: str) -> None:
    specs = {record["name"]: record for record in doc_parse.parse_machine_specs(snippet)}
    assert specs["t1-standard-4"]["guest_cpus"] == 4
    assert specs["t1-standard-4"]["memory_gb"] == Decimal(16)
    # Second table contributes the disk ceilings for the same machine type.
    assert specs["t1-standard-4"]["maximum_persistent_disks"] == 128
    assert specs["t1-standard-4"]["maximum_total_size_tib"] == Decimal(257)
