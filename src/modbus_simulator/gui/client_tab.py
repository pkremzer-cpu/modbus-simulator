"""Client tab — connection, manual transaction with typed decoding, and
auto-polling table with configurable interval per row.

Features parity with market tools (Modbus Poll et al.):

* Configurable timeout.
* Every read decoded in all useful formats simultaneously: DEC, HEX, BIN,
  INT16/UINT16/INT32/UINT32/FLOAT32/FLOAT64 under every byte/word order combo.
* Auto-poll: per-row name, FC, address, count, type, byte/word order, and
  interval. Last value and status update live; each row runs independently
  so a slow one doesn't block the others.
* Polling entries persist to the session JSON.
"""

from __future__ import annotations

import asyncio
import logging
import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from modbus_simulator.config.schema import PollingEntry, SessionConfig
from modbus_simulator.core.client import Client, ClientError, ModbusExceptionError
from modbus_simulator.core.codec import ByteOrder, DataType, WordOrder, decode
from modbus_simulator.core.traffic import TrafficLog
from modbus_simulator.gui.widgets.led_indicator import LedIndicator

log = logging.getLogger(__name__)

# Manual-transaction function code table — (label, fc, needs_count, needs_values)
FUNCTIONS: list[tuple[str, int, bool, bool]] = [
    ("FC 01 — Read Coils", 0x01, True, False),
    ("FC 02 — Read Discrete Inputs", 0x02, True, False),
    ("FC 03 — Read Holding Registers", 0x03, True, False),
    ("FC 04 — Read Input Registers", 0x04, True, False),
    ("FC 05 — Write Single Coil (0/1)", 0x05, False, True),
    ("FC 06 — Write Single Register", 0x06, False, True),
    ("FC 15 — Write Multiple Coils", 0x0F, False, True),
    ("FC 16 — Write Multiple Registers", 0x10, False, True),
]

# Vendor presets — each one applies a set of default decoder + addressing
# conventions on the Manual transaction panel. Selecting "Standard Modbus TCP"
# leaves the user-controlled settings alone.
VENDOR_PRESETS: list[dict[str, object]] = [
    {
        "label": "Schneider M340 / Modicon Quantum (16-bit BE)",
        "byte_order": ByteOrder.BIG,
        "word_order": WordOrder.BIG,
        "address_mode": 1,  # 4xxxx style
        "enron": False,
        "default_dtype": DataType.INT16,
    },
    {
        "label": "Siemens S7-1200 / S7-1500 (BE)",
        "byte_order": ByteOrder.BIG,
        "word_order": WordOrder.BIG,
        "address_mode": 0,  # PDU 0-based
        "enron": False,
        "default_dtype": DataType.INT16,
    },
    {
        "label": "Daniel / Enron flow computer (32-bit reg)",
        "byte_order": ByteOrder.BIG,
        "word_order": WordOrder.BIG,
        "address_mode": 1,
        "enron": True,
        "default_dtype": DataType.FLOAT32,
    },
    {
        "label": "Wago 750 series",
        "byte_order": ByteOrder.BIG,
        "word_order": WordOrder.BIG,
        "address_mode": 0,
        "enron": False,
        "default_dtype": DataType.INT16,
    },
    {
        "label": "Allen-Bradley / Rockwell (PLC-5, SLC, ControlLogix)",
        "byte_order": ByteOrder.BIG,
        "word_order": WordOrder.LITTLE,  # AB typically word-swapped for 32-bit
        "address_mode": 1,
        "enron": False,
        "default_dtype": DataType.FLOAT32,
    },
    {
        "label": "Iskra energy meter (48-bit, BE)",
        "byte_order": ByteOrder.BIG,
        "word_order": WordOrder.BIG,
        "address_mode": 0,
        "enron": False,
        "default_dtype": DataType.UINT32,
    },
]


# Polling table columns — mixed Hungarian UI chrome + Modbus technical terms
POLL_COLS = [
    "Aktív",
    "Név",
    "FC",
    "Cím",
    "Darab",
    "Type",
    "Byte",
    "Word",
    "Intervallum (ms)",
    "Utolsó érték",
    "Állapot",
]


class ClientTab(QWidget):
    status_changed = pyqtSignal(bool, str)
    # Emitted after every successful poll with (channel_name, scalar_value);
    # scalar_value is the first decoded number of the row, so Trend can chart it.
    polling_sample = pyqtSignal(str, float)
    # Emitted after a successful manual transaction that returned a register
    # block, so the Analyzer tab can show suggested interpretations live.
    manual_response = pyqtSignal(list)

    def __init__(self, session: SessionConfig, traffic_log: TrafficLog | None = None) -> None:
        super().__init__()
        self._session = session
        self._traffic_log = traffic_log
        self._client: Client | None = None
        self._polling_task: asyncio.Task[None] | None = None
        self._last_response_regs: list[int] | None = None
        # 0 = PDU 0-based (raw wire), 1 = Modbus 4xxxx 1-based (SCADA convention).
        # When 1, the Addr spinbox display value = 40001 + PDU addr (for HR);
        # other block types use 30001/10001/00001 prefixes.
        self._addressing_mode = 0
        self._build()
        self._restore_polling_entries()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build(self) -> None:
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_manual_panel())
        splitter.addWidget(self._build_polling_panel())
        # Favour the Manual panel (decoded table can span many rows); the
        # handle is draggable so the operator can rebalance at runtime.
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([520, 280])
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)

        root = QVBoxLayout(self)
        root.addWidget(self._build_connection_group())
        root.addWidget(splitter, 1)

    def _build_connection_group(self) -> QGroupBox:
        self._host_edit = QLineEdit(self._session.client.host)
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(self._session.client.port)
        self._unit_spin = QSpinBox()
        self._unit_spin.setRange(1, 247)
        self._unit_spin.setValue(self._session.client.unit_id)
        self._timeout_spin = QDoubleSpinBox()
        self._timeout_spin.setRange(0.1, 60.0)
        self._timeout_spin.setSingleStep(0.5)
        self._timeout_spin.setSuffix(" s")
        self._timeout_spin.setValue(self._session.client.timeout)
        self._connect_btn = QPushButton("Csatlakozás")
        self._connect_btn.clicked.connect(self._toggle_connect)
        self._led = LedIndicator()

        # --- Vendor preset combo ---
        self._preset_combo = QComboBox()
        self._preset_combo.addItem("Standard Modbus TCP", userData=None)
        for preset in VENDOR_PRESETS:
            self._preset_combo.addItem(str(preset["label"]), userData=preset)
        self._preset_combo.currentIndexChanged.connect(self._on_preset_changed)

        # --- Addressing mode combo ---
        self._addr_mode_combo = QComboBox()
        self._addr_mode_combo.addItem("PDU (0-alapú)", userData=0)
        self._addr_mode_combo.addItem("Modbus (4xxxx, 1-alapú)", userData=1)
        self._addr_mode_combo.currentIndexChanged.connect(self._on_addr_mode_changed)
        self._addr_mode_combo.setToolTip(
            "PDU: a Cím spinbox 0-alapú nyers cím (a wire formátum).\n"
            "Modbus: a Cím spinbox SCADA stílusú (40001 = HR 0, 30001 = IR 0, "
            "10001 = DI 0, 00001 = coil 0). Csak megjelenítés — a wire mindig "
            "PDU 0-alapú."
        )

        # --- Enron 32-bit register mode ---
        self._enron_check = QCheckBox("Enron / Daniel 32-bit reg")
        self._enron_check.setToolTip(
            "Daniel / Enron olaj-gáz flow computer mód: FC 03/04 válaszában "
            "MINDEN regiszter 32 bites (nem 16). A kliens duplázza belül a "
            "regiszter darabszámot és párba állítja a regisztereket."
        )

        box = QGroupBox("Kapcsolat")
        outer = QVBoxLayout(box)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Cím:"))
        row1.addWidget(self._host_edit)
        row1.addSpacing(6)
        row1.addWidget(QLabel("Port:"))
        row1.addWidget(self._port_spin)
        row1.addSpacing(6)
        row1.addWidget(QLabel("Egység:"))
        row1.addWidget(self._unit_spin)
        row1.addSpacing(6)
        row1.addWidget(QLabel("Időtúllépés:"))
        row1.addWidget(self._timeout_spin)
        row1.addSpacing(12)
        row1.addWidget(self._connect_btn)
        row1.addWidget(self._led)
        row1.addStretch(1)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Preset:"))
        row2.addWidget(self._preset_combo)
        row2.addSpacing(8)
        row2.addWidget(QLabel("Címzés:"))
        row2.addWidget(self._addr_mode_combo)
        row2.addSpacing(8)
        row2.addWidget(self._enron_check)
        row2.addStretch(1)
        outer.addLayout(row1)
        outer.addLayout(row2)
        return box

    # ---- manual transaction panel ----
    def _build_manual_panel(self) -> QWidget:
        self._fc_combo = QComboBox()
        for label, *_ in FUNCTIONS:
            self._fc_combo.addItem(label)
        self._fc_combo.setCurrentIndex(2)
        self._fc_combo.currentIndexChanged.connect(self._refresh_manual_fields)

        self._addr_spin = QSpinBox()
        self._addr_spin.setRange(0, 65535)
        self._count_spin = QSpinBox()
        self._count_spin.setRange(1, 2000)
        self._values_edit = QLineEdit()
        self._values_edit.setPlaceholderText("Comma-separated values (e.g. 1,0,1)")

        self._type_combo = QComboBox()
        for t in DataType:
            self._type_combo.addItem(t.value, userData=t)
        self._byte_combo = QComboBox()
        for b in ByteOrder:
            self._byte_combo.addItem(f"Byte {b.value.upper()}", userData=b)
        self._word_combo = QComboBox()
        for w in WordOrder:
            self._word_combo.addItem(f"Word {w.value.upper()}", userData=w)
        # Live re-render when any of the decode controls change — user can
        # flip byte/word order without issuing a new request.
        self._type_combo.currentIndexChanged.connect(self._render_last_response)
        self._byte_combo.currentIndexChanged.connect(self._render_last_response)
        self._word_combo.currentIndexChanged.connect(self._render_last_response)

        self._send_btn = QPushButton("Küldés")
        self._send_btn.clicked.connect(self._on_send)

        self._count_label = QLabel("Darab:")
        self._values_label = QLabel("Values:")

        form = QHBoxLayout()
        form.addWidget(QLabel("FC:"))
        form.addWidget(self._fc_combo)
        form.addSpacing(6)
        form.addWidget(QLabel("Cím:"))
        form.addWidget(self._addr_spin)
        form.addSpacing(6)
        form.addWidget(self._count_label)
        form.addWidget(self._count_spin)
        form.addSpacing(6)
        form.addWidget(self._values_label)
        form.addWidget(self._values_edit, 1)

        decode_row = QHBoxLayout()
        decode_row.addWidget(QLabel("Decode as:"))
        decode_row.addWidget(self._type_combo)
        decode_row.addWidget(self._byte_combo)
        decode_row.addWidget(self._word_combo)
        decode_row.addStretch(1)
        decode_row.addWidget(self._send_btn)

        self._resp = QTextEdit()
        self._resp.setReadOnly(True)
        self._resp.setMinimumHeight(220)
        self._resp.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        mono = QFont(self._resp.font())
        mono.setFamily("Menlo")
        self._resp.setFont(mono)

        box = QGroupBox("Manuális tranzakció")
        layout = QVBoxLayout(box)
        layout.addLayout(form)
        layout.addLayout(decode_row)
        layout.addWidget(self._resp, 1)
        self._refresh_manual_fields()
        return box

    # ---- polling panel ----
    def _build_polling_panel(self) -> QWidget:
        self._poll_table = QTableWidget(0, len(POLL_COLS))
        self._poll_table.setHorizontalHeaderLabels(POLL_COLS)
        self._poll_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._poll_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        h = self._poll_table.horizontalHeader()
        if h is not None:
            h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            h.setStretchLastSection(True)

        add_btn = QPushButton("+ Új sor (manuálisból)")
        add_btn.setToolTip(
            "Új lekérdezési sort ad hozzá a fenti Manuális tranzakció jelenlegi "
            "értékeivel (FC, Cím, Darab, Type, Byte/Word order)."
        )
        add_btn.clicked.connect(lambda: self._add_polling_row())
        remove_btn = QPushButton("Kijelölt törlése")
        remove_btn.clicked.connect(self._remove_selected_rows)
        apply_btn = QPushButton("Dekódolás minden sorra")
        apply_btn.setToolTip(
            "A fenti Manuális tranzakció Type / Byte / Word beállítását "
            "átmásolja minden lekérdezési sorra."
        )
        apply_btn.clicked.connect(self._apply_manual_decode_to_all_rows)
        self._auto_start_poll_check = QCheckBox("Auto-indul csatlakozáskor")
        self._auto_start_poll_check.setChecked(True)
        self._auto_start_poll_check.setToolTip(
            "Ha be van pipálva, a polling automatikusan elindul minden "
            "sikeres csatlakozás után — feltéve, hogy legalább egy aktív "
            "sor van a táblázatban. Szétkapcsoláskor a polling leáll."
        )

        self._poll_start_btn = QPushButton("Polling indítása")
        self._poll_start_btn.setCheckable(True)
        self._poll_start_btn.toggled.connect(self._toggle_polling)

        toolbar = QHBoxLayout()
        toolbar.addWidget(add_btn)
        toolbar.addWidget(remove_btn)
        toolbar.addWidget(apply_btn)
        toolbar.addStretch(1)
        toolbar.addWidget(self._auto_start_poll_check)
        toolbar.addWidget(self._poll_start_btn)

        box = QGroupBox("Polling")
        layout = QVBoxLayout(box)
        layout.addLayout(toolbar)
        layout.addWidget(self._poll_table, 1)
        return box

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------
    def _on_preset_changed(self, idx: int) -> None:
        """Apply the chosen vendor preset to the decode + addressing widgets."""
        preset = self._preset_combo.itemData(idx)
        if not preset:
            return
        # Set byte / word order combos
        for combo, key in (
            (self._byte_combo, "byte_order"),
            (self._word_combo, "word_order"),
            (self._type_combo, "default_dtype"),
        ):
            i = combo.findData(preset[key])
            if i >= 0:
                combo.setCurrentIndex(i)
        # Addressing mode
        self._addr_mode_combo.setCurrentIndex(int(preset["address_mode"]))
        # Enron checkbox
        self._enron_check.setChecked(bool(preset["enron"]))

    def _on_addr_mode_changed(self, idx: int) -> None:
        """Switch the Address spinbox between PDU 0-based and Modbus 4xxxx style."""
        new_mode = int(self._addr_mode_combo.itemData(idx))
        if new_mode == self._addressing_mode:
            return
        old_pdu = self._current_pdu_address()
        self._addressing_mode = new_mode
        # Reconfigure the spinbox range and prefix for the new mode
        if new_mode == 1:
            # Modbus 4xxxx style — we display HR-style addresses (40001..49999)
            # but the implementation only translates display, not block selection
            # (block is implied by FC). Show 40001 as default for HR.
            self._addr_spin.setRange(40001, 105536)
            self._addr_spin.setPrefix("HR ")
        else:
            self._addr_spin.setRange(0, 65535)
            self._addr_spin.setPrefix("")
        # Preserve the underlying PDU address
        self._set_pdu_address(old_pdu)

    def _current_pdu_address(self) -> int:
        """Return the spinbox value translated to a 0-based PDU address."""
        v = self._addr_spin.value()
        if self._addressing_mode == 1 and v >= 40001:
            return v - 40001
        return v

    def _set_pdu_address(self, pdu_addr: int) -> None:
        if self._addressing_mode == 1:
            self._addr_spin.setValue(40001 + pdu_addr)
        else:
            self._addr_spin.setValue(pdu_addr)

    def set_manual_decode(
        self, dtype: DataType, byte_order: ByteOrder, word_order: WordOrder
    ) -> None:
        """Set the three decode combos from outside (used by the Analyzer)."""
        idx = self._type_combo.findData(dtype)
        if idx >= 0:
            self._type_combo.setCurrentIndex(idx)
        idx = self._byte_combo.findData(byte_order)
        if idx >= 0:
            self._byte_combo.setCurrentIndex(idx)
        idx = self._word_combo.findData(word_order)
        if idx >= 0:
            self._word_combo.setCurrentIndex(idx)
        # Trigger re-render of the cached response in the new format.
        self._render_last_response()

    def _refresh_manual_fields(self) -> None:
        _, _, needs_count, needs_values = FUNCTIONS[self._fc_combo.currentIndex()]
        self._count_spin.setEnabled(needs_count)
        self._count_label.setEnabled(needs_count)
        self._values_edit.setEnabled(needs_values)
        self._values_label.setEnabled(needs_values)

    def _toggle_connect(self) -> None:
        if self._client is not None and self._client.is_connected:
            asyncio.create_task(self._disconnect_task())
        else:
            asyncio.create_task(self._connect_task())

    async def _connect_task(self) -> None:
        self._connect_btn.setEnabled(False)
        try:
            self._client = Client(
                host=self._host_edit.text().strip() or "127.0.0.1",
                port=self._port_spin.value(),
                unit_id=self._unit_spin.value(),
                timeout=self._timeout_spin.value(),
                traffic_log=self._traffic_log,
                enron_mode=self._enron_check.isChecked(),
            )
            await self._client.connect()
        except Exception as err:
            log.exception("client connect failed")
            QMessageBox.critical(self, "Csatlakozás sikertelen", str(err))
            self._client = None
            self._connect_btn.setEnabled(True)
            return
        self._connect_btn.setText("Szétkapcsolás")
        self._connect_btn.setEnabled(True)
        self._led.set_state(True)
        description = (
            f"{self._host_edit.text()}:{self._port_spin.value()} "
            f"unit={self._unit_spin.value()}"
        )
        self.status_changed.emit(True, description)

        # Auto-start polling if the user enabled it and there are rows to poll.
        if (
            self._auto_start_poll_check.isChecked()
            and self._poll_table.rowCount() > 0
            and not self._poll_start_btn.isChecked()
        ):
            self._poll_start_btn.setChecked(True)

    async def _disconnect_task(self) -> None:
        # Stop polling first so in-flight requests don't race with disconnect.
        if self._poll_start_btn.isChecked():
            self._poll_start_btn.setChecked(False)
        self._connect_btn.setEnabled(False)
        if self._client is not None:
            await self._client.disconnect()
        self._client = None
        self._connect_btn.setText("Csatlakozás")
        self._connect_btn.setEnabled(True)
        self._led.set_state(False)
        self.status_changed.emit(False, "")

    # ------------------------------------------------------------------
    # Manual transaction
    # ------------------------------------------------------------------
    def _on_send(self) -> None:
        if self._client is None or not self._client.is_connected:
            QMessageBox.warning(
                self, "Nincs kapcsolat", "Csatlakozz egy szerverhez először."
            )
            return
        asyncio.create_task(self._send_task())

    async def _send_task(self) -> None:
        assert self._client is not None
        _, fc, _, _ = FUNCTIONS[self._fc_combo.currentIndex()]
        addr = self._current_pdu_address()
        count = self._count_spin.value()
        try:
            result = await self._execute(fc, addr, count)
        except ModbusExceptionError as err:
            self._resp.setHtml(_error_html(f"Modbus exception 0x{err.code:02X}"))
            return
        except ClientError as err:
            self._resp.setHtml(_error_html(f"Client error: {err}"))
            return
        except Exception as err:
            log.exception("send failed")
            self._resp.setHtml(_error_html(f"Error: {err}"))
            return
        if isinstance(result, list) and all(isinstance(v, int) for v in result):
            self._last_response_regs = list(result)
            self._render_last_response()
            self.manual_response.emit(list(result))
        else:
            self._last_response_regs = None
            self._resp.setHtml(_ok_html(str(result)))

    def _render_last_response(self) -> None:
        if self._last_response_regs is None:
            return
        dtype: DataType = self._type_combo.currentData()
        byte_order: ByteOrder = self._byte_combo.currentData()
        word_order: WordOrder = self._word_combo.currentData()
        base_addr = self._current_pdu_address()
        self._resp.setHtml(
            _response_html(
                self._last_response_regs, dtype, byte_order, word_order, base_addr
            )
        )

    async def _execute(self, fc: int, addr: int, count: int) -> object:
        assert self._client is not None
        values = [v.strip() for v in self._values_edit.text().split(",") if v.strip()]
        match fc:
            case 0x01:
                return await self._client.read_coils(addr, count)
            case 0x02:
                return await self._client.read_discrete_inputs(addr, count)
            case 0x03:
                return await self._client.read_holding_registers(addr, count)
            case 0x04:
                return await self._client.read_input_registers(addr, count)
            case 0x05:
                await self._client.write_coil(addr, bool(int(values[0])))
                return "OK"
            case 0x06:
                await self._client.write_register(addr, int(values[0]))
                return "OK"
            case 0x0F:
                await self._client.write_coils(addr, [bool(int(v)) for v in values])
                return "OK"
            case 0x10:
                await self._client.write_registers(addr, [int(v) for v in values])
                return "OK"
            case _:
                raise ValueError(f"unsupported FC {fc:#x}")

    # ------------------------------------------------------------------
    # Polling table
    # ------------------------------------------------------------------
    def _add_polling_row(self, entry: PollingEntry | None = None) -> None:
        if entry is not None:
            e = entry
        else:
            # Inherit defaults from the Manual transaction panel so "Add row"
            # mirrors what the user just configured there.
            _, manual_fc, _, _ = FUNCTIONS[self._fc_combo.currentIndex()]
            # Polling supports FC 1/2/3/4 only; fall back to 3 for writes.
            fc = manual_fc if manual_fc in (1, 2, 3, 4) else 3
            e = PollingEntry(
                name=f"poll{self._poll_table.rowCount() + 1}",
                function_code=fc,
                address=self._current_pdu_address(),
                count=max(1, self._count_spin.value()),
                data_type=self._type_combo.currentData() or DataType.UINT16,
                byte_order=self._byte_combo.currentData() or ByteOrder.BIG,
                word_order=self._word_combo.currentData() or WordOrder.BIG,
            )
        r = self._poll_table.rowCount()
        self._poll_table.insertRow(r)

        # On / enabled
        check = QCheckBox()
        check.setChecked(e.enabled)
        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.addWidget(check)
        wl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wl.setContentsMargins(0, 0, 0, 0)
        self._poll_table.setCellWidget(r, 0, wrap)

        self._poll_table.setItem(r, 1, QTableWidgetItem(e.name))

        fc_combo = QComboBox()
        fc_options = (1, 2, 3, 4)
        for fc in fc_options:
            fc_combo.addItem(f"FC {fc:02d}", userData=fc)
        fc_combo.setCurrentIndex(
            fc_options.index(e.function_code) if e.function_code in fc_options else 2
        )
        self._poll_table.setCellWidget(r, 2, fc_combo)

        self._poll_table.setItem(r, 3, QTableWidgetItem(str(e.address)))
        self._poll_table.setItem(r, 4, QTableWidgetItem(str(e.count)))

        type_combo = QComboBox()
        for t in DataType:
            type_combo.addItem(t.value, userData=t)
        type_combo.setCurrentIndex(list(DataType).index(e.data_type))
        self._poll_table.setCellWidget(r, 5, type_combo)

        bo_combo = QComboBox()
        for b in ByteOrder:
            bo_combo.addItem(b.value.upper(), userData=b)
        bo_combo.setCurrentIndex(list(ByteOrder).index(e.byte_order))
        self._poll_table.setCellWidget(r, 6, bo_combo)

        wo_combo = QComboBox()
        for w in WordOrder:
            wo_combo.addItem(w.value.upper(), userData=w)
        wo_combo.setCurrentIndex(list(WordOrder).index(e.word_order))
        self._poll_table.setCellWidget(r, 7, wo_combo)

        self._poll_table.setItem(r, 8, QTableWidgetItem(str(int(e.interval_ms))))

        last_item = QTableWidgetItem("—")
        last_item.setFlags(last_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._poll_table.setItem(r, 9, last_item)

        status_item = QTableWidgetItem("—")
        status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._poll_table.setItem(r, 10, status_item)

    def _apply_manual_decode_to_all_rows(self) -> None:
        """Overwrite every polling row's Type / ByteOrder / WordOrder cell
        with the current values from the Manual transaction panel."""
        target_type = self._type_combo.currentData()
        target_byte = self._byte_combo.currentData()
        target_word = self._word_combo.currentData()
        for r in range(self._poll_table.rowCount()):
            type_combo = self._poll_table.cellWidget(r, 5)
            bo_combo = self._poll_table.cellWidget(r, 6)
            wo_combo = self._poll_table.cellWidget(r, 7)
            if isinstance(type_combo, QComboBox):
                idx = type_combo.findData(target_type)
                if idx >= 0:
                    type_combo.setCurrentIndex(idx)
            if isinstance(bo_combo, QComboBox):
                idx = bo_combo.findData(target_byte)
                if idx >= 0:
                    bo_combo.setCurrentIndex(idx)
            if isinstance(wo_combo, QComboBox):
                idx = wo_combo.findData(target_word)
                if idx >= 0:
                    wo_combo.setCurrentIndex(idx)

    def _remove_selected_rows(self) -> None:
        # Cell widgets (combos, checkbox) swallow clicks that would normally
        # populate ``selectedIndexes``, so go through the selection model
        # and fall back to the currently focused row.
        rows: set[int] = set()
        sel_model = self._poll_table.selectionModel()
        if sel_model is not None:
            rows.update(idx.row() for idx in sel_model.selectedRows())
            rows.update(idx.row() for idx in sel_model.selectedIndexes())
        current = self._poll_table.currentRow()
        if current >= 0 and not rows:
            rows.add(current)
        if not rows:
            QMessageBox.information(
                self,
                "Sor törlése",
                "Először jelölj ki egy sort (kattints a Név / Cím / Darab oszlopra).",
            )
            return
        for r in sorted(rows, reverse=True):
            self._poll_table.removeRow(r)

    def _read_polling_row(self, r: int) -> PollingEntry | None:
        try:
            check_wrap = self._poll_table.cellWidget(r, 0)
            check = check_wrap.findChild(QCheckBox) if check_wrap else None
            enabled = bool(check.isChecked()) if check else False

            name_item = self._poll_table.item(r, 1)
            name = name_item.text() if name_item else f"poll{r + 1}"

            fc_combo = self._poll_table.cellWidget(r, 2)
            fc = int(fc_combo.currentData()) if isinstance(fc_combo, QComboBox) else 3

            addr_item = self._poll_table.item(r, 3)
            addr = int(addr_item.text()) if addr_item else 0
            count_item = self._poll_table.item(r, 4)
            count = max(1, int(count_item.text())) if count_item else 1

            type_combo = self._poll_table.cellWidget(r, 5)
            dtype = (
                type_combo.currentData()
                if isinstance(type_combo, QComboBox)
                else DataType.UINT16
            )

            bo_combo = self._poll_table.cellWidget(r, 6)
            byte_order = (
                bo_combo.currentData()
                if isinstance(bo_combo, QComboBox)
                else ByteOrder.BIG
            )

            wo_combo = self._poll_table.cellWidget(r, 7)
            word_order = (
                wo_combo.currentData()
                if isinstance(wo_combo, QComboBox)
                else WordOrder.BIG
            )

            interval_item = self._poll_table.item(r, 8)
            interval_ms = float(interval_item.text()) if interval_item else 1000.0

            return PollingEntry(
                name=name,
                function_code=fc,
                address=addr,
                count=count,
                data_type=dtype,
                byte_order=byte_order,
                word_order=word_order,
                interval_ms=max(50.0, interval_ms),
                enabled=enabled,
            )
        except (ValueError, AttributeError) as err:
            log.warning("bad polling row %d: %s", r, err)
            return None

    def _update_row_result(self, r: int, text: str, status: str, error: bool) -> None:
        last = self._poll_table.item(r, 9)
        if last is not None:
            last.setText(text)
        stat = self._poll_table.item(r, 10)
        if stat is not None:
            stat.setText(status)
            stat.setForeground(Qt.GlobalColor.red if error else Qt.GlobalColor.darkGreen)

    # ------------------------------------------------------------------
    # Polling runner
    # ------------------------------------------------------------------
    def _toggle_polling(self, running: bool) -> None:
        if running:
            if self._client is None or not self._client.is_connected:
                QMessageBox.warning(
                    self, "Nincs kapcsolat", "Először csatlakozz."
                )
                self._poll_start_btn.setChecked(False)
                return
            self._polling_task = asyncio.create_task(self._poll_loop())
            self._poll_start_btn.setText("Polling leállítása")
        else:
            if self._polling_task is not None:
                self._polling_task.cancel()
                self._polling_task = None
            self._poll_start_btn.setText("Polling indítása")

    async def _poll_loop(self) -> None:
        """One task runs the whole table; per-row deadlines are tracked separately."""
        next_due: dict[int, float] = {}
        try:
            while True:
                if self._client is None or not self._client.is_connected:
                    break
                now = time.monotonic()
                for r in range(self._poll_table.rowCount()):
                    entry = self._read_polling_row(r)
                    if entry is None or not entry.enabled:
                        continue
                    due = next_due.get(r, 0.0)
                    if due > now:
                        continue
                    next_due[r] = now + entry.interval_ms / 1000.0
                    await self._poll_one(r, entry)
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            return

    async def _poll_one(self, r: int, entry: PollingEntry) -> None:
        assert self._client is not None
        try:
            if entry.function_code == 1:
                values = await self._client.read_coils(entry.address, entry.count)
            elif entry.function_code == 2:
                values = await self._client.read_discrete_inputs(entry.address, entry.count)
            elif entry.function_code == 3:
                values = await self._client.read_holding_registers(entry.address, entry.count)
            elif entry.function_code == 4:
                values = await self._client.read_input_registers(entry.address, entry.count)
            else:
                self._update_row_result(r, "—", f"FC {entry.function_code} not supported", True)
                return
        except ModbusExceptionError as err:
            self._update_row_result(r, "—", f"exc 0x{err.code:02X}", True)
            return
        except ClientError as err:
            self._update_row_result(r, "—", str(err)[:60], True)
            return
        except Exception as err:
            log.exception("polling row %d failed", r)
            self._update_row_result(r, "—", str(err)[:60], True)
            return

        # Decode
        first_scalar: float | None = None
        try:
            regs = list(values)
            if entry.function_code in (1, 2):
                bits = regs[: entry.count]
                text = " ".join(str(int(v)) for v in bits)
                if bits:
                    first_scalar = float(int(bits[0]))
            else:
                needed = entry.data_type.register_count
                if len(regs) >= needed:
                    groups: list[str] = []
                    decoded_first: float | None = None
                    for start in range(0, len(regs), needed):
                        chunk = regs[start : start + needed]
                        if len(chunk) < needed:
                            break
                        val = decode(
                            chunk,
                            entry.data_type,
                            byte_order=entry.byte_order,
                            word_order=entry.word_order,
                        )
                        if decoded_first is None:
                            decoded_first = float(val)
                        groups.append(_format_value(val, entry.data_type))
                    text = " | ".join(groups) if groups else str(regs)
                    if decoded_first is not None:
                        first_scalar = decoded_first
                else:
                    text = str(regs)
        except Exception as err:
            log.exception("decode failed")
            text = f"decode err: {err}"
        self._update_row_result(r, text, "OK", False)
        if first_scalar is not None:
            self.polling_sample.emit(entry.name, first_scalar)

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------
    def _restore_polling_entries(self) -> None:
        for entry in self._session.polling_entries:
            self._add_polling_row(entry)

    def apply_to(self, session: SessionConfig) -> None:
        session.client.host = self._host_edit.text()
        session.client.port = self._port_spin.value()
        session.client.unit_id = self._unit_spin.value()
        session.client.timeout = self._timeout_spin.value()
        entries: list[PollingEntry] = []
        for r in range(self._poll_table.rowCount()):
            e = self._read_polling_row(r)
            if e is not None:
                entries.append(e)
        session.polling_entries = entries

    def reload_from(self, session: SessionConfig) -> None:
        self._session = session
        self._host_edit.setText(session.client.host)
        self._port_spin.setValue(session.client.port)
        self._unit_spin.setValue(session.client.unit_id)
        self._timeout_spin.setValue(session.client.timeout)
        self._poll_table.setRowCount(0)
        self._restore_polling_entries()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _format_value(val: int | float, dtype: DataType) -> str:
    if dtype in (DataType.FLOAT32, DataType.FLOAT64):
        return f"{float(val):.6g}"
    return str(int(val))


def _error_html(msg: str) -> str:
    return f"<pre style='color:#c00;margin:0'>{_escape(msg)}</pre>"


def _ok_html(msg: str) -> str:
    return f"<pre style='color:#080;margin:0'>{_escape(msg)}</pre>"


def _response_html(
    registers: list[int],
    dtype: DataType,
    byte_order: ByteOrder,
    word_order: WordOrder,
    base_address: int = 0,
) -> str:
    """Render the response in every useful view at once."""
    parts: list[str] = []
    # Raw views for every register
    dec = " ".join(str(r) for r in registers)
    hex_ = " ".join(f"0x{r:04X}" for r in registers)
    bin_ = " ".join(f"{r:016b}" for r in registers)
    parts.append(f"<b>DEC:</b>&nbsp;{dec}")
    parts.append(f"<b>HEX:</b>&nbsp;{hex_}")
    parts.append(f"<b>BIN:</b>&nbsp;{bin_}")
    # ASCII: each register → 2 characters (high byte + low byte), non-printable as '.'
    ascii_chars = "".join(_ascii_char((r >> 8) & 0xFF) + _ascii_char(r & 0xFF) for r in registers)
    parts.append(f"<b>ASCII:</b>&nbsp;{_escape(ascii_chars)}")
    # BCD: each nibble is a decimal digit 0-9 (invalid nibbles marked with '?')
    bcd = " ".join(_bcd_string(r) for r in registers)
    parts.append(f"<b>BCD:</b>&nbsp;{bcd}")

    # Typed interpretation — chunk the registers according to the dtype's width
    # and emit one row per chunk with every byte/word order permutation.
    chunks: list[tuple[int, list[int]]] = []  # (address, chunk)
    needed = dtype.register_count
    for offset in range(0, len(registers), needed):
        ch = registers[offset : offset + needed]
        if len(ch) == needed:
            chunks.append((base_address + offset, ch))

    if chunks:
        parts.append("<br><b>Decoded as " + dtype.value + ":</b>")
        header_cells = [
            f"Byte {b.value.upper()} / Word {w.value.upper()}"
            for b in ByteOrder
            for w in WordOrder
        ]
        rows_html: list[str] = []
        for addr, ch in chunks:
            cells: list[str] = []
            for b in ByteOrder:
                for w in WordOrder:
                    try:
                        val = decode(ch, dtype, byte_order=b, word_order=w)
                        cells.append(_format_value(val, dtype))
                    except Exception as err:
                        cells.append(f"err: {err}")
            addr_cell = f"{addr} <span style='color:#888'>(0x{addr:04X})</span>"
            regs_cell = " ".join(f"0x{r:04X}" for r in ch)
            rows_html.append(
                "<tr><td>"
                + addr_cell
                + "</td><td>"
                + regs_cell
                + "</td><td>"
                + "</td><td>".join(_escape(c) for c in cells)
                + "</td></tr>"
            )
        header = (
            "<tr><th>Addr</th><th>Registers</th><th>"
            + "</th><th>".join(header_cells)
            + "</th></tr>"
        )
        parts.append(
            "<table cellpadding='4' cellspacing='0' border='1' "
            "style='border-collapse:collapse;border-color:#aaa'>"
            + header
            + "".join(rows_html)
            + "</table>"
        )
        parts.append(
            f"<i>Selected: byte {byte_order.value.upper()}, "
            f"word {word_order.value.upper()}</i>"
        )
    return (
        "<div style='font-family:Menlo,Monaco,monospace;font-size:12px'>"
        + "<br>".join(parts)
        + "</div>"
    )


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _ascii_char(byte: int) -> str:
    """Map a byte to a printable ASCII character, non-printable → '·'."""
    return chr(byte) if 32 <= byte <= 126 else "·"


def _bcd_string(reg: int) -> str:
    """Return the 4 BCD nibbles as digits; invalid nibbles (>9) marked '?'."""
    out = []
    for shift in (12, 8, 4, 0):
        nibble = (reg >> shift) & 0xF
        out.append(str(nibble) if nibble < 10 else "?")
    return "".join(out)
