"""Application-wide dark theme — a single QSS sheet applied on startup.

The palette targets a professional engineering-tool look: near-black chrome,
light-gray text, macOS-blue accent. Kept in one place so further tweaks don't
chase every widget file.
"""

from __future__ import annotations

DARK_QSS = """
QMainWindow, QWidget, QDialog { background: #1e1e1e; color: #e8e8e8; }

QTabWidget::pane { border: 1px solid #3a3a3a; background: #1e1e1e; top: -1px; }
QTabBar::tab {
    padding: 7px 18px; background: #262626; color: #bdbdbd;
    border: 1px solid #3a3a3a; border-bottom: 0;
    border-top-left-radius: 4px; border-top-right-radius: 4px;
}
QTabBar::tab:selected { background: #0a84ff; color: white; border-color: #0a84ff; }
QTabBar::tab:hover:!selected { background: #2d2d2d; color: #eee; }

QGroupBox {
    border: 1px solid #3a3a3a; border-radius: 6px; margin-top: 10px;
    padding: 14px 10px 10px 10px; color: #e8e8e8; font-weight: 600;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; }

QPushButton {
    background: #333; color: #eaeaea; border: 1px solid #4a4a4a;
    padding: 5px 14px; border-radius: 4px; min-width: 60px;
}
QPushButton:hover { background: #3b3b3b; border-color: #5a5a5a; }
QPushButton:pressed { background: #0a84ff; color: white; border-color: #0a84ff; }
QPushButton:checked { background: #0a84ff; color: white; border-color: #0a84ff; }
QPushButton:disabled { background: #262626; color: #555; border-color: #333; }

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit {
    background: #262626; color: #ececec; border: 1px solid #444;
    border-radius: 4px; padding: 3px 6px; selection-background-color: #0a84ff;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QComboBox:focus, QTextEdit:focus { border-color: #0a84ff; }
QComboBox::drop-down { border-left: 1px solid #444; width: 18px; }
QComboBox QAbstractItemView {
    background: #262626; color: #ececec; border: 1px solid #444;
    selection-background-color: #0a84ff;
}

QTableView, QTableWidget, QListWidget, QTreeView {
    background: #1e1e1e; color: #e8e8e8; alternate-background-color: #252525;
    gridline-color: #333; border: 1px solid #3a3a3a;
    selection-background-color: #0a84ff; selection-color: white;
}
QHeaderView::section {
    background: #2a2a2a; color: #d6d6d6; padding: 4px 6px; font-weight: 600;
    border: 0; border-right: 1px solid #3a3a3a; border-bottom: 1px solid #3a3a3a;
}

QStatusBar { background: #161616; color: #cccccc; border-top: 1px solid #3a3a3a; }
QStatusBar::item { border: 0; }

QMenuBar { background: #161616; color: #e0e0e0; }
QMenuBar::item:selected { background: #0a84ff; color: white; }
QMenu { background: #262626; color: #e0e0e0; border: 1px solid #3a3a3a; }
QMenu::item:selected { background: #0a84ff; color: white; }
QMenu::separator { height: 1px; background: #3a3a3a; margin: 4px 8px; }

QScrollBar:vertical { background: #1e1e1e; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #444; min-height: 24px; border-radius: 3px; margin: 2px; }
QScrollBar::handle:vertical:hover { background: #555; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #1e1e1e; height: 12px; margin: 0; }
QScrollBar::handle:horizontal {
    background: #444; min-width: 24px; border-radius: 3px; margin: 2px;
}
QScrollBar::handle:horizontal:hover { background: #555; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QSplitter::handle { background: #2a2a2a; }
QSplitter::handle:hover { background: #0a84ff; }

QCheckBox, QRadioButton, QLabel { color: #e8e8e8; }
QToolTip { background: #262626; color: #eaeaea; border: 1px solid #3a3a3a; padding: 4px; }
"""

PYQTGRAPH_BG = "#1e1e1e"
PYQTGRAPH_FG = "#e8e8e8"
PYQTGRAPH_GRID_ALPHA = 0.25
