"""Trend chart tab — live pyqtgraph plot.

Two sources can be trended simultaneously:

* **HR[addr]** — a holding register of the *local* server's datastore.
  Updates arrive as ``BlockChange`` events whenever any component writes to
  that register (Simulation tab, external master, etc.).
* **poll:<name>** — a polling-table row from the Client tab. The Client tab
  emits ``polling_sample(name, value)`` after each successful read; the
  Trend tab records the scalar into a per-name buffer.

X axis follows a sliding time window the user picks; Y axis is computed
explicitly from the visible samples every redraw, so live values always fill
the chart.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

import pyqtgraph as pg
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from modbus_simulator.core.datastore import BlockChange, DataStore

log = logging.getLogger(__name__)

WINDOW_OPTIONS_S = [10, 60, 300, 3600]
DEFAULT_WINDOW_S = 60
MAX_SAMPLES = 36_000  # 1 h at 10 Hz
SAMPLE_RATE_MS = 100


class TrendTab(QWidget):
    def __init__(self, datastore: DataStore) -> None:
        super().__init__()
        self._datastore = datastore

        # Two independent channel families identified by string key:
        #   "hr:<addr>"   — holding register from the local datastore
        #   "poll:<name>" — polling row from the Client tab
        self._samples: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=MAX_SAMPLES)
        )
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._start = time.monotonic()

        datastore.holding_registers.add_listener(self._on_hr_change)
        self._build()

    def _build(self) -> None:
        from modbus_simulator.gui.theme import PYQTGRAPH_BG, PYQTGRAPH_FG, PYQTGRAPH_GRID_ALPHA

        pg.setConfigOption("foreground", PYQTGRAPH_FG)
        self._plot = pg.PlotWidget()
        self._plot.setBackground(PYQTGRAPH_BG)
        self._plot.showGrid(x=True, y=True, alpha=PYQTGRAPH_GRID_ALPHA)
        self._plot.setLabel("bottom", "Idő (s)")
        self._plot.setLabel("left", "Érték")
        self._plot.addLegend()

        # ---- HR picker ----
        self._addr_spin = QSpinBox()
        self._addr_spin.setRange(0, self._datastore.holding_registers.size - 1)
        add_hr_btn = QPushButton("HR hozzáadása")
        add_hr_btn.clicked.connect(self._on_add_hr)

        # ---- Polling channel picker (free text; names come from the Client tab) ----
        self._poll_name_edit = QLineEdit()
        self._poll_name_edit.setPlaceholderText("polling sor neve")
        add_poll_btn = QPushButton("Polling csatorna hozzáadása")
        add_poll_btn.clicked.connect(self._on_add_poll)

        # ---- Selected list + remove ----
        self._selected = QListWidget()
        self._selected.setMaximumWidth(200)
        self._remove_btn = QPushButton("Kijelölt törlése")
        self._remove_btn.clicked.connect(self._on_remove)

        self._window_spin = QSpinBox()
        self._window_spin.setSuffix(" s")
        self._window_spin.setRange(5, 3600)
        self._window_spin.setValue(DEFAULT_WINDOW_S)

        left = QVBoxLayout()
        left.addWidget(QLabel("Holding register:"))
        row_hr = QHBoxLayout()
        row_hr.addWidget(self._addr_spin)
        row_hr.addWidget(add_hr_btn)
        left.addLayout(row_hr)
        left.addWidget(QLabel("Polling channel:"))
        row_poll = QHBoxLayout()
        row_poll.addWidget(self._poll_name_edit, 1)
        row_poll.addWidget(add_poll_btn)
        left.addLayout(row_poll)
        left.addWidget(QLabel(
            "Követett görbék (a polling sorok automatikusan megjelennek):"
        ))
        left.addWidget(self._selected, 1)
        left.addWidget(self._remove_btn)
        left.addWidget(QLabel("Időablak:"))
        left.addWidget(self._window_spin)

        top = QHBoxLayout()
        top.addLayout(left)
        top.addWidget(self._plot, 1)

        root = QVBoxLayout(self)
        root.addLayout(top)

        self._timer = QTimer(self)
        self._timer.setInterval(SAMPLE_RATE_MS)
        self._timer.timeout.connect(self._redraw)
        self._timer.start()

    # ------------------------------------------------------------------
    # Data ingress
    # ------------------------------------------------------------------
    def _on_hr_change(self, change: BlockChange) -> None:
        now = time.monotonic() - self._start
        for offset, value in enumerate(change.values):
            addr = change.address + offset
            key = f"hr:{addr}"
            if key in self._curves:
                self._samples[key].append((now, float(value)))

    def on_polling_sample(self, name: str, value: float) -> None:
        """Invoked by the MainWindow when ClientTab.polling_sample fires.

        Auto-creates a curve the first time a new polling channel is seen, so
        the operator doesn't have to type the name into the sidebar too.
        """
        key = f"poll:{name}"
        if key not in self._curves:
            self._add_curve(key, f"poll:{name}")
        now = time.monotonic() - self._start
        self._samples[key].append((now, float(value)))

    # ------------------------------------------------------------------
    # Channel management
    # ------------------------------------------------------------------
    def _on_add_hr(self) -> None:
        addr = self._addr_spin.value()
        self._add_curve(f"hr:{addr}", f"HR[{addr}]")
        # seed with current value so the curve isn't empty until next write
        try:
            current = self._datastore.holding_registers.get(addr)[0]
            now = time.monotonic() - self._start
            self._samples[f"hr:{addr}"].append((now, float(current)))
        except IndexError:
            pass

    def _on_add_poll(self) -> None:
        name = self._poll_name_edit.text().strip()
        if not name:
            return
        self._add_curve(f"poll:{name}", f"poll:{name}")
        self._poll_name_edit.clear()

    def _add_curve(self, key: str, label: str) -> None:
        if key in self._curves:
            return
        color = pg.intColor(len(self._curves), hues=8)
        curve = self._plot.plot([], [], pen=pg.mkPen(color, width=2), name=label)
        self._curves[key] = curve
        item = QListWidgetItem(label)
        item.setData(0x0100, key)  # Qt.UserRole
        self._selected.addItem(item)

    def _on_remove(self) -> None:
        items = self._selected.selectedItems()
        for item in items:
            key = str(item.data(0x0100))
            curve = self._curves.pop(key, None)
            if curve is not None:
                self._plot.removeItem(curve)
            self._samples.pop(key, None)
            self._selected.takeItem(self._selected.row(item))

    # ------------------------------------------------------------------
    # Redraw
    # ------------------------------------------------------------------
    def _redraw(self) -> None:
        if not self._curves:
            return
        now = time.monotonic() - self._start
        window = self._window_spin.value()
        lo = now - window

        visible_values: list[float] = []
        for key, curve in self._curves.items():
            data = [(t, v) for (t, v) in self._samples[key] if t >= lo]
            if not data:
                continue
            xs = [t for t, _ in data]
            ys = [v for _, v in data]
            curve.setData(xs, ys)
            visible_values.extend(ys)

        self._plot.setXRange(lo, now, padding=0)
        if visible_values:
            ymin = min(visible_values)
            ymax = max(visible_values)
            if ymin == ymax:
                pad = max(1.0, abs(ymin) * 0.1)
                ymin -= pad
                ymax += pad
            else:
                pad = (ymax - ymin) * 0.08
                ymin -= pad
                ymax += pad
            self._plot.setYRange(ymin, ymax, padding=0)
