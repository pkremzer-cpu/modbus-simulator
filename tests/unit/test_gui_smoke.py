"""Smoke tests for the GUI — construct widgets without running the event loop.

Uses pytest-qt's ``qtbot`` and the offscreen Qt platform so these run
headless on CI / in a tmux session without a display.

The autouse ``_isolate_session_io`` fixture redirects session persistence
to a temp directory so the autosave timer cannot overwrite the user's real
``~/Library/Application Support/ModbusSimulator/last_session.json``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest_plugins = ("pytestqt",)

from PyQt6.QtWidgets import QWidget  # noqa: E402

from modbus_simulator.config.schema import SessionConfig  # noqa: E402
from modbus_simulator.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_session_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect every persistence path seen by the GUI code to ``tmp_path``."""
    fake = tmp_path / "last_session.json"

    def fake_last_path() -> Path:
        return fake

    def fake_data_dir() -> Path:
        return tmp_path

    import modbus_simulator.config.paths as paths_mod
    import modbus_simulator.config.persistence as persist_mod
    import modbus_simulator.gui.main_window as mw_mod

    monkeypatch.setattr(paths_mod, "last_session_path", fake_last_path)
    monkeypatch.setattr(paths_mod, "data_dir", fake_data_dir)
    monkeypatch.setattr(persist_mod, "last_session_path", fake_last_path)
    monkeypatch.setattr(mw_mod, "save_session", lambda *_a, **_k: fake)
    monkeypatch.setattr(mw_mod, "load_session", lambda *_a, **_k: SessionConfig())


def test_main_window_constructs(qtbot) -> None:
    window = MainWindow(SessionConfig())
    qtbot.add_widget(window)
    assert window.windowTitle() == "Kremzer Péter ModbusTCP"
    central = window.centralWidget()
    assert central is not None
    assert hasattr(central, "count")
    assert central.count() == 8


def test_main_window_session_roundtrip(qtbot) -> None:
    session = SessionConfig()
    session.server.port = 5099
    session.client.host = "10.0.0.1"
    window = MainWindow(session)
    qtbot.add_widget(window)
    out = window.current_session()
    assert out.server.port == 5099
    assert out.client.host == "10.0.0.1"


@pytest.mark.parametrize(
    "tab_name",
    [
        "_server_tab",
        "_client_tab",
        "_traffic_tab",
        "_trend_tab",
        "_simulation_tab",
        "_exceptions_tab",
        "_scanner_tab",
        "_analyzer_tab",
    ],
)
def test_each_tab_is_qwidget(qtbot, tab_name: str) -> None:
    window = MainWindow(SessionConfig())
    qtbot.add_widget(window)
    tab = getattr(window, tab_name)
    assert isinstance(tab, QWidget)


def test_autosave_never_writes_real_path(qtbot, tmp_path: Path) -> None:
    """Guard against future regressions: confirm the fixture isolates disk IO."""
    import modbus_simulator.config.paths as paths_mod

    # The autouse fixture should have redirected last_session_path to tmp_path.
    assert paths_mod.last_session_path().parent == tmp_path
    # Build a window and manually trigger the autosave callback.
    window = MainWindow(SessionConfig())
    qtbot.add_widget(window)
    window._autosave_tick()  # type: ignore[attr-defined]
    # The real user directory must not have been touched.
    real = Path.home() / "Library" / "Application Support" / "ModbusSimulator" / "last_session.json"
    if real.exists():
        # Read original mtime just to confirm we didn't rewrite it during this test.
        mtime_before = real.stat().st_mtime
        window._autosave_tick()  # type: ignore[attr-defined]
        assert real.stat().st_mtime == mtime_before
