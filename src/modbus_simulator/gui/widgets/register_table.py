"""A Qt table model wrapping a :class:`RegisterBlock`.

Shows address + decimal + hex (and binary for coils). Updates in real time
when the datastore emits change events. Cells are editable by default; edits
call :meth:`RegisterBlock.set`.
"""

from __future__ import annotations

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PyQt6.QtCore import QVariant as _QVariant

from modbus_simulator.core.datastore import BlockChange, RegisterBlock

_COL_ADDRESS = 0
_COL_DEC = 1
_COL_HEX = 2


class RegisterTableModel(QAbstractTableModel):
    def __init__(self, block: RegisterBlock) -> None:
        super().__init__()
        self._block = block
        self._dirty = False
        # Coalesce change events — fired listeners just flag dirty; a 100 ms
        # timer issues one dataChanged signal covering the touched range.
        self._dirty_low = 0
        self._dirty_high = -1
        self._coalesce = QTimer()
        self._coalesce.setSingleShot(True)
        self._coalesce.setInterval(100)
        self._coalesce.timeout.connect(self._emit_dirty)
        block.add_listener(self._on_change)

    # ------------------------------------------------------------------
    # Qt model interface
    # ------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return self._block.size

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return 3

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if role != Qt.ItemDataRole.DisplayRole:
            return _QVariant()
        if orientation == Qt.Orientation.Horizontal:
            return ["Address", "Dec", "Hex"][section]
        return str(section)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return _QVariant()
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return _QVariant()
        row = index.row()
        col = index.column()
        if col == _COL_ADDRESS:
            return row
        try:
            value = self._block.get(row)[0]
        except IndexError:
            return _QVariant()
        if col == _COL_DEC:
            return value
        if col == _COL_HEX:
            width = 1 if self._block.kind.is_bit else 4
            return f"0x{value:0{width}X}"
        return _QVariant()

    def setData(
        self, index: QModelIndex, value: object, role: int = Qt.ItemDataRole.EditRole
    ) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        col = index.column()
        if col == _COL_ADDRESS:
            return False
        try:
            parsed = (
                int(value, 16) if col == _COL_HEX and isinstance(value, str) else int(value)  # type: ignore[call-overload]
            )
        except (TypeError, ValueError):
            return False
        try:
            self._block.set(index.row(), [parsed])
        except (ValueError, IndexError):
            return False
        return True

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == _COL_ADDRESS:
            return base
        return base | Qt.ItemFlag.ItemIsEditable

    # ------------------------------------------------------------------
    # Change coalescing
    # ------------------------------------------------------------------
    def _on_change(self, change: BlockChange) -> None:
        low = change.address
        high = change.address + len(change.values) - 1
        if not self._dirty:
            self._dirty = True
            self._dirty_low = low
            self._dirty_high = high
        else:
            self._dirty_low = min(self._dirty_low, low)
            self._dirty_high = max(self._dirty_high, high)
        if not self._coalesce.isActive():
            self._coalesce.start()

    def _emit_dirty(self) -> None:
        if not self._dirty:
            return
        top = self.index(self._dirty_low, _COL_DEC)
        bottom = self.index(self._dirty_high, _COL_HEX)
        self.dataChanged.emit(top, bottom)
        self._dirty = False
