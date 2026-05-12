"""Main application window.

Owns the server-side ``DataStore``, ``RuleEngine``, ``TrafficLog``, and
``Server`` (one shared set across tabs) plus a per-tab ``Client`` for the
client tab. Each tab receives references to the shared objects and the
``SessionConfig`` so user edits propagate and persist.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabWidget,
)

from modbus_simulator import __version__
from modbus_simulator.config.persistence import load_session, save_session
from modbus_simulator.config.schema import SessionConfig
from modbus_simulator.core.datastore import DataStore, DataStoreConfig
from modbus_simulator.core.exceptions import RuleEngine
from modbus_simulator.core.server import Server
from modbus_simulator.core.traffic import TrafficLog
from modbus_simulator.gui.analyzer_tab import AnalyzerTab
from modbus_simulator.gui.client_tab import ClientTab
from modbus_simulator.gui.exceptions_tab import ExceptionsTab
from modbus_simulator.gui.scanner_tab import ScannerTab
from modbus_simulator.gui.server_tab import ServerTab
from modbus_simulator.gui.simulation_tab import SimulationTab
from modbus_simulator.gui.traffic_tab import TrafficTab
from modbus_simulator.gui.trend_tab import TrendTab

log = logging.getLogger(__name__)

AUTOSAVE_INTERVAL_MS = 30_000


class MainWindow(QMainWindow):
    def __init__(self, session: SessionConfig) -> None:
        super().__init__()
        self._session = session

        # Shared server-side state
        self._datastore = DataStore(
            DataStoreConfig(
                coils_size=session.server.coils_size,
                discrete_inputs_size=session.server.discrete_inputs_size,
                holding_registers_size=session.server.holding_registers_size,
                input_registers_size=session.server.input_registers_size,
            )
        )
        self._rules = RuleEngine()
        self._traffic = TrafficLog(max_entries=session.ui.traffic_max_entries)
        self._server = Server(
            host=session.server.host,
            port=session.server.port,
            unit_id=session.server.unit_id,
            datastore=self._datastore,
            rule_engine=self._rules,
            traffic_log=self._traffic,
        )

        self.setWindowTitle("Kremzer Péter ModbusTCP")
        self.resize(1320, 860)

        self._tabs = QTabWidget()
        self._server_tab = ServerTab(self._server, self._datastore, session)
        self._client_tab = ClientTab(session, self._traffic)
        self._traffic_tab = TrafficTab(self._traffic)
        self._trend_tab = TrendTab(self._datastore)
        self._simulation_tab = SimulationTab(self._datastore, self._server, session)
        self._exceptions_tab = ExceptionsTab(self._rules, session)
        self._scanner_tab = ScannerTab()
        self._analyzer_tab = AnalyzerTab()

        self._tabs.addTab(self._server_tab, "Szerver")
        self._tabs.addTab(self._client_tab, "Kliens")
        self._tabs.addTab(self._scanner_tab, "Scanner")
        self._tabs.addTab(self._analyzer_tab, "Analizátor")
        self._tabs.addTab(self._traffic_tab, "Forgalom")
        self._tabs.addTab(self._trend_tab, "Trend")
        self._tabs.addTab(self._simulation_tab, "Szimuláció")
        self._tabs.addTab(self._exceptions_tab, "Kivétel szabályok")
        self._tabs.setCurrentIndex(min(session.ui.last_tab, self._tabs.count() - 1))

        self.setCentralWidget(self._tabs)

        self._build_menu()
        self._build_status_bar()
        self._wire_status_updates()

        self._autosave = QTimer(self)
        self._autosave.setInterval(AUTOSAVE_INTERVAL_MS)
        self._autosave.timeout.connect(self._autosave_tick)
        self._autosave.start()

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------
    def _build_menu(self) -> None:
        bar = self.menuBar()
        assert bar is not None

        file_menu = bar.addMenu("&Fájl")
        assert file_menu is not None
        act_save = QAction("Munkamenet &mentése…", self)
        act_save.setShortcut(QKeySequence.StandardKey.Save)
        act_save.triggered.connect(self._on_save_as)
        file_menu.addAction(act_save)

        act_load = QAction("Munkamenet &betöltése…", self)
        act_load.setShortcut(QKeySequence.StandardKey.Open)
        act_load.triggered.connect(self._on_load)
        file_menu.addAction(act_load)

        file_menu.addSeparator()
        act_quit = QAction("&Kilépés", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        view_menu = bar.addMenu("&Nézet")
        assert view_menu is not None
        act_clear = QAction("Forgalom &log törlése", self)
        act_clear.setShortcut("Ctrl+L")
        act_clear.triggered.connect(self._traffic.clear)
        view_menu.addAction(act_clear)

        help_menu = bar.addMenu("&Súgó")
        assert help_menu is not None
        act_about = QAction("&Névjegy", self)
        act_about.triggered.connect(self._on_about)
        help_menu.addAction(act_about)

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------
    def _build_status_bar(self) -> None:
        self._lbl_server = QLabel("Szerver: leállítva")
        self._lbl_client = QLabel("Kliens: nincs csatlakozva")
        self._lbl_traffic = QLabel("Forgalom: 0")
        status = self.statusBar()
        assert status is not None
        status.addWidget(self._lbl_server, 1)
        status.addWidget(self._lbl_client, 1)
        status.addWidget(self._lbl_traffic, 1)

    def _wire_status_updates(self) -> None:
        self._server_tab.status_changed.connect(self._on_server_status)
        self._client_tab.status_changed.connect(self._on_client_status)
        self._client_tab.polling_sample.connect(self._trend_tab.on_polling_sample)
        self._client_tab.manual_response.connect(self._analyzer_tab.set_registers)
        self._analyzer_tab.apply_to_manual.connect(self._client_tab.set_manual_decode)
        self._traffic.add_entry_listener(self._on_traffic_entry)

    def _on_server_status(self, running: bool) -> None:
        port = self._server.bound_port
        self._lbl_server.setText(
            f"Szerver: fut - {self._server.host}:{port}" if running else "Szerver: leállítva"
        )

    def _on_client_status(self, connected: bool, description: str) -> None:
        self._lbl_client.setText(
            f"Kliens: csatlakozva - {description}"
            if connected
            else "Kliens: nincs csatlakozva"
        )

    def _on_traffic_entry(self, _entry: object) -> None:
        # Called from the asyncio loop which IS the Qt loop under qasync,
        # so it's safe to touch widgets.
        self._lbl_traffic.setText(f"Forgalom: {self._traffic.size}")

    # ------------------------------------------------------------------
    # Session save/load
    # ------------------------------------------------------------------
    def current_session(self) -> SessionConfig:
        """Materialise current UI state into a fresh :class:`SessionConfig`."""
        session = self._session.model_copy(deep=True)
        session.ui.last_tab = self._tabs.currentIndex()
        session.ui.window_geometry = self.saveGeometry().toHex().data().decode("ascii")

        # Per-tab capture — each tab writes its pieces into the session.
        self._server_tab.apply_to(session)
        self._client_tab.apply_to(session)
        self._simulation_tab.apply_to(session)
        self._exceptions_tab.apply_to(session)
        return session

    def _on_save_as(self) -> None:
        start = str(Path.home() / "Desktop" / "modbus_session.json")
        path, _ = QFileDialog.getSaveFileName(self, "Save session", start, "JSON (*.json)")
        if not path:
            return
        try:
            save_session(self.current_session(), Path(path))
        except Exception as err:
            log.exception("save_session failed")
            QMessageBox.critical(self, "Save failed", str(err))

    def _on_load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load session", str(Path.home()), "JSON (*.json)"
        )
        if not path:
            return
        try:
            loaded = load_session(Path(path))
        except Exception as err:
            log.exception("load_session failed")
            QMessageBox.critical(self, "Load failed", str(err))
            return
        QMessageBox.information(
            self,
            "Loaded",
            "Session loaded. Some fields (register block sizes, server host/port) "
            "require restarting the application to take effect.",
        )
        self._session = loaded
        self._server_tab.reload_from(loaded)
        self._client_tab.reload_from(loaded)
        self._simulation_tab.reload_from(loaded)
        self._exceptions_tab.reload_from(loaded)

    def _autosave_tick(self) -> None:
        try:
            save_session(self.current_session())
        except Exception:
            log.exception("autosave failed")

    # ------------------------------------------------------------------
    # About
    # ------------------------------------------------------------------
    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            "Névjegy",
            f"Kremzer Péter ModbusTCP — {__version__}\n\n"
            "Modbus TCP szimulátor teljes szerver és kliens funkcionalitással: "
            "értékszimuláció, hibainjektálás, forgalom napló, regiszter trend "
            "grafikon, több-formátumú dekódolás, automatikus lekérdezés.\n\n"
            "© 2026 Kremzer Péter — MIT licenc.",
        )
