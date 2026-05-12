"""py2app build script.

Usage:
    uv run python setup.py py2app

This file exists ONLY for the macOS .app bundle build. For normal dev work
use pyproject.toml (the authoritative source of metadata and deps).
"""

from __future__ import annotations

from pathlib import Path

from setuptools import setup

ROOT = Path(__file__).parent
ICON = ROOT / "resources" / "icons" / "AppIcon.icns"

APP = [str(ROOT / "src" / "modbus_simulator" / "__main__.py")]

DATA_FILES: list[tuple[str, list[str]]] = []

OPTIONS: dict[str, object] = {
    "argv_emulation": False,
    "packages": ["modbus_simulator", "PyQt6", "pymodbus", "pyqtgraph", "qasync"],
    "includes": ["asyncio", "json", "logging"],
    "excludes": ["tkinter", "PyQt5", "PySide6"],
    "arch": "universal2",
    "plist": {
        "CFBundleName": "ModbusSimulator",
        "CFBundleDisplayName": "Modbus Simulator",
        "CFBundleIdentifier": "com.kremzerpeter.modbussimulator",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundlePackageType": "APPL",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
        "NSHumanReadableCopyright": "Copyright \u00a9 2026 Kremzer Peter. MIT License.",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
    },
}

if ICON.exists():
    OPTIONS["iconfile"] = str(ICON)
else:
    print(f"[setup.py] WARNING: icon not found at {ICON} \u2014 building without icon")

setup(
    name="ModbusSimulator",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
