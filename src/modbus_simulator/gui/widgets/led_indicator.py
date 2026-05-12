"""Simple coloured LED indicator widget."""

from __future__ import annotations

from PyQt6.QtCore import QRect, QSize
from PyQt6.QtGui import QBrush, QColor, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import QWidget


class LedIndicator(QWidget):
    def __init__(
        self,
        *,
        size: int = 14,
        on_color: QColor = QColor(60, 200, 60),
        off_color: QColor = QColor(120, 120, 120),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._size = size
        self._on = on_color
        self._off = off_color
        self._state = False
        self.setFixedSize(QSize(size + 4, size + 4))

    def set_state(self, on: bool) -> None:
        if on == self._state:
            return
        self._state = on
        self.update()

    def paintEvent(self, event: QPaintEvent | None) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRect(2, 2, self._size, self._size)
        painter.setBrush(QBrush(self._on if self._state else self._off))
        painter.setPen(QPen(QColor(40, 40, 40), 1))
        painter.drawEllipse(rect)
        painter.end()
        _ = event
