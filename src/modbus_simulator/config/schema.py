"""Pydantic v2 session schema.

Everything the UI can tune is persisted here so ``save_session`` can dump the
complete state to JSON and ``load_session`` can restore it on the next launch.
Config objects are pure data — no Qt, no pymodbus imports — so tests run headless.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from modbus_simulator.core.codec import ByteOrder, DataType, WordOrder
from modbus_simulator.core.datastore import BlockKind
from modbus_simulator.core.exceptions import RuleAction
from modbus_simulator.core.simulator import Distribution, RampDirection


# ---------------------------------------------------------------------------
# Server / client / UI
# ---------------------------------------------------------------------------
class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"  # noqa: S104 — default bind; user chooses
    port: int = Field(default=5020, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=1, le=247)
    coils_size: int = Field(default=1024, ge=1, le=65536)
    discrete_inputs_size: int = Field(default=1024, ge=1, le=65536)
    holding_registers_size: int = Field(default=1024, ge=1, le=65536)
    input_registers_size: int = Field(default=1024, ge=1, le=65536)


class ClientConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=502, ge=1, le=65535)  # Modbus default
    unit_id: int = Field(default=1, ge=1, le=247)
    timeout: float = Field(default=3.0, gt=0)


class Language(StrEnum):
    HU = "hu"
    EN = "en"


class UIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Language | None = None  # None = prompt on next launch
    window_geometry: str | None = None  # QByteArray hex — set by MainWindow
    last_tab: int = 0
    traffic_max_entries: int = Field(default=50_000, ge=100, le=1_000_000)


# ---------------------------------------------------------------------------
# Generators — discriminated union for simulation entries
# ---------------------------------------------------------------------------
class _GenBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConstantGenCfg(_GenBase):
    kind: Literal["constant"] = "constant"
    value: float = 0.0


class RampGenCfg(_GenBase):
    kind: Literal["ramp"] = "ramp"
    min: float
    max: float
    step: float = Field(gt=0)
    period_ms: float = Field(gt=0)
    direction: RampDirection = RampDirection.UP


class SineGenCfg(_GenBase):
    kind: Literal["sine"] = "sine"
    amplitude: float = 1.0
    offset: float = 0.0
    frequency_hz: float = Field(default=1.0, gt=0)
    phase_deg: float = 0.0


class RandomGenCfg(_GenBase):
    kind: Literal["random"] = "random"
    min: float
    max: float
    distribution: Distribution = Distribution.UNIFORM
    update_ms: float = Field(default=1000.0, gt=0)
    seed: int = 0


class ScriptGenCfg(_GenBase):
    kind: Literal["script"] = "script"
    source: str


class ToggleGenCfg(_GenBase):
    kind: Literal["toggle"] = "toggle"
    period_ms: float = Field(default=1000.0, gt=0)


class PatternGenCfg(_GenBase):
    kind: Literal["pattern"] = "pattern"
    bits: list[int]  # each 0 or 1
    shift_ms: float = Field(default=500.0, gt=0)


GeneratorCfg = Annotated[
    ConstantGenCfg
    | RampGenCfg
    | SineGenCfg
    | RandomGenCfg
    | ScriptGenCfg
    | ToggleGenCfg
    | PatternGenCfg,
    Field(discriminator="kind"),
]


class SimulationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block: BlockKind
    address: int = Field(ge=0, le=65535)
    generator: GeneratorCfg


# ---------------------------------------------------------------------------
# Exception rules
# ---------------------------------------------------------------------------
class ExceptionRuleCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    function_codes: list[int]  # will be validated >=1
    unit_ids: list[int] = Field(default_factory=list)  # empty = any
    address_start: int = Field(ge=0, le=65535)
    address_end: int = Field(ge=0, le=65535)
    action: RuleAction
    delay_ms: float = Field(default=0.0, ge=0)
    probability: float = Field(default=1.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Client polling entries
# ---------------------------------------------------------------------------
class PollingEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    function_code: int = Field(ge=1, le=127)
    address: int = Field(ge=0, le=65535)
    count: int = Field(default=1, ge=1, le=2000)
    data_type: DataType = DataType.UINT16
    byte_order: ByteOrder = ByteOrder.BIG
    word_order: WordOrder = WordOrder.BIG
    interval_ms: float = Field(default=1000.0, gt=0)
    enabled: bool = True


# ---------------------------------------------------------------------------
# Full session
# ---------------------------------------------------------------------------
class SessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    server: ServerConfig = Field(default_factory=ServerConfig)
    client: ClientConfig = Field(default_factory=ClientConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    simulations: list[SimulationEntry] = Field(default_factory=list)
    exception_rules: list[ExceptionRuleCfg] = Field(default_factory=list)
    polling_entries: list[PollingEntry] = Field(default_factory=list)
