"""Analyzer tab — intelligently interpret a raw register block.

Features:

* Auto-fill from the Manual transaction response.
* Manual / paste-friendly input (comma, semicolon, whitespace separated;
  decimal, ``0x...``, ``0b...``).
* Top-5 ranked recommendation cards with score, value, reasoning.
* Full matrix of every interpretation with sort + text filter.
* Per-row tooltips showing full reasoning.
* "Apply to Manual transaction" — pushes the selected dtype / byte / word
  back into the Client tab combos for a follow-up read with that decode.
* "Export report" — Markdown report for engineering documentation.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from modbus_simulator.core.analyzer import Interpretation, analyse
from modbus_simulator.core.codec import ByteOrder, DataType, WordOrder

log = logging.getLogger(__name__)

# Splits on any combination of comma, semicolon, whitespace
_TOKEN_RE = re.compile(r"[,\s;]+")


class AnalyzerTab(QWidget):
    # Emitted when the user clicks "Alkalmaz a Manuálisra" on a numeric interp.
    apply_to_manual = pyqtSignal(object, object, object)  # (DataType, ByteOrder, WordOrder)

    def __init__(self) -> None:
        super().__init__()
        self._last_interps: list[Interpretation] = []
        self._last_registers: list[int] = []
        self._build()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build(self) -> None:
        # ---- input ----
        self._input = QLineEdit()
        self._input.setPlaceholderText(
            "Vesszővel / szóközzel elválasztott regiszterek (dec, 0x..., 0b...). "
            "Pl: 0x1F4E, 0x1F3A 8014 8006 0xCAFE"
        )
        self._input.returnPressed.connect(self._analyse)
        analyse_btn = QPushButton("Elemzés")
        analyse_btn.clicked.connect(self._analyse)
        clear_btn = QPushButton("Törlés")
        clear_btn.clicked.connect(self._clear)
        self._export_btn = QPushButton("Jelentés mentése…")
        self._export_btn.clicked.connect(self._export_markdown)
        self._status = QLabel("")
        self._status.setStyleSheet("color: #888;")

        sync_label = QLabel(
            "<i>A Kliens tab Manuális tranzakció válasza automatikusan ide kerül; "
            "kézzel is felülírhatod, majd Enter / Elemzés.</i>"
        )
        sync_label.setWordWrap(True)

        input_box = QGroupBox("Nyers regiszterek")
        ig = QVBoxLayout(input_box)
        row = QHBoxLayout()
        row.addWidget(self._input, 1)
        row.addWidget(analyse_btn)
        row.addWidget(clear_btn)
        row.addWidget(self._export_btn)
        ig.addLayout(row)
        ig.addWidget(self._status)
        ig.addWidget(sync_label)

        # ---- recommendation cards ----
        self._cards = QTextEdit()
        self._cards.setReadOnly(True)
        mono = QFont(self._cards.font())
        mono.setFamily("Menlo")
        self._cards.setFont(mono)
        self._cards.setMinimumHeight(220)

        self._apply_btn = QPushButton("Alkalmaz Manuálisra (kijelölt mátrix sor)")
        self._apply_btn.setToolTip(
            "Az alsó mátrixban kijelölt sor Type/Byte/Word beállítását átküldi a "
            "Kliens tab Manuális tranzakció dekódoló combókba."
        )
        self._apply_btn.clicked.connect(self._on_apply)

        cards_box = QGroupBox("Javaslatok (legjobb 5)")
        cb = QVBoxLayout(cards_box)
        cb.addWidget(self._cards)
        cb.addWidget(self._apply_btn)

        # ---- full matrix ----
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(
            "Szűrő (pl. \"float\", \"BE\", \"sentinel\")…"
        )
        self._filter_edit.textChanged.connect(self._apply_filter)

        self._matrix = QTableWidget(0, 5)
        self._matrix.setHorizontalHeaderLabels(
            ["Pontszám", "Bizalom", "Értelmezés", "Dekódolt érték", "Indoklás"]
        )
        self._matrix.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._matrix.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._matrix.setSortingEnabled(True)
        h = self._matrix.horizontalHeader()
        if h is not None:
            h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            h.setStretchLastSection(True)

        matrix_box = QGroupBox(
            "Teljes mátrix (minden Type / Byte / Word + ASCII / BCD / bitfield "
            "/ timestamp / scaled)"
        )
        mb = QVBoxLayout(matrix_box)
        mb.addWidget(self._filter_edit)
        mb.addWidget(self._matrix)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(cards_box)
        splitter.addWidget(matrix_box)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setHandleWidth(6)

        layout = QVBoxLayout(self)
        layout.addWidget(input_box)
        layout.addWidget(splitter, 1)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_registers(self, registers: list[int]) -> None:
        """Receive a block from the Client tab and analyse immediately."""
        text = ", ".join(f"0x{r:04X}" for r in registers)
        self._input.setText(text)
        self._analyse()

    # ------------------------------------------------------------------
    def _clear(self) -> None:
        self._input.clear()
        self._cards.clear()
        self._matrix.setRowCount(0)
        self._last_interps = []
        self._last_registers = []
        self._set_status("")
        self._input.setStyleSheet("")

    def _analyse(self) -> None:
        parsed = self._parse_input(self._input.text())
        if parsed is None:
            return
        regs, warnings = parsed
        if not regs:
            self._set_status("Nincs értelmezhető regiszter.", error=True)
            self._input.setStyleSheet("border: 1px solid #ff453a;")
            return
        self._input.setStyleSheet("")
        msg = f"{len(regs)} regiszter beolvasva"
        if warnings:
            msg += f" — figyelmeztetés: {'; '.join(warnings)}"
        self._set_status(msg, error=bool(warnings))
        self._last_registers = regs
        interps = analyse(regs)
        self._last_interps = interps
        self._render_cards(interps[:5])
        self._render_matrix(interps)

    def _set_status(self, msg: str, *, error: bool = False) -> None:
        color = "#ff453a" if error else "#34c759" if msg else "#888"
        self._status.setStyleSheet(f"color: {color};")
        self._status.setText(msg)

    @staticmethod
    def _parse_input(raw: str) -> tuple[list[int], list[str]] | None:
        """Return (registers, warnings) or ``None`` if input was empty."""
        if not raw.strip():
            return None
        tokens = [t for t in _TOKEN_RE.split(raw.strip()) if t]
        regs: list[int] = []
        warnings: list[str] = []
        for tok in tokens:
            t = tok.replace("_", "")
            try:
                if t.lower().startswith("0x"):
                    value = int(t, 16)
                elif t.lower().startswith("0b"):
                    value = int(t, 2)
                elif "." in t or "e" in t.lower():
                    # Float input — round and warn rather than truncate silently.
                    f = float(t)
                    value = round(f)
                    warnings.append(
                        f"'{tok}' → kerekítve {value} (eredeti {f})"
                    )
                else:
                    value = int(t, 10)
                if not 0 <= value <= 0xFFFF:
                    warnings.append(f"'{tok}' kívül a [0, 0xFFFF] tartományon — kihagyva")
                    continue
                regs.append(value)
            except ValueError:
                warnings.append(f"'{tok}' nem értelmezhető — kihagyva")
        return regs, warnings

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _render_cards(self, top: list[Interpretation]) -> None:
        if not top:
            self._cards.setHtml("<i>Nincs értelmezhető bemenet.</i>")
            return
        parts: list[str] = []
        for rank, interp in enumerate(top, start=1):
            color = _confidence_color(interp.score)
            score_pct = round(interp.score * 100)
            reasons_html = "<br>".join(f"• {_html_escape(r)}" for r in interp.reasons)
            sentinel_badge = (
                ' <span style="background:#ff453a; color:white; padding:1px 6px; '
                'border-radius:3px; font-size:10px;">SENTINEL</span>'
                if interp.sentinel
                else ""
            )
            parts.append(
                f"<div style='border-left: 4px solid {color}; background:#262626; "
                f"padding:8px; margin:6px 0;'>"
                f"<b>#{rank}. {_html_escape(interp.label)}</b>{sentinel_badge} "
                f"<span style='color:{color}'>"
                f"({interp.confidence}, {score_pct}%)</span><br>"
                f"<span style='color:#9cdcfe'>érték:</span> "
                f"<span style='color:#ce9178'>{_html_escape(interp.decoded_text[:300])}</span>"
                f"<div style='color:#bbb; font-size:11px; margin-top:4px;'>{reasons_html}</div>"
                f"</div>"
            )
        self._cards.setHtml(
            "<div style='font-family:Menlo,Monaco,monospace;font-size:12px'>"
            + "".join(parts)
            + "</div>"
        )

    def _render_matrix(self, interps: list[Interpretation]) -> None:
        self._matrix.setSortingEnabled(False)
        self._matrix.setRowCount(len(interps))
        for r, interp in enumerate(interps):
            tooltip = (
                f"<b>{_html_escape(interp.label)}</b><br>"
                f"Pontszám: {interp.score:.3f} ({interp.confidence})<br>"
                f"Érték: {_html_escape(interp.decoded_text)}<br><br>"
                + "<br>".join(f"• {_html_escape(r)}" for r in interp.reasons)
            )

            score_item = _NumericItem(interp.score, f"{interp.score:.2f}")
            score_item.setForeground(QColor(_confidence_color(interp.score)))
            score_item.setToolTip(tooltip)
            self._matrix.setItem(r, 0, score_item)

            conf_text = "🔴 SENTINEL" if interp.sentinel else interp.confidence
            conf_item = QTableWidgetItem(conf_text)
            conf_item.setToolTip(tooltip)
            self._matrix.setItem(r, 1, conf_item)

            label_item = QTableWidgetItem(interp.label)
            label_item.setToolTip(tooltip)
            self._matrix.setItem(r, 2, label_item)

            val_item = QTableWidgetItem(interp.decoded_text)
            val_item.setToolTip(interp.decoded_text)
            self._matrix.setItem(r, 3, val_item)

            reasons_item = QTableWidgetItem("; ".join(interp.reasons))
            reasons_item.setToolTip(tooltip)
            self._matrix.setItem(r, 4, reasons_item)
        self._matrix.setSortingEnabled(True)
        self._apply_filter(self._filter_edit.text())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for r in range(self._matrix.rowCount()):
            visible = True
            if needle:
                visible = False
                for c in range(self._matrix.columnCount()):
                    item = self._matrix.item(r, c)
                    if item and needle in item.text().lower():
                        visible = True
                        break
            self._matrix.setRowHidden(r, not visible)

    # ------------------------------------------------------------------
    # Apply to Manual
    # ------------------------------------------------------------------
    def _on_apply(self) -> None:
        sel = self._matrix.selectedItems()
        if not sel:
            QMessageBox.information(
                self,
                "Nincs kijelölés",
                "Jelölj ki egy sort a mátrixban, majd kattints az 'Alkalmaz Manuálisra'-ra.",
            )
            return
        row = sel[0].row()
        label_item = self._matrix.item(row, 2)
        if not label_item:
            return
        label = label_item.text()
        # Find the interpretation by label (sorting may have moved it)
        interp = next((i for i in self._last_interps if i.label == label), None)
        if interp is None:
            return
        if not isinstance(interp.dtype, DataType):
            QMessageBox.information(
                self,
                "Nem alkalmazható",
                "Ez a sor (ASCII / BCD / bitfield / timestamp / scaled) nem alkalmazható "
                "közvetlenül a Manuális dekódolóra. Válassz numerikus értelmezést.",
            )
            return
        bo = interp.byte_order or ByteOrder.BIG
        wo = interp.word_order or WordOrder.BIG
        self.apply_to_manual.emit(interp.dtype, bo, wo)
        self._set_status(f"Alkalmazva a Kliens tab-ra: {interp.label}")

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def _export_markdown(self) -> None:
        if not self._last_interps:
            QMessageBox.information(
                self, "Nincs adat", "Először futtass egy elemzést."
            )
            return
        default = str(Path.home() / "modbus_analyzer_report.md")
        path, _ = QFileDialog.getSaveFileName(
            self, "Jelentés mentése", default, "Markdown (*.md)"
        )
        if not path:
            return
        try:
            Path(path).write_text(self._build_markdown_report(), encoding="utf-8")
            self._set_status(f"Jelentés mentve: {path}")
        except OSError as err:
            QMessageBox.critical(self, "Mentés sikertelen", str(err))

    def _build_markdown_report(self) -> str:
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        regs_hex = " ".join(f"0x{r:04X}" for r in self._last_registers)
        regs_dec = " ".join(str(r) for r in self._last_registers)
        lines = [
            "# Modbus regiszter elemzés — Kremzer Péter ModbusTCP",
            "",
            f"**Időbélyeg:** {now}",
            f"**Regiszterek száma:** {len(self._last_registers)}",
            "",
            "## Nyers bemenet",
            "```",
            f"hex: {regs_hex}",
            f"dec: {regs_dec}",
            "```",
            "",
            "## Top 5 javaslat",
            "",
        ]
        for rank, interp in enumerate(self._last_interps[:5], start=1):
            sentinel = " (SENTINEL)" if interp.sentinel else ""
            lines.append(
                f"### #{rank}. {interp.label}{sentinel}"
            )
            lines.append(f"- **Pontszám:** {interp.score:.2f} ({interp.confidence})")
            lines.append(f"- **Dekódolt érték:** `{interp.decoded_text}`")
            lines.append("- **Indoklás:**")
            for r in interp.reasons:
                lines.append(f"    - {r}")
            lines.append("")
        lines.append("## Teljes mátrix")
        lines.append("")
        lines.append("| # | Pontszám | Bizalom | Értelmezés | Érték | Indoklás |")
        lines.append("|---|---|---|---|---|---|")
        for rank, interp in enumerate(self._last_interps, start=1):
            reasons = "; ".join(interp.reasons).replace("|", "\\|")
            value = interp.decoded_text.replace("|", "\\|")
            sentinel = " 🔴" if interp.sentinel else ""
            lines.append(
                f"| {rank} | {interp.score:.2f} | {interp.confidence}{sentinel} | "
                f"{interp.label} | `{value}` | {reasons} |"
            )
        lines.append("")
        lines.append("---")
        lines.append("*Generálta: Kremzer Péter ModbusTCP analizátor.*")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
class _NumericItem(QTableWidgetItem):
    """QTableWidgetItem that sorts numerically by its underlying float."""

    def __init__(self, value: float, text: str) -> None:
        super().__init__(text)
        self._value = float(value)

    def __lt__(self, other: object) -> bool:
        if isinstance(other, _NumericItem):
            return self._value < other._value
        return bool(super().__lt__(other))  # type: ignore[operator]


def _confidence_color(score: float) -> str:
    if score >= 0.75:
        return "#34c759"
    if score >= 0.5:
        return "#0a84ff"
    if score >= 0.25:
        return "#ff9f0a"
    return "#8e8e93"


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
