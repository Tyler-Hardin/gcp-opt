# gcp-opt

A typed, sourced, hash-verified data layer for a Google Cloud **Persistent Disk
cost / performance optimizer**. It fetches live prices and VM machine shapes from
two official REST APIs, pins the disk-scaling rules that Google only publishes as
documentation, and joins everything into optimizer-ready rows (cost, read/write
IOPS, read/write throughput, and *which* layer is the bottleneck).

It intentionally does **not** ship a solver. The output is a candidate matrix that
feeds cvxopt / a MILP / a spreadsheet; the two motivating questions are answered
directly with exact closed-form inversion.

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

### 1.3 Units are MiB/s, and a single VM cannot reach 10 GB/s of PD

Google documents throughput in **MiB/s**, not MB/s. `0.28 MB/s/GB` and
`0.28 MiB/s/GB` differ by ~5%. And the per-VM Persistent Disk ceiling is
**1,200 MiB/s** for Balanced/SSD (~1.26 GB/s) or **4,000 MiB/s** for Extreme — so
*"minimum 10 GB/s"* is **not achievable on one instance**. `gcp_opt` reports this
as a hard infeasibility and tells you the binding ceiling; you reach 10 GB/s by
scaling out (see §5), which also requires instance pricing that this dataset does
not include.

---

## 2. What's in the box

```
src/gcp_opt/
  units.py         exact decimal conversions; GB vs GiB, Gbps vs GBps, 730-hr months
  models.py        pydantic models (frozen, extra="forbid") + snapshot envelopes
  constants.py     the sourced static scaling matrix + SKU matching patterns
  performance.py   pure math: MIN(instance, scaling, type_cap) and its inverse
  pricing.py       Cloud Billing Catalog v1 client + SKU classification
  compute.py       Compute Engine machineTypes client
  http.py          injectable, retrying JSON transport (stdlib only)
  snapshot.py      canonical JSON + SHA-256 integrity checking
  dataset.py       load the three committed snapshots
  catalog.py       join models + machine ceilings + prices -> DiskOption rows
  query.py         min-cost / max-throughput answers, scale-out
  export.py        candidate matrix -> CSV/JSON for cvxopt/MILP
  cli.py           `python -m gcp_opt ...`
  data/*.json      committed, hash-verified snapshots
tools/
  doc_parse.py         devsite HTML -> structured records
  refresh_snapshots.py regenerate all snapshots from official sources
tests/                 100+ tests incl. golden checks against Google's tables
```

Everything at rest is `decimal.Decimal` — no binary floats touch money or
performance. Every snapshot carries provenance (method, source URL, retrieval
time) and a `payload_sha256` that is re-verified on load; a hand-edited data file
fails loudly instead of feeding wrong numbers to the optimizer.

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

# Cost + achievable performance for concrete configs
poetry run python -m gcp_opt options --machine-types n2-standard-8 --sizes 500,1000

# Cheapest config meeting targets ("min 1.1 GB/s and 10 TB")
poetry run python -m gcp_opt min-cost --min-size 10TB --min-read-bandwidth 1.1GBps --family n2

# Max bandwidth for 10 TB under $2,000/month
poetry run python -m gcp_opt max-bandwidth --budget 2000 --min-size 10TB --family n2

# Export the candidate matrix for cvxopt / a MILP
poetry run python -m gcp_opt export --family n2 --sizes 100,500,1000,2000 --out candidates.csv
```

From Python:

```python
from gcp_opt.catalog import load_catalog
from gcp_opt.models import DiskKind
from gcp_opt.query import Requirement, min_cost_option

catalog = load_catalog()

# exactly the "data to feed an optimizer": one joined row
opt = catalog.disk_option("n2-standard-8", DiskKind.PD_SSD, 1000)
print(opt.monthly_cost_usd, opt.read_mibps, opt.instance_bound)

# cheapest single-instance answer for a target
best = min_cost_option(
    catalog,
    [n for n in catalog.machine_names() if n.startswith("n2-")],
    requirement=Requirement.build(min_total_size_gib="10TB", min_read_mibps="1.1GBps"),
)
```

`export.candidate_matrix(...)` returns `(cost, size_gib, read_iops, write_iops,
read_mibps, write_mibps)` as float lists via `.numeric_columns()` — feed those
straight into cvxopt, or treat each row as a binary selection variable in a MILP.

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

`AggregateOption.cost_note` records that **only disk cost is summed** — VM instance
cost is not in this dataset, so a fleet total is a lower bound.

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
```

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

* **Hyperdisk is not modeled.** Hyperdisk performance is *provisioned* (IOPS and
  throughput are purchased separately), not size-scaled, and is documented on a
  different page. Asking for it raises `UnmodeledDiskKindError` rather than
  inventing constants. Prices for Hyperdisk SKUs are still captured.
* **Regional (replicated) PD is not modeled**; only zonal scaling rules are
  pinned. Regional *prices* are captured.
* **One disk kind per instance in the query helpers.** The performance formulas
  aggregate all volumes of one type, so splitting a size across disks of the same
  type changes neither cost nor performance; mixing types under a shared IOPS
  budget is a richer problem left to your solver.
* **VM instance cost is excluded** (only disk capacity prices are fetched).
* Golden values were captured **2026-09-24**; the tests fail if Google changes a
  constant, which is the point.

## 9. Sources

| Fact | Source |
| --- | --- |
| Per-type caps, per-size formulas, per-machine-type/per-vCPU ceilings | https://cloud.google.com/compute/docs/disks/performance |
| `MachineType` schema (and the absence of IOPS/throughput) | `https://compute.googleapis.com/$discovery/rest?version=v1` |
| Disk capacity prices | https://cloud.google.com/compute/disks-image-pricing |
| Cloud Billing Catalog v1 + service id `6F81-5844-456A` | https://cloud.google.com/billing/docs/reference/rest/v1/services.skus/list |
| Machine shapes (docs fallback) | https://cloud.google.com/compute/docs/general-purpose-machines |
