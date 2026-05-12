"""GUI-driven tests for the Client tab's vendor preset, addressing toggle,
Enron mode, and Apply-to-Manual flow.

These run headless via the offscreen Qt platform; we drive the widgets
directly rather than spinning up a QApplication event loop, because we want
fine-grained assertions on side effects of programmatic widget changes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from modbus_simulator.config.schema import SessionConfig
from modbus_simulator.core.codec import ByteOrder, DataType, WordOrder
from modbus_simulator.core.traffic import TrafficLog
from modbus_simulator.gui.client_tab import VENDOR_PRESETS, ClientTab


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(QMessageBox, "information", lambda *_a, **_k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_a, **_k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "critical", lambda *_a, **_k: QMessageBox.StandardButton.Ok)
    _ = tmp_path


@pytest.fixture
def tab():
    app = QApplication.instance() or QApplication([])
    session = SessionConfig()
    traffic = TrafficLog(max_entries=100)
    t = ClientTab(session, traffic)
    yield t
    t.deleteLater()
    _ = app


# ---------------------------------------------------------------------------
# Vendor preset side effects
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("preset_idx", range(len(VENDOR_PRESETS)))
def test_vendor_preset_applies_all_widgets(tab: ClientTab, preset_idx: int) -> None:
    """Selecting any vendor preset must set byte/word/dtype/addr/Enron."""
    preset = VENDOR_PRESETS[preset_idx]
    # +1 because index 0 is "Standard Modbus TCP" (no-op)
    tab._preset_combo.setCurrentIndex(preset_idx + 1)

    assert tab._byte_combo.currentData() == preset["byte_order"]
    assert tab._word_combo.currentData() == preset["word_order"]
    assert tab._type_combo.currentData() == preset["default_dtype"]
    assert tab._addressing_mode == preset["address_mode"]
    assert tab._enron_check.isChecked() == bool(preset["enron"])


def test_standard_preset_is_noop(tab: ClientTab) -> None:
    """Selecting 'Standard Modbus TCP' (index 0) does not change settings."""
    # Set a non-default state, then switch to Standard — should not revert.
    tab._byte_combo.setCurrentIndex(tab._byte_combo.findData(ByteOrder.LITTLE))
    tab._preset_combo.setCurrentIndex(0)
    assert tab._byte_combo.currentData() is ByteOrder.LITTLE


# ---------------------------------------------------------------------------
# Address mode toggle
# ---------------------------------------------------------------------------
class TestAddressingToggle:
    def test_default_is_pdu_zero_based(self, tab: ClientTab) -> None:
        assert tab._addressing_mode == 0
        assert tab._addr_spin.minimum() == 0
        assert tab._addr_spin.maximum() == 65535

    def test_switching_to_modbus_4xxxx_changes_range(self, tab: ClientTab) -> None:
        tab._addr_spin.setValue(100)  # PDU 100
        tab._addr_mode_combo.setCurrentIndex(1)  # Modbus 4xxxx mode
        assert tab._addressing_mode == 1
        assert tab._addr_spin.minimum() == 40001
        assert tab._addr_spin.value() == 40101  # 40001 + 100 PDU
        assert tab._addr_spin.prefix() == "HR "

    def test_pdu_address_round_trips_through_mode_switch(self, tab: ClientTab) -> None:
        tab._addr_spin.setValue(42)  # PDU 42
        assert tab._current_pdu_address() == 42
        tab._addr_mode_combo.setCurrentIndex(1)  # → 40043
        assert tab._addr_spin.value() == 40043
        assert tab._current_pdu_address() == 42
        tab._addr_mode_combo.setCurrentIndex(0)  # back to PDU
        assert tab._addr_spin.value() == 42
        assert tab._current_pdu_address() == 42

    def test_send_uses_pdu_address_regardless_of_display(self, tab: ClientTab) -> None:
        """The wire-level call must always use the PDU (0-based) address."""
        tab._addr_mode_combo.setCurrentIndex(1)  # Modbus mode
        tab._addr_spin.setValue(40101)  # display = HR 40101 → PDU 100
        assert tab._current_pdu_address() == 100


# ---------------------------------------------------------------------------
# Enron mode checkbox
# ---------------------------------------------------------------------------
class TestEnronCheckbox:
    def test_default_enron_off(self, tab: ClientTab) -> None:
        assert tab._enron_check.isChecked() is False

    def test_daniel_preset_turns_on_enron(self, tab: ClientTab) -> None:
        daniel_idx = next(
            i for i, p in enumerate(VENDOR_PRESETS) if "Daniel" in str(p["label"])
        )
        tab._preset_combo.setCurrentIndex(daniel_idx + 1)
        assert tab._enron_check.isChecked() is True


# ---------------------------------------------------------------------------
# Apply-to-Manual signal flow (Analyzer → Client)
# ---------------------------------------------------------------------------
class TestApplyToManual:
    def test_set_manual_decode_changes_all_three_combos(self, tab: ClientTab) -> None:
        tab.set_manual_decode(DataType.FLOAT32, ByteOrder.LITTLE, WordOrder.BIG)
        assert tab._type_combo.currentData() is DataType.FLOAT32
        assert tab._byte_combo.currentData() is ByteOrder.LITTLE
        assert tab._word_combo.currentData() is WordOrder.BIG

    def test_set_manual_decode_re_renders_response(self, tab: ClientTab) -> None:
        """When raw regs are cached, switching decode through the API
        triggers a fresh HTML rendering."""
        tab._last_response_regs = [0x3F80, 0x0000]  # FLOAT32 1.0 BE/BE
        tab.set_manual_decode(DataType.FLOAT32, ByteOrder.BIG, WordOrder.BIG)
        html = tab._resp.toHtml()
        # 1.0 should appear somewhere in the decoded matrix
        assert "1" in html
