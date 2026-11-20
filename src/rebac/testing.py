"""Supported test helpers for projects that depend on the library."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from django.db import models, router

from . import backends
from .actors import ActorLike, to_subject_ref
from .backends.local import LocalBackend
from .resources import model_resource_type, to_object_ref
from .schema import parse_zed
from .schema.ast import Schema
from .types import CheckItem, ObjectRef


def install_schema(
    schema: Schema | str, *, backend: LocalBackend | None = None, using: str | None = None
) -> LocalBackend:
    """Make ``backend`` the active backend with ``schema``.

    ``schema`` is a parsed ``Schema`` or ``.zed`` text; it is installed as a
    manual schema, so the stored schema tables are not read. ``backend``
    defaults to a new ``LocalBackend``. After the call ``rebac.backend()``
    returns that instance in this process, and permissions on ``using`` (the
    relationship write alias by default) are decided from the rows already
    stored. ``rebac.backends.reset_backend()`` undoes the installation; call
    it in the test's teardown.
    """
    from .models import active_relationship_model
    from .models.generation import SchemaGeneration

    backends.reset_backend()
    local = backend if backend is not None else LocalBackend()
    local.set_schema(parse_zed(schema) if isinstance(schema, str) else schema)
    backends._backend = local
    alias = using or router.db_for_write(active_relationship_model())
    # A flushed test database (TransactionTestCase) loses the migration's row.
    SchemaGeneration.objects.using(alias).get_or_create(pk=1, defaults={"revision": ""})
    return local


def _member(value: Any, resource_type: str) -> ObjectRef:
    """An expected ``accessible`` member: a wire id of ``resource_type``, an ObjectRef or a resource."""
    if isinstance(value, str):
        return ObjectRef(resource_type, value)
    return value if isinstance(value, ObjectRef) else to_object_ref(value)


def assert_decisions(
    checks: Iterable[tuple[ActorLike, str, Any, bool]] = (),
    accessible: Iterable[tuple[ActorLike, str, Any, Iterable[Any]]] = (),
) -> None:
    """Assert a decision table against the active backend, naming every cell that differs.

    ``checks`` rows are ``(subject, action, resource, allowed)``: ``subject`` is
    any ``ActorLike``, ``resource`` an ``ObjectRef`` or anything
    ``to_object_ref`` resolves, and ``allowed`` whether the check allows (a
    conditional answer does not). They are decided in one
    ``check_bulk_permissions`` call. ``accessible`` rows are
    ``(subject, action, resource_type, ids)``: ``resource_type`` is a type name
    or a resource model, and ``ids`` the complete set ``accessible`` answers,
    as wire ids of that type, ``ObjectRef``s or resource instances; they are
    compared as ``ObjectRef``s, so another type's object never matches. One
    ``AssertionError`` lists every row whose answer differs, with the expected
    and the actual answer (a check's ``PermissionResult``).
    """
    active = backends.backend()
    rows = [
        (to_subject_ref(s), a, r if isinstance(r, ObjectRef) else to_object_ref(r), allowed)
        for s, a, r, allowed in checks
    ]
    answers = active.check_bulk_permissions(CheckItem(s, a, r) for s, a, r, _ in rows)
    failures = [
        f"{s} {a} {r}: expected {allowed}, got {answer.result}"
        for (s, a, r, allowed), answer in zip(rows, answers, strict=True)
        if bool(answer) != allowed
    ]
    for actor, action, target, ids in accessible:
        subject = to_subject_ref(actor)
        resource_type = target if isinstance(target, str) else model_resource_type(target)
        if resource_type is None:
            raise TypeError(f"{target!r} is not a resource type or a resource model")
        expected = {_member(i, resource_type) for i in ids}
        found = active.accessible(subject=subject, action=action, resource_type=resource_type)
        actual = {ObjectRef(resource_type, i) for i in found}
        if actual != expected:
            failures.append(
                f"{subject} {action} {resource_type}: "
                f"expected {sorted(map(str, expected))}, got {sorted(map(str, actual))}"
            )
    if failures:
        raise AssertionError("\n".join([f"{len(failures)} decision(s) differ:", *failures]))


__all__ = ["assert_decisions", "install_schema"]


def compare_mechanisms(
    run: Callable[[], Any],
    mechanisms: Mapping[str, Sequence[Any]],
    *,
    repeats: int = 5,
    using: str = "default",
) -> list[dict[str, Any]]:
    """Run one read under every combination of library settings and report each one's cost and result.

    ``mechanisms`` maps a setting (``"REBAC_ACTOR_PROFILES"``,
    ``"REBAC_STRUCTURAL_PREFETCH"``) to the values to try; the first value of
    each is the baseline. ``run`` performs the read and returns what it read,
    as something comparable: a list of ids, a mapping, a GraphQL data dict.

    Each combination is run once to warm it and then ``repeats`` times. A row
    of the report holds the settings, the statements and SQL size of one run,
    the median and best time, and whether the result equals the baseline's.
    Results are compared by what they loaded: a model instance (the result, a
    mapping's value, or an item of a list, tuple or set, at any depth) as its
    model and the values of the concrete fields it holds, so a row read whole
    and the same row read with fields hidden differ (``_loaded``).
    For two sets, lists or mappings it also says how many items were lost and
    gained (for a mapping an item is a key with its value), and for two
    mappings how many keys both hold with another value (``changed``), each
    item compared by the ``repr`` of that form. The baseline is the first row.

    It changes settings and clears the library's kept statements, so it is for
    a test or a benchmark, never a running application.
    """
    from itertools import product
    from statistics import median
    from time import perf_counter

    from django.db import connections
    from django.test import override_settings

    from .compile import read

    names = list(mechanisms)
    report: list[dict[str, Any]] = []
    baseline: Any = None
    for values in product(*(mechanisms[name] for name in names)):
        chosen = dict(zip(names, values, strict=True))
        with override_settings(**chosen):
            read.reset()
            result = run()
            statements: list[str] = []

            def capture(
                execute: Callable[..., Any],
                sql: str,
                params: Any,
                many: bool,
                context: Any,
                seen: list[str] = statements,
            ) -> Any:
                seen.append(sql)
                return execute(sql, params, many, context)

            with connections[using].execute_wrapper(capture):
                run()
            times = []
            for _ in range(repeats):
                started = perf_counter()
                run()
                times.append((perf_counter() - started) * 1000)
        read.reset()
        row: dict[str, Any] = {
            "settings": chosen,
            "statements": len(statements),
            "sql_kb": round(sum(len(sql) for sql in statements) / 1024, 1),
            "ms": round(median(times), 1),
            "best_ms": round(min(times), 1),
        }
        loaded = _loaded(result)
        if not report:
            baseline = loaded
            row["result"] = "baseline"
        else:
            row["result"] = "same" if loaded == baseline else "differs"
            if loaded != baseline:
                row.update(_difference(baseline, loaded))
        report.append(row)
    return report


def _loaded(value: Any) -> Any:
    """``value`` as ``compare_mechanisms`` compares it: a model instance as what it loaded.

    Django compares two instances by model and primary key alone, so a row read
    whole and the same row read with fields hidden are equal. Here an instance
    is its model's label and the value of each concrete field it holds, by
    attname (a deferred field is left out, and nothing is loaded). A mapping is
    its keys with their values' forms; a list or a tuple is one of its items'
    forms; a set or a frozenset is a frozenset of them, or itself where they
    cannot be hashed; anything else is itself.
    """
    if isinstance(value, models.Model):
        held = vars(value)
        return (
            value._meta.label,
            tuple(
                (field.attname, _loaded(held[field.attname]))
                for field in value._meta.concrete_fields
                if field.attname in held
            ),
        )
    if isinstance(value, Mapping):
        return {key: _loaded(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        try:
            return frozenset(_loaded(item) for item in value)
        except TypeError:
            return value
    if type(value) in (list, tuple):
        return type(value)(_loaded(item) for item in value)
    return value


def _difference(before: Any, after: Any) -> dict[str, int]:
    """How many items a result lost and gained, where both are collections of the same kind.

    ``before`` and ``after`` are compared as ``_loaded`` gives them, each item by
    its ``repr``. For two mappings an item is a key with its value, so a changed
    value is one item lost and one gained; ``changed`` counts the keys both hold
    whose values differ.
    """
    before, after = _loaded(before), _loaded(after)

    def items(value: Any) -> set[Any] | None:
        try:
            if isinstance(value, Mapping):
                return {(key, repr(item)) for key, item in value.items()}
            if isinstance(value, (list, tuple, set, frozenset)):
                return {repr(item) for item in value}
        except TypeError:
            return None
        return None

    old, new = items(before), items(after)
    if old is None or new is None:
        return {}
    found = {"lost": len(old - new), "gained": len(new - old)}
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        try:
            found["changed"] = sum(
                1 for key in before.keys() & after.keys() if repr(before[key]) != repr(after[key])
            )
        except TypeError:
            pass
    return found


def format_mechanisms(report: Sequence[Mapping[str, Any]]) -> str:
    """``compare_mechanisms``'s report as a text table."""
    names = list(report[0]["settings"]) if report else []
    header = [name.removeprefix("REBAC_").lower() for name in names]
    header += ["statements", "sql_kb", "ms", "best_ms", "result"]
    lines = [header]
    for row in report:
        cells = [str(row["settings"][name]) for name in names]
        cells += [str(row[key]) for key in ("statements", "sql_kb", "ms", "best_ms")]
        result = str(row["result"])
        if "lost" in row:
            changed = f", changed {row['changed']}" if "changed" in row else ""
            result += f" (lost {row['lost']}, gained {row['gained']}{changed})"
        lines.append([*cells, result])
    widths = [max(len(line[index]) for line in lines) for index in range(len(header))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip()
        for line in lines
    )
