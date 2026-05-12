"""JSON serialisation for :class:`SessionConfig`."""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import ValidationError

from modbus_simulator.config.paths import last_session_path
from modbus_simulator.config.schema import SessionConfig

log = logging.getLogger(__name__)


def save_session(config: SessionConfig, path: Path | None = None) -> Path:
    target = path or last_session_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file and atomically replace to avoid corruption on crash.
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


def load_session(path: Path | None = None) -> SessionConfig:
    source = path or last_session_path()
    if not source.exists():
        return SessionConfig()
    try:
        raw = source.read_text(encoding="utf-8")
        return SessionConfig.model_validate_json(raw)
    except (ValidationError, ValueError):
        log.exception("failed to load session from %s — using defaults", source)
        return SessionConfig()
