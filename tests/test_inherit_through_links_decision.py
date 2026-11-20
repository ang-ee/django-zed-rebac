"""The policy is a dimension of every scope decision (proposal 0023, the 0.38.0 rule).

A queryset reuses the scope its query carries when it is evaluated, or compiled into
another statement, under the same decision. The published policy and the policy without
inheritance through links share a revision, so the revision fence of a compiled statement
never closes a scope built under the other one, and the published one grants more. The
decision therefore carries the policy the scope reads (``_Decision.policy``): a scope built
under one value of ``REBAC_INHERIT_THROUGH_LINKS`` is built again under the other, at every
evaluation, in every position a queryset can be embedded in, for writes that select their
rows by a scope, in both directions of the change, in and out of an evaluator scope and in
both storage layouts. An eager ``scoped()`` queryset keeps the policy it was built under, as
it keeps its actor, and so does a scope built outside a queryset (a ``Q`` from
``queryset_filter``, an expression from ``LocalBackend.permission_expression``). With the
setting unchanged the same decision builds nothing again.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.models import Count, Exists, F, OuterRef
from django.test.utils import CaptureQueriesContext

import rebac.managers as rebac_managers
from rebac import (
    PermissionDenied,
    SubjectRef,
    actor_context,
    evaluator_scope,
    permission_expression,
    sudo,
)
from rebac.backends import backend
from rebac.conf import app_settings
from rebac.managers import RebacQuerySet
from rebac.schema.parser import parse_zed
from rebac.testing import install_schema
from tests.mechanism_policies import grant
from tests.test_security_failed_scope import LAYOUTS
from tests.test_security_scope_decision import POSITIONS, _outer
from tests.testapp.models import ScopeMessage, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
MEMBER = SubjectRef.of("auth/user", "member")
SETTING = "REBAC_INHERIT_THROUGH_LINKS"
# (built, read): the value of the setting a queryset is built under, and the one it is read under.
DIRECTIONS = [(True, False), (False, True)]
DIRECTION_IDS = ["built published, read narrowed", "built narrowed, read published"]


def flip(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Change the setting as a running process sees it, with no signal and no reset."""
    monkeypatch.setitem(app_settings._cache, SETTING, value)


@contextmanager
def scope(evaluator: bool) -> Iterator[None]:
    with evaluator_scope() if evaluator else nullcontext():
        yield


THREADS = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation pinned: scope/message
    permission read = reader + pinned->read
    permission write = read
    permission delete = read
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission read = direct + thread->read
    permission delete = thread->delete
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
    permission delete = message->delete
}
"""
# The threads the member reads under each policy. Thread 1 pins message 2, which the member
# reads through its thread, 2: the published policy reads thread 1 through that link, the
# narrowed one does not (a message lends its own ``direct``, not its thread's read).
READS = {True: [1, 2], False: [2]}


def install_threads(settings: Any, storage: str) -> None:
    """Threads 1-3, message ``n`` in thread ``n``; the member reads thread 2."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(THREADS)
    ScopeThread._base_manager.bulk_create(
        [ScopeThread(id=n, label=label) for n, label in ((1, "first"), (2, "second"), (3, "third"))]
    )
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=n, thread_id=n) for n in (1, 2, 3)])
    local.write_relationships(
        [
            grant("scope/thread", 2, "reader"),
            grant("scope/thread", 1, "pinned", SubjectRef.of("scope/message", "2")),
        ]
    )


def pks(rows: Any) -> list[int]:
    return sorted(row.pk for row in rows)


# ---------- The reviewer's reproduction: embedded in a statement REBAC does not scope ----------

PLAIN_STATEMENTS: dict[str, Callable[[Any], Any]] = {
    "In": lambda inner: ScopeThread._base_manager.filter(pk__in=inner),
    "In over values()": lambda inner: ScopeThread._base_manager.filter(pk__in=inner.values("pk")),
    "Exists": lambda inner: ScopeThread._base_manager.filter(
        Exists(inner.filter(pk=OuterRef("pk")))
    ),
}


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("evaluator", [False, True], ids=["outside", "evaluator scope"])
@pytest.mark.parametrize("shape", list(PLAIN_STATEMENTS))
@pytest.mark.parametrize(("built", "read"), DIRECTIONS, ids=DIRECTION_IDS)
def test_a_queryset_embedded_under_one_policy_is_read_under_the_other(
    settings, monkeypatch, storage, evaluator, shape, built, read
):
    """``Thread._base_manager.filter(pk__in=Thread.objects.with_actor(member))``, resolved
    under one value and compiled under the other, read the published threads under the
    narrowed policy, at every evaluation of the outer queryset and its clones."""
    install_threads(settings, storage)
    with scope(evaluator):
        flip(monkeypatch, built)
        outer = PLAIN_STATEMENTS[shape](ScopeThread.objects.with_actor(MEMBER))
        flip(monkeypatch, read)
        assert pks(outer) == READS[read]
        assert pks(outer.filter(pk__gt=0)) == READS[read]
        flip(monkeypatch, built)
        assert pks(outer.all()) == READS[built]


# ---------- Every position an embedded queryset is decided in ----------

POSITIONS_POLICY = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation sibling: scope/thread
    permission read = reader
    permission via = sibling->read
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation linked: scope/thread
    permission read = linked->via
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
}
"""


def install_positions(settings: Any, storage: str) -> None:
    """The member reads the three threads under both policies, and message 2 under the
    published one alone: it is linked to thread 1, whose ``via`` is its sibling's read,
    which a thread does not lend. ``POSITIONS`` then reads what the member reads under the
    published policy where it reads the member's rows, and under the narrowed one what it
    reads for an actor who reads no message."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(POSITIONS_POLICY)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=n) for n in (1, 2, 3)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=n, thread_id=n) for n in (1, 2, 3)])
    local.write_relationships(
        [grant("scope/thread", n, "reader") for n in (1, 2, 3)]
        + [
            grant("scope/message", 2, "linked", SubjectRef.of("scope/thread", "1")),
            grant("scope/thread", 1, "sibling", SubjectRef.of("scope/thread", "3")),
        ]
    )


def last_statement(read: Callable[[Any], Any], rows: Any) -> tuple[Any, str]:
    with CaptureQueriesContext(connection) as captured:
        found = read(rows)
    sent = [query["sql"] for query in captured.captured_queries if "testapp_scope" in query["sql"]]
    return found, sent[-1]


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("case", ["changes", "pinned"])
@pytest.mark.parametrize("position", list(POSITIONS))
@pytest.mark.parametrize(("built", "read_under"), DIRECTIONS, ids=DIRECTION_IDS)
def test_an_embedded_scope_is_decided_under_the_policy_of_each_statement(
    settings, monkeypatch, storage, case, position, built, read_under
):
    """Built under one value of the setting and read under the other, as it is, as a clone
    and inside an evaluator scope: every position of the statement reads the policy in
    force, whether the outer's own decision changes with the policy (``changes``) or not (its
    own bypass, ``pinned``), and sends, byte for byte, the statement built under it."""
    install_positions(settings, storage)
    build, read, published, narrowed = POSITIONS[position]
    expected = {True: published, False: narrowed}
    flip(monkeypatch, built)
    with actor_context(MEMBER):
        rows = build(_outer(case))
        assert read(rows.all()) == expected[built]
    flip(monkeypatch, read_under)
    with actor_context(MEMBER):
        fresh = last_statement(read, build(_outer(case)))
        assert fresh[0] == expected[read_under]
        assert last_statement(read, rows) == fresh
        assert last_statement(read, rows.all()) == fresh
        with evaluator_scope():
            assert read(rows.all()) == expected[read_under]
    flip(monkeypatch, built)
    with actor_context(MEMBER):
        assert read(rows.all()) == expected[built]


# ---------- Every evaluation of one queryset object ----------

# What each path reads of thread 1 under each policy: the member reads it published only.
PATHS: dict[str, tuple[Callable[[Any], Any], Any, Any]] = {
    "list": (pks, [1], []),
    "len": (len, 1, 0),
    "bool": (bool, True, False),
    "count": (lambda rows: rows.count(), 1, 0),
    "exists": (lambda rows: rows.exists(), True, False),
    "iterator": (lambda rows: pks(rows.iterator(chunk_size=10)), [1], []),
    "aggregate": (lambda rows: rows.aggregate(n=Count("pk"))["n"], 1, 0),
    # The same object's query, embedded: decided by the statement that compiles it.
    "lookup on its query": (
        lambda rows: pks(ScopeThread._base_manager.filter(pk__in=rows.query)),
        [1],
        [],
    ),
    "Exists": (
        lambda rows: ScopeThread._base_manager.filter(Exists(rows)).count(),
        3,
        0,
    ),
    "deep copy": (lambda rows: pks(copy.deepcopy(rows)), [1], []),
}
UNCACHED = ("count", "exists", "aggregate", "iterator")


def answer(path: str, value: bool) -> Any:
    return PATHS[path][1] if value else PATHS[path][2]


@pytest.mark.parametrize(
    ("storage", "evaluator"),
    [("denormalized", False), ("registry", True)],
    ids=["denormalized", "registry, evaluator scope"],
)
@pytest.mark.parametrize("second", list(PATHS))
@pytest.mark.parametrize("first", UNCACHED)
@pytest.mark.parametrize(("built", "read"), DIRECTIONS, ids=DIRECTION_IDS)
def test_each_evaluation_of_one_queryset_is_decided_under_the_policy_in_force(
    settings, monkeypatch, storage, evaluator, second, first, built, read
):
    """Evaluated under one value by a path that leaves no result cache, then by any path
    under the other: the scope is built again. Before, the decision compared the actor, the
    bypass and the database only, and ``iterator()``, a count or an embedding ran the scope of
    the other policy."""
    install_threads(settings, storage)
    with scope(evaluator):
        flip(monkeypatch, built)
        rows = ScopeThread.objects.with_actor(MEMBER).filter(pk=1)
        assert PATHS[first][0](rows) == answer(first, built)
        flip(monkeypatch, read)
        assert PATHS[second][0](rows) == answer(second, read)


async def _collect(rows: Any) -> list[int]:
    return sorted([row.pk async for row in rows])


async def _acollect(rows: Any) -> list[int]:
    return sorted([row.pk async for row in rows.aiterator(chunk_size=10)])


ASYNC: dict[str, tuple[Callable[[Any], Any], Any, Any]] = {
    "async for": (_collect, [1], []),
    "aiterator": (_acollect, [1], []),
    "acount": (lambda rows: rows.acount(), 1, 0),
    "aexists": (lambda rows: rows.aexists(), True, False),
    "aaggregate": (lambda rows: rows.aaggregate(n=Count("pk")), {"n": 1}, {"n": 0}),
}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("second", list(ASYNC))
@pytest.mark.parametrize("first", ["aiterator", "acount", "aexists", "aaggregate"])
@pytest.mark.parametrize(("built", "read"), DIRECTIONS, ids=DIRECTION_IDS)
def test_the_async_forms_are_decided_under_the_policy_in_force(
    settings, monkeypatch, second, first, built, read
):
    install_threads(settings, "denormalized")
    rows = ScopeThread.objects.with_actor(MEMBER).filter(pk=1)

    async def scenario() -> tuple[Any, Any]:
        flip(monkeypatch, built)
        before = await ASYNC[first][0](rows)
        flip(monkeypatch, read)
        return before, await ASYNC[second][0](rows)

    expected = {name: {True: entry[1], False: entry[2]} for name, entry in ASYNC.items()}
    assert asyncio.run(scenario()) == (expected[first][built], expected[second][read])


@pytest.mark.parametrize(("built", "read"), DIRECTIONS, ids=DIRECTION_IDS)
def test_an_iterator_after_an_evaluation_is_decided_under_the_policy_in_force(
    settings, monkeypatch, built, read
):
    """The reviewer's case: a list under one value, then ``iterator()`` under the other, which
    ran the first policy's statement."""
    install_threads(settings, "denormalized")
    flip(monkeypatch, built)
    rows = ScopeThread.objects.with_actor(MEMBER)
    assert pks(rows) == READS[built]
    flip(monkeypatch, read)
    assert pks(rows.iterator()) == READS[read]
    # Rows the queryset holds are handed out as Django hands them out (0.38.0).
    assert pks(rows) == READS[built]


ITERATED: dict[str, Callable[[], Any]] = {
    "its own scope": lambda: ScopeThread.objects.with_actor(MEMBER),
    "embedded in a plain statement": lambda: ScopeThread._base_manager.filter(
        pk__in=ScopeThread.objects.with_actor(MEMBER)
    ),
    "embedded in a scoped statement": lambda: ScopeMessage.objects.with_actor(MEMBER).filter(
        thread__in=ScopeThread.objects.with_actor(MEMBER)
    ),
}


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("shape", list(ITERATED))
@pytest.mark.parametrize(("built", "read"), DIRECTIONS, ids=DIRECTION_IDS)
def test_an_iterator_called_under_one_value_and_read_under_the_other(
    settings, monkeypatch, storage, shape, built, read
):
    """The reviewer's case: ``iterator()`` decides the actor, the bypass and the database where
    it is called, and its statement compiles at the first rows, where its embedded querysets
    are decided. The policy is decided there too, for the statement and what it embeds alike:
    called under one value and read under the other, it reads what a fresh read does, and
    builds its own scope once more. (Message ``n`` is in thread ``n`` and read through it.)"""
    install_threads(settings, storage)
    built_scopes: list[Any] = []
    real = RebacQuerySet._scope_query

    def counting(self: Any, query: Any, actor: Any, **kwargs: Any) -> Any:
        if "root" not in kwargs and self.model is ScopeThread:
            built_scopes.append(actor)
        return real(self, query, actor, **kwargs)

    flip(monkeypatch, built)
    pending = ITERATED[shape]().iterator()
    flip(monkeypatch, read)
    monkeypatch.setattr(RebacQuerySet, "_scope_query", counting)
    assert pks(pending) == READS[read]
    assert pks(ITERATED[shape]()) == READS[read]
    if shape == "its own scope":
        # Built once more at the first rows, then once for the fresh read.
        assert len(built_scopes) == 2


# Every thread's label is gated by the admin role's ``see``, which its member holds through a
# message the role owns (a link): the member reads every label under the published policy, so
# may filter on it, and none under the narrowed one, which refuses the filter.
GATED_LABEL = """
definition auth/user {}
definition scope/role {
    relation member: auth/user
    relation owner: scope/message
    permission see = member + owner->read
}
definition scope/thread {
    relation admin: scope/role // rebac:const=admin
    relation reader: auth/user
    permission read = reader
    permission read__label = admin->see
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission read = direct + thread->read
}
"""


def refused(read: Callable[[], Any]) -> Any:
    try:
        return read()
    except PermissionDenied:
        return "refused"


@pytest.mark.parametrize("change", [True, False], ids=["changed", "unchanged"])
@pytest.mark.parametrize(("built", "read"), DIRECTIONS, ids=DIRECTION_IDS)
def test_an_iterator_rebuilt_at_its_first_rows_checks_its_fields_again(
    settings, monkeypatch, change, built, read
):
    """The reviewer's case: a filter on a field the policy in force hides. ``iterator()``
    checks the fields its statement filters on at the call; where it builds its scope again
    at its first rows it checks them again, under the policy then, and only then. Called
    under the published policy and read under the narrowed one, it is refused, as a fresh
    read is; called under the narrowed one it is refused at the call, as a fresh read is
    there; without the filter it is read in either direction."""
    settings.REBAC_FIELD_READ_MODE = "redact"
    local = install_schema(GATED_LABEL)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=n, label=f"t{n}") for n in (1, 2)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=2, thread_id=2)])
    local.write_relationships(
        [
            grant("scope/thread", 1, "reader"),
            grant("scope/thread", 2, "reader"),
            grant("scope/role", "admin", "owner", SubjectRef.of("scope/message", "2")),
        ]
    )
    later = read if change else built
    guards: list[Any] = []
    real = RebacQuerySet._guard_projected_field_reads

    def counting(self: Any, *args: Any, **kwargs: Any) -> Any:
        guards.append(self)
        return real(self, *args, **kwargs)

    def filtered() -> Any:
        return ScopeThread.objects.with_actor(MEMBER).filter(label="t1")

    flip(monkeypatch, built)
    pending = refused(lambda: filtered().iterator())
    fresh_at_call = refused(lambda: pks(filtered()))
    flip(monkeypatch, later)
    got = pending if pending == "refused" else refused(lambda: pks(pending))
    fresh_at_rows = refused(lambda: pks(filtered()))
    assert got == ([1] if built and later else "refused")
    assert got == (fresh_at_call if fresh_at_call == "refused" else fresh_at_rows)
    flip(monkeypatch, built)
    monkeypatch.setattr(RebacQuerySet, "_guard_projected_field_reads", counting)
    unfiltered = ScopeThread.objects.with_actor(MEMBER).iterator()
    assert len(guards) == 1
    flip(monkeypatch, later)
    assert pks(unfiltered) == [1, 2]
    # Checked again where the scope was built again, and only there.
    assert len(guards) == (2 if change else 1)


# The policy installed in process in place of THREADS: a thread is read by its reader alone.
REPLACED = THREADS.replace("permission read = reader + pinned->read", "permission read = reader")


@pytest.mark.parametrize("how", ["install_schema", "set_schema"])
@pytest.mark.parametrize("value", [True, False], ids=["published", "narrowed"])
def test_a_queryset_kept_across_an_in_process_schema_reads_the_new_one(
    settings, monkeypatch, how, value
):
    """An in-process schema is fenced by the revision of the backend it was installed on, not
    by which backend is active: a queryset kept across ``install_schema()``, which makes a new
    backend active, read the replaced policy's scope, and one kept across ``set_schema()`` on
    the active backend read no row. The decision carries the active in-process revision, so
    either builds its scope again, as it is and embedded. (An eager ``scoped()`` queryset
    keeps the statement it was built with: ``tests/test_structural_scope_decision.py``.)"""
    install_threads(settings, "denormalized")
    flip(monkeypatch, value)
    rows = ScopeThread.objects.with_actor(MEMBER)
    embedded = ScopeThread._base_manager.filter(pk__in=ScopeThread.objects.with_actor(MEMBER))
    assert (rows.count(), pks(embedded)) == (len(READS[value]), READS[value])
    if how == "install_schema":
        install_schema(REPLACED)
    else:
        backend().set_schema(parse_zed(REPLACED))
    assert pks(ScopeThread.objects.with_actor(MEMBER)) == [2]
    assert (rows.count(), pks(rows.iterator()), pks(embedded.all())) == (1, [2], [2])


# ---------- Writes that select their rows by a scope ----------


def _bulk_update(rows: Any) -> int:
    threads = list(ScopeThread._base_manager.order_by("pk"))
    for thread in threads:
        thread.label = "written"
    return int(rows.bulk_update(threads, ["label"]))


WRITES: dict[str, Callable[[Any], int]] = {
    "update": lambda rows: int(rows.update(label="written")),
    "delete": lambda rows: int(rows.delete()[1].get("testapp.ScopeThread", 0)),
    "bulk_update": _bulk_update,
}
SELECTIONS: dict[str, Callable[[], Any]] = {
    # The write's own scope, on a queryset evaluated under the other value first.
    "its scope": lambda: ScopeThread.objects.with_actor(MEMBER),
    # A condition that embeds a queryset built under the other value.
    "an embedded queryset": lambda: ScopeThread.objects.sudo(
        reason="test.policy-decision-write"
    ).filter(pk__in=ScopeThread.objects.with_actor(MEMBER)),
}


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("selection", list(SELECTIONS))
@pytest.mark.parametrize("write", list(WRITES))
@pytest.mark.parametrize(("built", "read"), DIRECTIONS, ids=DIRECTION_IDS)
def test_a_write_selects_its_rows_under_the_policy_in_force(
    settings, monkeypatch, storage, selection, write, built, read
):
    install_threads(settings, storage)
    flip(monkeypatch, built)
    with actor_context(MEMBER):
        rows = SELECTIONS[selection]()
        assert pks(rows.all()) == READS[built]
        assert rows.count() == len(READS[built])
    flip(monkeypatch, read)
    with actor_context(MEMBER):
        assert WRITES[write](rows) == len(READS[read])
    labels = dict(ScopeThread._base_manager.values_list("pk", "label"))
    if write == "delete":
        assert sorted(labels) == [n for n in (1, 2, 3) if n not in READS[read]]
    else:
        written = {n: "written" for n in READS[read]}
        assert labels == {**{1: "first", 2: "second", 3: "third"}, **written}


# ---------- What keeps the policy it was built under ----------


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize(("built", "read"), DIRECTIONS, ids=DIRECTION_IDS)
def test_a_scope_built_eagerly_or_outside_a_queryset_keeps_its_policy(
    settings, monkeypatch, storage, built, read
):
    """An eager ``scoped()`` queryset and its clones, read or embedded, a ``Q`` from
    ``queryset_filter`` and an expression from ``LocalBackend.permission_expression`` keep the
    policy they were built under; ``rebac.permission_expression()`` binds it when compiled. A
    statement that embeds the eager queryset compiles it as it is: nothing is resolved again."""
    install_threads(settings, storage)
    local = backend()
    flip(monkeypatch, built)
    eager = ScopeThread.objects.with_actor(MEMBER).scoped()
    embedded = ScopeThread._base_manager.filter(pk__in=eager.values("pk"))
    condition = local.queryset_filter(
        model=ScopeThread, subject=MEMBER, action="read", using="default"
    )
    expression = local.permission_expression(
        model=ScopeThread, subject=MEMBER, action="read", using="default", identity=F("pk")
    )
    lazy = permission_expression(ScopeThread, "read", actor=MEMBER)
    flip(monkeypatch, read)

    def admitted(annotated: Any) -> list[int]:
        return sorted(pk for pk, allowed in annotated.values_list("pk", "allowed") if allowed)

    assert eager.count() == len(READS[built])
    assert pks(eager.filter(pk__gt=0)) == READS[built]
    assert pks(ScopeThread._base_manager.filter(pk__in=eager.values("pk"))) == READS[built]
    assert pks(ScopeThread._base_manager.filter(condition)) == READS[built]
    assert admitted(ScopeThread._base_manager.annotate(allowed=expression)) == READS[built]
    assert admitted(ScopeThread._base_manager.annotate(allowed=lazy)) == READS[read]
    assert pks(eager) == READS[built]
    resolved: list[Any] = []
    real = rebac_managers._RebacQuery.resolve_expression

    def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        resolved.append(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(rebac_managers._RebacQuery, "resolve_expression", counted)
    assert pks(embedded) == READS[built]
    assert resolved == []


# ---------- The cost of deciding again ----------


@pytest.mark.parametrize("value", [True, False], ids=["published", "narrowed"])
def test_the_same_decision_under_either_policy_builds_no_scope_again(settings, monkeypatch, value):
    """With the setting unchanged a decision costs one comparison, no statement and no
    build; a change of it builds the scope once more, and so does changing it back."""
    install_threads(settings, "denormalized")
    built: list[Any] = []
    real = RebacQuerySet._scope_query

    def counting(self: Any, query: Any, actor: Any, **kwargs: Any) -> Any:
        if "root" not in kwargs:
            built.append(actor)
        return real(self, query, actor, **kwargs)

    monkeypatch.setattr(RebacQuerySet, "_scope_query", counting)
    flip(monkeypatch, value)
    rows = ScopeThread.objects.with_actor(MEMBER)
    assert rows.count() == len(READS[value])
    with CaptureQueriesContext(connection) as captured:
        assert rows.count() == len(READS[value])
        assert rows.exists()
        assert pks(rows.iterator()) == READS[value]
    assert len(built) == 1
    assert len(captured.captured_queries) == 3
    flip(monkeypatch, not value)
    assert rows.count() == len(READS[not value])
    assert rows.count() == len(READS[not value])
    assert len(built) == 2
    flip(monkeypatch, value)
    assert rows.count() == len(READS[value])
    assert len(built) == 3


def test_a_bypass_decides_no_policy(settings):
    """A bypass reads every row whatever the setting, and its decision names no policy, so a
    setting that is not a bool, which every decision under an actor refuses, does not stop
    it; an actor's read raises."""
    install_threads(settings, "denormalized")
    rows = ScopeThread.objects.sudo(reason="test.policy-decision-bypass")
    assert rows.count() == 3
    assert rows.query._rebac_scope_for.policy is None
    settings.REBAC_INHERIT_THROUGH_LINKS = "False"
    assert rows.count() == 3
    with sudo(reason="test.policy-decision-block"):
        assert ScopeMessage.objects.count() == 3
    with pytest.raises(ImproperlyConfigured), actor_context(MEMBER):
        ScopeThread.objects.count()
