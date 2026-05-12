"""Platform-specific paths for app-owned state."""

from __future__ import annotations

from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "ModbusSimulator"
APP_AUTHOR = "Kremzer Peter"


def data_dir() -> Path:
    """Platform-specific app-data dir:

    * macOS: ``~/Library/Application Support/ModbusSimulator``
    * Windows: ``%LOCALAPPDATA%\\Kremzer Peter\\ModbusSimulator``
    * Linux: ``$XDG_DATA_HOME/ModbusSimulator`` (defaults to ``~/.local/share``)
    """
    path = Path(user_data_dir(APP_NAME, APP_AUTHOR))
    path.mkdir(parents=True, exist_ok=True)
    return path


def last_session_path() -> Path:
    return data_dir() / "last_session.json"
