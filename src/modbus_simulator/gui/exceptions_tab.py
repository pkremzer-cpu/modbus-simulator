"""Exception injection rule editor."""

from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from modbus_simulator.config.schema import ExceptionRuleCfg, SessionConfig
from modbus_simulator.core.exceptions import ExceptionRule, RuleAction, RuleEngine

log = logging.getLogger(__name__)


class ExceptionsTab(QWidget):
    def __init__(self, rules: RuleEngine, session: SessionConfig) -> None:
        super().__init__()
        self._rules = rules
        self._session = session
        self._cfgs: list[ExceptionRuleCfg] = list(session.exception_rules)
        self._build()
        self._rebuild_list()
        self._sync_engine()

    def _build(self) -> None:
        self._list = QListWidget()

        add_btn = QPushButton("Szabály hozzáadása…")
        add_btn.clicked.connect(self._on_add)
        remove_btn = QPushButton("Kijelölt törlése")
        remove_btn.clicked.connect(self._on_remove)

        row = QHBoxLayout()
        row.addWidget(add_btn)
        row.addWidget(remove_btn)
        row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self._list, 1)

    def _rebuild_list(self) -> None:
        self._list.clear()
        for rc in self._cfgs:
            self._list.addItem(
                f"{rc.name}  FC={','.join(map(str, rc.function_codes))}  "
                f"[{rc.address_start}..{rc.address_end}]  {rc.action.value}  "
                f"delay={rc.delay_ms:.0f}ms  p={rc.probability:.2f}"
            )

    def _sync_engine(self) -> None:
        self._rules.clear()
        for rc in self._cfgs:
            self._rules.add_rule(
                ExceptionRule(
                    name=rc.name,
                    function_codes=frozenset(rc.function_codes),
                    unit_ids=frozenset(rc.unit_ids),
                    address_start=rc.address_start,
                    address_end=rc.address_end,
                    action=rc.action,
                    delay_ms=rc.delay_ms,
                    probability=rc.probability,
                )
            )

    def _on_add(self) -> None:
        dialog = _RuleDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        cfg = dialog.result_cfg()
        if cfg is None:
            return
        self._cfgs.append(cfg)
        self._rebuild_list()
        self._sync_engine()

    def _on_remove(self) -> None:
        rows = sorted({self._list.row(it) for it in self._list.selectedItems()}, reverse=True)
        for r in rows:
            del self._cfgs[r]
        self._rebuild_list()
        self._sync_engine()

    # ------------------------------------------------------------------
    def apply_to(self, session: SessionConfig) -> None:
        session.exception_rules = list(self._cfgs)

    def reload_from(self, session: SessionConfig) -> None:
        self._session = session
        self._cfgs = list(session.exception_rules)
        self._rebuild_list()
        self._sync_engine()


class _RuleDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Új kivétel szabály")

        self._name = QLineEdit("szabaly")
        self._fcs = QLineEdit("3")
        self._fcs.setPlaceholderText("vesszővel elválasztott FC-k, pl. 3,4")
        self._units = QLineEdit("")
        self._units.setPlaceholderText("üres = mindegyik")
        self._start = QSpinBox()
        self._start.setRange(0, 65535)
        self._end = QSpinBox()
        self._end.setRange(0, 65535)
        self._end.setValue(65535)
        self._action = QComboBox()
        for a in RuleAction:
            self._action.addItem(a.value, userData=a)
        self._delay = QDoubleSpinBox()
        self._delay.setRange(0, 10000)
        self._delay.setSuffix(" ms")
        self._prob = QDoubleSpinBox()
        self._prob.setRange(0, 1)
        self._prob.setSingleStep(0.1)
        self._prob.setValue(1.0)

        form = QFormLayout()
        form.addRow("Név", self._name)
        form.addRow("Funkciókódok", self._fcs)
        form.addRow("Egység azonosítók", self._units)
        form.addRow("Cím kezdete", self._start)
        form.addRow("Cím vége", self._end)
        form.addRow("Művelet", self._action)
        form.addRow("Késleltetés", self._delay)
        form.addRow("Valószínűség", self._prob)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def result_cfg(self) -> ExceptionRuleCfg | None:
        try:
            fcs = [int(x.strip()) for x in self._fcs.text().split(",") if x.strip()]
            units = [int(x.strip()) for x in self._units.text().split(",") if x.strip()]
            action: RuleAction = self._action.currentData()
            return ExceptionRuleCfg(
                name=self._name.text() or "rule",
                function_codes=fcs or [3],
                unit_ids=units,
                address_start=self._start.value(),
                address_end=self._end.value(),
                action=action,
                delay_ms=self._delay.value(),
                probability=self._prob.value(),
            )
        except Exception:
            log.exception("rule dialog parse failed")
            return None
