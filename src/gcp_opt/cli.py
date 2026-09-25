"""Command-line interface: ``python -m gcp_opt <command>``."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from gcp_opt import constants, units
from gcp_opt.auth import metadata_project_id, resolve_access_token
from gcp_opt.catalog import Catalog
from gcp_opt.compute import ComputeMachineTypeClient
from gcp_opt.dataset import Dataset
from gcp_opt.errors import ApiError, GcpOptError, InfeasibleTargetError
from gcp_opt.export import candidate_matrix, machine_matrix, write_csv, write_json
from gcp_opt.models import (
    ConfigOption,
    CostBasis,
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
    rank_configs,
)
from gcp_opt.snapshot import DATA_DIR, GENERATOR, build_snapshot, write_snapshot

_ALL_KINDS = tuple(kind.value for kind in DiskKind)
_MACHINE_OBJECTIVES = ("max_vcpus", "max_memory", "max_network")


def _load_catalog() -> Catalog:
    """Load the bundled snapshots, honoring ``GCP_OPT_MACHINE_PRICES`` if set."""
    override = os.environ.get("GCP_OPT_MACHINE_PRICES")
    if override:
        return Catalog(Dataset.load(machine_prices_path=Path(override)))
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


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def _add_top_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-n",
        "--top",
        type=_positive_int,
        default=5,
        metavar="N",
        help="show the top N solutions (default: %(default)s)",
    )


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


def _fmt_terse(value: Decimal | None) -> str:
    """Format a decimal without trailing zeros (``32``, ``3.75``, ``400``)."""
    return "-" if value is None else format(value.normalize(), "f")


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


def _warn_unpriced(catalog: Catalog, machines: list[str], region: str | None) -> None:
    """Warn when some candidate machines have no price and are excluded."""
    if not catalog.has_machine_prices:
        return
    unpriced = [name for name in machines if catalog.machine_hourly_price(name, region) is None]
    if unpriced:
        print(
            f"note: {len(unpriced)} of {len(machines)} selected machines have no machine "
            "price and were excluded from cost ranking "
            "(run `gcp-opt refresh-machine-prices`)",
            file=sys.stderr,
        )


def _fmt_iops(value: Decimal | None) -> str:
    """Format IOPS compactly: ``16k``, ``9.8k``, ``900``."""
    if value is None:
        return "-"
    if value >= 1000:
        thousands = value / 1000
        if thousands == thousands.to_integral_value():
            return f"{int(thousands)}k"
        return f"{thousands:.1f}k"
    return f"{value:.0f}"


def _fmt_bw(value: Decimal | None) -> str:
    """Format throughput compactly from MiB/s: ``1.1G``, ``800M``."""
    if value is None:
        return "-"
    if value >= 1000:
        return f"{value / 1024:.1f}G"
    return f"{value:.0f}M"


def _fmt_capacity(value: Decimal | None) -> str:
    """Format GiB capacity compactly: ``9.1T``, ``500``."""
    if value is None:
        return "-"
    if value >= 1024:
        return f"{value / 1024:.1f}T"
    return f"{value:.0f}"


def _fmt_pair(
    read: Decimal | None, write: Decimal | None, formatter: Callable[[Decimal | None], str]
) -> str:
    """Format a read/write pair: ``4.0G/3.0G``."""
    if read is None and write is None:
        return "-"
    return f"{formatter(read)}/{formatter(write)}"


#: Compact cost-basis labels for the table.
_BASIS_LABEL: dict[CostBasis, str] = {
    CostBasis.MACHINE_AND_DISK: "vm+disk",
    CostBasis.DISK_ONLY: "disk",
    CostBasis.MACHINE_ONLY: "vm",
    CostBasis.UNKNOWN: "?",
}


def _print_options(options: list[DiskOption], catalog: Catalog) -> None:
    name_width = max((len(option.machine_type) for option in options), default=0)
    name_width = max(name_width, len("machine_type"))
    header = (
        f"{'machine_type':<{name_width}s} {'ram':>7s} {'net':>4s} {'disk':>11s} "
        f"{'size':>5s} {'provIOPS':>8s} {'IOPS r/w':>9s} {'bw r/w':>9s} "
        f"{'$/mo':>9s} {'vm-bound':>8s}"
    )
    print(header)
    print("-" * len(header))
    for option in options:
        info = catalog.machine_info(option.machine_type)
        ram = info.memory_gb if info is not None else None
        net = info.network_egress_gbps if info is not None else None
        print(
            f"{option.machine_type:<{name_width}s} {_fmt_terse(ram):>7s} {_fmt_terse(net):>4s} "
            f"{option.disk_kind.value:>11s} {_fmt_capacity(option.size_gib):>5s} "
            f"{_fmt_iops(option.provisioned_iops):>8s} "
            f"{_fmt_pair(option.read_iops, option.write_iops, _fmt_iops):>9s} "
            f"{_fmt_pair(option.read_mibps, option.write_mibps, _fmt_bw):>9s} "
            f"{_fmt(option.monthly_cost_usd):>9s} "
            f"{'yes' if option.instance_bound else 'no':>8s}"
        )


def _print_configs(configs: list[ConfigOption]) -> None:
    name_width = max((len(config.machine.name) for config in configs), default=0)
    name_width = max(name_width, len("machine_type"))
    header = (
        f"{'machine_type':<{name_width}s} {'ram':>7s} {'net':>4s} {'vcpu':>4s} "
        f"{'disk':>11s} {'size':>5s} {'provIOPS':>8s} {'IOPS r/w':>9s} "
        f"{'bw r/w':>9s} {'vm$':>8s} {'disk$':>8s} {'$/mo':>8s} {'basis':>7s}"
    )
    print(header)
    print("-" * len(header))
    for config in configs:
        print(
            f"{config.machine.name:<{name_width}s} {_fmt_terse(config.memory_gb):>7s} "
            f"{_fmt_terse(config.network_egress_gbps):>4s} "
            f"{_fmt_int(config.guest_cpus):>4s} "
            f"{(config.disk_kind.value if config.disk_kind else '-'):>11s} "
            f"{_fmt_capacity(config.size_gib):>5s} "
            f"{_fmt_iops(config.disk.provisioned_iops if config.disk else None):>8s} "
            f"{_fmt_pair(config.read_iops, config.write_iops, _fmt_iops):>9s} "
            f"{_fmt_pair(config.read_mibps, config.write_mibps, _fmt_bw):>9s} "
            f"{_fmt(config.machine_monthly_cost_usd):>8s} "
            f"{_fmt(config.disk_monthly_cost_usd):>8s} "
            f"{_fmt(config.monthly_cost_usd):>8s} "
            f"{_BASIS_LABEL[config.cost_basis]:>7s}"
        )
    if any(config.cost_basis is CostBasis.DISK_ONLY for config in configs):
        print(
            "\nnote: no machine prices -- $/mo is disk only. "
            "Run `gcp-opt refresh-machine-prices`."
        )


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

    name_width = max((len(info.name) for info in infos), default=0)
    name_width = max(name_width, len("machine_type"))
    header = (
        f"{'machine_type':<{name_width}s} {'ram':>8s} {'net':>6s} {'family':>7s} "
        f"{'vcpu':>5s} {'tier1':>6s} {'maxDisks':>8s} {'maxTotGiB':>10s} {'$/mo':>9s}"
    )
    print(header)
    print("-" * len(header))
    for info in infos:
        hourly = catalog.machine_hourly_price(info.name, args.region)
        monthly = hourly * units.HOURS_PER_MONTH if hourly is not None else None
        print(
            f"{info.name:<{name_width}s} {_fmt_terse(info.memory_gb):>8s} "
            f"{_fmt_terse(info.network_egress_gbps):>6s} {info.family or '-':>7s} "
            f"{_fmt_int(info.guest_cpus):>5s} "
            f"{_fmt_terse(info.network_tier1_egress_gbps):>6s} "
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
        _print_options(options, catalog)
    return 0


def cmd_min_cost(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    machines = _select_machines(catalog, machine_types=args.machine_types, family=args.family)
    _warn_unpriced(catalog, machines, args.region)
    try:
        configs = rank_configs(
            catalog,
            machines,
            objective=Objective.MIN_COST,
            top=args.top,
            requirement=_requirement_from_args(args),
            region=args.region,
            disk_kinds=_parse_kinds(args.kinds),
            allow_us_list_price=args.allow_us_list_price,
        )
    except InfeasibleTargetError as error:
        print(f"infeasible: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([_config_payload(config) for config in configs], indent=2))
    else:
        _print_configs(configs)
    return 0


def cmd_max_bandwidth(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    machines = _select_machines(catalog, machine_types=args.machine_types, family=args.family)
    _warn_unpriced(catalog, machines, args.region)
    objective = {
        "read": Objective.MAX_DISK_READ,
        "write": Objective.MAX_DISK_WRITE,
        "balanced": Objective.MAX_DISK_BALANCED,
    }[args.metric]
    requirement = replace(
        _requirement_from_args(args), max_monthly_cost_usd=Decimal(args.budget)
    )
    try:
        configs = rank_configs(
            catalog,
            machines,
            objective=objective,
            top=args.top,
            requirement=requirement,
            region=args.region,
            disk_kinds=_parse_kinds(args.kinds),
            allow_us_list_price=args.allow_us_list_price,
            budget_usd=Decimal(args.budget),
        )
    except InfeasibleTargetError as error:
        print(f"infeasible: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([_config_payload(config) for config in configs], indent=2))
    else:
        _print_configs(configs)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    machines = _select_machines(catalog, machine_types=args.machine_types, family=args.family)
    _warn_unpriced(catalog, machines, args.region)
    objective = Objective(args.objective)
    requirement = _requirement_from_args(args)
    if args.budget:
        requirement = replace(requirement, max_monthly_cost_usd=Decimal(args.budget))
    try:
        configs = rank_configs(
            catalog,
            machines,
            objective=objective,
            top=args.top,
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
        print(json.dumps([_config_payload(config) for config in configs], indent=2))
    else:
        print(f"objective: {objective.value}")
        _print_configs(configs)
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
    token, source = resolve_access_token(args.access_token)
    if not api_key and not token:
        print(
            "error: no credentials found. Pass --api-key/--access-token, set "
            "GCP_BILLING_API_KEY or GOOGLE_OAUTH_ACCESS_TOKEN, or run on a GCE "
            "instance with a service account.",
            file=sys.stderr,
        )
        return 2
    if source == "gce-metadata":
        print("using GCE instance credentials (metadata server)", file=sys.stderr)
    client = BillingCatalogClient(api_key=api_key, access_token=token)
    book = client.fetch_disk_price_book(region=args.region, currency_code=args.currency)
    provenance = book.provenance
    path = Path(args.out) if args.out else DATA_DIR / "disk_prices.json"
    write_snapshot(build_snapshot(book, kind=SnapshotKind.PRICES, provenance=provenance), path)
    print(f"wrote {len(book.skus)} SKUs for {args.region} to {path}")
    return 0


def cmd_refresh_machine_types(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    token, source = resolve_access_token(args.access_token)
    if not token:
        print(
            "error: no credentials found. Pass --access-token, set "
            "GOOGLE_OAUTH_ACCESS_TOKEN, or run on a GCE instance with a service account.",
            file=sys.stderr,
        )
        return 2
    if source == "gce-metadata":
        print("using GCE instance credentials (metadata server)", file=sys.stderr)
    project = args.project or metadata_project_id()
    if not project:
        print(
            "error: provide --project (could not read it from the metadata server)",
            file=sys.stderr,
        )
        return 2
    client = ComputeMachineTypeClient(project=project, access_token=token)
    infos: list[MachineTypeInfo] = client.aggregated_list()
    if not infos:
        print("error: Compute API returned no machine types", file=sys.stderr)
        return 2
    source_url = (
        f"{constants.COMPUTE_API_BASE_URL}/projects/{project}/aggregated/machineTypes"
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

    if not args.from_billing and not args.from_file:
        print("error: provide --from-billing or --from-file", file=sys.stderr)
        return 2
    if args.from_billing and args.from_file:
        print("error: --from-billing and --from-file are mutually exclusive", file=sys.stderr)
        return 2

    path = Path(args.out) if args.out else DATA_DIR / "machine_prices.json"

    if args.from_billing:
        api_key = args.api_key or os.environ.get("GCP_BILLING_API_KEY")
        token, token_source = resolve_access_token(args.access_token)
        if not api_key and not token:
            print(
                "error: --from-billing needs credentials. Pass --api-key/--access-token, "
                "set GCP_BILLING_API_KEY or GOOGLE_OAUTH_ACCESS_TOKEN, or run on a GCE "
                "instance with a service account.",
                file=sys.stderr,
            )
            return 2
        if token_source == "gce-metadata":
            print("using GCE instance credentials (metadata server)", file=sys.stderr)
        catalog = _load_catalog()
        families = {
            info.family for info in catalog.dataset.machine_types.values() if info.family
        }
        client = BillingCatalogClient(api_key=api_key, access_token=token)
        try:
            family_prices = client.fetch_machine_family_prices(
                region=args.region, known_families=families, currency_code=args.currency
            )
        except ApiError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        by_family = {price.family: price for price in family_prices}
        source = SourceRef(
            url=(
                f"{constants.BILLING_CATALOG_BASE_URL}/services/"
                f"{constants.COMPUTE_ENGINE_SERVICE_ID}/skus"
            ),
            note="Live Cloud Billing Catalog: per-family Instance Core + Instance Ram SKUs.",
        )
        prices: list[MachinePrice] = []
        priced_families: set[str] = set()
        for info in catalog.dataset.machine_types.values():
            family_price = by_family.get(info.family or "")
            if family_price is None or info.guest_cpus is None or info.memory_gb is None:
                continue
            hourly = family_price.hourly_for(vcpus=info.guest_cpus, memory_gib=info.memory_gb)
            if hourly is None:
                continue
            prices.append(
                MachinePrice(
                    machine_type=info.name,
                    region=args.region,
                    hourly_usd=hourly,
                    source=source,
                )
            )
            priced_families.add(family_price.family)
        if not prices:
            print(
                "error: matched core/RAM SKUs but could not price any machine type "
                "(missing vCPU/memory?); try --from-file",
                file=sys.stderr,
            )
            return 2
        provenance = Provenance(
            method=SourceMethod.CLOUD_BILLING_CATALOG,
            source_url=source.url,
            retrieved_at=datetime.now(UTC).replace(microsecond=0),
            generator=GENERATOR,
            notes=(
                f"Live Cloud Billing Catalog core/RAM prices for {len(priced_families)} "
                f"families in {args.region}. Shared-core machines are approximated by "
                "vCPU x core + GiB x ram."
            ),
        )
        write_snapshot(
            build_snapshot(prices, kind=SnapshotKind.MACHINE_PRICES, provenance=provenance), path
        )
        print(
            f"wrote {len(prices)} machine prices across {len(priced_families)} families "
            f"for {args.region} to {path}"
        )
        return 0

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
            "User-supplied machine prices. To get regional prices directly, use "
            "`refresh-machine-prices --from-billing`."
        ),
    )
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
    _add_top_flag(p_search)
    p_search.set_defaults(func=cmd_search)

    p_options = sub.add_parser("options", help="show priced/performance-bounded disk options")
    p_options.add_argument("--sizes", type=_parse_sizes, default=_parse_sizes("100,500,1000"))
    add_selection(p_options, constraints=False)
    p_options.set_defaults(func=cmd_options)

    p_min = sub.add_parser("min-cost", help="cheapest config meeting performance/capacity targets")
    _add_top_flag(p_min)
    add_selection(p_min, ALL_MODELED_DISK_KINDS)
    p_min.set_defaults(func=cmd_min_cost)

    p_bw = sub.add_parser("max-bandwidth", help="max disk throughput subject to a monthly budget")
    p_bw.add_argument("--budget", required=True, help="monthly budget in USD")
    p_bw.add_argument("--metric", choices=("read", "write", "balanced"), default="read")
    _add_top_flag(p_bw)
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
    p_mt.add_argument("--project", help="project id (default: instance metadata)")
    p_mt.add_argument("--access-token")
    p_mt.add_argument("--out")
    p_mt.set_defaults(func=cmd_refresh_machine_types)

    p_mp = sub.add_parser(
        "refresh-machine-prices",
        help="price machines so cost rankings include the VM (Billing API or a file)",
    )
    p_mp.add_argument(
        "--from-billing",
        action="store_true",
        help="fetch per-family Instance Core/RAM prices from the Cloud Billing Catalog",
    )
    p_mp.add_argument(
        "--from-file",
        help="import a JSON list/mapping or CSV with hourly_usd instead",
    )
    p_mp.add_argument("--region", default="us-central1")
    p_mp.add_argument("--currency", default="USD")
    p_mp.add_argument("--api-key")
    p_mp.add_argument("--access-token")
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
