"""Engineering-value codec for Modbus registers.

Converts between Python numeric values and tuples of 16-bit register words,
with configurable byte and word order to match different device conventions.

Modbus registers are 16-bit. Multi-register types are encoded as:
  - INT16/UINT16: 1 register
  - INT32/UINT32: 2 registers
  - FLOAT32:      2 registers (IEEE 754 binary32)
  - FLOAT64:      4 registers (IEEE 754 binary64)

Byte order controls the byte arrangement within each 16-bit register.
Word order controls the register sequence for multi-register values.

Both default to ``BIG`` which matches the Modbus spec (high byte / high word first).
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from enum import StrEnum


class DataType(StrEnum):
    INT16 = "int16"
    UINT16 = "uint16"
    INT32 = "int32"
    UINT32 = "uint32"
    FLOAT32 = "float32"
    FLOAT64 = "float64"

    @property
    def register_count(self) -> int:
        return _REGISTER_COUNT[self]

    @property
    def _struct_char(self) -> str:
        return _STRUCT_CHAR[self]


class ByteOrder(StrEnum):
    BIG = "big"
    LITTLE = "little"


class WordOrder(StrEnum):
    BIG = "big"
    LITTLE = "little"


_REGISTER_COUNT: dict[DataType, int] = {
    DataType.INT16: 1,
    DataType.UINT16: 1,
    DataType.INT32: 2,
    DataType.UINT32: 2,
    DataType.FLOAT32: 2,
    DataType.FLOAT64: 4,
}

_STRUCT_CHAR: dict[DataType, str] = {
    DataType.INT16: "h",
    DataType.UINT16: "H",
    DataType.INT32: "i",
    DataType.UINT32: "I",
    DataType.FLOAT32: "f",
    DataType.FLOAT64: "d",
}


Number = int | float


def encode(
    value: Number,
    dtype: DataType,
    *,
    byte_order: ByteOrder = ByteOrder.BIG,
    word_order: WordOrder = WordOrder.BIG,
) -> tuple[int, ...]:
    """Encode ``value`` into a tuple of 16-bit register words.

    Raises ``ValueError`` if ``value`` is out of range for ``dtype``.
    """
    try:
        packed = struct.pack(">" + dtype._struct_char, value)
    except struct.error as exc:
        raise ValueError(f"value {value!r} out of range for {dtype.value}: {exc}") from exc
    return _bytes_to_registers(packed, byte_order, word_order)


def decode(
    registers: Sequence[int],
    dtype: DataType,
    *,
    byte_order: ByteOrder = ByteOrder.BIG,
    word_order: WordOrder = WordOrder.BIG,
) -> Number:
    """Decode register words back into a Python number."""
    expected = dtype.register_count
    if len(registers) != expected:
        raise ValueError(f"expected {expected} registers for {dtype.value}, got {len(registers)}")
    for reg in registers:
        if not 0 <= reg <= 0xFFFF:
            raise ValueError(f"register value out of range [0, 0xFFFF]: {reg}")
    packed = _registers_to_bytes(registers, byte_order, word_order)
    result: Number = struct.unpack(">" + dtype._struct_char, packed)[0]
    return result


# ---------------------------------------------------------------------------
# Byte / word reordering
# ---------------------------------------------------------------------------
def _bytes_to_registers(
    buf: bytes, byte_order: ByteOrder, word_order: WordOrder
) -> tuple[int, ...]:
    words = [(buf[i] << 8) | buf[i + 1] for i in range(0, len(buf), 2)]
    if byte_order is ByteOrder.LITTLE:
        words = [_swap_bytes(w) for w in words]
    if word_order is WordOrder.LITTLE:
        words.reverse()
    return tuple(words)


def _registers_to_bytes(
    registers: Sequence[int], byte_order: ByteOrder, word_order: WordOrder
) -> bytes:
    words = list(registers)
    if word_order is WordOrder.LITTLE:
        words.reverse()
    if byte_order is ByteOrder.LITTLE:
        words = [_swap_bytes(w) for w in words]
    out = bytearray()
    for word in words:
        out.append((word >> 8) & 0xFF)
        out.append(word & 0xFF)
    return bytes(out)


def _swap_bytes(word: int) -> int:
    return ((word & 0xFF) << 8) | ((word >> 8) & 0xFF)
