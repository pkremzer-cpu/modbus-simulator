"""AST-whitelist sandbox for user-supplied Python expressions.

Used by the Script value generator. The user provides a single Python
expression evaluated with two inputs: ``t`` (seconds since server start,
float) and ``prev`` (previous register value).

Security model
--------------
The sandbox defends in three layers:

1. **Parse-time AST whitelist:** ``compile_script`` parses the source with
   :func:`ast.parse` in ``eval`` mode and walks the tree rejecting any node
   type, name, or attribute not on the whitelist. Anything with a leading
   underscore is banned. Chained attribute access (``foo.bar.baz``) is banned
   — only ``module.name`` patterns work. String and bytes literals are
   rejected; collection literals (``[...]``, ``(...)``) are rejected — these
   close obvious memory-bomb vectors (e.g. ``[0] * 10**8``).

2. **Curated runtime surface:** ``evaluate`` runs ``eval`` with
   ``__builtins__`` set to an empty dict; only the whitelisted callables and
   the curated module namespaces are reachable. ``pow``, ``math.factorial``,
   ``math.perm``, ``math.comb``, ``math.prod`` — common amplification
   primitives — are removed.

3. **Wall-clock timeout:** ``evaluate`` arms a ``SIGALRM`` for a configurable
   duration on the main thread. Pure-Python loops would be interrupted at the
   next bytecode boundary; long C extensions (e.g. ``2 ** 10**7``) cannot be
   pre-empted before they return, so a small residual DoS surface remains and
   is documented.

Allowed names
-------------
- Parameters: ``t``, ``prev``
- Builtins: ``abs``, ``min``, ``max``, ``round``, ``sum``, ``int``,
  ``float``, ``bool``, ``len``
- Modules: ``math`` (public attrs minus ``factorial``/``perm``/``comb``/
  ``prod``), ``random`` (curated), ``time`` (``time``, ``monotonic``)

Rejected
--------
Imports, assignments, comprehensions, lambdas, generators, f-strings, walrus,
yield, del, starred args, subscripts, any dunder, any non-whitelisted module
or attribute, list/tuple literals, string/bytes literals.
"""

from __future__ import annotations

import ast
import math
import random
import signal
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import CodeType
from typing import Any

Number = int | float

DEFAULT_TIMEOUT_S = 0.5


class ScriptCompileError(ValueError):
    """The script failed parse-time validation."""


class ScriptRuntimeError(RuntimeError):
    """The script raised while evaluating."""


class _ScriptTimeoutError(Exception):
    """Internal — raised inside ``eval`` by the SIGALRM handler."""


# ---------------------------------------------------------------------------
# Whitelists
# ---------------------------------------------------------------------------
_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Attribute,
    ast.keyword,
    # NOTE: Tuple and List intentionally NOT here. They open memory-bomb
    # vectors via BinOp(Mult) and are not needed for numeric expressions.
    # operators
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.LShift,
    ast.RShift,
    ast.BitOr,
    ast.BitXor,
    ast.BitAnd,
    ast.MatMult,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.Invert,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
)

_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    # NOTE: ``pow`` removed — use ``**`` operator; pow with huge exponents
    # is a common amplification primitive.
    "sum": sum,
    "int": int,
    "float": float,
    "bool": bool,
    "len": len,
}

# Names removed from math because they amplify input size catastrophically.
_MATH_DANGEROUS = {"factorial", "perm", "comb", "prod"}

_MATH_NAMES = frozenset(
    n for n in dir(math) if not n.startswith("_") and n not in _MATH_DANGEROUS
)
_RANDOM_NAMES = frozenset(
    {
        "random",
        "uniform",
        "gauss",
        "gammavariate",
        "expovariate",
        "triangular",
        "randint",
        "randrange",
        "choice",
        "shuffle",
        "seed",
    }
)
_TIME_NAMES = frozenset({"time", "monotonic"})

_MODULE_ATTRS: dict[str, frozenset[str]] = {
    "math": _MATH_NAMES,
    "random": _RANDOM_NAMES,
    "time": _TIME_NAMES,
}

_RESERVED_PARAMS = frozenset({"t", "prev"})
_ALLOWED_NAMES: frozenset[str] = (
    _RESERVED_PARAMS | frozenset(_SAFE_BUILTINS) | frozenset(_MODULE_ATTRS)
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class _Validator(ast.NodeVisitor):
    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, _ALLOWED_NODES):
            raise ScriptCompileError(f"disallowed syntax: {type(node).__name__}")
        super().generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # Reject string / bytes literals — they enable memory bombs via
        # multiplication ("x" * 10**8) and offer no value for numeric scripts.
        if isinstance(node.value, str | bytes):
            raise ScriptCompileError(
                f"{type(node.value).__name__} literals are forbidden in scripts"
            )

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("_"):
            raise ScriptCompileError(f"names with leading underscore are forbidden: {node.id}")
        if node.id not in _ALLOWED_NAMES:
            raise ScriptCompileError(f"unknown name: {node.id}")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            raise ScriptCompileError(f"dunder/private attribute forbidden: {node.attr}")
        if not isinstance(node.value, ast.Name):
            raise ScriptCompileError(
                "chained attribute access is forbidden (only module.name is allowed)"
            )
        module = node.value.id
        allowed = _MODULE_ATTRS.get(module)
        if allowed is None:
            raise ScriptCompileError(f"attribute access on {module!r} is forbidden")
        if node.attr not in allowed:
            raise ScriptCompileError(f"{module}.{node.attr} is not whitelisted")

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if keyword.arg is None:
                # **kwargs expansion
                raise ScriptCompileError("**kwargs expansion is forbidden")
        self.generic_visit(node)


def _parse_and_validate(source: str) -> ast.Expression:
    source = source.strip()
    if not source:
        raise ScriptCompileError("empty script")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ScriptCompileError(f"syntax error: {exc.msg}") from exc
    _Validator().visit(tree)
    assert isinstance(tree, ast.Expression)
    return tree


# ---------------------------------------------------------------------------
# Wall-clock timeout — best effort.
#
# SIGALRM only interrupts Python bytecodes, not single C-level operations. So
# pathological scripts like ``2 ** 10**7`` may run past the deadline before
# the handler can fire. For typical operator misuse (slow loops, recursive
# calculations), the alarm is sufficient.
# ---------------------------------------------------------------------------
@contextmanager
def _wall_clock_alarm(timeout_s: float) -> Iterator[bool]:
    """Arm SIGALRM for ``timeout_s`` if running on Unix main thread."""
    if not hasattr(signal, "SIGALRM"):
        yield False
        return
    if threading.current_thread() is not threading.main_thread():
        yield False
        return

    def _handler(_signum: int, _frame: object) -> None:
        raise _ScriptTimeoutError()

    old_handler = signal.signal(signal.SIGALRM, _handler)
    try:
        signal.setitimer(signal.ITIMER_REAL, timeout_s)
        yield True
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


# ---------------------------------------------------------------------------
# CompiledScript
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class CompiledScript:
    source: str
    _code: CodeType

    def evaluate(
        self,
        t: float,
        prev: Number,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> Number:
        env = {
            "__builtins__": {},
            **_SAFE_BUILTINS,
            "math": math,
            "random": random,
            "time": time,
            "t": t,
            "prev": prev,
        }
        try:
            with _wall_clock_alarm(timeout_s):
                result = eval(self._code, env, {})
        except _ScriptTimeoutError:
            raise ScriptRuntimeError(
                f"script exceeded {timeout_s:.3f}s wall-clock timeout"
            ) from None
        except Exception as exc:
            raise ScriptRuntimeError(f"{type(exc).__name__}: {exc}") from exc

        # Normalise bool -> int so callers can rely on the documented
        # ``Number = int | float`` contract without isinstance(value, bool)
        # corner cases.
        if isinstance(result, bool):
            result = int(result)
        if not isinstance(result, int | float):
            raise ScriptRuntimeError(
                f"script must return int or float, got {type(result).__name__}"
            )
        return result


def compile_script(source: str) -> CompiledScript:
    """Parse, validate, and compile a user script for repeated evaluation."""
    tree = _parse_and_validate(source)
    code = compile(tree, "<script>", "eval")
    return CompiledScript(source=source, _code=code)
