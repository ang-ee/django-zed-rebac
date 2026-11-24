"""Bounded reads (proposal 0025): a narrow list read decided by its candidate rows.

With ``REBAC_BOUNDED_READ`` on, a list read whose own conditions select at most 256 rows
reads their keys without the permission, the walk decides exactly those keys, and the read's
own statement runs with the admitted keys in place of its scope. Here: the same rows, order,
slices and downstream work as the compiled statement over random policies, both storage
layouts and both modes; the eligibility rule, under which every read it refuses sends the
compiled statement byte for byte; the guard against the walk's compiled parts; the decision
rule of 0.38.0; what crosses into the process; the statement count; and inertness when off.
"""

from __future__ import annotations

import logging
import random
import re
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
    OuterRef,
    Prefetch,
    Q,
    Subquery,
)
from django.db.models.expressions import RawSQL
from django.db.models.functions import Random
from django.test.utils import CaptureQueriesContext

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


def compiled(statements: list[tuple[str, Any]]) -> list[str]:
    """The statements that carry a compiled scope: an id set or a recursion."""
    return [sql for sql, _ in statements if "rebac_ids" in sql or sql.startswith("WITH")]


def both(read: Callable[[], Any], settings: Any, mode: Any = "identifiers"):
    """The read with the setting off and on: both outcomes must agree."""
    settings.REBAC_BOUNDED_READ = False
    expected, today = captured(read)
    settings.REBAC_BOUNDED_READ = mode
    found, statements = captured(read)
    settings.REBAC_BOUNDED_READ = False
    assert found == expected
    return expected, today, statements


def messages(actor: SubjectRef = MEMBER):
    return ScopeMessage.objects.with_actor(actor)


# ---------- A narrow read, decided by its candidates ----------


def test_a_narrow_read_is_decided_by_its_candidates(local, settings):
    result, today, statements = both(
        lambda: list(messages().filter(thread_id=1).order_by("pk")), settings
    )
    assert result == ("returned", [(n, n, 1) for n in (1, 2, 3, 4)])
    assert compiled(today) and not compiled(statements)
    [probe] = probes(statements, "testapp_scopemessage")
    # The read's own conditions, no ordering, no slice, the key column alone.
    assert probe == (
        'SELECT "testapp_scopemessage"."id" AS "pk" FROM "testapp_scopemessage" '
        'WHERE "testapp_scopemessage"."thread_id" = %s LIMIT 257'.replace("%s", "1")
        if connection.vendor == "sqlite"
        else probe
    )
    final = statements[-1][0]
    assert '"testapp_scopemessage"."id" IN (' in final and "ORDER BY" in final


def test_the_rows_are_read_in_the_reads_order_with_its_slice_taken_after_the_decision(
    local, settings
):
    # Messages 1-4 and 5 are readable, 6 and 7 not; 7 sorts first descending.
    for read in (
        lambda: list(messages().order_by("-pk")[:2]),
        lambda: list(messages().order_by("-pk")[1:3]),
        lambda: list(messages().order_by("pk")[3:]),
        lambda: messages().order_by("-pk")[0],
        lambda: messages().order_by("-thread_id", "pk")[:3],
    ):
        result, _today, statements = both(read, settings)
        assert result[0] == "returned"
        assert probes(statements, "testapp_scopemessage") and not compiled(statements)
    assert both(lambda: list(messages().order_by("-pk")[:2]), settings)[0] == (
        "returned",
        [(5, 5, 2), (4, 4, 1)],
    )


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda: messages().get(pk=2), id="get"),
        pytest.param(lambda: messages().get(pk=6), id="get-hidden"),
        pytest.param(lambda: messages().first(), id="first"),
        pytest.param(lambda: messages().last(), id="last"),
        pytest.param(lambda: messages().order_by("pk").last(), id="last-ordered"),
        pytest.param(lambda: messages().in_bulk([6]), id="in_bulk-one"),
        pytest.param(lambda: len(messages().filter(thread_id=2).order_by("pk")), id="len"),
        pytest.param(lambda: bool(messages().filter(thread_id=2, pk=6)), id="bool"),
        pytest.param(
            lambda: list(messages().order_by("pk").values_list("pk", flat=True)), id="values_list"
        ),
        pytest.param(
            lambda: list(messages().order_by("-pk").values("pk", "thread_id")), id="values"
        ),
        pytest.param(
            lambda: list(messages().order_by("pk").values_list("pk", "thread__label", named=True)),
            id="named-values",
        ),
        pytest.param(
            lambda: [
                (m.pk, m.thread.pk) for m in messages().select_related("thread").order_by("pk")
            ],
            id="select_related",
        ),
        pytest.param(
            lambda: messages().filter(thread_id=1).order_by("pk").distinct()[:2], id="distinct"
        ),
        pytest.param(
            lambda: list(
                messages().annotate(double=F("pk") * 2).filter(double__gt=4).order_by("pk")
            ),
            id="annotation",
        ),
    ],
)
def test_each_entry_point_reads_what_the_compiled_statement_reads(local, settings, read):
    result, _today, statements = both(read, settings)
    # Each is decided by its candidates: no compiled scope is sent.
    assert probes(statements, "testapp_scopemessage"), result
    assert not compiled(statements), result


def test_a_guarded_join_is_guarded_as_ever(local, settings):
    """``rebac_select_related()`` refuses a joined thread the member may not read: message 5
    is readable through its direct grant, its thread is not."""
    result, _today, statements = both(
        lambda: list(messages().rebac_select_related("thread").order_by("pk")), settings
    )
    assert result[:2] == ("raised", "PermissionDenied")
    assert probes(statements, "testapp_scopemessage")


def test_prefetched_children_are_decided_by_their_candidates_too(local, settings):
    """A prefetch whose queryset ends ties by the key: its children are decided as any read."""

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

    result, _today, statements = both(read, settings)
    assert result == ("returned", {1: {4: [4], 3: [3], 2: [2], 1: [1]}})
    assert probes(statements, "testapp_scopemessage") and probes(statements, "testapp_scopepart")
    assert not compiled(statements)


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
    assert probes(statements, "testapp_scopethread")
    assert not probes(statements, "testapp_scopemessage") and compiled(statements)


def test_structural_delivery_comes_first(local, settings):
    """A child query the running prefetch delivers carries no scope to replace."""
    settings.REBAC_STRUCTURAL_PREFETCH = True

    def read():
        threads = (
            ScopeThread.objects.with_actor(MEMBER)
            .order_by("pk")
            .rebac_prefetch_related("scopemessage_set")
        )
        return {thread.pk: [m.pk for m in thread.scopemessage_set.all()] for thread in threads}

    result, _today, statements = both(read, settings)
    assert result == ("returned", {1: [1, 2, 3, 4]})
    assert not probes(statements, "testapp_scopemessage")


# ---------- The bound ----------


def fill(thread: int, count: int, start: int = 100) -> None:
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=start + n, thread_id=thread) for n in range(count)]
    )


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
        assert not compiled(statements)
    else:
        # The key read, then the compiled read, byte for byte: never a second batch.
        assert statements == [(probe, statements[0][1]), *today]


def test_the_statement_count_does_not_depend_on_the_candidates(local, settings):
    """One key read, the walk's lookups, the rows: as many statements for 1 candidate as for 200."""
    fill(2, 198)
    settings.REBAC_BOUNDED_READ = "identifiers"
    few = captured(lambda: list(messages().filter(thread_id=3).order_by("pk")))[1]
    many = captured(lambda: list(messages().filter(thread_id=2).order_by("pk")))[1]
    assert len(probes(few, "testapp_scopemessage")) == len(probes(many, "testapp_scopemessage"))
    assert len(few) == len(many)
    # The key read; the candidate rows; their threads; reader, the admin role, blocked; the
    # hidden constant; the message's direct and editor grants; the rows.
    assert len(many) == 9, "\n".join(sql for sql, _ in many)


def test_a_read_that_selects_nothing_sends_the_key_read_alone(local, settings):
    result, _today, statements = both(
        lambda: list(messages().filter(thread_id=99).order_by("pk")), settings
    )
    assert result == ("returned", [])
    assert len(statements) == 1 and probes(statements, "testapp_scopemessage")


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
    "iterator": lambda: [m.pk for m in messages().filter(thread_id=1).iterator(chunk_size=10)],
    "unordered rows": lambda: sorted(m.pk for m in messages().filter(thread_id=1)),
    "in_bulk of several": lambda: messages().in_bulk([1, 5, 6]),
    "ordering with ties": lambda: [m.pk for m in messages().order_by("thread_id")],
    "none()": lambda: list(messages().none()),
}


# Why each read above keeps its statement, as ``rebac.bounded`` logs it; ``None``: a path the
# bounded read does not serve at all, which never asks.
REFUSED_BECAUSE: dict[str, str | None] = {
    "count": None,
    "exists": None,
    "aggregate": None,
    "annotated aggregate": "an aggregate or a grouping",
    "grouped values": "an aggregate or a grouping",
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
    "iterator": None,
    "unordered rows": "its ordering leaves ties",
    "in_bulk of several": "its ordering leaves ties",
    "ordering with ties": "its ordering leaves ties",
    "none()": "it selects nothing",
}


@pytest.mark.parametrize("name", list(REFUSED))
def test_a_read_the_rule_refuses_sends_the_compiled_statement(local, settings, caplog, name):
    with caplog.at_level(logging.DEBUG, logger="rebac.bounded"):
        refused(REFUSED[name], settings)
    kept = [record.getMessage() for record in caplog.records if record.name == "rebac.bounded"]
    because = REFUSED_BECAUSE[name]
    if because is None:
        assert kept == []
    else:
        assert kept and all(f"keeps its statement: {because}" in message for message in kept), kept


def test_select_for_update_sends_the_compiled_statement(local, settings):
    def read():
        with transaction.atomic():
            return [m.pk for m in messages().select_for_update().filter(thread_id=1)]

    refused(read, settings)


@pytest.mark.postgresql
def test_distinct_on_fields_sends_the_compiled_statement(local, settings):
    if connection.vendor != "postgresql":
        pytest.skip("DISTINCT ON is PostgreSQL's")
    refused(lambda: list(messages().order_by("thread_id", "pk").distinct("thread_id")), settings)


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
def test_a_bypass_sends_its_own_statement(local, settings, read):
    refused(read, settings)


def test_a_bypass_block_sends_its_own_statement(local, settings):
    def read():
        with sudo(reason="test"):
            return [m.pk for m in ScopeMessage.objects.order_by("pk")]

    refused(read, settings)

    def system():
        with system_context(reason="test"):
            return [m.pk for m in ScopeMessage.objects.order_by("pk")]

    refused(system, settings)


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
        return [m.pk for m in messages().order_by("pk")]

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
    result, _today, statements = both(
        lambda: [p.title for p in Post.objects.with_actor(alice).order_by("pk")], settings
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
    ordered = messages(OUTSIDER).order_by("pk")
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


def test_keys_mode_reads_nothing_of_a_candidate_but_its_key(local, settings, monkeypatch):
    """Threads: direct and public grants, a constant role and a filtered constant, all read
    by key. The walk follows no foreign key of a thread and no subject of a grant on one."""
    followed: list[tuple[str, set[str]]] = []
    targets = walk._Walk._targets

    def recording(self, resolved, source, ids):
        followed.append((resolved.source_model._meta.label, set(ids)))
        return targets(self, resolved, source, ids)

    monkeypatch.setattr(walk._Walk, "_targets", recording)
    result, _today, statements = both(
        lambda: [t.pk for t in ScopeThread.objects.with_actor(MEMBER).order_by("pk")],
        settings,
        mode=True,
    )
    assert result == ("returned", [1])
    assert probes(statements, "testapp_scopethread") and not compiled(statements)
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
    both(lambda: [m.pk for m in messages().filter(thread_id=2).order_by("pk")], settings)
    # Message 6 is not readable; its thread's key was read.
    assert {"5", "6"} in followed


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


def test_another_database_is_read_on_that_database(settings, django_db_blocker, tmp_path):
    from django.core.management import call_command

    """The keys, the walk and the rows are read on the database the evaluation decided: grants
    on the default database admit nothing on another, and the other's own grants do."""
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
            local.write_relationships([grant("scope/message", 2, "direct")])

            def read(database: str):
                return [m.pk for m in messages().using(database).order_by("pk")]

            def each(database: str) -> Any:
                settings.REBAC_BOUNDED_READ = False
                expected, _today = captured(lambda: read(database), database)
                settings.REBAC_BOUNDED_READ = "identifiers"
                with CaptureQueriesContext(
                    connections["default" if database == alias else alias]
                ) as elsewhere:
                    found, statements = captured(lambda: read(database), database)
                settings.REBAC_BOUNDED_READ = False
                assert found == expected
                assert probes(statements, "testapp_scopemessage")
                assert not compiled(statements)
                # Nothing of it is read on the other database.
                assert elsewhere.captured_queries == []
                return found

            assert each("default") == ("returned", [2])
            assert each(alias) == ("returned", [])
            relationships = active_relationship_model()._base_manager
            rows = list(relationships.using("default").all())
            for row in rows:
                row.pk = None
            relationships.using(alias).bulk_create(rows)
            relationships.using("default").all().delete()
            assert each(alias) == ("returned", [2])
            assert each("default") == ("returned", [])
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
        assert ("foreign keys" in issue.msg) is (value == "identifiers")
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
    }
    if model is ScopeMessage:
        reads.update(
            {
                "by thread": lambda: _pks(rows().filter(thread_id=1).order_by("-pk")),
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
            }
        )
    if model is ScopeThread:
        reads["messages"] = lambda: {
            t.pk: {m.pk: _pks(m.scopepart_set.all()) for m in t.scopemessage_set.all()}
            for t in rows().order_by("pk").rebac_prefetch_related("scopemessage_set__scopepart_set")
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
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("generator", list(GENERATORS))
@pytest.mark.parametrize("seed", seeds(24, fast=3))
def test_a_bounded_read_reads_what_the_compiled_statement_reads(
    seed, generator, storage, settings, monkeypatch
):
    """Random policies with unions, intersections, exclusions, constants, filtered constants,
    caveats, wildcards and subject sets: for every model, actor and read, with the setting in
    each mode, inside an evaluator scope and outside one, the read returns, or raises, what it
    does with the setting off, in the same order."""
    schema, tuples = GENERATORS[generator](seed)
    seeded(schema, tuples, storage, settings)
    if generator == "application":
        label_threads(application_shaped(seed)[2])
    served: list[bool] = []
    read_bounded = bounded.read

    def counting(queryset, query, **kwargs):
        found = read_bounded(queryset, query, **kwargs)
        served.append(found is not None)
        return found

    monkeypatch.setattr(bounded, "read", counting)
    compared = 0
    for actor in (ADMIN, MEMBER, OUTSIDER):
        for model in (ScopeThread, ScopeMessage, ScopePart):
            for name, read in _reads(model, actor).items():
                settings.REBAC_BOUNDED_READ = False
                expected = outcome(read)
                with evaluator_scope():
                    expected_scoped = outcome(read)
                assert expected_scoped == expected, (actor, model, name)
                for mode in MODES:
                    settings.REBAC_BOUNDED_READ = mode
                    assert outcome(read) == expected, (actor, model, name, mode, seed)
                    with evaluator_scope():
                        assert outcome(read) == expected, (actor, model, name, mode, "scope")
                    compared += 2
    CASES["compared"] += compared
    CASES["served"] += sum(served)
    # Read with ``-s``: the cases this run compared, and how many reads were decided by candidates.
    print(
        f"bounded differential {generator} {seed} {storage}: {compared} compared, {sum(served)} decided"
    )
    if generator in ("application", "keyed"):
        # These policies are read by the walk alone: the differential exercises the mechanism,
        # not only its fallback.
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
