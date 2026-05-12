"""Integration test fixtures.

Spins up a loopback Modbus TCP server on an ephemeral port and gives each test
a fresh, connected :class:`Client` plus references to the server-side datastore,
rule engine, and traffic log so tests can manipulate state directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio

from modbus_simulator.core.client import Client
from modbus_simulator.core.datastore import DataStore, DataStoreConfig
from modbus_simulator.core.exceptions import RuleEngine
from modbus_simulator.core.server import Server
from modbus_simulator.core.traffic import TrafficLog


@dataclass(slots=True)
class Harness:
    server: Server
    client: Client
    datastore: DataStore
    rules: RuleEngine
    traffic: TrafficLog


@pytest_asyncio.fixture
async def harness() -> AsyncIterator[Harness]:
    datastore = DataStore(
        DataStoreConfig(
            coils_size=200,
            discrete_inputs_size=200,
            holding_registers_size=200,
            input_registers_size=200,
        )
    )
    rules = RuleEngine()
    traffic = TrafficLog(max_entries=1000)
    server = Server(
        host="127.0.0.1",
        port=0,  # OS chooses
        unit_id=1,
        datastore=datastore,
        rule_engine=rules,
        traffic_log=traffic,
    )
    await server.start()
    client = Client(host="127.0.0.1", port=server.bound_port, unit_id=1, timeout=2.0)
    await client.connect()
    try:
        yield Harness(
            server=server, client=client, datastore=datastore, rules=rules, traffic=traffic
        )
    finally:
        await client.disconnect()
        await server.stop()


@pytest.fixture
def fast_timeout_harness(harness: Harness) -> Harness:
    """Alias for tests that rely on the fixture but want a shorter client timeout."""
    harness.client.timeout = 0.5
    return harness
