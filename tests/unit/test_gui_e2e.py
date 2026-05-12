"""GUI-driven end-to-end test — drive the app through actual widgets.

Simulates an operator starting the server on the Server tab, connecting on the
Client tab, then issuing a read through the manual-transaction panel and
verifying the response text widget shows the slave's register values.

Runs headless via the offscreen Qt platform and qasync. Modal dialogs
(QMessageBox.*) are patched to no-ops so an automated click doesn't wedge on
a confirmation popup.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import qasync
from PyQt6.QtWidgets import QApplication, QMessageBox

from modbus_simulator.config.schema import SessionConfig
from modbus_simulator.gui.main_window import MainWindow

FREE_TEST_PORT = 15020  # non-privileged, uncommon


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep autosave from writing to the real config dir, silence modals."""
    import modbus_simulator.config.paths as paths_mod
    import modbus_simulator.config.persistence as persist_mod
    import modbus_simulator.gui.main_window as mw_mod

    fake = tmp_path / "last_session.json"
    monkeypatch.setattr(paths_mod, "last_session_path", lambda: fake)
    monkeypatch.setattr(paths_mod, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(persist_mod, "last_session_path", lambda: fake)
    monkeypatch.setattr(mw_mod, "save_session", lambda *_a, **_k: fake)

    # Silence QMessageBox — they're modal and block the qasync loop under offscreen.
    monkeypatch.setattr(QMessageBox, "information", lambda *_a, **_k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_a, **_k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "critical", lambda *_a, **_k: QMessageBox.StandardButton.Ok)


@pytest.fixture
def qapp_loop():
    app = QApplication.instance() or QApplication([])
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    yield loop


async def _wait_until(predicate, timeout_s: float = 3.0, step_s: float = 0.05) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step_s)
    return predicate()


def test_server_and_client_roundtrip_through_gui(qapp_loop: qasync.QEventLoop) -> None:
    """Full GUI flow: start server → connect client → read HR → response text."""

    async def scenario() -> None:
        session = SessionConfig()
        session.server.port = FREE_TEST_PORT
        session.client.host = "127.0.0.1"
        session.client.port = FREE_TEST_PORT

        window = MainWindow(session)
        try:
            window._datastore.holding_registers.set(0, [11, 22, 33, 44, 55])

            # Start server via the button
            window._server_tab._start_btn.click()
            assert await _wait_until(
                lambda: window._server.is_running, timeout_s=4.0
            ), "server never reported running"
            assert window._server.bound_port == FREE_TEST_PORT

            # Configure + connect client via its button
            window._client_tab._host_edit.setText("127.0.0.1")
            window._client_tab._port_spin.setValue(FREE_TEST_PORT)
            window._client_tab._unit_spin.setValue(1)
            window._client_tab._connect_btn.click()
            assert await _wait_until(
                lambda: window._client_tab._client is not None
                and window._client_tab._client.is_connected,
                timeout_s=4.0,
            ), "client never connected"

            # Read 5 holding regs via the Send button
            window._client_tab._fc_combo.setCurrentIndex(2)  # FC 03
            window._client_tab._addr_spin.setValue(0)
            window._client_tab._count_spin.setValue(5)
            window._client_tab._resp.clear()
            window._client_tab._send_btn.click()

            assert await _wait_until(
                lambda: bool(window._client_tab._resp.toPlainText()), timeout_s=4.0
            ), "client response never arrived"
            response = window._client_tab._resp.toPlainText()
            # Response is rendered as multi-format HTML (DEC / HEX / BIN / decoded).
            # Plain-text extraction still contains the decimal values.
            for expected in ("11", "22", "33", "44", "55"):
                assert expected in response, f"missing {expected} in: {response!r}"

            entries = window._traffic.snapshot()
            assert any(
                e.direction.value == "rx" and e.function_code == 3 for e in entries
            )
            assert any(
                e.direction.value == "tx" and e.function_code == 3 for e in entries
            )

            window._client_tab._connect_btn.click()
            await _wait_until(lambda: window._client_tab._client is None, timeout_s=2.0)
            window._server_tab._start_btn.click()
            await _wait_until(lambda: not window._server.is_running, timeout_s=2.0)
        finally:
            window.close()
            window.deleteLater()

    qapp_loop.run_until_complete(scenario())


def test_write_through_gui_updates_slave_datastore(qapp_loop: qasync.QEventLoop) -> None:
    """FC 06 write from Client tab propagates to the server's slave datastore."""
    port = FREE_TEST_PORT + 1  # keep distinct from the read test

    async def scenario() -> None:
        session = SessionConfig()
        session.server.port = port
        session.client.host = "127.0.0.1"
        session.client.port = port

        window = MainWindow(session)
        try:
            window._server_tab._start_btn.click()
            assert await _wait_until(lambda: window._server.is_running, timeout_s=4.0)

            window._client_tab._host_edit.setText("127.0.0.1")
            window._client_tab._port_spin.setValue(port)
            window._client_tab._connect_btn.click()
            assert await _wait_until(
                lambda: window._client_tab._client is not None
                and window._client_tab._client.is_connected,
                timeout_s=4.0,
            )

            # FC 06 — write single register, addr=10, value=0xBEEF=48879
            window._client_tab._fc_combo.setCurrentIndex(5)
            window._client_tab._addr_spin.setValue(10)
            window._client_tab._values_edit.setText("48879")
            window._client_tab._resp.clear()
            window._client_tab._send_btn.click()

            assert await _wait_until(
                lambda: bool(window._client_tab._resp.toPlainText()), timeout_s=4.0
            )
            assert "OK" in window._client_tab._resp.toPlainText()
            assert window._datastore.holding_registers.get(10) == (0xBEEF,)

            window._client_tab._connect_btn.click()
            await _wait_until(lambda: window._client_tab._client is None, timeout_s=2.0)
            window._server_tab._start_btn.click()
            await _wait_until(lambda: not window._server.is_running, timeout_s=2.0)
        finally:
            window.close()
            window.deleteLater()

    qapp_loop.run_until_complete(scenario())
