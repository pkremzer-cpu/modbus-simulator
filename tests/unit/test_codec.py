"""Tests for modbus_simulator.core.codec."""

from __future__ import annotations

import math

import pytest

from modbus_simulator.core.codec import (
    ByteOrder,
    DataType,
    WordOrder,
    decode,
    encode,
)


# ---------------------------------------------------------------------------
# DataType metadata
# ---------------------------------------------------------------------------
class TestDataType:
    @pytest.mark.parametrize(
        ("dtype", "count"),
        [
            (DataType.INT16, 1),
            (DataType.UINT16, 1),
            (DataType.INT32, 2),
            (DataType.UINT32, 2),
            (DataType.FLOAT32, 2),
            (DataType.FLOAT64, 4),
        ],
    )
    def test_register_count(self, dtype: DataType, count: int) -> None:
        assert dtype.register_count == count


# ---------------------------------------------------------------------------
# 16-bit integer
# ---------------------------------------------------------------------------
class TestInt16:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, (0,)),
            (1, (1,)),
            (1234, (1234,)),
            (-1, (0xFFFF,)),
            (-32768, (0x8000,)),
            (32767, (0x7FFF,)),
        ],
    )
    def test_roundtrip(self, value: int, expected: tuple[int, ...]) -> None:
        assert encode(value, DataType.INT16) == expected
        assert decode(expected, DataType.INT16) == value

    @pytest.mark.parametrize("value", [-32769, 32768, 70000])
    def test_overflow_raises(self, value: int) -> None:
        with pytest.raises(ValueError):
            encode(value, DataType.INT16)


class TestUInt16:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, (0,)),
            (1, (1,)),
            (65535, (0xFFFF,)),
        ],
    )
    def test_roundtrip(self, value: int, expected: tuple[int, ...]) -> None:
        assert encode(value, DataType.UINT16) == expected
        assert decode(expected, DataType.UINT16) == value

    @pytest.mark.parametrize("value", [-1, 65536])
    def test_overflow_raises(self, value: int) -> None:
        with pytest.raises(ValueError):
            encode(value, DataType.UINT16)


# ---------------------------------------------------------------------------
# 32-bit integer  — byte / word order matrix
# ---------------------------------------------------------------------------
class TestInt32Order:
    # value 0x12345678 chosen so every byte is distinct
    VAL = 0x12345678

    def test_big_big(self) -> None:
        regs = encode(self.VAL, DataType.INT32)
        assert regs == (0x1234, 0x5678)
        assert decode(regs, DataType.INT32) == self.VAL

    def test_big_little_word(self) -> None:
        regs = encode(self.VAL, DataType.INT32, word_order=WordOrder.LITTLE)
        assert regs == (0x5678, 0x1234)
        assert decode(regs, DataType.INT32, word_order=WordOrder.LITTLE) == self.VAL

    def test_little_byte_big_word(self) -> None:
        regs = encode(self.VAL, DataType.INT32, byte_order=ByteOrder.LITTLE)
        assert regs == (0x3412, 0x7856)
        assert decode(regs, DataType.INT32, byte_order=ByteOrder.LITTLE) == self.VAL

    def test_little_byte_little_word(self) -> None:
        regs = encode(
            self.VAL,
            DataType.INT32,
            byte_order=ByteOrder.LITTLE,
            word_order=WordOrder.LITTLE,
        )
        assert regs == (0x7856, 0x3412)
        assert (
            decode(
                regs,
                DataType.INT32,
                byte_order=ByteOrder.LITTLE,
                word_order=WordOrder.LITTLE,
            )
            == self.VAL
        )


class TestInt32Range:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (-1, (0xFFFF, 0xFFFF)),
            (-2, (0xFFFF, 0xFFFE)),
            (0x7FFFFFFF, (0x7FFF, 0xFFFF)),
            (-0x80000000, (0x8000, 0x0000)),
        ],
    )
    def test_roundtrip(self, value: int, expected: tuple[int, int]) -> None:
        assert encode(value, DataType.INT32) == expected
        assert decode(expected, DataType.INT32) == value

    @pytest.mark.parametrize("value", [0x80000000, -0x80000001])
    def test_overflow(self, value: int) -> None:
        with pytest.raises(ValueError):
            encode(value, DataType.INT32)


class TestUInt32:
    def test_max(self) -> None:
        regs = encode(0xFFFFFFFF, DataType.UINT32)
        assert regs == (0xFFFF, 0xFFFF)
        assert decode(regs, DataType.UINT32) == 0xFFFFFFFF

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            encode(-1, DataType.UINT32)


# ---------------------------------------------------------------------------
# Floating point
# ---------------------------------------------------------------------------
class TestFloat32:
    @pytest.mark.parametrize(
        "value",
        [0.0, 1.0, -1.0, 3.14159, -273.15, 1.0e-10, 1.0e10, float("inf"), float("-inf")],
    )
    def test_roundtrip(self, value: float) -> None:
        regs = encode(value, DataType.FLOAT32)
        assert len(regs) == 2
        decoded = decode(regs, DataType.FLOAT32)
        assert isinstance(decoded, float)
        if math.isinf(value):
            assert math.isinf(decoded) and math.copysign(1, decoded) == math.copysign(1, value)
        else:
            # float32 has ~7 significant digits of precision
            assert math.isclose(decoded, value, rel_tol=1e-6, abs_tol=1e-30)

    def test_nan(self) -> None:
        regs = encode(float("nan"), DataType.FLOAT32)
        decoded = decode(regs, DataType.FLOAT32)
        assert isinstance(decoded, float) and math.isnan(decoded)

    def test_1_0_encoding(self) -> None:
        # IEEE 754 binary32 for 1.0 is 0x3F800000 → registers (0x3F80, 0x0000)
        assert encode(1.0, DataType.FLOAT32) == (0x3F80, 0x0000)

    def test_word_swap(self) -> None:
        assert encode(1.0, DataType.FLOAT32, word_order=WordOrder.LITTLE) == (0x0000, 0x3F80)


class TestFloat64:
    @pytest.mark.parametrize(
        "value",
        [0.0, 1.0, -1.0, math.pi, math.e, 1.0e-100, 1.0e100, -0.0],
    )
    def test_roundtrip_exact(self, value: float) -> None:
        regs = encode(value, DataType.FLOAT64)
        assert len(regs) == 4
        decoded = decode(regs, DataType.FLOAT64)
        # float64 is exact roundtrip
        if value == 0.0:
            assert decoded == 0.0
        else:
            assert decoded == value

    def test_word_order_reverses_all_four(self) -> None:
        regs_big = encode(math.pi, DataType.FLOAT64)
        regs_little = encode(math.pi, DataType.FLOAT64, word_order=WordOrder.LITTLE)
        assert regs_little == tuple(reversed(regs_big))


# ---------------------------------------------------------------------------
# Input validation on decode
# ---------------------------------------------------------------------------
class TestDecodeValidation:
    def test_wrong_register_count(self) -> None:
        with pytest.raises(ValueError, match="expected 2 registers"):
            decode((1, 2, 3), DataType.INT32)

    def test_register_value_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="register value out of range"):
            decode((0x10000,), DataType.UINT16)

    def test_negative_register_value(self) -> None:
        with pytest.raises(ValueError, match="register value out of range"):
            decode((-1,), DataType.UINT16)
