"""Entry point.

Run with:  uv run python -m modbus_simulator
"""

from __future__ import annotations

import sys

from modbus_simulator.app import main

if __name__ == "__main__":
    sys.exit(main())
