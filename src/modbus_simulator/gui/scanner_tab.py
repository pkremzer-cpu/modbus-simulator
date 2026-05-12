"""Unit ID scanner tab — probes unit IDs 1-247 on a given host:port.

Each unit ID is tried with a short-timeout FC 03 read at the chosen address.
The result table shows responsiveness + returned value(s) + error class.
Useful for discovering what's behind a gateway or multi-drop serial bridge
reached over Modbus TCP.
"""

from __future__ import annotations

import asyncio
import logging
import time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from modbus_simulator.core.client import Client, ClientError, ModbusExceptionError

log = logging.getLogger(__name__)


class ScannerTab(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._task: asyncio.Task[None] | None = None
        self._build()

    def _build(self) -> None:
        self._host_edit = QLineEdit("127.0.0.1")
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(502)
        self._addr_spin = QSpinBox()
        self._addr_spin.setRange(0, 65535)
        self._addr_spin.setValue(0)
        self._timeout_spin = QDoubleSpinBox()
        self._timeout_spin.setRange(0.1, 5.0)
        self._timeout_spin.setValue(0.5)
        self._timeout_spin.setSuffix(" s")
        self._from_spin = QSpinBox()
        self._from_spin.setRange(1, 247)
        self._from_spin.setValue(1)
        self._to_spin = QSpinBox()
        self._to_spin.setRange(1, 247)
        self._to_spin.setValue(247)
        self._scan_btn = QPushButton("Scan indítása")
        self._scan_btn.setCheckable(True)
        self._scan_btn.toggled.connect(self._on_toggle)
        self._progress = QProgressBar()
        self._progress.setRange(0, 247)

        conn_box = QGroupBox("Scan paraméterek")
        form = QHBoxLayout(conn_box)
        form.addWidget(QLabel("Cím:"))
        form.addWidget(self._host_edit)
        form.addSpacing(6)
        form.addWidget(QLabel("Port:"))
        form.addWidget(self._port_spin)
        form.addSpacing(6)
        form.addWidget(QLabel("Unit:"))
        form.addWidget(self._from_spin)
        form.addWidget(QLabel("-"))
        form.addWidget(self._to_spin)
        form.addSpacing(6)
        form.addWidget(QLabel("FC03 addr:"))
        form.addWidget(self._addr_spin)
        form.addSpacing(6)
        form.addWidget(QLabel("Timeout:"))
        form.addWidget(self._timeout_spin)
        form.addSpacing(12)
        form.addWidget(self._scan_btn)
        form.addStretch(1)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Unit", "Válasz", "Érték", "Megjegyzés"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        h = self._table.horizontalHeader()
        if h is not None:
            h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            h.setStretchLastSection(True)

        layout = QVBoxLayout(self)
        layout.addWidget(conn_box)
        layout.addWidget(self._progress)
        layout.addWidget(self._table, 1)

    def _on_toggle(self, running: bool) -> None:
        if running:
            self._task = asyncio.create_task(self._scan())
            self._scan_btn.setText("Scan leállítása")
        else:
            if self._task is not None:
                self._task.cancel()
                self._task = None
            self._scan_btn.setText("Scan indítása")

    async def _scan(self) -> None:
        host = self._host_edit.text().strip() or "127.0.0.1"
        port = self._port_spin.value()
        addr = self._addr_spin.value()
        timeout_s = self._timeout_spin.value()
        units = list(range(self._from_spin.value(), self._to_spin.value() + 1))
        self._table.setRowCount(0)
        self._progress.setRange(0, len(units))
        self._progress.setValue(0)

        try:
            for idx, unit in enumerate(units):
                status, value_text, note = await self._probe(host, port, unit, addr, timeout_s)
                r = self._table.rowCount()
                self._table.insertRow(r)
                self._table.setItem(r, 0, QTableWidgetItem(str(unit)))
                status_item = QTableWidgetItem(status)
                if status == "OK":
                    status_item.setForeground(Qt.GlobalColor.darkGreen)
                elif status == "timeout":
                    status_item.setForeground(Qt.GlobalColor.gray)
                else:
                    status_item.setForeground(Qt.GlobalColor.red)
                self._table.setItem(r, 1, status_item)
                self._table.setItem(r, 2, QTableWidgetItem(value_text))
                self._table.setItem(r, 3, QTableWidgetItem(note))
                self._progress.setValue(idx + 1)
        except asyncio.CancelledError:
            pass
        finally:
            self._scan_btn.setChecked(False)
            self._scan_btn.setText("Scan indítása")

    async def _probe(
        self, host: str, port: int, unit: int, addr: int, timeout_s: float
    ) -> tuple[str, str, str]:
        """Return (status, value_text, note) for a single unit."""
        client = Client(host=host, port=port, unit_id=unit, timeout=timeout_s)
        start = time.monotonic()
        try:
            await client.connect()
        except ClientError as err:
            return "no route", "", str(err)[:80]
        try:
            try:
                values = await client.read_holding_registers(addr, 1)
            except ModbusExceptionError as err:
                return "exception", "", f"0x{err.code:02X}"
            except ClientError as err:
                msg = str(err)
                if "timeout" in msg.lower():
                    return "timeout", "", ""
                return "error", "", msg[:80]
        finally:
            await client.disconnect()
        elapsed = (time.monotonic() - start) * 1000
        return "OK", str(values[0] if values else ""), f"{elapsed:.0f} ms"
