#!/usr/bin/env bash
# Build the DMG installer. Requires scripts/build_app.sh to have run first.
#
# Output:  dist/ModbusSimulator-<version>.dmg
set -euo pipefail
cd "$(dirname "$0")/.."

APP="dist/ModbusSimulator.app"
if [[ ! -d "$APP" ]]; then
    echo "[build_dmg] ERROR: $APP missing. Run ./scripts/build_app.sh first." >&2
    exit 1
fi

if ! command -v create-dmg >/dev/null 2>&1; then
    echo "[build_dmg] ERROR: create-dmg not installed. Run: brew install create-dmg" >&2
    exit 1
fi

VERSION=$(uv run python -c 'from modbus_simulator import __version__; print(__version__)')
DMG="dist/ModbusSimulator-${VERSION}.dmg"

echo "[build_dmg] packaging ${DMG}..."
rm -f "$DMG"

BG_ARGS=()
if [[ -f "resources/dmg/background.png" ]]; then
    BG_ARGS=(--background "resources/dmg/background.png")
fi

create-dmg \
    --volname "Modbus Simulator ${VERSION}" \
    --window-pos 200 120 \
    --window-size 600 400 \
    --icon-size 100 \
    --icon "ModbusSimulator.app" 150 200 \
    --app-drop-link 450 200 \
    --hide-extension "ModbusSimulator.app" \
    --no-internet-enable \
    "${BG_ARGS[@]}" \
    "$DMG" \
    "$APP"

echo "[build_dmg] OK: $DMG"
