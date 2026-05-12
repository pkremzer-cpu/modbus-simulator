"""Tests for modbus_simulator.core.datastore."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from modbus_simulator.core.datastore import (
    BlockChange,
    BlockKind,
    DataStore,
    DataStoreConfig,
    RegisterBlock,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# RegisterBlock — sizing
# ---------------------------------------------------------------------------
class TestRegisterBlockSize:
    @pytest.mark.parametrize("size", [1, 100, 65536])
    def test_valid_sizes(self, size: int) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, size)
        assert block.size == size

    @pytest.mark.parametrize("size", [0, -1, 65537, 100000])
    def test_invalid_sizes_raise(self, size: int) -> None:
        with pytest.raises(ValueError):
            RegisterBlock(BlockKind.HOLDING_REGISTERS, size)

    def test_initial_fill(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10, initial=0x1234)
        assert block.get(0, 10) == (0x1234,) * 10

    def test_initial_masked_for_bits(self) -> None:
        # 42 & 1 == 0
        block = RegisterBlock(BlockKind.COILS, 5, initial=42)
        assert block.get(0, 5) == (0,) * 5


# ---------------------------------------------------------------------------
# RegisterBlock — get / set happy path
# ---------------------------------------------------------------------------
class TestRegisterBlockAccess:
    def test_default_zero(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 100)
        assert block.get(0, 100) == (0,) * 100

    def test_set_get_roundtrip(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10)
        block.set(3, [100, 200, 300])
        assert block.get(3, 3) == (100, 200, 300)
        assert block.get(2, 5) == (0, 100, 200, 300, 0)

    def test_set_single_element(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10)
        block.set(5, [0xABCD])
        assert block.get(5) == (0xABCD,)

    def test_coil_accepts_0_1(self) -> None:
        block = RegisterBlock(BlockKind.COILS, 10)
        block.set(0, [1, 0, 1, 1, 0])
        assert block.get(0, 5) == (1, 0, 1, 1, 0)


# ---------------------------------------------------------------------------
# RegisterBlock — validation
# ---------------------------------------------------------------------------
class TestRegisterBlockValidation:
    def test_get_out_of_range(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10)
        with pytest.raises(IndexError):
            block.get(5, 6)

    def test_set_out_of_range(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10)
        with pytest.raises(IndexError):
            block.set(8, [1, 2, 3])

    def test_set_negative_value(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10)
        with pytest.raises(ValueError):
            block.set(0, [-1])

    def test_set_value_too_large(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10)
        with pytest.raises(ValueError):
            block.set(0, [0x10000])

    def test_set_coil_rejects_2(self) -> None:
        block = RegisterBlock(BlockKind.COILS, 10)
        with pytest.raises(ValueError):
            block.set(0, [2])

    def test_get_negative_address(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10)
        with pytest.raises(ValueError):
            block.get(-1, 1)

    def test_get_zero_count(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10)
        with pytest.raises(ValueError):
            block.get(0, 0)


# ---------------------------------------------------------------------------
# Change listeners
# ---------------------------------------------------------------------------
class TestChangeListeners:
    def test_set_emits_change(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10)
        events: list[BlockChange] = []
        block.add_listener(events.append)
        block.set(2, [100, 200])
        assert events == [
            BlockChange(
                kind=BlockKind.HOLDING_REGISTERS,
                address=2,
                values=(100, 200),
            )
        ]

    def test_removed_listener_silent(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10)
        events: list[BlockChange] = []
        listener: Callable[[BlockChange], None] = events.append
        block.add_listener(listener)
        block.set(0, [1])
        block.remove_listener(listener)
        block.set(0, [2])
        assert len(events) == 1

    def test_listener_exception_does_not_break_others(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10)
        events: list[BlockChange] = []

        def bad(_: BlockChange) -> None:
            raise RuntimeError("boom")

        block.add_listener(bad)
        block.add_listener(events.append)
        block.set(0, [42])
        assert len(events) == 1

    def test_no_emit_on_get(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10)
        events: list[BlockChange] = []
        block.add_listener(events.append)
        block.get(0, 5)
        assert events == []

    def test_no_emit_on_validation_failure(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 10)
        events: list[BlockChange] = []
        block.add_listener(events.append)
        with pytest.raises(ValueError):
            block.set(0, [0x10000])
        assert events == []


# ---------------------------------------------------------------------------
# Thread safety smoke test
# ---------------------------------------------------------------------------
class TestThreadSafety:
    def test_concurrent_writes(self) -> None:
        block = RegisterBlock(BlockKind.HOLDING_REGISTERS, 1000)
        barrier = threading.Barrier(4)

        def worker(start: int) -> None:
            barrier.wait()
            for i in range(250):
                block.set(start + i, [(start + i) & 0xFFFF])

        threads = [threading.Thread(target=worker, args=(s,)) for s in (0, 250, 500, 750)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert block.get(0, 1000) == tuple(i & 0xFFFF for i in range(1000))


# ---------------------------------------------------------------------------
# DataStore facade
# ---------------------------------------------------------------------------
class TestDataStore:
    def test_default_sizes(self) -> None:
        store = DataStore()
        assert store.coils.size == 1024
        assert store.discrete_inputs.size == 1024
        assert store.holding_registers.size == 1024
        assert store.input_registers.size == 1024

    def test_custom_sizes(self) -> None:
        store = DataStore(
            DataStoreConfig(
                coils_size=100,
                discrete_inputs_size=200,
                holding_registers_size=300,
                input_registers_size=400,
            )
        )
        assert store.coils.size == 100
        assert store.discrete_inputs.size == 200
        assert store.holding_registers.size == 300
        assert store.input_registers.size == 400

    def test_block_lookup(self) -> None:
        store = DataStore()
        assert store.block(BlockKind.COILS) is store.coils
        assert store.block(BlockKind.DISCRETE_INPUTS) is store.discrete_inputs
        assert store.block(BlockKind.HOLDING_REGISTERS) is store.holding_registers
        assert store.block(BlockKind.INPUT_REGISTERS) is store.input_registers

    def test_blocks_independent(self) -> None:
        store = DataStore()
        store.holding_registers.set(0, [42])
        assert store.input_registers.get(0) == (0,)
        assert store.coils.get(0) == (0,)
