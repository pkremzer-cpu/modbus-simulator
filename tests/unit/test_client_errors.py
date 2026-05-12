"""Unit tests for client-side error paths without a live server."""

from __future__ import annotations

import pytest

from modbus_simulator.core.client import Client, ClientError, NotConnectedError


def test_invalid_unit_id_rejected() -> None:
    with pytest.raises(ValueError):
        Client(host="127.0.0.1", port=5020, unit_id=0)
    with pytest.raises(ValueError):
        Client(host="127.0.0.1", port=5020, unit_id=248)


def test_is_connected_false_before_connect() -> None:
    client = Client(host="127.0.0.1", port=5020, unit_id=1)
    assert client.is_connected is False


async def test_read_before_connect_raises_not_connected() -> None:
    client = Client(host="127.0.0.1", port=5020, unit_id=1)
    with pytest.raises(NotConnectedError):
        await client.read_holding_registers(0, 1)


async def test_connect_to_unreachable_raises() -> None:
    # Assume nothing is listening on 127.0.0.1:1 (port 1 is privileged + unused)
    client = Client(host="127.0.0.1", port=1, unit_id=1, timeout=0.5)
    with pytest.raises(ClientError):
        await client.connect()


async def test_disconnect_is_idempotent() -> None:
    client = Client(host="127.0.0.1", port=5020, unit_id=1)
    await client.disconnect()  # no-op when never connected
    await client.disconnect()  # still no-op
