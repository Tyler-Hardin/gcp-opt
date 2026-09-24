"""Unit-conversion tests: exact decimals, bit/byte distinction, round trips."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gcp_opt import units


def test_decimal_from_float_is_decimal_of_repr() -> None:
    assert units.to_decimal(0.1) == Decimal("0.1")
    assert units.to_decimal(3) == Decimal(3)
    assert units.to_decimal("2.5") == Decimal("2.5")


def test_decimal_rejects_bool() -> None:
    with pytest.raises(TypeError):
        units.to_decimal(True)


def test_decimal_gib_conversions_are_exact() -> None:
    # 1 GB = 10**9 bytes, 1 GiB = 2**30 bytes -> exactly 0.931322574615478515625 GiB.
    assert units.gb_to_gib(1) == Decimal("0.931322574615478515625")
    assert units.gib_to_gb(1) == Decimal("1.073741824")
    assert units.tb_to_gib(10) == Decimal("9313.22574615478515625")
    assert units.tib_to_gib(1) == Decimal(1024)


def test_throughput_conversions_are_exact() -> None:
    # 10 GB/s (decimal bytes) = 10e9 / 2**20 MiB/s.
    assert units.gb_per_s_to_mibps(10) == Decimal("9536.7431640625")
    # 10 Gbps (bits) = 10e9 / 8 / 2**20 MiB/s.
    assert units.gbit_per_s_to_mibps(10) == Decimal("1192.0928955078125")
    assert units.mbps_to_mibps(1) == Decimal("0.95367431640625")


@given(st.integers(min_value=0, max_value=10**6))
def test_gib_gb_round_trip(value: int) -> None:
    assert units.gb_to_gib(units.gib_to_gb(value)) == Decimal(value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10TB", Decimal("9313.22574615478515625")),
        ("10TiB", Decimal(10240)),
        ("500GiB", Decimal(500)),
        ("500", Decimal(500)),
        ("1.5TiB", Decimal(1536)),
    ],
)
def test_parse_size_gib(text: str, expected: Decimal) -> None:
    assert units.parse_size_gib(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10GBps", Decimal("9536.7431640625")),  # bytes
        ("10Gbps", Decimal("1192.0928955078125")),  # bits
        ("1GB/s", Decimal("953.67431640625")),
        ("1Mbit/s", Decimal("0.11920928955078125")),
        ("1024MiB/s", Decimal(1024)),
        ("1024", Decimal(1024)),
    ],
)
def test_parse_throughput_mibps(text: str, expected: Decimal) -> None:
    assert units.parse_throughput_mibps(text) == expected


def test_bit_vs_byte_is_not_collapsed() -> None:
    # The single most dangerous ambiguity in this domain: 10 Gbps != 10 GBps.
    assert units.parse_throughput_mibps("10Gbps") * 8 == units.parse_throughput_mibps(
        "10GBps"
    )


@pytest.mark.parametrize("bad", ["", "10 furlongs", "abcGiB", "10XB"])
def test_parse_size_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError, match=r"cannot parse size|unknown size unit"):
        units.parse_size_gib(bad)


@pytest.mark.parametrize("bad", ["", "10 furlongs/s", "10Xbps"])
def test_parse_throughput_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError, match=r"cannot parse throughput|unknown throughput unit"):
        units.parse_throughput_mibps(bad)


def test_monthly_from_hourly_uses_730_hours() -> None:
    assert Decimal(730) == units.HOURS_PER_MONTH
    assert units.monthly_from_hourly(Decimal("0.000136986")) == Decimal("0.09999978")


def test_quantize_half_up() -> None:
    assert units.quantize(Decimal("1.005"), 2) == Decimal("1.01")
    assert units.quantize(Decimal("1.004"), 2) == Decimal("1.00")
