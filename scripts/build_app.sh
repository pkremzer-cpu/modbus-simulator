#!/usr/bin/env bash
# Build the macOS .app bundle via py2app.
#
# Output:  dist/ModbusSimulator.app
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[build_app] cleaning previous build artifacts..."
rm -rf build dist

echo "[build_app] invoking py2app..."
uv run python setup.py py2app

APP="dist/ModbusSimulator.app"
if [[ ! -d "$APP" ]]; then
    echo "[build_app] ERROR: $APP was not produced" >&2
    exit 1
fi

echo "[build_app] verifying binary linkage..."
BIN="$APP/Contents/MacOS/ModbusSimulator"
if [[ -f "$BIN" ]]; then
    otool -L "$BIN" | head -20
else
    echo "[build_app] WARNING: main binary not found at $BIN" >&2
fi

SIZE=$(du -sh "$APP" | cut -f1)
echo "[build_app] OK: $APP ($SIZE)"
