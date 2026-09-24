"""Command-line interface: ``python -m gcp_opt <command>``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from gcp_opt import constants, units
from gcp_opt.catalog import Catalog
from gcp_opt.compute import ComputeMachineTypeClient
from gcp_opt.dataset import Dataset
from gcp_opt.errors import GcpOptError, InfeasibleTargetError
from gcp_opt.export import candidate_matrix, write_csv, write_json
from gcp_opt.models import DiskKind, DiskOption, MachineTypeInfo, SnapshotKind
from gcp_opt.pricing import BillingCatalogClient
from gcp_opt.query import (
    DEFAULT_DISK_KINDS,
    Requirement,
    max_throughput_option,
    min_cost_option,
)
from gcp_opt.snapshot import DATA_DIR, GENERATOR, build_snapshot, write_snapshot

_ALL_KINDS = tuple(kind.value for kind in DiskKind)


def _load_catalog() -> Catalog:
    return Catalog(Dataset.load_bundled())


def _parse_kinds(value: str) -> tuple[DiskKind, ...]:
    kinds: list[DiskKind] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            kinds.append(DiskKind(token))
        except ValueError as error:
            raise argparse.ArgumentTypeError(
                f"unknown disk kind {token!r}; choose from {', '.join(_ALL_KINDS)}"
            ) from error
    return tuple(kinds) or DEFAULT_DISK_KINDS


def _parse_sizes(value: str) -> list[Decimal]:
    sizes: list[Decimal] = []
    for token in value.split(","):
        token = token.strip()
        if token:
            sizes.append(units.parse_size_gib(token))
    if not sizes:
        raise argparse.ArgumentTypeError("no sizes given")
    return sizes


def _select_machines(
    catalog: Catalog, *, machine_types: str | None, family: str | None
) -> list[str]:
    if machine_types:
        names = [name.strip() for name in machine_types.split(",") if name.strip()]
    else:
        names = catalog.machine_names()
    if family:
        names = [name for name in names if name.split("-", 1)[0] == family]
    if not names:
        raise GcpOptError("no machine types selected; check --machine-types/--family")
    return names


def _fmt(value: Decimal, places: int = 2) -> str:
    return f"{units.quantize(value, places):.{places}f}"


def _print_options(options: list[DiskOption]) -> None:
    header = (
        f"{'machine_type':22s} {'disk':12s} {'size GiB':>12s} {'$/mo':>9s} "
        f"{'rIOPS':>8s} {'wIOPS':>8s} {'rMiB/s':>8s} {'wMiB/s':>8s} {'vm-bound':>8s}"
    )
    print(header)
    print("-" * len(header))
    for option in options:
        print(
            f"{option.machine_type:22s} {option.disk_kind.value:12s} "
            f"{_fmt(option.size_gib, 0):>12s} {_fmt(option.monthly_cost_usd):>9s} "
            f"{_fmt(option.read_iops, 0):>8s} {_fmt(option.write_iops, 0):>8s} "
            f"{_fmt(option.read_mibps):>8s} {_fmt(option.write_mibps):>8s} "
            f"{'yes' if option.instance_bound else 'no':>8s}"
        )


def _option_payload(option: DiskOption) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(option.model_dump_json()))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_sources(_: argparse.Namespace) -> int:
    dataset = Dataset.load_bundled()
    for kind in (
        SnapshotKind.MACHINE_TYPE_LIMITS,
        SnapshotKind.MACHINE_TYPES,
        SnapshotKind.PRICES,
    ):
        provenance = dataset.provenance[kind]
        print(f"{kind.value}:")
        print(f"  method     : {provenance.method.value}")
        print(f"  source     : {provenance.source_url}")
        print(f"  retrieved  : {provenance.retrieved_at.isoformat()}")
        if provenance.notes:
            print(f"  notes      : {provenance.notes}")
    return 0


def cmd_options(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    machines = _select_machines(catalog, machine_types=args.machine_types, family=args.family)
    options: list[DiskOption] = []
    for machine in machines:
        options.extend(
            catalog.disk_options(
                machine,
                args.sizes,
                region=args.region,
                disk_kinds=_parse_kinds(args.kinds),
                allow_us_list_price=args.allow_us_list_price,
            )
        )
    if args.json:
        print(json.dumps([_option_payload(option) for option in options], indent=2))
    else:
        _print_options(options)
    return 0


def cmd_min_cost(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    machines = _select_machines(catalog, machine_types=args.machine_types, family=args.family)
    requirement = Requirement.build(
        min_total_size_gib=units.parse_size_gib(args.min_size) if args.min_size else 0,
        min_read_mibps=(
            units.parse_throughput_mibps(args.min_read_bandwidth)
            if args.min_read_bandwidth
            else None
        ),
        min_write_mibps=(
            units.parse_throughput_mibps(args.min_write_bandwidth)
            if args.min_write_bandwidth
            else None
        ),
        min_read_iops=Decimal(args.min_read_iops) if args.min_read_iops else None,
        min_write_iops=Decimal(args.min_write_iops) if args.min_write_iops else None,
        max_monthly_cost_usd=Decimal(args.max_monthly_cost) if args.max_monthly_cost else None,
    )
    try:
        option = min_cost_option(
            catalog,
            machines,
            requirement=requirement,
            region=args.region,
            disk_kinds=_parse_kinds(args.kinds),
            allow_us_list_price=args.allow_us_list_price,
        )
    except InfeasibleTargetError as error:
        print(f"infeasible: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(_option_payload(option), indent=2))
    else:
        _print_options([option])
    return 0


def cmd_max_bandwidth(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    machines = _select_machines(catalog, machine_types=args.machine_types, family=args.family)
    try:
        option = max_throughput_option(
            catalog,
            machines,
            budget_usd=Decimal(args.budget),
            min_total_size_gib=units.parse_size_gib(args.min_size) if args.min_size else 0,
            metric=args.metric,
            region=args.region,
            disk_kinds=_parse_kinds(args.kinds),
            allow_us_list_price=args.allow_us_list_price,
        )
    except InfeasibleTargetError as error:
        print(f"infeasible: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(_option_payload(option), indent=2))
    else:
        _print_options([option])
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    machines = _select_machines(catalog, machine_types=args.machine_types, family=args.family)
    matrix = candidate_matrix(
        catalog,
        machines,
        args.sizes,
        region=args.region,
        disk_kinds=_parse_kinds(args.kinds),
        allow_us_list_price=args.allow_us_list_price,
    )
    out = Path(args.out)
    if args.format == "json":
        write_json(matrix, out)
    else:
        write_csv(matrix, out)
    print(f"wrote {len(matrix.rows)} candidate rows to {out}")
    return 0


def cmd_refresh_prices(args: argparse.Namespace) -> int:
    api_key = args.api_key or os.environ.get("GCP_BILLING_API_KEY")
    token = args.access_token or os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
    if not api_key and not token:
        print(
            "error: provide --api-key/--access-token (or GCP_BILLING_API_KEY / "
            "GOOGLE_OAUTH_ACCESS_TOKEN)",
            file=sys.stderr,
        )
        return 2
    client = BillingCatalogClient(api_key=api_key, access_token=token)
    book = client.fetch_disk_price_book(region=args.region, currency_code=args.currency)
    provenance = book.provenance
    path = Path(args.out) if args.out else DATA_DIR / "disk_prices.json"
    write_snapshot(build_snapshot(book, kind=SnapshotKind.PRICES, provenance=provenance), path)
    print(f"wrote {len(book.skus)} SKUs for {args.region} to {path}")
    return 0


def cmd_refresh_machine_types(args: argparse.Namespace) -> int:
    token = args.access_token or os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
    if not token:
        print("error: provide --access-token or GOOGLE_OAUTH_ACCESS_TOKEN", file=sys.stderr)
        return 2
    client = ComputeMachineTypeClient(project=args.project, access_token=token)
    infos: list[MachineTypeInfo] = client.aggregated_list()
    if not infos:
        print("error: Compute API returned no machine types", file=sys.stderr)
        return 2
    from datetime import UTC, datetime

    from gcp_opt.models import Provenance, SourceMethod

    source_url = (
        f"{constants.COMPUTE_API_BASE_URL}/projects/{args.project}/aggregated/machineTypes"
    )
    provenance = Provenance(
        method=SourceMethod.COMPUTE_MACHINE_TYPES,
        source_url=source_url,
        retrieved_at=datetime.now(UTC).replace(microsecond=0),
        generator=GENERATOR,
        notes="Live Compute Engine API machineTypes aggregatedList.",
    )
    path = Path(args.out) if args.out else DATA_DIR / "machine_types.json"
    write_snapshot(
        build_snapshot(infos, kind=SnapshotKind.MACHINE_TYPES, provenance=provenance), path
    )
    print(f"wrote {len(infos)} machine types to {path}")
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gcp-opt",
        description=(
            "Grounded GCP disk cost/performance data for an external optimizer. "
            "Sizes accept GB/TB (decimal) or GiB/TiB (binary); throughput accepts "
            "GBps (bytes) or Gbps (bits) and defaults to MiB/s internally."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sources", help="show provenance of the bundled snapshots").set_defaults(
        func=cmd_sources
    )

    def add_selection(p: argparse.ArgumentParser) -> None:
        p.add_argument("--machine-types", help="comma-separated machine types (default: all)")
        p.add_argument("--family", help="filter machine types by family prefix, e.g. n2")
        p.add_argument("--region", default=None, help="pricing region (default: book region)")
        p.add_argument("--kinds", default=",".join(k.value for k in DEFAULT_DISK_KINDS))
        p.add_argument(
            "--allow-us-list-price",
            action="store_true",
            help="accept US list prices when no snapshot exists for the region",
        )
        p.add_argument("--json", action="store_true")

    p_options = sub.add_parser("options", help="show priced/performance-bounded options")
    p_options.add_argument("--sizes", type=_parse_sizes, default=_parse_sizes("100,500,1000"))
    add_selection(p_options)
    p_options.set_defaults(func=cmd_options)

    p_min = sub.add_parser("min-cost", help="cheapest option meeting performance/capacity targets")
    p_min.add_argument("--min-size", help="minimum total capacity, e.g. 10TB")
    p_min.add_argument("--min-read-bandwidth", help="minimum read throughput, e.g. 1.2GBps")
    p_min.add_argument("--min-write-bandwidth")
    p_min.add_argument("--min-read-iops", type=int)
    p_min.add_argument("--min-write-iops", type=int)
    p_min.add_argument("--max-monthly-cost", help="monthly disk budget in USD")
    add_selection(p_min)
    p_min.set_defaults(func=cmd_min_cost)

    p_bw = sub.add_parser("max-bandwidth", help="max throughput subject to a monthly disk budget")
    p_bw.add_argument("--budget", required=True, help="monthly disk budget in USD")
    p_bw.add_argument("--min-size", help="minimum total capacity, e.g. 10TB")
    p_bw.add_argument("--metric", choices=("read", "write", "balanced"), default="read")
    add_selection(p_bw)
    p_bw.set_defaults(func=cmd_max_bandwidth)

    p_export = sub.add_parser("export", help="export a candidate matrix for cvxopt/MILP")
    p_export.add_argument("--sizes", type=_parse_sizes, default=_parse_sizes("100,500,1000,2000"))
    p_export.add_argument("--out", required=True)
    p_export.add_argument("--format", choices=("csv", "json"), default="csv")
    add_selection(p_export)
    p_export.set_defaults(func=cmd_export)

    p_prices = sub.add_parser("refresh-prices", help="fetch regional prices from the Billing API")
    p_prices.add_argument("--region", required=True)
    p_prices.add_argument("--currency", default="USD")
    p_prices.add_argument("--api-key")
    p_prices.add_argument("--access-token")
    p_prices.add_argument("--out")
    p_prices.set_defaults(func=cmd_refresh_prices)

    p_mt = sub.add_parser(
        "refresh-machine-types", help="fetch machine shapes from the Compute Engine API"
    )
    p_mt.add_argument("--project", required=True)
    p_mt.add_argument("--access-token")
    p_mt.add_argument("--out")
    p_mt.set_defaults(func=cmd_refresh_machine_types)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except GcpOptError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
