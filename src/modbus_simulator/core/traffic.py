"""Traffic log — bounded circular buffer of Modbus frame events.

The server and client append :class:`TrafficEntry` objects; the GUI subscribes
as a listener to render a live table and takes ``snapshot()`` when the user
exports to CSV.

The log is deliberately framed around the application view (direction,
decoded fields) rather than the wire format, so operators can scan it without
parsing hex. The ``raw_hex`` field preserves the wire bytes for deep dives.

One capacity-warning signal fires exactly once when the buffer crosses the
configured fill ratio; it re-arms after :meth:`clear`.
"""

from __future__ import annotations

import csv
import io
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

log = logging.getLogger(__name__)


class Direction(StrEnum):
    RX = "rx"
    TX = "tx"


@dataclass(frozen=True, slots=True)
class TrafficEntry:
    timestamp: datetime
    direction: Direction
    peer: str  # "host:port"
    unit_id: int
    function_code: int
    exception_code: int | None
    address: int | None
    count: int | None
    values: tuple[int, ...] = ()
    raw_hex: str = ""
    notes: str = field(default="")


EntryListener = Callable[[TrafficEntry], None]
CapacityListener = Callable[[int], None]


class TrafficLog:
    def __init__(
        self,
        max_entries: int = 50_000,
        *,
        capacity_warn_ratio: float = 0.95,
    ) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        if not 0.0 <= capacity_warn_ratio <= 1.0:
            raise ValueError(f"capacity_warn_ratio must be in [0, 1], got {capacity_warn_ratio}")
        self._max = max_entries
        self._warn_threshold = max(1, round(max_entries * capacity_warn_ratio))
        self._entries: deque[TrafficEntry] = deque(maxlen=max_entries)
        self._lock = threading.RLock()
        self._entry_listeners: list[EntryListener] = []
        self._capacity_listeners: list[CapacityListener] = []
        self._capacity_fired = False

    # ----- read API -----
    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def max_entries(self) -> int:
        return self._max

    def snapshot(self) -> tuple[TrafficEntry, ...]:
        with self._lock:
            return tuple(self._entries)

    # ----- mutation API -----
    def append(self, entry: TrafficEntry) -> None:
        with self._lock:
            self._entries.append(entry)
            size = len(self._entries)
            cap_event: int | None = None
            if size >= self._warn_threshold and not self._capacity_fired:
                self._capacity_fired = True
                cap_event = size
        self._dispatch(self._entry_listeners, entry)
        if cap_event is not None:
            self._dispatch(self._capacity_listeners, cap_event)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._capacity_fired = False

    # ----- listeners -----
    def add_entry_listener(self, listener: EntryListener) -> None:
        self._entry_listeners.append(listener)

    def remove_entry_listener(self, listener: EntryListener) -> None:
        self._entry_listeners.remove(listener)

    def add_capacity_listener(self, listener: CapacityListener) -> None:
        self._capacity_listeners.append(listener)

    def remove_capacity_listener(self, listener: CapacityListener) -> None:
        self._capacity_listeners.remove(listener)

    # ----- export -----
    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "timestamp",
                "direction",
                "peer",
                "unit_id",
                "function_code",
                "exception_code",
                "address",
                "count",
                "values",
                "raw_hex",
                "notes",
            ]
        )
        for entry in self.snapshot():
            writer.writerow(
                [
                    entry.timestamp.isoformat(),
                    entry.direction.value,
                    entry.peer,
                    entry.unit_id,
                    entry.function_code,
                    "" if entry.exception_code is None else entry.exception_code,
                    "" if entry.address is None else entry.address,
                    "" if entry.count is None else entry.count,
                    " ".join(str(v) for v in entry.values),
                    entry.raw_hex,
                    entry.notes,
                ]
            )
        return buf.getvalue()

    # ----- internals -----
    @staticmethod
    def _dispatch(listeners: list[Callable[..., None]], payload: object) -> None:
        for listener in list(listeners):
            try:
                listener(payload)
            except Exception:
                log.exception("traffic listener %r raised", listener)
