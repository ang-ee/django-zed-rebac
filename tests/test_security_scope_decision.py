"""A queryset's scope is decided where it is evaluated, at every evaluation.

``_apply_scope_in_place`` returned early once a scope was recorded as applied, so an
evaluation that leaves no result cache (``count()``, ``exists()``, ``aggregate()``,
``iterator()`` and their async forms), and a list after one of them, read with the scope
of the first evaluation, whatever the context it ran in now: a count inside a ``sudo()``
block left the same queryset unscoped for every later read outside it, a module-level
queryset one request had counted read that request's rows for the next one. And
``iterator()`` decided whether to scope from the result cache: a related manager Django's
prefetch filled holds rows and a query nothing scoped, which ``iterator()`` ran as it
stood. A structural delivery, decided inside a running prefetch, was kept on the queryset
beyond it. Each statement now carries the scope decided for its evaluation, by the same
function (``effective_actor``), with the database it reads and the evaluator scope, read
from the query and never from a cache; a delivery serves the one evaluation that reads its
rows inside the prefetch. Here: every transition the reviewer listed, every path that
reads, each dimension of the decision, the pins that win over ambient state at every
evaluation, the rows a queryset already holds, prefetched related managers, structural
deliveries, and the async forms.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import pickle
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.db import connection, connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Count, Exists, Prefetch, Subquery
from django.db.models.signals import post_init

from rebac import (
    MissingActorError,
    PermissionDenied,
    actor_context,
    evaluator_scope,
    structural,
    sudo,
    system_context,
)
from rebac.managers import RebacQuerySet
from rebac.models import Relationship
from rebac.testing import install_schema
from tests.backend_setup import sqlite_alias
from tests.mechanism_policies import grant
from tests.test_security_failed_scope import (
    LAYOUTS,
    MEMBER,
    OUTSIDER,
    PATHS,
    install,
)
from tests.testapp.models import ScopeMessage, ScopePart, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]

# The reads that leave no result cache: after one of them, any read of the same object
# used to reuse its scope.
UNCACHED = ("count", "exists", "aggregate", "iterator")
# What each path answers when it reads every thread (unscoped), and the member's thread.
EVERY: dict[str, Any] = {
    "list": [1, 2, 3],
    "len": 3,
    "bool": True,
    "count": 3,
    "exists": True,
    "iterator": [1, 2, 3],
    "aggregate": 3,
}
MEMBERS: dict[str, Any] = {
    "list": [2],
    "len": 1,
    "bool": True,
    "count": 1,
    "exists": True,
    "iterator": [2],
    "aggregate": 1,
}


def outsiders(path: str) -> Any:
    return PATHS[path][1]


# ---------- The transitions the review listed ----------


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("second", list(PATHS))
@pytest.mark.parametrize("first", UNCACHED)
@pytest.mark.parametrize("block", ["sudo", "system_context"])
def test_a_read_after_a_bypass_block_is_scoped(settings, storage, block, first, second):
    install(settings, storage=storage)
    rows = ScopeThread.objects.all()
    elevated = sudo if block == "sudo" else system_context
    with elevated(reason="test.decision-block"):
        assert PATHS[first][0](rows) == EVERY[first]
    with actor_context(OUTSIDER):
        assert PATHS[second][0](rows) == outsiders(second)


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("second", list(PATHS))
@pytest.mark.parametrize("first", UNCACHED)
def test_a_shared_queryset_read_by_another_actor_is_scoped_for_it(settings, storage, first, second):
    """A module-level queryset: one request (the member) reads it, the next (the
    outsider) reads the same object."""
    install(settings, storage=storage)
    shared = ScopeThread.objects.all()
    with actor_context(MEMBER):
        assert PATHS[first][0](shared) == MEMBERS[first]
    with actor_context(OUTSIDER):
        assert PATHS[second][0](shared) == outsiders(second)
    if second in UNCACHED:
        # A list, len or bool leaves the outsider's rows held: they are handed out as
        # they are (below). The other reads decide again, back to the member's scope.
        with actor_context(MEMBER):
            assert PATHS[second][0](shared) == MEMBERS[second]


def test_a_total_counted_in_a_sudo_block_leaves_the_rows_listed_after_it_scoped(settings):
    install(settings)
    rows = ScopeThread.objects.all()
    with actor_context(OUTSIDER):
        with sudo(reason="test.decision-total"):
            total = rows.count()
        visible = sorted(row.pk for row in rows)
    assert (total, visible) == (3, [])


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("path", UNCACHED)
def test_enforcement_switched_on_scopes_the_next_read(settings, storage, path):
    install(settings, storage=storage)
    rows = ScopeThread.objects.all()
    settings.REBAC_ENABLED = False
    assert PATHS[path][0](rows) == EVERY[path]
    settings.REBAC_ENABLED = True
    with actor_context(OUTSIDER):
        assert PATHS[path][0](rows) == outsiders(path)


@pytest.mark.parametrize("path", UNCACHED)
def test_enforcement_switched_off_reads_every_row(settings, path):
    install(settings)
    rows = ScopeThread.objects.with_actor(OUTSIDER)
    assert PATHS[path][0](rows) == outsiders(path)
    settings.REBAC_ENABLED = False
    assert PATHS[path][0](rows) == EVERY[path]


# ---------- The pins win over ambient state at every evaluation ----------


@pytest.mark.parametrize("path", list(PATHS))
def test_an_actor_pinned_on_the_queryset_reads_as_itself_inside_a_later_sudo_block(settings, path):
    install(settings)
    rows = ScopeThread.objects.with_actor(MEMBER)
    assert rows.count() == 1
    with sudo(reason="test.decision-pinned"):
        assert PATHS[path][0](rows) == MEMBERS[path]
    with actor_context(OUTSIDER):
        assert PATHS[path][0](rows) == MEMBERS[path]


@pytest.mark.parametrize("path", list(PATHS))
def test_a_queryset_bypass_reads_every_row_under_any_ambient_actor(settings, path):
    install(settings)
    rows = ScopeThread.objects.sudo(reason="test.decision-own-bypass")
    with actor_context(MEMBER):
        assert rows.count() == 3
    with actor_context(OUTSIDER):
        assert PATHS[path][0](rows) == EVERY[path]


# ---------- The other dimensions of the decision ----------


class _Route:
    """Reads of the scope models go to ``alias``: a router whose answer changes."""

    alias = "default"

    def db_for_read(self, model: Any, **hints: Any) -> str | None:
        return _Route.alias if model in (ScopeThread, ScopeMessage) else None


def _behind_the_library(alias: str, type_: str, id_: object, relation: str) -> None:
    """A relationship row written on ``alias`` as another process writes it."""
    target = connections[alias]
    table = target.ops.quote_name(Relationship._meta.db_table)
    with target.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table} (resource_type, resource_id, relation, subject_type, subject_id, "
            "optional_subject_relation, caveat_name, caveat_context, caveat_key, written_at_xid) "
            "VALUES (%s, %s, %s, 'auth/user', %s, '', '', NULL, '', 0)",
            [type_, str(id_), relation, MEMBER.subject_id],
        )


PROFILED = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission read = thread->read + direct
}
"""


def test_a_queryset_routed_to_another_database_is_scoped_for_it(
    settings, django_db_blocker, tmp_path
):
    """Inside an evaluator scope the member's grant profile, read on the database the
    scope is built for, leaves out the arms of relations it holds no row of there. The
    other database holds the member's direct grant on message 3: built for the first
    database, the scope read it without that arm."""
    from rebac.testing import install_schema

    alias = "decision_other"
    target = sqlite_alias(alias, tmp_path / "other.sqlite3")
    connections[alias] = target
    try:
        with django_db_blocker.unblock():
            call_command("migrate", database=alias, verbosity=0)
            install_schema(PROFILED)
            for database in ("default", alias):
                ScopeThread._base_manager.using(database).bulk_create(
                    [ScopeThread(id=n) for n in (1, 2, 3)]
                )
                ScopeMessage._base_manager.using(database).bulk_create(
                    [ScopeMessage(id=n, thread_id=n) for n in (1, 2, 3)]
                )
            _behind_the_library(alias, "scope/message", 3, "direct")
            settings.DATABASE_ROUTERS = [_Route()]
            rows = ScopeMessage.objects.with_actor(MEMBER)
            with evaluator_scope():
                _Route.alias = "default"
                assert rows.count() == 0
                _Route.alias = alias
                assert rows.count() == 1
                assert sorted(row.pk for row in rows) == [3]
    finally:
        _Route.alias = "default"
        target.close()
        del connections[alias]


WITNESSED = """
definition auth/user {}
definition scope/role {
    relation direct: auth/user
    permission member = direct
}
definition scope/thread {
    relation admin: scope/role // rebac:const=admin
    relation reader: auth/user
    permission read = admin->member + reader
}
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("later", ["another scope", "no scope"])
def test_a_queryset_evaluated_in_another_evaluator_scope_is_scoped_there(settings, later):
    """Inside an evaluator scope, building a scope decides whether the actor holds the
    permission's constant arms, and a scope built on a ``False`` leaves them out. That is
    the scope's plan choice: a read in a later scope, or outside one, does not inherit
    it. The member, in another role (so the grant profile keeps the arm and the witness
    decides it), is made an administrator behind the library after the first scope."""
    from rebac.testing import install_schema

    install_schema(WITNESSED)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=n) for n in (1, 2, 3)])
    _behind_the_library("default", "scope/role", "staff", "direct")
    rows = ScopeThread.objects.with_actor(MEMBER)
    with evaluator_scope():
        assert rows.count() == 0
    _behind_the_library("default", "scope/role", "admin", "direct")
    if later == "another scope":
        with evaluator_scope():
            assert rows.count() == 3
    else:
        assert rows.count() == 3


@pytest.mark.parametrize("storage", LAYOUTS)
def test_the_elevation_is_compared_where_the_actor_is_the_same(settings, storage):
    """An aggregate scope without an actor reads nothing, a bypass reads every row: both
    decide no actor, and only the elevation tells them apart."""
    install(settings, storage=storage)
    settings.REBAC_ENABLED = False
    rows = ScopeThread.objects.all().scoped_for_aggregate()
    assert rows.count() == 3
    settings.REBAC_ENABLED = True
    assert rows.count() == 0
    with sudo(reason="test.decision-aggregate"):
        assert rows.count() == 3
    assert rows.aggregate(n=Count("pk"))["n"] == 0


@pytest.mark.parametrize("storage", LAYOUTS)
def test_a_combination_decided_again_keeps_no_part_of_the_earlier_scope(settings, storage):
    install(settings, storage=storage)
    combined = ScopeThread.objects.values_list("pk", flat=True).union(
        ScopeMessage.objects.values_list("pk", flat=True), all=True
    )
    with actor_context(MEMBER):
        assert combined.count() == 2
    with sudo(reason="test.decision-combined"):
        assert combined.count() == 6
    with actor_context(OUTSIDER):
        assert combined.count() == 0
        assert list(combined.all()) == []


# ---------- A structural delivery serves the one evaluation it was decided for ----------


def _delivering_a_revoked_parent() -> Any:
    """A running prefetch that admitted thread 1, which the member does not hold (read a
    moment ago, say, and revoked since): its message 1 is delivered with no statement."""
    [thread] = ScopeThread._base_manager.filter(pk=1)
    return structural.delivering(MEMBER, ScopeThread, "read", [thread])


def _children() -> Any:
    return ScopeMessage.objects.with_actor(MEMBER).filter(thread_id__in=[1])


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("path", UNCACHED)
def test_only_the_evaluation_that_reads_its_rows_now_is_delivered(settings, storage, path):
    """A count, an existence test, an aggregate and ``iterator()`` read their statement
    inside the running prefetch too."""
    install(settings, storage=storage)
    rows = _children()
    with _delivering_a_revoked_parent() as delivery:
        assert PATHS[path][0](rows) == outsiders(path)
        assert sorted(row.pk for row in rows.all()) == [1]
        assert delivery.delivered == 1


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("path", UNCACHED)
def test_a_delivered_evaluation_leaves_no_decision_behind(settings, storage, path):
    """The delivered rows are held, and handed out as held rows are. Whatever reads the
    database next decides again: inside the prefetch and after it."""
    install(settings, storage=storage)
    rows = _children()
    with _delivering_a_revoked_parent() as delivery:
        assert sorted(row.pk for row in rows) == [1]
        assert delivery.delivered == 1
        if path == "iterator":
            assert [row.pk for row in rows.iterator()] == []
    held = {"count": 1, "exists": True}
    assert PATHS[path][0](rows) == held.get(path, outsiders(path))
    assert sorted(row.pk for row in rows.all()) == []
    everything = ScopeThread.objects.sudo(reason="test.delivered-embedded")
    assert not any(everything.annotate(found=Exists(rows)).values_list("found", flat=True))


@pytest.mark.parametrize("storage", LAYOUTS)
def test_a_scope_made_eager_inside_a_prefetch_is_not_delivered(settings, storage):
    install(settings, storage=storage)
    with _delivering_a_revoked_parent():
        eager = _children().scoped()
    assert list(eager) == []
    everything = ScopeThread.objects.sudo(reason="test.delivered-eager")
    assert not any(everything.annotate(found=Exists(eager)).values_list("found", flat=True))


@pytest.mark.parametrize("storage", LAYOUTS)
def test_a_delivered_evaluation_that_raised_leaves_no_decision(settings, storage):
    """The related guard refuses thread 1 after the messages were delivered."""
    install(settings, storage=storage)
    rows = _children().rebac_select_related("thread")
    with _delivering_a_revoked_parent():
        with pytest.raises(PermissionDenied):
            list(rows)
    assert rows._result_cache is None
    assert rows.count() == 0
    assert list(rows) == []


# ---------- A delivery serves its own thread and task, while its prefetch runs ----------


@contextlib.contextmanager
def _copying_a_context_inside_the_prefetch() -> Iterator[list[contextvars.Context]]:
    """The first message read inside a running prefetch copies the context, as a signal
    receiver handing work to a pool, or starting a task, would."""
    copied: list[contextvars.Context] = []

    def receiver(sender: Any, instance: Any, **kwargs: Any) -> None:
        if not copied:
            copied.append(contextvars.copy_context())

    post_init.connect(receiver, sender=ScopeMessage)
    try:
        yield copied
    finally:
        post_init.disconnect(receiver, sender=ScopeMessage)


@pytest.mark.parametrize("storage", LAYOUTS)
def test_a_context_copied_inside_a_prefetch_delivers_nothing_after_it(settings, storage):
    """The member then loses thread 2. Before, the copied context still held the record
    of the admitted thread: its messages, and their parts, were read with no statement."""
    local = install(settings, storage=storage)
    settings.REBAC_STRUCTURAL_PREFETCH = True
    with _copying_a_context_inside_the_prefetch() as copied:
        threads = ScopeThread.objects.with_actor(MEMBER).prefetch_related(
            Prefetch("scopemessage_set", queryset=ScopeMessage.objects.with_actor(MEMBER))
        )
        assert {t.pk: [m.pk for m in t.scopemessage_set.all()] for t in threads} == {2: [2]}
    local.delete_relationship(grant("scope/thread", 2, "reader"))
    [context] = copied
    messages = context.run(
        lambda: [m.pk for m in ScopeMessage.objects.with_actor(MEMBER).filter(thread_id__in=[2])]
    )
    parts = context.run(
        lambda: [p.pk for p in ScopePart.objects.with_actor(MEMBER).filter(message_id__in=[2])]
    )
    assert (messages, parts) == ([], [])


def test_a_delivery_serves_only_the_thread_and_task_that_opened_it(settings):
    install(settings)
    [thread] = ScopeThread._base_manager.filter(pk=1)
    query = ScopeMessage.objects.filter(thread_id__in=[1]).query
    [part_parent] = ScopeMessage._base_manager.filter(pk=1)

    def delivers() -> bool:
        return structural.delivers(query, ScopeMessage, MEMBER, "read", using="default")

    async def in_a_task() -> bool:
        return delivers()

    answers: dict[str, bool] = {}
    with structural.delivering(MEMBER, ScopeThread, "read", [thread]) as delivery:
        context = contextvars.copy_context()
        answers["here"] = delivers()

        def elsewhere() -> None:
            answers["thread"] = context.run(delivers)
            context.run(structural.admit, MEMBER, ScopeMessage, "read", [part_parent])

        worker = threading.Thread(target=elsewhere)
        worker.start()
        worker.join()
        answers["task"] = asyncio.run(in_a_task())
    answers["after"] = context.run(delivers)
    context.run(structural.admit, MEMBER, ScopeMessage, "read", [part_parent])
    assert answers == {"here": True, "thread": False, "task": False, "after": False}
    assert delivery.delivered == 1
    assert (ScopeMessage, "default", "read") not in delivery.rows


def _spying_on_deliveries(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    opened: list[Any] = []
    real = structural.delivering

    @contextlib.contextmanager
    def spying(*args: Any, **kwargs: Any) -> Any:
        with real(*args, **kwargs) as delivery:
            opened.append(delivery)
            yield delivery

    monkeypatch.setattr(structural, "delivering", spying)
    return opened


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("form", ["async for", "sync_to_async", "sync_to_async, any thread"])
def test_an_async_evaluation_still_delivers(settings, monkeypatch, form):
    """Django runs the sync ORM in a worker thread: the whole evaluation, prefetch and
    deliveries included, happens in that one thread."""
    install(settings)
    settings.REBAC_STRUCTURAL_PREFETCH = True
    opened = _spying_on_deliveries(monkeypatch)
    threads = ScopeThread.objects.with_actor(MEMBER).prefetch_related(
        Prefetch("scopemessage_set", queryset=ScopeMessage.objects.with_actor(MEMBER))
    )

    def listed(rows: Any) -> list[Any]:
        try:
            return list(rows)
        finally:
            if form != "sync_to_async":
                # A pool thread of its own: its connection is closed here.
                BaseDatabaseWrapper.close(connections["default"])

    async def evaluate() -> list[Any]:
        if form == "async for":
            return [row async for row in threads]
        return await sync_to_async(listed, thread_sensitive=form == "sync_to_async")(threads)

    found = asyncio.run(evaluate())
    assert {t.pk: [m.pk for m in t.scopemessage_set.all()] for t in found} == {2: [2]}
    assert [delivery.delivered for delivery in opened] == [1]


# ---------- Rows held by a queryset whose own query was never scoped ----------

HELD_SCHEMA = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation reader: auth/user
    permission read = reader
}
"""


def _held_messages(settings: Any, storage: str) -> Any:
    """Thread 1 holds messages 1 and 2; the member reads the thread and message 1."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(HELD_SCHEMA)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=n, thread_id=1) for n in (1, 2)])
    local.write_relationships(
        [grant("scope/thread", 1, "reader"), grant("scope/message", 1, "reader")]
    )
    return local


# Each statement a queryset holding rows can still send, and what the member reads.
HELD_READS: dict[str, tuple[Callable[[Any], Any], Any]] = {
    "iterator": (lambda rows: [row.pk for row in rows.iterator()], [1]),
    "aggregate": (lambda rows: rows.aggregate(n=Count("pk"))["n"], 1),
    "clone": (lambda rows: [row.pk for row in rows.all()], [1]),
    "values": (lambda rows: list(rows.values_list("pk", flat=True)), [1]),
    "embedded": (
        lambda rows: sorted(
            ScopeMessage.objects.sudo(reason="test.held-embedded")
            .filter(pk__in=Subquery(rows.values("pk")))
            .values_list("pk", flat=True)
        ),
        [1],
    ),
    "embedded as it is": (
        lambda rows: sorted(
            pk
            for pk, found in ScopeThread.objects.sudo(reason="test.held-exists")
            .annotate(found=Exists(rows.filter(pk=2)))
            .values_list("pk", "found")
            if found
        ),
        [],
    ),
}


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("read", list(HELD_READS))
@pytest.mark.parametrize("loading", ["rebac_prefetch_related", "prefetch_related"])
def test_a_prefetched_related_manager_scopes_every_statement_it_sends(
    settings, storage, read, loading
):
    """Django's prefetch hands each thread's manager the messages it read for it, scoped
    for the member: message 1. The manager's own query was never scoped; ``iterator()``,
    which Django sends to the database whatever is held, read it as it stood: 1 and 2."""
    _held_messages(settings, storage)
    threads = getattr(ScopeThread.objects.with_actor(MEMBER), loading)("scopemessage_set")
    [thread] = threads
    held = thread.scopemessage_set.all()
    assert held._result_cache is not None
    assert [row.pk for row in held] == [1]
    assert (held.count(), held.exists()) == (1, True)
    function, expected = HELD_READS[read]
    assert function(held) == expected


@pytest.mark.django_db(transaction=True)
def test_a_prefetched_related_manager_scopes_its_async_iterator(settings):
    _held_messages(settings, "denormalized")
    [thread] = ScopeThread.objects.with_actor(MEMBER).rebac_prefetch_related("scopemessage_set")
    held = thread.scopemessage_set.all()

    async def collect() -> list[int]:
        return sorted([row.pk async for row in held.aiterator(chunk_size=10)])

    assert asyncio.run(collect()) == [1]


def test_a_related_manager_prefetched_with_no_actor_decides_where_it_reads(settings):
    """Threads read inside a ``sudo()`` block carry no actor; their prefetched managers hold
    every message, read in the block, and pin no actor. What they send afterwards is
    decided where it is sent, as any related manager of an unpinned row decides: the
    ambient actor, a block, or, in strict mode, no actor and a refusal."""
    _held_messages(settings, "denormalized")
    with sudo(reason="test.held-unpinned"):
        [thread] = ScopeThread.objects.prefetch_related("scopemessage_set")
    held = thread.scopemessage_set.all()
    with actor_context(MEMBER):
        assert [row.pk for row in held.iterator()] == [1]
        # The rows the block read are held, and handed out as held rows are.
        assert sorted(row.pk for row in held) == [1, 2]
    with actor_context(OUTSIDER):
        assert held.aggregate(n=Count("pk"))["n"] == 0
    with sudo(reason="test.held-unpinned-again"):
        assert sorted(row.pk for row in held.iterator()) == [1, 2]
    with pytest.raises(MissingActorError):
        list(held.iterator())


M2M_SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation viewer: auth/user
    permission read = viewer
}
definition blog/post {
    relation viewer: auth/user
    permission read = viewer
}
"""


@pytest.mark.parametrize("side", ["forward", "reverse"])
@pytest.mark.parametrize("read", ["iterator", "aggregate", "clone"])
def test_a_prefetched_many_to_many_manager_scopes_every_statement_it_sends(settings, side, read):
    """A post collected in two folders, a folder holding two posts: the member reads one
    of each pair."""
    from tests.testapp.models import Folder, Post

    local = install_schema(M2M_SCHEMA)
    with sudo(reason="test.fixture"):
        post, other = Post.objects.create(title="shown"), Post.objects.create(title="hidden")
        folder, hidden = Folder.objects.create(name="shown"), Folder.objects.create(name="hidden")
        post.collections.add(folder, hidden)
        other.collections.add(folder)
    local.write_relationships(
        [grant("blog/post", post.pk, "viewer"), grant("blog/folder", folder.pk, "viewer")]
    )
    if side == "forward":
        [loaded] = (
            Post.objects.with_actor(MEMBER).filter(pk=post.pk).rebac_prefetch_related("collections")
        )
        held, readable = loaded.collections.all(), folder.pk
    else:
        [loaded] = Folder.objects.with_actor(MEMBER).rebac_prefetch_related("collected_posts")
        held, readable = loaded.collected_posts.all(), post.pk
    assert held._result_cache is not None and [row.pk for row in held] == [readable]
    function, _expected = HELD_READS[read]
    assert function(held) == ([readable] if read != "aggregate" else 1)


def test_a_queryset_read_in_an_evaluator_scope_pickles_without_it(settings):
    """The decision names the evaluator scope by weak reference; a pickle carries none."""
    install(settings)
    rows = ScopeThread.objects.with_actor(MEMBER)
    combined = rows.values_list("pk", flat=True).union(
        ScopeMessage.objects.with_actor(MEMBER).values_list("pk", flat=True)
    )
    with evaluator_scope():
        assert rows.count() == 1
        assert sorted(combined) == [2]
        restored = pickle.loads(pickle.dumps(rows))
        assert pickle.loads(pickle.dumps(combined)).query.combinator == "union"
    with actor_context(MEMBER):
        assert [row.pk for row in restored] == [2]


def test_the_same_decision_builds_no_scope_again(settings, monkeypatch):
    """The cost of deciding again: one comparison, no statement and no build."""
    install(settings)
    built: list[Any] = []
    real = RebacQuerySet._scope_query

    def counting(self: Any, query: Any, actor: Any, **kwargs: Any) -> Any:
        if "root" not in kwargs:
            built.append(actor)
        return real(self, query, actor, **kwargs)

    monkeypatch.setattr(RebacQuerySet, "_scope_query", counting)
    rows = ScopeThread.objects.with_actor(MEMBER)
    assert rows.count() == 1
    assert len(built) == 1
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        assert rows.count() == 1
        assert rows.exists()
        assert [row.pk for row in rows.iterator()] == [2]
    assert len(built) == 1
    assert len(captured.captured_queries) == 3


# ---------- Rows a queryset holds ----------


def test_rows_a_queryset_holds_are_handed_out_as_django_hands_them_out(settings):
    """A result cache is rows already read, stamped, redacted and guarded by the
    evaluation that read them; handing them out again reads nothing. The reads that go
    to the database whether or not rows are held decide their scope where they run."""
    install(settings)
    rows = ScopeThread.objects.all()
    with sudo(reason="test.decision-held"):
        assert sorted(row.pk for row in rows) == [1, 2, 3]
    with actor_context(OUTSIDER):
        assert sorted(row.pk for row in rows) == [1, 2, 3]
        assert (len(rows), rows.count(), rows.exists()) == (3, 3, True)
        assert [row.pk for row in rows.iterator()] == []
        assert rows.aggregate(n=Count("pk"))["n"] == 0
        # A clone reads again, under the outsider.
        assert list(rows.all()) == []


def test_an_iterator_reads_the_scope_decided_when_it_was_called(settings):
    """The rows are read lazily. A later evaluation of the same queryset under another
    decision replaces its query; the iterator keeps reading the one it decided."""
    install(settings)
    rows = ScopeThread.objects.all()
    with actor_context(OUTSIDER):
        pending = rows.iterator(chunk_size=10)
    with sudo(reason="test.decision-lazy"):
        assert rows.count() == 3
    assert list(pending) == []


# ---------- The async forms ----------


async def _collect(rows: Any) -> list[int]:
    return sorted([row.pk async for row in rows])


async def _acollect(rows: Any) -> list[int]:
    return sorted([row.pk async for row in rows.aiterator(chunk_size=10)])


ASYNC: dict[str, tuple[Callable[[Any], Any], Any, Any]] = {
    "async for": (_collect, [1, 2, 3], []),
    "aiterator": (_acollect, [1, 2, 3], []),
    "acount": (lambda rows: rows.acount(), 3, 0),
    "aexists": (lambda rows: rows.aexists(), True, False),
    "aaggregate": (lambda rows: rows.aaggregate(n=Count("pk")), {"n": 3}, {"n": 0}),
}
ASYNC_UNCACHED = ("aiterator", "acount", "aexists", "aaggregate")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("second", list(ASYNC))
@pytest.mark.parametrize("first", ASYNC_UNCACHED)
def test_the_async_forms_decide_at_each_evaluation(settings, first, second):
    install(settings)
    rows = ScopeThread.objects.all()

    async def scenario() -> tuple[Any, Any]:
        with sudo(reason="test.decision-async"):
            elevated = await ASYNC[first][0](rows)
        with actor_context(OUTSIDER):
            outside = await ASYNC[second][0](rows)
        return elevated, outside

    assert asyncio.run(scenario()) == (ASYNC[first][1], ASYNC[second][2])


@pytest.mark.django_db(transaction=True)
def test_an_aiterator_reads_the_scope_decided_when_it_was_called(settings):
    install(settings)
    rows = ScopeThread.objects.all()

    async def scenario() -> list[int]:
        with actor_context(OUTSIDER):
            pending = rows.aiterator(chunk_size=10)
        with sudo(reason="test.decision-async-lazy"):
            assert await rows.acount() == 3
        return sorted([row.pk async for row in pending])

    assert asyncio.run(scenario()) == []
