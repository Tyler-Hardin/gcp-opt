"""Candidate-matrix export and CLI smoke tests."""

from __future__ import annotations

import json
from pathlib import Path

from gcp_opt.catalog import Catalog
from gcp_opt.cli import main
from gcp_opt.export import COLUMNS, candidate_matrix, write_csv, write_json
from gcp_opt.models import DiskKind


def test_candidate_matrix_shape(catalog: Catalog) -> None:
    matrix = candidate_matrix(catalog, ["n2-standard-8"], [100, 500])
    assert matrix.columns == COLUMNS
    assert len(matrix.rows) == 6  # 2 sizes x 3 default disk kinds
    assert all(len(row) == len(COLUMNS) for row in matrix.rows)
    numeric = matrix.numeric_columns()
    assert set(numeric) == {
        "monthly_cost_usd",
        "size_gib",
        "read_iops",
        "write_iops",
        "read_mibps",
        "write_mibps",
    }
    assert numeric["monthly_cost_usd"][1] > numeric["monthly_cost_usd"][0]


def test_candidate_matrix_single_kind(catalog: Catalog) -> None:
    matrix = candidate_matrix(
        catalog, ["n2-standard-8"], [1000], disk_kinds=(DiskKind.PD_SSD,)
    )
    assert len(matrix.rows) == 1
    assert matrix.options[0].disk_kind is DiskKind.PD_SSD


def test_write_csv_and_json(catalog: Catalog, tmp_path: Path) -> None:
    matrix = candidate_matrix(catalog, ["n2-standard-8"], [100, 500])
    csv_path = tmp_path / "out.csv"
    json_path = tmp_path / "out.json"
    write_csv(matrix, csv_path)
    write_json(matrix, json_path)

    lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 + len(matrix.rows)
    assert lines[0].split(",")[0] == "machine_type"

    records = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(records) == len(matrix.rows)
    assert records[0]["machine_type"] == "n2-standard-8"


def test_cli_sources_exits_zero(capsys: object) -> None:
    assert main(["sources"]) == 0


def test_cli_options_exits_zero() -> None:
    assert main(["options", "--machine-types", "n2-standard-8", "--sizes", "500,1000"]) == 0


def test_cli_min_cost_reports_infeasible(capsys: object) -> None:
    # A single VM cannot reach 10 GB/s of Persistent Disk throughput.
    assert main(["min-cost", "--min-size", "10TB", "--min-read-bandwidth", "10GBps"]) == 2


def test_cli_max_bandwidth_exits_zero() -> None:
    assert main(["max-bandwidth", "--budget", "2000", "--min-size", "10TB", "--family", "n2"]) == 0


def test_cli_export_writes_file(tmp_path: Path) -> None:
    out = tmp_path / "candidates.csv"
    assert (
        main(
            [
                "export",
                "--machine-types",
                "n2-standard-8",
                "--sizes",
                "100,500",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert out.exists()


def test_cli_refresh_prices_requires_credentials() -> None:
    assert main(["refresh-prices", "--region", "us-central1"]) == 2
