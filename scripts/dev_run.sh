#!/usr/bin/env bash
# Run the app from source in a uv-managed venv.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python -m modbus_simulator "$@"
