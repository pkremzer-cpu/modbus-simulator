"""Tests for modbus_simulator.core.simulator."""

from __future__ import annotations

import math

import pytest

from modbus_simulator.core.simulator import (
    Constant,
    Distribution,
    Pattern,
    Ramp,
    RampDirection,
    RandomGen,
    Script,
    Sine,
    Toggle,
)


class TestConstant:
    def test_returns_value_regardless_of_time(self) -> None:
        g = Constant(value=42)
        assert g.sample(0.0, 0) == 42
        assert g.sample(1000.0, 99) == 42

    def test_float_value(self) -> None:
        assert Constant(value=3.14).sample(0.0, 0) == pytest.approx(3.14)


class TestRamp:
    def test_up_starts_at_min(self) -> None:
        g = Ramp(min=0, max=100, step=10, period_ms=1000, direction=RampDirection.UP)
        assert g.sample(0.0, 0) == 0

    def test_up_reaches_near_max_mid_cycle(self) -> None:
        g = Ramp(min=0, max=100, step=1, period_ms=1000, direction=RampDirection.UP)
        assert g.sample(0.5, 0) == pytest.approx(50)
        assert g.sample(0.99, 0) == pytest.approx(99)

    def test_up_wraps(self) -> None:
        g = Ramp(min=0, max=100, step=10, period_ms=1000, direction=RampDirection.UP)
        # At t=1.0 we wrap back to min (exact modulo)
        assert g.sample(1.0, 0) == 0

    def test_up_quantised(self) -> None:
        g = Ramp(min=0, max=100, step=10, period_ms=1000, direction=RampDirection.UP)
        assert g.sample(0.23, 0) == 20  # 23 snaps to nearest 10

    def test_down_starts_at_max(self) -> None:
        g = Ramp(min=0, max=100, step=10, period_ms=1000, direction=RampDirection.DOWN)
        assert g.sample(0.0, 0) == 100

    def test_down_mid_cycle(self) -> None:
        g = Ramp(min=0, max=100, step=1, period_ms=1000, direction=RampDirection.DOWN)
        assert g.sample(0.5, 0) == pytest.approx(50)

    def test_pingpong_peaks_at_half(self) -> None:
        g = Ramp(min=0, max=100, step=1, period_ms=1000, direction=RampDirection.PINGPONG)
        assert g.sample(0.0, 0) == pytest.approx(0, abs=1)
        assert g.sample(0.5, 0) == pytest.approx(100, abs=1)
        assert g.sample(1.0, 0) == pytest.approx(0, abs=1)

    def test_pingpong_is_symmetric(self) -> None:
        g = Ramp(min=0, max=100, step=1, period_ms=1000, direction=RampDirection.PINGPONG)
        assert g.sample(0.25, 0) == pytest.approx(g.sample(0.75, 0), abs=1)

    def test_rejects_min_ge_max(self) -> None:
        with pytest.raises(ValueError):
            Ramp(min=10, max=5, step=1, period_ms=1000, direction=RampDirection.UP)

    def test_rejects_zero_step(self) -> None:
        with pytest.raises(ValueError):
            Ramp(min=0, max=100, step=0, period_ms=1000, direction=RampDirection.UP)

    def test_rejects_zero_period(self) -> None:
        with pytest.raises(ValueError):
            Ramp(min=0, max=100, step=1, period_ms=0, direction=RampDirection.UP)


class TestSine:
    def test_at_t_zero(self) -> None:
        g = Sine(amplitude=1.0, offset=0.0, frequency_hz=1.0, phase_deg=0.0)
        assert g.sample(0.0, 0) == pytest.approx(0.0)

    def test_at_quarter_period(self) -> None:
        g = Sine(amplitude=1.0, offset=0.0, frequency_hz=1.0, phase_deg=0.0)
        assert g.sample(0.25, 0) == pytest.approx(1.0)

    def test_offset_shifts_curve(self) -> None:
        g = Sine(amplitude=1.0, offset=100.0, frequency_hz=1.0, phase_deg=0.0)
        assert g.sample(0.25, 0) == pytest.approx(101.0)

    def test_phase_90_is_cosine(self) -> None:
        g = Sine(amplitude=1.0, offset=0.0, frequency_hz=1.0, phase_deg=90.0)
        assert g.sample(0.0, 0) == pytest.approx(1.0)

    def test_rejects_non_positive_frequency(self) -> None:
        with pytest.raises(ValueError):
            Sine(amplitude=1.0, offset=0.0, frequency_hz=0.0, phase_deg=0.0)


class TestRandomGen:
    def test_uniform_within_bounds(self) -> None:
        g = RandomGen(min=10, max=20, distribution=Distribution.UNIFORM, update_ms=100)
        for t in [i / 10 for i in range(50)]:
            v = float(g.sample(t, 0))
            assert 10.0 <= v <= 20.0

    def test_gaussian_mostly_within_range(self) -> None:
        g = RandomGen(min=0, max=100, distribution=Distribution.GAUSSIAN, update_ms=100)
        samples = [float(g.sample(i / 10, 0)) for i in range(200)]
        # Mean should be near 50, rough check
        avg = sum(samples) / len(samples)
        assert 35 < avg < 65

    def test_deterministic_within_bucket(self) -> None:
        g = RandomGen(min=0, max=1000, distribution=Distribution.UNIFORM, update_ms=100, seed=7)
        # Two calls within the same 100 ms bucket return identical values
        v1 = g.sample(0.02, 0)
        v2 = g.sample(0.05, 0)
        v3 = g.sample(0.09, 0)
        assert v1 == v2 == v3

    def test_changes_across_buckets(self) -> None:
        g = RandomGen(min=0, max=10000, distribution=Distribution.UNIFORM, update_ms=100, seed=7)
        values = {g.sample(i * 0.1, 0) for i in range(10)}
        # It's astronomically unlikely all 10 buckets produce the same float
        assert len(values) > 1

    def test_rejects_min_ge_max(self) -> None:
        with pytest.raises(ValueError):
            RandomGen(min=10, max=5, distribution=Distribution.UNIFORM, update_ms=100)

    def test_rejects_zero_update_ms(self) -> None:
        with pytest.raises(ValueError):
            RandomGen(min=0, max=10, distribution=Distribution.UNIFORM, update_ms=0)


class TestScript:
    def test_evaluates(self) -> None:
        g = Script.from_source("prev + 1")
        assert g.sample(0.0, 5) == 6

    def test_uses_math(self) -> None:
        g = Script.from_source("math.sin(t * math.pi / 2)")
        assert g.sample(1.0, 0) == pytest.approx(1.0)

    def test_compile_error_surfaces(self) -> None:
        from modbus_simulator.core.script_sandbox import ScriptCompileError

        with pytest.raises(ScriptCompileError):
            Script.from_source("import os")


class TestToggle:
    def test_period_100ms(self) -> None:
        g = Toggle(period_ms=100)
        # Period of 100 ms = 50 ms half-cycle.
        assert g.sample(0.00, 0) == 0
        assert g.sample(0.05, 0) == 1
        assert g.sample(0.10, 0) == 0
        assert g.sample(0.15, 0) == 1

    def test_rejects_zero_period(self) -> None:
        with pytest.raises(ValueError):
            Toggle(period_ms=0)


class TestPattern:
    def test_cycles_through_bits(self) -> None:
        g = Pattern(bits=(1, 0, 1, 1, 0), shift_ms=100)
        assert g.sample(0.00, 0) == 1
        assert g.sample(0.10, 0) == 0
        assert g.sample(0.20, 0) == 1
        assert g.sample(0.30, 0) == 1
        assert g.sample(0.40, 0) == 0
        # wraps
        assert g.sample(0.50, 0) == 1

    def test_rejects_empty_pattern(self) -> None:
        with pytest.raises(ValueError):
            Pattern(bits=(), shift_ms=100)

    def test_rejects_non_bit_value(self) -> None:
        with pytest.raises(ValueError):
            Pattern(bits=(1, 2, 0), shift_ms=100)

    def test_rejects_zero_shift(self) -> None:
        with pytest.raises(ValueError):
            Pattern(bits=(1, 0), shift_ms=0)


class TestProtocolCompliance:
    @pytest.mark.parametrize(
        "generator",
        [
            Constant(value=1),
            Ramp(min=0, max=10, step=1, period_ms=100, direction=RampDirection.UP),
            Sine(amplitude=1, offset=0, frequency_hz=1, phase_deg=0),
            RandomGen(min=0, max=1, distribution=Distribution.UNIFORM, update_ms=100),
            Script.from_source("1"),
            Toggle(period_ms=100),
            Pattern(bits=(1, 0), shift_ms=100),
        ],
    )
    def test_sample_returns_number(self, generator: object) -> None:
        assert hasattr(generator, "sample")
        v = generator.sample(0.1, 0)  # type: ignore[attr-defined]
        assert isinstance(v, (int, float)) and not math.isnan(float(v))
