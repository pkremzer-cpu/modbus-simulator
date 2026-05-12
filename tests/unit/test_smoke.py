"""Smoke test — verifies the package is importable and version is well-formed.

Replaced by real unit tests as core/ modules land in roadmap step 2.
"""

from __future__ import annotations

import re

import modbus_simulator


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", modbus_simulator.__version__)
