"""Register store for the simulator server.

Holds the four Modbus address spaces (coils, discrete inputs, holding
registers, input registers) as plain Python lists. Writes are broadcast to
registered listeners so the GUI, the trend buffer, and the simulator can react.

This module is deliberately Qt-free so it can be unit-tested without a
``QApplication``. The GUI layer wraps it and re-emits the change events as Qt
signals.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

log = logging.getLogger(__name__)

MAX_BLOCK_SIZE = 65536


class BlockKind(StrEnum):
    COILS = "coils"
    DISCRETE_INPUTS = "discrete_inputs"
    HOLDING_REGISTERS = "holding_registers"
    INPUT_REGISTERS = "input_registers"

    @property
    def is_bit(self) -> bool:
        return self in (BlockKind.COILS, BlockKind.DISCRETE_INPUTS)


@dataclass(frozen=True, slots=True)
class BlockChange:
    kind: BlockKind
    address: int
    values: tuple[int, ...]


ChangeListener = Callable[[BlockChange], None]


class RegisterBlock:
    """One Modbus address space (coils / DI / HR / IR)."""

    def __init__(self, kind: BlockKind, size: int, *, initial: int = 0) -> None:
        if not 1 <= size <= MAX_BLOCK_SIZE:
            raise ValueError(f"size must be in [1, {MAX_BLOCK_SIZE}], got {size}")
        self._kind = kind
        self._size = size
        self._max_value = 1 if kind.is_bit else 0xFFFF
        self._values: list[int] = [initial & self._max_value] * size
        self._lock = threading.RLock()
        self._listeners: list[ChangeListener] = []

    # ----- metadata -----
    @property
    def kind(self) -> BlockKind:
        return self._kind

    @property
    def size(self) -> int:
        return self._size

    @property
    def max_value(self) -> int:
        return self._max_value

    # ----- access -----
    def get(self, address: int, count: int = 1) -> tuple[int, ...]:
        self._check_range(address, count)
        with self._lock:
            return tuple(self._values[address : address + count])

    def set(self, address: int, values: Sequence[int]) -> None:
        count = len(values)
        self._check_range(address, count)
        normalized: list[int] = []
        for value in values:
            if not 0 <= value <= self._max_value:
                raise ValueError(
                    f"value {value} out of range [0, {self._max_value}] for {self._kind.value}"
                )
            normalized.append(value)
        with self._lock:
            self._values[address : address + count] = normalized
        self._emit(BlockChange(kind=self._kind, address=address, values=tuple(normalized)))

    def _check_range(self, address: int, count: int) -> None:
        if address < 0:
            raise ValueError(f"address must be non-negative, got {address}")
        if count < 1:
            raise ValueError(f"count must be at least 1, got {count}")
        if address + count > self._size:
            raise IndexError(
                f"range [{address}, {address + count}) exceeds block size {self._size}"
            )

    # ----- listeners -----
    def add_listener(self, listener: ChangeListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: ChangeListener) -> None:
        self._listeners.remove(listener)

    def _emit(self, change: BlockChange) -> None:
        # Iterate over a copy so listeners may unsubscribe during dispatch.
        for listener in list(self._listeners):
            try:
                listener(change)
            except Exception:
                log.exception("change listener %r raised", listener)


# ---------------------------------------------------------------------------
# DataStore facade
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class DataStoreConfig:
    coils_size: int = 1024
    discrete_inputs_size: int = 1024
    holding_registers_size: int = 1024
    input_registers_size: int = 1024


@dataclass(slots=True)
class DataStore:
    """All four Modbus blocks for one unit ID."""

    coils: RegisterBlock = field(init=False)
    discrete_inputs: RegisterBlock = field(init=False)
    holding_registers: RegisterBlock = field(init=False)
    input_registers: RegisterBlock = field(init=False)
    _config: DataStoreConfig = field(default_factory=DataStoreConfig)

    def __init__(self, config: DataStoreConfig | None = None) -> None:
        self._config = config or DataStoreConfig()
        self.coils = RegisterBlock(BlockKind.COILS, self._config.coils_size)
        self.discrete_inputs = RegisterBlock(
            BlockKind.DISCRETE_INPUTS, self._config.discrete_inputs_size
        )
        self.holding_registers = RegisterBlock(
            BlockKind.HOLDING_REGISTERS, self._config.holding_registers_size
        )
        self.input_registers = RegisterBlock(
            BlockKind.INPUT_REGISTERS, self._config.input_registers_size
        )

    def block(self, kind: BlockKind) -> RegisterBlock:
        match kind:
            case BlockKind.COILS:
                return self.coils
            case BlockKind.DISCRETE_INPUTS:
                return self.discrete_inputs
            case BlockKind.HOLDING_REGISTERS:
                return self.holding_registers
            case BlockKind.INPUT_REGISTERS:
                return self.input_registers
