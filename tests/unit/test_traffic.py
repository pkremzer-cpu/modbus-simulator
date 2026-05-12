"""Tests for modbus_simulator.core.traffic."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from modbus_simulator.core.traffic import (
    Direction,
    TrafficEntry,
    TrafficLog,
)


def make_entry(
    *,
    ts: datetime | None = None,
    direction: Direction = Direction.RX,
    peer: str = "127.0.0.1:52000",
    unit_id: int = 1,
    function_code: int = 3,
    exception_code: int | None = None,
    address: int | None = 0,
    count: int | None = 10,
    values: tuple[int, ...] = (),
    raw_hex: str = "",
) -> TrafficEntry:
    return TrafficEntry(
        timestamp=ts or datetime(2026, 4, 22, 12, 0, 0, tzinfo=UTC),
        direction=direction,
        peer=peer,
        unit_id=unit_id,
        function_code=function_code,
        exception_code=exception_code,
        address=address,
        count=count,
        values=values,
        raw_hex=raw_hex,
    )


class TestAppend:
    def test_single(self) -> None:
        log = TrafficLog(max_entries=10)
        e = make_entry()
        log.append(e)
        assert log.snapshot() == (e,)
        assert log.size == 1

    def test_many(self) -> None:
        log = TrafficLog(max_entries=100)
        entries = [make_entry(function_code=i) for i in range(50)]
        for e in entries:
            log.append(e)
        assert log.snapshot() == tuple(entries)


class TestCircular:
    def test_evicts_oldest_when_full(self) -> None:
        log = TrafficLog(max_entries=3)
        a, b, c, d = (make_entry(function_code=i) for i in range(4))
        for e in (a, b, c, d):
            log.append(e)
        assert log.snapshot() == (b, c, d)
        assert log.size == 3


class TestClear:
    def test_clear_empties(self) -> None:
        log = TrafficLog(max_entries=10)
        log.append(make_entry())
        log.append(make_entry())
        log.clear()
        assert log.snapshot() == ()
        assert log.size == 0


# ---------------------------------------------------------------------------
# Listeners
# ---------------------------------------------------------------------------
class TestListeners:
    def test_on_entry_called(self) -> None:
        log = TrafficLog(max_entries=10)
        seen: list[TrafficEntry] = []
        log.add_entry_listener(seen.append)
        e = make_entry()
        log.append(e)
        assert seen == [e]

    def test_bad_listener_doesnt_block_others(self) -> None:
        log = TrafficLog(max_entries=10)
        seen: list[TrafficEntry] = []

        def bad(_: TrafficEntry) -> None:
            raise RuntimeError("boom")

        log.add_entry_listener(bad)
        log.add_entry_listener(seen.append)
        log.append(make_entry())
        assert len(seen) == 1

    def test_remove_listener(self) -> None:
        log = TrafficLog(max_entries=10)
        seen: list[TrafficEntry] = []
        log.add_entry_listener(seen.append)
        log.append(make_entry())
        log.remove_entry_listener(seen.append)
        log.append(make_entry())
        assert len(seen) == 1


# ---------------------------------------------------------------------------
# Capacity warning
# ---------------------------------------------------------------------------
class TestCapacityWarning:
    def test_warning_fires_once_when_crossing_threshold(self) -> None:
        log = TrafficLog(max_entries=100, capacity_warn_ratio=0.9)
        warnings: list[int] = []
        log.add_capacity_listener(warnings.append)
        for _ in range(89):
            log.append(make_entry())
        assert warnings == []
        log.append(make_entry())  # 90 entries -> at threshold
        assert warnings == [90]

    def test_warning_does_not_refire_above_threshold(self) -> None:
        log = TrafficLog(max_entries=100, capacity_warn_ratio=0.9)
        warnings: list[int] = []
        log.add_capacity_listener(warnings.append)
        for _ in range(95):
            log.append(make_entry())
        assert warnings == [90]  # fired exactly once

    def test_warning_rearms_after_clear(self) -> None:
        log = TrafficLog(max_entries=10, capacity_warn_ratio=0.9)
        warnings: list[int] = []
        log.add_capacity_listener(warnings.append)
        for _ in range(9):
            log.append(make_entry())
        log.clear()
        for _ in range(9):
            log.append(make_entry())
        assert warnings == [9, 9]


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
class TestCsvExport:
    def test_header_present(self) -> None:
        log = TrafficLog(max_entries=10)
        csv = log.to_csv()
        lines = csv.splitlines()
        assert lines[0].startswith("timestamp,direction,peer,unit_id,function_code")

    def test_row_count(self) -> None:
        log = TrafficLog(max_entries=10)
        log.append(make_entry(function_code=3))
        log.append(make_entry(function_code=4))
        lines = log.to_csv().splitlines()
        assert len(lines) == 3  # header + 2 rows

    def test_exception_code_column(self) -> None:
        log = TrafficLog(max_entries=10)
        log.append(make_entry(exception_code=0x04))
        rows = log.to_csv().splitlines()
        assert ",4," in rows[1] or rows[1].endswith(",4")

    def test_values_column_is_space_separated(self) -> None:
        log = TrafficLog(max_entries=10)
        log.append(make_entry(values=(100, 200, 300)))
        rows = log.to_csv().splitlines()
        assert "100 200 300" in rows[1]

    def test_empty_log_csv_is_header_only(self) -> None:
        log = TrafficLog(max_entries=10)
        csv = log.to_csv()
        assert len(csv.splitlines()) == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class TestValidation:
    def test_rejects_zero_max(self) -> None:
        with pytest.raises(ValueError):
            TrafficLog(max_entries=0)

    def test_rejects_warn_ratio_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            TrafficLog(max_entries=10, capacity_warn_ratio=1.5)
        with pytest.raises(ValueError):
            TrafficLog(max_entries=10, capacity_warn_ratio=-0.1)
