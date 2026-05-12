"""Custom asyncio Modbus TCP server.

Why not pymodbus on the server side? The simulator needs:

* Raw-hex request/response logging (not just decoded fields).
* Exception injection covering every exception code plus drop and delay.
* Per-request hooks so the :class:`RuleEngine` and :class:`TrafficLog` run
  before the response is emitted.

pymodbus does not expose these hooks cleanly, so we implement the MBAP framing
and PDU dispatch ourselves. The protocol surface we need is small:

* MBAP header: ``[tx_id:2][proto=0:2][length:2][unit_id:1]``
* PDU: ``[function_code:1][data]``
* Exception response: ``[fc|0x80:1][exc_code:1]``

Supported function codes: 1, 2, 3, 4, 5, 6, 8, 15, 16, 22, 23, 24.
Diagnostic (FC 08) sub-functions: 00, 01, 04, 0A, 0B, 0C, 0D, 0E.
FC 24 (Read FIFO Queue) returns the last 31 holding registers from the
supplied pointer address, capped to 31 entries — the simulator has no
dedicated FIFO buffer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import Callable
from datetime import UTC, datetime

from modbus_simulator.core.datastore import DataStore
from modbus_simulator.core.exceptions import RuleEngine
from modbus_simulator.core.traffic import Direction, TrafficEntry, TrafficLog

log = logging.getLogger(__name__)

# Exception codes
EXC_ILLEGAL_FUNCTION = 0x01
EXC_ILLEGAL_DATA_ADDRESS = 0x02
EXC_ILLEGAL_DATA_VALUE = 0x03
EXC_SLAVE_DEVICE_FAILURE = 0x04

# Spec limits
MAX_READ_BITS = 2000
MAX_READ_REGS = 125
MAX_WRITE_COILS = 1968
MAX_WRITE_REGS = 123
MAX_RW_READ_REGS = 125
MAX_RW_WRITE_REGS = 121
MAX_FIFO_REGS = 31


class ModbusError(Exception):
    """Raised by a handler to emit a Modbus exception response."""

    def __init__(self, code: int) -> None:
        super().__init__(f"Modbus exception 0x{code:02X}")
        self.code = code


class Server:
    def __init__(
        self,
        *,
        host: str = "0.0.0.0",  # noqa: S104 — operator-chosen bind address
        port: int = 5020,
        unit_id: int = 1,
        datastore: DataStore | None = None,
        rule_engine: RuleEngine | None = None,
        traffic_log: TrafficLog | None = None,
    ) -> None:
        if not 1 <= unit_id <= 247:
            raise ValueError(f"unit_id must be 1..247, got {unit_id}")
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.datastore = datastore or DataStore()
        self.rule_engine = rule_engine or RuleEngine()
        self.traffic_log = traffic_log or TrafficLog()
        self._server: asyncio.base_events.Server | None = None
        self._counters = _DiagnosticCounters()

    # ----- lifecycle -----
    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("server already started")
        self._server = await asyncio.start_server(self._client_handler, self.host, self.port)
        log.info("modbus server listening on %s:%d unit=%d", self.host, self.port, self.unit_id)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        log.info("modbus server stopped")

    @property
    def is_running(self) -> bool:
        return self._server is not None

    @property
    def bound_port(self) -> int:
        """Actual port the OS assigned (useful when ``port=0``)."""
        if self._server is None or not self._server.sockets:
            return self.port
        return int(self._server.sockets[0].getsockname()[1])

    # ----- connection loop -----
    async def _client_handler(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer_info = writer.get_extra_info("peername")
        peer = f"{peer_info[0]}:{peer_info[1]}" if peer_info else "?"
        try:
            while True:
                try:
                    mbap = await reader.readexactly(7)
                except asyncio.IncompleteReadError:
                    return
                tx_id, proto, length, unit_id = struct.unpack(">HHHB", mbap)
                if proto != 0 or length < 2 or length > 260:
                    log.warning("invalid MBAP from %s: proto=%d length=%d", peer, proto, length)
                    return
                try:
                    pdu = await reader.readexactly(length - 1)
                except asyncio.IncompleteReadError:
                    return
                self._counters.bus_messages += 1
                response_pdu = await self._process_pdu(unit_id, pdu, peer)
                if response_pdu is None:
                    continue  # drop semantics — no response sent
                out = struct.pack(">HHHB", tx_id, 0, 1 + len(response_pdu), unit_id) + response_pdu
                writer.write(out)
                await writer.drain()
                self._log(Direction.TX, peer, unit_id, response_pdu)
        except asyncio.CancelledError:
            raise
        except ConnectionResetError:
            pass
        except Exception:
            log.exception("unexpected error in client handler for %s", peer)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    # ----- request processing -----
    async def _process_pdu(self, unit_id: int, pdu: bytes, peer: str) -> bytes | None:
        self._log(Direction.RX, peer, unit_id, pdu)

        if not pdu:
            return None
        fc = pdu[0]
        data = pdu[1:]

        # Unit ID filter — a TCP server typically has one configured unit; requests
        # for other units are ignored (no response).
        if unit_id != self.unit_id and unit_id != 0:
            return None
        self._counters.slave_messages += 1

        # Parse address/count for rule evaluation; broad fallback if not applicable.
        addr, count = _extract_addr_count(fc, data)
        match = self.rule_engine.evaluate(
            function_code=fc, unit_id=unit_id, address=addr, count=count
        )
        if match is not None:
            if match.delay_ms > 0:
                await asyncio.sleep(match.delay_ms / 1000.0)
            if match.is_drop:
                return None
            assert match.exception_code is not None
            self._counters.exception_responses += 1
            return _exception_pdu(fc, match.exception_code)

        handler = _HANDLERS.get(fc)
        if handler is None:
            self._counters.exception_responses += 1
            return _exception_pdu(fc, EXC_ILLEGAL_FUNCTION)

        try:
            response_data = handler(self, data)
        except ModbusError as err:
            self._counters.exception_responses += 1
            return _exception_pdu(fc, err.code)
        except Exception:
            log.exception("handler FC=0x%02X raised", fc)
            self._counters.exception_responses += 1
            return _exception_pdu(fc, EXC_SLAVE_DEVICE_FAILURE)

        return bytes([fc]) + response_data

    # ----- logging -----
    def _log(self, direction: Direction, peer: str, unit_id: int, pdu: bytes) -> None:
        fc = pdu[0] if pdu else 0
        exception_code: int | None = None
        if pdu and fc & 0x80 and len(pdu) >= 2:
            exception_code = pdu[1]
            fc &= 0x7F
        addr, count = _extract_addr_count(fc, pdu[1:]) if pdu else (0, 0)
        entry = TrafficEntry(
            timestamp=datetime.now(UTC),
            direction=direction,
            peer=peer,
            unit_id=unit_id,
            function_code=fc,
            exception_code=exception_code,
            address=addr if addr else None,
            count=count if count else None,
            values=(),
            raw_hex=pdu.hex(),
        )
        self.traffic_log.append(entry)


# ---------------------------------------------------------------------------
# PDU handlers — each returns the response data bytes (the FC byte is prepended
# by the caller). They raise :class:`ModbusError` to emit an exception response.
# ---------------------------------------------------------------------------
def _handle_read_coils(server: Server, data: bytes) -> bytes:
    if len(data) != 4:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    addr, count = struct.unpack(">HH", data)
    if not 1 <= count <= MAX_READ_BITS:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    try:
        bits = server.datastore.coils.get(addr, count)
    except IndexError as err:
        raise ModbusError(EXC_ILLEGAL_DATA_ADDRESS) from err
    return _pack_bits(bits)


def _handle_read_discrete_inputs(server: Server, data: bytes) -> bytes:
    if len(data) != 4:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    addr, count = struct.unpack(">HH", data)
    if not 1 <= count <= MAX_READ_BITS:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    try:
        bits = server.datastore.discrete_inputs.get(addr, count)
    except IndexError as err:
        raise ModbusError(EXC_ILLEGAL_DATA_ADDRESS) from err
    return _pack_bits(bits)


def _handle_read_holding_registers(server: Server, data: bytes) -> bytes:
    if len(data) != 4:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    addr, count = struct.unpack(">HH", data)
    if not 1 <= count <= MAX_READ_REGS:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    try:
        regs = server.datastore.holding_registers.get(addr, count)
    except IndexError as err:
        raise ModbusError(EXC_ILLEGAL_DATA_ADDRESS) from err
    return _pack_registers(regs)


def _handle_read_input_registers(server: Server, data: bytes) -> bytes:
    if len(data) != 4:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    addr, count = struct.unpack(">HH", data)
    if not 1 <= count <= MAX_READ_REGS:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    try:
        regs = server.datastore.input_registers.get(addr, count)
    except IndexError as err:
        raise ModbusError(EXC_ILLEGAL_DATA_ADDRESS) from err
    return _pack_registers(regs)


def _handle_write_single_coil(server: Server, data: bytes) -> bytes:
    if len(data) != 4:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    addr, raw = struct.unpack(">HH", data)
    if raw not in (0x0000, 0xFF00):
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    try:
        server.datastore.coils.set(addr, [1 if raw == 0xFF00 else 0])
    except IndexError as err:
        raise ModbusError(EXC_ILLEGAL_DATA_ADDRESS) from err
    return data  # echo


def _handle_write_single_register(server: Server, data: bytes) -> bytes:
    if len(data) != 4:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    addr, value = struct.unpack(">HH", data)
    try:
        server.datastore.holding_registers.set(addr, [value])
    except IndexError as err:
        raise ModbusError(EXC_ILLEGAL_DATA_ADDRESS) from err
    return data  # echo


def _handle_diagnostic(server: Server, data: bytes) -> bytes:
    if len(data) < 2:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    sub_fc = struct.unpack(">H", data[0:2])[0]
    payload = data[2:]
    counters = server._counters
    if sub_fc == 0x0000:  # Return Query Data
        return data
    if sub_fc == 0x0001:  # Restart Communications Option
        counters.reset()
        return data
    if sub_fc == 0x0004:  # Force Listen-Only Mode (spec: no response)
        return data  # simulator still answers for test-friendliness
    if sub_fc == 0x000A:  # Clear Counters and Diagnostic Register
        counters.reset()
        return data
    if sub_fc == 0x000B:  # Return Bus Message Count
        return struct.pack(">HH", sub_fc, counters.bus_messages & 0xFFFF)
    if sub_fc == 0x000C:  # Return Bus Communication Error Count
        return struct.pack(">HH", sub_fc, counters.comm_errors & 0xFFFF)
    if sub_fc == 0x000D:  # Return Bus Exception Error Count
        return struct.pack(">HH", sub_fc, counters.exception_responses & 0xFFFF)
    if sub_fc == 0x000E:  # Return Slave Message Count
        return struct.pack(">HH", sub_fc, counters.slave_messages & 0xFFFF)
    # unknown sub-function code
    _ = payload
    raise ModbusError(EXC_ILLEGAL_DATA_VALUE)


def _handle_write_multiple_coils(server: Server, data: bytes) -> bytes:
    if len(data) < 5:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    addr, count, byte_count = struct.unpack(">HHB", data[:5])
    if not 1 <= count <= MAX_WRITE_COILS:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    if byte_count != (count + 7) // 8 or len(data) != 5 + byte_count:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    bits = _unpack_bits(data[5:], count)
    try:
        server.datastore.coils.set(addr, bits)
    except IndexError as err:
        raise ModbusError(EXC_ILLEGAL_DATA_ADDRESS) from err
    return struct.pack(">HH", addr, count)


def _handle_write_multiple_registers(server: Server, data: bytes) -> bytes:
    if len(data) < 5:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    addr, count, byte_count = struct.unpack(">HHB", data[:5])
    if not 1 <= count <= MAX_WRITE_REGS:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    if byte_count != count * 2 or len(data) != 5 + byte_count:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    regs = list(struct.unpack(">" + "H" * count, data[5 : 5 + byte_count]))
    try:
        server.datastore.holding_registers.set(addr, regs)
    except IndexError as err:
        raise ModbusError(EXC_ILLEGAL_DATA_ADDRESS) from err
    return struct.pack(">HH", addr, count)


def _handle_mask_write_register(server: Server, data: bytes) -> bytes:
    if len(data) != 6:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    addr, and_mask, or_mask = struct.unpack(">HHH", data)
    try:
        current = server.datastore.holding_registers.get(addr)[0]
    except IndexError as err:
        raise ModbusError(EXC_ILLEGAL_DATA_ADDRESS) from err
    new_value = (current & and_mask) | (or_mask & (~and_mask & 0xFFFF))
    server.datastore.holding_registers.set(addr, [new_value & 0xFFFF])
    return data  # echo


def _handle_read_write_multiple(server: Server, data: bytes) -> bytes:
    if len(data) < 9:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    read_addr, read_count, write_addr, write_count, write_bytes = struct.unpack(">HHHHB", data[:9])
    if not 1 <= read_count <= MAX_RW_READ_REGS:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    if not 1 <= write_count <= MAX_RW_WRITE_REGS:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    if write_bytes != write_count * 2 or len(data) != 9 + write_bytes:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    write_regs = list(struct.unpack(">" + "H" * write_count, data[9 : 9 + write_bytes]))
    # Spec: write happens first, then read.
    try:
        server.datastore.holding_registers.set(write_addr, write_regs)
        read_regs = server.datastore.holding_registers.get(read_addr, read_count)
    except IndexError as err:
        raise ModbusError(EXC_ILLEGAL_DATA_ADDRESS) from err
    return _pack_registers(read_regs)


def _handle_read_fifo_queue(server: Server, data: bytes) -> bytes:
    """Simplified FIFO: reads the next 31 holding registers at ``addr``."""
    if len(data) != 2:
        raise ModbusError(EXC_ILLEGAL_DATA_VALUE)
    addr = struct.unpack(">H", data)[0]
    remaining = max(0, server.datastore.holding_registers.size - addr)
    fifo_count = min(MAX_FIFO_REGS, remaining)
    try:
        regs = server.datastore.holding_registers.get(addr, fifo_count) if fifo_count > 0 else ()
    except IndexError as err:
        raise ModbusError(EXC_ILLEGAL_DATA_ADDRESS) from err
    byte_count = 2 + fifo_count * 2  # fifo_count field + registers
    return struct.pack(">HH", byte_count, fifo_count) + _pack_registers_raw(regs)


HandlerFn = Callable[["Server", bytes], bytes]

_HANDLERS: dict[int, HandlerFn] = {
    0x01: _handle_read_coils,
    0x02: _handle_read_discrete_inputs,
    0x03: _handle_read_holding_registers,
    0x04: _handle_read_input_registers,
    0x05: _handle_write_single_coil,
    0x06: _handle_write_single_register,
    0x08: _handle_diagnostic,
    0x0F: _handle_write_multiple_coils,
    0x10: _handle_write_multiple_registers,
    0x16: _handle_mask_write_register,
    0x17: _handle_read_write_multiple,
    0x18: _handle_read_fifo_queue,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _DiagnosticCounters:
    def __init__(self) -> None:
        self.bus_messages = 0
        self.slave_messages = 0
        self.exception_responses = 0
        self.comm_errors = 0  # always 0 on TCP; kept for diagnostic completeness

    def reset(self) -> None:
        self.bus_messages = 0
        self.slave_messages = 0
        self.exception_responses = 0
        self.comm_errors = 0


def _exception_pdu(fc: int, exception_code: int) -> bytes:
    return bytes([fc | 0x80, exception_code])


def _pack_bits(bits: tuple[int, ...]) -> bytes:
    byte_count = (len(bits) + 7) // 8
    buf = bytearray(byte_count)
    for i, bit in enumerate(bits):
        if bit:
            buf[i // 8] |= 1 << (i % 8)
    return bytes([byte_count]) + bytes(buf)


def _unpack_bits(packed: bytes, count: int) -> list[int]:
    out: list[int] = []
    for i in range(count):
        out.append((packed[i // 8] >> (i % 8)) & 1)
    return out


def _pack_registers(regs: tuple[int, ...]) -> bytes:
    return bytes([len(regs) * 2]) + _pack_registers_raw(regs)


def _pack_registers_raw(regs: tuple[int, ...]) -> bytes:
    return struct.pack(">" + "H" * len(regs), *regs)


def _extract_addr_count(fc: int, data: bytes) -> tuple[int, int]:
    """Best-effort extraction for rule-engine / log dispatch. Returns (0, 0) on failure."""
    try:
        if fc in (0x01, 0x02, 0x03, 0x04) and len(data) >= 4:
            addr, count = struct.unpack(">HH", data[:4])
            return addr, count
        if fc in (0x05, 0x06) and len(data) >= 2:
            return struct.unpack(">H", data[:2])[0], 1
        if fc in (0x0F, 0x10) and len(data) >= 4:
            addr, count = struct.unpack(">HH", data[:4])
            return addr, count
        if fc == 0x16 and len(data) >= 2:
            return struct.unpack(">H", data[:2])[0], 1
        if fc == 0x17 and len(data) >= 4:
            # use read_addr/read_count as the canonical target
            addr, count = struct.unpack(">HH", data[:4])
            return addr, count
        if fc == 0x18 and len(data) >= 2:
            return struct.unpack(">H", data[:2])[0], 1
    except struct.error:
        pass
    return 0, 0
