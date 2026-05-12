"""Value generators for the simulation engine.

Each generator is a small, stateless dataclass with a ``sample(t, prev)``
method. ``t`` is the elapsed time in seconds since server start (float),
``prev`` is the last register value seen.

Keeping the generators stateless makes them trivially serialisable (for
config persistence) and thread-safe. Temporal behaviour like the Random
generator's ``update_ms`` cadence is achieved by deterministically bucketing
``t`` rather than holding mutable state.

The scheduler that drives generators at the configured rate lives with the
server wrapper — this module only defines *what* each generator computes for
a given ``t``.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from modbus_simulator.core.script_sandbox import CompiledScript, compile_script

Number = int | float


@runtime_checkable
class ValueGenerator(Protocol):
    def sample(self, t: float, prev: Number) -> Number: ...


# ---------------------------------------------------------------------------
# Constant
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Constant:
    value: Number

    def sample(self, t: float, prev: Number) -> Number:
        return self.value


# ---------------------------------------------------------------------------
# Ramp
# ---------------------------------------------------------------------------
class RampDirection(StrEnum):
    UP = "up"
    DOWN = "down"
    PINGPONG = "pingpong"


@dataclass(frozen=True, slots=True)
class Ramp:
    min: Number
    max: Number
    step: Number
    period_ms: float
    direction: RampDirection

    def __post_init__(self) -> None:
        if self.min >= self.max:
            raise ValueError(f"min ({self.min}) must be less than max ({self.max})")
        if self.step <= 0:
            raise ValueError(f"step must be positive, got {self.step}")
        if self.period_ms <= 0:
            raise ValueError(f"period_ms must be positive, got {self.period_ms}")

    def sample(self, t: float, prev: Number) -> Number:
        cycle_s = self.period_ms / 1000.0
        phase = (t % cycle_s) / cycle_s  # 0..1
        span = self.max - self.min

        raw: float
        if self.direction is RampDirection.UP:
            raw = float(self.min) + phase * span
        elif self.direction is RampDirection.DOWN:
            raw = float(self.max) - phase * span
        else:  # PINGPONG — full min→max→min in one cycle
            if phase < 0.5:
                raw = float(self.min) + 2.0 * phase * span
            else:
                raw = float(self.max) - 2.0 * (phase - 0.5) * span

        # snap to step grid anchored at min, then clamp
        snapped = self.min + self.step * round((raw - self.min) / self.step)
        return max(self.min, min(self.max, snapped))


# ---------------------------------------------------------------------------
# Sine
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Sine:
    amplitude: float
    offset: float
    frequency_hz: float
    phase_deg: float

    def __post_init__(self) -> None:
        if self.frequency_hz <= 0:
            raise ValueError(f"frequency_hz must be positive, got {self.frequency_hz}")

    def sample(self, t: float, prev: Number) -> Number:
        omega = 2.0 * math.pi * self.frequency_hz
        phase_rad = math.radians(self.phase_deg)
        return self.offset + self.amplitude * math.sin(omega * t + phase_rad)


# ---------------------------------------------------------------------------
# Random
# ---------------------------------------------------------------------------
class Distribution(StrEnum):
    UNIFORM = "uniform"
    GAUSSIAN = "gaussian"


@dataclass(frozen=True, slots=True)
class RandomGen:
    min: Number
    max: Number
    distribution: Distribution
    update_ms: float
    seed: int = 0

    def __post_init__(self) -> None:
        if self.min >= self.max:
            raise ValueError(f"min ({self.min}) must be less than max ({self.max})")
        if self.update_ms <= 0:
            raise ValueError(f"update_ms must be positive, got {self.update_ms}")

    def sample(self, t: float, prev: Number) -> Number:
        bucket = int(t * 1000.0 / self.update_ms)
        rng = random.Random(self.seed * 1_000_003 + bucket)  # noqa: S311 — simulator PRNG, not crypto
        if self.distribution is Distribution.UNIFORM:
            return rng.uniform(self.min, self.max)
        mean = (self.min + self.max) / 2.0
        stddev = (self.max - self.min) / 6.0
        # truncate to [min, max] to prevent 0.3% tail outliers leaking out
        value = rng.gauss(mean, stddev)
        return max(self.min, min(self.max, value))


# ---------------------------------------------------------------------------
# Script
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Script:
    source: str
    _compiled: CompiledScript = field(repr=False)

    @classmethod
    def from_source(cls, source: str) -> Script:
        return cls(source=source, _compiled=compile_script(source))

    def sample(self, t: float, prev: Number) -> Number:
        return self._compiled.evaluate(t, prev)


# ---------------------------------------------------------------------------
# Toggle (bit)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Toggle:
    period_ms: float

    def __post_init__(self) -> None:
        if self.period_ms <= 0:
            raise ValueError(f"period_ms must be positive, got {self.period_ms}")

    def sample(self, t: float, prev: Number) -> Number:
        half_cycle_index = int(t * 2000.0 / self.period_ms)
        return half_cycle_index % 2


# ---------------------------------------------------------------------------
# Pattern (bit sequence)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Pattern:
    bits: tuple[int, ...]
    shift_ms: float

    def __post_init__(self) -> None:
        if not self.bits:
            raise ValueError("bits must be a non-empty tuple")
        for bit in self.bits:
            if bit not in (0, 1):
                raise ValueError(f"pattern entries must be 0 or 1, got {bit}")
        if self.shift_ms <= 0:
            raise ValueError(f"shift_ms must be positive, got {self.shift_ms}")

    def sample(self, t: float, prev: Number) -> Number:
        idx = int(t * 1000.0 / self.shift_ms) % len(self.bits)
        return self.bits[idx]
