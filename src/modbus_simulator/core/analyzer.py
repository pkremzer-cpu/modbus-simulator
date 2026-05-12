"""Format / data-type analyser for a block of raw Modbus register values.

Tries every reasonable interpretation and scores each heuristically:

* INT16, UINT16, INT32, UINT32, FLOAT32, FLOAT64 (every byte x word order)
* ASCII string (high + low byte per register, trailing-null aware)
* Packed BCD (4 nibbles per register)
* 16-bit bitfield (Modbus-typical status / alarm register layout)
* Unix epoch timestamp (UINT32 between 2003-01-01 and 2099-12-31)
* Scaled integer (x0.001 / x0.01 / x0.1 / x10 / x100 hints)

Each interpretation is scored 0..1; the scorer rewards "engineering-plausible"
values and penalises NaN / Inf / sentinel / saturation patterns. Sentinel
detection flags common SCADA "invalid" markers (-1, 0x7FFF, 0xFFFF, INT max,
FLOAT32 max, etc.).

Single-register dtypes (INT16/UINT16) ignore word order — only one entry per
byte order is emitted for them, avoiding duplicate rows in the GUI matrix.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from modbus_simulator.core.codec import ByteOrder, DataType, WordOrder, decode

Number = int | float


class SpecialFormat(StrEnum):
    """Non-numeric interpretations of a register block."""

    ASCII = "ascii"
    BCD = "bcd"
    SIGNED_BCD = "signed_bcd"
    BITFIELD = "bitfield"
    TIMESTAMP_UNIX = "timestamp_unix"
    SCALED_INT = "scaled_int"
    MOD10K = "mod10k"                 # legacy Modicon Mod10000 dual-register int
    INT48 = "int48"                   # 48-bit signed across 3 registers
    UINT48 = "uint48"                 # 48-bit unsigned across 3 registers
    FLOAT16 = "float16"               # IEEE 754 binary16 (half float), 1 register
    UTF16_STRING = "utf16_string"     # UTF-16 BE/LE string


@dataclass(frozen=True, slots=True)
class Interpretation:
    """One way to interpret the register block."""

    label: str
    dtype: DataType | SpecialFormat
    byte_order: ByteOrder | None     # None for ASCII/BCD/bitfield/scaled
    word_order: WordOrder | None
    decoded_text: str
    score: float                     # 0..1 plausibility
    reasons: list[str] = field(default_factory=list)
    sentinel: bool = False           # True if the values look like invalid markers

    @property
    def confidence(self) -> str:
        if self.score >= 0.75:
            return "Erős javaslat"
        if self.score >= 0.5:
            return "Valószínű"
        if self.score >= 0.25:
            return "Lehetséges"
        return "Valószínűtlen"


# ---------------------------------------------------------------------------
# Known sentinel constants per dtype
# ---------------------------------------------------------------------------
_INT16_SENTINELS = frozenset({-1, -32768, 32767})
_UINT16_SENTINELS = frozenset({0xFFFF, 0x8000})
_INT32_SENTINELS = frozenset({-1, -(2**31), 2**31 - 1})
_UINT32_SENTINELS = frozenset({0xFFFFFFFF, 0x80000000})
# FLOAT32 max — used as analog-invalid by some PLCs
_FLOAT32_MAX = 3.4028235e38

_UNIX_EPOCH_LO = datetime(2003, 1, 1, tzinfo=UTC).timestamp()
_UNIX_EPOCH_HI = datetime(2099, 12, 31, tzinfo=UTC).timestamp()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def analyse(registers: list[int]) -> list[Interpretation]:
    """Return every interpretation, sorted by score (best first)."""
    if not registers:
        return []
    out: list[Interpretation] = []
    for dtype in DataType:
        for byte_order in ByteOrder:
            # Single-register types don't need both word orders — they yield
            # identical results. Emit only WordOrder.BIG and label without
            # a word-order suffix (see _label).
            word_orders = (WordOrder.BIG,) if dtype.register_count == 1 else tuple(WordOrder)
            for word_order in word_orders:
                out.append(_score_numeric(registers, dtype, byte_order, word_order))

    # Composite / non-numeric interpretations
    out.append(_score_ascii(registers))
    out.append(_score_bcd(registers))
    out.append(_score_signed_bcd(registers))
    out.append(_score_bitfield(registers))
    timestamp = _score_unix_timestamp(registers)
    if timestamp is not None:
        out.append(timestamp)
    out.extend(_scaled_int_candidates(registers))
    out.extend(_score_mod10k(registers))
    out.extend(_score_int48_uint48(registers))
    out.append(_score_float16(registers))
    out.extend(_score_utf16(registers))

    # Cross-register pattern annotations are *added* to existing interpretations
    # (they don't create new ones); see :func:`_annotate_cross_register`.
    _annotate_cross_register(out, registers)

    out.sort(key=lambda i: -i.score)
    return out


# ---------------------------------------------------------------------------
# Numeric scoring
# ---------------------------------------------------------------------------
def _score_numeric(
    registers: list[int], dtype: DataType, byte_order: ByteOrder, word_order: WordOrder
) -> Interpretation:
    chunks = _chunks(registers, dtype.register_count)
    if not chunks:
        return Interpretation(
            label=_label(dtype, byte_order, word_order),
            dtype=dtype,
            byte_order=byte_order,
            word_order=word_order,
            decoded_text="—",
            score=0.0,
            reasons=["nem fér ki egy chunk sem ennyi regiszterbe"],
        )
    values: list[Number] = [
        decode(c, dtype, byte_order=byte_order, word_order=word_order) for c in chunks
    ]

    sentinel_count = _count_sentinels(values, dtype)
    is_sentinel_block = sentinel_count == len(values)

    if dtype in (DataType.FLOAT32, DataType.FLOAT64):
        score, reasons = _score_float(values, dtype)
        text = ", ".join(_fmt_float(v) for v in values)
    elif dtype in (DataType.INT16, DataType.INT32):
        score, reasons = _score_signed(values, dtype)
        text = ", ".join(str(int(v)) for v in values)
    else:
        score, reasons = _score_unsigned(values, dtype)
        text = ", ".join(str(int(v)) for v in values)

    # Sentinel penalty / annotation
    if sentinel_count > 0:
        ratio = sentinel_count / len(values)
        score -= 0.4 * ratio
        if is_sentinel_block:
            reasons.append("MINDEN érték ismert SCADA sentinel (invalid marker)")
        else:
            reasons.append(f"{sentinel_count}/{len(values)} érték ismert sentinel")

    # Proportional leftover penalty (Fix D)
    used = len(chunks) * dtype.register_count
    if used < len(registers):
        leftover = len(registers) - used
        coverage = used / len(registers)
        score *= coverage
        reasons.append(
            f"{leftover} regiszter kimaradt a végén "
            f"({(1 - coverage) * 100:.0f}% lefedettlen)"
        )

    return Interpretation(
        label=_label(dtype, byte_order, word_order),
        dtype=dtype,
        byte_order=byte_order,
        word_order=word_order,
        decoded_text=text,
        score=max(0.0, min(1.0, score)),
        reasons=reasons,
        sentinel=is_sentinel_block,
    )


def _count_sentinels(values: list[Number], dtype: DataType) -> int:
    """Count how many values match documented SCADA sentinel markers."""
    if dtype is DataType.INT16:
        return sum(1 for v in values if int(v) in _INT16_SENTINELS)
    if dtype is DataType.UINT16:
        return sum(1 for v in values if int(v) in _UINT16_SENTINELS)
    if dtype is DataType.INT32:
        return sum(1 for v in values if int(v) in _INT32_SENTINELS)
    if dtype is DataType.UINT32:
        return sum(1 for v in values if int(v) in _UINT32_SENTINELS)
    if dtype is DataType.FLOAT32:
        return sum(
            1 for v in values
            if math.isnan(float(v)) or math.isinf(float(v))
            or math.isclose(abs(float(v)), _FLOAT32_MAX, rel_tol=1e-5)
        )
    if dtype is DataType.FLOAT64:
        return sum(1 for v in values if math.isnan(float(v)) or math.isinf(float(v)))
    return 0


def _score_float(values: list[Number], dtype: DataType) -> tuple[float, list[str]]:
    _ = dtype  # threshold tweaks per dtype could go here; deliberately uniform v1
    reasons: list[str] = []
    score = 0.5
    nan_count = sum(1 for v in values if math.isnan(float(v)))
    inf_count = sum(1 for v in values if math.isinf(float(v)))
    if nan_count == len(values):
        return 0.0, ["minden érték NaN — biztosan nem float"]
    if (nan_count + inf_count) >= max(1, len(values) // 2):
        return 0.05, [f"{nan_count} NaN + {inf_count} Inf — nem float"]
    if nan_count > 0:
        score -= 0.25 * (nan_count / len(values))
        reasons.append(f"{nan_count} NaN érték")
    if inf_count > 0:
        score -= 0.25 * (inf_count / len(values))
        reasons.append(f"{inf_count} Inf érték")

    finite = [float(v) for v in values if not (math.isnan(float(v)) or math.isinf(float(v)))]
    if not finite:
        return 0.05, [*reasons, "nincs véges érték"]

    abs_max = max(abs(v) for v in finite)
    nonzero = [v for v in finite if v != 0]
    abs_min_nz = min(abs(v) for v in nonzero) if nonzero else 0.0

    if abs_max > 1e20:
        score -= 0.35
        reasons.append(f"szélsőséges magnitudó: max |x| = {abs_max:.2e}")
    if abs_min_nz != 0 and abs_min_nz < 1e-20:
        score -= 0.3
        reasons.append(f"szélsőséges magnitudó: min |x| = {abs_min_nz:.2e}")
    if all(-1e6 <= v <= 1e6 for v in finite):
        score += 0.3
        reasons.append("mérnöki tartományban [-1M, 1M]")
    elif all(-1e9 <= v <= 1e9 for v in finite):
        score += 0.15
        reasons.append("mérnöki tartományban [-1G, 1G]")
    if nonzero:
        score += 0.15
        reasons.append(f"{len(nonzero)} nem-nulla érték")
    else:
        score -= 0.1
        reasons.append("minden érték nulla")

    denormal = sum(1 for v in nonzero if 0 < abs(v) < 1e-30)
    if denormal:
        score -= 0.2
        reasons.append(f"{denormal} denormalizált érték")

    return score, reasons


def _score_signed(values: list[Number], dtype: DataType) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.5
    ints = [int(v) for v in values]
    pos = sum(1 for v in ints if v > 0)
    neg = sum(1 for v in ints if v < 0)
    zero = sum(1 for v in ints if v == 0)

    if neg > 0 and pos > 0:
        score += 0.35
        reasons.append("vegyes előjelű (pozitív + negatív) — előjeles típus indokolt")
    elif neg == 0 and pos > 0:
        score -= 0.1
        reasons.append("nincs negatív érték — előjel valószínűleg felesleges")
    if zero == len(ints):
        score -= 0.2
        reasons.append("minden érték nulla")

    abs_max = max((abs(v) for v in ints), default=0)
    if abs_max < 1000:
        score += 0.1
        reasons.append("kis abszolút tartomány (|x| < 1000)")
    elif abs_max > 10**8:
        score -= 0.1
        reasons.append(f"nagyon nagy abszolút érték: {abs_max}")

    _ = dtype
    return score, reasons


def _score_unsigned(values: list[Number], dtype: DataType) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.5
    ints = [int(v) for v in values]
    sat_max = 0xFFFF if dtype is DataType.UINT16 else 0xFFFFFFFF

    high_bit_threshold = 0x8000 if dtype is DataType.UINT16 else 0x80000000
    high_bit_set = sum(1 for v in ints if v >= high_bit_threshold)
    if high_bit_set == 0:
        score += 0.15
        reasons.append("nincs felső-bit beállítva — természetes unsigned tartomány")
    elif high_bit_set == len(ints):
        score -= 0.15
        reasons.append("minden értéknek MSB beállítva — előjeles típus indokoltabb lehet")

    nonzero = sum(1 for v in ints if v != 0)
    if nonzero == 0:
        score -= 0.2
        reasons.append("minden érték nulla")
    elif nonzero > 0:
        score += 0.1

    _ = sat_max  # used by sentinel detection elsewhere
    return score, reasons


# ---------------------------------------------------------------------------
# ASCII / BCD / bitfield / timestamp / scaling
# ---------------------------------------------------------------------------
def _score_ascii(registers: list[int]) -> Interpretation:
    chars: list[str] = []
    printable = 0
    null_count = 0
    nonprint = 0
    trailing_null_run = 0
    seen_nonnull = False
    for r in registers:
        for byte in ((r >> 8) & 0xFF, r & 0xFF):
            if byte == 0:
                null_count += 1
                chars.append("·")
                if seen_nonnull:
                    trailing_null_run = 0  # interrupted; recount from end
            elif 32 <= byte <= 126:
                printable += 1
                seen_nonnull = True
                trailing_null_run = 0
                chars.append(chr(byte))
            else:
                nonprint += 1
                seen_nonnull = True
                trailing_null_run = 0
                chars.append("·")

    # Count trailing nulls from the end (canonical "string + 0x00 pad" pattern)
    for byte in reversed([b for r in registers for b in ((r >> 8) & 0xFF, r & 0xFF)]):
        if byte == 0:
            trailing_null_run += 1
        else:
            break

    text = "".join(chars)
    total = len(registers) * 2
    if total == 0:
        return Interpretation(
            label="ASCII", dtype=SpecialFormat.ASCII, byte_order=None, word_order=None,
            decoded_text="", score=0.0, reasons=["üres"],
        )

    reasons = [f"{printable}/{total} nyomtatható, {null_count} null, {nonprint} egyéb"]
    nonnull = max(1, total - null_count)
    ratio = printable / nonnull
    if printable == 0:
        score = 0.05
        reasons.append("egyetlen nyomtatható karakter sincs")
    else:
        # Continuous score: 0.3 (no visibles) → 0.9 (all visibles)
        score = 0.3 + 0.6 * ratio
        reasons.append(f"a nem-null bájtok {ratio * 100:.0f}%-a nyomtatható")
        # Trailing-null padding is the canonical Modbus string pattern (Fix H)
        if trailing_null_run >= 2 and printable >= 2:
            score += 0.05
            reasons.append(
                f"{trailing_null_run} bájt trailing-null padding — string + 0x00 pad minta"
            )

    return Interpretation(
        label='ASCII string ("HxLx" / regiszter)',
        dtype=SpecialFormat.ASCII,
        byte_order=None,
        word_order=None,
        decoded_text=text,
        score=max(0.0, min(1.0, score)),
        reasons=reasons,
    )


def _score_bcd(registers: list[int]) -> Interpretation:
    digits: list[str] = []
    invalid = 0
    total = 0
    all_zero = True
    for r in registers:
        for shift in (12, 8, 4, 0):
            nibble = (r >> shift) & 0xF
            total += 1
            if nibble != 0:
                all_zero = False
            if nibble < 10:
                digits.append(str(nibble))
            else:
                invalid += 1
                digits.append("?")
    text = " ".join("".join(digits[i : i + 4]) for i in range(0, len(digits), 4))
    if total == 0:
        return Interpretation(
            label="BCD", dtype=SpecialFormat.BCD, byte_order=None, word_order=None,
            decoded_text="", score=0.0, reasons=["üres"],
        )
    if invalid == 0 and not all_zero:
        score = 0.7
        reasons = ["minden nibble érvényes BCD (0-9)"]
    elif invalid == 0 and all_zero:
        score = 0.25
        reasons = ["minden nibble 0 — formálisan érvényes BCD, de információtlan"]
    elif invalid / total > 0.4:
        score = 0.05
        reasons = [f"{invalid}/{total} érvénytelen nibble"]
    else:
        score = max(0.1, 0.6 - invalid / total)
        reasons = [f"{invalid}/{total} érvénytelen nibble"]
    return Interpretation(
        label="Packed BCD (4 nibble / regiszter)",
        dtype=SpecialFormat.BCD,
        byte_order=None,
        word_order=None,
        decoded_text=text,
        score=score,
        reasons=reasons,
    )


def _score_bitfield(registers: list[int]) -> Interpretation:
    """16-bit-per-register status / alarm interpretation."""
    if not registers:
        return Interpretation(
            label="Bitfield", dtype=SpecialFormat.BITFIELD, byte_order=None, word_order=None,
            decoded_text="", score=0.0, reasons=["üres"],
        )

    per_reg_descriptions: list[str] = []
    total_bits = 0
    bits_set = 0
    high_population_count = 0  # registers with >8 bits set (unlikely for bitfield)
    for idx, r in enumerate(registers):
        ones = bin(r).count("1")
        total_bits += 16
        bits_set += ones
        if ones > 8:
            high_population_count += 1
        # show high-order first
        bit_positions = [str(b) for b in range(16) if r & (1 << b)]
        if bit_positions:
            per_reg_descriptions.append(f"r{idx}=0x{r:04X} bits:{','.join(bit_positions)}")
        else:
            per_reg_descriptions.append(f"r{idx}=0x0000 ø")

    density = bits_set / max(1, total_bits)
    score = 0.3
    reasons: list[str] = [f"{bits_set}/{total_bits} bit set, sűrűség {density*100:.0f}%"]
    if density == 0:
        score = 0.15
        reasons.append("minden bit nulla — vagy nincs státusz, vagy üres")
    elif density < 0.25 and high_population_count == 0:
        # Sparse bits set → classic status/alarm register
        score = 0.65
        reasons.append("ritka bit-mintázat (≤25%, max 8/reg) — klasszikus status/alarm minta")
    elif density < 0.5:
        score = 0.45
        reasons.append("közepes bit-sűrűség")
    else:
        score = 0.2
        reasons.append("sűrű bit-mintázat — kevésbé valószínű bitfield")

    return Interpretation(
        label="Bitfield (16 bit / regiszter)",
        dtype=SpecialFormat.BITFIELD,
        byte_order=None,
        word_order=None,
        decoded_text=" | ".join(per_reg_descriptions),
        score=score,
        reasons=reasons,
    )


def _score_unix_timestamp(registers: list[int]) -> Interpretation | None:
    """UINT32 BE/BE in a plausible epoch range → Unix timestamp."""
    if len(registers) < 2:
        return None
    chunks = _chunks(registers, 2)
    decoded_dates: list[str] = []
    valid = 0
    for ch in chunks:
        ts_be = decode(ch, DataType.UINT32, byte_order=ByteOrder.BIG, word_order=WordOrder.BIG)
        ts_be_int = int(ts_be)
        if _UNIX_EPOCH_LO <= ts_be_int <= _UNIX_EPOCH_HI:
            valid += 1
            dt = datetime.fromtimestamp(ts_be_int, tz=UTC)
            decoded_dates.append(f"{ts_be_int} → {dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        else:
            decoded_dates.append(f"{ts_be_int} → kívül az epoch tartományon")
    if valid == 0:
        return None
    score = 0.5 + 0.45 * (valid / max(1, len(chunks)))
    reasons = [f"{valid}/{len(chunks)} UINT32 chunk Unix epoch tartományon belül (2003-2099)"]
    return Interpretation(
        label="Unix timestamp (UINT32 BE/BE seconds)",
        dtype=SpecialFormat.TIMESTAMP_UNIX,
        byte_order=ByteOrder.BIG,
        word_order=WordOrder.BIG,
        decoded_text=" | ".join(decoded_dates),
        score=min(1.0, score),
        reasons=reasons,
    )


def _score_signed_bcd(registers: list[int]) -> Interpretation:
    """Packed BCD with last nibble as sign indicator (0xC = +, 0xD = -, 0xF = unsigned)."""
    if not registers:
        return Interpretation(
            label="Signed BCD",
            dtype=SpecialFormat.SIGNED_BCD,
            byte_order=None,
            word_order=None,
            decoded_text="",
            score=0.0,
            reasons=["üres"],
        )
    decoded_per_reg: list[str] = []
    valid_count = 0
    for r in registers:
        digits = [(r >> 12) & 0xF, (r >> 8) & 0xF, (r >> 4) & 0xF]
        sign_nib = r & 0xF
        if all(d <= 9 for d in digits) and sign_nib in (0xA, 0xB, 0xC, 0xD, 0xE, 0xF):
            sign = -1 if sign_nib in (0xB, 0xD) else 1
            value = sign * (digits[0] * 100 + digits[1] * 10 + digits[2])
            decoded_per_reg.append(str(value))
            valid_count += 1
        else:
            decoded_per_reg.append("?")
    total = len(registers)
    if valid_count == 0:
        return Interpretation(
            label="Signed BCD (3 digit + sign nibble)",
            dtype=SpecialFormat.SIGNED_BCD,
            byte_order=None,
            word_order=None,
            decoded_text=", ".join(decoded_per_reg),
            score=0.05,
            reasons=["egyik regiszter sem érvényes signed BCD"],
        )
    score = 0.4 + 0.4 * (valid_count / total)
    reasons = [
        f"{valid_count}/{total} regiszter érvényes signed BCD (sign nibble: A-F)",
        "3 BCD digit + 1 nibble előjel (IBM/Honeywell EBCDIC mintázat)",
    ]
    return Interpretation(
        label="Signed BCD (3 digit + sign nibble)",
        dtype=SpecialFormat.SIGNED_BCD,
        byte_order=None,
        word_order=None,
        decoded_text=", ".join(decoded_per_reg),
        score=min(0.8, score),
        reasons=reasons,
    )


def _score_mod10k(registers: list[int]) -> list[Interpretation]:
    """Modicon Mod10K: 32-bit value split as low (mod 10000) + high (quotient).

    Two register orderings: low-word-first (legacy Modicon) or high-word-first.
    Valid signal: ALL low-words < 10000. Low-word == 10000 disqualifies.
    """
    if len(registers) < 2:
        return []
    out: list[Interpretation] = []
    for low_first, label_suffix in [
        (True, "low-word first (legacy Modicon)"),
        (False, "high-word first"),
    ]:
        chunks = _chunks(registers, 2)
        if not chunks:
            continue
        valid = 0
        values: list[int] = []
        for ch in chunks:
            if low_first:
                low, high = ch[0], ch[1]
            else:
                high, low = ch[0], ch[1]
            if low >= 10000:
                values.append(-1)  # invalid Mod10K
                continue
            value = high * 10000 + low
            values.append(value)
            valid += 1
        if valid == 0:
            continue
        decoded = ", ".join(str(v) if v >= 0 else "INVALID" for v in values)
        score = 0.4 + 0.4 * (valid / len(chunks))
        reasons = [
            f"{valid}/{len(chunks)} chunk érvényes Mod10K (low-word < 10000)",
            "32-bit érték = high * 10000 + low (legacy Modicon konvenció)",
        ]
        out.append(
            Interpretation(
                label=f"Mod10K {label_suffix}",
                dtype=SpecialFormat.MOD10K,
                byte_order=ByteOrder.BIG,
                word_order=WordOrder.LITTLE if low_first else WordOrder.BIG,
                decoded_text=decoded,
                score=min(0.85, score),
                reasons=reasons,
            )
        )
    return out


def _score_int48_uint48(registers: list[int]) -> list[Interpretation]:
    """48-bit integers across 3 registers (Iskra energy meters, Schneider PM)."""
    if len(registers) < 3:
        return []
    out: list[Interpretation] = []
    chunks = _chunks(registers, 3)
    for signed, label in [(False, SpecialFormat.UINT48), (True, SpecialFormat.INT48)]:
        for byte_order in ByteOrder:
            for word_order in WordOrder:
                values: list[int] = []
                for ch in chunks:
                    raw = _assemble_48bit(ch, byte_order, word_order)
                    if signed and raw >= 1 << 47:
                        raw -= 1 << 48
                    values.append(raw)
                # Score: meaningfully large but not at 2^48 ceiling
                meaningful = sum(1 for v in values if 0 < abs(v) < (1 << 47) - 1000)
                ratio = meaningful / max(1, len(chunks))
                score = 0.3 + 0.4 * ratio
                if all(v == 0 for v in values):
                    score = 0.1
                reasons = [
                    f"{meaningful}/{len(chunks)} chunk értelmes 48-bit tartományban",
                    "Iskra / Schneider PM energy meter konvenció (3 regiszter / 48-bit)",
                ]
                bo = "BE" if byte_order is ByteOrder.BIG else "LE"
                wo = "BE" if word_order is WordOrder.BIG else "LE"
                out.append(
                    Interpretation(
                        label=f"{label.value.upper()} byte={bo} word={wo}",
                        dtype=label,
                        byte_order=byte_order,
                        word_order=word_order,
                        decoded_text=", ".join(str(v) for v in values),
                        score=min(0.8, score),
                        reasons=reasons,
                    )
                )
    return out


def _assemble_48bit(regs: list[int], byte_order: ByteOrder, word_order: WordOrder) -> int:
    """Assemble 3 16-bit registers into one unsigned 48-bit integer."""
    if word_order is WordOrder.LITTLE:
        regs = list(reversed(regs))
    bytes_buf = bytearray()
    for r in regs:
        if byte_order is ByteOrder.BIG:
            bytes_buf.extend([(r >> 8) & 0xFF, r & 0xFF])
        else:
            bytes_buf.extend([r & 0xFF, (r >> 8) & 0xFF])
    return int.from_bytes(bytes_buf, "big", signed=False)


def _score_float16(registers: list[int]) -> Interpretation:
    """IEEE 754 binary16 (half float) — one register per value."""
    import struct as _struct

    if not registers:
        return Interpretation(
            label="FLOAT16",
            dtype=SpecialFormat.FLOAT16,
            byte_order=None,
            word_order=None,
            decoded_text="",
            score=0.0,
            reasons=["üres"],
        )
    values: list[float] = []
    nan_count = 0
    for r in registers:
        try:
            val = _struct.unpack(">e", r.to_bytes(2, "big"))[0]
        except _struct.error:
            val = float("nan")
        if math.isnan(val):
            nan_count += 1
        values.append(val)
    total = len(values)
    finite = [v for v in values if not (math.isnan(v) or math.isinf(v))]
    if not finite:
        return Interpretation(
            label="FLOAT16 (IEEE 754 binary16)",
            dtype=SpecialFormat.FLOAT16,
            byte_order=ByteOrder.BIG,
            word_order=None,
            decoded_text=", ".join(f"{v:.4g}" for v in values),
            score=0.05,
            reasons=[f"{nan_count}/{total} NaN — biztosan nem half float"],
        )
    abs_max = max(abs(v) for v in finite)
    score = 0.35
    reasons = [f"{len(finite)}/{total} véges érték"]
    # half-float effective range is ~6e-5 to ~65504
    if all(1e-3 <= abs(v) <= 65504 for v in finite):
        score += 0.3
        reasons.append("mind a half-float értelmes magnitudó-tartományán belül")
    if abs_max > 65504:
        score -= 0.3
        reasons.append(f"max |x| = {abs_max:.1f} túl nagy half-float-hoz (max 65504)")
    return Interpretation(
        label="FLOAT16 (IEEE 754 binary16)",
        dtype=SpecialFormat.FLOAT16,
        byte_order=ByteOrder.BIG,
        word_order=None,
        decoded_text=", ".join(f"{v:.4g}" for v in values),
        score=max(0.0, min(1.0, score)),
        reasons=reasons,
    )


def _score_utf16(registers: list[int]) -> list[Interpretation]:
    """UTF-16 string decoding (BE / LE)."""
    if not registers:
        return []
    out: list[Interpretation] = []
    raw_be = b"".join(r.to_bytes(2, "big") for r in registers)
    raw_le = b"".join(r.to_bytes(2, "little") for r in registers)
    for raw, endian, label_suffix in [(raw_be, "BE", "BE"), (raw_le, "LE", "LE")]:
        try:
            text = raw.decode(f"utf-16-{endian.lower()}", errors="replace")
        except UnicodeDecodeError:
            continue
        trimmed = text.rstrip("\x00")
        if not trimmed:
            score, reasons = 0.05, ["csak null karakterek"]
        else:
            printable = sum(1 for c in trimmed if c.isprintable())
            ratio = printable / len(trimmed)
            if printable == 0:
                score = 0.05
                reasons = ["egyetlen nyomtatható karakter sincs"]
            else:
                score = 0.3 + 0.5 * ratio
                reasons = [f"a karakterek {ratio * 100:.0f}%-a nyomtatható UTF-16 {endian}"]
        out.append(
            Interpretation(
                label=f"UTF-16 {label_suffix} string",
                dtype=SpecialFormat.UTF16_STRING,
                byte_order=ByteOrder.BIG if endian == "BE" else ByteOrder.LITTLE,
                word_order=None,
                decoded_text=text.replace("\x00", "·"),
                score=min(0.85, score),
                reasons=reasons,
            )
        )
    return out


def _scaled_int_candidates(registers: list[int]) -> list[Interpretation]:
    """Suggest scaled-integer interpretations when raw values look like
    fixed-point engineering data (e.g. temperature stored as ``int * 10``)."""
    out: list[Interpretation] = []
    for divisor, hint in [
        (1000, "milli → kettő tizedes nyomatékos"),
        (100, "centi → két tizedes"),
        (10, "deci → egy tizedes"),
    ]:
        # Try INT16 BIG signed first
        try:
            values = [decode([r], DataType.INT16, byte_order=ByteOrder.BIG) for r in registers]
        except ValueError:
            continue
        ints = [int(v) for v in values]
        if not ints:
            continue
        # Heuristic: values must look "scaled" — at least one value > divisor and < divisor*1e6
        plausible = sum(1 for v in ints if divisor <= abs(v) < divisor * 1000000)
        if plausible < max(1, len(ints) // 2):
            continue
        decoded = ", ".join(f"{v / divisor:g}" for v in ints)
        score = 0.55 + 0.2 * (plausible / len(ints))
        out.append(
            Interpretation(
                label=f"INT16 x 1/{divisor} (skálázott, {hint})",
                dtype=SpecialFormat.SCALED_INT,
                byte_order=ByteOrder.BIG,
                word_order=None,
                decoded_text=decoded,
                score=min(0.85, score),
                reasons=[
                    f"{plausible}/{len(ints)} érték a {divisor}-szeres skálázott "
                    f"tartományban — lehet, hogy a SCADA-ban / {divisor} kell"
                ],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Cross-register patterns (annotation only)
# ---------------------------------------------------------------------------
def _annotate_cross_register(interps: list[Interpretation], registers: list[int]) -> None:
    """Inject extra reasons into matching interpretations when a cross-register
    pattern is detected (status + complement, sum constant, etc.)."""
    if len(registers) < 2:
        return
    patterns: list[str] = []
    pairs = list(zip(registers[::2], registers[1::2], strict=False))
    if pairs and all(((a ^ b) & 0xFFFF) == 0xFFFF for a, b in pairs):
        patterns.append(
            "XOR-komplemens párok (r[2k] XOR r[2k+1] == 0xFFFF) "
            "— status + komplement minta"
        )
    if (
        pairs
        and (pairs[0][0] != 0 or pairs[0][1] != 0)
        and all(
            (a + b) & 0xFFFF == (pairs[0][0] + pairs[0][1]) & 0xFFFF for a, b in pairs
        )
    ):
        patterns.append(
            f"szomszédos párok összege konstans (0x{(pairs[0][0] + pairs[0][1]) & 0xFFFF:04X})"
        )
    if not patterns:
        return
    # Add the patterns as reasons on every interpretation (informational)
    for i in interps:
        i.reasons.extend(patterns)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _chunks(registers: list[int], width: int) -> list[list[int]]:
    return [
        registers[i : i + width]
        for i in range(0, len(registers), width)
        if i + width <= len(registers)
    ]


def _fmt_float(v: Number) -> str:
    f = float(v)
    if math.isnan(f):
        return "NaN"
    if math.isinf(f):
        return "+Inf" if f > 0 else "-Inf"
    return f"{f:.6g}"


def _label(dtype: DataType, byte_order: ByteOrder, word_order: WordOrder) -> str:
    bo = "BE" if byte_order is ByteOrder.BIG else "LE"
    if dtype.register_count == 1:
        return f"{dtype.value.upper()} byte={bo}"
    wo = "BE" if word_order is WordOrder.BIG else "LE"
    return f"{dtype.value.upper()} byte={bo} / word={wo}"
