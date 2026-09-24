"""Candidate-matrix export and CLI smoke tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gcp_opt.catalog import Catalog
from gcp_opt.cli import main
from gcp_opt.export import COLUMNS, NUMERIC_COLUMNS, candidate_matrix, write_csv, write_json
from gcp_opt.models import DiskKind


def test_candidate_matrix_shape(catalog: Catalog) -> None:
    matrix = candidate_matrix(catalog, ["n2-standard-8"], [100, 500])
    assert matrix.columns == COLUMNS
    assert len(matrix.rows) == 6  # 2 sizes x 3 default disk kinds
    assert all(len(row) == len(COLUMNS) for row in matrix.rows)
    numeric = matrix.numeric_columns()
    assert set(numeric) == set(NUMERIC_COLUMNS)
    assert numeric["monthly_cost_usd"][1] > numeric["monthly_cost_usd"][0]


def test_candidate_matrix_includes_machine_columns(catalog: Catalog) -> None:
    matrix = candidate_matrix(catalog, ["n2-standard-8"], [100])
    index = {name: position for position, name in enumerate(matrix.columns)}
    row = matrix.rows[0]
    assert row[index["guest_cpus"]] == 8
    assert row[index["network_egress_gbps"]] == "16"
    assert row[index["cost_basis"]] == "disk_only"
    assert row[index["machine_monthly_cost_usd"]] is None


def test_machine_matrix(catalog: Catalog) -> None:
    from gcp_opt.export import machine_matrix

    infos = [catalog.machine_info("n2-standard-8")]
    matrix = machine_matrix(catalog, [i for i in infos if i is not None])
    assert len(matrix.rows) == 1
    index = {name: position for position, name in enumerate(matrix.columns)}
    assert matrix.rows[0][index["disk_kind"]] is None
    assert matrix.rows[0][index["guest_cpus"]] == 8


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


def test_cli_machines_lists_shapes() -> None:
    assert main(["machines", "--machine-types", "n2-standard-8,n2-standard-128"]) == 0
    assert main(["machines", "--family", "n2", "--sort", "memory"]) == 0


def test_cli_search_machine_objectives() -> None:
    assert main(["search", "--objective", "max_memory"]) == 0
    assert main(["search", "--objective", "max_network", "--min-memory", "512GiB"]) == 0
    assert main(["search", "--objective", "max_vcpus", "--family", "n2"]) == 0


def test_cli_search_disk_objective() -> None:
    assert (
        main(
            [
                "search",
                "--objective",
                "max_disk_read",
                "--family",
                "n2",
                "--min-size",
                "10TB",
                "--budget",
                "2000",
            ]
        )
        == 0
    )


def test_cli_search_infeasible_returns_2() -> None:
    assert (
        main(["search", "--objective", "max_memory", "--min-memory", "99999TiB"]) == 2
    )


def test_cli_export_machines_only(tmp_path: Path) -> None:
    out = tmp_path / "machines.csv"
    assert (
        main(
            [
                "export",
                "--family",
                "n2",
                "--machines-only",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert out.exists()


def test_cli_import_machine_prices(tmp_path: Path) -> None:
    price_file = tmp_path / "prices.json"
    price_file.write_text(
        json.dumps({"n2-standard-8": 0.5, "n2-standard-4": 0.25}), encoding="utf-8"
    )
    out = tmp_path / "machine_prices.json"
    assert (
        main(
            [
                "refresh-machine-prices",
                "--from-file",
                str(price_file),
                "--region",
                "us-central1",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert out.exists()


def test_cli_top_flag_limits_results() -> None:
    assert main(["max-bandwidth", "--budget", "3000", "--min-size", "10TB", "-n", "2"]) == 0
    assert main(["max-bandwidth", "--budget", "3000", "--min-size", "10TB", "--top", "3"]) == 0
    assert main(["min-cost", "--min-size", "10TB", "-n", "2"]) == 0
    assert main(["search", "--objective", "max_memory", "-n", "3"]) == 0


def test_cli_top_rejects_zero() -> None:
    with pytest.raises(SystemExit):
        main(["min-cost", "--min-size", "10TB", "-n", "0"])


def test_cli_table_shows_provisioned_iops(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "max-bandwidth",
                "--budget",
                "3000",
                "--min-size",
                "10TB",
                "--family",
                "n2",
                "-n",
                "2",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "provIOPS" in out


def test_cli_machines_shows_ram_and_net_after_machine_type(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["machines", "--machine-types", "n2-standard-8"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:3] == ["machine_type", "ram", "net"]
    row = lines[2].split()
    assert row[0] == "n2-standard-8"
    assert row[1] == "32"  # RAM in GiB
    assert row[2] == "16"  # default egress bandwidth in Gbps


def test_cli_machines_ram_is_terse_for_fractional_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["machines", "--machine-types", "n1-standard-1"]) == 0
    row = capsys.readouterr().out.splitlines()[2].split()
    assert row[0] == "n1-standard-1"
    assert row[1] == "3.75"  # not "3.8" or "3.750"
