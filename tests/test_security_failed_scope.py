"""A queryset whose scope raised keeps none of it: its next evaluation builds it again.

``_apply_scope_in_place`` recorded the scope as applied and then built it. Building it
reads the policy (for a stored policy, its published revision and rows), compiles it,
and inside an evaluator scope reads the actor's grant profile; any of that can raise: the
compiler refuses a policy (``rebac.E016``), a statement fails or is cancelled, the process
is interrupted. The queryset used to keep the record of a scope it did not hold, so the
same object, evaluated again or embedded, ran its own conditions with no permission
condition. The scope is now built on a copy of the query, which replaces the queryset's
only once it is complete, and only then recorded. Here each such failure is followed by a
second evaluation on every path that reuses the object (``list``, ``len``, ``bool``,
``count``, ``exists``, ``iterator``, ``aggregate`` and the async forms), by the object
embedded in another statement or copied, on every shape of set combination at every point
it can fail, under structural prefetch, and while another thread, or the build itself,
reads the same object. A clone, a write, and the failures raised before the scope is
recorded or after it is built were never affected; they are pinned as well.
"""

from __future__ import annotations

import asyncio
import copy
import pickle
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.db import OperationalError, connection, connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Count, Exists, Prefetch, Q, Subquery

from rebac import (
    MissingActorError,
    PermissionDenied,
    SubjectRef,
    actor_context,
    evaluator_scope,
    rowscope,
)
from rebac.errors import SchemaError
from rebac.managers import _nested_queries, _ScopeWhere
from rebac.testing import install_schema
from tests.mechanism_policies import grant
from tests.testapp.models import ScopeMessage, ScopePart, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
MEMBER = SubjectRef.of("auth/user", "member")
# Holds nothing: a scoped read finds no row, an unscoped one every row.
OUTSIDER = SubjectRef.of("auth/user", "outsider")
LAYOUTS = ["denormalized", "registry"]

SCHEMA = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
    permission write = reader
    permission delete = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    permission read = thread->read
    permission delete = thread->delete
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
    permission delete = message->delete
}
"""
# A recursion through an exclusion: the compiler refuses it (rebac.E016) wherever a
# scope is built from this policy, at every evaluation.
REFUSED = SCHEMA.replace(
    "    relation reader: auth/user\n    permission read = reader\n",
    "    relation reader: auth/user\n    relation sibling: scope/thread\n"
    "    permission read = reader - sibling->read\n",
)
# The member may not read a thread's label.
GATED = SCHEMA.replace(
    "    permission write = reader\n",
    "    permission write = reader\n    permission read__label = nil\n",
)
assert SCHEMA != REFUSED and SCHEMA != GATED


def install(settings: Any, schema: str = SCHEMA, storage: str = "denormalized") -> Any:
    """Threads 1-3, message ``n`` in thread ``n`` and part ``n`` of it; the member reads
    thread 2, and through it message 2 and part 2."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(schema)
    ScopeThread._base_manager.bulk_create(
        [ScopeThread(id=n, label=label) for n, label in ((1, "first"), (2, "second"), (3, "third"))]
    )
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=n, thread_id=n) for n in (1, 2, 3)])
    ScopePart._base_manager.bulk_create(
        [ScopePart(id=n, message_id=n, fragment_id=n) for n in (1, 2, 3)]
    )
    local.write_relationships([grant("scope/thread", 2, "reader")])
    return local


def failing_once(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, model: type[Any] | None = None
) -> list[Any]:
    """Building the next scope (of ``model``, when given) raises ``error``, once."""
    real = rowscope.predicate
    raised: list[Any] = []

    def predicate(query: Any, scoped: Any, *args: Any, **kwargs: Any) -> Any:
        if not raised and (model is None or scoped is model):
            raised.append(scoped)
            raise error
        return real(query, scoped, *args, **kwargs)

    monkeypatch.setattr(rowscope, "predicate", predicate)
    return raised


@contextmanager
def profile_statement_fails_once() -> Iterator[list[str]]:
    """Inside an evaluator scope, the statement that reads the actor's grant profile, which
    runs while the scope is built, fails the first time, as a cancelled statement does."""
    failed: list[str] = []

    def wrapper(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        if not failed and '"rebac_profile"' in sql:
            failed.append(sql)
            raise OperationalError("canceling statement due to statement timeout")
        return execute(sql, params, many, context)

    with evaluator_scope(), connection.execute_wrapper(wrapper):
        yield failed


# What each path that reuses the queryset object answers for the outsider: no row.
PATHS: dict[str, tuple[Callable[[Any], Any], Any]] = {
    "list": (lambda rows: sorted(row.pk for row in rows), []),
    "len": (len, 0),
    "bool": (bool, False),
    "count": (lambda rows: rows.count(), 0),
    "exists": (lambda rows: rows.exists(), False),
    "iterator": (lambda rows: sorted(row.pk for row in rows.iterator(chunk_size=10)), []),
    "aggregate": (lambda rows: rows.aggregate(n=Count("pk"))["n"], 0),
}
ERRORS = {
    "SchemaError": SchemaError("planted (rebac.E016)"),
    "DatabaseError": OperationalError("canceling statement due to statement timeout"),
    "ValueError": ValueError("planted"),
    "TimeoutError": TimeoutError("planted"),
    "KeyboardInterrupt": KeyboardInterrupt(),
}


# ---------- The same queryset, evaluated again ----------


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("error", list(ERRORS))
@pytest.mark.parametrize("path", list(PATHS))
def test_a_scope_that_raised_is_built_again_on_every_path(
    settings, monkeypatch, storage, error, path
):
    install(settings, storage=storage)
    read, scoped = PATHS[path]
    failing_once(monkeypatch, ERRORS[error])
    rows = ScopeThread.objects.with_actor(OUTSIDER)
    with pytest.raises(type(ERRORS[error])):
        read(rows)
    # Before, every thread: the query ran with no scope.
    assert read(rows) == scoped


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("path", list(PATHS))
def test_a_policy_the_compiler_refuses_raises_at_every_evaluation(settings, storage, path):
    install(settings, REFUSED, storage=storage)
    read, _scoped = PATHS[path]
    rows = ScopeThread.objects.with_actor(OUTSIDER)
    for _ in range(2):
        with pytest.raises(SchemaError, match=r"rebac\.E016"):
            read(rows)


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("path", list(PATHS))
def test_a_failed_profile_statement_is_read_again(settings, storage, path):
    install(settings, storage=storage)
    # The profile is read where the compiled statement is built. With REBAC_ROW_SCOPE, the
    # statement a queryset runs itself (list, len, bool) is written as conditions on its
    # rows instead, which read no profile: there is no profile statement there to fail.
    settings.REBAC_ROW_SCOPE = False
    read, scoped = PATHS[path]
    with profile_statement_fails_once() as failed:
        rows = ScopeThread.objects.with_actor(OUTSIDER)
        with pytest.raises(OperationalError):
            read(rows)
        assert failed
        assert read(rows) == scoped


@pytest.mark.parametrize("storage", LAYOUTS)
def test_the_scope_built_again_is_the_actors(settings, monkeypatch, storage):
    """Not merely empty: the member reads its thread, message and part."""
    install(settings, storage=storage)
    for model, readable in ((ScopeThread, [2]), (ScopeMessage, [2]), (ScopePart, [2])):
        failing_once(monkeypatch, ValueError("planted"))
        rows = model.objects.with_actor(MEMBER)
        with pytest.raises(ValueError):
            list(rows)
        assert sorted(row.pk for row in rows) == readable, model


@pytest.mark.django_db(transaction=True)
def test_the_async_paths_build_the_scope_again(settings, monkeypatch):
    install(settings)

    async def asynchronous(read: Callable[[Any], Any]) -> Any:
        return await read(ScopeThread.objects.with_actor(OUTSIDER))

    paths: dict[str, Callable[[Any], Any]] = {
        "aiterator": lambda rows: _collect(rows.aiterator(chunk_size=10)),
        "async for": lambda rows: _collect(rows),
        "acount": lambda rows: rows.acount(),
        "aexists": lambda rows: rows.aexists(),
        "aaggregate": lambda rows: rows.aaggregate(n=Count("pk")),
    }
    expected = {"aiterator": [], "async for": [], "acount": 0, "aexists": False}
    for name, read in paths.items():
        rows = ScopeThread.objects.with_actor(OUTSIDER)

        async def twice(read: Callable[[Any], Any] = read, rows: Any = rows) -> Any:
            with pytest.raises(ValueError):
                await read(rows)
            return await read(rows)

        failing_once(monkeypatch, ValueError("planted"))
        found = asyncio.run(twice())
        assert found == expected.get(name, {"n": 0}), name
    assert asyncio.run(asynchronous(lambda rows: rows.acount())) == 0


async def _collect(rows: Any) -> list[int]:
    return sorted([row.pk async for row in rows])


# ---------- The same queryset, embedded or copied ----------

# A statement that reads every message, holding the outsider's threads (none).
EMBEDDED: dict[str, Callable[[Any, Any], list[int]]] = {
    "Subquery": lambda everything, ids: sorted(
        everything.filter(thread__in=Subquery(ids)).values_list("pk", flat=True)
    ),
    "Exists": lambda everything, ids: [
        pk for pk, flag in everything.annotate(found=Exists(ids)).values_list("pk", "found") if flag
    ],
    "lookup on its query": lambda everything, ids: sorted(
        everything.filter(thread__in=ids.query).values_list("pk", flat=True)
    ),
    "lookup on the queryset": lambda everything, ids: sorted(
        everything.filter(thread__in=ids).values_list("pk", flat=True)
    ),
}


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("embed", list(EMBEDDED))
def test_a_queryset_whose_scope_raised_is_scoped_where_it_is_embedded(
    settings, monkeypatch, storage, embed
):
    """``Subquery()`` and ``Exists()`` copy the queryset's query, which carried the record
    of the scope it did not hold; a lookup on ``queryset.query`` embeds the query itself.
    Before, each ran with no scope. A lookup on the queryset clones it and never did."""
    install(settings, storage=storage)
    ids = ScopeThread.objects.with_actor(OUTSIDER).values_list("pk", flat=True)
    failing_once(monkeypatch, ValueError("planted"))
    with pytest.raises(ValueError):
        list(ids)
    monkeypatch.undo()
    everything = ScopeMessage.objects.sudo(reason="test.failed-embedded")
    assert EMBEDDED[embed](everything, ids) == []


@pytest.mark.parametrize("copied", ["deepcopy", "pickle", "query handed over"])
def test_a_queryset_whose_scope_raised_is_scoped_when_copied(settings, monkeypatch, copied):
    install(settings)
    rows = ScopeThread.objects.with_actor(OUTSIDER)
    failing_once(monkeypatch, ValueError("planted"))
    with pytest.raises(ValueError):
        list(rows)
    monkeypatch.undo()
    if copied == "deepcopy":
        other = copy.deepcopy(rows)
    elif copied == "pickle":
        # The actor does not travel through a pickle.
        other = pickle.loads(pickle.dumps(rows)).with_actor(OUTSIDER)
    else:
        other = ScopeThread.objects.with_actor(OUTSIDER)
        other.query = rows.query
    assert sorted(row.pk for row in other) == []
    assert other.count() == 0


# ---------- A query scoped in part ----------


@pytest.mark.parametrize("storage", LAYOUTS)
def test_a_combination_scoped_in_part_is_scoped_again_whole(settings, monkeypatch, storage):
    """The threads' operand is scoped, then the messages' raises. Before, the next
    evaluation ran the threads' operand scoped and the messages' unscoped."""
    install(settings, storage=storage)
    failing_once(monkeypatch, ValueError("planted"), model=ScopeMessage)
    combined = (
        ScopeThread.objects.with_actor(MEMBER)
        .values_list("pk", flat=True)
        .union(ScopeMessage.objects.with_actor(MEMBER).values_list("pk", flat=True))
    )
    with pytest.raises(ValueError):
        list(combined)
    assert sorted(combined) == [2]


def _threads() -> Any:
    return ScopeThread.objects.with_actor(MEMBER)


def _messages() -> Any:
    return ScopeMessage.objects.with_actor(MEMBER)


def _parts() -> Any:
    return ScopePart.objects.with_actor(MEMBER)


# Every shape that scopes more than one query: the operands of set combinations, nested
# ones, and the bounded subqueries Django builds for a sliced operand of ``|``, ``^``.
SHAPES: dict[str, Callable[[], Any]] = {
    "union of three": lambda: (
        _threads()
        .values_list("pk", flat=True)
        .union(_messages().values_list("pk", flat=True), _parts().values_list("pk", flat=True))
    ),
    "union of four, all": lambda: (
        _threads()
        .values_list("pk", flat=True)
        .union(
            _messages().values_list("pk", flat=True),
            _parts().values_list("pk", flat=True),
            _threads().filter(pk__gte=2).values_list("pk", flat=True),
            all=True,
        )
    ),
    "nested union": lambda: (
        _threads()
        .values_list("pk", flat=True)
        .union(
            _messages().values_list("pk", flat=True).union(_parts().values_list("pk", flat=True))
        )
    ),
    "intersection": lambda: (
        _threads()
        .values_list("pk", flat=True)
        .intersection(
            _messages().values_list("pk", flat=True), _parts().values_list("pk", flat=True)
        )
    ),
    "difference": lambda: (
        _threads()
        .values_list("pk", flat=True)
        .difference(
            _messages().filter(pk=3).values_list("pk", flat=True),
            _parts().filter(pk=1).values_list("pk", flat=True),
        )
    ),
    "or, left sliced": lambda: _threads().order_by("pk")[:2] | _threads().filter(pk=3),
    "or, both sliced": lambda: _threads().order_by("pk")[:2] | _threads().order_by("-pk")[:2],
    "or of three, sliced": lambda: (
        (_threads().order_by("pk")[:1] | _threads().order_by("-pk")[:1])
        | _threads().order_by("pk")[1:2]
    ),
    "xor, sliced": lambda: _threads().order_by("pk")[:2] ^ _threads().order_by("-pk")[:2],
    "or, sliced, excluded": lambda: (
        _threads().order_by("pk")[:2] | _threads().filter(pk=3)
    ).exclude(pk=1),
    "or, sliced, negated filter": lambda: (
        _threads().order_by("pk")[:2] | _threads().order_by("-pk")[:1]
    ).filter(~Q(label="x")),
}


def _scope_nodes(query: Any) -> int:
    """The scope conditions a query holds, in its operands and parts too."""

    def where(node: Any) -> int:
        return sum(
            isinstance(child, _ScopeWhere) + where(child) for child in getattr(node, "children", ())
        )

    return (
        where(query.where)
        + sum(
            _scope_nodes(operand)
            for operand in _nested_queries(query.where, boolean_operands_only=True)
        )
        + sum(_scope_nodes(part) for part in query.combined_queries)
    )


def _key(row: Any) -> Any:
    return getattr(row, "pk", row)


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("shape", list(SHAPES))
def test_a_scope_that_raised_anywhere_leaves_no_part_and_is_built_again_whole(
    settings, monkeypatch, storage, shape
):
    """At every point where building the scope of a combination can raise: the failed
    queryset holds no scope condition anywhere, and its next evaluation reads the rows,
    the scope conditions and the SQL of a queryset that never failed."""
    install(settings, storage=storage)
    build = SHAPES[shape]
    fresh = build()
    rows_expected = sorted(map(_key, fresh))
    sql_expected = str(fresh.query)
    nodes_expected = _scope_nodes(fresh.query)
    builds: list[int] = []
    real = rowscope.predicate

    def counting(*args: Any, **kwargs: Any) -> Any:
        builds.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(rowscope, "predicate", counting)
    list(build())
    monkeypatch.undo()
    assert len(builds) >= 2, "a shape that scopes one query only"
    for point in range(1, len(builds) + 1):
        calls: list[int] = []

        def failing(*args: Any, point: int = point, calls: list[int] = calls) -> Any:
            calls.append(1)
            if len(calls) == point:
                raise ValueError("planted")
            return real(*args)

        rows = build()
        monkeypatch.setattr(rowscope, "predicate", failing)
        with pytest.raises(ValueError):
            list(rows)
        monkeypatch.undo()
        assert _scope_nodes(rows.query) == 0, (point, "a part of the scope is left")
        assert sorted(map(_key, rows)) == rows_expected, point
        assert _scope_nodes(rows.query) == nodes_expected, point
        assert str(rows.query) == sql_expected, point


# ---------- Another reader of the same queryset while its scope is built ----------


def _reading_while_built(
    monkeypatch: pytest.MonkeyPatch, read: Callable[[], Any], *, threaded: bool
) -> list[Any]:
    """``read`` runs once, while the first scope built after this call is being built:
    in another thread, with its own connection, or in the build itself."""
    real = rowscope.predicate
    seen: list[Any] = []

    def predicate(*args: Any, **kwargs: Any) -> Any:
        if not seen:
            seen.append(None)
            if threaded:

                def other() -> None:
                    try:
                        seen[0] = read()
                    finally:
                        BaseDatabaseWrapper.close(connections["default"])

                worker = threading.Thread(target=other)
                worker.start()
                worker.join()
            else:
                seen[0] = read()
        return real(*args, **kwargs)

    monkeypatch.setattr(rowscope, "predicate", predicate)
    return seen


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("reader", ["the same actor", "another actor"])
def test_another_thread_reading_the_queryset_while_its_scope_is_built_reads_its_own_scope(
    settings, monkeypatch, reader
):
    """Before, the scope was recorded first: the other thread found it recorded, did not
    wait for it, and read every row. Django does not make a shared queryset thread-safe;
    what a race may cost is a scope built twice, never a statement without one."""
    install(settings)
    rows = ScopeThread.objects.all()
    actor, expected = (OUTSIDER, []) if reader == "the same actor" else (MEMBER, [2])

    def read() -> list[int]:
        with actor_context(actor):
            return sorted(row.pk for row in rows.iterator(chunk_size=10))

    seen = _reading_while_built(monkeypatch, read, threaded=True)
    with actor_context(OUTSIDER):
        assert sorted(row.pk for row in rows) == []
    assert seen == [expected]
    with actor_context(OUTSIDER):
        assert rows.count() == 0


def test_a_build_that_reads_the_same_queryset_reads_it_scoped(settings, monkeypatch):
    """Nothing in the library reads a queryset while building its scope; were anything to,
    it would build its own scope rather than find one recorded and absent."""
    install(settings)
    rows = ScopeThread.objects.with_actor(OUTSIDER)
    seen = _reading_while_built(monkeypatch, rows.count, threaded=False)
    assert sorted(row.pk for row in rows) == []
    assert seen == [0]


@pytest.mark.parametrize("storage", LAYOUTS)
def test_structural_prefetch_admits_only_parents_the_scope_read(settings, monkeypatch, storage):
    """Before, the unscoped messages were admitted as parents, and every part was delivered
    with them, with no permission statement."""
    install(settings, storage=storage)
    settings.REBAC_STRUCTURAL_PREFETCH = True
    failing_once(monkeypatch, ValueError("planted"), model=ScopeMessage)
    messages = ScopeMessage.objects.with_actor(MEMBER).prefetch_related(
        Prefetch("scopepart_set", queryset=ScopePart.objects.with_actor(MEMBER))
    )
    with pytest.raises(ValueError):
        list(messages)
    delivered = {
        message.pk: [part.pk for part in message.scopepart_set.all()] for message in messages
    }
    assert delivered == {2: [2]}


# ---------- Never affected ----------


def test_a_clone_builds_its_own_scope(settings, monkeypatch):
    """A clone made before the failure, or after it, carries no record of it."""
    install(settings)
    rows = ScopeThread.objects.with_actor(MEMBER)
    before = rows.filter(pk__gte=1)
    failing_once(monkeypatch, ValueError("planted"))
    with pytest.raises(ValueError):
        list(rows)
    assert sorted(row.pk for row in before) == [2]
    assert sorted(row.pk for row in rows.all()) == [2]
    assert sorted(rows.values_list("pk", flat=True)) == [2]
    assert sorted(rows.in_bulk()) == [2]
    assert rows.get(pk=2).pk == 2
    with pytest.raises(ScopeThread.DoesNotExist):
        rows.get(pk=3)
    assert rows.order_by("-pk").first().pk == 2
    assert [row.pk for row in rows.order_by("pk")[:5]] == [2]
    # Embedded in a statement that reads every message: the subquery is the member's scope.
    everything = ScopeMessage.objects.sudo(reason="test.embedded-scope")
    assert sorted(everything.filter(thread__in=rows).values_list("pk", flat=True)) == [2]


def test_a_prefetch_queryset_is_scoped_at_each_use(settings, monkeypatch):
    install(settings)
    threads = ScopeThread.objects.with_actor(MEMBER)
    messages = ScopeMessage.objects.with_actor(MEMBER).prefetch_related(
        Prefetch("thread", queryset=threads)
    )
    failing_once(monkeypatch, ValueError("planted"), model=ScopeThread)
    with pytest.raises(ValueError):
        list(messages)
    assert [(message.pk, message.thread.pk) for message in messages] == [(2, 2)]
    assert sorted(row.pk for row in threads) == [2]


@pytest.mark.parametrize("write", ["update", "delete", "bulk_update"])
def test_a_write_selects_its_rows_by_a_scope_of_its_own(settings, monkeypatch, write):
    """After the queryset's read scope raised, and when the write's own scope raised once:
    only the thread in scope is written."""
    install(settings)

    def act(rows: Any) -> Any:
        if write == "update":
            return rows.update(label="written")
        if write == "delete":
            # The thread's message and part go with it.
            return rows.delete()[1]["testapp.ScopeThread"]
        threads = list(ScopeThread._base_manager.order_by("pk"))
        for thread in threads:
            thread.label = "written"
        return rows.bulk_update(threads, ["label"])

    rows = ScopeThread.objects.with_actor(MEMBER)
    failing_once(monkeypatch, ValueError("planted"))
    with pytest.raises(ValueError):
        list(rows)
    failing_once(monkeypatch, ValueError("planted"))
    with pytest.raises(ValueError):
        act(rows)
    assert act(rows) == 1
    remaining = dict(ScopeThread._base_manager.values_list("pk", "label"))
    if write == "delete":
        assert remaining == {1: "first", 3: "third"}
    else:
        assert remaining == {1: "first", 2: "written", 3: "third"}


def test_a_missing_actor_is_refused_before_a_scope_is_recorded(settings):
    install(settings)
    rows = ScopeThread.objects.all()
    with pytest.raises(MissingActorError):
        list(rows)
    with pytest.raises(MissingActorError):
        rows.count()
    with actor_context(MEMBER):
        assert sorted(row.pk for row in rows) == [2]


def test_a_field_guard_refuses_after_the_scope_is_built(settings):
    install(settings, GATED)
    settings.REBAC_FIELD_READ_MODE = "redact"
    rows = ScopeThread.objects.with_actor(MEMBER).filter(label="second")
    for _ in range(2):
        with pytest.raises(PermissionDenied, match="read__label"):
            list(rows)
        with pytest.raises(PermissionDenied, match="read__label"):
            rows.count()
