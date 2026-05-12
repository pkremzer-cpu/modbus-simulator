"""Combinatorial cross-validation.

* Every :class:`RuleAction` that produces an exception -> the right code
  reaches the client, through the real wire.
* Every DataType x ByteOrder x WordOrder combination round-trips through the
  server (write via FC 16, read back via FC 03, decode through
  :mod:`modbus_simulator.core.codec`).
* Polling → Trend signal wiring (headless, without the full GUI).
"""

from __future__ import annotations

import asyncio
import math

import pytest

from modbus_simulator.core.client import Client, ModbusExceptionError
from modbus_simulator.core.codec import ByteOrder, DataType, WordOrder, decode, encode
from modbus_simulator.core.exceptions import (
    ACTION_TO_CODE,
    ExceptionRule,
    RuleAction,
)

from .conftest import Harness

pytestmark = [pytest.mark.integration]  # asyncio mode=auto picks up async defs


# ---------------------------------------------------------------------------
# Every exception-producing RuleAction round-trips with the right code.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action", [a for a in RuleAction if a is not RuleAction.DROP])
async def test_every_rule_action_delivers_correct_code(
    harness: Harness, action: RuleAction
) -> None:
    harness.rules.clear()
    harness.rules.add_rule(
        ExceptionRule(
            name=action.value,
            function_codes=frozenset({3}),
            unit_ids=frozenset(),
            address_start=0,
            address_end=10,
            action=action,
        )
    )
    with pytest.raises(ModbusExceptionError) as info:
        await harness.client.read_holding_registers(0, 1)
    assert info.value.code == ACTION_TO_CODE[action]


# ---------------------------------------------------------------------------
# DataType x ByteOrder x WordOrder — round-trip through the wire.
# ---------------------------------------------------------------------------
_DATATYPE_SAMPLES: dict[DataType, int | float] = {
    DataType.INT16: -12345,
    DataType.UINT16: 54321,
    DataType.INT32: -0x1234_5678,
    DataType.UINT32: 0xDEAD_BEEF,
    DataType.FLOAT32: -math.pi,
    DataType.FLOAT64: math.e,
}


@pytest.mark.parametrize("dtype", list(DataType))
@pytest.mark.parametrize("byte_order", list(ByteOrder))
@pytest.mark.parametrize("word_order", list(WordOrder))
async def test_codec_roundtrip_through_wire(
    harness: Harness,
    dtype: DataType,
    byte_order: ByteOrder,
    word_order: WordOrder,
) -> None:
    """Encode → write over FC 16 → read back over FC 03 → decode → compare."""
    value = _DATATYPE_SAMPLES[dtype]
    registers = list(encode(value, dtype, byte_order=byte_order, word_order=word_order))

    await harness.client.write_registers(0, registers)
    read_back = await harness.client.read_holding_registers(0, len(registers))
    assert read_back == registers

    decoded = decode(read_back, dtype, byte_order=byte_order, word_order=word_order)
    if dtype is DataType.FLOAT32:
        assert math.isclose(float(decoded), float(value), rel_tol=1e-6, abs_tol=1e-30)
    else:
        assert decoded == value


# ---------------------------------------------------------------------------
# Polling → Trend wiring — verify the signal actually delivers samples.
# ---------------------------------------------------------------------------
class TestPollingTrendWiring:
    def test_trend_tab_records_polling_sample(self) -> None:
        """Without launching the full app, prove the ``on_polling_sample``
        pathway records a sample into the trend buffer."""
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        from modbus_simulator.core.datastore import DataStore
        from modbus_simulator.gui.trend_tab import TrendTab

        app = QApplication.instance() or QApplication([])
        ds = DataStore()
        trend = TrendTab(ds)
        try:
            # Must add the channel first — otherwise samples are ignored (by design).
            trend._add_curve("poll:probe", "poll:probe")
            trend.on_polling_sample("probe", 123.0)
            trend.on_polling_sample("probe", 456.0)
            buffer = trend._samples["poll:probe"]
            assert len(buffer) == 2
            assert [v for _, v in buffer] == [123.0, 456.0]
        finally:
            trend.deleteLater()
        _ = app  # keep reference alive


# ---------------------------------------------------------------------------
# Client signal-side: ensure polling_sample is emitted with a scalar value.
# ---------------------------------------------------------------------------
async def test_client_tab_polling_emits_sample_signal(harness: Harness) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    from modbus_simulator.config.schema import SessionConfig
    from modbus_simulator.gui.client_tab import ClientTab

    app = QApplication.instance() or QApplication([])
    harness.datastore.holding_registers.set(0, [777])
    session = SessionConfig()
    session.client.host = "127.0.0.1"
    session.client.port = harness.server.bound_port
    session.client.unit_id = 1

    tab = ClientTab(session, harness.traffic)
    try:
        received: list[tuple[str, float]] = []
        tab.polling_sample.connect(lambda n, v: received.append((n, v)))

        # Connect and run a single poll synchronously.
        await tab._connect_task()
        assert tab._client is not None

        from modbus_simulator.config.schema import PollingEntry

        await tab._poll_one(
            0,
            PollingEntry(
                name="probe",
                function_code=3,
                address=0,
                count=1,
                enabled=True,
            ),
        )
        await tab._disconnect_task()
    finally:
        tab.deleteLater()
    _ = app

    assert received, "polling_sample never fired"
    assert received[-1] == ("probe", 777.0)


# ---------------------------------------------------------------------------
# Client raw-hex logging: the synthesised MBAP+PDU is parsable back.
# ---------------------------------------------------------------------------
async def test_client_raw_hex_is_parseable(harness: Harness) -> None:
    import struct

    from modbus_simulator.core.traffic import Direction, TrafficLog

    traffic = TrafficLog(max_entries=50)
    client = Client(
        host="127.0.0.1",
        port=harness.server.bound_port,
        unit_id=1,
        timeout=2.0,
        traffic_log=traffic,
    )
    await client.connect()
    try:
        await client.read_holding_registers(7, 3)
    finally:
        await client.disconnect()

    tx_entries = [e for e in traffic.snapshot() if e.direction is Direction.TX]
    assert tx_entries
    raw = bytes.fromhex(tx_entries[-1].raw_hex)
    assert len(raw) >= 12  # MBAP (7) + FC (1) + addr (2) + count (2)
    _, proto, length, unit_id, fc, addr, count = struct.unpack(">HHHBBHH", raw)
    assert proto == 0
    assert length == 6
    assert unit_id == 1
    assert fc == 0x03
    assert addr == 7
    assert count == 3


# ---------------------------------------------------------------------------
# Traffic-log overflow — server-side alone.
# ---------------------------------------------------------------------------
async def test_enron_mode_doubles_count(harness: Harness) -> None:
    """Enron 32-bit register mode: caller asks for N regs, wire sees 2N."""
    from modbus_simulator.core.client import Client

    # Seed 8 16-bit regs that pair up to 4 32-bit values
    harness.datastore.holding_registers.set(0, [0x1234, 0x5678, 0xAAAA, 0xBBBB, 0, 1, 2, 3])
    client = Client(
        host="127.0.0.1",
        port=harness.server.bound_port,
        unit_id=1,
        timeout=2.0,
        enron_mode=True,
    )
    await client.connect()
    try:
        # Ask for 4 Enron regs (= 8 wire regs)
        regs = await client.read_holding_registers(0, 4)
        assert len(regs) == 8
        assert regs == [0x1234, 0x5678, 0xAAAA, 0xBBBB, 0, 1, 2, 3]
    finally:
        await client.disconnect()


class TestEnronModeDeep:
    """Cross-validation: Enron mode read against the server, with the same
    block readable in standard mode for comparison."""

    async def test_enron_input_registers_doubled(self, harness: Harness) -> None:
        from modbus_simulator.core.client import Client

        harness.datastore.input_registers.set(0, [10, 20, 30, 40])
        client = Client(
            host="127.0.0.1",
            port=harness.server.bound_port,
            unit_id=1,
            timeout=2.0,
            enron_mode=True,
        )
        await client.connect()
        try:
            regs = await client.read_input_registers(0, 2)  # 2 Enron = 4 wire
            assert regs == [10, 20, 30, 40]
        finally:
            await client.disconnect()

    async def test_enron_mode_exception_propagates(self, harness: Harness) -> None:
        from modbus_simulator.core.client import Client, ModbusExceptionError

        client = Client(
            host="127.0.0.1",
            port=harness.server.bound_port,
            unit_id=1,
            timeout=2.0,
            enron_mode=True,
        )
        await client.connect()
        try:
            # Request 60 Enron regs = 120 wire regs (under FC 03 limit 125, valid)
            # But address 100 with count 120 → out of 200-reg block → 0x02
            with pytest.raises(ModbusExceptionError) as info:
                await client.read_holding_registers(100, 60)
            assert info.value.code == 0x02
        finally:
            await client.disconnect()

    async def test_enron_mode_off_by_default_keeps_count(self, harness: Harness) -> None:
        """Sanity: enron_mode=False (default) leaves wire count unchanged."""
        from modbus_simulator.core.client import Client

        harness.datastore.holding_registers.set(0, [100, 200, 300])
        client = Client(
            host="127.0.0.1",
            port=harness.server.bound_port,
            unit_id=1,
            timeout=2.0,
            enron_mode=False,
        )
        await client.connect()
        try:
            regs = await client.read_holding_registers(0, 3)
            assert regs == [100, 200, 300]
        finally:
            await client.disconnect()


async def test_traffic_log_evicts_after_max(harness: Harness) -> None:
    max_entries = harness.traffic.max_entries
    harness.traffic.clear()
    await asyncio.gather(
        *(harness.client.read_holding_registers(0, 1) for _ in range(min(max_entries, 200)))
    )
    # Size is capped
    assert harness.traffic.size <= max_entries


# ---------------------------------------------------------------------------
# Server multi-FC parametrised.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("fc", "block_attr", "block_size"),
    [
        (3, "holding_registers", 125),
        (4, "input_registers", 125),
    ],
)
async def test_read_at_limit_succeeds(
    harness: Harness, fc: int, block_attr: str, block_size: int
) -> None:
    """Reads exactly at the spec limit (125 regs) must succeed."""
    block = getattr(harness.datastore, block_attr)
    block.set(0, list(range(block_size)))
    if fc == 3:
        result = await harness.client.read_holding_registers(0, block_size)
    else:
        result = await harness.client.read_input_registers(0, block_size)
    assert result == list(range(block_size))
