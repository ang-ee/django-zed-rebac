"""Bounded reads (proposal 0025): a narrow list read decided by its candidate rows.

With ``REBAC_BOUNDED_READ`` on, a list read whose conditions are all on the model's own key or
foreign keys, and select at most 256 rows, reads their keys without the permission, the walk
decides exactly those keys, and the read's own statement runs with the admitted keys in place
of its scope. Here: the same rows, order, slices and downstream work as the compiled statement
over random policies, both storage layouts and both modes; the eligibility rule, under which
every read it refuses sends the compiled statement byte for byte, each refusal asserted by the
reason the library logs; what the key read and the walk select from model tables; what a reader
can still learn from the statements a read sends, pinned by their counts; the statements of one
read under a concurrent commit; the guard and the cost rule against the walk's compiled parts;
the decision rule of 0.38.0; the statement count; and inertness when off.
"""

from __future__ import annotations

import logging
import random
import re
import threading
from collections.abc import Callable
from typing import Any

import pytest
from django.db import connection, connections, transaction
from django.db.models import (
    BooleanField,
    Count,
    Exists,
    F,
    FilteredRelation,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Value,
    Window,
)
from django.db.models.expressions import RawSQL
from django.db.models.functions import Abs, Random, RowNumber
from django.db.models.lookups import Exact
from django.db.models.query import ModelIterable
from django.test import override_settings
from django.test.utils import CaptureQueriesContext, register_lookup

from rebac import (
    MissingActorError,
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    actor_context,
    bounded,
    evaluator_scope,
    structural,
    sudo,
    system_context,
    walk,
)
from rebac.managers import _ScopeWhere
from rebac.snapshot import read_snapshot
from rebac.testing import install_schema
from tests.backend_setup import sqlite_alias
from tests.mechanism_policies import application_shaped, label_threads, seeds
from tests.test_actor_profiles import seeded, subtracting
from tests.test_inherited_witness_reference import build
from tests.testapp.models import ScopeMessage, ScopePart, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]

MEMBER = SubjectRef.of("auth/user", "member")
ADMIN = SubjectRef.of("auth/user", "admin")
OUTSIDER = SubjectRef.of("auth/user", "outsider")
MODES = (True, "identifiers")

# Threads read by keys alone: direct and public grants, a constant role, a filtered constant
# and an exclusion; messages through their thread (a foreign key) or a direct grant.
SCHEMA = """
definition auth/user {}
definition auth/group { relation member: auth/user }
definition scope/role { relation member: auth/user | auth/group#member }
definition scope/thread {
    relation admin: scope/role // rebac:const=admin
    relation reader: auth/user | auth/user:*
    relation blocked: auth/user
    relation hidden: scope/thread // rebac:const={"target_id":"hidden","filters":{"label":"hidden"}}
    permission conceal = authenticated
    permission withheld = hidden->conceal
    permission read = ((reader + admin->member) - blocked) - withheld
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    relation editor: auth/user | auth/group#member
    permission read = thread->read + direct + editor
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
}
"""


def grant(type_: str, id_: object, relation: str, subject: SubjectRef = MEMBER):
    return RelationshipTuple(ObjectRef(type_, str(id_)), relation, subject)


@pytest.fixture
def local():
    """Threads 1-3 (3 labelled hidden); messages 1-4 in thread 1, 5-6 in thread 2, 7 in 3;
    part ``n`` of message ``n``. The member reads thread 1 and message 5 directly."""
    backend = install_schema(SCHEMA)
    ScopeThread._base_manager.bulk_create(
        [ScopeThread(id=1), ScopeThread(id=2), ScopeThread(id=3, label="hidden")]
    )
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=n, thread_id=1 if n <= 4 else 2 if n <= 6 else 3) for n in range(1, 8)]
    )
    ScopePart._base_manager.bulk_create(
        [ScopePart(id=n, message_id=n, fragment_id=n) for n in range(1, 8)]
    )
    backend.write_relationships(
        [
            grant("scope/thread", 1, "reader"),
            grant("scope/thread", 3, "reader"),
            grant("scope/message", 5, "direct"),
        ]
    )
    return backend


def captured(read: Callable[[], Any], using: str = "default") -> tuple[Any, list[tuple[str, Any]]]:
    """What ``read`` returns (or raises), and the statements it sends, with their parameters."""
    with CaptureQueriesContext(connections[using]) as capture:
        result = outcome(read)
    return result, [(query["sql"], query.get("params")) for query in capture.captured_queries]


def outcome(read: Callable[[], Any]) -> Any:
    try:
        return ("returned", shape(read()))
    except Exception as error:
        return ("raised", type(error).__name__, str(error))


def shape(value: Any) -> Any:
    """A read's result as plain data: rows as their key and column values, in order."""
    if hasattr(value, "_meta") and hasattr(value, "pk"):
        return (value.pk, *(getattr(value, f.attname) for f in value._meta.concrete_fields))
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)) or (
        hasattr(value, "__iter__") and not isinstance(value, (str, bytes))
    ):
        return [shape(item) for item in value]
    return value


def probes(statements: list[tuple[str, Any]], table: str) -> list[str]:
    """The candidates' statements: a key read of ``table`` with no ordering, limited to 257."""
    return [
        sql
        for sql, _ in statements
        if re.match(f'SELECT (DISTINCT )?"{table}"."id" AS "pk" FROM "{table}"', sql)
        and sql.endswith("LIMIT 257")
    ]


def compiled(statements: list[tuple[str, Any]], table: str | None = None) -> list[str]:
    """The statements that carry a compiled scope (an id set or a recursion); with ``table``,
    those whose own rows are that table's: a read of it, not of another table."""
    own = re.compile(
        rf'SELECT (?:DISTINCT )?"{table}"\."\w+"(?: AS "\w+")?(?:, "\w+"\."\w+"(?: AS "\w+")?)*'
        rf' FROM "{table}"'
    )
    return [
        sql
        for sql, _ in statements
        if ("rebac_ids" in sql or sql.startswith("WITH")) and (table is None or own.search(sql))
    ]


def both(read: Callable[[], Any], settings: Any, mode: Any = "identifiers"):
    """The read with the setting off and on: both outcomes must agree. (The setting is entered
    and left by ``override_settings``: the ``settings`` fixture keeps one more holder for each
    assignment, and a thousand of them exhaust the recursion limit.)"""
    with override_settings(REBAC_BOUNDED_READ=False):
        expected, today = captured(read)
    with override_settings(REBAC_BOUNDED_READ=mode):
        found, statements = captured(read)
    assert found == expected
    return expected, today, statements


def messages(actor: SubjectRef = MEMBER):
    return ScopeMessage.objects.with_actor(actor)


def threads(actor: SubjectRef = MEMBER):
    return ScopeThread.objects.with_actor(actor)


def parent_check(keys: list[int], actor: SubjectRef = MEMBER) -> str:
    """The statement the reader's own read of those threads sends today: what a bounded read
    sends first to check the parents it names."""
    with override_settings(REBAC_BOUNDED_READ=False):
        _result, statements = captured(
            lambda: list(
                threads(actor).filter(pk__in=sorted(keys)).order_by().values_list("pk", flat=True)
            )
        )
    [(sql, _params)] = statements
    return sql


# ---------- A narrow read, decided by its candidates ----------


def test_a_narrow_read_is_decided_by_its_candidates(local, settings):
    """Messages of thread 1, which the member may read: the reader's own read of thread 1
    first, then the key read, the walk's lookups and the rows. No compiled read of messages."""
    result, today, statements = both(
        lambda: list(messages().filter(thread_id=1).order_by("pk")), settings
    )
    assert result == ("returned", [(n, n, 1) for n in (1, 2, 3, 4)])
    assert compiled(today, "testapp_scopemessage")
    assert not compiled(statements, "testapp_scopemessage")
    assert statements[0][0] == parent_check([1])
    [probe] = probes(statements, "testapp_scopemessage")
    assert probe == statements[1][0]
    # The read's own conditions, no ordering, no slice, the key column alone.
    assert probe == (
        'SELECT "testapp_scopemessage"."id" AS "pk" FROM "testapp_scopemessage" '
        'WHERE "testapp_scopemessage"."thread_id" = %s LIMIT 257'.replace("%s", "1")
        if connection.vendor == "sqlite"
        else probe
    )
    final = statements[-1][0]
    assert '"testapp_scopemessage"."id" IN (' in final and "ORDER BY" in final


def test_the_key_read_is_the_tables_keys_under_the_reads_conditions(local, settings):
    """No join, ordering, slice, projection or distinct of the read reaches the key read: it
    reads the model's own table under the read's conditions, which are on keys alone."""
    expected = (
        'SELECT "testapp_scopemessage"."id" AS "pk" FROM "testapp_scopemessage" '
        'WHERE "testapp_scopemessage"."thread_id" IN ({}) LIMIT 257'
    )
    for read in (
        lambda: list(messages().filter(thread_id__in=[1]).order_by("thread__label", "-pk")[1:3]),
        lambda: list(
            messages().filter(thread_id__in=[1]).order_by("pk").values_list("pk", "thread__label")
        ),
        lambda: list(
            messages().filter(thread_id__in=[1]).annotate(label=F("thread__label")).order_by("pk")
        ),
        lambda: list(messages().filter(thread_id__in=[1]).select_related("thread").order_by("pk")),
        lambda: list(messages().filter(thread_id__in=[1]).order_by("pk").distinct()),
    ):
        _result, _today, statements = both(read, settings)
        [probe] = probes(statements, "testapp_scopemessage")
        assert probe == statements[1][0]
        if connection.vendor == "sqlite":
            assert probe == expected.format("1")
        else:
            assert probe == expected.format("%s") or probe == expected.format("1"), probe


def test_the_rows_are_read_in_the_reads_order_with_its_slice_taken_after_the_decision(settings):
    """Thread 1 is readable; of its messages 1-12 the member may read the odd ones by a direct
    grant and not the even ones (``LEAKS``): slices before and after the first row, indexing
    and descending orders read today's rows in today's order."""
    local = install_schema(LEAKS)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=n, thread_id=1) for n in range(1, 13)])
    local.write_relationships(
        [grant("scope/thread", 1, "reader")]
        + [grant("scope/message", n, "direct") for n in range(1, 13, 2)]
    )

    def in_thread():
        return messages().filter(thread_id=1)

    for read in (
        lambda: list(in_thread().order_by("-pk")[:2]),
        lambda: list(in_thread().order_by("-pk")[1:3]),
        lambda: list(in_thread().order_by("pk")[3:]),
        lambda: in_thread().order_by("-pk")[0],
        lambda: in_thread().order_by("-thread_id", "pk")[:3],
    ):
        result, _today, statements = both(read, settings)
        assert result[0] == "returned"
        assert probes(statements, "testapp_scopemessage")
        assert not compiled(statements, "testapp_scopemessage")
    assert both(lambda: list(in_thread().order_by("-pk")[:2]), settings)[0] == (
        "returned",
        [(11, 11, 1), (9, 9, 1)],
    )


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda: messages().filter(thread_id=1).order_by("-pk")[:1].get(), id="get"),
        pytest.param(lambda: messages().filter(thread_id=1).first(), id="first"),
        pytest.param(lambda: messages().filter(thread_id=1).last(), id="last"),
        pytest.param(lambda: messages().filter(thread_id=1).earliest("pk"), id="earliest"),
        pytest.param(lambda: messages().filter(thread_id=1).order_by("pk")[2], id="index"),
        pytest.param(lambda: len(messages().filter(thread_id=1).order_by("pk")), id="len"),
        pytest.param(lambda: bool(messages().filter(thread_id=1).order_by("pk")), id="bool"),
        pytest.param(
            lambda: list(
                messages().filter(thread_id=1).order_by("pk").values_list("pk", flat=True)
            ),
            id="values_list",
        ),
        pytest.param(
            lambda: list(messages().filter(thread_id=1).order_by("-pk").values("pk", "thread_id")),
            id="values",
        ),
        pytest.param(
            lambda: list(
                messages()
                .filter(thread_id=1)
                .order_by("pk")
                .values_list("pk", "thread__label", named=True)
            ),
            id="named-values",
        ),
        pytest.param(
            lambda: [
                (m.pk, m.thread.pk)
                for m in messages().filter(thread_id=1).select_related("thread").order_by("pk")
            ],
            id="select_related",
        ),
        pytest.param(
            lambda: messages().filter(thread_id=1).order_by("pk").distinct()[:2], id="distinct"
        ),
        pytest.param(
            lambda: list(
                messages()
                .filter(thread_id=1)
                .annotate(double=F("pk") * 2)
                .order_by("-double", "pk")
            ),
            id="annotation, ordered by it",
        ),
        pytest.param(
            lambda: list(messages().filter(thread=ScopeThread(pk=1)).order_by("pk")),
            id="a foreign key by its object",
        ),
        pytest.param(
            lambda: list(messages().filter(thread__pk=1).order_by("pk")),
            id="a foreign key by its target's key",
        ),
        pytest.param(
            lambda: [m.pk for m in threads().get(pk=1).scopemessage_set.order_by("pk")],
            id="related manager",
        ),
    ],
)
def test_each_entry_point_reads_what_the_compiled_statement_reads(local, settings, read):
    result, _today, statements = both(read, settings)
    # Each is decided by its candidates: no compiled read of messages is sent.
    assert probes(statements, "testapp_scopemessage"), result
    assert not compiled(statements, "testapp_scopemessage"), result


def test_a_forward_prefetch_of_referred_rows_is_decided_by_its_candidates(local, settings):
    """Django reads a forward foreign key's prefetch as ``pk IN ((1,),)`` (``TupleIn``): the
    related rows' own keys, which the rule reads as ``pk IN (1)``. The reader holds key 1:
    the messages this evaluation read refer to it."""
    result, _today, statements = both(
        lambda: [
            (m.pk, m.thread.pk, m.thread.label)
            for m in messages().filter(thread_id=1).order_by("pk").rebac_prefetch_related("thread")
        ],
        settings,
    )
    assert result == ("returned", [[n, 1, "thread"] for n in (1, 2, 3, 4)])
    assert probes(statements, "testapp_scopemessage") and probes(statements, "testapp_scopethread")
    assert not compiled(statements, "testapp_scopemessage")
    # The one compiled read of threads is the check of thread 1, the messages' parent.
    assert compiled(statements, "testapp_scopethread") == [parent_check([1])]


def test_a_guarded_join_is_guarded_as_ever(local, settings):
    """``rebac_select_related()`` refuses a joined thread the member may not read: part 5's
    message is readable through its direct grant, its thread is not."""
    result, _today, statements = both(
        lambda: list(
            ScopePart.objects.with_actor(MEMBER)
            .filter(message_id=5)
            .rebac_select_related("message__thread")
            .order_by("pk")
        ),
        settings,
    )
    assert result[:2] == ("raised", "PermissionDenied")
    assert probes(statements, "testapp_scopepart")


def test_prefetched_children_are_decided_by_their_candidates_too(local, settings):
    """A prefetch whose queryset ends ties by the key: its children are decided as any read,
    their parents proven by the evaluation that read them, with no statement of its own. The
    threads themselves, read by no condition, keep their statement."""

    def read():
        threads = (
            ScopeThread.objects.with_actor(MEMBER)
            .order_by("pk")
            .rebac_prefetch_related(
                Prefetch("scopemessage_set", queryset=messages().order_by("-pk")),
                Prefetch(
                    "scopemessage_set__scopepart_set",
                    queryset=ScopePart.objects.with_actor(MEMBER).order_by("pk"),
                ),
            )
        )
        return {
            thread.pk: {
                message.pk: [part.pk for part in message.scopepart_set.all()]
                for message in thread.scopemessage_set.all()
            }
            for thread in threads
        }

    result, today, statements = both(read, settings)
    assert result == ("returned", {1: {4: [4], 3: [3], 2: [2], 1: [1]}})
    assert probes(statements, "testapp_scopemessage") and probes(statements, "testapp_scopepart")
    assert not compiled(statements, "testapp_scopemessage")
    assert not compiled(statements, "testapp_scopepart")
    # The threads' own statement, and then no check of a parent: the prefetch admitted them.
    assert statements[0] == today[0]
    assert len(compiled(statements)) == 1


def test_unordered_children_keep_their_statement(local, settings):
    """Django orders no prefetch: children that tie come in the order the statement gives them,
    which the read by key does not reproduce, so several children keep their statement."""

    def read():
        threads = (
            ScopeThread.objects.with_actor(MEMBER)
            .order_by("pk")
            .rebac_prefetch_related("scopemessage_set")
        )
        return {thread.pk: [m.pk for m in thread.scopemessage_set.all()] for thread in threads}

    result, _today, statements = both(read, settings)
    assert result[0] == "returned"
    assert not probes(statements, "testapp_scopemessage")
    assert compiled(statements, "testapp_scopemessage")


def test_structural_delivery_comes_first(local, settings, caplog):
    """A child query the running prefetch delivers carries no scope to replace. (Its children
    are ordered by the key, so that nothing else refuses it.)"""
    settings.REBAC_STRUCTURAL_PREFETCH = True

    def read():
        threads = (
            ScopeThread.objects.with_actor(MEMBER)
            .filter(pk__in=[1, 2])
            .order_by("pk")
            .rebac_prefetch_related(
                Prefetch("scopemessage_set", queryset=messages().order_by("pk"))
            )
        )
        return {thread.pk: [m.pk for m in thread.scopemessage_set.all()] for thread in threads}

    with caplog.at_level(logging.DEBUG, logger="rebac.bounded"):
        result, _today, statements = both(read, settings)
    assert result == ("returned", {1: [1, 2, 3, 4]})
    assert not probes(statements, "testapp_scopemessage")
    assert any(
        "keeps its statement: it carries no compiled scope" in message for message in logged(caplog)
    ), logged(caplog)


# ---------- The bound ----------


def analyzed() -> None:
    """After a bulk load inside the test's transaction: on PostgreSQL the planner has no
    statistics for it, and nests loops over the whole load (a statement of minutes, holding the
    test's locks after the test times out). As ``test_read_contract`` does."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE")


def fill(thread: int, count: int, start: int = 100) -> None:
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=start + n, thread_id=thread) for n in range(count)]
    )
    analyzed()


@pytest.mark.parametrize(("count", "served"), [(256, True), (257, False)])
def test_a_read_past_the_bound_sends_the_compiled_statement_unchanged(
    local, settings, count, served
):
    # Thread 1 holds 4 messages; these make ``count``.
    fill(1, count - 4)
    result, today, statements = both(
        lambda: [m.pk for m in messages().filter(thread_id=1).order_by("-pk")[:3]], settings
    )
    assert result == ("returned", [100 + count - 5, 100 + count - 6, 100 + count - 7])
    [probe] = probes(statements, "testapp_scopemessage")
    if served:
        assert not compiled(statements, "testapp_scopemessage")
    else:
        # The check of thread 1, the key read, then the compiled read, byte for byte: never a
        # second batch.
        assert [sql for sql, _ in statements] == [parent_check([1]), probe, *(s for s, _ in today)]


def test_the_statement_count_does_not_depend_on_the_candidates(local, settings):
    """The check of the parent, one key read, the walk's lookups, the rows: as many statements
    for 1 candidate as for 202. (Threads 1 and 4, both readable, are the parents.)"""
    ScopeThread._base_manager.bulk_create([ScopeThread(id=4)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=50, thread_id=4)])
    local.write_relationships([grant("scope/thread", 4, "reader")])
    fill(1, 198)
    settings.REBAC_BOUNDED_READ = "identifiers"
    few = captured(lambda: list(messages().filter(thread_id=4).order_by("pk")))[1]
    many = captured(lambda: list(messages().filter(thread_id=1).order_by("pk")))[1]
    assert len(probes(few, "testapp_scopemessage")) == len(probes(many, "testapp_scopemessage"))
    assert len(few) == len(many)
    # The thread; the key read; the candidate rows; their threads; reader, the admin role,
    # blocked; the hidden constant; the rows.
    assert len(many) == 9, "\n".join(sql for sql, _ in many)


def test_a_read_that_selects_nothing_sends_the_check_and_the_key_read_alone(local, settings):
    """Thread 4 is readable and holds no message."""
    ScopeThread._base_manager.bulk_create([ScopeThread(id=4)])
    local.write_relationships([grant("scope/thread", 4, "reader")])
    result, _today, statements = both(
        lambda: list(messages().filter(thread_id=4).order_by("pk")), settings
    )
    assert result == ("returned", [])
    assert [sql for sql, _ in statements] == [
        parent_check([4]),
        *probes(statements, "testapp_scopemessage"),
    ]
    assert len(statements) == 2


# ---------- What a read must be: each refusal sends the compiled statement ----------


def refused(read: Callable[[], Any], settings: Any, mode: Any = "identifiers") -> Any:
    """The read sends the same statements, byte for byte, with the setting on as off (a bypass
    audit row's timestamp aside)."""
    result, today, statements = both(read, settings, mode)
    assert _untimed(statements) == _untimed(today)
    assert not [sql for sql, _ in statements if sql.endswith("LIMIT 257")]
    return result


def _untimed(statements: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    stamp = re.compile(r"'\d{4}-\d{2}-\d{2}[ T][\d:.+]+'")
    savepoint = re.compile(r'"s\d+_x\d+"')
    # PostgreSQL's server-side cursors are named by a counter.
    cursor = re.compile(r'"_django_curs_\d+_\w+?_\d+"')
    return [
        (
            cursor.sub('"<cursor>"', savepoint.sub('"<savepoint>"', stamp.sub("'<time>'", sql))),
            params,
        )
        for sql, params in statements
    ]


def _transformed() -> Any:
    with register_lookup(IntegerField, Abs):
        return list(messages().filter(pk__abs=2).order_by("pk"))


def _labelled() -> Any:
    """A manager whose own queryset filters on a content column, as a default filter does."""

    class Labelled(type(ScopeThread.objects)):  # type: ignore[misc]
        def get_queryset(self) -> Any:
            return super().get_queryset().filter(label="thread")

    manager = Labelled()
    manager.model = ScopeThread
    return list(manager.with_actor(MEMBER).filter(pk__in=[1, 2]).order_by("pk"))


def _locked() -> Any:
    with transaction.atomic():
        return [m.pk for m in messages().select_for_update().filter(thread_id=1).order_by("pk")]


class _OwnRows(ModelIterable):  # type: ignore[type-arg]
    """A third-party row class, here Django's own under another name."""


def _own_rows() -> Any:
    queryset = messages().filter(thread_id=1).order_by("pk")
    queryset._iterable_class = _OwnRows
    return [m.pk for m in queryset]


REFUSED: dict[str, Callable[[], Any]] = {
    "count": lambda: messages().filter(thread_id=1).count(),
    "exists": lambda: messages().filter(thread_id=1).exists(),
    "aggregate": lambda: messages().filter(thread_id=1).aggregate(n=Count("pk")),
    "annotated aggregate": lambda: list(
        messages().annotate(n=Count("scopepart")).order_by("pk").values_list("pk", "n")
    ),
    "grouped values": lambda: list(
        messages().values("thread_id").annotate(n=Count("pk")).order_by("thread_id")
    ),
    "window": lambda: list(
        messages()
        .filter(thread_id=1)
        .annotate(n=Window(RowNumber(), order_by=F("pk").asc()))
        .order_by("pk")
    ),
    "union": lambda: list(messages().filter(pk=1).union(messages().filter(pk=5))),
    "intersection": lambda: list(messages().filter(pk__lt=5).intersection(messages())),
    "or of sliced operands": lambda: list(
        messages().order_by("pk")[:1] | messages().order_by("pk")[:2]
    ),
    "embedded queryset": lambda: list(
        messages().filter(thread__in=ScopeThread.objects.with_actor(MEMBER)).order_by("pk")
    ),
    "embedded query": lambda: list(
        messages()
        .filter(thread__in=ScopeThread.objects.with_actor(MEMBER).values("pk").query)
        .order_by("pk")
    ),
    "exists condition": lambda: list(
        messages()
        .filter(Exists(ScopePart.objects.with_actor(MEMBER).filter(message=OuterRef("pk"))))
        .order_by("pk")
    ),
    "subquery annotation": lambda: list(
        messages()
        .annotate(
            first=Subquery(
                ScopePart.objects.with_actor(MEMBER).filter(message=OuterRef("pk")).values("pk")[:1]
            )
        )
        .order_by("pk")
    ),
    "a join to many rows": lambda: list(
        messages().filter(scopepart__fragment_id__gt=0).order_by("pk")
    ),
    "excluded across a to-many relation": lambda: list(
        messages().exclude(scopepart__fragment_id=1).order_by("pk")
    ),
    "filtered relation": lambda: list(
        messages()
        .annotate(early=FilteredRelation("scopepart", condition=Q(scopepart__fragment_id=1)))
        .filter(early__isnull=False)
        .order_by("pk")
    ),
    "extra select": lambda: list(messages().extra(select={"two": "2"}).order_by("pk")),
    "raw condition": lambda: list(
        messages().filter(RawSQL("1 = %s", (1,), output_field=BooleanField())).order_by("pk")
    ),
    "random order": lambda: len(list(messages().filter(thread_id=1).order_by("?"))),
    "random condition": lambda: len(
        list(messages().annotate(draw=Random()).filter(draw__lt=2).order_by("pk"))
    ),
    "select_for_update": _locked,
    "distinct on fields": lambda: list(
        messages().filter(thread_id__in=[1, 2]).order_by("thread_id", "pk").distinct("thread_id")
    ),
    "own iterable class": _own_rows,
    "a subject set as the actor": lambda: [
        m.pk
        for m in messages(SubjectRef.of("auth/group", "g", "member"))
        .filter(thread_id=1)
        .order_by("pk")
    ],
    # The conditions a read may have: on its own key or a foreign key of its own table, by
    # exact, in or isnull, against literal values, joined by AND. Every other condition is
    # refused; each read below is otherwise eligible.
    "a content column": lambda: list(threads().filter(label="thread").order_by("pk")),
    "a pattern on a content column": lambda: list(
        threads().filter(label__startswith="hid").order_by("pk")
    ),
    "a range on the key": lambda: list(messages().filter(pk__gt=2).order_by("pk")),
    "between two keys": lambda: list(messages().filter(pk__range=(1, 3)).order_by("pk")),
    "keys joined by OR": lambda: list(messages().filter(Q(pk=1) | Q(thread_id=2)).order_by("pk")),
    "querysets joined by OR": lambda: list(
        (messages().filter(pk=1) | messages().filter(pk=5)).order_by("pk")
    ),
    "exclude()": lambda: list(messages().exclude(pk=1).order_by("pk")),
    "a negated Q": lambda: list(messages().filter(~Q(thread_id=2)).order_by("pk")),
    "a transform": _transformed,
    "an expression as the condition's left side": lambda: list(
        messages().filter(Exact(Abs("pk"), 2)).order_by("pk")
    ),
    "an annotation in a condition": lambda: list(
        messages().annotate(double=F("pk") * 2).filter(double=4).order_by("pk")
    ),
    "F() on the right": lambda: list(messages().filter(pk=F("thread_id")).order_by("pk")),
    "Value() on the right": lambda: list(messages().filter(pk=Value(2)).order_by("pk")),
    "a condition through a join": lambda: list(
        messages().filter(thread__label="thread").order_by("pk")
    ),
    "a boolean expression": lambda: list(
        messages().filter(Value(True, output_field=BooleanField())).order_by("pk")
    ),
    "a manager's own filter": _labelled,
    "iterator": lambda: [m.pk for m in messages().filter(thread_id=1).iterator(chunk_size=10)],
    "unordered rows": lambda: sorted(m.pk for m in messages().filter(thread_id=1)),
    "in_bulk of several": lambda: messages().in_bulk([1, 5, 6]),
    "ordering with ties": lambda: [m.pk for m in messages().order_by("thread_id")],
    "none()": lambda: list(messages().none()),
    # Every key the conditions name must be one the reader holds.
    "a client's key": lambda: list(threads().filter(pk=2).order_by("pk")),
    "get() by a client's key": lambda: threads().get(pk=1),
    "in_bulk()": lambda: threads().order_by("pk").in_bulk([1, 2]),
    "a client's key beside a parent": lambda: list(
        messages().filter(thread_id=1, pk__in=[1, 2]).order_by("pk")
    ),
    "threads by a client's key": lambda: list(threads().filter(pk__in=[1, 2]).order_by("pk")),
    "no condition": lambda: list(threads().order_by("pk")),
    "isnull": lambda: list(threads().filter(pk__isnull=False).order_by("pk")),
    "a foreign key by isnull": lambda: list(messages().filter(thread__isnull=False).order_by("pk")),
}

# The reads refused for their conditions, which the narrowed rule added.
CONDITIONS = [
    "a content column",
    "a pattern on a content column",
    "a range on the key",
    "between two keys",
    "keys joined by OR",
    "querysets joined by OR",
    "exclude()",
    "a negated Q",
    "a transform",
    "an expression as the condition's left side",
    "an annotation in a condition",
    "F() on the right",
    "Value() on the right",
    "a condition through a join",
    "a boolean expression",
    "a manager's own filter",
]
NEITHER_KEY = "a condition on a column that is neither the primary key nor a foreign key"
UNHELD = "a key that no row this evaluation read refers to"
EXPRESSION = "a condition on an expression, a transform or an annotation"

# Why each read above keeps its statement, as ``rebac.bounded`` logs it; ``None``: a path the
# bounded read does not serve at all, which never asks.
REFUSED_BECAUSE: dict[str, str | None] = {
    "count": None,
    "exists": None,
    "aggregate": None,
    "annotated aggregate": "an aggregate or a grouping",
    "grouped values": "an aggregate or a grouping",
    "window": "a window",
    "union": "a set combination",
    "intersection": "a set combination",
    "or of sliced operands": "a query embedded in it",
    "embedded queryset": "a query embedded in it",
    "embedded query": "a query embedded in it",
    "exists condition": "a query embedded in it",
    "subquery annotation": "a query embedded in it",
    "a join to many rows": "a join that can repeat a row",
    "excluded across a to-many relation": "a query embedded in it",
    "filtered relation": "a FilteredRelation",
    "extra select": "extra()",
    "raw condition": "hand-written SQL",
    "random order": "a random ordering",
    "random condition": "a random value",
    "select_for_update": "select_for_update()",
    "distinct on fields": "distinct(*fields)",
    "own iterable class": "its rows are built by an iterable class of its own",
    "a subject set as the actor": "an actor the walk does not read",
    "a content column": NEITHER_KEY,
    "a pattern on a content column": "a condition by the lookup 'startswith'",
    "a range on the key": "a condition by the lookup 'gt'",
    "between two keys": "a condition by the lookup 'range'",
    "keys joined by OR": "conditions joined by OR or XOR",
    "querysets joined by OR": "conditions joined by OR or XOR",
    "exclude()": "a negated condition",
    "a negated Q": "a negated condition",
    "a transform": EXPRESSION,
    "an expression as the condition's left side": EXPRESSION,
    "an annotation in a condition": EXPRESSION,
    "F() on the right": "a condition compared with an expression",
    "Value() on the right": "a condition compared with an expression",
    "a condition through a join": "a condition reached through a join",
    "a boolean expression": EXPRESSION,
    "a manager's own filter": NEITHER_KEY,
    "iterator": None,
    "unordered rows": "its ordering leaves ties",
    "in_bulk of several": "its ordering leaves ties",
    "ordering with ties": "its ordering leaves ties",
    "none()": "it selects nothing",
    "a client's key": UNHELD,
    "get() by a client's key": UNHELD,
    "in_bulk()": UNHELD,
    "a client's key beside a parent": UNHELD,
    "threads by a client's key": UNHELD,
    "no condition": "it names no key the reader holds: it has no condition",
    "isnull": "it names no key the reader holds: isnull",
    "a foreign key by isnull": "it names no key the reader holds: isnull",
}

# Keys mode refuses a message's read before it reads what the conditions name: the message's
# read follows its thread.
REFUSED_IN_KEYS_MODE: dict[str, str] = {
    name: "the walk would ask the compiled statement: scope/message#thread"
    for name in ("a client's key beside a parent", "a foreign key by isnull")
}


def logged(caplog: Any) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "rebac.bounded"]


def refused_because(
    read: Callable[[], Any], settings: Any, caplog: Any, because: str, mode: Any = "identifiers"
) -> Any:
    """``refused``, and every read it served logged ``because`` as the reason it kept its
    statement."""
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="rebac.bounded"):
        result = refused(read, settings, mode)
    messages = logged(caplog)
    assert messages and all(f"keeps its statement: {because}" in m for m in messages), messages
    return result


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", list(REFUSED))
def test_a_read_the_rule_refuses_sends_the_compiled_statement(local, settings, caplog, name, mode):
    because = REFUSED_BECAUSE[name]
    if mode is True:
        because = REFUSED_IN_KEYS_MODE.get(name, because)
    if because is None:
        with caplog.at_level(logging.DEBUG, logger="rebac.bounded"):
            refused(REFUSED[name], settings, mode)
        assert logged(caplog) == []
    else:
        refused_because(REFUSED[name], settings, caplog, because, mode)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "parents", [[2], [3], [99], [1, 2]], ids=["with children", "without", "absent", "one of two"]
)
def test_a_read_naming_a_parent_the_reader_may_not_read_keeps_its_statement(
    leaky, settings, caplog, mode, parents
):
    """The member may not read thread 2 (three messages) or thread 3 (none); thread 99 does not
    exist. The check is one statement, the reader's own read of the threads named, byte for
    byte; then today's statement. No key is read."""
    with caplog.at_level(logging.DEBUG, logger="rebac.bounded"):
        _result, today, statements = both(
            lambda: list(messages().filter(thread_id__in=parents).order_by("pk")), settings, mode
        )
    assert [sql for sql, _ in statements] == [parent_check(parents), *(sql for sql, _ in today)]
    assert logged(caplog) == [
        f"A read of {ScopeMessage} keeps its statement: "
        "it names a parent the reader may not read (testapp.ScopeThread)."
    ]


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda: list(messages().sudo(reason="test").order_by("pk")), id="sudo"),
        pytest.param(
            lambda: list(messages().system_context(reason="test").order_by("pk")),
            id="system_context",
        ),
    ],
)
def test_a_bypass_sends_its_own_statement(local, settings, caplog, read):
    refused_because(read, settings, caplog, "a bypass reads every row")


def test_a_bypass_block_sends_its_own_statement(local, settings, caplog):
    def read():
        with sudo(reason="test"):
            return [m.pk for m in ScopeMessage.objects.filter(thread_id=1).order_by("pk")]

    refused_because(read, settings, caplog, "a bypass reads every row")

    def system():
        with system_context(reason="test"):
            return [m.pk for m in ScopeMessage.objects.filter(thread_id=1).order_by("pk")]

    refused_because(system, settings, caplog, "a bypass reads every row")


def test_nothing_enforced_sends_the_plain_statement(local, settings):
    settings.REBAC_ENABLED = False
    assert refused(lambda: [m.pk for m in messages().order_by("pk")], settings) == (
        "returned",
        [1, 2, 3, 4, 5, 6, 7],
    )


def test_strict_mode_with_no_actor_raises_before_any_statement(local, settings):
    settings.REBAC_BOUNDED_READ = "identifiers"
    result, statements = captured(lambda: list(ScopeMessage.objects.order_by("pk")))
    assert result[:2] == ("raised", "MissingActorError") and statements == []
    with pytest.raises(MissingActorError):
        list(ScopeMessage.objects.filter(pk=1))


def test_no_actor_outside_strict_mode_reads_every_row_as_before(local, settings):
    settings.REBAC_STRICT_MODE = False
    refused(lambda: [m.pk for m in ScopeMessage.objects.order_by("pk")], settings)


def test_the_ambient_actor_and_a_pinned_actor_are_decided_alike(local, settings):
    def ambient():
        with actor_context(MEMBER):
            return [m.pk for m in ScopeMessage.objects.filter(thread_id=1).order_by("pk")]

    result, _today, statements = both(ambient, settings)
    assert result == ("returned", [1, 2, 3, 4]) and probes(statements, "testapp_scopemessage")
    pinned = both(lambda: [m.pk for m in messages().filter(thread_id=1).order_by("pk")], settings)
    assert pinned[0] == result


@pytest.mark.parametrize("kind", ["extend", "loosen", "disable", "tighten", "recaveat"])
def test_a_stored_override_keeps_the_compiled_statement(settings, kind):
    """A policy stored as rows, read by the walk alone until an override of any kind is stored."""
    from django.contrib.contenttypes.models import ContentType

    from rebac import LocalBackend
    from rebac.models import (
        SchemaCaveat,
        SchemaDefinition,
        SchemaOverride,
        SchemaPermission,
        SchemaRelation,
    )

    SchemaDefinition.objects.create(resource_type="auth/user")
    thread = SchemaDefinition.objects.create(resource_type="scope/thread")
    message = SchemaDefinition.objects.create(resource_type="scope/message")
    caveat = SchemaCaveat.objects.create(
        name="enabled", params=[{"name": "ok", "type": "bool"}], expression="ok"
    )
    SchemaRelation.objects.create(
        definition=thread, name="reader", allowed_subjects=[{"type": "auth/user"}]
    )
    SchemaRelation.objects.create(
        definition=thread, name="extra", allowed_subjects=[{"type": "auth/user"}]
    )
    read = SchemaPermission.objects.create(definition=thread, name="read", expression="reader")
    SchemaRelation.objects.create(
        definition=message,
        name="thread",
        allowed_subjects=[{"type": "scope/thread"}],
        backing={"kind": "fk", "path": "thread"},
    )
    SchemaPermission.objects.create(definition=message, name="read", expression="thread->read")
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1), ScopeThread(id=2)])
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=n, thread_id=1 if n < 3 else 2) for n in (1, 2, 3)]
    )
    LocalBackend().write_relationships([grant("scope/thread", 1, "reader")])

    def read_messages():
        return [m.pk for m in messages().filter(thread_id=1).order_by("pk")]

    result, _today, statements = both(read_messages, settings)
    assert result == ("returned", [1, 2]) and probes(statements, "testapp_scopemessage")
    target, target_pk = (
        (SchemaCaveat, caveat.pk) if kind == "recaveat" else (SchemaPermission, read.pk)
    )
    SchemaOverride.objects.create(
        kind=kind,
        target_ct=ContentType.objects.get_for_model(target),
        target_pk=target_pk,
        expression="true" if kind == "recaveat" else "extra",
        reason="test",
    )
    queryset = messages().order_by("pk")
    settings.REBAC_BOUNDED_READ = "identifiers"
    assert bounded.refusal(queryset, queryset.scoped().query) == "the policy carries an override"
    refused(read_messages, settings)


# The message's read with an exclusion whose subtrahend the walk cannot read: a caveated
# relation, a recursion. Below an exclusion nothing is asked of the compiled statement, so the
# walk would hand the whole permission, the read's own question, to it.
CAVEATED = SCHEMA.replace(
    "definition auth/user {}", "caveat enabled(ok bool) { ok }\ndefinition auth/user {}"
).replace(
    "permission read = thread->read + direct + editor",
    "relation muted: auth/user with enabled\n    permission read = (thread->read + direct) - muted",
)
RECURSIVE = SCHEMA.replace(
    "definition auth/group { relation member: auth/user }",
    "definition auth/group { relation member: auth/user | auth/group#member }",
).replace(
    "permission read = thread->read + direct + editor",
    "relation muted: auth/user | auth/group#member\n    permission read = (thread->read + direct) - muted",
)
# An arm of the read's own union the walk cannot read: the question would be compiled.
UNREADABLE = SCHEMA.replace(
    "permission read = thread->read + direct + editor",
    "relation linked: scope/thread | auth/user:*\n    permission read = thread->read + direct + linked->read",
)


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        pytest.param(
            CAVEATED,
            "scope/message#muted is recursive or admits a caveated subject, below an exclusion",
            id="caveat-below-an-exclusion",
        ),
        pytest.param(
            RECURSIVE,
            "auth/group#member is recursive or admits a caveated subject, below an exclusion",
            id="recursion-below-an-exclusion",
        ),
    ],
)
def test_where_the_walk_would_hand_the_question_to_the_compiler_the_read_keeps_its_statement(
    local, settings, schema, reason
):
    """The guard is decided from the policy, per type and action, and kept on the policy."""
    install_schema(schema)
    policy = structural.current_policy()
    assert walk.compiler_part(policy, "scope/message", "read") == reason
    assert (
        walk.compiler_part(policy, "scope/message", "read")
        is policy.walked[("scope/message", "read", False)]
    )
    refused(lambda: [m.pk for m in messages().filter(thread_id=1).order_by("pk")], settings)


def test_an_arm_of_the_question_the_walk_cannot_read_keeps_the_statement(local, settings):
    install_schema(UNREADABLE)
    reason = walk.compiler_part(structural.current_policy(), "scope/message", "read")
    assert reason == (
        "scope/message#linked: a caveated subject, or a wildcard or fixed id followed through"
    )
    refused(lambda: [m.pk for m in messages().filter(thread_id=1).order_by("pk")], settings)


TREE = """
definition auth/user {}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    permission read = parent->read + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation owner: auth/user
    permission read = folder->read + owner
}
"""


@pytest.mark.parametrize("limit", [1, 2, 8])
def test_a_recursion_under_the_depth_limit(settings, limit):
    """Folders read ``parent->read``: their own question is recursive, so a list of folders keeps
    its statement. A post reads ``folder->read``: the walk asks the compiled statement for the
    folders' part alone, entering the recursion there as the post's statement does, with the
    whole limit."""
    from tests.testapp.models import Folder, Post

    settings.REBAC_DEPTH_LIMIT = limit
    local = install_schema(TREE)
    alice = SubjectRef.of("auth/user", "alice")
    with sudo(reason="fixture"):
        root = Folder.objects.create(name="0")
        chain = [root]
        for depth in range(1, 4):
            chain.append(Folder.objects.create(name=str(depth), parent=chain[-1]))
        for folder in chain:
            Post.objects.create(title=folder.name, folder=folder)
    local.write_relationships(
        [RelationshipTuple(ObjectRef("blog/folder", str(root.pk)), "viewer", alice)]
    )
    assert walk.compiler_part(structural.current_policy(), "blog/folder", "read") == (
        "blog/folder#read is recursive or admits a caveated subject"
    )
    refused(lambda: [f.pk for f in Folder.objects.with_actor(alice).order_by("pk")], settings)
    with override_settings(REBAC_BOUNDED_READ=False):
        readable = [f.pk for f in Folder.objects.with_actor(alice).order_by("pk")]
    # The posts of the folders alice may read: those folders are the parents the read names.
    result, _today, statements = both(
        lambda: [
            p.title
            for p in Post.objects.with_actor(alice).filter(folder_id__in=readable).order_by("pk")
        ],
        settings,
    )
    assert result == ("returned", [str(depth) for depth in range(min(limit, 3) + 1)])
    assert probes(statements, "testapp_post")


@pytest.mark.django_db(transaction=True)
def test_an_actor_whose_profile_admits_nothing_keeps_its_statement(settings):
    """Inside an evaluator scope an actor who holds nothing the permission reads is answered by
    the compiled statement, which the profile folds to nothing: no key is read. (Committed
    grants: while a relationship write may still roll back, nothing is folded.)"""
    local = install_schema(
        """
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
    )
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=n, thread_id=1) for n in (1, 2)])
    local.write_relationships([grant("scope/thread", 1, "reader")])
    settings.REBAC_BOUNDED_READ = "identifiers"
    ordered = messages(OUTSIDER).filter(thread_id=1).order_by("pk")
    assert not bounded.refusal(ordered, ordered.scoped().query)
    for actor, rows, probed in ((OUTSIDER, [], False), (MEMBER, [1, 2], True)):
        with evaluator_scope():
            result, statements = captured(
                lambda actor=actor: [
                    m.pk for m in messages(actor).filter(thread_id=1).order_by("pk")
                ]
            )
        assert result == ("returned", rows)
        assert bool(probes(statements, "testapp_scopemessage")) is probed


def test_the_grant_profile_of_an_evaluator_scope_reads_the_same_rows(local, settings):
    for actor in (MEMBER, OUTSIDER, ADMIN):

        def read(actor=actor):
            with evaluator_scope():
                return [m.pk for m in messages(actor).filter(thread_id__in=[1, 2]).order_by("pk")]

        both(read, settings)


# ---------- Keys mode: of a row the actor may not read, nothing but its key ----------


def cached(row: Any, name: str) -> Any:
    """The key of the related row a prefetch delivered to ``row``, ``None`` for none."""
    related = row._state.fields_cache.get(name)
    return None if related is None else related.pk


def test_keys_mode_reads_nothing_of_a_candidate_but_its_key(local, settings, monkeypatch):
    """Threads: direct and public grants, a constant role and a filtered constant, all read
    by key. The walk follows no foreign key of a thread and no subject of a grant on one. The
    messages, read through their thread, keep their statement in keys mode; the thread they
    refer to is read by a forward prefetch of one row and decided by its candidates. (Django
    reads a forward prefetch unordered, so a read of several such rows ties.)"""
    followed: list[tuple[str, set[str]]] = []
    targets = walk._Walk._targets

    def recording(self, resolved, source, ids):
        followed.append((resolved.source_model._meta.label, set(ids)))
        return targets(self, resolved, source, ids)

    monkeypatch.setattr(walk._Walk, "_targets", recording)
    result, _today, statements = both(
        lambda: [
            (m.pk, cached(m, "thread"))
            for m in messages()
            .filter(thread_id__in=[1])
            .order_by("pk")
            .rebac_prefetch_related("thread")
        ],
        settings,
        mode=True,
    )
    assert result == ("returned", [[1, 1], [2, 1], [3, 1], [4, 1]])
    assert probes(statements, "testapp_scopethread")
    assert not compiled(statements, "testapp_scopethread")
    assert followed == []
    for sql, _params in statements[:-1]:
        selected = re.match(r'SELECT (.*?) FROM "testapp_scopethread"', sql)
        if selected is not None:
            # A thread's key, never another of its columns.
            assert selected.group(1) in (
                '"testapp_scopethread"."id" AS "pk"',
                '"testapp_scopethread"."id" AS "id"',
            ), sql


def test_keys_mode_refuses_a_read_through_a_foreign_key(local, settings):
    """A message is read through its thread: the walk would read the thread's key of every
    candidate message, which is a column other than the message's key."""

    def queryset():
        return messages().filter(thread_id=1).order_by("pk")

    settings.REBAC_BOUNDED_READ = True
    assert bounded.refusal(queryset(), queryset().scoped().query) == (
        "the walk would ask the compiled statement: "
        "scope/message#thread: a foreign key of the rows asked about, which is not their key"
    )
    refused(lambda: [m.pk for m in queryset()], settings, mode=True)


def test_identifiers_mode_reads_the_foreign_keys_it_follows(local, settings, monkeypatch):
    """What the wider mode reads of a candidate beside its key: the foreign key it follows."""
    followed: list[set[str]] = []
    targets = walk._Walk._targets

    def recording(self, resolved, source, ids):
        followed.append(set(ids))
        return targets(self, resolved, source, ids)

    monkeypatch.setattr(walk._Walk, "_targets", recording)
    both(lambda: [m.pk for m in messages().filter(thread_id=1).order_by("pk")], settings)
    # The thread's key of every candidate was read, to follow ``thread->read``.
    assert {"1", "2", "3", "4"} in followed


# ---------- What crosses: the columns the key read and the walk select ----------

MODEL_TABLES: dict[str, Any] = {
    model._meta.db_table: model for model in (ScopeThread, ScopeMessage, ScopePart)
}
# A statement that selects plain columns of one table; a compiled part, which selects a key
# and a truth value computed inside the database.
COLUMNS = re.compile(r'SELECT (?:DISTINCT )?((?:"\w+"\."\w+"(?: AS "\w+")?(?:, )?)+) FROM "(\w+)"')
COMPILED_PART = re.compile(r'SELECT "\w+"\."\w+" AS "pk", CASE WHEN ')


def stated(table: str, mode: str) -> set[str]:
    """The columns of a model table a bounded read's key read and walk may select in ``mode``
    (``rebac.bounded.mode()``): the key, and with ``"identifiers"`` the foreign keys."""
    meta = MODEL_TABLES[table]._meta
    columns = {meta.pk.column}
    if mode == "identifiers":
        columns |= {field.column for field in meta.concrete_fields if field.is_relation}
    return columns


def crossing(statements: list[str], mode: str) -> list[str]:
    """The statements among ``statements`` that select from a model table any column ``mode``
    does not let cross, or that read a model table in a shape not recognised here."""
    found = []
    for sql in statements:
        if sql.startswith("WITH") or COMPILED_PART.match(sql):
            continue
        match = COLUMNS.match(sql)
        if match is None:
            if any(f'"{table}"' in sql for table in MODEL_TABLES):
                found.append(sql)
            continue
        if match.group(2) not in MODEL_TABLES:
            continue
        columns = set(re.findall(r'"\w+"\."(\w+)"', match.group(1)))
        if columns - stated(match.group(2), mode):
            found.append(sql)
    return found


class Boundary:
    """Records every statement a bounded read sends before its rows' statement (the key read,
    the walk's lookups and compiled parts) and keeps those that select more than the mode lets
    cross. The rows' statement, and today's statement after a fallback, run after it."""

    def __init__(self, monkeypatch: Any) -> None:
        self.windows = 0
        self.found: list[tuple[str, str]] = []
        decided = bounded._decided

        def recording(queryset: Any, query: Any) -> Any:
            mode = bounded.mode()
            with CaptureQueriesContext(connections[query._rebac_scope_for[2]]) as capture:
                result = decided(queryset, query)
            self.windows += 1
            statements = [item["sql"] for item in capture.captured_queries]
            self.found += [(mode, sql) for sql in crossing(statements, mode)]
            return result

        monkeypatch.setattr(bounded, "_decided", recording)


# A message's foreign key read inside another permission of its type, not in its own union:
# the guard lets ``True`` through, and the walk must refuse the lookup when it would run.
FOREIGN_KEY_BELOW = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission view = thread->read
    permission read = direct + view
}
definition scope/part { permission read = nil }
"""

# A grant to a subject set read inside another permission of the thread's type.
SUBJECT_SET_BELOW = """
definition auth/user {}
definition auth/group { relation member: auth/user }
definition scope/thread {
    relation reader: auth/user
    relation grp: auth/group#member
    permission shared = grp
    permission read = reader + shared
}
definition scope/message {
    relation direct: auth/user
    permission read = direct
}
definition scope/part { permission read = nil }
"""


def test_keys_mode_selects_no_foreign_key_of_a_candidate(settings, caplog, monkeypatch):
    local = install_schema(FOREIGN_KEY_BELOW)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1), ScopeThread(id=2)])
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=n, thread_id=1 if n < 3 else 2) for n in (1, 2, 3, 4)]
    )
    local.write_relationships(
        [grant("scope/thread", 1, "reader"), grant("scope/message", 3, "direct")]
    )
    policy = structural.current_policy()
    assert walk.compiler_part(policy, "scope/message", "read", keys_only=True) is None
    boundary = Boundary(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="rebac.bounded"):
        result, today, statements = both(
            lambda: [m.pk for m in messages().filter(thread_id=1).order_by("pk")],
            settings,
            mode=True,
        )
    assert result == ("returned", [1, 2])
    assert boundary.windows == 1 and boundary.found == []
    # The walk ends at ``view``: it would read the candidates' thread, or compile a permission
    # of their type that reaches what the question reaches. Today's statement follows.
    assert logged(caplog) == [
        f"A read of {ScopeMessage} keeps its statement: "
        "the walk would ask the compiled statement for these rows."
    ]
    assert _untimed(statements[-len(today) :]) == _untimed(today)


def test_keys_mode_reads_no_subject_of_a_grant_on_a_candidate(settings, caplog, monkeypatch):
    """Threads 2 and 3 are granted to groups by ``grp``; only group g holds the member. Keys mode
    asks the compiled statement for ``grp`` on the candidates, which reads those grants inside
    the database, and never selects the subject of a grant on a candidate."""
    local = install_schema(SUBJECT_SET_BELOW)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=n) for n in (1, 2, 3)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=n, thread_id=n) for n in (1, 2, 3)])
    local.write_relationships(
        [
            *(grant("scope/message", n, "direct") for n in (1, 2, 3)),
            grant("scope/thread", 1, "reader"),
            grant("scope/thread", 2, "grp", SubjectRef.of("auth/group", "g", "member")),
            grant("scope/thread", 3, "grp", SubjectRef.of("auth/group", "h", "member")),
            grant("auth/group", "g", "member"),
        ]
    )
    assert (
        walk.compiler_part(structural.current_policy(), "scope/thread", "read", keys_only=True)
        is None
    )
    lookups: list[int] = []
    stored = walk._Walk._through_stored

    def recording(self, definition, relation, target, ids, path, negated):
        shapes = self.stored_shapes(relation, target)
        on_candidates = definition.resource_type == "scope/thread" and bool(
            set(ids) & {"1", "2", "3"}
        )
        with CaptureQueriesContext(connection) as capture:
            found = stored(self, definition, relation, target, ids, path, negated)
        if shapes is not None and shapes[1] and on_candidates:
            lookups.append(len(capture.captured_queries))
        return found

    monkeypatch.setattr(walk._Walk, "_through_stored", recording)
    # The message of each thread, and by a forward prefetch the thread it refers to, one row
    # decided by its candidates.
    for thread, expected in ((1, [[1, 1]]), (2, [[2, 2]]), (3, [[3, None]])):
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="rebac.bounded"):
            result, _today, _statements = both(
                lambda thread=thread: [
                    (m.pk, cached(m, "thread"))
                    for m in messages()
                    .filter(thread_id=thread)
                    .order_by("pk")
                    .rebac_prefetch_related("thread")
                ],
                settings,
                mode=True,
            )
        assert result == ("returned", expected)
        assert f"A read of {ScopeThread} is decided by its candidates." in logged(caplog)
    # Asked, refused before any statement.
    assert lookups and set(lookups) == {0}, lookups


# ---------- The cost rule: a permission as wide as the question ends the walk ----------

# ``other`` holds an arm the walk cannot read (a wildcard followed through): it would be
# compiled for the candidates ``direct`` leaves, and it reaches all but one of the relations
# the question reaches.
AS_WIDE = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation editor: auth/user
    permission view = editor
    permission read = reader + editor
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation linked: scope/thread | auth/user:*
    relation direct: auth/user
    permission other = linked->read + thread->view
    permission read = direct + other
}
definition scope/part { permission read = nil }
"""


@pytest.mark.parametrize("mode", MODES)
def test_the_walk_ends_where_it_would_compile_a_permission_as_wide_as_the_question(
    settings, caplog, mode
):
    """The guard lets the question through: ``other`` is not the question. Thread 1 is readable
    (``reader``) but grants no ``view``. The walk reads ``direct``, which leaves messages 1, 2
    and 4, and ``thread->view``, which grants none, and would then compile ``other`` for them, a
    permission of the question's type that reaches half of what the question reaches or more:
    it ends, and the read sends today's statements after the key read and the lookups so far."""
    local = install_schema(AS_WIDE)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=n, thread_id=1) for n in (1, 2, 3, 4)])
    local.write_relationships(
        [grant("scope/thread", 1, "reader"), grant("scope/message", 3, "direct")]
    )
    policy = structural.current_policy()
    assert walk.compiler_part(policy, "scope/message", "read", keys_only=mode is True) is None
    assert 2 * walk._reach(policy, ("scope/message", "other")) >= walk._reach(
        policy, ("scope/message", "read")
    )
    with caplog.at_level(logging.DEBUG, logger="rebac.bounded"):
        result, today, statements = both(
            lambda: [m.pk for m in messages().filter(thread_id=1).order_by("pk")],
            settings,
            mode,
        )
    assert result == ("returned", [3])
    assert logged(caplog) == [
        f"A read of {ScopeMessage} keeps its statement: "
        "the walk would ask the compiled statement for these rows."
    ]
    # The check of thread 1, the key read, the walk's lookups so far, then today's statement
    # byte for byte.
    assert _untimed(statements[-len(today) :]) == _untimed(today)
    assert statements[0][0] == parent_check([1])
    assert probes(statements[1:2], "testapp_scopemessage")
    assert (len(today), len(statements)) == WALKED_THEN_KEPT[mode], [s for s, _ in statements]


# Today's statements, and the statements of the read above: the check of thread 1, the key
# read and the walk's lookups before it ends (keys mode reads nothing of a candidate's thread),
# then today's.
WALKED_THEN_KEPT: dict[Any, tuple[int, int]] = {True: (1, 5), "identifiers": (1, 7)}


# ---------- What a reader can still learn: pinned by the statements a read sends ----------

# Threads and messages both decided by the walk in either mode. A message is read by a direct
# grant or a group's ``editor`` grant, less ``muted``.
LEAKS = """
definition auth/user {}
definition auth/group { relation member: auth/user }
definition scope/thread {
    relation reader: auth/user
    permission read = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    relation editor: auth/group#member
    relation muted: auth/user
    permission read = (direct + editor) - muted
}
definition scope/part { permission read = nil }
"""


@pytest.fixture
def leaky():
    """The member may read threads 1 and 4, not 2 or 3. Thread 1 holds message 1, which the
    member may read, and messages 2, 6 and 7, which it may not: 2 holds no grant, 6 is granted
    to the member and muted, 7 is granted to group h, which the member is not in. Thread 2
    holds messages 3-5, threads 3 and 4 none."""
    backend = install_schema(LEAKS)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=n) for n in (1, 2, 3, 4, 5)])
    ScopeMessage._base_manager.bulk_create(
        [
            ScopeMessage(id=n, thread_id=thread)
            for n, thread in ((1, 1), (2, 1), (3, 2), (4, 2), (5, 2), (6, 1), (7, 1))
        ]
    )
    backend.write_relationships(
        [
            grant("scope/thread", 1, "reader"),
            grant("scope/thread", 4, "reader"),
            grant("scope/message", 1, "direct"),
            grant("scope/message", 6, "direct"),
            grant("scope/message", 6, "muted"),
            grant("scope/message", 7, "editor", SubjectRef.of("auth/group", "h", "member")),
        ]
    )
    return backend


# Reads by keys the member does not hold: a key it names itself, a parent it may not read or
# that does not exist. Each pair of the same kind must send the same statements.
UNHELD_READS: dict[str, Callable[[], Any]] = {
    "a hidden key": lambda: list(messages().filter(pk=3).order_by("pk")),
    "an absent key": lambda: list(messages().filter(pk=99).order_by("pk")),
    "a hidden key and a parent": lambda: list(messages().filter(pk=3, thread_id=2).order_by("pk")),
    "an absent key and a parent": lambda: list(
        messages().filter(pk=99, thread_id=2).order_by("pk")
    ),
    "a parent with three hidden children": lambda: list(
        messages().filter(thread_id=2).order_by("pk")
    ),
    "a parent with no children": lambda: list(messages().filter(thread_id=3).order_by("pk")),
    "an absent parent": lambda: list(messages().filter(thread_id=99).order_by("pk")),
}


@pytest.mark.parametrize("mode", MODES)
def test_a_read_by_keys_the_reader_does_not_hold_says_nothing_of_hidden_rows(leaky, mode):
    """A key the reader names itself keeps today's statement, whether a row has it or not. A
    parent the reader may not read, or that does not exist, costs the check (the reader's own
    read of it) and then today's statement, whether it has children or not. Every read here
    returns nothing; each pair sends the same statements as the other, byte for byte but for
    the key named."""
    sent = {}
    for name, read in UNHELD_READS.items():
        with override_settings(REBAC_BOUNDED_READ=False):
            expected, today = captured(read)
        with override_settings(REBAC_BOUNDED_READ=mode):
            result, statements = captured(read)
        assert result == expected == ("returned", []), name
        sent[name] = [sql for sql, _ in statements]
        if "parent" in name and "key" not in name:
            assert sent[name][1:] == [sql for sql, _ in today], name
        else:
            assert sent[name] == [sql for sql, _ in today], name

    def masked(name: str) -> list[str]:
        return [re.sub(r"\b(3|99)\b", "<key>", sql) for sql in sent[name]]

    assert masked("a hidden key") == masked("an absent key")
    assert masked("a hidden key and a parent") == masked("an absent key and a parent")
    assert {name: len(found) for name, found in sent.items()} == {
        "a hidden key": 1,
        "an absent key": 1,
        "a hidden key and a parent": 1,
        "an absent key and a parent": 1,
        "a parent with three hidden children": 2,
        "a parent with no children": 2,
        "an absent parent": 2,
    }
    assert sent["an absent parent"][0] == parent_check([99])


@pytest.mark.parametrize("mode", MODES)
def test_the_children_of_a_parent_the_reader_may_not_read_send_the_same_statements(leaky, mode):
    """Thread 3, which the member may not read, holds 0, then 3, then 300 messages: the read of
    its children sends the same statements each time, byte for byte."""
    sent = []
    for count in (0, 3, 300):
        ScopeMessage._base_manager.filter(thread_id=3).delete()
        ScopeMessage._base_manager.bulk_create(
            [ScopeMessage(id=1000 + n, thread_id=3) for n in range(count)]
        )
        analyzed()
        with override_settings(REBAC_BOUNDED_READ=mode):
            result, statements = captured(
                lambda: list(messages().filter(thread_id=3).order_by("pk"))
            )
        assert result == ("returned", [])
        sent.append(statements)
    assert sent[0] == sent[1] == sent[2]
    assert sent[0][0][0] == parent_check([3])
    assert len(sent[0]) == 2


# What a reader learns of a parent it may read: whether it has hidden children and how many.
# The statements the read of thread 4's messages sends with 0, 1, 3 and 300 messages the
# member may not read in it: the check; the key read; then the walk's lookups, or today's
# statement past the bound.
HIDDEN_CHILDREN: dict[int, int] = {0: 2, 1: 6, 3: 6, 300: 3}


@pytest.mark.parametrize("mode", MODES)
def test_the_hidden_children_of_a_readable_parent_show_in_the_statements(leaky, mode):
    counts = {}
    for count in HIDDEN_CHILDREN:
        ScopeMessage._base_manager.filter(thread_id=4).delete()
        ScopeMessage._base_manager.bulk_create(
            [ScopeMessage(id=2000 + n, thread_id=4) for n in range(count)]
        )
        analyzed()
        with override_settings(REBAC_BOUNDED_READ=mode):
            result, statements = captured(
                lambda: list(messages().filter(thread_id=4).order_by("pk"))
            )
        assert result == ("returned", [])
        assert statements[0][0] == parent_check([4])
        counts[count] = len(statements)
    assert counts == HIDDEN_CHILDREN, counts


@pytest.mark.parametrize("mode", MODES)
def test_the_bound_tells_how_many_hidden_children_a_readable_parent_has(leaky, settings, mode):
    """Thread 1 holds one message the member may read and three it may not. Thread 4 holds 252
    the member may read, thread 5, which it may read too, one more. Threads 1 and 4 select 256
    candidates, decided by the walk; threads 1, 4 and 5 select 257, and the read sends today's
    statement after the check and the key read. The reader who counts its own 253 learns that
    thread 1 holds 256 - 252 - 1 = 3 it may not read."""
    keys = range(1000, 1253)
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=key, thread_id=4 if key < 1252 else 5) for key in keys]
    )
    leaky.write_relationships(
        [grant("scope/thread", 5, "reader")]
        + [grant("scope/message", key, "direct") for key in keys]
    )
    analyzed()
    counts = []
    for threads_read in ([1, 4], [1, 4, 5]):
        result, today, statements = both(
            lambda threads_read=threads_read: [
                m.pk for m in messages().filter(thread_id__in=threads_read).order_by("pk")
            ],
            settings,
            mode,
        )
        assert result == (
            "returned",
            [1, *(key for key in keys if key < 1252 or 5 in threads_read)],
        )
        assert statements[0][0] == parent_check(threads_read)
        counts.append((len(today), len(statements)))
    # The check (one statement for all the threads named), the key read, the walk's lookups,
    # the rows; past the bound the check, the key read and today's statement.
    assert counts == BOUND_COUNTS[mode], counts


BOUND_COUNTS: dict[Any, list[tuple[int, int]]] = {
    # ``editor`` asked of the compiled statement for the candidates ``direct`` left.
    True: [(1, 8), (1, 3)],
    # ``editor``'s rows, then group h's members, for message 7.
    "identifiers": [(1, 9), (1, 3)],
}


# What the walk's lookups show of a hidden child of a readable parent: each of threads 10-12,
# which the member may read, holds one message it may not: 10 one with no grant, 11 one granted
# to the member and muted, 12 one granted to group h. The statements each read sends, by kind:
# the check of the thread, the key read, the candidates that exist, a relationship lookup, a
# compiled part, the policy's fence read again after an exclusion.
WHAT_HOLDS: dict[Any, dict[int, list[str]]] = {
    # Keys mode reads no subject of a grant on a candidate: ``editor`` is asked of the compiled
    # statement unless ``direct`` settled the candidate first.
    True: {
        10: ["check", "keys", "rows", "lookup", "part", "fence"],
        11: ["check", "keys", "rows", "lookup", "lookup", "fence"],
        12: ["check", "keys", "rows", "lookup", "part", "fence"],
    },
    # Identifiers mode follows the group a grant on the candidate names.
    "identifiers": {
        10: ["check", "keys", "rows", "lookup", "lookup", "fence"],
        11: ["check", "keys", "rows", "lookup", "lookup", "fence"],
        12: ["check", "keys", "rows", "lookup", "lookup", "lookup", "fence"],
    },
}


def kind(sql: str) -> str:
    if sql.endswith("LIMIT 257"):
        return "keys"
    if re.match(r'SELECT "\w+"\."id" AS "pk", CASE WHEN', sql):
        return "part"
    if sql.startswith("WITH") or "rebac_ids" in sql:
        return "check"
    if '"rebac_relationship"' in sql.split(" FROM ")[1][:40]:
        return "lookup"
    if sql.startswith('SELECT 1 AS "a" FROM "rebac_schemageneration"'):
        return "fence"
    return "rows"


@pytest.mark.parametrize("mode", MODES)
def test_the_lookups_follow_what_holds_on_a_hidden_child_of_a_readable_parent(leaky, mode):
    ScopeThread._base_manager.bulk_create([ScopeThread(id=n) for n in (10, 11, 12)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=n, thread_id=n) for n in (10, 11, 12)])
    leaky.write_relationships(
        [
            *(grant("scope/thread", n, "reader") for n in (10, 11, 12)),
            grant("scope/message", 11, "direct"),
            grant("scope/message", 11, "muted"),
            grant("scope/message", 12, "editor", SubjectRef.of("auth/group", "h", "member")),
        ]
    )
    kinds = {}
    with override_settings(REBAC_BOUNDED_READ=mode):
        for thread in (10, 11, 12):
            result, statements = captured(
                lambda thread=thread: list(messages().filter(thread_id=thread).order_by("pk"))
            )
            assert result == ("returned", [])
            kinds[thread] = [kind(sql) for sql, _ in statements]
    assert kinds == WHAT_HOLDS[mode], kinds


# ---------- The record of held keys: one evaluation, one actor, one database ----------


def test_a_referred_key_is_held_only_inside_the_evaluation_that_read_the_referring_row(
    local, settings, caplog
):
    """The messages of thread 1 refer to thread 1: inside their prefetch the member holds key 1.
    Once that evaluation ends, a read of thread 1 by its key names a key the member supplies,
    and keeps today's statement."""
    settings.REBAC_BOUNDED_READ = "identifiers"
    with caplog.at_level(logging.DEBUG, logger="rebac.bounded"):
        rows = list(messages().filter(thread_id=1).order_by("pk").rebac_prefetch_related("thread"))
        assert [cached(m, "thread") for m in rows] == [1, 1, 1, 1]
        assert f"A read of {ScopeThread} is decided by its candidates." in logged(caplog)
        caplog.clear()
        assert [t.pk for t in threads().filter(pk=1).order_by("pk")] == [1]
    assert logged(caplog) == [f"A read of {ScopeThread} keeps its statement: {UNHELD}."]


def test_a_referred_key_is_held_only_by_the_actor_that_read_the_referring_row(
    local, settings, caplog
):
    """Inside the member's prefetch, a prefetched read pinned to another actor holds none of the
    keys the member's rows refer to."""
    settings.REBAC_BOUNDED_READ = "identifiers"
    with caplog.at_level(logging.DEBUG, logger="rebac.bounded"):
        rows = list(
            messages()
            .filter(thread_id=1)
            .order_by("pk")
            .rebac_prefetch_related(Prefetch("thread", queryset=threads(ADMIN)))
        )
    assert [m.pk for m in rows] == [1, 2, 3, 4]
    assert f"A read of {ScopeThread} keeps its statement: {UNHELD}." in logged(caplog)
    assert f"A read of {ScopeThread} is decided by its candidates." not in logged(caplog)


# ---------- Several statements: a commit between the walk and the rows ----------


def _concurrently(write: Callable[[], None]) -> None:
    """Run ``write`` in a transaction on a connection of its own, and commit it, before
    returning. Whatever happens, that transaction ends and that connection closes: an error in
    ``write`` rolls it back; a writer still running after 30 s has its PostgreSQL backend ended,
    which ends its transaction and its locks, and the test fails."""
    errors: list[BaseException] = []
    backends: list[int] = []

    def run() -> None:
        try:
            with transaction.atomic():
                if connection.vendor == "postgresql":
                    backends.append(connection.connection.info.backend_pid)
                write()
        except BaseException as error:
            errors.append(error)
        finally:
            for wrapper in connections.all(initialized_only=True):
                if wrapper.vendor == "sqlite" and wrapper.connection is not None:
                    # Django leaves an in-memory database open on close(); this thread's
                    # connection is one of several to it, and the test's own keeps it.
                    wrapper.connection.close()
                    wrapper.connection = None
                else:
                    wrapper.close()

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(30)
    if worker.is_alive():
        if backends:
            ending = connections.create_connection("default")
            try:
                with ending.cursor() as cursor:
                    cursor.execute("SELECT pg_terminate_backend(%s)", [backends[0]])
            finally:
                ending.close()
        worker.join(10)
        pytest.fail("the writer did not finish")
    assert not errors, errors


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("isolation", ["autocommit", "read_snapshot", "repeatable read"])
def test_the_rows_statement_reads_what_was_committed_after_the_walk(
    settings, monkeypatch, isolation
):
    """Pinned, as proposal 0025 states it. The member reads the messages of thread 1, and
    thread 1 by the forward prefetch of the thread they refer to. The walk admits thread 1 by
    the member's grant; a transaction then revokes the grant, rewrites the thread's label and
    commits; the thread's rows' statement reads the new label. Under READ COMMITTED, inside
    ``read_snapshot()`` too, the read returns what neither state gives: the messages and their
    thread, with content written after the member lost it. In a transaction at REPEATABLE READ
    every statement reads one snapshot, and the read returns the state before the write."""
    if connection.vendor != "postgresql" and isolation != "autocommit":
        pytest.skip("on SQLite a transaction reads one snapshot and holds the writer off")
    local = install_schema(SCHEMA)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1, label="old")])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=n, thread_id=1) for n in (1, 2)])
    local.write_relationships([grant("scope/thread", 1, "reader")])

    def read() -> list[tuple[int, Any]]:
        rows = (
            messages()
            .filter(thread_id=1)
            .order_by("pk")
            .rebac_prefetch_related(Prefetch("thread", queryset=threads().order_by("pk")))
        )
        return [(m.pk, getattr(m._state.fields_cache.get("thread"), "label", None)) for m in rows]

    with override_settings(REBAC_BOUNDED_READ=False):
        before = read()

    def write() -> None:
        local.delete_relationship(grant("scope/thread", 1, "reader"))
        ScopeThread._base_manager.filter(pk=1).update(label="written after the revocation")

    decided = walk.decided_alone
    fired: list[bool] = []

    def deciding(**kwargs: Any) -> Any:
        found = decided(**kwargs)
        if kwargs["resource_type"] == "scope/thread" and not fired:
            fired.append(True)
            _concurrently(write)
        return found

    monkeypatch.setattr(walk, "decided_alone", deciding)
    try:
        with override_settings(REBAC_BOUNDED_READ=True):
            if isolation == "autocommit":
                found = read()
            elif isolation == "read_snapshot":
                with read_snapshot():
                    found = read()
            else:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    found = read()
    finally:
        # Whatever failed above, the test's own connection holds no transaction after it.
        assert not connection.in_atomic_block
    with override_settings(REBAC_BOUNDED_READ=False):
        after = read()
    assert fired and (before, after) == ([(1, "old"), (2, "old")], [])
    if isolation == "repeatable read":
        assert found == before
    else:
        assert found == [(1, "written after the revocation"), (2, "written after the revocation")]


# ---------- Other refusals, each by its reason ----------


def test_a_model_whose_identity_is_not_its_integer_key_keeps_its_statement(settings, caplog):
    from django.contrib.auth import get_user_model

    from tests.testapp.models import TextIdentityPost

    local = install_schema(
        """
        definition auth/user {}
        definition blog/textidentitypost {
            relation shared: auth/user
            permission read = shared
        }
        """
    )
    with sudo(reason="fixture"):
        author = get_user_model()._base_manager.create(username="author")
        posts = [
            TextIdentityPost._base_manager.create(public_id=name, title=name, author=author)
            for name in ("a", "b")
        ]
    local.write_relationships(
        [RelationshipTuple(ObjectRef("blog/textidentitypost", "a"), "shared", MEMBER)]
    )
    refused_because(
        lambda: [
            p.public_id
            for p in TextIdentityPost.objects.with_actor(MEMBER)
            .filter(pk__in=[post.pk for post in posts])
            .order_by("pk")
        ],
        settings,
        caplog,
        "the model's identity is not its integer primary key",
    )


def test_the_walk_answers_nothing_under_another_policy_than_the_one_named(local):
    """``decided_alone`` reads the policy in force and answers only if it is the one the read's
    scope was built under."""
    policy = structural.current_policy()
    question: dict[str, Any] = {
        "backend": local,
        "resource_type": "scope/thread",
        "action": "read",
        "actor": MEMBER,
        "ids": ["1", "2", "3"],
        "using": "default",
        "keys_only": True,
    }
    assert walk.decided_alone(**question, policy_key=policy.key) == {"1"}
    assert walk.decided_alone(**question, policy_key=("another", "policy")) is None


# ---------- The decision rule of 0.38.0 ----------


def test_one_queryset_object_is_decided_at_each_evaluation(local, settings):
    settings.REBAC_BOUNDED_READ = "identifiers"
    queryset = ScopeMessage.objects.filter(thread_id=1).order_by("pk")
    with actor_context(MEMBER):
        assert [m.pk for m in queryset] == [1, 2, 3, 4]
    # The queryset keeps the query its evaluation decided, with the compiled scope: nothing
    # of the bounded read.
    query = queryset.query
    [_scope] = [child for child in query.where.children if isinstance(child, _ScopeWhere)]
    assert "rebac_ids" in str(query) and '"id" IN (1, 2, 3, 4)' not in str(query)
    queryset._result_cache = None
    with actor_context(OUTSIDER):
        assert [m.pk for m in queryset] == []
    with actor_context(OUTSIDER):
        assert queryset._clone().count() == 0
        statements = captured(lambda: [m.pk for m in queryset._clone().iterator()])[1]
        assert compiled(statements) and not probes(statements, "testapp_scopemessage")


def test_a_scope_kept_from_an_earlier_evaluation_is_read_as_kept(local, settings):
    """An eager ``scoped()`` queryset, and one object counted and then listed, read the scope
    an earlier evaluation built, with what it pinned then: the compiled statement, unchanged."""

    def eager():
        return [m.pk for m in messages().filter(thread_id=1).order_by("pk").scoped()]

    refused(eager, settings)

    def counted_then_listed():
        queryset = messages().filter(thread_id=1).order_by("pk")
        return queryset.count(), [m.pk for m in queryset]

    result, today, statements = both(counted_then_listed, settings)
    assert result == ("returned", [4, [1, 2, 3, 4]])
    assert _untimed(statements) == _untimed(today)


def test_a_scope_built_under_another_policy_is_not_decided_by_its_candidates(local, settings):
    settings.REBAC_BOUNDED_READ = "identifiers"
    queryset = messages().filter(thread_id=1).order_by("pk")
    scoped = queryset.scoped()
    install_schema(
        SCHEMA.replace(
            "permission read = thread->read + direct + editor", "permission read = direct"
        )
    )
    assert bounded.refusal(scoped, scoped.query) == "its scope was built under another policy"
    # The eager scope's revision fence reads nothing, as it always did.
    settings.REBAC_BOUNDED_READ = False
    expected = [m.pk for m in scoped._clone()]
    settings.REBAC_BOUNDED_READ = "identifiers"
    assert [m.pk for m in scoped._clone()] == expected


def test_another_database_is_read_on_that_database(settings, caplog, django_db_blocker, tmp_path):
    """The check of the parent, the keys, the walk and the rows are read on the database the
    evaluation decided: grants on the default database admit nothing on another, and the
    other's own grants do. A parent the reader may read there is checked there."""
    from django.core.management import call_command

    from rebac.models import active_relationship_model

    alias = "bounded_other"
    connections[alias] = sqlite_alias(alias, tmp_path / "other.sqlite3")
    try:
        with django_db_blocker.unblock():
            call_command("migrate", database=alias, verbosity=0)
            local = install_schema(SCHEMA)
            for database in ("default", alias):
                ScopeThread._base_manager.using(database).bulk_create([ScopeThread(id=1)])
                ScopeMessage._base_manager.using(database).bulk_create(
                    [ScopeMessage(id=n, thread_id=1) for n in (1, 2)]
                )
            local.write_relationships([grant("scope/thread", 1, "reader")])

            def read(database: str):
                return [m.pk for m in messages().using(database).filter(thread_id=1).order_by("pk")]

            def each(database: str, served: bool) -> Any:
                settings.REBAC_BOUNDED_READ = False
                expected, _today = captured(lambda: read(database), database)
                settings.REBAC_BOUNDED_READ = "identifiers"
                with CaptureQueriesContext(
                    connections["default" if database == alias else alias]
                ) as elsewhere:
                    found, statements = captured(lambda: read(database), database)
                settings.REBAC_BOUNDED_READ = False
                assert found == expected
                # Where thread 1 is readable on that database the read is decided by its
                # candidates; where it is not, its check keeps today's statement.
                assert bool(probes(statements, "testapp_scopemessage")) is served
                assert not compiled(statements, "testapp_scopemessage") or not served
                # Nothing of it is read on the other database.
                assert elsewhere.captured_queries == []
                return found

            assert each("default", True) == ("returned", [1, 2])
            assert each(alias, False) == ("returned", [])
            # A key a row read on one database refers to is not held on another: the thread
            # prefetched on the other database keeps its statement.
            settings.REBAC_BOUNDED_READ = "identifiers"
            with caplog.at_level(logging.DEBUG, logger="rebac.bounded"):
                list(
                    messages()
                    .filter(thread_id=1)
                    .order_by("pk")
                    .rebac_prefetch_related(
                        Prefetch("thread", queryset=threads().using(alias).order_by("pk"))
                    )
                )
            settings.REBAC_BOUNDED_READ = False
            assert f"A read of {ScopeThread} keeps its statement: {UNHELD}." in logged(caplog)
            relationships = active_relationship_model()._base_manager
            rows = list(relationships.using("default").all())
            for row in rows:
                row.pk = None
            relationships.using(alias).bulk_create(rows)
            relationships.using("default").all().delete()
            assert each(alias, True) == ("returned", [1, 2])
            assert each("default", False) == ("returned", [])
    finally:
        connections[alias].close()
        del connections[alias]


# ---------- Off: inert ----------


def test_off_nothing_of_the_mechanism_runs(local, settings, monkeypatch):
    """With the setting off (the default) no part of the mechanism is entered, for any read."""
    from rebac.compile import read as compiled

    entered: list[str] = []

    def refuse(name: str) -> Callable[..., Any]:
        def entering(*args: Any, **kwargs: Any) -> Any:
            entered.append(name)
            raise AssertionError(f"{name} ran while the setting is off")

        return entering

    for target, name in (
        (bounded, "read"),
        (bounded, "refusal"),
        (walk, "decided_alone"),
        (walk, "compiler_part"),
        (compiled, "scope_admits_nothing"),
    ):
        monkeypatch.setattr(target, name, refuse(name))
    assert bounded.mode() == ""
    for read in REFUSED.values():
        outcome(read)
    assert [m.pk for m in messages().filter(thread_id=1).order_by("pk")] == [1, 2, 3, 4]
    assert messages().get(pk=5).pk == 5
    assert list(messages().order_by("-pk").values_list("pk", flat=True)[:2]) == [5, 4]
    assert entered == []
    # The scope records no policy for it either, as before.
    assert getattr(messages().scoped().query, "_rebac_scope_policy", None) is None


# ---------- The checks ----------


@pytest.mark.parametrize("value", [False, True, "identifiers", "True"])
def test_a_warning_says_what_the_setting_discloses(settings, value):
    from rebac.checks import check_bounded_read

    settings.REBAC_BOUNDED_READ = value
    issues = check_bounded_read()
    if value in (True, "identifiers"):
        [issue] = issues
        assert issue.id == "rebac.W016"
        assert "experimental" in issue.msg and "257 primary keys" in issue.msg
        assert "latency" in issue.msg and "adjacent states" in issue.hint
        # What a reader still learns, as the proposal states it.
        assert "for a parent they may read, how many of its children (up to 256)" in issue.msg
        assert "of rows whose key or parent they do not already hold, nothing" in issue.msg
        assert "content written after the actor lost it" in issue.hint
        assert ("the foreign keys the walk follows from them" in issue.msg) is (
            value == "identifiers"
        )
    else:
        assert issues == []


# ---------- Random policies: the same rows as the compiled statement ----------


def keyed(seed: int) -> tuple[str, list[RelationshipTuple]]:
    """A policy whose threads are read by key alone in half its permissions."""
    rng = random.Random(seed)

    def union(atoms: list[str]) -> str:
        return " + ".join(rng.sample(atoms, rng.randint(1, len(atoms))))

    grants = ["reader", "admin->member", "pub", "owner_role->member"]
    denials = ["blocked", "withheld"]
    shape = rng.choice(["union", "minus", "both", "kept"])
    if shape == "union":
        read = union(grants)
    elif shape == "minus":
        read = f"({union(grants)}) - ({union(denials)})"
    elif shape == "both":
        read = f"({union(grants)}) & ({union(grants)})"
    else:
        read = f"(({union(grants)}) - ({union(denials)})) + ({union(grants)})"
    message = rng.choice(["direct", "direct - banned", "thread->read + direct", "direct + editor"])
    schema = f"""
definition auth/user {{}}
definition auth/group {{ relation member: auth/user }}
definition scope/role {{ relation member: auth/user | auth/group#member }}
definition scope/thread {{
    relation admin: scope/role // rebac:const=admin
    relation owner_role: scope/role // rebac:const=owner
    relation reader: auth/user
    relation pub: auth/user:*
    relation blocked: auth/user
    relation hidden: scope/thread // rebac:const={{"target_id":"hidden","filters":{{"label":"hidden"}}}}
    permission conceal = authenticated
    permission withheld = hidden->conceal
    permission read = {read}
}}
definition scope/message {{
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    relation banned: auth/user
    relation editor: auth/user | auth/group#member
    permission read = {message}
}}
definition scope/part {{
    relation message: scope/message // rebac:field=message
    relation direct: auth/user
    permission read = direct
}}
"""
    group = SubjectRef.of("auth/group", "g", "member")
    candidates = [
        grant("auth/group", "g", "member"),
        grant("scope/role", "admin", "member", ADMIN),
        grant("scope/role", "owner", "member", group),
        *(grant("scope/thread", n, "reader") for n in (1, 2, 3)),
        grant("scope/thread", 2, "pub", SubjectRef.of("auth/user", "*")),
        *(grant("scope/thread", n, "blocked") for n in (1, 2)),
        *(
            grant("scope/message", n, "direct", actor)
            for n in (1, 2, 3)
            for actor in (MEMBER, ADMIN)
        ),
        *(grant("scope/message", n, "banned") for n in (1, 3)),
        grant("scope/message", 2, "editor", group),
        *(grant("scope/part", n, "direct") for n in (1, 2, 3)),
    ]
    return schema, [item for item in candidates if rng.random() < 0.5]


def _pks(rows: Any) -> list[Any]:
    return [row.pk for row in rows]


def _reads(model: Any, actor: SubjectRef) -> dict[str, Callable[[], Any]]:
    def rows():
        return model.objects.with_actor(actor)

    reads: dict[str, Callable[[], Any]] = {
        "list": lambda: list(rows().order_by("pk")),
        "descending slice": lambda: list(rows().order_by("-pk")[:2]),
        "offset slice": lambda: list(rows().order_by("pk")[1:3]),
        "index": lambda: rows().order_by("-pk")[1],
        "unordered": lambda: list(rows().filter(pk__in=[1, 2, 3])),
        "one": lambda: list(rows().filter(pk=2)),
        "get": lambda: rows().get(pk=1),
        "first": lambda: rows().first(),
        "last": lambda: rows().last(),
        "in_bulk": lambda: rows().in_bulk([1, 2, 3, 4]),
        "len": lambda: len(rows()),
        "bool": lambda: bool(rows().filter(pk=3)),
        "values_list": lambda: list(rows().order_by("-pk").values_list("pk", flat=True)),
        "values": lambda: list(rows().order_by("pk").values("pk")),
        "count": lambda: rows().count(),
        "exists": lambda: rows().filter(pk=2).exists(),
        # Conditions the rule accepts, and conditions it refuses.
        "keys": lambda: list(rows().filter(pk__in=[1, 3]).order_by("-pk")),
        "no key": lambda: list(rows().filter(pk__isnull=True).order_by("pk")),
        "range": lambda: list(rows().filter(pk__gte=2).order_by("pk")),
        "or": lambda: list(rows().filter(Q(pk=1) | Q(pk=3)).order_by("pk")),
        "exclude": lambda: list(rows().exclude(pk=2).order_by("-pk")[:2]),
    }
    if model is ScopeThread:
        reads["content"] = lambda: list(rows().filter(label="hidden").order_by("pk"))
    if model is ScopePart:
        reads["by message"] = lambda: _pks(rows().filter(message_id__in=[1, 2]).order_by("pk"))
        reads["through a join"] = lambda: _pks(rows().filter(message__thread_id=1).order_by("pk"))
        # A forward prefetch of one row: the message the parts of message 1 refer to.
        reads["with their message"] = lambda: [
            (p.pk, cached(p, "message"))
            for p in rows().filter(message_id=1).order_by("pk").rebac_prefetch_related("message")
        ]
    if model is ScopeMessage:
        reads.update(
            {
                "by thread": lambda: _pks(rows().filter(thread_id=1).order_by("-pk")),
                "by thread and key": lambda: _pks(
                    rows().filter(thread__in=[1, 2], pk__in=[1, 3]).order_by("pk")
                ),
                "with a thread": lambda: _pks(rows().filter(thread__isnull=False).order_by("pk")),
                "by thread label": lambda: _pks(
                    rows().filter(thread__label="hidden").order_by("pk")
                ),
                "with its thread": lambda: [
                    (m.pk, m.thread.pk, m.thread.label)
                    for m in rows().filter(pk=3).rebac_prefetch_related("thread")
                ],
                "with their threads": lambda: [
                    (m.pk, m.thread.pk)
                    for m in rows().order_by("pk").rebac_prefetch_related("thread")
                ],
                "joined": lambda: [
                    (m.pk, m.thread.pk) for m in rows().select_related("thread").order_by("pk")
                ],
                "guarded join": lambda: [
                    (m.pk, m.thread.pk)
                    for m in rows().rebac_select_related("thread").order_by("pk")
                ],
                "parts": lambda: {
                    m.pk: _pks(m.scopepart_set.all())
                    for m in rows().order_by("pk").rebac_prefetch_related("scopepart_set")
                },
                # A reverse prefetch ordered by the key: its parents, admitted by the
                # evaluation that read them, are the keys its children's read names.
                "parts by key": lambda: {
                    m.pk: _pks(m.scopepart_set.all())
                    for m in rows()
                    .order_by("pk")
                    .rebac_prefetch_related(
                        Prefetch(
                            "scopepart_set",
                            queryset=ScopePart.objects.with_actor(actor).order_by("pk"),
                        )
                    )
                },
                "parts of thread 1 by key": lambda: {
                    m.pk: _pks(m.scopepart_set.all())
                    for m in rows()
                    .filter(thread_id=1)
                    .order_by("-pk")
                    .rebac_prefetch_related(
                        Prefetch(
                            "scopepart_set",
                            queryset=ScopePart.objects.with_actor(actor).order_by("-pk"),
                        )
                    )
                },
            }
        )
    if model is ScopeThread:
        reads["messages"] = lambda: {
            t.pk: {m.pk: _pks(m.scopepart_set.all()) for m in t.scopemessage_set.all()}
            for t in rows().order_by("pk").rebac_prefetch_related("scopemessage_set__scopepart_set")
        }
        reads["messages and parts by key"] = lambda: {
            t.pk: {m.pk: _pks(m.scopepart_set.all()) for m in t.scopemessage_set.all()}
            for t in rows()
            .order_by("pk")
            .rebac_prefetch_related(
                Prefetch(
                    "scopemessage_set",
                    queryset=ScopeMessage.objects.with_actor(actor).order_by("pk"),
                ),
                Prefetch(
                    "scopemessage_set__scopepart_set",
                    queryset=ScopePart.objects.with_actor(actor).order_by("pk"),
                ),
            )
        }
    return reads


GENERATORS: dict[str, Callable[[int], tuple[str, list[RelationshipTuple]]]] = {
    "inherited": build,
    "subtracting": subtracting,
    "application": lambda seed: application_shaped(seed)[:2],
    "keyed": keyed,
}
# The cases the differential compares, and the reads the bounded path served among them.
CASES: dict[str, int] = {"compared": 0, "served": 0}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("structural_prefetch", [False, True], ids=["", "structural"])
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("generator", list(GENERATORS))
@pytest.mark.parametrize("seed", seeds(24, fast=3))
def test_a_bounded_read_reads_what_the_compiled_statement_reads(
    seed, generator, storage, structural_prefetch, settings, monkeypatch
):
    """Random policies with unions, intersections, exclusions, constants, filtered constants,
    caveats, wildcards and subject sets: for every model, actor and read, with the setting in
    each mode, inside an evaluator scope and outside one, the read returns, or raises, what it
    does with the setting off, in the same order."""
    schema, tuples = GENERATORS[generator](seed)
    seeded(schema, tuples, storage, settings)
    # Structural prefetch delivers children of admitted parents with no scope, before the
    # bounded read is asked; the same record proves the parents of the children it does not.
    settings.REBAC_STRUCTURAL_PREFETCH = structural_prefetch
    if generator == "application":
        label_threads(application_shaped(seed)[2])
    served: list[bool] = []
    read_bounded = bounded.read

    def counting(queryset, query, **kwargs):
        found = read_bounded(queryset, query, **kwargs)
        served.append(found is not None)
        return found

    monkeypatch.setattr(bounded, "read", counting)
    # What the key read and the walk select from model tables, for every read either mode walks.
    boundary = Boundary(monkeypatch)
    compared = 0
    for actor in (ADMIN, MEMBER, OUTSIDER):
        for model in (ScopeThread, ScopeMessage, ScopePart):
            for name, read in _reads(model, actor).items():
                with override_settings(REBAC_BOUNDED_READ=False):
                    expected = outcome(read)
                    with evaluator_scope():
                        expected_scoped = outcome(read)
                assert expected_scoped == expected, (actor, model, name)
                for mode in MODES:
                    with override_settings(REBAC_BOUNDED_READ=mode):
                        assert outcome(read) == expected, (actor, model, name, mode, seed)
                        with evaluator_scope():
                            assert outcome(read) == expected, (actor, model, name, mode, "scope")
                    compared += 2
    CASES["compared"] += compared
    CASES["served"] += sum(served)
    # Read with ``-s``: the cases this run compared, and how many reads were decided by candidates.
    print(
        f"bounded differential {generator} {seed} {storage}: {compared} compared, "
        f"{sum(served)} decided, {boundary.windows} walked"
    )
    assert boundary.found == [], boundary.found[:3]
    if generator == "keyed":
        # These policies are read by the walk alone: the differential exercises the mechanism,
        # not only its fallback. (An application-shaped policy can leave no read served once
        # every key must be held: its thread's read follows a parent the reads do not name.)
        assert any(served)


@pytest.mark.parametrize("generator", list(GENERATORS))
@pytest.mark.parametrize("seed", seeds(12, fast=3))
def test_where_the_guard_lets_a_question_through_the_walk_answers_it_alone(
    seed, generator, settings, monkeypatch
):
    """``compiler_part`` is read from the policy, ``decided_alone`` from the rows. Over the
    random policies, wherever the first finds nothing, the second answers in both modes with
    exactly what the compiled statement answers, or answers nothing only where it would ask the
    compiled statement for another permission of the question's type, reaching half of what the
    question reaches, over the candidates its readable arms left. Where the guard refuses, a read never reaches the walk."""
    schema, tuples = GENERATORS[generator](seed)
    local = seeded(schema, tuples, "denormalized", settings)
    if generator == "application":
        label_threads(application_shaped(seed)[2])
    policy = structural.current_policy()
    refused: list[tuple[str, str]] = []
    compiled_part = walk._Walk._compiled

    def recording(self, key, ids):
        found = compiled_part(self, key, ids)
        if found is None:
            refused.append(key)
        return found

    monkeypatch.setattr(walk._Walk, "_compiled", recording)
    candidates = {
        "scope/thread": ["1", "2"],
        "scope/message": ["1", "2", "3"],
        "scope/part": ["1", "2", "3"],
    }
    let_through = answered = 0
    for resource_type, ids in candidates.items():
        definition = policy.schema.get_definition(resource_type)
        for action in [permission.name for permission in definition.permissions]:
            for actor in (ADMIN, MEMBER, OUTSIDER):
                expected = local.held(
                    subject=actor, action=action, resource_type=resource_type, ids=ids
                )
                for keys_only in (False, True):
                    if walk.compiler_part(policy, resource_type, action, keys_only=keys_only):
                        continue
                    let_through += 1
                    refused.clear()
                    found = walk.decided_alone(
                        backend=local,
                        resource_type=resource_type,
                        action=action,
                        actor=actor,
                        ids=ids,
                        using="default",
                        policy_key=policy.key,
                        keys_only=keys_only,
                    )
                    question = (resource_type, action, actor, keys_only, seed)
                    if found is None:
                        # Only a permission of the question's type that reaches half of what the
                        # question reaches ends the walk on these policies.
                        assert refused and all(
                            key[0] == resource_type
                            and key != (resource_type, action)
                            and 2 * walk._reach(policy, key)
                            >= walk._reach(policy, (resource_type, action))
                            for key in refused
                        ), (question, refused)
                    else:
                        answered += 1
                        assert found == expected, question
    if generator in ("application", "keyed"):
        assert answered
