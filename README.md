# gcp-opt

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![mypy: strict](https://img.shields.io/badge/mypy-strict-blue.svg)](https://mypy.readthedocs.io/)
[![lint: ruff](https://img.shields.io/badge/lint-ruff-261230.svg)](https://docs.astral.sh/ruff/)
[![tests: 182 passing](https://img.shields.io/badge/tests-182%20passing-brightgreen.svg)](#development)
[![license: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Grounded, typed data for a Google Cloud machine + disk configuration optimizer.**

`gcp-opt` turns Google's documentation and two official REST APIs into
optimizer-ready rows, so you can ask questions like *"the most memory with at least
10 Gbps of network"*, *"the most disk throughput for under \$2,000/month"*, or
*"the cheapest config with 16 vCPU, 10 TB and 4 GB/s"* — and get answers that are
traceable to a source.

Every axis is a first-class target you can **constrain** (`min`/`max`) or
**maximize**: vCPU, memory, network egress bandwidth, disk capacity, disk IOPS,
disk throughput, and cost.

```console
$ poetry run gcp-opt min-cost --min-size 10TB --min-read-bandwidth 4GBps
machine_type                ram    net  vcpu         disk   size GiB provIOPS       vm$     disk$      $/mo            basis
----------------------------------------------------------------------------------------------------------------------------
n2-highcpu-64                64     32    64   pd-extreme       9313    15259         -   2155.97   2155.97        disk_only
    disk: rIOPS=15259 wIOPS=15259 rMiB/s=3814.70 wMiB/s=3000.00
    disk$: capacity $1164.15 + provisioned IOPS $991.82

note: cost is DISK ONLY -- no machine prices in the snapshot, so the vm$ column is empty.
      run `gcp-opt refresh-machine-prices` to price the VM too.

$ poetry run gcp-opt search --objective max_network --min-memory 512GiB -n 2
objective: max_network
machine_type                ram    net  vcpu         disk   size GiB provIOPS       vm$     disk$      $/mo            basis
----------------------------------------------------------------------------------------------------------------------------
z4d-highmem-384-standardlssd     3024    400   384            -          -        -         -         -         -          unknown
h4d-highmem-192            1488    200   192            -          -        -         -         -         -          unknown
```

Every machine row starts with `machine_type`, `ram` (GiB) and `net` (Gbps egress
bandwidth). `provIOPS` is the provisioned-IOPS level: `pd-extreme` performance is
bought, so two rows can share a machine and a disk size yet differ in throughput
and cost (e.g. 16,000 vs 9,782 provisioned IOPS on the same `pd-extreme` volume).
`vm$`, `disk$` and `$/mo` break the cost down; `$/mo` is the **pairing total** the
ranking uses, and `basis` (`machine_and_disk` vs `disk_only`) says whether machine
prices were available. Populate them with
[`refresh-machine-prices`](#refreshing-live-data), and the budget constraint then
covers the VM *plus* its disks.


> **What it is not.** This is a *data layer*, not a solver. It ships no optimization
> algorithm — it produces the candidate matrix (cost, capacity, IOPS, throughput,
> vCPU, memory, network, ceilings) that you feed to
> [cvxopt](https://cvxopt.org/), a MILP, or a spreadsheet. Simple questions are
> answered with exact closed-form inversion; richer ones are left to your solver.

---

## Contents

- [Why](#why)
- [Install](#install)
- [Quickstart](#quickstart)
- [The axes](#the-axes)
- [CLI reference](#cli-reference)
- [Python API](#python-api)
- [Data and provenance](#data-and-provenance)
- [Verified facts (and common mistakes)](#verified-facts-and-common-mistakes)
- [Scale-out](#scale-out)
- [Refreshing live data](#refreshing-live-data)
- [Units policy](#units-policy)
- [Limitations and non-goals](#limitations-and-non-goals)
- [Development](#development)
- [License](#license)

---

## Why

Sizing a VM and its disks is an exercise in intersecting constraints: the disk
scales with capacity, the machine caps it, prices vary by region, and the numbers
you need are split across an API, a documentation table, and a static formula that
only exists in prose. Getting any of that subtly wrong — GiB vs GB, `Gbps` vs
`GBps`, a wrong baseline offset, the API field that doesn't exist — produces a
confident, wrong answer.

`gcp-opt` exists to make those facts **typed, sourced, and testable**:

- **Typed.** Frozen [pydantic](https://docs.pydantic.dev/) models for every record;
  `mypy --strict` across the codebase.
- **Exact.** All money and performance is `decimal.Decimal`. No binary floats.
- **Sourced.** Every snapshot records its method, source URL and retrieval time;
  every constant carries a source reference.
- **Verified.** Committed snapshots are SHA-256 checked on load (hand-edits fail
  loudly), and golden tests re-derive Google's own published tables.
- **Offline-first.** The package works with no credentials; live API clients fetch
  fresher data when you have them.

## Install

Requires **Python ≥ 3.11**. There is exactly one runtime dependency
(`pydantic`); HTTP uses the standard library.

```bash
git clone https://github.com/<you>/gcp-opt.git
cd gcp-opt
poetry install --with dev
```

Using pip instead:

```bash
pip install -e .
```

This installs a `gcp-opt` console script. The examples below use
`poetry run gcp-opt`; if you ran `pip install -e .` or activated the virtualenv
(`eval "$(poetry env activate)"`), drop the `poetry run` prefix and just use
`gcp-opt`.

> **`No module named gcp_opt` or `gcp-opt: command not found`?** You are running
> outside the environment that has the package. Either prefix the command with
> `poetry run`, activate the venv (`eval "$(poetry env activate)"`), or use the
> module form inside it: `poetry run python -m gcp_opt ...`.
>
> **Moved or copied the project?** Poetry keys its virtualenv to the project path,
> so a moved checkout points at a different, empty venv. Run `poetry install` again
> in the new location to recreate the environment and the `gcp-opt` script. Check
> with `poetry env info -p`.

## Quickstart

```bash
# Where did the numbers come from?
poetry run gcp-opt sources

# Machine shapes: vCPU, memory, network, disk ceilings (and $/mo when priced)
poetry run gcp-opt machines --family n2 --sort memory

# Maximize any axis, subject to any constraints
poetry run gcp-opt search --objective max_memory
poetry run gcp-opt search --objective max_network --min-memory 512GiB
poetry run gcp-opt search --objective max_vcpus --family n2
poetry run gcp-opt search --objective max_disk_read --min-size 10TB --budget 2000

# Cheapest config meeting targets: 16 vCPU, 1.1 GB/s, 10 TB
poetry run gcp-opt min-cost --min-vcpus 16 --min-size 10TB --min-read-bandwidth 1.1GBps

# Top 3 disk-throughput options under a budget (default is 5)
poetry run gcp-opt max-bandwidth --budget 3000 --min-size 10TB -n 3

# Export a candidate matrix for your own solver
poetry run gcp-opt export --family n2 --sizes 100,500,1000,2000 --out candidates.csv
poetry run gcp-opt export --machines-only --out machines.csv
```

From Python:

```python
from gcp_opt.catalog import load_catalog
from gcp_opt.models import Objective
from gcp_opt.query import Requirement, best_machine, optimize

catalog = load_catalog()

# Machine search: the most memory that also has >= 10 Gbps network
best = best_machine(
    catalog,
    catalog.machine_names(),
    objective=Objective.MAX_MEMORY,
    requirement=Requirement.build(min_network_gbps=10),
)
print(best.machine_type, best.memory_gb, best.network_egress_gbps)

# One call answers any axis; constraints span machine and disk
config = optimize(
    catalog,
    [n for n in catalog.machine_names() if n.startswith("n2-")],
    objective=Objective.MIN_COST,
    requirement=Requirement.build(
        min_vcpus=16,
        min_memory_gib="64GiB",
        min_total_size_gib="10TB",
        min_read_mibps="1.1GBps",
    ),
)
print(config.machine_type, config.size_gib, config.monthly_cost_usd, config.cost_basis)
```

`Requirement.build(...)` accepts human units (`"10TB"`, `"512GiB"`, `"1.1GBps"`,
`"10Gbps"`).

## The axes

| Axis | Field | Where it comes from |
| --- | --- | --- |
| vCPU | `guest_cpus` | Compute Engine API (docs fallback) |
| Memory | `memory_gb` | Compute Engine API (docs fallback) |
| Network egress | `network_egress_gbps`, `network_tier1_egress_gbps` | machine-family docs |
| Disk count / total size | `maximum_persistent_disks`, `maximum_total_size_gib` | Compute Engine API (docs fallback) |
| Disk capacity | `size_gib` | your choice, priced |
| Disk IOPS / throughput | `read_iops`, `read_mibps`, `write_iops`, `write_mibps` | size-scaled or provisioned |
| Cost | `monthly_cost_usd` | disk prices always; machine prices via `refresh-machine-prices` |

Objectives (`--objective` / `Objective`):

| Objective | Searches over | Notes |
| --- | --- | --- |
| `min_cost` | machines + disks | ranks by machine + disk when machine prices exist |
| `max_disk_size` | machines + disks | largest affordable capacity |
| `max_disk_read` / `max_disk_write` | machines + disks | needs `--budget` |
| `max_disk_iops` | machines + disks | needs `--budget` |
| `max_vcpus` / `max_memory` / `max_network` | machines | attaches a disk only if the requirement constrains disk axes |

Constraints (available on `search`, `min-cost`, `max-bandwidth`, `machines`):
`--min-vcpus`, `--max-vcpus`, `--min-memory`, `--max-memory`, `--min-network`,
`--max-network`, `--min-size`, `--min-read-bandwidth`, `--min-write-bandwidth`,
`--min-read-iops`, `--min-write-iops`, `--max-monthly-cost`.

A `min` constraint rejects machines whose value is unknown (it can't be proven to
hold); a `max` constraint does not.

`search`, `min-cost` and `max-bandwidth` show the **top N solutions** (`-n/--top`,
default 5). For disk objectives the ranking is a *cost/performance frontier*:
several budget points are probed and duplicate outcomes are collapsed, so you see
distinct options (e.g. more throughput for more money) rather than the same disk
listed on five machines. `-n 1` gives just the best.

## CLI reference

| Command | Purpose |
| --- | --- |
| `sources` | Provenance of every bundled snapshot |
| `machines` | List machine shapes; `--sort {name,vcpus,memory,network}` |
| `search` | Optimize any `--objective` under any constraints; `-n/--top N` |
| `min-cost` | Cheapest configs meeting targets; `-n/--top N` |
| `max-bandwidth` | Max disk throughput for a `--budget` (`--metric read\|write\|balanced`, `-n/--top N`) |
| `options` | Priced, performance-bounded disk rows for specific sizes |
| `export` | Candidate matrix to CSV/JSON (`--machines-only` for machine rows) |
| `refresh-prices` | Regional disk prices from the Cloud Billing Catalog API |
| `refresh-machine-types` | Machine shapes from the Compute Engine API |
| `refresh-machine-prices` | Price machines from the Billing API (`--from-billing`) or a file (`--from-file`) |

Common selection flags on most commands: `--machine-types`, `--family`, `--region`,
`--kinds`, `--allow-us-list-price`, `--json`.

## Python API

```python
from gcp_opt.catalog import load_catalog
from gcp_opt.models import DiskKind, Objective
from gcp_opt.query import Requirement, best_machine, min_cost_option, optimize

catalog = load_catalog()

catalog.machine_names()                       # all machine types
catalog.machine_info("n2-standard-8")         # shape + network + disk ceilings
catalog.limit_for("n2-standard-8", DiskKind.PD_SSD)   # per-VM disk ceiling
catalog.disk_option("n2-standard-8", DiskKind.PD_SSD, 1000)  # priced disk row
catalog.config_option("n2-standard-8", disk_kind=DiskKind.PD_SSD, size_gib=1000)
```

For an external solver, `gcp_opt.export.candidate_matrix(...)` returns machine
columns (vCPU, memory, network, disk ceilings) plus disk columns and cost
components. `.numeric_columns()` gives float lists (with `nan` for missing) ready
for cvxopt, or treat each row as a binary selection variable in a MILP.

## Data and provenance

The package ships hash-verified snapshots in `src/gcp_opt/data/`:

| Snapshot | Contents | Source |
| --- | --- | --- |
| `machine_type_disk_limits.json` | per-machine-type and per-vCPU disk IOPS/throughput ceilings | Persistent Disk performance docs |
| `machine_types.json` | vCPU, memory, network egress, disk-count/size ceilings | machine-family docs |
| `disk_prices.json` | disk capacity (and provisioned-IOPS) prices | disk pricing page (US list bootstrap) |
| `machine_prices.json` | optional per-machine hourly prices | your import (not bundled) |

Every file carries provenance and a `payload_sha256` that is re-verified on load:

```
machine-type-disk-limits:
  method     : google_cloud_docs_scrape
  source     : https://cloud.google.com/compute/docs/disks/performance
  retrieved  : 2026-09-24T20:05:22+00:00
```

Regenerate everything from the official docs:

```bash
poetry run python tools/refresh_snapshots.py
```

## Verified facts (and common mistakes)

These are the facts the code is built on, each verified against a primary source —
worth stating because they are widely repeated incorrectly.

**1. The `machineTypes` API does not expose disk bandwidth or IOPS.** Verified
against the public discovery document
(`https://compute.googleapis.com/$discovery/rest?version=v1`, revision `20260910`):
a `MachineType` has exactly `accelerators, architecture, bundledLocalSsds,
creationTimestamp, deprecated, description, guestCpus, id, imageSpaceGb,
isSharedCpu, kind, maximumPersistentDisks, maximumPersistentDisksSizeGb, memoryMb,
name, selfLink, zone`. There is no `capabilities` block and no throughput field.
`maximumPersistentDisksSizeGb` is a string in GiB (257 TiB → `"263168"`). Network
bandwidth is absent too, so it comes from the machine-family docs. The per-VM disk
performance wall comes from the documented machine-series tables.

**2. The disk scaling constants.** With `x` = combined GiB of all volumes of one
type on the instance:

| Disk type | IOPS | Throughput (MiB/s) |
| --- | --- | --- |
| `pd-standard` | `MIN(instance, 0.75x)` read / `MIN(instance, 1.5x)` write | `MIN(instance, 0.12x)` read / write |
| `pd-balanced` | `MIN(instance, 6x + 3000, 80000)` | `MIN(instance, 0.28x + 140, 1200)` |
| `pd-ssd` | `MIN(instance, 30x + 6000, 100000)` | `MIN(instance, 0.48x + 240, 1200)` |
| `pd-extreme` | provisioned 2,500–120,000 | `provisioned_iops × 256 KiB/s` |

The offsets are **+3,000 IOPS / +140 MiB/s** (balanced) and **+6,000 / +240** (SSD).
Standard PD's per-instance caps are asymmetric (7,500 read / 15,000 write IOPS;
1,200 read / 400 write MiB/s). These live in `src/gcp_opt/constants.py` with a
source reference, and `tests/test_performance.py` re-derives **every row of
Google's per-size tables** from them.

**3. Units are MiB/s, and one VM caps Persistent Disk throughput.** The per-VM
ceiling is **1,200 MiB/s** for Balanced/SSD and **4,000 MiB/s for Extreme**
(~4.19 GB/s). So **4 GB/s is solvable on one VM** with provisioned `pd-extreme`,
while **10 GB/s is not** — `gcp-opt` reports the hard infeasibility and the binding
ceiling, and you reach 10 GB/s by [scaling out](#scale-out). Extreme PD costs
capacity *plus* provisioned IOPS (~\$0.125/GiB-month + \$0.065/IOPS-month US list).

## Scale-out

A single VM's disk pool is capped at 1,200 MiB/s (Balanced/SSD) or 4,000 MiB/s
(Extreme). For more, replicate:

```python
from decimal import Decimal
from gcp_opt.query import min_replicas, scale_out

opt = catalog.disk_option("n2-standard-8", DiskKind.PD_SSD, 1000)
n = min_replicas(opt, min_total_size_gib=Decimal("9313.2"), min_read_mibps=Decimal("9536.7"))
fleet = scale_out(opt, n)   # sums disk cost, capacity and bandwidth
```

`AggregateOption.cost_note` records that only disk cost is summed — import machine
prices if you want the fleet total to include the VMs.

## Refreshing live data

The bundled disk price book is a **US list-price bootstrap**. Requesting another
region raises unless you opt in, so a São Paulo query can never silently use US
prices.

```bash
# Regional, undiscounted on-demand disk prices (Cloud Billing Catalog API)
poetry run gcp-opt refresh-prices --region southamerica-east1

# Authoritative machine shapes (Compute Engine API); --project is optional
poetry run gcp-opt refresh-machine-types

# Machine (instance) prices, so cost rankings cover the VM + disk pairing.
# Two ways:
#   (a) from the Cloud Billing Catalog (per-family Instance Core / Instance Ram)
poetry run gcp-opt refresh-machine-prices --from-billing --region us-central1
#   (b) from your own list: JSON mapping {"n2-standard-8": 0.5} or CSV with hourly_usd
poetry run gcp-opt refresh-machine-prices --from-file machine_prices.json --region us-central1
```

**Credentials resolve automatically.** Each live command takes `--access-token` /
`--api-key`, then falls back to `GOOGLE_OAUTH_ACCESS_TOKEN` /
`GCP_ACCESS_TOKEN` / `GCP_BILLING_API_KEY`, and finally to the **GCE metadata
server** — so on a Compute Engine instance with a service account (the
`cloud-platform` scope) no flags or tokens are needed, and `refresh-machine-types`
reads the project id from metadata too. On a workstation, use
`--access-token "$(gcloud auth print-access-token)"`.

Machine prices are what make the cost meaningful: without them the `$/mo` column is
**disk only** and every `basis` reads `disk_only`, which the CLI states explicitly.
With them, `vm$` is filled in, `basis` becomes `machine_and_disk`, and `max-bandwidth`
/ `min-cost` rank and budget on the **pairing total** — so a pricey VM pushing a
10 TB `pd-extreme` disk over budget is correctly rejected.

`--from-billing` matches per-family `Instance Core` / `Instance Ram` SKUs (the same
shape Apache libcloud's GCE price scraper uses) and reports how many families it
priced; the catalog's description layout has drifted over time, so if a family comes
back unpriced, use `--from-file`. Prices can also live outside the package: point
`GCP_OPT_MACHINE_PRICES` at a snapshot file.

## Units policy

- Capacity is **GiB**; throughput is **MiB/s**; a month is **730 hours**.
- `10TB` is decimal, `10TiB` is binary; a bare number is GiB.
- `10GBps` is gigabytes/s (**bytes**); `10Gbps` is gigabits/s (**bits**).
  `tests/test_units.py` asserts they differ by exactly 8×.

## Limitations and non-goals

- **No solver.** By design — bring cvxopt, a MILP solver, or a spreadsheet.
- **Machine prices are not bundled** (see [Refreshing live
  data](#refreshing-live-data)). Populate them and costs become VM + disk pairings;
  without them `$/mo` is disk-only, `basis` says `disk_only`, and the CLI prints a
  note so it is never mistaken for a total. `--from-billing` is best-effort against
  a catalog whose description format has changed over time; `--from-file` is the
  authoritative path.
- **Hyperdisk is not modeled.** Its performance is provisioned, not size-scaled,
  and documented separately; asking for it raises `UnmodeledDiskKindError` rather
  than inventing constants. Hyperdisk *prices* are captured.
- **Regional (replicated) PD is not modeled**; only zonal scaling rules are pinned.
  Regional prices are captured.
- **One disk kind per instance** in the query helpers. Mixing kinds under a shared
  IOPS budget is a richer problem left to your solver.
- **Network bandwidth comes from docs, not an API**, so it is `None` for families
  whose pages don't publish it (e.g. some accelerator families).
- Snapshot values were captured **2026-09-24**; the golden tests fail if Google
  changes a constant, which is the point.

## Development

```bash
poetry install --with dev
poetry run pytest          # 182 tests: unit, golden, property-based
poetry run mypy            # strict
poetry run ruff check .    # lint + import order + docstrings
```

```
gcp-opt/
├── src/gcp_opt/
│   ├── units.py          # exact decimal conversions (GB/GiB, Gbps/GBps, 730-hr month)
│   ├── models.py         # pydantic models: machine, disk, ConfigOption, Objective
│   ├── constants.py      # sourced static scaling matrix + SKU matching
│   ├── performance.py    # MIN(instance, scaling, type_cap) and its inverse
│   ├── pricing.py        # Cloud Billing Catalog v1 client
│   ├── compute.py        # Compute Engine machineTypes client
│   ├── http.py           # injectable, retrying JSON transport (stdlib)
│   ├── snapshot.py       # canonical JSON + SHA-256 integrity
│   ├── dataset.py        # load the committed snapshots
│   ├── catalog.py        # join machines + ceilings + prices -> ConfigOption
│   ├── query.py          # Requirement, best_machine(), optimize(objective=...)
│   ├── export.py         # candidate matrix -> CSV/JSON
│   ├── errors.py
│   ├── cli.py
│   └── data/             # committed, hash-verified snapshots
├── tools/
│   ├── doc_parse.py           # devsite HTML -> structured records
│   └── refresh_snapshots.py   # regenerate all snapshots
├── tests/                # unit + golden + property tests
├── LICENSE
└── pyproject.toml
```

The golden tests parse `tests/fixtures/doc_size_tables.json` (generated from the
Persistent Disk performance page) and assert the formulas reproduce every
documented row. `tests/test_doc_parse.py` exercises the scraper offline against a
committed HTML fixture, so the regeneration path is tested without network access.

Contributions that add a grounded source, tighten a validation, or extend the
golden tests are very welcome. Please keep `ruff` and `mypy --strict` clean and add
a test for any new constant.

## License

[MIT](LICENSE).
