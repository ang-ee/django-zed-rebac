"""Supported test helpers for projects that depend on the library."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db import router

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
    as wire ids or resource instances. One ``AssertionError`` lists every row
    whose answer differs, with the expected and the actual answer.
    """
    active = backends.backend()
    rows = [
        (to_subject_ref(s), a, r if isinstance(r, ObjectRef) else to_object_ref(r), allowed)
        for s, a, r, allowed in checks
    ]
    answers = active.check_bulk_permissions(CheckItem(s, a, r) for s, a, r, _ in rows)
    failures = [
        f"{s} {a} {r}: expected {allowed}, got {bool(answer)}"
        for (s, a, r, allowed), answer in zip(rows, answers, strict=True)
        if bool(answer) != allowed
    ]
    for actor, action, target, ids in accessible:
        subject = to_subject_ref(actor)
        resource_type = target if isinstance(target, str) else model_resource_type(target)
        if resource_type is None:
            raise TypeError(f"{target!r} is not a resource type or a resource model")
        expected = sorted(i if isinstance(i, str) else to_object_ref(i).resource_id for i in ids)
        found = active.accessible(subject=subject, action=action, resource_type=resource_type)
        if sorted(found) != expected:
            failures.append(
                f"{subject} {action} {resource_type}: expected {expected}, got {sorted(found)}"
            )
    if failures:
        raise AssertionError("\n".join([f"{len(failures)} decision(s) differ:", *failures]))


__all__ = ["assert_decisions", "install_schema"]
