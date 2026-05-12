"""Integration tests — loopback TCP, real wire format, all supported FCs."""

from __future__ import annotations

import asyncio

import pytest

from modbus_simulator.core.client import Client, ClientError, ModbusExceptionError
from modbus_simulator.core.exceptions import ExceptionRule, RuleAction
from modbus_simulator.core.traffic import Direction

from .conftest import Harness

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
async def test_server_start_stop_clean(harness: Harness) -> None:
    assert harness.server.is_running
    assert harness.client.is_connected


# ---------------------------------------------------------------------------
# FC 03 / 06 / 16 — holding registers
# ---------------------------------------------------------------------------
async def test_fc03_read_holding_registers(harness: Harness) -> None:
    harness.datastore.holding_registers.set(10, [100, 200, 300, 400, 500])
    values = await harness.client.read_holding_registers(10, 5)
    assert values == [100, 200, 300, 400, 500]


async def test_fc06_write_single_register(harness: Harness) -> None:
    await harness.client.write_register(5, 0xBEEF)
    assert harness.datastore.holding_registers.get(5) == (0xBEEF,)


async def test_fc16_write_multiple_registers(harness: Harness) -> None:
    await harness.client.write_registers(20, [1, 2, 3, 4, 5])
    assert harness.datastore.holding_registers.get(20, 5) == (1, 2, 3, 4, 5)


async def test_fc03_roundtrip_via_fc16(harness: Harness) -> None:
    payload = list(range(50, 100))
    await harness.client.write_registers(50, payload)
    assert await harness.client.read_holding_registers(50, 50) == payload


# ---------------------------------------------------------------------------
# FC 04 — input registers (read-only from client POV)
# ---------------------------------------------------------------------------
async def test_fc04_read_input_registers(harness: Harness) -> None:
    harness.datastore.input_registers.set(0, [1000, 2000, 3000])
    values = await harness.client.read_input_registers(0, 3)
    assert values == [1000, 2000, 3000]


# ---------------------------------------------------------------------------
# FC 01 / 05 / 15 — coils
# ---------------------------------------------------------------------------
async def test_fc01_read_coils(harness: Harness) -> None:
    harness.datastore.coils.set(0, [1, 0, 1, 1, 0, 0, 1, 1, 1, 0])
    values = await harness.client.read_coils(0, 10)
    assert values == [1, 0, 1, 1, 0, 0, 1, 1, 1, 0]


async def test_fc05_write_single_coil(harness: Harness) -> None:
    await harness.client.write_coil(3, True)
    assert harness.datastore.coils.get(3) == (1,)
    await harness.client.write_coil(3, False)
    assert harness.datastore.coils.get(3) == (0,)


async def test_fc15_write_multiple_coils(harness: Harness) -> None:
    await harness.client.write_coils(10, [True, False, True, True, False, True, False, True])
    assert harness.datastore.coils.get(10, 8) == (1, 0, 1, 1, 0, 1, 0, 1)


# ---------------------------------------------------------------------------
# FC 02 — discrete inputs
# ---------------------------------------------------------------------------
async def test_fc02_read_discrete_inputs(harness: Harness) -> None:
    harness.datastore.discrete_inputs.set(5, [1, 1, 0, 1])
    values = await harness.client.read_discrete_inputs(5, 4)
    assert values == [1, 1, 0, 1]


# ---------------------------------------------------------------------------
# FC 22 — mask write register
# ---------------------------------------------------------------------------
async def test_fc22_mask_write(harness: Harness) -> None:
    harness.datastore.holding_registers.set(0, [0b1010_1010_1010_1010])
    # result = (current & AND) | (OR & ~AND)
    await harness.client.mask_write_register(address=0, and_mask=0xFF00, or_mask=0x00FF)
    assert harness.datastore.holding_registers.get(0) == (0xAAFF,)


# ---------------------------------------------------------------------------
# FC 23 — read/write multiple in one transaction
# ---------------------------------------------------------------------------
async def test_fc23_readwrite_multiple(harness: Harness) -> None:
    harness.datastore.holding_registers.set(0, [111, 222, 333, 444, 555])
    result = await harness.client.readwrite_registers(
        read_address=0,
        read_count=5,
        write_address=100,
        write_values=[10, 20, 30],
    )
    # Writes happen first, then reads — but we read from 0..4, which is unaffected.
    assert result == [111, 222, 333, 444, 555]
    assert harness.datastore.holding_registers.get(100, 3) == (10, 20, 30)


async def test_fc23_read_of_freshly_written_range(harness: Harness) -> None:
    result = await harness.client.readwrite_registers(
        read_address=50,
        read_count=4,
        write_address=50,
        write_values=[7, 8, 9, 10],
    )
    # Spec: write first, then read — we read the freshly written values.
    assert result == [7, 8, 9, 10]


# ---------------------------------------------------------------------------
# Exception conditions
# ---------------------------------------------------------------------------
async def test_illegal_function_code(harness: Harness) -> None:
    # Use an unassigned FC (0x42) by sending a raw MBAP frame.
    #   tx_id=0x0001  proto=0  length=2 (unit+fc)  unit=1  fc=0x42
    reader, writer = await asyncio.open_connection("127.0.0.1", harness.server.bound_port)
    try:
        writer.write(bytes.fromhex("00010000000201") + bytes([0x42]))
        await writer.drain()
        resp = await reader.readexactly(9)
    finally:
        writer.close()
        await writer.wait_closed()
    # response PDU starts at byte 7: [fc|0x80][exception_code]
    assert resp[7] == 0x42 | 0x80
    assert resp[8] == 0x01  # Illegal Function


async def test_illegal_data_address_on_oob_read(harness: Harness) -> None:
    # Datastore size is 200; reading beyond => Illegal Data Address (0x02)
    with pytest.raises(ModbusExceptionError) as exc_info:
        await harness.client.read_holding_registers(195, 10)
    assert exc_info.value.code == 0x02


async def test_illegal_data_value_on_oversize_read(harness: Harness) -> None:
    # FC 03 spec limit is 125 registers. The pymodbus client clamps count client-side,
    # so we have to send the oversized request over a raw socket to exercise the server.
    #   tx=1  proto=0  length=6  unit=1  fc=3  addr=0  count=200
    reader, writer = await asyncio.open_connection("127.0.0.1", harness.server.bound_port)
    try:
        writer.write(bytes.fromhex("00010000000601030000") + bytes.fromhex("00C8"))
        await writer.drain()
        resp = await reader.readexactly(9)
    finally:
        writer.close()
        await writer.wait_closed()
    assert resp[7] == 0x03 | 0x80  # exception on FC 3
    assert resp[8] == 0x03  # Illegal Data Value


# ---------------------------------------------------------------------------
# Exception injection rule engine
# ---------------------------------------------------------------------------
async def test_injection_slave_busy(harness: Harness) -> None:
    harness.rules.add_rule(
        ExceptionRule(
            name="busy",
            function_codes=frozenset({3}),
            unit_ids=frozenset(),
            address_start=0,
            address_end=100,
            action=RuleAction.SLAVE_BUSY,
        )
    )
    with pytest.raises(ModbusExceptionError) as exc_info:
        await harness.client.read_holding_registers(0, 1)
    assert exc_info.value.code == 0x06


async def test_injection_drop_causes_timeout(fast_timeout_harness: Harness) -> None:
    fast_timeout_harness.rules.add_rule(
        ExceptionRule(
            name="drop",
            function_codes=frozenset({3}),
            unit_ids=frozenset(),
            address_start=0,
            address_end=100,
            action=RuleAction.DROP,
        )
    )
    with pytest.raises(ClientError):
        await fast_timeout_harness.client.read_holding_registers(0, 1)


async def test_injection_delay_is_observable(harness: Harness) -> None:
    harness.rules.add_rule(
        ExceptionRule(
            name="slow",
            function_codes=frozenset({3}),
            unit_ids=frozenset(),
            address_start=0,
            address_end=100,
            action=RuleAction.SLAVE_DEVICE_FAILURE,
            delay_ms=200,
        )
    )
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(ModbusExceptionError):
        await harness.client.read_holding_registers(0, 1)
    elapsed = loop.time() - start
    assert elapsed >= 0.15  # allow some scheduler jitter


# ---------------------------------------------------------------------------
# Traffic log
# ---------------------------------------------------------------------------
async def test_traffic_log_captures_rx_and_tx(harness: Harness) -> None:
    harness.traffic.clear()
    harness.datastore.holding_registers.set(0, [42])
    await harness.client.read_holding_registers(0, 1)
    entries = harness.traffic.snapshot()
    directions = [e.direction for e in entries]
    assert Direction.RX in directions
    assert Direction.TX in directions
    assert any(e.function_code == 3 for e in entries)


async def test_traffic_log_records_exception(harness: Harness) -> None:
    harness.traffic.clear()
    with pytest.raises(ModbusExceptionError):
        await harness.client.read_holding_registers(195, 10)
    entries = harness.traffic.snapshot()
    # At least one TX entry should carry an exception code
    assert any(e.exception_code == 0x02 for e in entries if e.direction == Direction.TX)


# ---------------------------------------------------------------------------
# Multi-client
# ---------------------------------------------------------------------------
async def test_two_clients_interleave(harness: Harness) -> None:
    harness.datastore.holding_registers.set(0, list(range(100)))
    second = Client(host="127.0.0.1", port=harness.server.bound_port, unit_id=1, timeout=2.0)
    await second.connect()
    try:
        a_task = asyncio.create_task(harness.client.read_holding_registers(0, 50))
        b_task = asyncio.create_task(second.read_holding_registers(50, 50))
        a_result, b_result = await asyncio.gather(a_task, b_task)
        assert a_result == list(range(0, 50))
        assert b_result == list(range(50, 100))
    finally:
        await second.disconnect()
