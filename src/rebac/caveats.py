"""Caveat compilation and evaluation.

Caveats are CEL expressions parameterised by typed inputs that come from two
sources:

  - **static context**: stored on the `Relationship` row at write time
    (`Relationship.caveat_context`). Common pattern: pin the high half
    (`expires_at`) on the row and let the request supply the low half
    (`now`).
  - **dynamic context**: passed to `check_access(context=...)` at check time.

The two are merged with **static > dynamic** precedence, mirroring SpiceDB.
Request context can supply missing inputs but cannot override pinned policy.

Evaluation is tri-state:

  - `(True, ())`   — caveat is satisfied; the row counts.
  - `(False, ())`  — caveat denies; the row is treated as if absent.
  - `(None, missing)` — required parameters are missing in *both* contexts;
    the row is conditional. The caller surfaces this as
    `CheckResult.conditional(missing=...)` when no unconditional path
    matches.

`cel-python` is an optional dependency; importing this module never imports
`celpy`. The first call to `compile_caveat()` triggers the import and raises
`CaveatUnsupportedError` (with a `pip install` hint) if `celpy` is absent.
That keeps installations without caveat usage zero-cost.
"""

from __future__ import annotations

import hashlib
import importlib.util
from threading import Lock
from typing import TYPE_CHECKING, Any

from .errors import CaveatUnsupportedError
from .schema.ast import Caveat

if TYPE_CHECKING:
    # Imported only for type-checkers. The runtime import is lazy.
    import celpy  # noqa: F401


# Module-level compile cache, keyed by (caveat_name, expression_hash) so a
# silent edit-in-place to a caveat body re-compiles. The hash is sha256 over
# the raw expression text — deterministic and collision-resistant for our
# use.
_compile_cache: dict[tuple[str, str], Any] = {}
_compile_lock = Lock()


# Lazy module handle for celpy. None = not yet attempted; the
# CaveatUnsupportedError sentinel = attempted and failed.
_CELPY_MODULE: Any = None
_CELPY_TRIED: bool = False


def _load_celpy() -> Any:
    """Import `celpy` on first use. Raises `CaveatUnsupportedError` if absent."""
    global _CELPY_MODULE, _CELPY_TRIED
    if _CELPY_TRIED:
        if _CELPY_MODULE is None:
            raise CaveatUnsupportedError(
                "Caveat evaluation requires cel-python. "
                "Install with `pip install django-zed-rebac[caveats]`."
            )
        return _CELPY_MODULE
    _CELPY_TRIED = True
    # Presence check first so the import below can't raise — keeps the
    # optional dependency truly optional without guarding the import.
    if importlib.util.find_spec("celpy") is None:  # pragma: no cover
        raise CaveatUnsupportedError(
            "Caveat evaluation requires cel-python. "
            "Install with `pip install django-zed-rebac[caveats]`."
        )
    import celpy as _cel

    _CELPY_MODULE = _cel
    return _cel


def _expression_hash(expression: str) -> str:
    return hashlib.sha256(expression.encode("utf-8")).hexdigest()


def compile_caveat(caveat: Caveat) -> Any:
    """Compile a CEL expression once; cached at module level.

    Cache key: `(caveat.name, sha256(caveat.expression))`. Editing a caveat's
    body invalidates only that entry. Returns the celpy `Runner` (the program
    instance produced by `Environment.program(ast)`).
    """
    key = (caveat.name, _expression_hash(caveat.expression))
    cached = _compile_cache.get(key)
    if cached is not None:
        return cached
    with _compile_lock:
        # Re-check inside the lock.
        cached = _compile_cache.get(key)
        if cached is not None:
            return cached
        cel = _load_celpy()
        env = cel.Environment()
        try:
            ast = env.compile(caveat.expression)
        except Exception as exc:  # CEL parse error
            raise CaveatUnsupportedError(
                f"Failed to compile caveat {caveat.name!r}: {exc}"
            ) from exc
        program = env.program(ast)
        _compile_cache[key] = program
        return program


def _coerce_param(value: Any, type_name: str, cel: Any) -> Any:
    """Map a Python / JSON value to the appropriate CEL type.

    `caveat_context` round-trips through JSON (it lives in a `JSONField`), so
    timestamps land as ISO 8601 strings. We rebuild the proper CEL types so
    `<` / `>` semantics work as users expect.
    """
    if value is None:
        return None
    ct = cel.celtypes
    # Strip generic suffix: `list<string>` -> `list`. We only do best-effort
    # type coercion; CEL itself enforces the operators.
    base = type_name.split("<", 1)[0].strip()
    if base == "timestamp":
        if isinstance(value, ct.TimestampType):
            return value
        return ct.TimestampType(value)
    if base == "duration":
        if isinstance(value, ct.DurationType):
            return value
        return ct.DurationType(value)
    if base == "int":
        return ct.IntType(value)
    if base == "uint":
        return ct.UintType(value)
    if base in ("double", "float"):
        return ct.DoubleType(value)
    if base == "bool":
        if not isinstance(value, (bool, ct.BoolType)):
            raise CaveatUnsupportedError("A bool caveat parameter requires a boolean value")
        return ct.BoolType(value)
    if base == "string":
        return ct.StringType(value)
    if base == "bytes":
        return ct.BytesType(value)
    # Unknown / unsupported (e.g. ipaddress) — let CEL surface its own error
    # if used. Returning the raw value is the safest fallback.
    return value


def evaluate(
    caveat: Caveat,
    static_context: dict[str, Any] | None,
    dynamic_context: dict[str, Any] | None,
) -> tuple[bool | None, tuple[str, ...]]:
    """Evaluate a caveat against the union of static + dynamic context.

    Returns:
        `(True, ())`   — caveat satisfied.
        `(False, ())`  — caveat denies.
        `(None, missing)` — required params missing in both contexts; tuple
        of names is sorted (deterministic).

    `static_context` (write-time) takes precedence over `dynamic_context`
    (request-time) when a key is present in both, as in SpiceDB.
    """
    static = static_context or {}
    dynamic = dynamic_context or {}

    # Determine the set of params actually referenced. We use the declared
    # `caveat.params` as the contract: if the schema says a param is part of
    # the caveat signature, the caller must supply it (or accept CONDITIONAL).
    declared = {p.name: p.type for p in caveat.params}

    missing: list[str] = []
    for name in declared:
        if name not in static and name not in dynamic:
            missing.append(name)

    if missing:
        # Sorted for deterministic surfaces (tests, audit logs).
        return None, tuple(sorted(missing))

    # All declared params are present somewhere; build the CEL activation.
    cel = _load_celpy()
    activation: dict[str, Any] = {}
    for name, type_name in declared.items():
        # Stored policy wins when both supply.
        raw = static[name] if name in static else dynamic[name]
        activation[name] = _coerce_param(raw, type_name, cel)

    # Surface anything else the caller passed (caveats can reference globals
    # like `request.ip` if the schema declares them; we already handled
    # declared params above). Everything else is best-effort opaque.
    for name, raw in dynamic.items():
        if name in declared or name in activation:
            continue
        activation[name] = raw
    for name, raw in static.items():
        if name in declared:
            continue
        activation[name] = raw

    program = compile_caveat(caveat)
    try:
        result = program.evaluate(activation)
    except cel.CELEvalError as exc:
        # Most commonly: a downstream sub-expression references an undeclared
        # variable. Treat as CONDITIONAL when the underlying error is a
        # KeyError (missing var); otherwise propagate as a CaveatUnsupportedError.
        cause = exc.args[1] if len(exc.args) > 1 else None
        names = exc.args[2] if len(exc.args) > 2 else None
        if cause is KeyError and isinstance(names, tuple):
            return None, tuple(sorted(str(n) for n in names))
        raise CaveatUnsupportedError(
            f"Caveat {caveat.name!r} failed to evaluate: {exc.args[0] if exc.args else exc}"
        ) from exc

    if not isinstance(result, (bool, cel.celtypes.BoolType)):
        raise CaveatUnsupportedError(f"Caveat {caveat.name!r} must evaluate to a boolean")
    return bool(result), ()


def reset_cache() -> None:
    """Test ergonomics: drop the compile cache."""
    with _compile_lock:
        _compile_cache.clear()


__all__ = ["compile_caveat", "evaluate", "reset_cache"]
