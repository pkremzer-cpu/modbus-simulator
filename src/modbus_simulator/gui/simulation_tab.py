"""Simulation tab — list of generator assignments and a runner task.

Minimal editor: add Constant / Sine / Ramp entries via buttons, remove with a
click, toggle a background scheduler that applies values at 10 Hz. Advanced
generators (Random / Script / Toggle / Pattern) can be added by loading a
session JSON file.
"""

from __future__ import annotations

import asyncio
import logging
import time

from PyQt6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from modbus_simulator.config.schema import (
    ConstantGenCfg,
    RampGenCfg,
    SessionConfig,
    SimulationEntry,
    SineGenCfg,
)
from modbus_simulator.core.datastore import BlockKind, DataStore
from modbus_simulator.core.server import Server
from modbus_simulator.core.simulator import (
    Constant,
    Pattern,
    Ramp,
    RampDirection,
    RandomGen,
    Script,
    Sine,
    Toggle,
    ValueGenerator,
)

log = logging.getLogger(__name__)

TICK_INTERVAL_S = 0.1


class SimulationTab(QWidget):
    def __init__(self, datastore: DataStore, server: Server, session: SessionConfig) -> None:
        super().__init__()
        self._datastore = datastore
        self._server = server
        self._session = session
        self._entries: list[SimulationEntry] = list(session.simulations)
        self._task: asyncio.Task[None] | None = None
        self._build()
        self._rebuild_list()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        self._list = QListWidget()

        add_const = QPushButton("+ Állandó HR")
        add_const.clicked.connect(self._add_constant)
        add_sine = QPushButton("+ Szinusz HR")
        add_sine.clicked.connect(self._add_sine)
        add_ramp = QPushButton("+ Rámpa HR")
        add_ramp.clicked.connect(self._add_ramp)
        remove_btn = QPushButton("Kijelölt törlése")
        remove_btn.clicked.connect(self._remove_selected)

        self._run_btn = QPushButton("Szimuláció indítása")
        self._run_btn.setCheckable(True)
        self._run_btn.toggled.connect(self._on_toggle_run)

        row = QHBoxLayout()
        row.addWidget(add_const)
        row.addWidget(add_sine)
        row.addWidget(add_ramp)
        row.addWidget(remove_btn)
        row.addStretch(1)
        row.addWidget(self._run_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Aktív szimulációk (haladó generátorok — Script, Random, Toggle, "
                "Pattern — munkamenet JSON-ból tölthetők):"
            )
        )
        layout.addLayout(row)
        layout.addWidget(self._list, 1)

    def _rebuild_list(self) -> None:
        self._list.clear()
        for entry in self._entries:
            item = QListWidgetItem(_describe(entry))
            self._list.addItem(item)

    # ------------------------------------------------------------------
    # Adders
    # ------------------------------------------------------------------
    def _add_constant(self) -> None:
        addr, ok = _spin_dialog(self, "Holding register address", 0, 65535)
        if not ok:
            return
        value, ok = _double_dialog(self, "Constant value", 0.0, -1e9, 1e9)
        if not ok:
            return
        self._entries.append(
            SimulationEntry(
                block=BlockKind.HOLDING_REGISTERS,
                address=addr,
                generator=ConstantGenCfg(value=value),
            )
        )
        self._rebuild_list()

    def _add_sine(self) -> None:
        addr, ok = _spin_dialog(self, "Holding register address", 0, 65535)
        if not ok:
            return
        amp, ok = _double_dialog(self, "Amplitude", 100.0, 0, 1e9)
        if not ok:
            return
        offset, ok = _double_dialog(self, "Offset", 0.0, -1e9, 1e9)
        if not ok:
            return
        freq, ok = _double_dialog(self, "Frequency (Hz)", 1.0, 0.001, 1000)
        if not ok:
            return
        self._entries.append(
            SimulationEntry(
                block=BlockKind.HOLDING_REGISTERS,
                address=addr,
                generator=SineGenCfg(amplitude=amp, offset=offset, frequency_hz=freq),
            )
        )
        self._rebuild_list()

    def _add_ramp(self) -> None:
        addr, ok = _spin_dialog(self, "Holding register address", 0, 65535)
        if not ok:
            return
        mn, ok = _double_dialog(self, "Min", 0.0, -1e9, 1e9)
        if not ok:
            return
        mx, ok = _double_dialog(self, "Max", 100.0, -1e9, 1e9)
        if not ok or mx <= mn:
            QMessageBox.warning(self, "Bad range", "Max must be greater than min")
            return
        step, ok = _double_dialog(self, "Step", 1.0, 0.0001, 1e9)
        if not ok:
            return
        period_ms, ok = _double_dialog(self, "Period (ms)", 1000.0, 10, 60_000)
        if not ok:
            return
        self._entries.append(
            SimulationEntry(
                block=BlockKind.HOLDING_REGISTERS,
                address=addr,
                generator=RampGenCfg(
                    min=mn, max=mx, step=step, period_ms=period_ms, direction=RampDirection.UP
                ),
            )
        )
        self._rebuild_list()

    def _remove_selected(self) -> None:
        rows = sorted({self._list.row(it) for it in self._list.selectedItems()}, reverse=True)
        for r in rows:
            del self._entries[r]
        self._rebuild_list()

    # ------------------------------------------------------------------
    # Runner
    # ------------------------------------------------------------------
    def _on_toggle_run(self, running: bool) -> None:
        if running:
            self._task = asyncio.create_task(self._run())
            self._run_btn.setText("Szimuláció leállítása")
        else:
            if self._task is not None:
                self._task.cancel()
            self._run_btn.setText("Szimuláció indítása")

    async def _run(self) -> None:
        start = time.monotonic()
        try:
            while True:
                t = time.monotonic() - start
                self._apply_all(t)
                await asyncio.sleep(TICK_INTERVAL_S)
        except asyncio.CancelledError:
            return

    def _apply_all(self, t: float) -> None:
        for entry in list(self._entries):
            generator = _build_generator(entry)
            try:
                block = self._datastore.block(entry.block)
                prev = block.get(entry.address)[0]
                raw = generator.sample(t, prev)
                value = _clamp(int(raw), 0, block.max_value)
                block.set(entry.address, [value])
            except Exception:
                log.exception("simulation tick failed for %r", entry)

    # ------------------------------------------------------------------
    def apply_to(self, session: SessionConfig) -> None:
        session.simulations = list(self._entries)

    def reload_from(self, session: SessionConfig) -> None:
        self._session = session
        self._entries = list(session.simulations)
        self._rebuild_list()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _spin_dialog(
    parent: QWidget, label: str, default: int, lo: int, hi: int = 65535
) -> tuple[int, bool]:
    return QInputDialog.getInt(parent, "Simulation", label, default, lo, hi)


def _double_dialog(
    parent: QWidget, label: str, default: float, lo: float, hi: float
) -> tuple[float, bool]:
    return QInputDialog.getDouble(parent, "Simulation", label, default, lo, hi, 4)


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _describe(entry: SimulationEntry) -> str:
    g = entry.generator
    match g.kind:
        case "constant":
            params = f"value={g.value}"
        case "sine":
            params = f"amp={g.amplitude} off={g.offset} f={g.frequency_hz}Hz"
        case "ramp":
            params = f"{g.min}..{g.max} step={g.step} T={g.period_ms}ms {g.direction.value}"
        case "random":
            params = f"{g.min}..{g.max} {g.distribution.value} every {g.update_ms}ms"
        case "script":
            params = f"script: {g.source[:40]}"
        case "toggle":
            params = f"toggle T={g.period_ms}ms"
        case "pattern":
            params = f"pattern {list(g.bits)} shift={g.shift_ms}ms"
        case _:
            params = "?"
    return f"{entry.block.value}[{entry.address}]  {g.kind}  {params}"


def _build_generator(entry: SimulationEntry) -> ValueGenerator:
    g = entry.generator
    match g.kind:
        case "constant":
            return Constant(value=g.value)
        case "sine":
            return Sine(
                amplitude=g.amplitude,
                offset=g.offset,
                frequency_hz=g.frequency_hz,
                phase_deg=g.phase_deg,
            )
        case "ramp":
            return Ramp(
                min=g.min,
                max=g.max,
                step=g.step,
                period_ms=g.period_ms,
                direction=g.direction,
            )
        case "random":
            return RandomGen(
                min=g.min,
                max=g.max,
                distribution=g.distribution,
                update_ms=g.update_ms,
                seed=g.seed,
            )
        case "script":
            return Script.from_source(g.source)
        case "toggle":
            return Toggle(period_ms=g.period_ms)
        case "pattern":
            return Pattern(bits=tuple(g.bits), shift_ms=g.shift_ms)
    raise ValueError(f"unknown generator kind: {g.kind}")
