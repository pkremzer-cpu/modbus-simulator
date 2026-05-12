"""Async Modbus TCP client — thin wrapper around pymodbus.

The GUI uses this for both the manual transaction panel and the polling tab.
It converts pymodbus' response objects into plain Python values and raises
:class:`ClientError` / :class:`ModbusExceptionError` instead of expecting the
caller to check ``isError()`` each time.

If a :class:`TrafficLog` is passed to the constructor, every request/response
is also journalled — decoded fields plus a synthesised MBAP+PDU hex string
for the Traffic tab's "Raw" column. The hex is best-effort (pymodbus picks
the transaction ID internally; ours for display is monotonic), so Wireshark
captures remain authoritative, but the log is enough to trace the flow.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from typing import Any

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.pdu import ExceptionResponse

from modbus_simulator.core.traffic import Direction, TrafficEntry, TrafficLog

_METHOD_TO_FC: dict[str, int] = {
    "read_coils": 0x01,
    "read_discrete_inputs": 0x02,
    "read_holding_registers": 0x03,
    "read_input_registers": 0x04,
    "write_coil": 0x05,
    "write_register": 0x06,
    "write_coils": 0x0F,
    "write_registers": 0x10,
    "mask_write_register": 0x16,
    "readwrite_registers": 0x17,
}


class ClientError(Exception):
    """Base class for transport- or protocol-level client failures."""


class NotConnectedError(ClientError):
    """Operation attempted before :meth:`Client.connect`."""


class ModbusExceptionError(ClientError):
    """Peer responded with a Modbus exception."""

    def __init__(self, code: int) -> None:
        super().__init__(f"Modbus exception 0x{code:02X}")
        self.code = code


class Client:
    def __init__(
        self,
        host: str,
        port: int = 5020,
        unit_id: int = 1,
        *,
        timeout: float = 3.0,
        traffic_log: TrafficLog | None = None,
        enron_mode: bool = False,
    ) -> None:
        if not 1 <= unit_id <= 247:
            raise ValueError(f"unit_id must be 1..247, got {unit_id}")
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self._client: AsyncModbusTcpClient | None = None
        self._traffic_log = traffic_log
        self._tx_counter = 0
        # Daniel/Enron mode: each "register" is 32 bits on the wire. The client
        # transparently doubles the count when reading and exposes the original
        # register-pair semantics to the caller; pair packing assumes the
        # device returns the bytes in standard FC 03 framing (big-endian),
        # which most real devices do — non-standard byte_count framing is out
        # of scope for v1.
        self.enron_mode = enron_mode

    # ----- lifecycle -----
    async def connect(self) -> None:
        client = AsyncModbusTcpClient(host=self.host, port=self.port, timeout=self.timeout)
        ok = await client.connect()
        if not ok:
            raise ClientError(f"could not connect to {self.host}:{self.port}")
        self._client = client

    async def disconnect(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.connected

    # ----- reads -----
    async def read_coils(self, address: int, count: int) -> list[int]:
        resp = await self._call("read_coils", address=address, count=count)
        return [int(b) for b in resp.bits[:count]]

    async def read_discrete_inputs(self, address: int, count: int) -> list[int]:
        resp = await self._call("read_discrete_inputs", address=address, count=count)
        return [int(b) for b in resp.bits[:count]]

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        # In Enron mode, the caller's count is in 32-bit registers, but the
        # underlying wire transaction is in 16-bit registers — we ask for 2N.
        wire_count = count * 2 if self.enron_mode else count
        resp = await self._call(
            "read_holding_registers", address=address, count=wire_count
        )
        return list(resp.registers)

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        wire_count = count * 2 if self.enron_mode else count
        resp = await self._call(
            "read_input_registers", address=address, count=wire_count
        )
        return list(resp.registers)

    # ----- writes -----
    async def write_coil(self, address: int, value: bool) -> None:
        await self._call("write_coil", address=address, value=value)

    async def write_register(self, address: int, value: int) -> None:
        await self._call("write_register", address=address, value=value)

    async def write_coils(self, address: int, values: list[bool] | list[int]) -> None:
        await self._call("write_coils", address=address, values=[bool(v) for v in values])

    async def write_registers(self, address: int, values: list[int]) -> None:
        await self._call("write_registers", address=address, values=list(values))

    async def mask_write_register(self, address: int, and_mask: int, or_mask: int) -> None:
        await self._call("mask_write_register", address=address, and_mask=and_mask, or_mask=or_mask)

    async def readwrite_registers(
        self,
        *,
        read_address: int,
        read_count: int,
        write_address: int,
        write_values: list[int],
    ) -> list[int]:
        resp = await self._call(
            "readwrite_registers",
            read_address=read_address,
            read_count=read_count,
            write_address=write_address,
            values=list(write_values),
        )
        return list(resp.registers)

    # ----- internal -----
    async def _call(self, method: str, **kwargs: object) -> Any:
        if self._client is None:
            raise NotConnectedError("call connect() first")
        fn = getattr(self._client, method)

        fc = _METHOD_TO_FC.get(method, 0)
        addr = self._extract_address(kwargs)
        request_values = self._extract_values(kwargs, fc)
        count = self._extract_count(kwargs, request_values)
        tx_hex = self._synth_request_hex(fc, addr, count, request_values)
        self._log(
            Direction.TX,
            fc,
            address=addr,
            count=count,
            values=tuple(request_values),
            raw_hex=tx_hex,
        )

        try:
            resp = await fn(device_id=self.unit_id, **kwargs)
        except TimeoutError as err:
            self._log(Direction.RX, fc, address=addr, count=count, notes="timeout")
            raise ClientError(f"timeout calling {method}") from err
        except ModbusException as err:
            self._log(Direction.RX, fc, address=addr, count=count, notes=f"transport: {err}")
            raise ClientError(f"transport error calling {method}: {err}") from err

        if isinstance(resp, ExceptionResponse):
            self._log(
                Direction.RX, fc, address=addr, count=count, exception_code=resp.exception_code
            )
            raise ModbusExceptionError(resp.exception_code)
        if resp.isError():
            self._log(Direction.RX, fc, address=addr, count=count, notes=repr(resp))
            raise ClientError(f"error response from peer for {method}: {resp!r}")

        # Successful response — decode values for the log
        response_values: tuple[int, ...] = ()
        if hasattr(resp, "registers") and resp.registers is not None:
            response_values = tuple(resp.registers)
        elif hasattr(resp, "bits") and resp.bits is not None:
            response_values = tuple(int(b) for b in resp.bits[: (count or len(resp.bits))])
        self._log(
            Direction.RX, fc, address=addr, count=count, values=response_values
        )
        return resp

    # ----- traffic logging helpers -----
    def _next_tx(self) -> int:
        self._tx_counter = (self._tx_counter + 1) & 0xFFFF
        return self._tx_counter

    @staticmethod
    def _extract_address(kwargs: dict[str, Any]) -> int | None:
        # Use explicit None check — address 0 is valid and must not fall through.
        for key in ("address", "read_address"):
            if key in kwargs and kwargs[key] is not None:
                return int(kwargs[key])
        return None

    @staticmethod
    def _extract_values(kwargs: dict[str, Any], fc: int) -> list[int]:
        if "values" in kwargs:
            raw = kwargs["values"] or []
            return [int(bool(v)) if fc in (0x05, 0x0F) else int(v) for v in raw]
        if "value" in kwargs:
            v = kwargs["value"]
            return [int(bool(v)) if fc == 0x05 else int(v)]
        return []

    @staticmethod
    def _extract_count(kwargs: dict[str, Any], request_values: list[int]) -> int | None:
        for key in ("count", "read_count"):
            if key in kwargs and kwargs[key] is not None:
                return int(kwargs[key])
        return len(request_values) or None

    def _synth_request_hex(
        self, fc: int, address: int | None, count: int | None, values: list[int]
    ) -> str:
        if fc == 0 or address is None:
            return ""
        try:
            if fc in (0x01, 0x02, 0x03, 0x04):
                pdu = struct.pack(">BHH", fc, address, count or 1)
            elif fc == 0x05:
                pdu = struct.pack(">BHH", fc, address, 0xFF00 if values and values[0] else 0x0000)
            elif fc == 0x06:
                pdu = struct.pack(">BHH", fc, address, (values or [0])[0] & 0xFFFF)
            elif fc == 0x0F:
                bits = values or []
                byte_count = (len(bits) + 7) // 8 if bits else 1
                packed = bytearray(byte_count)
                for i, b in enumerate(bits):
                    if b:
                        packed[i // 8] |= 1 << (i % 8)
                pdu = struct.pack(">BHHB", fc, address, len(bits), byte_count) + bytes(packed)
            elif fc == 0x10:
                regs = values or []
                body = struct.pack(">" + "H" * len(regs), *(v & 0xFFFF for v in regs))
                pdu = (
                    struct.pack(">BHHB", fc, address, len(regs), len(regs) * 2) + body
                )
            else:
                return ""
        except struct.error:
            return ""
        tx_id = self._next_tx()
        mbap = struct.pack(">HHHB", tx_id, 0, 1 + len(pdu), self.unit_id)
        return (mbap + pdu).hex()

    def _log(
        self,
        direction: Direction,
        fc: int,
        *,
        address: int | None = None,
        count: int | None = None,
        values: tuple[int, ...] = (),
        exception_code: int | None = None,
        raw_hex: str = "",
        notes: str = "",
    ) -> None:
        if self._traffic_log is None:
            return
        self._traffic_log.append(
            TrafficEntry(
                timestamp=datetime.now(UTC),
                direction=direction,
                peer=f"{self.host}:{self.port}",
                unit_id=self.unit_id,
                function_code=fc,
                exception_code=exception_code,
                address=address,
                count=count,
                values=values,
                raw_hex=raw_hex,
                notes=notes,
            )
        )
