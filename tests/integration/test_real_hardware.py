"""Optional real-hardware smoke tests.

Skipped by default — run explicitly with::

    uv run pytest -m realhw

Targets the device at :data:`REAL_HOST`:``502``. Only non-destructive reads
are performed; no writes, so it's safe against a live PLC. Each test skips
if the host is not reachable within the timeout so a missing device doesn't
fail the run.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from modbus_simulator.core.client import Client, ClientError

REAL_HOST = "10.0.0.91"
REAL_PORT = 502
REAL_UNIT = 1
CONNECT_TIMEOUT = 1.0

pytestmark = [pytest.mark.realhw, pytest.mark.asyncio]


async def _reachable() -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(REAL_HOST, REAL_PORT), timeout=CONNECT_TIMEOUT
        )
    except (TimeoutError, OSError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    _ = reader
    return True


@pytest.fixture
async def real_client():
    if not await _reachable():
        pytest.skip(f"{REAL_HOST}:{REAL_PORT} not reachable")
    client = Client(host=REAL_HOST, port=REAL_PORT, unit_id=REAL_UNIT, timeout=2.0)
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


async def test_connects_to_real_device(real_client: Client) -> None:
    assert real_client.is_connected


async def test_reads_holding_registers_at_address_0(real_client: Client) -> None:
    values = await real_client.read_holding_registers(0, 4)
    assert isinstance(values, list) and len(values) == 4
    # Register values are 16-bit unsigned
    for v in values:
        assert 0 <= v <= 0xFFFF


async def test_reads_at_address_2049(real_client: Client) -> None:
    """Address the user observed during the live test."""
    try:
        values = await real_client.read_holding_registers(2049, 1)
    except ClientError as err:
        pytest.skip(f"device rejected address 2049: {err}")
        return
    assert isinstance(values, list) and len(values) == 1


async def test_larger_block_read_within_spec(real_client: Client) -> None:
    """Many devices refuse large reads — just verify bounded response."""
    try:
        values = await real_client.read_holding_registers(0, 32)
    except ClientError as err:
        pytest.skip(f"device refused 32-reg read: {err}")
        return
    assert len(values) == 32
