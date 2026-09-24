"""Command-line interface: ``python -m gcp_opt <command>``."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from gcp_opt import constants, units
from gcp_opt.catalog import Catalog
from gcp_opt.compute import ComputeMachineTypeClient
from gcp_opt.dataset import Dataset
from gcp_opt.errors import GcpOptError, InfeasibleTargetError
from gcp_opt.export import candidate_matrix, machine_matrix, write_csv, write_json
from gcp_opt.models import (
    ConfigOption,
    DiskKind,
    DiskOption,
    MachinePrice,
    MachineTypeInfo,
    Objective,
    Provenance,
    SnapshotKind,
    SourceMethod,
    SourceRef,
)
from gcp_opt.pricing import BillingCatalogClient
from gcp_opt.query import (
    ALL_MODELED_DISK_KINDS,
    DEFAULT_DISK_KINDS,
    Requirement,
    machine_satisfies,
    max_throughput_option,
    min_cost_option,
    optimize,
)
from gcp_opt.snapshot import DATA_DIR, GENERATOR, build_snapshot, write_snapshot

_ALL_KINDS = tuple(kind.value for kind in DiskKind)
_MACHINE_OBJECTIVES = ("max_vcpus", "max_memory", "max_network")


def _load_catalog() -> Catalog:
    return Catalog(Dataset.load_bundled())


def _parse_kinds(
    value: str | None, default: tuple[DiskKind, ...] = DEFAULT_DISK_KINDS
) -> tuple[DiskKind, ...]:
    kinds: list[DiskKind] = []
    for token in (value or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            kinds.append(DiskKind(token))
        except ValueError as error:
            raise argparse.ArgumentTypeError(
                f"unknown disk kind {token!r}; choose from {', '.join(_ALL_KINDS)}"
            ) from error
    return tuple(kinds) or default


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


def _fmt(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "-"
    return f"{units.quantize(value, places):.{places}f}"


def _fmt_int(value: int | None) -> str:
    return "-" if value is None else str(value)


def _requirement_from_args(args: argparse.Namespace) -> Requirement:
    """Build a :class:`Requirement` from the shared CLI constraint flags."""

    def size(name: str) -> Decimal | None:
        raw = getattr(args, name, None)
        return units.parse_size_gib(str(raw)) if raw else None

    def throughput(name: str) -> Decimal | None:
        raw = getattr(args, name, None)
        return units.parse_throughput_mibps(str(raw)) if raw else None

    def number(name: str) -> Decimal | None:
        raw = getattr(args, name, None)
        return Decimal(str(raw)) if raw else None

    return Requirement.build(
        min_vcpus=getattr(args, "min_vcpus", None),
        max_vcpus=getattr(args, "max_vcpus", None),
        min_memory_gib=size("min_memory"),
        max_memory_gib=size("max_memory"),
        min_network_gbps=number("min_network"),
        max_network_gbps=number("max_network"),
        min_total_size_gib=size("min_size") or 0,
        min_read_mibps=throughput("min_read_bandwidth"),
        min_write_mibps=throughput("min_write_bandwidth"),
        min_read_iops=number("min_read_iops"),
        min_write_iops=number("min_write_iops"),
        max_monthly_cost_usd=number("max_monthly_cost"),
    )


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


def _print_configs(configs: list[ConfigOption]) -> None:
    header = (
        f"{'machine_type':22s} {'vcpu':>5s} {'memGiB':>8s} {'netGbps':>7s} "
        f"{'disk':>12s} {'size GiB':>12s} {'$/mo':>9s} {'basis':>17s}"
    )
    print(header)
    print("-" * len(header))
    for config in configs:
        print(
            f"{config.machine.name:22s} {_fmt_int(config.guest_cpus):>5s} "
            f"{_fmt(config.memory_gb, 1):>8s} {_fmt(config.network_egress_gbps, 1):>7s} "
            f"{(config.disk_kind.value if config.disk_kind else '-'):>12s} "
            f"{_fmt(config.size_gib, 0):>12s} {_fmt(config.monthly_cost_usd):>9s} "
            f"{config.cost_basis.value:>17s}"
        )
        if config.disk is not None:
            print(
                f"    disk: rIOPS={_fmt(config.read_iops, 0)} wIOPS={_fmt(config.write_iops, 0)} "
                f"rMiB/s={_fmt(config.read_mibps)} wMiB/s={_fmt(config.write_mibps)}"
            )
        if config.monthly_cost_usd is not None and config.cost_note:
            print(f"    cost: {config.cost_note}")


def _option_payload(option: DiskOption) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(option.model_dump_json()))


def _config_payload(config: ConfigOption) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(config.model_dump_json()))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_sources(_: argparse.Namespace) -> int:
    dataset = Dataset.load_bundled()
    ordered = [
        SnapshotKind.MACHINE_TYPE_LIMITS,
        SnapshotKind.MACHINE_TYPES,
        SnapshotKind.PRICES,
        SnapshotKind.MACHINE_PRICES,
    ]
    for kind in ordered:
        provenance = dataset.provenance.get(kind)
        if provenance is None:
            print(f"{kind.value}: (not bundled)")
            continue
        print(f"{kind.value}:")
        print(f"  method     : {provenance.method.value}")
        print(f"  source     : {provenance.source_url}")
        print(f"  retrieved  : {provenance.retrieved_at.isoformat()}")
        if provenance.notes:
            print(f"  notes      : {provenance.notes}")
    return 0


def cmd_machines(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    requirement = _requirement_from_args(args)
    infos: list[MachineTypeInfo] = []
    for name in _select_machines(catalog, machine_types=args.machine_types, family=args.family):
        info = catalog.machine_info(name)
        if info is None:
            continue
        ok, _ = machine_satisfies(info, requirement)
        if ok:
            infos.append(info)
    key = {
        "name": lambda i: (i.name,),
        "vcpus": lambda i: (-(i.guest_cpus or 0), i.name),
        "memory": lambda i: (-(i.memory_gb or Decimal(0)), i.name),
        "network": lambda i: (-(i.network_egress_gbps or Decimal(0)), i.name),
    }[args.sort]
    infos.sort(key=key)

    header = (
        f"{'machine_type':22s} {'family':>7s} {'vcpu':>5s} {'memGiB':>9s} "
        f"{'netGbps':>7s} {'tier1':>6s} {'maxDisks':>8s} {'maxTotGiB':>10s} {'$/mo':>9s}"
    )
    print(header)
    print("-" * len(header))
    for info in infos:
        hourly = catalog.machine_hourly_price(info.name, args.region)
        monthly = hourly * units.HOURS_PER_MONTH if hourly is not None else None
        print(
            f"{info.name:22s} {info.family or '-':>7s} {_fmt_int(info.guest_cpus):>5s} "
            f"{_fmt(info.memory_gb, 1):>9s} {_fmt(info.network_egress_gbps, 1):>7s} "
            f"{_fmt(info.network_tier1_egress_gbps, 1):>6s} "
            f"{_fmt_int(info.maximum_persistent_disks):>8s} "
            f"{_fmt(info.maximum_total_size_gib, 0):>10s} {_fmt(monthly, 2):>9s}"
        )
    print(f"\n{len(infos)} machines (sorted by {args.sort})")
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
    try:
        option = min_cost_option(
            catalog,
            machines,
            requirement=_requirement_from_args(args),
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
            metric=args.metric,
            region=args.region,
            disk_kinds=_parse_kinds(args.kinds),
            allow_us_list_price=args.allow_us_list_price,
            requirement=_requirement_from_args(args),
        )
    except InfeasibleTargetError as error:
        print(f"infeasible: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(_option_payload(option), indent=2))
    else:
        _print_options([option])
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    machines = _select_machines(catalog, machine_types=args.machine_types, family=args.family)
    objective = Objective(args.objective)
    requirement = _requirement_from_args(args)
    if args.budget:
        requirement = replace(requirement, max_monthly_cost_usd=Decimal(args.budget))
    try:
        config = optimize(
            catalog,
            machines,
            objective=objective,
            requirement=requirement,
            region=args.region,
            disk_kinds=_parse_kinds(args.kinds),
            allow_us_list_price=args.allow_us_list_price,
            budget_usd=Decimal(args.budget) if args.budget else None,
        )
    except InfeasibleTargetError as error:
        print(f"infeasible: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(_config_payload(config), indent=2))
    else:
        print(f"objective: {objective.value}")
        _print_configs([config])
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    machines = _select_machines(catalog, machine_types=args.machine_types, family=args.family)
    out = Path(args.out)
    if args.machines_only:
        infos = [catalog.machine_info(name) for name in machines]
        matrix = machine_matrix(catalog, [i for i in infos if i is not None], region=args.region)
    else:
        matrix = candidate_matrix(
            catalog,
            machines,
            args.sizes,
            region=args.region,
            disk_kinds=_parse_kinds(args.kinds),
            allow_us_list_price=args.allow_us_list_price,
        )
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

    source_url = (
        f"{constants.COMPUTE_API_BASE_URL}/projects/{args.project}/aggregated/machineTypes"
    )
    provenance = Provenance(
        method=SourceMethod.COMPUTE_MACHINE_TYPES,
        source_url=source_url,
        retrieved_at=datetime.now(UTC).replace(microsecond=0),
        generator=GENERATOR,
        notes=(
            "Live Compute Engine API machineTypes aggregatedList. Network bandwidth is "
            "not exposed by that API; keep the documented network values by merging."
        ),
    )
    path = Path(args.out) if args.out else DATA_DIR / "machine_types.json"
    write_snapshot(
        build_snapshot(infos, kind=SnapshotKind.MACHINE_TYPES, provenance=provenance), path
    )
    print(f"wrote {len(infos)} machine types to {path}")
    return 0


def _load_machine_prices(path: Path, region: str) -> list[MachinePrice]:
    """Read machine prices from JSON (list or mapping) or CSV."""
    if path.suffix.lower() == ".csv":
        rows: list[dict[str, str]] = []
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            rows = [
                {"machine_type": key, "hourly_usd": str(value)} for key, value in payload.items()
            ]
        else:
            rows = list(payload)

    source = SourceRef(url=f"file://{path}", note="User-supplied machine price list.")
    prices: list[MachinePrice] = []
    for row in rows:
        machine_type = str(row.get("machine_type", "")).strip()
        raw = row.get("hourly_usd")
        if not machine_type or raw in (None, ""):
            continue
        try:
            hourly = Decimal(str(raw))
        except InvalidOperation:
            continue
        prices.append(
            MachinePrice(
                machine_type=machine_type,
                region=str(row.get("region") or region),
                hourly_usd=hourly,
                source=source,
            )
        )
    return prices


def cmd_refresh_machine_prices(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    source_path = Path(args.from_file)
    if not source_path.exists():
        print(f"error: price file not found: {source_path}", file=sys.stderr)
        return 2
    prices = _load_machine_prices(source_path, args.region)
    if not prices:
        print("error: no usable machine prices parsed", file=sys.stderr)
        return 2
    provenance = Provenance(
        method=SourceMethod.MANUAL,
        source_url=f"file://{source_path}",
        retrieved_at=datetime.now(UTC).replace(microsecond=0),
        generator=GENERATOR,
        notes=(
            "User-supplied machine prices. To get authoritative regional prices, query "
            "the Cloud Billing Catalog for Compute 'Instance Core'/'Instance Ram' SKUs."
        ),
    )
    path = Path(args.out) if args.out else DATA_DIR / "machine_prices.json"
    write_snapshot(
        build_snapshot(prices, kind=SnapshotKind.MACHINE_PRICES, provenance=provenance), path
    )
    print(f"wrote {len(prices)} machine prices to {path}")
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def _add_constraint_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--min-vcpus", type=int, help="minimum vCPU count")
    p.add_argument("--max-vcpus", type=int, help="maximum vCPU count")
    p.add_argument("--min-memory", help="minimum memory, e.g. 256GiB")
    p.add_argument("--max-memory", help="maximum memory, e.g. 512GiB")
    p.add_argument("--min-network", help="minimum egress bandwidth in Gbps")
    p.add_argument("--max-network", help="maximum egress bandwidth in Gbps")
    p.add_argument("--min-size", help="minimum total disk capacity, e.g. 10TB")
    p.add_argument("--min-read-bandwidth", help="minimum disk read throughput, e.g. 1.2GBps")
    p.add_argument("--min-write-bandwidth", help="minimum disk write throughput")
    p.add_argument("--min-read-iops", type=int)
    p.add_argument("--min-write-iops", type=int)
    p.add_argument("--max-monthly-cost", help="monthly budget in USD")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gcp-opt",
        description=(
            "Grounded GCP machine + disk configuration data for an external optimizer. "
            "Sizes accept GB/TB (decimal) or GiB/TiB (binary); throughput accepts "
            "GBps (bytes) or Gbps (bits) and defaults to MiB/s internally."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sources", help="show provenance of the bundled snapshots").set_defaults(
        func=cmd_sources
    )

    def add_selection(
        p: argparse.ArgumentParser,
        default_kinds: tuple[DiskKind, ...] = DEFAULT_DISK_KINDS,
        *,
        constraints: bool = True,
    ) -> None:
        p.add_argument("--machine-types", help="comma-separated machine types (default: all)")
        p.add_argument("--family", help="filter machine types by family prefix, e.g. n2")
        p.add_argument("--region", default=None, help="pricing region (default: book region)")
        p.add_argument(
            "--kinds",
            default=",".join(k.value for k in default_kinds),
            help="comma-separated disk kinds (default: %(default)s)",
        )
        p.add_argument(
            "--allow-us-list-price",
            action="store_true",
            help="accept US list prices when no snapshot exists for the region",
        )
        if constraints:
            _add_constraint_flags(p)
        p.add_argument("--json", action="store_true")

    p_machines = sub.add_parser("machines", help="list machine shapes (vCPU/memory/network/disks)")
    add_selection(p_machines, constraints=True)
    p_machines.add_argument(
        "--sort", choices=("name", "vcpus", "memory", "network"), default="name"
    )
    p_machines.set_defaults(func=cmd_machines)

    p_search = sub.add_parser("search", help="optimize any objective over machines and disks")
    add_selection(p_search, ALL_MODELED_DISK_KINDS)
    p_search.add_argument(
        "--objective",
        required=True,
        choices=tuple(objective.value for objective in Objective),
        help="axis to optimize",
    )
    p_search.add_argument("--budget", help="monthly budget in USD (for max_* objectives)")
    p_search.set_defaults(func=cmd_search)

    p_options = sub.add_parser("options", help="show priced/performance-bounded disk options")
    p_options.add_argument("--sizes", type=_parse_sizes, default=_parse_sizes("100,500,1000"))
    add_selection(p_options, constraints=False)
    p_options.set_defaults(func=cmd_options)

    p_min = sub.add_parser("min-cost", help="cheapest config meeting performance/capacity targets")
    add_selection(p_min, ALL_MODELED_DISK_KINDS)
    p_min.set_defaults(func=cmd_min_cost)

    p_bw = sub.add_parser("max-bandwidth", help="max disk throughput subject to a monthly budget")
    p_bw.add_argument("--budget", required=True, help="monthly budget in USD")
    p_bw.add_argument("--metric", choices=("read", "write", "balanced"), default="read")
    add_selection(p_bw, ALL_MODELED_DISK_KINDS)
    p_bw.set_defaults(func=cmd_max_bandwidth)

    p_export = sub.add_parser("export", help="export a candidate matrix for cvxopt/MILP")
    p_export.add_argument("--sizes", type=_parse_sizes, default=_parse_sizes("100,500,1000,2000"))
    p_export.add_argument("--out", required=True)
    p_export.add_argument("--format", choices=("csv", "json"), default="csv")
    p_export.add_argument(
        "--machines-only", action="store_true", help="export machine rows without disks"
    )
    add_selection(p_export, constraints=False)
    p_export.set_defaults(func=cmd_export)

    p_prices = sub.add_parser(
        "refresh-prices", help="fetch regional disk prices from the Billing API"
    )
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

    p_mp = sub.add_parser(
        "refresh-machine-prices",
        help="import a machine-type hourly price list (JSON/CSV) so min-cost includes VMs",
    )
    p_mp.add_argument("--from-file", required=True, help="JSON list/mapping or CSV with hourly_usd")
    p_mp.add_argument("--region", default="us-central1")
    p_mp.add_argument("--out")
    p_mp.set_defaults(func=cmd_refresh_machine_prices)

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
