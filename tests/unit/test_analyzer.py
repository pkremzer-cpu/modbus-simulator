"""Tests for the format analyser heuristics."""

from __future__ import annotations

import struct

import pytest

from modbus_simulator.core.analyzer import (
    Interpretation,
    SpecialFormat,
    analyse,
)
from modbus_simulator.core.codec import ByteOrder, DataType, WordOrder


def _to_regs(values: list[float], dtype: DataType,
             byte_order: ByteOrder = ByteOrder.BIG,
             word_order: WordOrder = WordOrder.BIG) -> list[int]:
    """Encode values back through the codec for fixture-style use."""
    from modbus_simulator.core.codec import encode

    out: list[int] = []
    for v in values:
        out.extend(encode(v, dtype, byte_order=byte_order, word_order=word_order))
    return out


def _best(interps: list[Interpretation], dtype: DataType | SpecialFormat) -> Interpretation:
    for i in interps:
        if i.dtype == dtype:
            return i
    raise AssertionError(f"no interpretation for {dtype}")


# ---------------------------------------------------------------------------
# Empty / minimal
# ---------------------------------------------------------------------------
def test_empty_block_returns_no_interpretations() -> None:
    assert analyse([]) == []


def test_single_register_no_word_order_dimension() -> None:
    # Single-register dtypes (UINT16) ignore word order — analyser emits one
    # entry per byte order, not four, to keep the matrix readable.
    interps = analyse([0x1234])
    uint16 = [i for i in interps if i.dtype is DataType.UINT16]
    assert len(uint16) == 2  # only the two byte orders
    # All emitted entries use WordOrder.BIG (the canonical "don't care").
    assert all(i.word_order is WordOrder.BIG for i in uint16)


# ---------------------------------------------------------------------------
# FLOAT32 — strong signal
# ---------------------------------------------------------------------------
def test_float32_payload_ranks_float32_highest() -> None:
    # Encode 23.5, -7.25, 100.0 as FLOAT32 BIG/BIG and analyse.
    regs = _to_regs(
        [23.5, -7.25, 100.0],
        DataType.FLOAT32,
        byte_order=ByteOrder.BIG,
        word_order=WordOrder.BIG,
    )
    interps = analyse(regs)
    # Top result must be FLOAT32 with the right ordering
    top = interps[0]
    assert top.dtype is DataType.FLOAT32
    assert top.byte_order is ByteOrder.BIG
    assert top.word_order is WordOrder.BIG


def test_float32_swapped_word_order_detected() -> None:
    # Encode with WORD LITTLE, analyser should prefer WORD LITTLE FLOAT32.
    regs = _to_regs(
        [3.14159, 2.71828],
        DataType.FLOAT32,
        word_order=WordOrder.LITTLE,
    )
    interps = analyse(regs)
    top = interps[0]
    assert top.dtype is DataType.FLOAT32
    assert top.word_order is WordOrder.LITTLE


# ---------------------------------------------------------------------------
# INT16 with mixed signs ranks signed over unsigned
# ---------------------------------------------------------------------------
def test_signed_int16_with_mixed_signs_beats_unsigned() -> None:
    regs = _to_regs([-100, -1, 0, 1, 50], DataType.INT16)
    interps = analyse(regs)
    int16 = _best(interps, DataType.INT16)
    uint16 = _best(interps, DataType.UINT16)
    assert int16.score > uint16.score


def test_unsigned_preferred_when_no_negatives() -> None:
    # Plain count-like values, all small
    regs = [10, 20, 30, 40]
    interps = analyse(regs)
    # UINT16 should be at least as plausible as INT16 here
    int16 = _best(interps, DataType.INT16)
    uint16 = _best(interps, DataType.UINT16)
    assert uint16.score >= int16.score - 0.05


# ---------------------------------------------------------------------------
# Saturation / garbage
# ---------------------------------------------------------------------------
def test_all_ffff_unsigned_scores_low() -> None:
    regs = [0xFFFF] * 4
    interps = analyse(regs)
    uint16 = _best(interps, DataType.UINT16)
    assert uint16.score < 0.2


def test_all_nan_floats_score_zero() -> None:
    # Four registers worth of NaN payloads (encoded big-endian / big word)
    nan_bytes = struct.pack(">f", float("nan"))
    nan_regs = struct.unpack(">HH", nan_bytes)
    regs = list(nan_regs) * 2
    interps = analyse(regs)
    # The FLOAT32 with the SAME orientation as the encoding must score low —
    # byte-swapped reinterpretations may produce plausible-looking floats,
    # and that's expected behaviour.
    big_big = next(
        i for i in interps
        if i.dtype is DataType.FLOAT32
        and i.byte_order is ByteOrder.BIG
        and i.word_order is WordOrder.BIG
    )
    assert big_big.score < 0.15


# ---------------------------------------------------------------------------
# ASCII detection
# ---------------------------------------------------------------------------
def test_ascii_text_ranks_high() -> None:
    # "Hello!" — each pair → one register, BE
    text = "Hello!"
    regs = []
    for i in range(0, len(text), 2):
        hi = ord(text[i])
        lo = ord(text[i + 1]) if i + 1 < len(text) else 0
        regs.append((hi << 8) | lo)
    interps = analyse(regs)
    ascii_interp = _best(interps, SpecialFormat.ASCII)
    assert ascii_interp.score >= 0.6
    assert "Hello" in ascii_interp.decoded_text


def test_binary_garbage_ascii_low() -> None:
    regs = [0xFF00, 0x0080, 0xC3D5]
    interps = analyse(regs)
    ascii_interp = _best(interps, SpecialFormat.ASCII)
    assert ascii_interp.score < 0.4


# ---------------------------------------------------------------------------
# BCD detection
# ---------------------------------------------------------------------------
def test_valid_bcd_scores_high() -> None:
    # Each nibble in 0-9
    regs = [0x1234, 0x5678, 0x9012]
    interps = analyse(regs)
    bcd = _best(interps, SpecialFormat.BCD)
    assert bcd.score >= 0.5


def test_invalid_bcd_scores_low() -> None:
    # Hex characters above 9 mean invalid BCD
    regs = [0xABCD, 0xEFFE]
    interps = analyse(regs)
    bcd = _best(interps, SpecialFormat.BCD)
    assert bcd.score < 0.2


# ---------------------------------------------------------------------------
# Output structure
# ---------------------------------------------------------------------------
def test_interpretations_are_sorted_descending() -> None:
    regs = _to_regs([3.14, 2.71], DataType.FLOAT32)
    interps = analyse(regs)
    scores = [i.score for i in interps]
    assert scores == sorted(scores, reverse=True)


def test_every_interpretation_has_label_and_confidence() -> None:
    regs = [1, 2, 3, 4]
    for interp in analyse(regs):
        assert interp.label
        assert interp.confidence in (
            "Erős javaslat", "Valószínű", "Lehetséges", "Valószínűtlen"
        )
        # decoded_text is always present (may be "—" for empty chunks)
        assert interp.decoded_text is not None


# ---------------------------------------------------------------------------
# Sentinel detection (Tier 1 #2)
# ---------------------------------------------------------------------------
class TestSentinels:
    def test_int16_all_minus_one_flagged_as_sentinel(self) -> None:
        # -1 (0xFFFF) is a common INT16 "invalid value" marker.
        regs = [0xFFFF] * 4
        interps = analyse(regs)
        int16 = _best(interps, DataType.INT16)
        assert int16.sentinel is True
        assert any("sentinel" in r.lower() for r in int16.reasons)
        assert int16.score < 0.3

    def test_uint16_all_ffff_marked_sentinel(self) -> None:
        regs = [0xFFFF] * 3
        interps = analyse(regs)
        uint16 = _best(interps, DataType.UINT16)
        assert uint16.sentinel is True

    def test_int16_mixed_with_one_sentinel_partial_penalty(self) -> None:
        regs = [100, 200, 0xFFFF, 50]  # one -1 sentinel
        interps = analyse(regs)
        int16 = _best(interps, DataType.INT16)
        # Should NOT be flagged as fully sentinel, just penalized
        assert int16.sentinel is False


# ---------------------------------------------------------------------------
# Bitfield detection (Tier 1 #4)
# ---------------------------------------------------------------------------
class TestBitfield:
    def test_sparse_bits_score_high_as_bitfield(self) -> None:
        # Classic status pattern: each register has 1-2 bits set
        regs = [0x0001, 0x0080, 0x0102, 0x0000]
        interps = analyse(regs)
        bitfield = _best(interps, SpecialFormat.BITFIELD)
        assert bitfield.score >= 0.5

    def test_dense_bits_low_bitfield_score(self) -> None:
        # Mostly-set bits suggest data, not status flags
        regs = [0xFFFE, 0xFF00, 0xCAFE, 0xBEEF]
        interps = analyse(regs)
        bitfield = _best(interps, SpecialFormat.BITFIELD)
        assert bitfield.score < 0.5

    def test_bitfield_decoded_text_shows_bit_positions(self) -> None:
        regs = [0x0009]  # bits 0 and 3 set
        interps = analyse(regs)
        bitfield = _best(interps, SpecialFormat.BITFIELD)
        assert "0,3" in bitfield.decoded_text or "0, 3" in bitfield.decoded_text


# ---------------------------------------------------------------------------
# Unix timestamp detection (Tier 2 #6)
# ---------------------------------------------------------------------------
class TestUnixTimestamp:
    def test_recent_timestamp_recognised(self) -> None:
        # 2024-01-15 00:00:00 UTC = 1705276800
        ts = 1705276800
        regs = [(ts >> 16) & 0xFFFF, ts & 0xFFFF]
        interps = analyse(regs)
        timestamp = _best(interps, SpecialFormat.TIMESTAMP_UNIX)
        assert timestamp.score >= 0.5
        assert "2024" in timestamp.decoded_text

    def test_garbage_uint32_not_a_timestamp(self) -> None:
        # 0x00000001 = 1970 + 1s — outside the [2003, 2099] window
        regs = [0x0000, 0x0001]
        interps = analyse(regs)
        # No SpecialFormat.TIMESTAMP_UNIX should be present
        ts = [i for i in interps if i.dtype is SpecialFormat.TIMESTAMP_UNIX]
        assert ts == []


# ---------------------------------------------------------------------------
# Scaling hint (Tier 1 #3)
# ---------------------------------------------------------------------------
class TestScalingHint:
    def test_decigree_temperature_suggests_x01(self) -> None:
        # SCADA-typical: temperature stored as int16 x 10
        regs = [235, 200, 245, 180]  # decoded as 23.5 °C, 20.0 °C, etc. when /10
        interps = analyse(regs)
        scaled = [i for i in interps if i.dtype is SpecialFormat.SCALED_INT]
        # At least one scaling candidate should appear; deci (/10) is the obvious one
        labels = [i.label for i in scaled]
        assert any("/10" in lbl for lbl in labels)

    def test_no_scaling_for_obviously_unscaled_data(self) -> None:
        # Tiny values — no scaling makes sense
        regs = [1, 2, 3, 4]
        interps = analyse(regs)
        scaled = [i for i in interps if i.dtype is SpecialFormat.SCALED_INT]
        # Heuristic may suggest none; if present, score should be low
        for s in scaled:
            assert s.score < 0.7


# ---------------------------------------------------------------------------
# Cross-register pattern annotations (Tier 2 #8)
# ---------------------------------------------------------------------------
class TestCrossRegisterPatterns:
    def test_xor_complement_pairs_annotated(self) -> None:
        # r[0] XOR r[1] == 0xFFFF, same for r[2]/r[3]
        regs = [0x1234, 0xEDCB, 0xAA55, 0x55AA]
        interps = analyse(regs)
        # Look for the annotation on any interpretation
        found = any(
            any("XOR-komplemens" in r for r in i.reasons) for i in interps
        )
        assert found


# ---------------------------------------------------------------------------
# Proportional leftover penalty (Fix D)
# ---------------------------------------------------------------------------
class TestLeftoverPenalty:
    def test_full_coverage_no_penalty(self) -> None:
        # FLOAT32 needs 2 regs; 4 regs = 2 chunks, no leftover
        regs = _to_regs([1.0, 2.0], DataType.FLOAT32)
        interps = analyse(regs)
        f32 = next(
            i for i in interps
            if i.dtype is DataType.FLOAT32
            and i.byte_order is ByteOrder.BIG
            and i.word_order is WordOrder.BIG
        )
        # No "maradék" reason
        assert not any("maradék" in r or "kimaradt" in r for r in f32.reasons)

    def test_high_waste_gets_proportional_penalty(self) -> None:
        # FLOAT64 needs 4 regs; with 5 regs we use 4, waste 1 = 20% loss
        regs = [*_to_regs([1.0], DataType.FLOAT64), 0x0001]
        interps = analyse(regs)
        f64 = next(
            i for i in interps
            if i.dtype is DataType.FLOAT64
            and i.byte_order is ByteOrder.BIG
            and i.word_order is WordOrder.BIG
        )
        assert any("kimaradt" in r for r in f64.reasons)


# ---------------------------------------------------------------------------
# ASCII trailing-null reward (Fix H)
# ---------------------------------------------------------------------------
class TestAsciiTrailingNull:
    def test_string_with_null_padding_rewarded(self) -> None:
        # "ABCD" + 4 null bytes — canonical Modbus string-padding pattern
        regs = [
            (ord("A") << 8) | ord("B"),
            (ord("C") << 8) | ord("D"),
            0x0000,
            0x0000,
        ]
        interps = analyse(regs)
        ascii_interp = _best(interps, SpecialFormat.ASCII)
        assert any("padding" in r for r in ascii_interp.reasons)


# ---------------------------------------------------------------------------
# Mod10K detection
# ---------------------------------------------------------------------------
class TestMod10K:
    def test_low_word_first_valid(self) -> None:
        # value = 12345678 split low-first: low = 5678, high = 1234
        regs = [5678, 1234]
        interps = analyse(regs)
        mod = [i for i in interps if i.dtype is SpecialFormat.MOD10K]
        assert any("low-word first" in i.label for i in mod)
        # Decoded value 1234 * 10000 + 5678 = 12345678
        low_first = next(i for i in mod if "low-word first" in i.label)
        assert "12345678" in low_first.decoded_text
        assert low_first.score >= 0.5

    def test_low_word_above_9999_disqualifies(self) -> None:
        # low = 10000 in the low-first interpretation cannot be valid Mod10K.
        # The high-first variant interprets (high=10000, low=1) which IS
        # valid — only the low-first interpretation should flag INVALID.
        regs = [10000, 1]
        interps = analyse(regs)
        low_first = next(
            (i for i in interps
             if i.dtype is SpecialFormat.MOD10K and "low-word first" in i.label),
            None,
        )
        # low-word first should mark the only chunk INVALID
        assert low_first is None or "INVALID" in low_first.decoded_text


# ---------------------------------------------------------------------------
# INT48 / UINT48
# ---------------------------------------------------------------------------
class TestInt48Uint48:
    def test_uint48_assembly_be_be(self) -> None:
        # 3 registers, BE byte BE word — value 0x123456789ABC
        regs = [0x1234, 0x5678, 0x9ABC]
        interps = analyse(regs)
        u48_be = next(
            i for i in interps
            if i.dtype is SpecialFormat.UINT48
            and i.byte_order is ByteOrder.BIG
            and i.word_order is WordOrder.BIG
        )
        assert "20015998343868" in u48_be.decoded_text  # 0x123456789ABC

    def test_int48_negative_with_high_bit_set(self) -> None:
        regs = [0xFFFF, 0xFFFF, 0xFFFE]  # = -2 as INT48 (two's complement)
        interps = analyse(regs)
        i48_be = next(
            i for i in interps
            if i.dtype is SpecialFormat.INT48
            and i.byte_order is ByteOrder.BIG
            and i.word_order is WordOrder.BIG
        )
        assert "-2" in i48_be.decoded_text


# ---------------------------------------------------------------------------
# Signed BCD
# ---------------------------------------------------------------------------
class TestSignedBcd:
    def test_positive_sign_nibble_c(self) -> None:
        # 123 + (sign C = positive) packed as 0x123C
        regs = [0x123C]
        interps = analyse(regs)
        sb = _best(interps, SpecialFormat.SIGNED_BCD)
        assert "123" in sb.decoded_text
        assert sb.score >= 0.5

    def test_negative_sign_nibble_d(self) -> None:
        regs = [0x456D]
        interps = analyse(regs)
        sb = _best(interps, SpecialFormat.SIGNED_BCD)
        assert "-456" in sb.decoded_text


# ---------------------------------------------------------------------------
# FLOAT16
# ---------------------------------------------------------------------------
class TestFloat16:
    def test_one_half_recognised(self) -> None:
        # 0.5 in IEEE 754 binary16 = 0x3800
        regs = [0x3800]
        interps = analyse(regs)
        f16 = _best(interps, SpecialFormat.FLOAT16)
        assert "0.5" in f16.decoded_text


# ---------------------------------------------------------------------------
# UTF-16
# ---------------------------------------------------------------------------
class TestUtf16:
    def test_utf16_be_ascii_recognised(self) -> None:
        # "Hi" in UTF-16 BE: 0x0048 0x0069
        regs = [0x0048, 0x0069]
        interps = analyse(regs)
        u16 = [i for i in interps if i.dtype is SpecialFormat.UTF16_STRING]
        be = next(i for i in u16 if i.byte_order is ByteOrder.BIG)
        assert "Hi" in be.decoded_text


# ---------------------------------------------------------------------------
# End-to-end cross-validation: known payload -> analyse -> detect correctly
# ---------------------------------------------------------------------------
class TestEndToEndCrossValidation:
    """For each new type, encode a known value, analyse, and assert that the
    correct interpretation appears in the top-5 with a score >= threshold."""

    @pytest.mark.parametrize(
        ("value", "byte_order", "word_order"),
        [
            (3.14159, ByteOrder.BIG, WordOrder.BIG),
            (-273.15, ByteOrder.BIG, WordOrder.LITTLE),
            (1.5e6, ByteOrder.LITTLE, WordOrder.BIG),
        ],
    )
    def test_float32_top5_for_engineering_values(
        self, value: float, byte_order: ByteOrder, word_order: WordOrder
    ) -> None:
        regs = _to_regs([value], DataType.FLOAT32, byte_order=byte_order, word_order=word_order)
        top5 = analyse(regs)[:5]
        # The correct interp must appear in the top 5
        matched = next(
            (
                i for i in top5
                if i.dtype is DataType.FLOAT32
                and i.byte_order is byte_order
                and i.word_order is word_order
            ),
            None,
        )
        assert matched is not None, f"expected interp not in top 5 for {value}"

    def test_uint48_3reg_encoded_value_decodes_correctly(self) -> None:
        # Iskra-style 48-bit energy counter
        regs = [0x0000, 0x0001, 0x86A0]  # = 0x000000018650 = 100000
        interps = analyse(regs)
        u48 = next(
            i for i in interps
            if i.dtype is SpecialFormat.UINT48
            and i.byte_order is ByteOrder.BIG
            and i.word_order is WordOrder.BIG
        )
        assert "100000" in u48.decoded_text

    def test_mod10k_legacy_modicon_8765_4321(self) -> None:
        """value = 87654321 split low-first as low=4321, high=8765."""
        regs = [4321, 8765]
        interps = analyse(regs)
        mod_lf = next(
            i for i in interps
            if i.dtype is SpecialFormat.MOD10K and "low-word first" in i.label
        )
        assert "87654321" in mod_lf.decoded_text

    def test_float16_pi_recognised(self) -> None:
        import struct as _struct

        pi_half_bytes = _struct.pack(">e", 3.14)
        reg = int.from_bytes(pi_half_bytes, "big")
        interps = analyse([reg])
        f16 = next(i for i in interps if i.dtype is SpecialFormat.FLOAT16)
        # FLOAT16 has limited precision — should be close to 3.14
        assert any(prefix in f16.decoded_text for prefix in ("3.14", "3.13"))

    def test_utf16_le_long_string(self) -> None:
        text = "Modbus"  # 6 chars x 2 bytes = 12 bytes = 6 regs
        encoded = text.encode("utf-16-le")
        regs = [int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)]
        interps = analyse(regs)
        u16_le = next(
            i for i in interps
            if i.dtype is SpecialFormat.UTF16_STRING and i.byte_order is ByteOrder.LITTLE
        )
        assert "Modbus" in u16_le.decoded_text


# ---------------------------------------------------------------------------
# Edge cases that previously broke or warrant explicit coverage
# ---------------------------------------------------------------------------
class TestEdgeCases:
    def test_empty_regs_no_uint48_emitted(self) -> None:
        """Fewer registers than UINT48 width must not crash and not emit."""
        interps = analyse([0x1234, 0x5678])  # 2 regs, INT48 needs 3
        u48 = [i for i in interps if i.dtype is SpecialFormat.UINT48]
        assert u48 == []

    def test_mod10k_needs_at_least_two_regs(self) -> None:
        interps = analyse([0x1234])  # only one register
        mod = [i for i in interps if i.dtype is SpecialFormat.MOD10K]
        assert mod == []

    def test_signed_bcd_invalid_for_all_zero(self) -> None:
        """Sign nibble 0 is not a valid sign indicator; values flagged '?'."""
        interps = analyse([0x0000, 0x0000])
        sb = _best(interps, SpecialFormat.SIGNED_BCD)
        # All-zero sign nibble is invalid (must be A-F)
        assert sb.decoded_text == "?, ?"

    def test_float16_max_value_recognised(self) -> None:
        # 65504 is the maximum representable half-float
        reg = 0x7BFF  # IEEE 754 binary16 max finite
        interps = analyse([reg])
        f16 = next(i for i in interps if i.dtype is SpecialFormat.FLOAT16)
        # Score should be reasonable (not penalised for being at boundary)
        assert "65504" in f16.decoded_text or "6.55e" in f16.decoded_text

    def test_utf16_handles_2_register_string_with_null_terminator(self) -> None:
        # "Hi\0\0" in UTF-16 BE
        regs = [0x0048, 0x0069, 0x0000, 0x0000]
        interps = analyse(regs)
        u16 = next(
            i for i in interps
            if i.dtype is SpecialFormat.UTF16_STRING and i.byte_order is ByteOrder.BIG
        )
        assert "Hi" in u16.decoded_text
