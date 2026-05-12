"""Tests for modbus_simulator.core.script_sandbox.

Sandbox goals:
  1) Parse-time: reject anything outside a tight AST whitelist.
  2) Runtime: expose only ``t``, ``prev``, and curated math/random/time names.
"""

from __future__ import annotations

import math

import pytest

from modbus_simulator.core.script_sandbox import (
    CompiledScript,
    ScriptCompileError,
    ScriptRuntimeError,
    compile_script,
)


# ---------------------------------------------------------------------------
# Happy path — allowed constructs
# ---------------------------------------------------------------------------
class TestAllowed:
    def test_constant(self) -> None:
        s = compile_script("42")
        assert s.evaluate(0.0, 0) == 42

    def test_t_parameter(self) -> None:
        s = compile_script("t * 2")
        assert s.evaluate(5.0, 0) == 10.0

    def test_prev_parameter(self) -> None:
        s = compile_script("prev + 1")
        assert s.evaluate(0.0, 7) == 8

    def test_math_sin(self) -> None:
        s = compile_script("math.sin(t)")
        assert math.isclose(s.evaluate(math.pi / 2, 0), 1.0)

    def test_math_pi_constant(self) -> None:
        s = compile_script("math.pi")
        assert math.isclose(s.evaluate(0.0, 0), math.pi)

    def test_random_uniform(self) -> None:
        s = compile_script("random.uniform(0, 1)")
        for _ in range(20):
            v = s.evaluate(0.0, 0)
            assert 0.0 <= float(v) <= 1.0

    def test_time_time(self) -> None:
        s = compile_script("time.time()")
        v = s.evaluate(0.0, 0)
        assert isinstance(v, float) and v > 1_700_000_000

    def test_conditional(self) -> None:
        s = compile_script("100 if t > 0 else -100")
        assert s.evaluate(1.0, 0) == 100
        assert s.evaluate(-1.0, 0) == -100

    def test_abs(self) -> None:
        s = compile_script("abs(prev - 100)")
        assert s.evaluate(0.0, 95) == 5

    def test_min_max(self) -> None:
        assert compile_script("min(1, 2, 3)").evaluate(0.0, 0) == 1
        assert compile_script("max(1, 2, 3)").evaluate(0.0, 0) == 3

    def test_chained_arithmetic(self) -> None:
        s = compile_script("(t * 10) + math.sin(t) * 5")
        assert math.isclose(s.evaluate(0.0, 0), 0.0)

    def test_bitwise(self) -> None:
        s = compile_script("prev ^ 0xFF")
        assert s.evaluate(0.0, 0x0F) == 0xF0

    def test_boolean_int(self) -> None:
        s = compile_script("int(t > 0)")
        assert s.evaluate(1.0, 0) == 1
        assert s.evaluate(-1.0, 0) == 0


# ---------------------------------------------------------------------------
# Rejections — security-critical
# ---------------------------------------------------------------------------
class TestRejected:
    @pytest.mark.parametrize(
        "source",
        [
            "import os",
            "from os import path",
            "__import__('os')",
            "open('/etc/passwd')",
            "exec('print(1)')",
            "eval('1+1')",
            "()  .__class__",
            "().__class__.__base__.__subclasses__()",
            "lambda x: x + 1",
            "[x for x in range(10)]",
            "(x for x in range(10))",
            "{k: v for k, v in items}",
            "{1, 2, 3}",
            "f'{t}'",
            "a := 1",
            "yield 1",
            "x = 1",
            "del prev",
            "*args",
            "globals()",
            "locals()",
            "dir()",
            "getattr(math, 'sin')",
            "setattr(math, 'x', 1)",
            "compile('1', '<s>', 'eval')",
            "math.__dict__",
            "math.sin.__globals__",
            "prev._value",
            "math._name",
            "sys.modules",
            "os.system('ls')",
            "{}.__class__",
            "type(t)",
            "vars()",
        ],
    )
    def test_compile_rejects(self, source: str) -> None:
        with pytest.raises(ScriptCompileError):
            compile_script(source)

    def test_non_whitelisted_module(self) -> None:
        with pytest.raises(ScriptCompileError):
            compile_script("os.getcwd()")

    def test_chained_attribute(self) -> None:
        with pytest.raises(ScriptCompileError):
            compile_script("math.sin(t).real")

    def test_subscript_rejected(self) -> None:
        with pytest.raises(ScriptCompileError):
            compile_script("math.__dict__['sin']")

    def test_random_non_whitelisted_attr(self) -> None:
        with pytest.raises(ScriptCompileError):
            compile_script("random.SystemRandom()")


# ---------------------------------------------------------------------------
# Runtime errors
# ---------------------------------------------------------------------------
class TestRuntimeErrors:
    def test_division_by_zero(self) -> None:
        s = compile_script("1 / 0")
        with pytest.raises(ScriptRuntimeError):
            s.evaluate(0.0, 0)

    def test_math_domain_error(self) -> None:
        s = compile_script("math.sqrt(-1)")
        with pytest.raises(ScriptRuntimeError):
            s.evaluate(0.0, 0)

    def test_type_error(self) -> None:
        # String literals are rejected at compile time, so trigger a runtime
        # TypeError through ``len`` on a non-iterable instead.
        s = compile_script("len(math.pi)")
        with pytest.raises(ScriptRuntimeError):
            s.evaluate(0.0, 0)


# ---------------------------------------------------------------------------
# Compile errors surface syntax problems cleanly
# ---------------------------------------------------------------------------
class TestSyntaxErrors:
    def test_syntax_error(self) -> None:
        with pytest.raises(ScriptCompileError):
            compile_script("1 +")

    def test_empty(self) -> None:
        with pytest.raises(ScriptCompileError):
            compile_script("")


class TestReuse:
    def test_compiled_reusable(self) -> None:
        s: CompiledScript = compile_script("prev + 1")
        for i in range(100):
            assert s.evaluate(0.0, i) == i + 1


# ---------------------------------------------------------------------------
# Hardened surface (post-validation patches)
# ---------------------------------------------------------------------------
class TestHardenedSurface:
    """Each item below was an attack vector closed off after the audit."""

    def test_list_literal_rejected(self) -> None:
        with pytest.raises(ScriptCompileError):
            compile_script("[0] * 1000000")

    def test_tuple_literal_rejected(self) -> None:
        with pytest.raises(ScriptCompileError):
            compile_script("(1, 2, 3)")

    def test_string_literal_rejected(self) -> None:
        with pytest.raises(ScriptCompileError):
            compile_script("'x' * 1000000")

    def test_bytes_literal_rejected(self) -> None:
        with pytest.raises(ScriptCompileError):
            compile_script("b'x' * 1000000")

    def test_pow_builtin_removed(self) -> None:
        # The `pow` builtin is no longer in scope; use `**` instead.
        with pytest.raises(ScriptCompileError):
            compile_script("pow(2, 10)")

    def test_pow_operator_still_works(self) -> None:
        assert compile_script("2 ** 10").evaluate(0.0, 0) == 1024

    @pytest.mark.parametrize("name", ["factorial", "perm", "comb", "prod"])
    def test_math_amplifiers_removed(self, name: str) -> None:
        with pytest.raises(ScriptCompileError):
            compile_script(f"math.{name}(10)")

    def test_math_sin_still_works(self) -> None:
        import math as math_mod

        assert math.isclose(
            compile_script("math.sin(math.pi / 2)").evaluate(0.0, 0),
            math_mod.sin(math_mod.pi / 2),
        )


class TestReturnTypeNormalisation:
    def test_bool_true_normalised_to_int_one(self) -> None:
        v = compile_script("1 == 1").evaluate(0.0, 0)
        assert v == 1 and type(v) is int

    def test_bool_false_normalised_to_int_zero(self) -> None:
        v = compile_script("1 == 2").evaluate(0.0, 0)
        assert v == 0 and type(v) is int


class TestTimeout:
    """Best-effort wall-clock timeout. C-only ops are not interruptible, so we
    can only verify timeout works for callable chains that yield to the Python
    interpreter (which most user-level usage falls under)."""

    def test_explicit_short_timeout_is_accepted(self) -> None:
        # Sanity: passing a custom timeout doesn't break normal evaluation.
        s = compile_script("t + prev")
        assert s.evaluate(1.0, 2, timeout_s=1.0) == 3

    def test_default_timeout_attr_exists(self) -> None:
        from modbus_simulator.core.script_sandbox import DEFAULT_TIMEOUT_S

        assert DEFAULT_TIMEOUT_S > 0


class TestRejectedAdditions:
    """Extras for nodes that are not in the allowlist after hardening."""

    @pytest.mark.parametrize(
        "source",
        [
            "[1, 2, 3]",
            "(1, 2, 3)",
            "'hello'",
            "b'hello'",
            "'a' + 'b'",
            "factorial(5)",
            "pow(2, 8)",
        ],
    )
    def test_compile_rejects(self, source: str) -> None:
        with pytest.raises(ScriptCompileError):
            compile_script(source)
