"""Explicit unit handling for the GCP disk optimizer.

Canonical units used internally everywhere in this package:

======================  ==========================================
Quantity                Canonical unit
======================  ==========================================
disk capacity           **GiB** (gibibyte, 2**30 bytes)
disk throughput         **MiB/s** (mebibytes per second, 2**20 B/s)
disk IOPS               operations / second
money                   **decimal.Decimal**
time                    hours; months are 730 hours (Google's billing rule)
======================  ==========================================

Google Cloud documents disk capacity in GiB and disk throughput in MiB/s, but
humans (and marketing pages, and LLM answers) mix decimal *GB/TB/Gbps* with
binary *GiB/TiB/MiB/s* freely.  Silently mixing them is a classic optimizer bug
(10 GB/s is 9,536.7 MiB/s, not 10,000), so every conversion in this module is a
named, individually tested function instead of an inline magic number.

Note on bit/byte ambiguity: ``Gbps`` means *gigabits* per second (lowercase
``b``) while ``GBps`` means *gigabytes* per second (uppercase ``B``).  The
parsers in this module preserve that distinction deliberately.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Final

DecimalLike = Decimal | int | str | float

# --- byte/capacity constants -------------------------------------------------
BYTES_PER_KIB: Final[Decimal] = Decimal(2**10)
BYTES_PER_MIB: Final[Decimal] = Decimal(2**20)
BYTES_PER_GIB: Final[Decimal] = Decimal(2**30)
BYTES_PER_TIB: Final[Decimal] = Decimal(2**40)
BYTES_PER_KB: Final[Decimal] = Decimal(10**3)
BYTES_PER_MB: Final[Decimal] = Decimal(10**6)
BYTES_PER_GB: Final[Decimal] = Decimal(10**9)
BYTES_PER_TB: Final[Decimal] = Decimal(10**12)

GIB_PER_TIB: Final[Decimal] = Decimal(1024)

#: Google Cloud bills on a 730-hour average month (365 * 24 / 12).
HOURS_PER_MONTH: Final[Decimal] = Decimal(730)


def to_decimal(value: DecimalLike) -> Decimal:
    """Convert a value to :class:`~decimal.Decimal` without binary float error.

    ``float`` inputs are routed through ``str`` so that ``0.1`` becomes
    ``Decimal("0.1")`` rather than the binary representation of ``0.1``.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # pragma: no cover - defensive
        raise TypeError("bool is not a valid numeric input")
    if isinstance(value, (int, str)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    raise TypeError(f"unsupported numeric type: {type(value).__name__}")


# --- capacity ----------------------------------------------------------------
def gb_to_gib(gb: DecimalLike) -> Decimal:
    """Convert decimal gigabytes to gibibytes."""
    return to_decimal(gb) * BYTES_PER_GB / BYTES_PER_GIB


def gib_to_gb(gib: DecimalLike) -> Decimal:
    """Convert gibibytes to decimal gigabytes."""
    return to_decimal(gib) * BYTES_PER_GIB / BYTES_PER_GB


def tb_to_gib(tb: DecimalLike) -> Decimal:
    """Convert decimal terabytes to gibibytes."""
    return to_decimal(tb) * BYTES_PER_TB / BYTES_PER_GIB


def gib_to_tb(gib: DecimalLike) -> Decimal:
    """Convert gibibytes to decimal terabytes."""
    return to_decimal(gib) * BYTES_PER_GIB / BYTES_PER_TB


def tib_to_gib(tib: DecimalLike) -> Decimal:
    """Convert binary tebibytes to gibibytes."""
    return to_decimal(tib) * GIB_PER_TIB


def gib_to_tib(gib: DecimalLike) -> Decimal:
    """Convert gibibytes to tebibytes."""
    return to_decimal(gib) / GIB_PER_TIB


# --- throughput --------------------------------------------------------------
def mbps_to_mibps(mbps: DecimalLike) -> Decimal:
    """Convert decimal megabytes/second to mebibytes/second."""
    return to_decimal(mbps) * BYTES_PER_MB / BYTES_PER_MIB


def mibps_to_mbps(mibps: DecimalLike) -> Decimal:
    """Convert mebibytes/second to decimal megabytes/second."""
    return to_decimal(mibps) * BYTES_PER_MIB / BYTES_PER_MB


def gb_per_s_to_mibps(gbps: DecimalLike) -> Decimal:
    """Convert decimal gigabytes/second (``GB/s``) to mebibytes/second."""
    return to_decimal(gbps) * BYTES_PER_GB / BYTES_PER_MIB


def mibps_to_gb_per_s(mibps: DecimalLike) -> Decimal:
    """Convert mebibytes/second to decimal gigabytes/second."""
    return to_decimal(mibps) * BYTES_PER_MIB / BYTES_PER_GB


def gbit_per_s_to_mibps(gbps: DecimalLike) -> Decimal:
    """Convert decimal gigabits/second (``Gbps``, network convention) to MiB/s."""
    return to_decimal(gbps) * BYTES_PER_GB / Decimal(8) / BYTES_PER_MIB


# --- money -------------------------------------------------------------------
def monthly_from_hourly(
    hourly: DecimalLike, hours: DecimalLike = HOURS_PER_MONTH
) -> Decimal:
    """Convert an hourly rate to a monthly rate (default 730-hour month)."""
    return to_decimal(hourly) * to_decimal(hours)


def quantize(value: DecimalLike, places: int = 6) -> Decimal:
    """Round a decimal to ``places`` decimal places, half-up (money convention)."""
    exponent = Decimal(1).scaleb(-places)
    with localcontext() as ctx:
        ctx.prec = 40
        return to_decimal(value).quantize(exponent, rounding=ROUND_HALF_UP)


# --- text parsing ------------------------------------------------------------
_NUM_UNIT_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<num>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>[A-Za-z]+(?:/[A-Za-z]+)?)\s*$"
)
_BARE_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"^\s*[0-9]+(?:\.[0-9]+)?\s*$")

_SIZE_TO_GIB = {
    "gib": Decimal(1),
    "tib": GIB_PER_TIB,
    "kib": Decimal(1) / Decimal(2**20),
    "mib": Decimal(1) / Decimal(2**10),
    "gb": BYTES_PER_GB / BYTES_PER_GIB,
    "tb": BYTES_PER_TB / BYTES_PER_GIB,
    "kb": BYTES_PER_KB / BYTES_PER_GIB,
    "mb": BYTES_PER_MB / BYTES_PER_GIB,
}


def parse_size_gib(text: str) -> Decimal:
    """Parse a human size such as ``"10TB"``, ``"500 GiB"`` into GiB.

    ``GB/TB`` are decimal (10**9 / 10**12 bytes); ``GiB/TiB`` are binary.  A bare
    number is interpreted as GiB, the canonical unit of this package.

    Raises:
        ValueError: if ``text`` is not a recognized size.
    """
    if _BARE_NUMBER_RE.match(text):
        return to_decimal(text.strip())
    match = _NUM_UNIT_RE.match(text)
    if match is None:
        raise ValueError(f"cannot parse size {text!r}; expected e.g. '10TB' or '500GiB'")
    unit = match.group("unit").lower()
    if unit not in _SIZE_TO_GIB:
        known = ", ".join(sorted(_SIZE_TO_GIB))
        raise ValueError(f"unknown size unit {match.group('unit')!r} in {text!r}; known: {known}")
    return to_decimal(match.group("num")) * _SIZE_TO_GIB[unit]


_BYTE_RATE_UNITS: Final[dict[str, Decimal]] = {
    "b/s": Decimal(1),
    "kb/s": BYTES_PER_KB,
    "mb/s": BYTES_PER_MB,
    "gb/s": BYTES_PER_GB,
    "kib/s": BYTES_PER_KIB,
    "mib/s": BYTES_PER_MIB,
    "gib/s": BYTES_PER_GIB,
}
_BIT_RATE_UNITS: Final[dict[str, Decimal]] = {
    "bit/s": Decimal(1),
    "kbit/s": BYTES_PER_KB,
    "mbit/s": BYTES_PER_MB,
    "gbit/s": BYTES_PER_GB,
    "kibit/s": BYTES_PER_KIB,
    "mibit/s": BYTES_PER_MIB,
    "gibit/s": BYTES_PER_GIB,
}
_PREFIX_FACTORS: Final[dict[str, Decimal]] = {
    "k": BYTES_PER_KB,
    "m": BYTES_PER_MB,
    "g": BYTES_PER_GB,
    "ki": BYTES_PER_KIB,
    "mi": BYTES_PER_MIB,
    "gi": BYTES_PER_GIB,
}


def parse_throughput_mibps(text: str) -> Decimal:
    """Parse a throughput such as ``"10GBps"`` or ``"1Gbps"`` into MiB/s.

    Lowercase ``b`` means bits (``Gbps``), uppercase ``B`` means bytes
    (``GBps``).  ``/s`` forms follow the same rule (``MB/s`` vs ``Mbit/s``).  A
    bare number is interpreted as MiB/s, the canonical unit of this package.

    Raises:
        ValueError: if ``text`` is not a recognized throughput.
    """
    if _BARE_NUMBER_RE.match(text):
        return to_decimal(text.strip())
    match = _NUM_UNIT_RE.match(text)
    if match is None:
        raise ValueError(f"cannot parse throughput {text!r}; expected e.g. '10GBps' or '1Gbps'")
    num = to_decimal(match.group("num"))
    unit = match.group("unit")
    lower = unit.lower()

    if lower in _BYTE_RATE_UNITS:
        bytes_per_second = num * _BYTE_RATE_UNITS[lower]
    elif lower in _BIT_RATE_UNITS:
        bytes_per_second = num * _BIT_RATE_UNITS[lower] / Decimal(8)
    elif lower.endswith("bps"):
        prefix = lower[:-3]
        factor = _PREFIX_FACTORS.get(prefix)
        if factor is None:
            raise ValueError(f"unknown throughput unit {unit!r} in {text!r}")
        # 'GBps' (uppercase B) is bytes; 'Gbps' (lowercase b) is bits.
        bytes_per_second = num * factor if "B" in unit else num * factor / Decimal(8)
    else:
        raise ValueError(f"unknown throughput unit {unit!r} in {text!r}")
    return bytes_per_second / BYTES_PER_MIB
