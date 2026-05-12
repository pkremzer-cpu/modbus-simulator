# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows .exe build.

Run via ``scripts\\build_exe.ps1`` which invokes
``uv run pyinstaller --noconfirm scripts/modbus_simulator.spec``.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "resources" / "icons" / "AppIcon.ico"

block_cipher = None

a = Analysis(
    [str(ROOT / "src" / "modbus_simulator" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    # PyQt6 + pymodbus pull in submodules that PyInstaller's static analysis
    # may miss; declare them explicitly so the bundled exe doesn't crash on
    # import at runtime.
    hiddenimports=[
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        "qasync",
        "pyqtgraph",
        "pymodbus",
        "pymodbus.client",
        "pymodbus.server",
        "pymodbus.framer",
        "pymodbus.pdu",
        "pymodbus.transaction",
        "platformdirs",
        "pydantic",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PyQt5",
        "PySide6",
        "matplotlib",
        "scipy",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ModbusSimulator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX can break PyQt6 — leave off
    console=False,             # GUI app, no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ModbusSimulator",
)
