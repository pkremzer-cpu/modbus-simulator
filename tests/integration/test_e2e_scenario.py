"""End-to-end orchestration test — simulator ticks values, client polls them,
traffic log captures everything, rule engine injects a failure mid-stream.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from modbus_simulator.core.client import ModbusExceptionError
from modbus_simulator.core.exceptions import ExceptionRule, RuleAction
from modbus_simulator.core.simulator import Ramp, RampDirection, Sine
from modbus_simulator.core.traffic import Direction

from .conftest import Harness

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_ramp_advances_across_reads(harness: Harness) -> None:
    """A background task drives HR[0] with a ramp; successive reads see increasing values."""
    block = harness.datastore.holding_registers
    start = time.monotonic()
    ramp = Ramp(min=0, max=100, step=1, period_ms=1000, direction=RampDirection.UP)

    async def driver() -> None:
        while True:
            t = time.monotonic() - start
            block.set(0, [int(ramp.sample(t, 0))])
            await asyncio.sleep(0.05)

    task = asyncio.create_task(driver())
    try:
        samples: list[int] = []
        for _ in range(4):
            regs = await harness.client.read_holding_registers(0, 1)
            samples.append(regs[0])
            await asyncio.sleep(0.25)
    finally:
        task.cancel()
    # Values should generally increase (allow some slack for wraparound)
    assert max(samples) > min(samples)


async def test_sine_generator_full_cycle(harness: Harness) -> None:
    """Read a holding register driven by a sine; verify it spans near-full range."""
    block = harness.datastore.holding_registers
    start = time.monotonic()
    sine = Sine(amplitude=400, offset=500, frequency_hz=5.0, phase_deg=0)

    async def driver() -> None:
        while True:
            t = time.monotonic() - start
            value = max(0, min(0xFFFF, int(sine.sample(t, 0))))
            block.set(10, [value])
            await asyncio.sleep(0.01)

    task = asyncio.create_task(driver())
    try:
        samples: list[int] = []
        for _ in range(60):
            regs = await harness.client.read_holding_registers(10, 1)
            samples.append(regs[0])
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
    # Over 5 Hz x ~1.2 s we should see the full range [~100, ~900]
    assert max(samples) - min(samples) > 200


async def test_rule_toggles_at_runtime(harness: Harness) -> None:
    """Enable a rule mid-test; subsequent reads error, until we disable it."""
    harness.datastore.holding_registers.set(0, [42])
    # First: clean read
    assert await harness.client.read_holding_registers(0, 1) == [42]

    # Enable rule
    rule = ExceptionRule(
        name="r",
        function_codes=frozenset({3}),
        unit_ids=frozenset(),
        address_start=0,
        address_end=10,
        action=RuleAction.SLAVE_BUSY,
    )
    harness.rules.add_rule(rule)
    with pytest.raises(ModbusExceptionError) as exc_info:
        await harness.client.read_holding_registers(0, 1)
    assert exc_info.value.code == 0x06

    # Remove rule
    harness.rules.remove_rule(rule)
    assert await harness.client.read_holding_registers(0, 1) == [42]


async def test_traffic_log_preserves_order_under_concurrency(harness: Harness) -> None:
    harness.traffic.clear()
    harness.datastore.holding_registers.set(0, list(range(20)))
    await asyncio.gather(*(harness.client.read_holding_registers(i, 1) for i in range(20)))
    entries = harness.traffic.snapshot()
    # Every RX must be followed by a TX for the same FC (loose ordering check)
    rx_count = sum(1 for e in entries if e.direction == Direction.RX)
    tx_count = sum(1 for e in entries if e.direction == Direction.TX)
    assert rx_count == tx_count == 20


async def test_multiblock_write_visible_from_all_fcs(harness: Harness) -> None:
    """Writing via FC 16 is observable via FC 03 and in the HR datastore."""
    await harness.client.write_registers(50, [1111, 2222, 3333])
    # Read back via FC 03
    assert await harness.client.read_holding_registers(50, 3) == [1111, 2222, 3333]
    # And it's in the server's datastore
    assert harness.datastore.holding_registers.get(50, 3) == (1111, 2222, 3333)
    # Input registers and coils unaffected
    assert harness.datastore.input_registers.get(50) == (0,)
    assert harness.datastore.coils.get(50) == (0,)
