"""Tests for the config layer — schema validation and JSON persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from modbus_simulator.config.persistence import load_session, save_session
from modbus_simulator.config.schema import (
    ClientConfig,
    ConstantGenCfg,
    ExceptionRuleCfg,
    Language,
    PatternGenCfg,
    PollingEntry,
    RampGenCfg,
    RandomGenCfg,
    ScriptGenCfg,
    ServerConfig,
    SessionConfig,
    SimulationEntry,
    SineGenCfg,
    ToggleGenCfg,
    UIConfig,
)
from modbus_simulator.core.datastore import BlockKind
from modbus_simulator.core.exceptions import RuleAction
from modbus_simulator.core.simulator import Distribution, RampDirection


class TestDefaults:
    def test_session_defaults_valid(self) -> None:
        cfg = SessionConfig()
        assert cfg.schema_version == 1
        assert cfg.server.port == 5020
        assert cfg.ui.language is None
        assert cfg.simulations == []

    def test_server_host_default(self) -> None:
        assert ServerConfig().host == "0.0.0.0"


class TestValidation:
    @pytest.mark.parametrize("port", [0, -1, 65536, 100000])
    def test_invalid_port_rejected(self, port: int) -> None:
        with pytest.raises(ValidationError):
            ServerConfig(port=port)

    @pytest.mark.parametrize("unit", [0, 248, -1])
    def test_invalid_unit_rejected(self, unit: int) -> None:
        with pytest.raises(ValidationError):
            ServerConfig(unit_id=unit)

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ServerConfig.model_validate({"host": "1.2.3.4", "bogus": 99})


# ---------------------------------------------------------------------------
# Discriminated generator union
# ---------------------------------------------------------------------------
class TestGeneratorUnion:
    @pytest.mark.parametrize(
        "payload",
        [
            {"kind": "constant", "value": 42},
            {"kind": "ramp", "min": 0, "max": 10, "step": 1, "period_ms": 500},
            {"kind": "sine", "frequency_hz": 2.0},
            {"kind": "random", "min": 0, "max": 1, "update_ms": 100},
            {"kind": "script", "source": "t + 1"},
            {"kind": "toggle", "period_ms": 100},
            {"kind": "pattern", "bits": [1, 0, 1], "shift_ms": 100},
        ],
    )
    def test_discriminator_picks_right_model(self, payload: dict[str, object]) -> None:
        entry = SimulationEntry.model_validate(
            {"block": "holding_registers", "address": 0, "generator": payload}
        )
        assert entry.generator.kind == payload["kind"]

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SimulationEntry.model_validate(
                {
                    "block": "holding_registers",
                    "address": 0,
                    "generator": {"kind": "bogus"},
                }
            )


class TestBlockKindEnum:
    def test_accepts_string_values(self) -> None:
        entry = SimulationEntry.model_validate(
            {"block": "coils", "address": 0, "generator": {"kind": "constant", "value": 1}}
        )
        assert entry.block is BlockKind.COILS


# ---------------------------------------------------------------------------
# Exception rule
# ---------------------------------------------------------------------------
class TestExceptionRuleCfg:
    def test_probability_clamped(self) -> None:
        with pytest.raises(ValidationError):
            ExceptionRuleCfg(
                name="x",
                function_codes=[3],
                address_start=0,
                address_end=10,
                action=RuleAction.SLAVE_BUSY,
                probability=2.0,
            )

    def test_negative_delay_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExceptionRuleCfg(
                name="x",
                function_codes=[3],
                address_start=0,
                address_end=10,
                action=RuleAction.SLAVE_BUSY,
                delay_ms=-1,
            )


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------
def _sample() -> SessionConfig:
    return SessionConfig(
        server=ServerConfig(port=5021, unit_id=2, holding_registers_size=4096),
        client=ClientConfig(host="192.168.1.5", port=5021, timeout=5.0),
        ui=UIConfig(language=Language.EN, last_tab=2),
        simulations=[
            SimulationEntry(
                block=BlockKind.HOLDING_REGISTERS,
                address=10,
                generator=RampGenCfg(
                    min=0, max=100, step=1, period_ms=1000, direction=RampDirection.PINGPONG
                ),
            ),
            SimulationEntry(
                block=BlockKind.HOLDING_REGISTERS,
                address=20,
                generator=SineGenCfg(amplitude=100, frequency_hz=0.5),
            ),
            SimulationEntry(
                block=BlockKind.HOLDING_REGISTERS,
                address=30,
                generator=RandomGenCfg(min=0, max=1023, distribution=Distribution.GAUSSIAN),
            ),
            SimulationEntry(
                block=BlockKind.HOLDING_REGISTERS,
                address=40,
                generator=ScriptGenCfg(source="math.sin(t) * 100"),
            ),
            SimulationEntry(
                block=BlockKind.COILS,
                address=5,
                generator=ToggleGenCfg(period_ms=500),
            ),
            SimulationEntry(
                block=BlockKind.COILS,
                address=10,
                generator=PatternGenCfg(bits=[1, 0, 1, 1, 0], shift_ms=250),
            ),
            SimulationEntry(
                block=BlockKind.HOLDING_REGISTERS,
                address=50,
                generator=ConstantGenCfg(value=42),
            ),
        ],
        exception_rules=[
            ExceptionRuleCfg(
                name="slow",
                function_codes=[3, 4],
                unit_ids=[1],
                address_start=0,
                address_end=100,
                action=RuleAction.SLAVE_BUSY,
                delay_ms=250,
                probability=0.5,
            )
        ],
        polling_entries=[
            PollingEntry(name="temp", function_code=3, address=0, count=2, interval_ms=500)
        ],
    )


class TestPersistence:
    def test_roundtrip_in_memory(self) -> None:
        original = _sample()
        reloaded = SessionConfig.model_validate_json(original.model_dump_json())
        assert reloaded == original

    def test_roundtrip_disk(self, tmp_path: Path) -> None:
        target = tmp_path / "session.json"
        original = _sample()
        written = save_session(original, path=target)
        assert written == target
        assert target.exists()
        loaded = load_session(path=target)
        assert loaded == original

    def test_load_missing_returns_defaults(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.json"
        loaded = load_session(path=missing)
        assert loaded == SessionConfig()

    def test_load_corrupt_returns_defaults(self, tmp_path: Path) -> None:
        target = tmp_path / "bad.json"
        target.write_text("{not: json}")
        loaded = load_session(path=target)
        assert loaded == SessionConfig()

    def test_load_with_unknown_field_returns_defaults(self, tmp_path: Path) -> None:
        target = tmp_path / "bad.json"
        target.write_text(json.dumps({"server": {"port": 5020, "extra_field": 1}}))
        loaded = load_session(path=target)
        # unknown field rejected by extra=forbid; loader falls back to defaults
        assert loaded == SessionConfig()

    def test_save_is_atomic(self, tmp_path: Path) -> None:
        target = tmp_path / "s.json"
        save_session(SessionConfig(), path=target)
        assert target.exists()
        assert not (tmp_path / "s.json.tmp").exists()
