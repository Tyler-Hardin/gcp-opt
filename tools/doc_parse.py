"""Parse Google Cloud documentation tables into structured records.

Dev-only helper (not imported at runtime).  It powers ``tools/refresh_snapshots.py``
and is exercised against a committed HTML fixture in the test suite so a docs
markup change fails loudly instead of silently producing an empty dataset.

The docs are rendered by devsite with regular ``<h2>/<h3>/<table>`` markup, so a
small stdlib :class:`~html.parser.HTMLParser` that records headings and tables in
document order is both simpler and more robust than regex-scraping raw HTML.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_HEADING_LEVEL = {tag: int(tag[1]) for tag in _HEADING_TAGS}

#: Machine type names look like ``n2-standard-8`` or ``a3-megagpu-8g``.
_MACHINE_TYPE_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)+$")
_DIGITS_RE = re.compile(r"-?[0-9][0-9,]*(?:\.[0-9]+)?")


@dataclass
class Block:
    """A heading or a table, in document order."""

    kind: str  # "heading" | "table"
    level: int = 0
    heading_id: str | None = None
    text: str = ""
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


class _DocParser(HTMLParser):
    """Collect headings and tables from devsite HTML in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._heading_tag: str | None = None
        self._heading_id: str | None = None
        self._buf: list[str] = []
        self._table: Block | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._cell_is_header = False
        self._skip_depth = 0
        # Footnote markers are rendered as <sup>1</sup>; their text must not be
        # concatenated onto the number ("1,200<sup>1</sup>" -> 1200, not 12001).
        self._sup_depth = 0

    # -- helpers ---------------------------------------------------------
    def _flush_heading(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if text:
            self.blocks.append(
                Block(
                    kind="heading",
                    level=_HEADING_LEVEL.get(self._heading_tag or "h6", 6),
                    heading_id=self._heading_id,
                    text=text,
                )
            )
        self._heading_tag = None
        self._heading_id = None
        self._buf = []

    # -- HTMLParser hooks ------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "nav", "header", "footer"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "sup":
            self._sup_depth += 1
            return
        if tag in _HEADING_TAGS:
            self._heading_tag = tag
            self._heading_id = dict(attrs).get("id")
            self._buf = []
        elif tag == "table":
            self._table = Block(kind="table")
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
            self._cell_is_header = tag == "th"
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "header", "footer"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "sup":
            self._sup_depth = max(0, self._sup_depth - 1)
            return
        if tag in _HEADING_TAGS and self._heading_tag == tag:
            self._flush_heading()
        elif tag in {"td", "th"} and self._cell is not None and self._row is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(cell for cell in self._row):
                self._table.rows.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            rows = self._table.rows
            if rows:
                # devsite uses <th> for the header row; fall back to row 0.
                header_row, body = rows[0], rows[1:]
                self._table.headers = header_row
                self._table.rows = body
                self.blocks.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._sup_depth:
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._heading_tag is not None:
            self._buf.append(data)


def parse_blocks(html: str) -> list[Block]:
    """Return headings and tables in document order."""
    parser = _DocParser()
    parser.feed(html)
    parser.close()
    return parser.blocks


def parse_number(text: str) -> Decimal | None:
    """Extract the first number from a docs cell (``"15,000"`` -> ``Decimal(15000)``)."""
    match = _DIGITS_RE.search(text.replace("\u00a0", " "))
    if match is None:
        return None
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:  # pragma: no cover - regex guarantees validity
        return None


def normalize_header(text: str) -> str:
    """Lowercase and strip footnote markers/superscripts from a header cell."""
    text = re.sub(r"\s*\d+\s*$", "", text.strip())
    return re.sub(r"\s+", " ", text).lower()


def is_machine_type(name: str) -> bool:
    """True if a cell looks like a GCP machine type name."""
    return bool(_MACHINE_TYPE_RE.match(name.strip())) and any(ch.isdigit() for ch in name)


def family_of(machine_type: str) -> str:
    """Derive a machine family from a machine type name (``n2-standard-8`` -> ``n2``)."""
    return machine_type.split("-", 1)[0]


# --------------------------------------------------------------------------- #
# Machine-series disk limits (the "VM bottleneck" tables)
# --------------------------------------------------------------------------- #

_DISK_KIND_IDS = {
    "pd-standard": "pd-standard",
    "pd-balanced": "pd-balanced",
    "pd-ssd": "pd-ssd",
    "pd-extreme": "pd-extreme",
}

# devsite de-duplicates repeated heading ids by appending ``_1``, ``_2``, ...
_HEADING_ID_SUFFIX_RE = re.compile(r"_\d+$")


def normalize_heading_id(heading_id: str | None) -> str | None:
    """Strip devsite's numeric de-duplication suffix from a heading id."""
    if heading_id is None:
        return None
    return _HEADING_ID_SUFFIX_RE.sub("", heading_id)

_LIMIT_HEADERS = {
    "maximum write iops": "max_write_iops",
    "maximum read iops": "max_read_iops",
    "maximum write throughput (mib/s)": "max_write_mibps",
    "maximum read throughput (mib/s)": "max_read_mibps",
}


#: Docs append footnote digits directly to some names/counts (``e2-medium1``).
_FOOTNOTE_TAIL_RE = re.compile(r"(?<=[a-z])\d+$")
_VCPU_RANGE_RE = re.compile(r"^(\d+)\s*(?:-|to)\s*(\d+)$", re.IGNORECASE)
_VCPU_MIN_RE = re.compile(r"^(\d+)\s+or more$", re.IGNORECASE)
_VCPU_EXACT_RE = re.compile(r"^(\d+)$")


def strip_footnote_marker(text: str) -> str:
    """Remove a trailing footnote digit glued to a token (``e2-medium1``)."""
    return _FOOTNOTE_TAIL_RE.sub("", text.strip())


def parse_vcpu_label(label: str) -> tuple[int, int | None] | None:
    """Parse a vCPU row label into ``(min, max)``; ``None`` if not a count.

    Handles ``"4"``, ``"2-7"``, ``"8 to 14"`` and ``"64 or more"`` (unbounded).
    """
    text = re.sub(r"\s+", " ", label.replace("\u00a0", " ")).strip()
    if (match := _VCPU_RANGE_RE.match(text)) is not None:
        low, high = int(match.group(1)), int(match.group(2))
        return (low, high) if low <= high else (high, low)
    if (match := _VCPU_MIN_RE.match(text)) is not None:
        return int(match.group(1)), None
    if (match := _VCPU_EXACT_RE.match(text)) is not None:
        return int(match.group(1)), int(match.group(1))
    return None


def family_id_of(family_label: str) -> str:
    """Derive the machine-family id from a docs heading (``"N2 Instances"`` -> ``"n2"``)."""
    return family_label.strip().split()[0].lower()


def _limits_from(mapping: dict[str, int], row: list[str]) -> dict[str, object] | None:
    """Extract the four limit fields from a table row, or ``None`` if any is missing."""
    values: dict[str, object] = {}
    for field_name, index in mapping.items():
        if index >= len(row):
            return None
        number = parse_number(row[index])
        if number is None:
            return None
        values[field_name] = number
    return values


def parse_machine_series_limits(
    html: str, *, section_id: str = "machine-type-disk-limits"
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Parse the machine-series performance tables from the docs page.

    Returns ``(by_machine_type, by_vcpu)``.  Families such as A2/A3 publish one row
    per machine type; C2/C3/N1/N2/N2D/E2/T2A/T2D/Z3 publish one row per vCPU count
    or range.  Callers resolve a concrete machine type by trying the machine-type
    table first, then the family's vCPU range.
    """
    blocks = parse_blocks(html)
    start = next(
        (
            index
            for index, block in enumerate(blocks)
            if block.kind == "heading" and block.heading_id == section_id
        ),
        None,
    )
    if start is None:
        raise ValueError(f"section id {section_id!r} not found; docs markup changed?")

    by_machine_type: list[dict[str, object]] = []
    by_vcpu: list[dict[str, object]] = []
    family: str | None = None
    disk_kind: str | None = None
    for block in blocks[start + 1 :]:
        if block.kind == "heading":
            if block.level <= 2:
                break  # next top-level section
            kind_id = normalize_heading_id(block.heading_id)
            if block.level == 3:
                if kind_id in _DISK_KIND_IDS:
                    disk_kind = _DISK_KIND_IDS[kind_id]
                elif kind_id == "example":
                    family, disk_kind = None, None
                else:
                    family = block.text
                    disk_kind = None
            elif kind_id in _DISK_KIND_IDS:
                disk_kind = _DISK_KIND_IDS[kind_id]
            continue

        if block.kind != "table" or family is None or disk_kind is None or not block.headers:
            continue
        headers = [normalize_header(cell) for cell in block.headers]
        mapping = {
            _LIMIT_HEADERS[h]: index for index, h in enumerate(headers) if h in _LIMIT_HEADERS
        }
        if len(mapping) < 4:
            continue

        common: dict[str, object] = {
            "family": family,
            "family_id": family_id_of(family),
            "disk_kind": disk_kind,
        }
        key_header = headers[0]
        for row in block.rows:
            if not row:
                continue
            values = _limits_from(mapping, row)
            if values is None:
                continue
            if key_header.startswith("machine type"):
                name = strip_footnote_marker(row[0])
                if is_machine_type(name):
                    by_machine_type.append({"machine_type": name, **common, **values})
            elif key_header.startswith("number of vcpus") or key_header.startswith("vcpus"):
                vcpu_range = parse_vcpu_label(row[0])
                if vcpu_range is not None:
                    by_vcpu.append(
                        {
                            "vcpu_label": re.sub(r"\s+", " ", row[0]).strip(),
                            "vcpus_min": vcpu_range[0],
                            "vcpus_max": vcpu_range[1],
                            **common,
                            **values,
                        }
                    )
                else:
                    name = strip_footnote_marker(row[0])
                    if is_machine_type(name):
                        by_machine_type.append({"machine_type": name, **common, **values})
    return by_machine_type, by_vcpu


# --------------------------------------------------------------------------- #
# "Performance limits by size" tables (golden fixtures)
# --------------------------------------------------------------------------- #

_SIZE_TABLE_HEADING_RE = re.compile(
    r"^(?P<scope>zonal|regional)\s+(?P<kind>ssd|balanced|standard|extreme)\s+persistent disk$",
    re.IGNORECASE,
)


def parse_size_tables(html: str) -> list[dict[str, object]]:
    """Parse the documented per-size performance tables (used as test fixtures).

    The tables are preceded by headings such as ``"Zonal SSD Persistent Disk"``
    (scope + kind) under a ``"... performance limits by size"`` parent heading.
    """
    tables: list[dict[str, object]] = []
    current: dict[str, str] | None = None
    for block in parse_blocks(html):
        if block.kind == "heading":
            match = _SIZE_TABLE_HEADING_RE.match(block.text.strip())
            current = (
                {"scope": match.group("scope").lower(), "kind": match.group("kind").lower()}
                if match
                else None
            )
        elif block.kind == "table" and current is not None:
            tables.append(
                {
                    "scope": current["scope"],
                    "kind": current["kind"],
                    "heading": block.text,
                    "headers": block.headers,
                    "rows": block.rows,
                }
            )
            current = None
    return tables


# --------------------------------------------------------------------------- #
# Machine shapes (vCPUs / memory / disk-count ceilings)
# --------------------------------------------------------------------------- #

_SPEC_COLUMNS = {
    "vCPUs": "guest_cpus",
    "Memory (GB)": "memory_gb",
}


def _find_column(headers: list[str], needle: str) -> int | None:
    for index, header in enumerate(headers):
        if normalize_header(header).startswith(needle.lower()):
            return index
    return None


def parse_machine_specs(html: str) -> list[dict[str, object]]:
    """Parse vCPU/memory and disk-count/size ceilings from a machine-family page.

    Merges several table shapes (compute families publish vCPU+disk ceilings in
    separate tables) keyed by machine type name.
    """
    merged: dict[str, dict[str, object]] = {}
    for block in parse_blocks(html):
        if block.kind != "table" or not block.headers:
            continue
        name_col = _find_column(block.headers, "Machine type")
        if name_col is None:
            continue

        vcpu_col = _find_column(block.headers, "vCPUs")
        mem_col = _find_column(block.headers, "Memory (GB)")
        disk_count_col = None
        for candidate in (
            "Max number of Persistent Disk",
            "Max number of disks per VM",
            "Max number of Persistent Disk (PDs)",
            "Max number of disks",
        ):
            disk_count_col = _find_column(block.headers, candidate)
            if disk_count_col is not None:
                break
        total_size_col = _find_column(block.headers, "Max total disk size (TiB)")
        if total_size_col is None:
            total_size_col = _find_column(block.headers, "Max total Persistent Disk size (TiB)")
        if total_size_col is None:
            total_size_col = _find_column(block.headers, "Max total PD size (TiB)")

        # Network egress bandwidth is documented per machine family (the Compute
        # API's MachineType does not expose it).  Families word this as either
        # "Default" or "Maximum"; both feed the same field.
        egress_col = _find_column(block.headers, "Default egress bandwidth (Gbps)")
        if egress_col is None:
            egress_col = _find_column(block.headers, "Maximum egress bandwidth (Gbps)")
        tier1_col = _find_column(block.headers, "Tier_1 egress bandwidth (Gbps)")

        if (
            vcpu_col is None
            and disk_count_col is None
            and total_size_col is None
            and egress_col is None
        ):
            continue

        for row in block.rows:
            if name_col >= len(row) or not is_machine_type(row[name_col]):
                continue
            name = row[name_col].strip()
            record = merged.setdefault(name, {"name": name, "family": family_of(name)})
            if vcpu_col is not None and vcpu_col < len(row):
                value = parse_number(row[vcpu_col])
                if value is not None:
                    record["guest_cpus"] = int(value)
            if mem_col is not None and mem_col < len(row):
                value = parse_number(row[mem_col])
                if value is not None:
                    record["memory_gb"] = value
            if disk_count_col is not None and disk_count_col < len(row):
                value = parse_number(row[disk_count_col])
                if value is not None:
                    record["maximum_persistent_disks"] = int(value)
            if total_size_col is not None and total_size_col < len(row):
                value = parse_number(row[total_size_col])
                if value is not None:
                    record["maximum_total_size_tib"] = value
            if egress_col is not None and egress_col < len(row):
                value = parse_number(row[egress_col])
                if value is not None:
                    record["network_egress_gbps"] = value
            if tier1_col is not None and tier1_col < len(row):
                value = parse_number(row[tier1_col])
                if value is not None:
                    record["network_tier1_egress_gbps"] = value
    return sorted(merged.values(), key=lambda item: str(item["name"]))
