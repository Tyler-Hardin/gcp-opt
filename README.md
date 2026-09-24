# gcp-opt

A typed, sourced, hash-verified data layer for a Google Cloud **machine + disk
configuration optimizer**. Every axis is a first-class target you can constrain or
maximize: **vCPU, memory, network egress bandwidth, disk capacity, disk IOPS, disk
throughput, and cost**. It fetches live prices and VM machine shapes from two
official REST APIs, pins the facts Google only publishes as documentation (disk
scaling rules and per-machine network bandwidth), and joins everything into
optimizer-ready rows.

It intentionally does **not** ship a solver. The output is a candidate matrix that
feeds cvxopt / a MILP / a spreadsheet, and common questions ("max memory", "max
network", "cheapest config with ≥10 TB and ≥4 GB/s") are answered directly with
exact closed-form inversion.

```bash
python -m gcp_opt search --objective max_memory
python -m gcp_opt search --objective max_network --min-memory 512GiB
python -m gcp_opt search --objective min_cost --min-vcpus 16 --min-size 10TB --min-read-bandwidth 4GBps
```

---

## 1. Corrections to the commonly-pasted advice

The prompt that started this repo contained three factual errors worth pinning
down, because each one silently produces a wrong optimizer.

### 1.1 The `machineTypes` API does **not** expose disk bandwidth or IOPS

* The endpoint in the advice (`GET https://googleapis.com{project}/zones/{zone}/machineTypes/{machineType}`)
  is not a real URL. The correct one is
  `GET https://compute.googleapis.com/compute/v1/projects/{project}/zones/{zone}/machineTypes/{machineType}`.
* There is no `capabilities` block. Verified against the public discovery document
  (`https://compute.googleapis.com/$discovery/rest?version=v1`, revision `20260910`):
  a `MachineType` has exactly `accelerators, architecture, bundledLocalSsds,
  creationTimestamp, deprecated, description, guestCpus, id, imageSpaceGb,
  isSharedCpu, kind, maximumPersistentDisks, maximumPersistentDisksSizeGb,
  memoryMb, name, selfLink, zone`. **No IOPS, no throughput, no `capabilities`.**
* `maximumPersistentDisksSizeGb` is returned as a *string* and is effectively
  **GiB** (257 TiB is returned as `"263168"`), so `gcp_opt` normalizes it to GiB.
* Consequence: the Compute API supplies the machine *shape* and the disk-count/size
  ceilings. The per-VM disk **performance wall** comes from the documented
  "Persistent Disk performance limits by machine series" tables, which
  `tools/refresh_snapshots.py` scrapes into a committed snapshot.

### 1.2 The "static constants" in that JSON were wrong

The pasted snippet claimed `baseline_iops: 1200` for `pd-balanced` and `3000` for
`pd-ssd`. Google's published formulas are (x = combined GiB of all volumes of that
type on the instance):

| Disk type | IOPS | Throughput (MiB/s) |
| --- | --- | --- |
| `pd-standard` | `MIN(instance, 0.75x)` read / `MIN(instance, 1.5x)` write | `MIN(instance, 0.12x)` read / write |
| `pd-balanced` | `MIN(instance, 6x + 3000, 80000)` | `MIN(instance, 0.28x + 140, 1200)` |
| `pd-ssd` | `MIN(instance, 30x + 6000, 100000)` | `MIN(instance, 0.48x + 240, 1200)` |
| `pd-extreme` | provisioned 2,500–120,000 | `provisioned_iops × 256 KiB/s` |

The offsets are **+3,000 IOPS / +140 MiB/s** (balanced) and **+6,000 / +240**
(SSD) — not 1,200 / 3,000. Standard PD's per-instance caps are asymmetric
(7,500 read / 15,000 write IOPS; 1,200 read / 400 write MiB/s). These live in
`src/gcp_opt/constants.py` with a source reference, and
`tests/test_performance.py` re-derives **every row of Google's per-size tables**
from them.

### 1.3 Units are MiB/s, and a single VM caps Persistent Disk throughput

Google documents throughput in **MiB/s**, not MB/s. `0.28 MB/s/GB` and
`0.28 MiB/s/GB` differ by ~5%. The per-VM Persistent Disk ceiling is
**1,200 MiB/s** for Balanced/SSD (~1.26 GB/s) — but **4,000 MiB/s for Extreme PD**
(~4.19 GB/s). So:

* **4 GB/s is solvable on one VM** with `pd-extreme` (which requires provisioned
  IOPS). `gcp_opt` finds it: `min-cost --min-size 10TB --min-read-bandwidth 4GBps`
  returns an `n2-*-64` + `pd-extreme` configuration.
* **10 GB/s is not achievable on one instance.** `gcp_opt` reports the hard
  infeasibility and the binding 4,000 MiB/s ceiling; you reach 10 GB/s by scaling
  out (see §5), which also needs instance pricing that this dataset does not
  include.

Extreme PD is **provisioned**, so its cost is capacity *plus* provisioned IOPS
(`$0.125/GiB-month + $0.065/IOPS-month` in the US list price), and its throughput
is `provisioned_iops × 256 KiB/s`.

---

## 2. What's in the box

```
src/gcp_opt/
  units.py         exact decimal conversions; GB vs GiB, Gbps vs GBps, 730-hr months
  models.py        pydantic models: machine shape, disk model, ConfigOption, Objective
  constants.py     the sourced static scaling matrix + SKU matching patterns
  performance.py   pure math: MIN(instance, scaling, type_cap) and its inverse
  pricing.py       Cloud Billing Catalog v1 client + SKU classification
  compute.py       Compute Engine machineTypes client
  http.py          injectable, retrying JSON transport (stdlib only)
  snapshot.py      canonical JSON + SHA-256 integrity checking
  dataset.py       load the committed snapshots (machine prices optional)
  catalog.py       join machines + disk ceilings + prices -> ConfigOption rows
  query.py         Requirement (all axes), best_machine(), optimize(objective=...)
  export.py        machine+disk candidate matrix -> CSV/JSON for cvxopt/MILP
  cli.py           `python -m gcp_opt ...`
  data/*.json      committed, hash-verified snapshots
tools/
  doc_parse.py         devsite HTML -> structured records (disks, bandwidth, prices)
  refresh_snapshots.py regenerate all snapshots from official sources
tests/                 140+ tests incl. golden checks against Google's tables
```

Everything at rest is `decimal.Decimal` — no binary floats touch money or
performance. Every snapshot carries provenance (method, source URL, retrieval
time) and a `payload_sha256` that is re-verified on load; a hand-edited data file
fails loudly instead of feeding wrong numbers to the optimizer.

### The axes

| Axis | Field | Source |
| --- | --- | --- |
| vCPU | `guest_cpus` | Compute API (docs fallback) |
| Memory | `memory_gb` | Compute API (docs fallback) |
| Network egress | `network_egress_gbps` / `network_tier1_egress_gbps` | machine family docs |
| Disk count / total size | `maximum_persistent_disks` / `maximum_total_size_gib` | Compute API (docs fallback) |
| Disk capacity | `size_gib` | your choice, priced |
| Disk IOPS / throughput | `read_iops` / `read_mibps` | size-scaled or provisioned |
| Cost | `monthly_cost_usd` | disk prices always; machine prices optional |

Any axis can be constrained with `min_*`/`max_*` in a `Requirement`, and any axis
can be the objective passed to `optimize()`. Machine objectives
(`max_vcpus`, `max_memory`, `max_network`) search machines; disk objectives and
`min_cost` search machines **and** disks. **Network bandwidth is not exposed by the
Compute API**, so it comes from the machine-family docs snapshot.

---

## 3. Install and verify

Requires Python ≥ 3.11 and [Poetry](https://python-poetry.org/).

```bash
poetry install --with dev
poetry run pytest                 # unit + golden + property tests
poetry run mypy                   # strict
poetry run ruff check .
```

## 4. Use the bundled data

```bash
# Where did the numbers come from?
poetry run python -m gcp_opt sources

# Machine shapes: vCPU, memory, network, disk ceilings (and $/mo when priced)
poetry run python -m gcp_opt machines --family n2 --sort memory

# Optimize any axis, subject to any constraints
poetry run python -m gcp_opt search --objective max_memory
poetry run python -m gcp_opt search --objective max_network --min-memory 512GiB
poetry run python -m gcp_opt search --objective max_vcpus --family n2
poetry run python -m gcp_opt search --objective max_disk_read --min-size 10TB --budget 2000

# Cheapest config meeting targets ("min 16 vCPU, 1.1 GB/s and 10 TB")
poetry run python -m gcp_opt min-cost --min-vcpus 16 --min-size 10TB --min-read-bandwidth 1.1GBps

# 4 GB/s needs provisioned Extreme PD (min-cost considers it by default)
poetry run python -m gcp_opt min-cost --min-size 10TB --min-read-bandwidth 4GBps

# Export the machine+disk candidate matrix for cvxopt / a MILP
poetry run python -m gcp_opt export --family n2 --sizes 100,500,1000,2000 --out candidates.csv
poetry run python -m gcp_opt export --machines-only --out machines.csv
```

From Python:

```python
from gcp_opt.catalog import load_catalog
from gcp_opt.models import Objective
from gcp_opt.query import Requirement, best_machine, optimize

catalog = load_catalog()

# Machine search: biggest memory that also has >= 10 Gbps network
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
        min_vcpus=16, min_memory_gib="64GiB", min_total_size_gib="10TB", min_read_mibps="1.1GBps"
    ),
)
print(config.machine_type, config.size_gib, config.monthly_cost_usd, config.cost_basis)
```

`Requirement.build` accepts human units (`"10TB"`, `"512GiB"`, `"1.1GBps"`,
`"10Gbps"`). `export.candidate_matrix(...)` returns machine columns (vCPU, memory,
network, disk ceilings) plus disk columns and cost components; `.numeric_columns()`
gives float lists (with `nan` for missing) ready for cvxopt, or treat each row as a
binary selection variable in a MILP.

## 5. Reaching multi-GB/s: scale-out

A single VM's PD pool is capped at 1,200 MiB/s (Balanced/SSD) or 4,000 MiB/s
(Extreme). For aggregate bandwidth, replicate:

```python
from decimal import Decimal
from gcp_opt.query import min_replicas, scale_out

opt = catalog.disk_option("n2-standard-8", DiskKind.PD_SSD, 1000)
n = min_replicas(opt, min_total_size_gib=Decimal("9313.2"), min_read_mibps=Decimal("9536.7"))
fleet = scale_out(opt, n)   # sums disk cost/capacity/bandwidth
```

`AggregateOption.cost_note` records that **only disk cost is summed** — import
machine prices if you want the fleet total to include the VMs.

## 6. Live data (regional prices, machine shapes)

The bundled price book is a **US list-price bootstrap** scraped from the pricing
page. Requesting another region raises unless you opt in, so a São Paulo query
cannot silently use US prices.

```bash
# Regional, undiscounted on-demand prices from the Cloud Billing Catalog API
export GCP_BILLING_API_KEY=...        # or GOOGLE_OAUTH_ACCESS_TOKEN=...
poetry run python -m gcp_opt refresh-prices --region southamerica-east1

# Authoritative machine shapes (vCPU/memory/disk-count ceilings) from the Compute API
export GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)"
poetry run python -m gcp_opt refresh-machine-types --project my-project

# Machine (instance) prices so min-cost includes the VM, not just the disk.
# Google's VM pricing page is client-side rendered, so import a price list
# (JSON mapping or CSV) that you obtained from the Cloud Billing Catalog
# "Instance Core"/"Instance Ram" SKUs for your region:
poetry run python -m gcp_opt refresh-machine-prices \
    --from-file machine_prices.json --region us-central1
```

When `machine_prices.json` is absent, cost figures are reported as
`cost_basis: "disk_only"` and the CLI prints an explicit note — a São Paulo query
never silently inherits a US machine price.

The Billing client paginates `services/6F81-5844-456A/skus` (the Compute Engine
service id, cross-checked against Apache libcloud's GCE scraper), filters
`category.resourceFamily == "Storage"` and `serviceRegions`, and preserves tiered
rates (Standard PD's first 30 GiB/month are free).

Regenerate all committed snapshots from the official docs:

```bash
poetry run python tools/refresh_snapshots.py
```

## 7. Units policy

* Capacity is **GiB**; throughput is **MiB/s**; months are **730 hours**.
* `--min-size 10TB` = decimal terabytes; `10TiB` = binary. A bare number is GiB.
* `10GBps` = gigabytes/s (**bytes**); `10Gbps` = gigabits/s (**bits**).
  `tests/test_units.py` asserts the two differ by exactly 8×.
* `mypy --strict` and `ruff` run over `src`, `tests` and `tools`.

## 8. Deliberate limitations

* **Machine prices are an optional layer, not bundled.** Google's VM pricing page
  is rendered client-side, so the bootstrap ships none. Import a price list with
  `refresh-machine-prices`; until then `ConfigOption.cost_basis` is `disk_only`
  and `optimize(MIN_COST)` minimizes disk cost, tie-breaking toward the smallest
  machine that satisfies the constraints. Machine objectives (max memory/cpu/net)
  are unaffected.
* **Network bandwidth comes from the docs, not the API.** The Compute Engine
  `MachineType` resource has no bandwidth field, so `network_egress_gbps` is
  scraped from the machine-family docs (`None` for families whose pages do not
  publish it, e.g. some accelerator families). A `min` network constraint rejects
  machines with unknown bandwidth; a `max` constraint does not.
* **Hyperdisk is not modeled.** Hyperdisk performance is *provisioned* (IOPS and
  throughput are purchased separately) and is documented on a different page.
  Asking for it raises `UnmodeledDiskKindError` rather than inventing constants.
  Hyperdisk prices (capacity, provisioned IOPS/throughput) *are* captured.
* **`pd-extreme` is modeled** (provisioned IOPS, throughput = IOPS × 256 KiB/s,
  capacity + IOPS priced) and included in disk searches by default.
* **Regional (replicated) PD is not modeled**; only zonal scaling rules are
  pinned. Regional *prices* are captured.
* **One disk kind per instance in the query helpers.** The performance formulas
  aggregate all volumes of one type, so splitting a size across disks of the same
  type changes neither cost nor performance; mixing types under a shared IOPS
  budget is a richer problem left to your solver.
* Golden values were captured **2026-09-24**; the tests fail if Google changes a
  constant, which is the point.

## 9. Sources

| Fact | Source |
| --- | --- |
| Per-type caps, per-size formulas, per-machine-type/per-vCPU ceilings | https://cloud.google.com/compute/docs/disks/performance |
| `MachineType` schema (no IOPS/throughput/bandwidth fields) | `https://compute.googleapis.com/$discovery/rest?version=v1` |
| Disk capacity prices | https://cloud.google.com/compute/disks-image-pricing |
| Machine vCPU/memory, disk-count/size ceilings, network egress bandwidth | https://cloud.google.com/compute/docs/general-purpose-machines (and the other family pages) |
| Cloud Billing Catalog v1 + service id `6F81-5844-456A` | https://cloud.google.com/billing/docs/reference/rest/v1/services.skus/list |
