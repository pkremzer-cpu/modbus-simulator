"""Exception injection rule engine.

The server consults the engine before answering each request. If a rule
matches the request (function code, unit ID, address range) and its
probability gate passes, the engine returns a :class:`RuleMatch` describing
what the server should do: return a specific Modbus exception code, drop the
response entirely (simulating a timeout), and/or delay the response.

Rules are evaluated top-down; the first match wins. The GUI presents rules
as an ordered list so operators can control priority.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class RuleAction(StrEnum):
    ILLEGAL_FUNCTION = "illegal_function"
    ILLEGAL_DATA_ADDRESS = "illegal_data_address"
    ILLEGAL_DATA_VALUE = "illegal_data_value"
    SLAVE_DEVICE_FAILURE = "slave_device_failure"
    SLAVE_BUSY = "slave_busy"
    GATEWAY_PATH_UNAVAILABLE = "gateway_path_unavailable"
    GATEWAY_TARGET_FAILED = "gateway_target_failed"
    DROP = "drop"


ACTION_TO_CODE: dict[RuleAction, int] = {
    RuleAction.ILLEGAL_FUNCTION: 0x01,
    RuleAction.ILLEGAL_DATA_ADDRESS: 0x02,
    RuleAction.ILLEGAL_DATA_VALUE: 0x03,
    RuleAction.SLAVE_DEVICE_FAILURE: 0x04,
    RuleAction.SLAVE_BUSY: 0x06,
    RuleAction.GATEWAY_PATH_UNAVAILABLE: 0x0A,
    RuleAction.GATEWAY_TARGET_FAILED: 0x0B,
}


@dataclass(frozen=True, slots=True)
class ExceptionRule:
    """One rule in the injection list.

    ``unit_ids`` empty = match any unit. ``function_codes`` must be non-empty.
    Address overlap is inclusive on both ends.
    """

    name: str
    function_codes: frozenset[int]
    unit_ids: frozenset[int]
    address_start: int
    address_end: int
    action: RuleAction
    delay_ms: float = 0.0
    probability: float = 1.0

    def __post_init__(self) -> None:
        if not self.function_codes:
            raise ValueError("function_codes must be non-empty")
        if self.address_start > self.address_end:
            raise ValueError(
                f"address_start ({self.address_start}) > address_end ({self.address_end})"
            )
        if self.delay_ms < 0:
            raise ValueError(f"delay_ms must be non-negative, got {self.delay_ms}")
        if not 0.0 <= self.probability <= 1.0:
            raise ValueError(f"probability must be in [0, 1], got {self.probability}")


@dataclass(frozen=True, slots=True)
class RuleMatch:
    rule: ExceptionRule
    exception_code: int | None  # None => DROP
    delay_ms: float

    @property
    def is_drop(self) -> bool:
        return self.exception_code is None


class RuleEngine:
    """Ordered list of rules. First match wins."""

    def __init__(
        self,
        rules: Iterable[ExceptionRule] = (),
        *,
        rng: random.Random | None = None,
    ) -> None:
        self._rules: list[ExceptionRule] = list(rules)
        self._rng = rng or random.Random()  # noqa: S311 — probability gate, not crypto

    @property
    def rules(self) -> list[ExceptionRule]:
        return list(self._rules)

    def add_rule(self, rule: ExceptionRule) -> None:
        self._rules.append(rule)

    def remove_rule(self, rule: ExceptionRule) -> None:
        self._rules.remove(rule)

    def clear(self) -> None:
        self._rules.clear()

    def evaluate(
        self,
        *,
        function_code: int,
        unit_id: int,
        address: int,
        count: int,
    ) -> RuleMatch | None:
        for rule in self._rules:
            if not self._matches(rule, function_code, unit_id, address, count):
                continue
            if rule.probability < 1.0 and self._rng.random() >= rule.probability:
                continue
            exception_code = None if rule.action is RuleAction.DROP else ACTION_TO_CODE[rule.action]
            return RuleMatch(rule=rule, exception_code=exception_code, delay_ms=rule.delay_ms)
        return None

    @staticmethod
    def _matches(
        rule: ExceptionRule, function_code: int, unit_id: int, address: int, count: int
    ) -> bool:
        if function_code not in rule.function_codes:
            return False
        if rule.unit_ids and unit_id not in rule.unit_ids:
            return False
        request_end = address + count - 1
        return not (request_end < rule.address_start or address > rule.address_end)
