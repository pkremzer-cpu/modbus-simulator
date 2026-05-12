"""Tests for modbus_simulator.core.exceptions — exception injection engine."""

from __future__ import annotations

import random

import pytest

from modbus_simulator.core.exceptions import (
    ACTION_TO_CODE,
    ExceptionRule,
    RuleAction,
    RuleEngine,
)


def rule(
    *,
    name: str = "r",
    fcs: frozenset[int] | set[int] = frozenset({3}),
    units: frozenset[int] | set[int] = frozenset(),
    start: int = 0,
    end: int = 100,
    action: RuleAction = RuleAction.SLAVE_DEVICE_FAILURE,
    delay_ms: float = 0.0,
    probability: float = 1.0,
) -> ExceptionRule:
    return ExceptionRule(
        name=name,
        function_codes=frozenset(fcs),
        unit_ids=frozenset(units),
        address_start=start,
        address_end=end,
        action=action,
        delay_ms=delay_ms,
        probability=probability,
    )


# ---------------------------------------------------------------------------
# Basic matching
# ---------------------------------------------------------------------------
class TestMatching:
    def test_empty_engine_returns_none(self) -> None:
        engine = RuleEngine()
        assert engine.evaluate(function_code=3, unit_id=1, address=0, count=1) is None

    def test_fc_and_address_match(self) -> None:
        engine = RuleEngine([rule(fcs={3}, start=10, end=20)])
        match = engine.evaluate(function_code=3, unit_id=1, address=15, count=1)
        assert match is not None
        assert match.exception_code == ACTION_TO_CODE[RuleAction.SLAVE_DEVICE_FAILURE]

    def test_fc_mismatch(self) -> None:
        engine = RuleEngine([rule(fcs={3}, start=0, end=100)])
        assert engine.evaluate(function_code=4, unit_id=1, address=0, count=1) is None

    def test_address_below_range(self) -> None:
        engine = RuleEngine([rule(start=10, end=20)])
        assert engine.evaluate(function_code=3, unit_id=1, address=5, count=1) is None

    def test_address_above_range(self) -> None:
        engine = RuleEngine([rule(start=10, end=20)])
        assert engine.evaluate(function_code=3, unit_id=1, address=21, count=1) is None

    def test_address_range_partial_overlap(self) -> None:
        engine = RuleEngine([rule(start=10, end=20)])
        # request 8..12 overlaps with rule 10..20
        match = engine.evaluate(function_code=3, unit_id=1, address=8, count=5)
        assert match is not None

    def test_address_range_contained(self) -> None:
        engine = RuleEngine([rule(start=10, end=20)])
        # request 12..15 contained in rule 10..20
        match = engine.evaluate(function_code=3, unit_id=1, address=12, count=4)
        assert match is not None

    def test_address_range_spans_rule(self) -> None:
        engine = RuleEngine([rule(start=10, end=20)])
        # request 5..25 contains rule 10..20
        match = engine.evaluate(function_code=3, unit_id=1, address=5, count=21)
        assert match is not None


class TestUnitIdMatching:
    def test_empty_set_matches_any_unit(self) -> None:
        engine = RuleEngine([rule(units=set())])
        for uid in (1, 5, 247):
            assert engine.evaluate(function_code=3, unit_id=uid, address=0, count=1) is not None

    def test_specific_unit_matches(self) -> None:
        engine = RuleEngine([rule(units={5})])
        assert engine.evaluate(function_code=3, unit_id=5, address=0, count=1) is not None

    def test_specific_unit_mismatch(self) -> None:
        engine = RuleEngine([rule(units={5})])
        assert engine.evaluate(function_code=3, unit_id=6, address=0, count=1) is None


# ---------------------------------------------------------------------------
# Rule priority — first match wins
# ---------------------------------------------------------------------------
class TestRulePriority:
    def test_first_match_wins(self) -> None:
        engine = RuleEngine(
            [
                rule(name="broad", start=0, end=1000, action=RuleAction.SLAVE_BUSY),
                rule(name="narrow", start=5, end=10, action=RuleAction.ILLEGAL_FUNCTION),
            ]
        )
        match = engine.evaluate(function_code=3, unit_id=1, address=7, count=1)
        assert match is not None and match.rule.name == "broad"


# ---------------------------------------------------------------------------
# Action codes
# ---------------------------------------------------------------------------
class TestActionCodes:
    @pytest.mark.parametrize(
        ("action", "code"),
        [
            (RuleAction.ILLEGAL_FUNCTION, 0x01),
            (RuleAction.ILLEGAL_DATA_ADDRESS, 0x02),
            (RuleAction.ILLEGAL_DATA_VALUE, 0x03),
            (RuleAction.SLAVE_DEVICE_FAILURE, 0x04),
            (RuleAction.SLAVE_BUSY, 0x06),
            (RuleAction.GATEWAY_PATH_UNAVAILABLE, 0x0A),
            (RuleAction.GATEWAY_TARGET_FAILED, 0x0B),
        ],
    )
    def test_exception_code_mapping(self, action: RuleAction, code: int) -> None:
        engine = RuleEngine([rule(action=action)])
        match = engine.evaluate(function_code=3, unit_id=1, address=0, count=1)
        assert match is not None and match.exception_code == code

    def test_drop_has_no_code(self) -> None:
        engine = RuleEngine([rule(action=RuleAction.DROP)])
        match = engine.evaluate(function_code=3, unit_id=1, address=0, count=1)
        assert match is not None and match.exception_code is None
        assert match.is_drop is True


class TestDelay:
    def test_delay_passes_through(self) -> None:
        engine = RuleEngine([rule(delay_ms=500)])
        match = engine.evaluate(function_code=3, unit_id=1, address=0, count=1)
        assert match is not None and match.delay_ms == 500


# ---------------------------------------------------------------------------
# Probability
# ---------------------------------------------------------------------------
class TestProbability:
    def test_probability_1_always_fires(self) -> None:
        engine = RuleEngine([rule(probability=1.0)])
        for _ in range(20):
            assert engine.evaluate(function_code=3, unit_id=1, address=0, count=1) is not None

    def test_probability_0_never_fires(self) -> None:
        engine = RuleEngine([rule(probability=0.0)])
        for _ in range(20):
            assert engine.evaluate(function_code=3, unit_id=1, address=0, count=1) is None

    def test_probability_half_fires_roughly_half(self) -> None:
        engine = RuleEngine([rule(probability=0.5)], rng=random.Random(42))
        hits = sum(
            1
            for _ in range(1000)
            if engine.evaluate(function_code=3, unit_id=1, address=0, count=1) is not None
        )
        assert 400 < hits < 600  # binomial confidence

    def test_rejects_probability_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            ExceptionRule(
                name="x",
                function_codes=frozenset({3}),
                unit_ids=frozenset(),
                address_start=0,
                address_end=10,
                action=RuleAction.SLAVE_BUSY,
                probability=1.5,
            )


# ---------------------------------------------------------------------------
# Rule construction validation
# ---------------------------------------------------------------------------
class TestRuleValidation:
    def test_rejects_start_gt_end(self) -> None:
        with pytest.raises(ValueError):
            ExceptionRule(
                name="x",
                function_codes=frozenset({3}),
                unit_ids=frozenset(),
                address_start=100,
                address_end=50,
                action=RuleAction.SLAVE_BUSY,
            )

    def test_rejects_empty_function_codes(self) -> None:
        with pytest.raises(ValueError):
            ExceptionRule(
                name="x",
                function_codes=frozenset(),
                unit_ids=frozenset(),
                address_start=0,
                address_end=10,
                action=RuleAction.SLAVE_BUSY,
            )

    def test_rejects_negative_delay(self) -> None:
        with pytest.raises(ValueError):
            ExceptionRule(
                name="x",
                function_codes=frozenset({3}),
                unit_ids=frozenset(),
                address_start=0,
                address_end=10,
                action=RuleAction.SLAVE_BUSY,
                delay_ms=-1,
            )


# ---------------------------------------------------------------------------
# Engine mutation
# ---------------------------------------------------------------------------
class TestEngineMutation:
    def test_add_rule(self) -> None:
        engine = RuleEngine()
        r = rule()
        engine.add_rule(r)
        assert engine.rules == [r]

    def test_remove_rule(self) -> None:
        engine = RuleEngine()
        r = rule()
        engine.add_rule(r)
        engine.remove_rule(r)
        assert engine.rules == []

    def test_clear(self) -> None:
        engine = RuleEngine([rule(), rule()])
        engine.clear()
        assert engine.rules == []
