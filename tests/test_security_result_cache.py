"""A queryset holds rows only once everything that makes them safe to hand out has succeeded.

Django's ``_fetch_all`` fills the result cache, and the library then ran the prefetches,
field redaction and the ``rebac_select_related()`` guards on it. When one of them raised,
the cache kept the rows as they were: iteration, ``len()`` and ``bool()`` ran the work
again, but indexing, slicing, ``first()`` on an ordered queryset, ``repr()``, a later
``count()`` and a paginator's slice read the cache as it stood, unredacted, with a related
row nobody had checked. And the record that the cache had been redacted outlived the
cache: after ``update()``, ``delete()`` or ``copy.deepcopy()`` dropped the rows, the next
evaluation read new ones and redacted nothing. Here each failure point, each path that
reads a held cache, the streaming paths, and the rows Django's prefetch hands a related
manager.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest
from django.core.paginator import Paginator
from django.db import OperationalError
from django.db.models import Prefetch

from rebac import PermissionDenied, field_visibility
from rebac import managers as rebac_managers
from rebac.backends import backend
from tests.mechanism_policies import grant
from tests.test_security_failed_scope import (
    GATED,
    LAYOUTS,
    MEMBER,
    SCHEMA,
    failing_once,
    install,
)
from tests.testapp.models import ScopeMessage, ScopePart, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]

# The member reads message 1 through a relation of its own; its thread is not readable.
OWN_MESSAGE = SCHEMA.replace(
    "    relation thread: scope/thread // rebac:field=thread\n    permission read = thread->read\n",
    "    relation thread: scope/thread // rebac:field=thread\n    relation reader: auth/user\n"
    "    permission read = reader\n",
)
assert OWN_MESSAGE != SCHEMA

# What reading a held cache looks like on each path that does not run the work again.
HELD: dict[str, Any] = {
    "index": lambda rows: rows[0],
    "slice": lambda rows: next(iter(rows[0:3])),
    "first": lambda rows: rows.first(),
    "last": lambda rows: rows.last(),
    "repr": lambda rows: repr(rows),
    "paginator": lambda rows: Paginator(rows, 10).page(1).object_list[0],
}


def _unchecked_threads(settings: Any) -> Any:
    local = install(settings, OWN_MESSAGE)
    local.write_relationships([grant("scope/message", 1, "reader")])
    return ScopeMessage.objects.with_actor(MEMBER).order_by("pk").rebac_select_related("thread")


# ---------- A refused or failed evaluation holds no rows ----------


@pytest.mark.parametrize("path", list(HELD))
def test_rows_a_related_guard_refused_are_not_held(settings, path):
    """Thread 1 is not readable: the guard refuses, and every later read refuses too."""
    rows = _unchecked_threads(settings)
    with pytest.raises(PermissionDenied):
        list(rows)
    assert rows._result_cache is None
    with pytest.raises(PermissionDenied):
        HELD[path](rows)
    with pytest.raises(PermissionDenied):
        rows.count() and list(rows)


def _gated_rows(settings: Any, storage: str) -> Any:
    """The member reads thread 2, not its label."""
    install(settings, GATED, storage=storage)
    settings.REBAC_FIELD_READ_MODE = "redact"
    return ScopeThread.objects.with_actor(MEMBER).order_by("pk")


def _label(found: Any) -> Any:
    return found if isinstance(found, str) else found.label


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("path", list(HELD))
def test_rows_whose_prefetch_failed_are_not_held(settings, monkeypatch, storage, path):
    """The prefetch's own scope raises after the threads are read, before they are
    redacted (``read__label = nil``)."""
    rows = _gated_rows(settings, storage).prefetch_related(
        Prefetch("scopemessage_set", queryset=ScopeMessage.objects.with_actor(MEMBER))
    )
    failing_once(monkeypatch, OperationalError("canceling statement"), model=ScopeMessage)
    with pytest.raises(OperationalError):
        list(rows)
    monkeypatch.undo()
    assert rows._result_cache is None
    found = HELD[path](rows)
    if path == "repr":
        assert "first" not in found and "second" not in found
    else:
        assert found.label is None


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("path", list(HELD))
def test_rows_whose_redaction_failed_are_not_held(settings, monkeypatch, storage, path):
    rows = _gated_rows(settings, storage)
    real = field_visibility.visible_id_sets
    calls: list[int] = []

    def cancelled_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("canceling statement")
        return real(*args, **kwargs)

    monkeypatch.setattr(field_visibility, "visible_id_sets", cancelled_once)
    with pytest.raises(OperationalError):
        list(rows)
    assert rows._result_cache is None
    found = HELD[path](rows)
    if path != "repr":
        assert found.label is None


def test_rows_whose_redaction_failed_are_read_again_with_their_prefetches(settings, monkeypatch):
    """The prefetch ran, then redaction raised: the queryset records neither rows nor a
    prefetch done, so the next evaluation reads both again."""
    rows = _gated_rows(settings, "denormalized").prefetch_related(
        Prefetch("scopemessage_set", queryset=ScopeMessage.objects.with_actor(MEMBER))
    )
    real = field_visibility.visible_id_sets
    calls: list[int] = []

    def cancelled_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("canceling statement")
        return real(*args, **kwargs)

    monkeypatch.setattr(field_visibility, "visible_id_sets", cancelled_once)
    with pytest.raises(OperationalError):
        list(rows)
    assert (rows._result_cache, rows._prefetch_done) == (None, False)
    [thread] = rows
    assert thread.label is None
    prefetched = thread._prefetched_objects_cache["scopemessage_set"]
    assert [message.pk for message in prefetched] == [2]


def test_rows_redacted_in_part_are_not_held(settings, monkeypatch):
    """Redaction marks row by row: it raises on the second row, after the first is
    redacted. Neither is held; the next read redacts both."""
    local = install(settings, GATED)
    local.write_relationships([grant("scope/thread", 1, "reader")])
    settings.REBAC_FIELD_READ_MODE = "redact"
    real = field_visibility._conditional_field_is_visible
    calls: list[int] = []

    def second_fails(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:
            raise OperationalError("canceling statement")
        return real(*args, **kwargs)

    monkeypatch.setattr(field_visibility, "_conditional_field_is_visible", second_fails)
    rows = ScopeThread.objects.with_actor(MEMBER).order_by("pk")
    with pytest.raises(OperationalError):
        list(rows)
    assert rows._result_cache is None
    assert [row.label for row in rows[0:2]] == [None, None]
    assert [row.label for row in rows] == [None, None]


# ---------- The record of redaction belongs to the rows it describes ----------


@pytest.mark.parametrize("drop", ["update", "delete", "deepcopy"])
def test_rows_read_after_the_held_ones_were_dropped_are_redacted(settings, drop):
    """``update()`` and ``delete()`` drop the rows a queryset holds, as Django's do, and
    ``copy.deepcopy()`` copies none. The next evaluation reads rows again: they are
    redacted and guarded as the first ones were."""
    rows = _gated_rows(settings, "denormalized")
    assert [row.label for row in rows] == [None]
    if drop == "update":
        rows.update(label="written")
    elif drop == "delete":
        rows.delete()
        ScopeThread._base_manager.bulk_create([ScopeThread(id=2, label="again")])
        backend().write_relationships([grant("scope/thread", 2, "reader")])
    else:
        rows = copy.deepcopy(rows)
    assert rows._result_cache is None
    assert [row.label for row in rows] == [None]
    assert rows[0].label is None


def test_related_rows_read_after_the_held_ones_were_dropped_are_guarded(settings):
    """The member could read thread 1 when the rows were first read; it no longer can when
    a copy of the queryset reads them again."""
    local = install(settings, OWN_MESSAGE)
    readable = grant("scope/thread", 1, "reader")
    local.write_relationships([grant("scope/message", 1, "reader"), readable])
    rows = ScopeMessage.objects.with_actor(MEMBER).rebac_select_related("thread")
    assert [message.thread.pk for message in rows] == [1]
    local.delete_relationship(readable)
    copied = copy.deepcopy(rows)
    with pytest.raises(PermissionDenied):
        list(copied)
    with pytest.raises(PermissionDenied):
        copied[0]


# ---------- Rows held without being read here ----------


def test_rows_a_prefetch_gave_a_related_manager_are_dropped_when_they_cannot_be_finished(
    settings, monkeypatch
):
    """Django's prefetch hands each related manager the rows it read for it, finished
    by that read. The manager redacts them for its actor once, at its first use; when
    that raises it keeps none, and its next use reads them again."""
    install(settings)
    threads = list(
        ScopeThread.objects.with_actor(MEMBER).prefetch_related(
            Prefetch("scopemessage_set", queryset=ScopeMessage.objects.with_actor(MEMBER))
        )
    )
    [thread] = threads
    held = thread.scopemessage_set.all()
    assert held._result_cache is not None and not held._rebac_visibility_applied
    real = rebac_managers.apply_field_visibility

    def cancelled(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError("canceling statement")

    monkeypatch.setattr(rebac_managers, "apply_field_visibility", cancelled)
    with pytest.raises(OperationalError):
        list(held)
    assert held._result_cache is None
    monkeypatch.setattr(rebac_managers, "apply_field_visibility", real)
    assert [message.pk for message in held] == [2]


def test_held_rows_dropped_are_read_again_with_their_own_prefetches(settings, monkeypatch):
    """A related manager whose prefetch queryset prefetches further holds rows prefetched
    by the read that loaded them. Dropped, they are read again, and prefetched again."""
    install(settings)
    parts = Prefetch("scopepart_set", queryset=ScopePart.objects.with_actor(MEMBER))
    messages = ScopeMessage.objects.with_actor(MEMBER).prefetch_related(parts)
    [thread] = ScopeThread.objects.with_actor(MEMBER).prefetch_related(
        Prefetch("scopemessage_set", queryset=messages)
    )
    held = thread.scopemessage_set.all()
    assert held._prefetch_done and not held._rebac_visibility_applied
    real = rebac_managers.apply_field_visibility

    def cancelled(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError("canceling statement")

    monkeypatch.setattr(rebac_managers, "apply_field_visibility", cancelled)
    with pytest.raises(OperationalError):
        list(held)
    assert (held._result_cache, held._prefetch_done) == (None, False)
    monkeypatch.setattr(rebac_managers, "apply_field_visibility", real)
    [message] = held
    assert [part.pk for part in message._prefetched_objects_cache["scopepart_set"]] == [2]


# ---------- The streaming paths hand out finished chunks only ----------


def test_an_iterator_yields_no_row_of_a_chunk_it_could_not_finish(settings):
    rows = _unchecked_threads(settings)
    pending = rows.iterator(chunk_size=10)
    with pytest.raises(PermissionDenied):
        next(pending)


@pytest.mark.django_db(transaction=True)
def test_an_aiterator_yields_no_row_of_a_chunk_it_could_not_finish(settings):
    rows = _unchecked_threads(settings)

    async def first() -> Any:
        async for row in rows.aiterator(chunk_size=10):
            return row
        return None

    with pytest.raises(PermissionDenied):
        asyncio.run(first())


@pytest.mark.django_db(transaction=True)
def test_async_iteration_holds_no_rows_after_a_refusal(settings):
    rows = _unchecked_threads(settings)

    async def twice() -> Any:
        with pytest.raises(PermissionDenied):
            [row async for row in rows]
        return rows._result_cache

    assert asyncio.run(twice()) is None
