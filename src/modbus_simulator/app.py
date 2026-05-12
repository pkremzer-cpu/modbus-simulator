"""Application bootstrap.

Creates the :class:`QApplication`, installs ``qasync`` so asyncio shares the
Qt event loop, loads the session from disk, and hands control to
:class:`MainWindow`. ``main()`` is the entry point used by both ``__main__.py``
and the packaged ``.app``.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import qasync
from PyQt6.QtWidgets import QApplication

from modbus_simulator.config.paths import APP_AUTHOR, APP_NAME
from modbus_simulator.config.persistence import load_session, save_session
from modbus_simulator.gui.main_window import MainWindow
from modbus_simulator.gui.theme import DARK_QSS

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_AUTHOR)
    app.setApplicationDisplayName("Kremzer Péter ModbusTCP")
    app.setStyleSheet(DARK_QSS)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    session = load_session()
    window = MainWindow(session)
    window.show()

    # On quit, persist session
    def _on_quit() -> None:
        try:
            save_session(window.current_session())
        except Exception:
            log.exception("failed to save session on quit")

    app.aboutToQuit.connect(_on_quit)

    with loop:
        loop.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
