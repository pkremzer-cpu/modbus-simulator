"""Traffic log tab — live table of RX/TX frames with CSV export + breakdown."""

from __future__ import annotations

import contextlib
import logging
import struct
from pathlib import Path
from typing import IO

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from modbus_simulator.core.traffic import TrafficEntry, TrafficLog

log = logging.getLogger(__name__)


class TrafficModel(QAbstractTableModel):
    _HEADERS: tuple[str, ...] = (
        "Idő",
        "Irány",
        "Kapcsolat",
        "Egység",
        "FC",
        "Kivétel",
        "Cím",
        "Darab",
        "Nyers",
    )

    rows_changed = pyqtSignal()

    def __init__(self, traffic: TrafficLog) -> None:
        super().__init__()
        self._traffic = traffic
        self._entries: list[TrafficEntry] = list(traffic.snapshot())
        traffic.add_entry_listener(self._on_entry)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._HEADERS[section]
        return str(section)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        entry = self._entries[index.row()]
        col = index.column()
        if col == 0:
            return entry.timestamp.strftime("%H:%M:%S.%f")[:-3]
        if col == 1:
            return entry.direction.value.upper()
        if col == 2:
            return entry.peer
        if col == 3:
            return entry.unit_id
        if col == 4:
            return f"0x{entry.function_code:02X}"
        if col == 5:
            return "" if entry.exception_code is None else f"0x{entry.exception_code:02X}"
        if col == 6:
            return "" if entry.address is None else entry.address
        if col == 7:
            return "" if entry.count is None else entry.count
        if col == 8:
            return entry.raw_hex
        return None

    def _on_entry(self, entry: TrafficEntry) -> None:
        row = len(self._entries)
        self.beginInsertRows(QModelIndex(), row, row)
        self._entries.append(entry)
        self.endInsertRows()
        # Enforce circular buffer: snapshot size may be less if old entries were evicted.
        snapshot_size = self._traffic.size
        if len(self._entries) > snapshot_size:
            to_remove = len(self._entries) - snapshot_size
            self.beginRemoveRows(QModelIndex(), 0, to_remove - 1)
            del self._entries[:to_remove]
            self.endRemoveRows()
        self.rows_changed.emit()

    def reset_from_log(self) -> None:
        self.beginResetModel()
        self._entries = list(self._traffic.snapshot())
        self.endResetModel()


class TrafficTab(QWidget):
    def __init__(self, traffic: TrafficLog) -> None:
        super().__init__()
        self._traffic = traffic
        self._model = TrafficModel(traffic)
        self._csv_stream_file: IO[str] | None = None
        self._csv_stream_path: Path | None = None

        self._view = QTableView()
        self._view.setModel(self._model)
        vheader = self._view.verticalHeader()
        if vheader is not None:
            vheader.setDefaultSectionSize(20)
        header = self._view.horizontalHeader()
        if header is not None:
            header.setStretchLastSection(True)
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        sel = self._view.selectionModel()
        if sel is not None:
            sel.currentRowChanged.connect(self._on_row_changed)

        # Frame breakdown pane — decodes the selected row's MBAP+PDU
        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        mono = QFont(self._detail.font())
        mono.setFamily("Menlo")
        self._detail.setFont(mono)
        self._detail.setMinimumHeight(160)
        self._detail.setHtml(
            "<i>Jelölj ki egy sort a fenti táblázatban a keret részletes "
            "dekódolásához.</i>"
        )

        self._clear_btn = QPushButton("Törlés")
        self._clear_btn.clicked.connect(self._on_clear)
        self._export_btn = QPushButton("CSV exportálás…")
        self._export_btn.clicked.connect(self._on_export)
        self._autoscroll_btn = QPushButton("Automata görgetés: BE")
        self._autoscroll_btn.setCheckable(True)
        self._autoscroll_btn.setChecked(True)
        self._autoscroll_btn.toggled.connect(self._on_autoscroll)
        self._stream_btn = QPushButton("CSV stream: KI")
        self._stream_btn.setCheckable(True)
        self._stream_btn.toggled.connect(self._on_toggle_stream)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self._clear_btn)
        toolbar.addWidget(self._export_btn)
        toolbar.addWidget(self._autoscroll_btn)
        toolbar.addWidget(self._stream_btn)
        toolbar.addStretch(1)

        self._model.rows_changed.connect(self._maybe_scroll)
        traffic.add_entry_listener(self._on_entry_for_stream)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._view)
        splitter.addWidget(self._detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(splitter, 1)

    # ------------------------------------------------------------------
    def _on_clear(self) -> None:
        self._traffic.clear()
        self._model.reset_from_log()

    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export traffic log", str(Path.home() / "traffic.csv"), "CSV (*.csv)"
        )
        if not path:
            return
        try:
            Path(path).write_text(self._traffic.to_csv(), encoding="utf-8")
        except Exception as err:
            log.exception("csv export failed")
            QMessageBox.critical(self, "Export failed", str(err))

    def _on_autoscroll(self, checked: bool) -> None:
        self._autoscroll_btn.setText(
            "Automata görgetés: BE" if checked else "Automata görgetés: KI"
        )

    def _maybe_scroll(self) -> None:
        if self._autoscroll_btn.isChecked():
            self._view.scrollToBottom()

    # ------------------------------------------------------------------
    # Frame breakdown
    # ------------------------------------------------------------------
    def _on_row_changed(self, current: QModelIndex, _prev: QModelIndex) -> None:
        if not current.isValid():
            return
        entries = self._traffic.snapshot()
        if current.row() >= len(entries):
            return
        entry = entries[current.row()]
        self._detail.setHtml(_frame_breakdown_html(entry))

    # ------------------------------------------------------------------
    # Real-time CSV stream
    # ------------------------------------------------------------------
    def _on_toggle_stream(self, on: bool) -> None:
        if on:
            path, _ = QFileDialog.getSaveFileName(
                self,
                "CSV stream fájl",
                str(Path.home() / "modbus_traffic_stream.csv"),
                "CSV (*.csv)",
            )
            if not path:
                self._stream_btn.setChecked(False)
                return
            target = Path(path)
            try:
                self._csv_stream_file = target.open("a", encoding="utf-8")
                if target.stat().st_size == 0:
                    self._csv_stream_file.write(
                        "timestamp,direction,peer,unit_id,function_code,"
                        "exception_code,address,count,values,raw_hex,notes\n"
                    )
            except OSError as err:
                QMessageBox.critical(self, "Megnyitás sikertelen", str(err))
                self._stream_btn.setChecked(False)
                return
            self._csv_stream_path = target
            self._stream_btn.setText(f"CSV stream: BE ({target.name})")
        else:
            if self._csv_stream_file is not None:
                with contextlib.suppress(OSError):
                    self._csv_stream_file.close()
            self._csv_stream_file = None
            self._csv_stream_path = None
            self._stream_btn.setText("CSV stream: KI")

    def _on_entry_for_stream(self, entry: TrafficEntry) -> None:
        if self._csv_stream_file is None:
            return
        values = " ".join(str(v) for v in entry.values)
        exc = "" if entry.exception_code is None else str(entry.exception_code)
        addr = "" if entry.address is None else str(entry.address)
        count = "" if entry.count is None else str(entry.count)
        row = (
            f"{entry.timestamp.isoformat()},{entry.direction.value},{entry.peer},"
            f"{entry.unit_id},{entry.function_code},{exc},{addr},{count},"
            f'"{values}",{entry.raw_hex},{_csv_escape(entry.notes)}\n'
        )
        try:
            self._csv_stream_file.write(row)
            self._csv_stream_file.flush()
        except OSError:
            log.exception("csv stream write failed — disabling stream")
            self._stream_btn.setChecked(False)


# ---------------------------------------------------------------------------
# Frame breakdown helpers
# ---------------------------------------------------------------------------
_FC_NAMES = {
    0x01: "Read Coils",
    0x02: "Read Discrete Inputs",
    0x03: "Read Holding Registers",
    0x04: "Read Input Registers",
    0x05: "Write Single Coil",
    0x06: "Write Single Register",
    0x08: "Diagnostics",
    0x0F: "Write Multiple Coils",
    0x10: "Write Multiple Registers",
    0x16: "Mask Write Register",
    0x17: "Read/Write Multiple Registers",
    0x18: "Read FIFO Queue",
}

_EXC_NAMES = {
    0x01: "Illegal Function",
    0x02: "Illegal Data Address",
    0x03: "Illegal Data Value",
    0x04: "Slave Device Failure",
    0x05: "Acknowledge",
    0x06: "Slave Busy",
    0x0A: "Gateway Path Unavailable",
    0x0B: "Gateway Target Device Failed",
}


def _csv_escape(s: str) -> str:
    if "," in s or '"' in s or "\n" in s:
        return '"' + s.replace('"', '""') + '"'
    return s


def _frame_breakdown_html(entry: TrafficEntry) -> str:
    raw = bytes.fromhex(entry.raw_hex) if entry.raw_hex else b""
    lines: list[str] = []
    lines.append(
        f"<b>{entry.direction.value.upper()}</b> &nbsp; "
        f"<span style='color:#888'>{entry.timestamp.strftime('%H:%M:%S.%f')[:-3]}</span>"
        f" &nbsp; peer={entry.peer}"
    )
    if not raw:
        lines.append("<i>Nincs nyers hex — a kliens oldali keretek "
                     "szintetikus hex-et kapnak.</i>")
        return "<br>".join(lines)

    # Detect whether raw is MBAP+PDU (server log) or just PDU (client TX synthesis
    # can include MBAP too but with the synthesized tx_id).
    def row(label: str, value: str, hex_bytes: str) -> str:
        return (
            f"<tr><td style='color:#9cdcfe'>{label}</td>"
            f"<td style='color:#ce9178'>{value}</td>"
            f"<td style='color:#888'>{hex_bytes}</td></tr>"
        )

    rows: list[str] = []
    if len(raw) >= 7:
        tx_id, proto, length, unit_id = struct.unpack(">HHHB", raw[:7])
        rows.append(row("Transaction ID", str(tx_id), raw[:2].hex()))
        rows.append(row("Protocol ID", str(proto), raw[2:4].hex()))
        rows.append(row("Length", f"{length} bytes follow", raw[4:6].hex()))
        rows.append(row("Unit ID", str(unit_id), raw[6:7].hex()))

        pdu = raw[7:]
        if pdu:
            fc_byte = pdu[0]
            is_exception = bool(fc_byte & 0x80)
            fc = fc_byte & 0x7F
            fc_name = _FC_NAMES.get(fc, f"FC {fc}")
            if is_exception:
                rows.append(
                    row("Function Code", f"0x{fc:02X} — {fc_name} (EXCEPTION)", f"{fc_byte:02x}")
                )
                if len(pdu) >= 2:
                    ec = pdu[1]
                    rows.append(
                        row("Exception Code", f"0x{ec:02X} — {_EXC_NAMES.get(ec, '?')}",
                            f"{ec:02x}")
                    )
            else:
                rows.append(row("Function Code", f"0x{fc:02X} — {fc_name}", f"{fc_byte:02x}"))
                rows.extend(_decode_pdu_fields(fc, pdu[1:]))
    else:
        rows.append(row("Truncated frame", raw.hex(), raw.hex()))

    lines.append(
        "<table cellpadding='3' cellspacing='0' border='1' "
        "style='border-collapse:collapse;border-color:#3a3a3a'>"
        "<tr><th>Field</th><th>Value</th><th>Hex</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    return (
        "<div style='font-family:Menlo,Monaco,monospace;font-size:12px'>"
        + "<br>".join(lines)
        + "</div>"
    )


def _decode_pdu_fields(fc: int, data: bytes) -> list[str]:
    """Produce HTML <tr> rows for the PDU body keyed by FC."""
    def row(label: str, value: str, hex_bytes: str) -> str:
        return (
            f"<tr><td style='color:#9cdcfe'>{label}</td>"
            f"<td style='color:#ce9178'>{value}</td>"
            f"<td style='color:#888'>{hex_bytes}</td></tr>"
        )

    rows: list[str] = []
    try:
        if fc in (0x01, 0x02, 0x03, 0x04) and len(data) == 4:
            addr, count = struct.unpack(">HH", data)
            rows.append(row("Address", f"{addr} (0x{addr:04X})", data[0:2].hex()))
            rows.append(row("Count", str(count), data[2:4].hex()))
        elif fc in (0x01, 0x02) and len(data) >= 1:
            bc = data[0]
            rows.append(row("Byte count", str(bc), data[0:1].hex()))
            bit_hex = data[1 : 1 + bc].hex()
            rows.append(row("Bits (packed)", bit_hex, bit_hex))
        elif fc in (0x03, 0x04) and len(data) >= 1:
            bc = data[0]
            rows.append(row("Byte count", str(bc), data[0:1].hex()))
            if bc % 2 == 0 and len(data) >= 1 + bc:
                regs = struct.unpack(">" + "H" * (bc // 2), data[1 : 1 + bc])
                rows.append(row("Registers", " ".join(str(r) for r in regs),
                                data[1 : 1 + bc].hex()))
        elif fc == 0x05 and len(data) == 4:
            addr, val = struct.unpack(">HH", data)
            rows.append(row("Address", f"{addr}", data[0:2].hex()))
            if val == 0xFF00:
                state = "ON (0xFF00)"
            elif val == 0:
                state = "OFF (0x0000)"
            else:
                state = f"? 0x{val:04X}"
            rows.append(row("Value", state, data[2:4].hex()))
        elif fc == 0x06 and len(data) == 4:
            addr, val = struct.unpack(">HH", data)
            rows.append(row("Address", f"{addr}", data[0:2].hex()))
            rows.append(row("Value", f"{val} (0x{val:04X})", data[2:4].hex()))
        elif fc == 0x0F and len(data) >= 5:
            addr, count, bc = struct.unpack(">HHB", data[:5])
            rows.append(row("Address", f"{addr}", data[0:2].hex()))
            rows.append(row("Coil count", str(count), data[2:4].hex()))
            rows.append(row("Byte count", str(bc), data[4:5].hex()))
            rows.append(row("Bits", data[5 : 5 + bc].hex(), data[5:].hex()))
        elif fc == 0x10 and len(data) >= 5:
            addr, count, bc = struct.unpack(">HHB", data[:5])
            rows.append(row("Address", f"{addr}", data[0:2].hex()))
            rows.append(row("Register count", str(count), data[2:4].hex()))
            rows.append(row("Byte count", str(bc), data[4:5].hex()))
            if len(data) >= 5 + bc and bc % 2 == 0:
                regs = struct.unpack(">" + "H" * (bc // 2), data[5 : 5 + bc])
                rows.append(row("Values", " ".join(str(r) for r in regs), data[5:].hex()))
        elif fc == 0x16 and len(data) == 6:
            addr, and_mask, or_mask = struct.unpack(">HHH", data)
            rows.append(row("Address", f"{addr}", data[0:2].hex()))
            rows.append(row("AND mask", f"0x{and_mask:04X}", data[2:4].hex()))
            rows.append(row("OR mask", f"0x{or_mask:04X}", data[4:6].hex()))
        elif fc == 0x08 and len(data) >= 2:
            sub, = struct.unpack(">H", data[:2])
            rows.append(row("Sub-function", f"0x{sub:04X}", data[0:2].hex()))
            if len(data) > 2:
                rows.append(row("Data", data[2:].hex(), data[2:].hex()))
        elif data:
            rows.append(row("Data", data.hex(), data.hex()))
    except struct.error:
        rows.append(row("Parse error", "—", data.hex()))
    return rows
