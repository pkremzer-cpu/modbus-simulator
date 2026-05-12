"""Server tab — lifecycle + register browser for all four blocks."""

from __future__ import annotations

import asyncio
import logging

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from modbus_simulator.config.schema import SessionConfig
from modbus_simulator.core.datastore import BlockKind, DataStore
from modbus_simulator.core.server import Server
from modbus_simulator.gui.widgets.led_indicator import LedIndicator
from modbus_simulator.gui.widgets.register_table import RegisterTableModel

log = logging.getLogger(__name__)


class ServerTab(QWidget):
    status_changed = pyqtSignal(bool)

    def __init__(self, server: Server, datastore: DataStore, session: SessionConfig) -> None:
        super().__init__()
        self._server = server
        self._datastore = datastore
        self._session = session
        self._build()

    def _build(self) -> None:
        # ---- top controls ----
        self._host_label = QLabel(self._server.host)
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(self._server.port)

        self._unit_spin = QSpinBox()
        self._unit_spin.setRange(1, 247)
        self._unit_spin.setValue(self._server.unit_id)

        self._start_btn = QPushButton("Indítás")
        self._start_btn.clicked.connect(self._toggle)

        self._led = LedIndicator()

        controls = QGroupBox("Szerver")
        row = QHBoxLayout(controls)
        row.addWidget(QLabel("Cím:"))
        row.addWidget(self._host_label)
        row.addSpacing(12)
        row.addWidget(QLabel("Port:"))
        row.addWidget(self._port_spin)
        row.addSpacing(12)
        row.addWidget(QLabel("Egység ID:"))
        row.addWidget(self._unit_spin)
        row.addSpacing(12)
        row.addWidget(self._start_btn)
        row.addWidget(self._led)
        row.addStretch(1)

        # ---- register blocks ----
        blocks = QTabWidget()
        for kind, title in [
            (BlockKind.COILS, "Coils (FC 1/5/15)"),
            (BlockKind.DISCRETE_INPUTS, "Discrete Inputs (FC 2)"),
            (BlockKind.HOLDING_REGISTERS, "Holding Registers (FC 3/6/16/22/23)"),
            (BlockKind.INPUT_REGISTERS, "Input Registers (FC 4)"),
        ]:
            view = QTableView()
            model = RegisterTableModel(self._datastore.block(kind))
            view.setModel(model)
            view.setAlternatingRowColors(True)
            vheader = view.verticalHeader()
            if vheader is not None:
                vheader.setDefaultSectionSize(20)
            hheader = view.horizontalHeader()
            if hheader is not None:
                hheader.setStretchLastSection(True)
            blocks.addTab(view, title)

        layout = QVBoxLayout(self)
        layout.addWidget(controls)
        layout.addWidget(blocks, 1)

    # ------------------------------------------------------------------
    def _toggle(self) -> None:
        if self._server.is_running:
            asyncio.create_task(self._stop_task())
        else:
            # Apply edits (port / unit id may have changed). Host is informational
            # only here — changing it requires a full restart at the AppContext level.
            self._server.port = self._port_spin.value()
            self._server.unit_id = self._unit_spin.value()
            if self._server.port < 1024:
                QMessageBox.information(
                    self,
                    "Privilegizált port",
                    f"A(z) {self._server.port}-es port macOS-en rendszergazdai jogot "
                    "igényel. Futtasd az appot sudo-val, vagy válassz 1024 feletti "
                    "portot (pl. 5020).",
                )
            asyncio.create_task(self._start_task())

    async def _start_task(self) -> None:
        self._start_btn.setEnabled(False)
        try:
            await self._server.start()
        except Exception as err:
            log.exception("server start failed")
            QMessageBox.critical(
                self, "Indítás sikertelen", f"A szerver nem indult el: {err}"
            )
            self._start_btn.setEnabled(True)
            return
        self._start_btn.setText("Leállítás")
        self._start_btn.setEnabled(True)
        self._led.set_state(True)
        self.status_changed.emit(True)

    async def _stop_task(self) -> None:
        self._start_btn.setEnabled(False)
        try:
            await self._server.stop()
        except Exception:
            log.exception("server stop failed")
        self._start_btn.setText("Indítás")
        self._start_btn.setEnabled(True)
        self._led.set_state(False)
        self.status_changed.emit(False)

    # ------------------------------------------------------------------
    # Session glue
    # ------------------------------------------------------------------
    def apply_to(self, session: SessionConfig) -> None:
        session.server.port = self._port_spin.value()
        session.server.unit_id = self._unit_spin.value()

    def reload_from(self, session: SessionConfig) -> None:
        self._session = session
        self._port_spin.setValue(session.server.port)
        self._unit_spin.setValue(session.server.unit_id)
        self._host_label.setText(session.server.host)
