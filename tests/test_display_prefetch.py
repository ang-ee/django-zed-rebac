"""Display rows through a prefetch (proposal 0024): ``for_display()`` on a forward to-one prefetch."""

from __future__ import annotations

import asyncio
import contextvars
import pickle
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, connections, models, transaction
from django.db.models import F, Prefetch, Value
from django.db.models.expressions import ColPairs
from django.db.models.fields.related_descriptors import ForwardManyToOneDescriptor
from django.db.models.fields.tuple_lookups import TupleIn
from django.db.models.functions import Upper
from django.db.models.query import ModelIterable
from django.db.models.signals import post_init
from django.test.utils import CaptureQueriesContext

from rebac import (
    LocalBackend,
    MissingActorError,
    PermissionDenied,
    SubjectRef,
    TrackedQuerySet,
    actor_context,
    display,
    structural,
)
from rebac.compile import read as compiled
from rebac.field_visibility import redacted_fields
from rebac.models import SchemaDefinition, SchemaOverride, SchemaPermission, SchemaRelation
from rebac.schema.parser import parse_zed
from rebac.testing import compare_mechanisms, install_schema
from tests.mechanism_policies import application_shaped, grant, label_threads, seeds
from tests.testapp.models import (
    CodedFolder,
    Folder,
    LabAccount,
    LabBadge,
    LabGatedNote,
    LabNote,
    LabPlace,
    LabPlain,
    LabPublicAccount,
    LabShop,
    Post,
    ScopeMessage,
    ScopePart,
    ScopeProfile,
    ScopeThread,
)

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
MEMBER = SubjectRef.of("auth/user", "member")
OTHER = SubjectRef.of("auth/user", "other")

SCHEMA = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
    permission write = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission read = direct
    permission write = direct
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    relation direct: auth/user
    permission read = direct
}
definition scope/profile {
    relation owner: scope/message // rebac:field=owner
    relation reader: auth/user
    permission read = reader
}
"""
# A part of a readable message is readable: structural prefetch can deliver parts.
INHERITING = SCHEMA.replace(
    "    relation direct: auth/user\n    permission read = direct\n}\ndefinition scope/profile",
    "    relation direct: auth/user\n    permission read = message->read + direct\n}\n"
    "definition scope/profile",
)
# The member may not read a message's thread field.
GATED_THREAD = SCHEMA.replace(
    "    permission write = direct\n}\ndefinition scope/part",
    "    permission write = direct\n    permission read__thread = nil\n}\ndefinition scope/part",
)
# Each variant's replacement found its text.
assert SCHEMA != INHERITING and SCHEMA != GATED_THREAD


def install(schema: str = SCHEMA) -> Any:
    """The schema, with the member's grants: messages 1-3 and parts 1-3, no thread, no profile.

    The other actor reads thread 1 and nothing else.
    """
    local = install_schema(schema)
    local.write_relationships(
        [grant("scope/message", n, "direct") for n in (1, 2, 3)]
        + [grant("scope/part", n, "direct") for n in (1, 2, 3)]
        + [grant("scope/thread", 1, "reader", OTHER)]
    )
    return local


def seed() -> None:
    """Threads 1-3; messages 1 and 2 in thread 1, 3 in thread 2, 4 in thread 3; a part of each of
    the first three and a profile of messages 1 and 4."""
    ScopeThread._base_manager.bulk_create(
        [ScopeThread(id=n, label=label) for n, label in ((1, "first"), (2, "second"), (3, "third"))]
    )
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=n, thread_id=t) for n, t in ((1, 1), (2, 1), (3, 2), (4, 3))]
    )
    ScopePart._base_manager.bulk_create(
        [ScopePart(id=n, message_id=n, fragment_id=n) for n in (1, 2, 3)]
    )
    ScopeProfile._base_manager.bulk_create(
        [ScopeProfile(id=1, owner_id=1, nick="one"), ScopeProfile(id=4, owner_id=4, nick="four")]
    )


@pytest.fixture
def world():
    seed()
    local = install()
    display.declare(ScopeThread, "label")
    yield local
    display.declare(ScopeThread)


@pytest.fixture
def messages_shown():
    """Messages declare their key alone: a display message hides its thread."""
    display.declare(ScopeMessage, "id")
    yield
    display.declare(ScopeMessage)


def shown_threads(actor: SubjectRef = MEMBER) -> Any:
    return ScopeThread.objects.with_actor(actor).for_display()


def statements(read_rows: Callable[[], Any]) -> tuple[Any, list[str]]:
    """What a read returns and the statements of its second run (the first one warms kept state)."""
    read_rows()
    with CaptureQueriesContext(connection) as captured:
        found = read_rows()
    return found, [query["sql"] for query in captured.captured_queries]


def messages(thread_queryset: Any = None, actor: SubjectRef = MEMBER, **prefetch: Any) -> list[Any]:
    """The actor's messages, each with its thread prefetched through ``thread_queryset``."""
    queryset = ScopeMessage.objects.with_actor(actor).order_by("pk")
    if thread_queryset is not None:
        queryset = queryset.prefetch_related(
            Prefetch("thread", queryset=thread_queryset, **prefetch)
        )
    return list(queryset)


def related(row: Any, name: str) -> Any:
    """``row``'s related object ``name``, or None where its prefetch delivered none."""
    try:
        return getattr(row, name)
    except type(row)._meta.get_field(name).related_model.DoesNotExist:
        return None


def threads_of(rows: list[Any]) -> list[Any]:
    """Each message's thread as ``(pk, label, display row)``, or None where none was delivered."""
    return [
        None
        if (thread := related(row, "thread")) is None
        else (thread.pk, thread.label, display.is_display_row(thread))
        for row in rows
    ]


DELIVERED = [(1, "first", True), (1, "first", True), (2, "second", True)]


# ---------- The request changes nothing where it does not apply ----------


@pytest.mark.parametrize("setting", [False, "fallback", True])
def test_where_it_does_not_apply_the_request_changes_no_statement(world, settings, setting):
    """Off, every statement is today's; on, a model that declares no display fields reads as
    before. The statement text is compared, on PostgreSQL as well (``pg_delta``)."""
    settings.REBAC_DISPLAY_FIELDS = setting
    if setting:
        display.declare(ScopeThread)
    plain, plain_sql = statements(
        lambda: threads_of(messages(ScopeThread.objects.with_actor(MEMBER)))
    )
    asked, asked_sql = statements(lambda: threads_of(messages(shown_threads())))
    assert asked_sql == plain_sql
    assert asked == plain == [None, None, None]
    # The thread prefetch is the scoped read.
    assert len(asked_sql) == len(statements(messages)[1]) + 1 and "rebac_" in asked_sql[-1]


def test_outside_a_prefetch_the_request_changes_no_statement(world, settings):
    settings.REBAC_DISPLAY_FIELDS = True

    def first_two(queryset: Any) -> list[Any]:
        return list(queryset.filter(pk__in=[1, 2]).order_by("pk"))

    plain, plain_sql = statements(lambda: first_two(ScopeThread.objects.with_actor(OTHER)))
    asked, asked_sql = statements(lambda: first_two(shown_threads(OTHER)))
    assert asked_sql == plain_sql and [t.pk for t in asked] == [t.pk for t in plain] == [1]
    assert not display.is_display_row(asked[0])


# ---------- Delivery ----------


@pytest.mark.parametrize("setting", ["fallback", True])
def test_rows_the_parents_refer_to_are_delivered_without_a_permission_statement(
    world, settings, setting
):
    settings.REBAC_DISPLAY_FIELDS = setting
    _rows, alone = statements(messages)
    found, sqls = statements(lambda: threads_of(messages(shown_threads())))
    assert found == DELIVERED
    # The root's statements, then the library's own read of the threads by primary key, with
    # their keys and labels: not the prefetch's lookup, which never runs.
    assert sqls[: len(alone)] == alone and len(sqls) == len(alone) + 1
    label_read = sqls[-1]
    assert "rebac_" not in label_read
    assert label_read.startswith(
        'SELECT "testapp_scopethread"."id" AS "id", "testapp_scopethread"."label" AS "label" '
        'FROM "testapp_scopethread" WHERE "testapp_scopethread"."id" IN ('
    )


def test_a_prefetch_that_takes_the_roots_actor_is_delivered(world, settings):
    """``rebac_prefetch_related`` pins the root's actor on a prefetch queryset that has none."""
    settings.REBAC_DISPLAY_FIELDS = True
    rows = list(
        ScopeMessage.objects.with_actor(MEMBER)
        .order_by("pk")
        .rebac_prefetch_related(Prefetch("thread", queryset=ScopeThread.objects.for_display()))
    )
    assert threads_of(rows) == DELIVERED
    assert all(row.thread.actor() == MEMBER for row in rows)


def test_a_row_the_actor_may_read_is_a_display_row_too(world, settings):
    """The request is not a check: it delivers display rows whether or not the actor holds them."""
    settings.REBAC_DISPLAY_FIELDS = "fallback"
    world.write_relationships([grant("scope/thread", 1, "reader")])
    assert threads_of(messages(shown_threads())) == DELIVERED
    # The same read without the request delivers the thread it holds, whole.
    plain = threads_of(messages(ScopeThread.objects.with_actor(MEMBER)))
    assert plain == [(1, "first", False), (1, "first", False), None]


def test_a_to_attr_prefetch_is_delivered(world, settings):
    settings.REBAC_DISPLAY_FIELDS = True
    rows = messages(shown_threads(), to_attr="shown")
    assert [(row.shown.pk, display.is_display_row(row.shown)) for row in rows] == [
        (1, True),
        (1, True),
        (2, True),
    ]


def test_a_reverse_one_to_one_is_read_by_its_scope(world, settings):
    """The target holds the one-to-one to the parent: the parent holds no key of it, so this is not
    a reference the reader read. The member reads no profile."""
    settings.REBAC_DISPLAY_FIELDS = True
    display.declare(ScopeProfile, "nick")
    try:
        _rows, sqls = statements(
            lambda: list(
                ScopeMessage.objects.with_actor(MEMBER).prefetch_related(
                    Prefetch(
                        "profile", queryset=ScopeProfile.objects.with_actor(MEMBER).for_display()
                    )
                )
            )
        )
        assert not any(hasattr(row, "profile") for row in _rows)
        assert 'FROM "testapp_scopeprofile"' in sqls[-1] and "rebac_" in sqls[-1]
    finally:
        display.declare(ScopeProfile)


def test_a_display_rows_relations_followed_lazily_load_as_any_rows_do(
    world, settings, messages_shown
):
    """Followed lazily, a display message's relations load as the whole message's do. Its hidden
    foreign key gives nothing, with no statement. Its reverse one-to-one is loaded by Django's
    descriptor through the base manager, whole and with no permission statement, as from any row
    (``rebac.W003`` lists such relations): the member reads no profile and gets message 1's.
    Its reverse foreign key is read through a related manager, by its scope."""
    settings.REBAC_DISPLAY_FIELDS = True
    [part] = list(
        ScopePart.objects.with_actor(MEMBER)
        .filter(pk=1)
        .prefetch_related(
            Prefetch("message", queryset=ScopeMessage.objects.with_actor(MEMBER).for_display())
        )
    )
    shown = part.message
    whole = ScopeMessage.objects.with_actor(MEMBER).get(pk=1)
    assert display.is_display_row(shown) and not display.is_display_row(whole)
    assert not ScopeProfile.objects.with_actor(MEMBER).exists()
    with CaptureQueriesContext(connection) as from_shown:
        with pytest.raises(ScopeMessage.thread.RelatedObjectDoesNotExist):
            _ = shown.thread
    assert not from_shown.captured_queries
    with CaptureQueriesContext(connection) as from_shown:
        profile = shown.profile
    with CaptureQueriesContext(connection) as from_whole:
        same = whole.profile
    assert (profile.pk, profile.nick) == (same.pk, same.nick) == (1, "one")
    [lazy] = [query["sql"] for query in from_shown.captured_queries]
    assert [query["sql"] for query in from_whole.captured_queries] == [lazy]
    assert "rebac_" not in lazy
    # A refresh names the fields the row shows, so, as any refresh with fields, it keeps the
    # cached reverse one-to-one that a refresh of the whole row forgets.
    with CaptureQueriesContext(connection) as refreshed:
        shown.refresh_from_db()
        assert shown.profile is profile
    assert len(refreshed.captured_queries) == 1
    with CaptureQueriesContext(connection) as from_shown:
        assert [row.pk for row in shown.scopepart_set.all()] == [1]
    assert "rebac_" in from_shown.captured_queries[-1]["sql"]


CODED = """
definition auth/user {}
definition blog/codedfolder {
    relation reader: auth/user
    permission read = reader
}
"""


def test_a_relation_to_another_unique_column_is_read_by_its_scope(settings):
    """A foreign key with ``to_field`` names its target by another column than the primary key: it
    is read by its scope. The member reads folder 2 and not its parent."""
    settings.REBAC_DISPLAY_FIELDS = True
    local = install_schema(CODED)
    CodedFolder._base_manager.bulk_create(
        [CodedFolder(id=1, code="top"), CodedFolder(id=2, code="sub", parent_id="top")]
    )
    local.write_relationships([grant("blog/codedfolder", 2, "reader")])
    display.declare(CodedFolder, "id")
    try:
        [row] = list(
            CodedFolder.objects.with_actor(MEMBER).prefetch_related(
                Prefetch("parent", queryset=CodedFolder.objects.with_actor(MEMBER).for_display())
            )
        )
        assert row.parent_id == "top" and row.parent is None
    finally:
        display.declare(CodedFolder)


def test_structural_and_display_delivery_compose(world, settings):
    """Structural prefetch delivers a readable parent's children; the request delivers labels."""
    settings.REBAC_DISPLAY_FIELDS = True
    settings.REBAC_STRUCTURAL_PREFETCH = True
    install(INHERITING)

    def read_rows() -> list[Any]:
        return list(
            ScopeMessage.objects.with_actor(MEMBER)
            .order_by("pk")
            .prefetch_related(
                Prefetch("thread", queryset=shown_threads()),
                Prefetch("scopepart_set", queryset=ScopePart.objects.with_actor(MEMBER)),
            )
        )

    rows, sqls = statements(read_rows)
    assert threads_of(rows) == DELIVERED
    assert [[p.pk for p in row.scopepart_set.all()] for row in rows] == [[1], [2], [3]]
    assert all("rebac_" not in sql for sql in sqls[1:]) and len(sqls) == 3


def test_display_tracking_delivers_no_child_structurally(world, settings):
    """With display fields on and structural prefetch off, the rows a prefetch admits are
    recorded for display alone: a child structural prefetch would deliver is read by its scope."""
    settings.REBAC_DISPLAY_FIELDS = True
    install(INHERITING)
    _rows, sqls = statements(
        lambda: list(
            ScopeMessage.objects.with_actor(MEMBER).prefetch_related(
                Prefetch("thread", queryset=shown_threads()),
                Prefetch("scopepart_set", queryset=ScopePart.objects.with_actor(MEMBER)),
            )
        )
    )
    # The root, the labels by key, the parts by their scope.
    [_root, labels, parts] = sqls
    assert "rebac_" not in labels and '"testapp_scopepart"' in parts and "rebac_" in parts


# ---------- Everywhere else it falls back to the scoped read ----------

FALLBACKS: dict[str, Callable[[Any], Any]] = {
    "filtered on a shown field": lambda q: q.filter(label__in=["first", "second"]),
    "filtered on the key": lambda q: q.filter(pk__in=[1, 2, 3]),
    "excluded": lambda q: q.exclude(pk=99),
    "annotated": lambda q: q.annotate(copied=F("label")),
    "aliased": lambda q: q.alias(copied=F("label")),
    "distinct": lambda q: q.distinct(),
    "extra": lambda q: q.extra(select={"copied": "label"}),
    "another actor": lambda q: q.with_actor(OTHER),
    "nothing": lambda q: q.none(),
}


@pytest.mark.parametrize("how", sorted(FALLBACKS))
@pytest.mark.parametrize("setting", ["fallback", True])
def test_a_prefetch_the_request_cannot_honour_is_read_by_its_scope(world, settings, setting, how):
    settings.REBAC_DISPLAY_FIELDS = setting
    world.write_relationships([grant("scope/thread", 2, "reader")])
    found = threads_of(messages(FALLBACKS[how](shown_threads())))
    # Only rows the prefetch's actor may read, and whole: the member reads thread 2, the
    # other actor thread 1.
    if how == "nothing":
        expected: list[Any] = [None, None, None]
    elif how == "another actor":
        expected = [(1, "first", False), (1, "first", False), None]
    else:
        expected = [None, None, (2, "second", False)]
    assert found == expected


def test_the_columns_a_caller_chose_are_the_delivery_s_own(world, settings):
    """``only()`` and ``defer()`` choose columns; the delivery reads its own."""
    settings.REBAC_DISPLAY_FIELDS = True
    for chosen in (shown_threads().only("label"), shown_threads().defer("label")):
        assert threads_of(messages(chosen)) == DELIVERED


# What a display queryset may carry besides Django's binding and still be delivered: nothing of
# it changes a condition, a join, a column or the order of the library's read.
CARRIED: dict[str, Callable[[Any], Any]] = {
    "with_action": lambda q: q.with_action("write"),
    "on_field_deny": lambda q: q.on_field_deny("omit"),
    "only": lambda q: q.only("pk"),
    "defer": lambda q: q.defer("label"),
    "reverse": lambda q: q.reverse(),
    "fetch_mode": lambda q: q.fetch_mode(models.FETCH_PEERS),
    "prefetch_related": lambda q: q.prefetch_related("scopemessage_set"),
    "using the parents' database": lambda q: q.using("default"),
}


@pytest.mark.parametrize("how", sorted(CARRIED))
def test_what_a_display_queryset_carries_besides_the_binding(world, settings, how):
    settings.REBAC_DISPLAY_FIELDS = True
    assert threads_of(messages(CARRIED[how](shown_threads()))) == DELIVERED


def test_an_eager_scope_is_read_by_its_scope(world, settings):
    """``scoped()`` scopes the queryset's query when it is built, and its clones keep it: the
    prefetch's evaluation finds its query scoped for its decision and reads by that statement."""
    settings.REBAC_DISPLAY_FIELDS = True
    assert threads_of(messages(shown_threads().scoped())) == [None, None, None]


def test_a_to_many_prefetch_is_read_by_its_scope(world, settings, messages_shown):
    """The other actor reads thread 1 and none of its messages: a to-many prefetch is not a
    reference from the thread, so it delivers no display row of them."""
    settings.REBAC_DISPLAY_FIELDS = True
    threads = list(
        ScopeThread.objects.with_actor(OTHER).prefetch_related(
            Prefetch(
                "scopemessage_set", queryset=ScopeMessage.objects.with_actor(OTHER).for_display()
            )
        )
    )
    assert [(t.pk, list(t.scopemessage_set.all())) for t in threads] == [(1, [])]


def test_parents_read_without_an_actor_scope_admit_nothing(world, settings):
    settings.REBAC_DISPLAY_FIELDS = True
    rows = list(
        ScopeMessage.objects.sudo(reason="test.display")
        .order_by("pk")
        .prefetch_related(Prefetch("thread", queryset=shown_threads()))
    )
    assert threads_of(rows) == [None, None, None, None]


@pytest.mark.parametrize(
    ("relation", "reason"), [("thread", "is a relation"), ("profile", "own columns")]
)
def test_a_relation_cannot_be_a_display_field(world, settings, relation, reason):
    """A foreign key (or the reverse side of a one-to-one) is not a plain column: refused, for
    the prefetched and the joined display rows alike."""
    settings.REBAC_DISPLAY_FIELDS = True
    display.declare(ScopeMessage, "id", relation)
    try:
        with pytest.raises(ImproperlyConfigured, match=reason):
            display.fields(ScopeMessage)
        with pytest.raises(ImproperlyConfigured):
            list(
                ScopePart.objects.with_actor(MEMBER).prefetch_related(
                    Prefetch(
                        "message", queryset=ScopeMessage.objects.with_actor(MEMBER).for_display()
                    )
                )
            )
        with pytest.raises(ImproperlyConfigured):
            list(ScopePart.objects.with_actor(MEMBER).rebac_select_related("message"))
    finally:
        display.declare(ScopeMessage)


def test_a_hidden_foreign_key_of_a_display_row_leads_nowhere(world, settings, messages_shown):
    settings.REBAC_DISPLAY_FIELDS = True

    def parts(*lookups: Any) -> list[Any]:
        return list(
            ScopePart.objects.with_actor(MEMBER)
            .order_by("pk")
            .prefetch_related(
                Prefetch("message", queryset=ScopeMessage.objects.with_actor(MEMBER).for_display()),
                *lookups,
            )
        )

    _rows, alone = statements(parts)
    found, sqls = statements(lambda: parts(Prefetch("message__thread", queryset=shown_threads())))
    # No thread is read at all: every display message's thread key is hidden.
    assert sqls == alone
    assert [(p.message.thread_id, related(p.message, "thread")) for p in found] == [
        (None, None)
    ] * 3


def test_a_display_rows_children_are_read_by_their_scope(world, settings, messages_shown):
    """A display row is not an admitted parent, even where structural prefetch would deliver its
    children: part 5 of message 1 is one the other actor may not read."""
    settings.REBAC_DISPLAY_FIELDS = True
    settings.REBAC_STRUCTURAL_PREFETCH = True
    local = install(INHERITING)
    local.write_relationships([grant("scope/part", 1, "direct", OTHER)])
    ScopePart._base_manager.bulk_create([ScopePart(id=5, message_id=1, fragment_id=5)])
    [part] = list(
        ScopePart.objects.with_actor(OTHER).prefetch_related(
            Prefetch("message", queryset=ScopeMessage.objects.with_actor(OTHER).for_display()),
            Prefetch("message__scopepart_set", queryset=ScopePart.objects.with_actor(OTHER)),
        )
    )
    assert display.is_display_row(part.message)
    assert [child.pk for child in part.message.scopepart_set.all()] == [1]


def test_a_gated_relation_on_the_parent_delivers_nothing(world, settings):
    """The member may not read a message's thread field: its value is not a reference it read,
    whether or not the field mode hides it."""
    settings.REBAC_DISPLAY_FIELDS = True
    install(GATED_THREAD)
    for mode in ("allow", "redact"):
        settings.REBAC_FIELD_READ_MODE = mode
        rows = messages(shown_threads(), to_attr="shown")
        assert [row.shown for row in rows] == [None, None, None]
    assert all(redacted_fields(row) == {"thread"} for row in rows)


# ---------- The condition, forced ----------


def forged(queryset: Any, keys: list[Any]) -> Any:
    """``queryset`` bound as Django binds a message's thread prefetch, to ``keys``."""
    pk = ScopeThread._meta.pk
    relation = ScopeMessage._meta.get_field("thread")
    pairs = ColPairs("testapp_scopethread", [pk], [pk], relation)
    return queryset.filter(TupleIn(pairs, [(key,) for key in keys]))


def admitted(
    actor: SubjectRef = MEMBER, pks: tuple[int, ...] = (1,)
) -> AbstractContextManager[Any]:
    """A running evaluation for ``actor`` that admitted the messages ``pks`` to be read."""
    rows = list(ScopeMessage._base_manager.filter(pk__in=pks))
    return display.referring(actor, ScopeMessage, "read", rows)


def test_a_binding_to_a_referred_key_is_delivered(world, settings):
    settings.REBAC_DISPLAY_FIELDS = True
    with admitted():
        rows = list(forged(shown_threads(), [1]))
    assert [(t.pk, t.label, display.is_display_row(t)) for t in rows] == [(1, "first", True)]


@pytest.mark.parametrize("keys", [[1, 3], [3], [2, 3], [1, 2]])
def test_a_key_no_admitted_row_refers_to_is_never_delivered(world, settings, keys):
    """Message 1 refers to thread 1. A binding that asks for any thread it does not refer to is
    read by its scope, all of it: the member may read none of them."""
    settings.REBAC_DISPLAY_FIELDS = True
    with admitted():
        assert list(forged(shown_threads(), keys)) == []


def test_a_key_forced_into_a_prefetch_is_never_delivered(world, settings, monkeypatch):
    """The prefetch is handed a parent no actor-scoped read admitted, referring to thread 3."""
    settings.REBAC_DISPLAY_FIELDS = True
    original = ForwardManyToOneDescriptor.get_prefetch_querysets

    def with_a_stranger(self: Any, instances: list[Any], querysets: Any = None) -> Any:
        if self.field.model is ScopeMessage:
            instances = [*instances, ScopeMessage(id=4, thread_id=3)]
        return original(self, instances, querysets)

    monkeypatch.setattr(ForwardManyToOneDescriptor, "get_prefetch_querysets", with_a_stranger)
    assert threads_of(messages(shown_threads())) == [None, None, None]


def test_a_binding_through_another_relation_is_not_a_reference(world, settings):
    """Admitted parts refer to message 1, not to thread 1: neither the messages' relation to
    threads nor the parts' relation to messages, laid on the thread table, admits thread 1."""
    settings.REBAC_DISPLAY_FIELDS = True
    pk = ScopeThread._meta.pk
    parts = list(ScopePart._base_manager.filter(pk=1))
    with display.referring(MEMBER, ScopePart, "read", parts):
        assert list(forged(shown_threads(), [1])) == []
        pairs = ColPairs("testapp_scopethread", [pk], [pk], ScopePart._meta.get_field("message"))
        assert list(shown_threads().filter(TupleIn(pairs, [(1,)]))) == []


def test_a_binding_on_another_column_of_the_target_is_not_a_reference(world, settings):
    """Part 1 refers to message 1. A binding that names that relation but another column of the
    messages (their thread key, also 1) would read message 2 as well: it is read by its scope,
    and the other actor reads no message."""
    settings.REBAC_DISPLAY_FIELDS = True
    display.declare(ScopeMessage, "id")
    try:
        column = ScopeMessage._meta.get_field("thread")
        relation = ScopePart._meta.get_field("message")
        pairs = ColPairs("testapp_scopemessage", [column], [column], relation)
        parts = list(ScopePart._base_manager.filter(pk=1))
        with display.referring(OTHER, ScopePart, "read", parts):
            queryset = ScopeMessage.objects.with_actor(OTHER).for_display()
            assert list(queryset.filter(TupleIn(pairs, [(1,)]))) == []
    finally:
        display.declare(ScopeMessage)


FORGED: dict[str, Callable[[Any], Any]] = {
    "values": lambda q: list(q.values("label")),
    "values_list": lambda q: list(q.values_list("label", flat=True)),
    "values of every field": lambda q: list(q.values()),
    "values_list of every field": lambda q: list(q.values_list()),
    "ordered": lambda q: list(q.order_by("label")),
    "sliced": lambda q: list(q[:5]),
    "in_bulk": lambda q: shown_threads().in_bulk([1]) or [],
    "iterator": lambda q: list(q.iterator(chunk_size=10)),
    "count": lambda q: [n for n in (q.count(), int(q.exists())) if n],
    "first": lambda q: [row for row in (q.first(),) if row is not None],
    "filtered": lambda q: list(q.filter(pk=1)),
    "an expression for a key": lambda q: list(forged(shown_threads(), [Value(1)])),
    "a key of another type": lambda q: list(forged(shown_threads(), ["1"])),
}


@pytest.mark.parametrize("how", sorted(FORGED))
def test_a_forged_read_inside_a_prefetch_is_read_by_its_scope(world, settings, how):
    settings.REBAC_DISPLAY_FIELDS = True
    with admitted():
        assert FORGED[how](forged(shown_threads(), [1])) == []


def test_a_queryset_of_another_iterable_is_read_by_its_scope(world, settings):
    """Rows built by an iterable other than Django's own (a third party's) may read more than the
    delivery selects: the request is not honoured for them."""
    from django.db.models.query import ModelIterable

    class Rows(ModelIterable):  # type: ignore[type-arg]
        pass

    settings.REBAC_DISPLAY_FIELDS = True
    queryset = forged(shown_threads(), [1])
    queryset._iterable_class = Rows
    with admitted():
        assert list(queryset) == []


def test_parents_on_another_database_or_of_another_actor_refer_to_nothing(world, settings):
    settings.REBAC_DISPLAY_FIELDS = True
    with admitted(OTHER):
        assert list(forged(shown_threads(), [1])) == []
    elsewhere = list(ScopeMessage._base_manager.filter(pk=1))
    elsewhere[0]._state.db = "elsewhere"
    with display.referring(MEMBER, ScopeMessage, "read", elsewhere):
        assert list(forged(shown_threads(), [1])) == []


def test_a_delivered_queryset_keeps_nothing_of_its_delivery(world, settings):
    """The delivery serves the evaluation that decided it, as a scope does: the rows it read
    stay, and whatever reads the queryset next is decided again, by its permission statement
    (``iterator()``, its query embedded, a clone). The member reads no thread."""
    settings.REBAC_DISPLAY_FIELDS = True
    queryset = forged(shown_threads(), [1])
    with admitted():
        assert [t.pk for t in queryset] == [1]
    assert [t.pk for t in queryset] == [1]
    assert "_rebac_display_keys" not in vars(queryset.query)
    assert list(queryset.iterator(chunk_size=10)) == []
    assert list(ScopeThread._base_manager.filter(pk__in=queryset.query)) == []
    assert list(queryset.all()) == [] and list(queryset.filter(pk=1)) == []


SCOPING_READS: dict[str, Callable[[Any], Any]] = {
    "count": lambda q: q.count(),
    "exists": lambda q: q.exists(),
    "iterator": lambda q: list(q.iterator(chunk_size=10)),
    "aggregate": lambda q: q.aggregate(n=models.Count("pk")),
}


@pytest.mark.parametrize("first", sorted(SCOPING_READS))
def test_a_queryset_an_earlier_read_scoped_is_read_by_that_scope(world, settings, first):
    """``count()``, ``exists()``, ``iterator()`` and ``aggregate()`` leave the queryset's query
    scoped for their decision. Its evaluation under the same decision reuses that query, at the
    cost of one comparison, and is decided neither for display nor for structural delivery: it
    reads by that permission statement, inside a running evaluation too. A clone is decided
    again."""
    settings.REBAC_DISPLAY_FIELDS = True
    queryset = forged(shown_threads(), [1])
    with admitted():
        SCOPING_READS[first](queryset)
        assert list(queryset) == []
        assert shown(queryset.all()) == [(1, True)]


def unpinned_threads() -> Any:
    """Thread 1 asked for as a message's thread prefetch would, pinning no actor: decided by the
    context each evaluation runs in."""
    return forged(ScopeThread.objects.for_display(), [1])


def shown(rows: Any) -> list[Any]:
    return [(t.pk, display.is_display_row(t)) for t in rows]


def test_a_display_read_reused_under_another_actor_is_decided_for_it(world, settings):
    """The running evaluation serves the member alone. The other actor, who reads thread 1, reads
    it by its statement, whole, inside it and after it; the member, after it, reads none."""
    settings.REBAC_DISPLAY_FIELDS = True
    queryset = unpinned_threads()
    with admitted():
        with actor_context(MEMBER):
            assert shown(queryset) == [(1, True)]
        with actor_context(OTHER):
            assert shown(queryset.all()) == [(1, False)]
    with actor_context(OTHER):
        assert shown(queryset.all()) == [(1, False)]
    with actor_context(MEMBER):
        assert shown(queryset.all()) == []


def test_a_display_read_embedded_is_decided_where_it_is_embedded(world, settings):
    """An embedding is never delivered: it is decided for the statement that holds it, inside the
    running evaluation too."""
    settings.REBAC_DISPLAY_FIELDS = True
    everything = ScopeThread.objects.sudo(reason="test.display-embedded")
    with admitted():
        with actor_context(MEMBER):
            inside = list(everything.filter(pk__in=unpinned_threads().values("pk")))
        with actor_context(OTHER):
            other = list(everything.filter(pk__in=unpinned_threads().values("pk")))
    assert ([t.pk for t in inside], [t.pk for t in other]) == ([], [1])


def test_a_display_read_iterated_in_another_context_reads_its_statement(world, settings):
    """``iterator()`` is decided where it is called and never delivered: called inside the running
    evaluation and consumed after it, or the other way round."""
    settings.REBAC_DISPLAY_FIELDS = True
    with actor_context(MEMBER):
        with admitted():
            called_inside = unpinned_threads().iterator()
        assert list(called_inside) == []
        called_outside = unpinned_threads().iterator()
        with admitted():
            assert list(called_outside) == []


def test_a_display_evaluation_that_raised_leaves_no_decision(world, settings, monkeypatch):
    """The library's own read raises once: the queryset holds no rows and no decision, and its next
    evaluation decides again, by its statement outside the running evaluation and as display rows
    inside one."""
    settings.REBAC_DISPLAY_FIELDS = True
    real = display.read
    calls: list[int] = []

    def failing(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("planted")
        return real(*args, **kwargs)

    monkeypatch.setattr(display, "read", failing)
    queryset = forged(shown_threads(), [1])
    with admitted():
        with pytest.raises(RuntimeError, match="planted"):
            list(queryset)
    assert queryset._result_cache is None
    assert list(queryset.all()) == []
    with admitted():
        assert shown(queryset) == [(1, True)]


def test_a_queryset_reused_across_prefetches_decides_for_each(world, settings):
    settings.REBAC_DISPLAY_FIELDS = True
    shared = shown_threads()
    first = list(
        ScopeMessage.objects.with_actor(MEMBER)
        .filter(pk__in=[1, 2])
        .prefetch_related(Prefetch("thread", queryset=shared))
    )
    second = list(
        ScopeMessage.objects.with_actor(MEMBER)
        .filter(pk=3)
        .prefetch_related(Prefetch("thread", queryset=shared))
    )
    assert threads_of(first) == [(1, "first", True), (1, "first", True)]
    assert threads_of(second) == [(2, "second", True)]
    # Evaluated on its own it is a scoped read.
    assert list(shared) == []


def test_a_pickled_request_carries_no_actor(world, settings):
    settings.REBAC_DISPLAY_FIELDS = True
    restored = pickle.loads(pickle.dumps(shown_threads()))
    with pytest.raises(MissingActorError):
        messages(restored)
    assert threads_of(messages(restored.with_actor(MEMBER))) == DELIVERED


@pytest.mark.django_db(transaction=True)
def test_async_reads_deliver_where_the_sync_read_does(settings):
    """``async for`` runs the evaluation, prefetch and all, in one worker; ``aiterator()``'s
    chunks open no running prefetch, so they are read by their scope."""
    seed()
    install()
    display.declare(ScopeThread, "label")
    settings.REBAC_DISPLAY_FIELDS = True
    try:
        queryset = (
            ScopeMessage.objects.with_actor(MEMBER)
            .order_by("pk")
            .prefetch_related(Prefetch("thread", queryset=shown_threads()))
        )

        async def evaluated() -> list[Any]:
            return [row async for row in queryset.all()]

        async def chunked() -> list[Any]:
            return [row async for row in queryset.aiterator(chunk_size=10)]

        assert threads_of(asyncio.run(evaluated())) == DELIVERED
        assert threads_of(asyncio.run(chunked())) == [None, None, None]
    finally:
        display.declare(ScopeThread)


# ---------- What counts as a reference ----------

LAB = """
definition auth/user {}
definition lab/account {
    relation reader: auth/user
    permission read = reader
    permission read__code = nil
    permission read__secret = nil
}
definition lab/publicaccount {
    relation reader: auth/user
    permission read = reader
}
definition lab/note {
    relation viewer: auth/user
    relation peeker: auth/user
    permission read = viewer
    permission peek = peeker + viewer
}
definition lab/badge {
    relation reader: auth/user
    permission read = reader
}
definition lab/place {
    relation reader: auth/user
    permission read = reader
}
definition lab/shop {
    relation reader: auth/user
    permission read = reader
}
definition lab/gatednote {
    relation viewer: auth/user
    permission read = viewer
    permission read__owner = nil
}
"""


@pytest.fixture
def lab(settings):
    """Accounts Alice, Bob and Carol; note 1 is Alice's, note 2 Carol's; Alice holds badge 10 by
    her code. The member reads note 1 and nothing else; an account shows its name."""
    settings.REBAC_DISPLAY_FIELDS = True
    local = install_schema(LAB)
    LabAccount._base_manager.bulk_create(
        [
            LabAccount(id=1, code="alice-code", name="Alice", secret="s1"),
            LabAccount(id=2, code="bob-code", name="Bob", secret="s2"),
            LabAccount(id=3, code="carol-code", name="Carol", secret="s3"),
        ]
    )
    LabNote._base_manager.bulk_create(
        [LabNote(id=1, owner_id=1, title="n1"), LabNote(id=2, owner_id=3, title="n2")]
    )
    LabBadge._base_manager.bulk_create([LabBadge(id=10, holder_id="alice-code", shown="gold")])
    local.write_relationships([grant("lab/note", 1, "viewer")])
    display.declare(LabAccount, "name")
    yield local
    for model in (LabAccount, LabPublicAccount, LabBadge, LabShop):
        display.declare(model)


def owners(notes: Any, accounts: Any = None) -> list[Any]:
    """Each note's owner as ``(pk, name, secret, display row)``, or None where none came."""
    accounts = accounts if accounts is not None else LabAccount.objects.with_actor(MEMBER)
    rows = list(
        notes.order_by("pk").prefetch_related(Prefetch("owner", queryset=accounts.for_display()))
    )
    return [
        None
        if (owner := related(row, "owner")) is None
        else (owner.pk, owner.name, owner.secret, display.is_display_row(owner))
        for row in rows
    ]


LABEL_READ = (
    'SELECT "testapp_labaccount"."id" AS "id", "testapp_labaccount"."name" AS "name" '
    'FROM "testapp_labaccount" WHERE "testapp_labaccount"."id" IN ('
)


def label_read_of(sqls: list[str]) -> str:
    """The delivered read's statement up to its parameters: its exact columns, table and condition."""
    [read_by_key] = [sql for sql in sqls if sql.startswith('SELECT "testapp_labaccount".')]
    return read_by_key[: read_by_key.index(" IN (") + len(" IN (")]


def test_a_note_shows_its_owners_name(lab):
    """The read is the library's own: exactly the key and the display column, by key, nothing
    else (the text is compared on PostgreSQL as well)."""
    found, sqls = statements(lambda: owners(LabNote.objects.with_actor(MEMBER)))
    assert found == [(1, "Alice", None, True)]
    assert label_read_of(sqls) == LABEL_READ


class _Meddling(TrackedQuerySet):  # type: ignore[type-arg]
    """An application's queryset class that changes the rows it builds."""

    def _fetch_all(self) -> None:
        super()._fetch_all()
        for row in self._result_cache or []:
            row.__dict__["meddled"] = True


class _MeddlingManager(models.Manager.from_queryset(_Meddling)):  # type: ignore[misc]
    """A base manager that annotates, selects extra, joins and filters every row away."""

    joined: tuple[str, ...] = ()

    def get_queryset(self) -> Any:
        queryset = (
            super()
            .get_queryset()
            .annotate(secret_shout=Upper("secret"))
            .extra(select={"extra_secret": "secret"})
            .filter(name="nobody")
        )
        return queryset.select_related(*self.joined) if self.joined else queryset


def meddling(monkeypatch: Any, model: type[models.Model], *joined: str) -> None:
    """Give ``model`` the meddling base manager for the test."""
    manager = _MeddlingManager()
    manager.model = model
    manager.name = "meddling"
    manager.joined = joined
    monkeypatch.setitem(model._meta.__dict__, "base_manager", manager)
    assert model._base_manager is manager


def test_the_read_is_the_librarys_own_whatever_the_base_manager_does(lab, monkeypatch):
    """A base manager may annotate, select extra, join, filter or build rows its own way: the
    delivered read is built from a bare queryset, and none of it reaches the statement or the
    rows."""
    _found, plain = statements(lambda: owners(LabNote.objects.with_actor(MEMBER)))
    meddling(monkeypatch, LabAccount)
    rows = list(
        LabNote.objects.with_actor(MEMBER).prefetch_related(
            Prefetch("owner", queryset=LabAccount.objects.with_actor(MEMBER).for_display())
        )
    )
    owner = rows[0].owner
    assert (owner.pk, owner.name, owner.secret) == (1, "Alice", None)
    assert not {"secret_shout", "extra_secret", "meddled"} & owner.__dict__.keys()
    _found, asked = statements(lambda: owners(LabNote.objects.with_actor(MEMBER)))
    assert label_read_of(asked) == label_read_of(plain) == LABEL_READ


def shop_world() -> None:
    """Shop 1, a multi-table row: its name, secret and keeper (account 2) on the parent table,
    its till on its own; note 1 names it."""
    LabPlace._base_manager.bulk_create(
        [LabPlace(id=1, name="corner", secret="p-secret", keeper_id=2)]
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO testapp_labshop (labplace_ptr_id, till) VALUES (%s, %s)", [1, "t-secret"]
        )
    LabNote._base_manager.filter(pk=1).update(shop_id=1)


def note_shops(*lookups: Any) -> Any:
    [note] = list(
        LabNote.objects.with_actor(MEMBER)
        .filter(pk=1)
        .prefetch_related(
            Prefetch("shop", queryset=LabShop.objects.with_actor(MEMBER).for_display()), *lookups
        )
    )
    return note


def test_a_multi_table_target_shows_its_parents_columns_and_hides_its_keeper(lab, monkeypatch):
    """A multi-table target shown by its parent's name: its keeper is hidden, even where its base
    manager would join the keeper, and reads as absent, with no statement to find out."""
    shop_world()
    display.declare(LabShop, "name")
    meddling(monkeypatch, LabShop, "keeper")
    with CaptureQueriesContext(connection) as captured:
        shop = note_shops().shop
    sql = captured.captured_queries[-1]["sql"]
    assert "secret" not in sql and "till" not in sql and "testapp_labaccount" not in sql
    assert display.is_display_row(shop)
    assert (shop.pk, shop.id, shop.name, shop.secret, shop.till) == (1, 1, "corner", None, None)
    assert redacted_fields(shop) == {"secret", "till", "keeper"}
    with CaptureQueriesContext(connection) as followed:
        assert shop.keeper_id is None and shop.keeper is None
    assert not followed.captured_queries


def test_a_multi_table_target_shows_its_parents_key_with_its_own_columns(lab):
    """Shown its own column alone, a shop still holds its parent's key, read with it."""
    shop_world()
    display.declare(LabShop, "till")
    shop = note_shops().shop
    assert (shop.pk, shop.id, shop.till, shop.name) == (1, 1, "t-secret", None)


# ---------- The target's own ordering ----------


def label_statement(sqls: list[str]) -> str:
    [read_by_key] = [sql for sql in sqls if sql.startswith('SELECT "testapp_labaccount".')]
    return read_by_key


@pytest.mark.parametrize("ordering", [["name"], ["-code", "pk"]])
def test_a_targets_own_ordering_does_not_disable_the_delivery(lab, monkeypatch, ordering):
    """The owner is delivered, by the library's read, which orders nothing. Django's prefetch of
    a forward to-one clears the queryset's ordering, the model's default included, before it is
    evaluated, so a target's ``Meta.ordering`` never reached the decision here; the decision no
    longer depends on that."""
    monkeypatch.setattr(LabAccount._meta, "ordering", ordering)
    found, sqls = statements(lambda: owners(LabNote.objects.with_actor(MEMBER)))
    assert found == [(1, "Alice", None, True)]
    assert label_read_of(sqls) == LABEL_READ and "ORDER BY" not in label_statement(sqls)
    assert len(sqls) == 2


def test_a_binding_evaluated_as_it_stands_ignores_the_targets_own_ordering(lab, monkeypatch):
    """Evaluated as it stands, a binding keeps the model's default ordering, which the caller did
    not add and a read by key does not need: delivered. Before, it was read by its scope. An
    ordering the caller asks for still is."""
    monkeypatch.setattr(LabAccount._meta, "ordering", ["name"])
    binding = lab_binding([1])
    assert binding.query.default_ordering and not binding.query.order_by
    with note_one():
        assert [(a.pk, a.name, display.is_display_row(a)) for a in binding] == [(1, "Alice", True)]
        assert list(lab_binding([1]).order_by("name")) == []
        assert list(lab_binding([1]).order_by("-pk")) == []


def test_an_ordering_django_clears_from_the_prefetch_never_reaches_the_decision(lab):
    """Django clears the caller's ``order_by`` from a forward to-one prefetch before evaluating
    it, as one row is prefetched per parent: the owner is delivered, as the scoped read would
    have been unordered too."""
    accounts = LabAccount.objects.with_actor(MEMBER).order_by("-name")
    assert owners(LabNote.objects.with_actor(MEMBER), accounts) == [(1, "Alice", None, True)]


def test_a_targets_ordering_across_a_relation_does_not_disable_the_delivery(lab, monkeypatch):
    """A multi-table shop ordered by its keeper's name, a column of another table: delivered,
    and the library's read joins only its parent table and orders nothing."""
    shop_world()
    display.declare(LabShop, "name")
    monkeypatch.setattr(LabShop._meta, "ordering", ["keeper__name", "till"])
    note_shops()
    with CaptureQueriesContext(connection) as captured:
        shop = note_shops().shop
    sql = captured.captured_queries[-1]["sql"]
    assert display.is_display_row(shop) and (shop.pk, shop.name) == (1, "corner")
    assert "testapp_labaccount" not in sql and "ORDER BY" not in sql
    assert len(captured.captured_queries) == 2
    pk = LabShop._meta.pk
    binding = (
        LabShop.objects.with_actor(MEMBER)
        .for_display()
        .filter(
            TupleIn(
                ColPairs("testapp_labshop", [pk], [pk], LabNote._meta.get_field("shop")), [(1,)]
            )
        )
    )
    assert binding.query.default_ordering
    with display.referring(MEMBER, LabNote, "read", list(LabNote._base_manager.filter(pk=1))):
        assert [(row.pk, display.is_display_row(row)) for row in binding] == [(1, True)]


def test_known_related_objects_do_not_change_what_a_row_refers_to(world, settings):
    """A related manager's queryset sets its instance on every row it builds, after the database
    returned them: here a thread whose key the caller changed to 3. The messages are in thread 1;
    what they refer to is what the database returned."""
    settings.REBAC_DISPLAY_FIELDS = True
    thread = ScopeThread._base_manager.get(pk=1)
    queryset = thread.scopemessage_set.with_actor(MEMBER).order_by("pk")
    thread.pk = 3
    rows = list(
        queryset.prefetch_related(Prefetch("thread", queryset=shown_threads(), to_attr="shown"))
    )
    assert [(row.pk, row.thread_id) for row in rows] == [(1, 3), (2, 3)]
    assert [row.shown for row in rows] == [None, None]


def test_rows_another_actor_reads_inside_this_evaluation_refer_to_nothing(lab):
    """The other actor reads note 2, Carol's, inside the member's evaluation."""
    other = SubjectRef.of("auth/user", "other")
    lab.write_relationships([grant("lab/note", 2, "viewer", other)])
    with display.referring(MEMBER, LabNote, "read", []):
        assert [note.pk for note in LabNote.objects.with_actor(other).filter(pk=2)] == [2]
        assert list(lab_binding([3])) == []


def test_a_gate_on_the_parents_own_type_decides(lab, settings):
    """Read as the proxy type, which gates ``owner``, a note refers to no owner, though the note
    model's own type gates nothing."""
    settings.REBAC_FIELD_READ_MODE = "redact"
    lab.write_relationships([grant("lab/gatednote", 1, "viewer")])
    [note] = list(
        LabGatedNote.objects.with_actor(MEMBER).prefetch_related(
            Prefetch(
                "owner",
                queryset=LabAccount.objects.with_actor(MEMBER).for_display(),
                to_attr="shown",
            )
        )
    )
    assert note.shown is None


class _Fabricating(ModelIterable):  # type: ignore[type-arg]
    """An iterable that builds a row of its own, as if returned by the database: note 9, Bob's."""

    def __iter__(self) -> Any:
        yield from super().__iter__()
        yield LabNote.from_db(self.queryset.db, ["id", "owner_id", "title"], [9, 2, "made up"])


def test_rows_of_another_iterable_refer_to_nothing(lab):
    queryset = LabNote.objects.with_actor(MEMBER).prefetch_related(
        Prefetch(
            "owner", queryset=LabAccount.objects.with_actor(MEMBER).for_display(), to_attr="shown"
        )
    )
    queryset._iterable_class = _Fabricating
    rows = list(queryset)
    assert [row.pk for row in rows] == [1, 9]
    assert [row.shown for row in rows] == [None, None]


def test_a_row_the_database_did_not_build_refers_to_nothing(lab):
    """Built in memory, with the database and state a read would give it: no returned values."""
    built = LabNote(id=9, owner_id=2)
    built._state.db = "default"
    built._state.adding = False
    with display.referring(MEMBER, LabNote, "read", [built]):
        assert list(lab_binding([2])) == []


@pytest.fixture
def elsewhere():
    """A second database holding the label tables alone, under the alias ``elsewhere``."""
    import tempfile
    from pathlib import Path

    from tests.backend_setup import sqlite_alias

    directory = tempfile.TemporaryDirectory()
    alias = "elsewhere"
    connections[alias] = sqlite_alias(alias, Path(directory.name) / "elsewhere.sqlite3")
    try:
        with connections[alias].schema_editor() as editor:
            editor.create_model(LabAccount)
            editor.create_model(LabPlace)
            editor.create_model(LabShop)
            editor.create_model(LabNote)
        yield alias
    finally:
        connections[alias].close()
        del connections[alias]
        directory.cleanup()


@pytest.mark.django_db(databases=["default"])
def test_rows_are_read_where_their_parents_were(lab, elsewhere):
    """Account 1 is Alice here and someone else elsewhere; note 1 elsewhere refers to it there."""
    LabAccount._base_manager.db_manager(elsewhere).bulk_create(
        [LabAccount(id=1, code="c", name="Elsewhere-Alice")]
    )
    LabNote._base_manager.db_manager(elsewhere).bulk_create([LabNote(id=1, owner_id=1)])
    parents = list(LabNote._base_manager.db_manager(elsewhere).filter(pk=1))
    with display.referring(MEMBER, LabNote, "read", parents):
        there = list(lab_binding([1]).using(elsewhere))
        here = list(lab_binding([1]))
    assert [(a.name, a._state.db) for a in there] == [("Elsewhere-Alice", elsewhere)]
    assert here == []


def test_a_thread_started_inside_an_evaluation_refers_to_nothing(lab):
    """The thread runs with a copy of the evaluation's context: the copy neither delivers nor
    admits."""
    carols = list(LabNote._base_manager.filter(pk=2))
    found: dict[str, Any] = {}

    def inside() -> None:
        found["delivered"] = display.deliverable(lab_binding([1]), MEMBER)
        display.admit(MEMBER, LabNote, "read", carols)

    with note_one():
        assert display.deliverable(lab_binding([1]), MEMBER) == [1]
        worker = threading.Thread(target=contextvars.copy_context().run, args=(inside,))
        worker.start()
        worker.join()
        assert found == {"delivered": None}
        assert display.deliverable(lab_binding([3]), MEMBER) is None


def test_a_task_started_inside_an_evaluation_refers_to_nothing(lab):
    """A task copies the context it starts in. A record serves the task that opened it."""
    alices = list(LabNote._base_manager.filter(pk=1))

    async def started_inside() -> Any:
        return display.deliverable(lab_binding([1]), MEMBER)

    async def opened_inside() -> tuple[Any, Any]:
        with display.referring(MEMBER, LabNote, "read", alices):
            mine = display.deliverable(lab_binding([1]), MEMBER)
            theirs = await asyncio.create_task(started_inside())
        return mine, theirs

    with note_one():
        assert asyncio.run(started_inside()) is None
    assert asyncio.run(opened_inside()) == ([1], None)


def test_a_record_serves_nothing_once_its_evaluation_ends(lab):
    """A context copied inside an evaluation and run after it, in the same thread."""
    kept: dict[str, Any] = {}
    with note_one():
        kept["context"] = contextvars.copy_context()
        assert kept["context"].run(lambda: display.deliverable(lab_binding([1]), MEMBER)) == [1]
    assert kept["context"].run(lambda: display.deliverable(lab_binding([1]), MEMBER)) is None


def test_an_evaluation_in_another_thread_adds_nothing_to_the_outer_one(lab):
    """The thread opens an evaluation of its own, with a copy of the outer context: what it admits
    (note 2, Carol's) is its own, and the outer evaluation does not refer to Carol."""
    carols = list(LabNote._base_manager.filter(pk=2))

    def inside() -> None:
        with display.referring(MEMBER, LabNote, "read", carols):
            pass

    with note_one():
        worker = threading.Thread(target=contextvars.copy_context().run, args=(inside,))
        worker.start()
        worker.join()
        assert display.deliverable(lab_binding([3]), MEMBER) is None
        assert display.deliverable(lab_binding([1]), MEMBER) == [1]


def test_code_a_task_hands_back_to_the_evaluations_thread_is_served(lab):
    """asgiref's ``async_to_sync`` runs a coroutine as a task in a thread of its own, and a
    ``sync_to_async`` call in it (thread-sensitive, the default) back in the calling thread,
    outside any task, with a copy of the context it was called with. Called inside an
    evaluation, that code runs in the evaluation's thread with no task and the open record:
    thread and task identity cannot tell it from the evaluation's own code, and the record
    serves it, for the same actor, until the evaluation ends. The task's own code is served
    nothing."""

    async def in_the_task(actor: SubjectRef) -> Any:
        return display.deliverable(lab_binding([1]), actor)

    async def handed_back(actor: SubjectRef) -> Any:
        return await sync_to_async(lambda: display.deliverable(lab_binding([1]), actor))()

    with note_one():
        assert async_to_sync(in_the_task)(MEMBER) is None
        assert async_to_sync(handed_back)(MEMBER) == [1]
        assert async_to_sync(handed_back)(OTHER) is None
    assert async_to_sync(handed_back)(MEMBER) is None


def chunked_owners() -> list[Any]:
    """The member's notes read by ``iterator()``, each owner as ``(pk, display row)`` or None."""
    rows = (
        LabNote.objects.with_actor(MEMBER)
        .order_by("pk")
        .prefetch_related(
            Prefetch("owner", queryset=LabAccount.objects.with_actor(MEMBER).for_display())
        )
        .iterator(chunk_size=10)
    )
    return [
        None
        if (owner := related(row, "owner")) is None
        else (owner.pk, display.is_display_row(owner))
        for row in rows
    ]


def test_an_iterators_chunks_admit_nothing_and_a_running_evaluation_serves_them(lab):
    """``iterator()``'s chunks open no record and admit none of their rows. Outside an evaluation
    their to-one prefetches are read by their scope; inside one for the same actor they are
    delivered for the keys its rows hold, and only those."""
    assert chunked_owners() == [None]
    with note_one():
        assert chunked_owners() == [(1, True)]
    with display.referring(MEMBER, LabNote, "read", []):
        assert chunked_owners() == [None]


def test_a_post_init_receiver_on_a_parent_does_not_change_what_it_refers_to(lab):
    """A receiver rewrites a note's owner to account 2 as the note is built: the note refers to
    what the database returned, account 1, and asks for account 2, which nobody refers to."""

    def rewrite(sender: Any, instance: Any, **kwargs: Any) -> None:
        if "owner_id" in instance.__dict__:
            instance.__dict__["owner_id"] = 2

    post_init.connect(rewrite, sender=LabNote)
    try:
        assert owners(LabNote.objects.with_actor(MEMBER)) == [None]
    finally:
        post_init.disconnect(rewrite, sender=LabNote)


def test_a_multi_table_parent_refers_by_its_inherited_foreign_key(lab):
    """The shop's keeper is a column of its parent's table: read as a shop, it refers to Bob."""
    shop_world()
    lab.write_relationships([grant("lab/shop", 1, "reader")])
    [shop] = list(
        LabShop.objects.with_actor(MEMBER).prefetch_related(
            Prefetch(
                "keeper",
                queryset=LabAccount.objects.with_actor(MEMBER).for_display(),
                to_attr="shown",
            )
        )
    )
    assert (shop.shown.pk, shop.shown.name, display.is_display_row(shop.shown)) == (2, "Bob", True)


def test_model_code_that_runs_as_a_display_row_is_built_sees_no_hidden_value(lab, monkeypatch):
    """An ``__init__`` override and a ``post_init`` receiver that keep a hidden value see None, and
    no statement loads it: nothing is deferred while the row is built."""
    seen: list[tuple[str, Any]] = []
    original = LabAccount.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        seen.append(("init", self.secret))

    def remember(sender: Any, instance: Any, **kwargs: Any) -> None:
        instance._original_secret = instance.secret
        seen.append(("post_init", instance.code))

    monkeypatch.setattr(LabAccount, "__init__", init)
    post_init.connect(remember, sender=LabAccount)
    try:
        _rows, alone = statements(lambda: list(LabNote.objects.with_actor(MEMBER)))
        seen.clear()
        rows, sqls = statements(
            lambda: list(
                LabNote.objects.with_actor(MEMBER).prefetch_related(
                    Prefetch("owner", queryset=LabAccount.objects.with_actor(MEMBER).for_display())
                )
            )
        )
    finally:
        post_init.disconnect(remember, sender=LabAccount)
    owner = rows[0].owner
    assert display.is_display_row(owner) and owner.name == "Alice"
    assert owner._original_secret is None and set(seen) == {("init", None), ("post_init", None)}
    assert len(sqls) == len(alone) + 1
    # What the row was built from: its key and display field; the hidden ones were never values.
    assert owner._rebac_loaded_values == {"id": 1, "name": "Alice"}


def test_an_evaluation_that_delivers_display_rows_offers_them_to_no_record(lab, monkeypatch):
    """``_fetch_all`` offers the rows of an evaluation to the running records (display's and
    structural's) only when a permission statement read them: delivered display rows are offered
    to neither, prefetch below them or not. The notes are offered."""
    offered: list[tuple[str, type[models.Model]]] = []

    def spying(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def spy(actor: Any, model: type[models.Model], *args: Any, **kwargs: Any) -> Any:
            offered.append((name, model))
            return real(actor, model, *args, **kwargs)

        return spy

    for module, name in (
        (display, "admit"),
        (display, "referring"),
        (structural, "admit"),
        (structural, "delivering"),
    ):
        monkeypatch.setattr(
            module, name, spying(f"{module.__name__}.{name}", getattr(module, name))
        )
    accounts = LabAccount.objects.with_actor(MEMBER).prefetch_related("owned_notes")
    assert owners(LabNote.objects.with_actor(MEMBER), accounts) == [(1, "Alice", None, True)]
    assert [entry for entry in offered if entry[1] is LabAccount] == []
    assert ("rebac.display.referring", LabNote) in offered


FOLDERS_COLLECTED = """
definition auth/user {}
definition blog/folder {
    relation editor: auth/user
    permission read = editor
}
definition blog/post {
    relation reader: auth/user
    permission read = reader
}
"""


def test_rows_read_through_a_many_to_many_prefetch_refer_to_nothing(settings):
    """Django builds a many-to-many prefetch with ``extra(select=...)``: its rows are never
    admitted, so their foreign keys are read by their scope."""
    settings.REBAC_DISPLAY_FIELDS = True
    local = install_schema(FOLDERS_COLLECTED)
    Folder._base_manager.bulk_create([Folder(id=1, name="shelf"), Folder(id=2, name="private")])
    Post._base_manager.bulk_create([Post(id=1, title="p", folder_id=2)])
    Post.collections.through._base_manager.bulk_create(
        [Post.collections.through(post_id=1, folder_id=1)]
    )
    local.write_relationships([grant("blog/folder", 1, "editor"), grant("blog/post", 1, "reader")])
    display.declare(Folder, "name")
    try:
        [shelf] = list(
            Folder.objects.with_actor(MEMBER).prefetch_related(
                Prefetch("collected_posts", queryset=Post.objects.with_actor(MEMBER)),
                Prefetch(
                    "collected_posts__folder",
                    queryset=Folder.objects.with_actor(MEMBER).for_display(),
                ),
            )
        )
        [post] = shelf.collected_posts.all()
        assert post.folder_id == 2 and post.folder is None
    finally:
        display.declare(Folder)


@pytest.mark.parametrize("mode", ["redact", "omit"])
def test_a_reverse_one_to_one_to_a_gated_column_shows_nothing_of_it(lab, settings, mode):
    """The badge holds a one-to-one to the account's code, which the member may not read: the
    reverse prefetch is read by its scope, so no badge, and so no code, comes back."""
    settings.REBAC_FIELD_READ_MODE = mode
    lab.write_relationships([grant("lab/account", 1, "reader")])
    display.declare(LabBadge, "shown")
    [account] = list(
        LabAccount.objects.with_actor(MEMBER).prefetch_related(
            Prefetch("badge", queryset=LabBadge.objects.with_actor(MEMBER).for_display())
        )
    )
    assert account.code is None and "code" in redacted_fields(account)
    assert not hasattr(account, "badge")


def test_a_proxy_of_the_target_is_read_by_its_scope(lab):
    """The proxy has a resource type and display fields of its own, and shows the secret its
    target gates. The prefetch's model must be the relation's target itself."""
    display.declare(LabPublicAccount, "name", "secret")
    found = owners(LabNote.objects.with_actor(MEMBER), LabPublicAccount.objects.with_actor(MEMBER))
    assert found == [None]


def test_a_parent_admitted_under_another_action_refers_to_nothing(lab):
    """The member may peek at note 2 and not read it: peeking is not reading."""
    lab.write_relationships([grant("lab/note", 2, "peeker")])
    assert [n.pk for n in LabNote.objects.with_actor(MEMBER)] == [1]
    assert owners(LabNote.objects.with_actor(MEMBER).with_action("peek")) == [None, None]


# ---------- Parents admitted under an action that implies read ----------

# An inbox lists notes under ``read_inbox``, which grants nothing ``read`` does not.
INBOX = LAB.replace(
    "    relation peeker: auth/user\n    permission read = viewer\n    permission peek = peeker + viewer\n}",
    "    relation peeker: auth/user\n    relation muted: auth/user\n    permission read = viewer\n"
    "    permission peek = peeker + viewer\n    permission read_inbox = viewer - muted\n}",
)
# ``read_inbox`` admits peekers too: it no longer implies ``read``.
PEEKING_INBOX = INBOX.replace(
    "permission read_inbox = viewer - muted", "permission read_inbox = (viewer + peeker) - muted"
)
# No reader of a note may read its owner field.
GATED_INBOX = INBOX.replace(
    "    permission read_inbox = viewer - muted\n}",
    "    permission read_inbox = viewer - muted\n    permission read__owner = nil\n}",
)
assert len({LAB, INBOX, PEEKING_INBOX, GATED_INBOX}) == 4


def inbox(schema: str = INBOX) -> LocalBackend:
    """``schema`` over the lab: the member reads note 1 (Alice's) and peeks at note 2 (Carol's)."""
    local = install_schema(schema)
    local.write_relationships([grant("lab/note", 2, "peeker")])
    return local


def inbox_owners(action: str = "read_inbox") -> list[Any]:
    return owners(LabNote.objects.with_actor(MEMBER).with_action(action))


def test_parents_admitted_under_an_action_that_implies_read_refer(lab):
    """``read_inbox`` admits note 1, and the policy proves that whoever holds it reads the note:
    the note's owner is shown, read by key with no permission statement, as under ``read``."""
    inbox()
    assert display.admits_reading(LabNote, "read_inbox")
    assert display.admits_reading(LabNote, "read")
    assert not display.admits_reading(LabNote, "peek")
    for action in ("read", "read_inbox"):
        found, sqls = statements(lambda action=action: inbox_owners(action))
        assert found == [(1, "Alice", None, True)], action
        assert label_read_of(sqls) == LABEL_READ and len(sqls) == 2, action


def test_an_action_that_does_not_imply_read_lends_nothing(lab):
    """``peek``, and a ``read_inbox`` that admits peekers, grant what ``read`` does not: the
    member peeks at note 2 and may not read it. Neither lends a label, whichever note it
    admitted: the owners are read by their scope, and the member reads no account."""
    inbox(PEEKING_INBOX)
    assert not display.admits_reading(LabNote, "read_inbox")
    assert inbox_owners("peek") == [None, None]
    assert inbox_owners("read_inbox") == [None, None]
    assert inbox_owners("read") == [(1, "Alice", None, True)]


def test_the_proof_is_kept_per_policy_and_a_new_policy_proves_again(lab):
    """The proof is structural's own, kept by the policy's key: a policy installed after it is
    proven afresh, both ways."""
    local = inbox()
    assert inbox_owners() == [(1, "Alice", None, True)]
    key = ("implies", compiled._policy(local).key, "lab/note", "read_inbox", "read")
    assert structural._PROOFS.get(key) is True
    local.set_schema(parse_zed(PEEKING_INBOX))
    assert inbox_owners() == [None, None]
    local.set_schema(parse_zed(INBOX))
    assert inbox_owners() == [(1, "Alice", None, True)]


# A note's read is recursive (through its parent note); ``browse`` is the read action itself.
RECURSIVE_READ = LAB.replace(
    "    relation peeker: auth/user\n    permission read = viewer\n",
    "    relation peeker: auth/user\n    relation parent: lab/note\n"
    "    permission read = viewer + parent->read\n    permission browse = read\n",
)
assert RECURSIVE_READ != LAB


def test_no_action_is_proven_to_imply_a_recursive_read(lab):
    """``implies`` never opens a recursive name on the side it proves: even ``browse = read``
    is not proven to imply ``read`` when ``read`` is recursive. Notes the member browses refer to
    nothing; read under ``read`` itself, they refer as ever."""
    install_schema(RECURSIVE_READ)
    assert not display.admits_reading(LabNote, "browse")
    assert [n.pk for n in LabNote.objects.with_actor(MEMBER).with_action("browse")] == [1]
    assert inbox_owners("browse") == [None]
    assert inbox_owners("read") == [(1, "Alice", None, True)]


def stored_inbox() -> SchemaPermission:
    """The lab's notes and accounts stored as schema rows, so that an override can name them."""
    SchemaDefinition.objects.create(resource_type="auth/user")
    account = SchemaDefinition.objects.create(resource_type="lab/account")
    note = SchemaDefinition.objects.create(resource_type="lab/note")
    user = [{"type": "auth/user"}]
    SchemaRelation.objects.create(definition=account, name="reader", allowed_subjects=user)
    SchemaPermission.objects.create(definition=account, name="read", expression="reader")
    for name in ("viewer", "muted"):
        SchemaRelation.objects.create(definition=note, name=name, allowed_subjects=user)
    SchemaPermission.objects.create(definition=note, name="read", expression="viewer")
    return SchemaPermission.objects.create(
        definition=note, name="read_inbox", expression="viewer - muted"
    )


def test_only_the_read_action_counts_while_an_override_is_stored(settings):
    """An override of any kind makes what is evaluated depend on more than the policy's text,
    and the prover reads the text: while one is stored, a note admitted under ``read_inbox``
    lends nothing, and one admitted under ``read`` lends its owner as ever."""
    settings.REBAC_DISPLAY_FIELDS = True
    permission = stored_inbox()
    LabAccount._base_manager.bulk_create([LabAccount(id=1, code="alice-code", name="Alice")])
    LabNote._base_manager.bulk_create([LabNote(id=1, owner_id=1)])
    LocalBackend().write_relationships([grant("lab/note", 1, "viewer")])
    display.declare(LabAccount, "name")
    try:
        assert inbox_owners() == [(1, "Alice", None, True)]
        SchemaOverride.objects.create(
            kind=SchemaOverride.KIND_EXTEND,
            target_ct=ContentType.objects.get_for_model(SchemaPermission),
            target_pk=permission.pk,
            expression="viewer",
            reason="test",
        )
        assert not display.admits_reading(LabNote, "read_inbox")
        assert inbox_owners() == [None]
        assert inbox_owners("read") == [(1, "Alice", None, True)]
    finally:
        display.declare(LabAccount)


@pytest.mark.parametrize(
    ("admitting", "deciding", "expected"),
    [
        (PEEKING_INBOX, INBOX, [None, None]),
        (INBOX, PEEKING_INBOX, [(1, "Alice", None, True)]),
    ],
    ids=["proven only after admission", "proven only at admission"],
)
def test_what_a_parent_was_admitted_to_is_what_the_policy_that_admitted_it_proves(
    lab, monkeypatch, admitting, deciding, expected
):
    """The policy is replaced after the notes are admitted and before the owners' delivery is
    decided. The record holds what the admitting policy proved: decided at the delivery, the
    new policy's proof would show Carol, the owner of a note the member may read under neither
    policy; and a note admitted to be read stays so, as one admitted under ``read`` does."""
    local = inbox(admitting)
    decide = display.deliverable

    def replaced_first(queryset: Any, actor: SubjectRef) -> Any:
        local.set_schema(parse_zed(deciding))
        return decide(queryset, actor)

    monkeypatch.setattr(display, "deliverable", replaced_first)
    assert inbox_owners() == expected


def test_a_policy_replaced_while_the_parents_are_built_did_not_admit_them(lab):
    """The notes' scope pinned the policy that admits peekers; the rows it read are built after
    another policy is installed. What they were admitted to is decided where their scope pinned
    its policy, not where they are recorded: decided there, Carol would be shown."""
    local = inbox(PEEKING_INBOX)

    def replace(sender: Any, instance: Any, **kwargs: Any) -> None:
        local.set_schema(parse_zed(INBOX))

    post_init.connect(replace, sender=LabNote)
    try:
        assert inbox_owners() == [None, None]
    finally:
        post_init.disconnect(replace, sender=LabNote)


def test_the_other_conditions_hold_under_an_implied_action(lab):
    """Admitted under ``read_inbox``, a note still lends nothing through a gated foreign key,
    and a binding to a key no admitted note holds is read by its scope."""
    inbox()
    notes = list(LabNote._base_manager.filter(pk=1))
    with display.referring(MEMBER, LabNote, "read_inbox", notes):
        assert [(a.pk, display.is_display_row(a)) for a in lab_binding([1])] == [(1, True)]
        assert list(lab_binding([1, 3])) == []
    inbox(GATED_INBOX)
    with display.referring(MEMBER, LabNote, "read_inbox", notes):
        assert list(lab_binding([1])) == []
    assert inbox_owners() == [None]


def test_rows_of_a_root_with_extra_columns_refer_to_nothing(lab):
    """``extra(select=...)`` can stand in for any column: here note 1 claims account 2."""
    notes = LabNote.objects.with_actor(MEMBER).extra(select={"owner_id": "%s"}, select_params=[2])
    assert owners(notes) == [None]


def test_rows_of_a_combined_root_refer_to_nothing(lab):
    """A union's rows may come from another table: a plain row claims account 2. Django refuses
    a prefetch on a union, so such rows could only be admitted inside a running evaluation."""
    LabPlain.objects.create(id=99, a=2)
    notes = (
        LabNote.objects.with_actor(MEMBER)
        .only("id", "owner")
        .union(LabPlain.objects.values_list("id", "a"))
    )
    with display.referring(MEMBER, LabNote, "read", []):
        assert sorted((row.pk, row.owner_id) for row in notes) == [(1, 1), (99, 2)]
        assert list(lab_binding([2])) == []


def test_a_field_cannot_be_shadowed_by_an_annotation_or_an_alias(lab):
    """What the documents rely on: Django refuses a name of a field or of its column."""
    notes = LabNote.objects.with_actor(MEMBER)
    for name in ("owner", "owner_id"):
        with pytest.raises(ValueError, match="conflicts with a field"):
            notes.annotate(**{name: Value(2)})
        with pytest.raises(ValueError, match="conflicts with a field"):
            notes.alias(**{name: Value(2)})


def test_a_foreign_key_assigned_after_admission_refers_as_it_was_read(lab):
    """What a row refers to is recorded as it was read: assigning to it later changes nothing."""
    [note] = list(LabNote._base_manager.filter(pk=1))
    with display.referring(MEMBER, LabNote, "read", [note]):
        note.owner_id = 2
        assert list(lab_binding([2])) == []
        assert [a.pk for a in lab_binding([1])] == [1]


def test_a_deferred_foreign_key_is_not_loaded_to_find_a_reference(world, settings):
    """Messages read again with ``only("id")`` in the same evaluation hold no thread key: they
    are not asked for it, so the request adds no statement, where it cost one per message."""
    settings.REBAC_DISPLAY_FIELDS = True
    ScopeProfile._base_manager.bulk_create(
        [ScopeProfile(id=n, owner_id=n, nick=f"p{n}") for n in (2, 3)]
    )
    world.write_relationships([grant("scope/profile", n, "reader") for n in (1, 2, 3)])

    def read_rows(threads: Any) -> list[Any]:
        return list(
            ScopeMessage.objects.with_actor(MEMBER)
            .order_by("pk")
            .prefetch_related(
                Prefetch("profile", queryset=ScopeProfile.objects.with_actor(MEMBER)),
                Prefetch(
                    "profile__owner",
                    queryset=ScopeMessage.objects.with_actor(MEMBER).only("id"),
                    to_attr="owner_only",
                ),
                Prefetch("thread", queryset=threads),
            )
        )

    _rows, plain = statements(lambda: read_rows(ScopeThread.objects.with_actor(MEMBER)))
    rows, asked = statements(lambda: read_rows(shown_threads()))
    assert threads_of(rows) == DELIVERED
    assert len(asked) == len(plain)
    assert not any('WHERE "testapp_scopemessage"."id" = ' in sql for sql in asked)


def test_a_root_with_its_foreign_key_deferred_refers_to_nothing(lab):
    """Admitted with its key deferred, a note refers to nothing: Django loads the key afterwards
    to bind the prefetch, which is then read by its scope, statement for statement as without
    the request."""
    notes = LabNote.objects.with_actor(MEMBER).only("id", "title").order_by("pk")
    plain_accounts = LabAccount.objects.with_actor(MEMBER)
    _rows, plain = statements(
        lambda: list(notes.prefetch_related(Prefetch("owner", queryset=plain_accounts)))
    )
    found, asked = statements(lambda: owners(notes))
    assert found == [None] and asked == plain


def lab_binding(keys: list[Any], relation: Any = None, source: Any = None) -> Any:
    """Accounts bound as Django binds a note's owner prefetch, to ``keys``."""
    pk = LabAccount._meta.pk
    relation = relation if relation is not None else LabNote._meta.get_field("owner")
    pairs = ColPairs("testapp_labaccount", [pk], [source if source is not None else pk], relation)
    return (
        LabAccount.objects.with_actor(MEMBER)
        .for_display()
        .filter(TupleIn(pairs, [(key,) for key in keys]))
    )


def note_one() -> AbstractContextManager[Any]:
    return display.referring(MEMBER, LabNote, "read", list(LabNote._base_manager.filter(pk=1)))


def test_a_binding_lab_note_refers_to_is_delivered(lab):
    with note_one():
        assert [(a.pk, a.name) for a in lab_binding([1])] == [(1, "Alice")]


BENT: dict[str, Callable[[], Any]] = {
    "negated": lambda: (
        LabAccount.objects.with_actor(MEMBER)
        .for_display()
        .exclude(
            TupleIn(
                ColPairs(
                    "testapp_labaccount",
                    [LabAccount._meta.pk],
                    [LabAccount._meta.pk],
                    LabNote._meta.get_field("owner"),
                ),
                [(1,)],
            )
        )
    ),
    "a union of it": lambda: lab_binding([1]).union(LabAccount.objects.with_actor(MEMBER)),
    "a key equal only across types": lambda: lab_binding([True]),
    "another source": lambda: lab_binding([1], source=LabAccount._meta.get_field("code")),
}


@pytest.mark.parametrize("how", sorted(BENT))
def test_a_binding_bent_out_of_shape_is_read_by_its_scope(lab, how):
    """A negated or combined binding asks for other rows than the keys it names; a key of another
    type or prepared by another column is not the key a note holds. The member reads no account."""
    with note_one():
        assert list(BENT[how]()) == []


def test_a_binding_to_a_unique_column_is_read_by_its_scope(lab):
    """With a source that lowercases, ``Top`` would read the row coded ``top``, which nobody
    refers to: a ``to_field`` binding is never the library's to read."""
    CodedFolder._base_manager.bulk_create(
        [
            CodedFolder(id=1, code="Top"),
            CodedFolder(id=2, code="top"),
            CodedFolder(id=3, code="leaf", parent_id="Top"),
        ]
    )
    install_schema(LAB + CODED.replace("definition auth/user {}", "")).write_relationships(
        [grant("blog/codedfolder", 3, "reader")]
    )
    display.declare(CodedFolder, "id", "code")
    try:
        code = CodedFolder._meta.get_field("code")
        pairs = ColPairs(
            "testapp_codedfolder",
            [code],
            [Folder._meta.get_field("kind")],
            CodedFolder._meta.get_field("parent"),
        )
        admitted_rows = list(CodedFolder.objects.with_actor(MEMBER).filter(pk=3))
        with display.referring(MEMBER, CodedFolder, "read", admitted_rows):
            bound = (
                CodedFolder.objects.with_actor(MEMBER)
                .for_display()
                .filter(TupleIn(pairs, [("Top",)]))
            )
            assert list(bound) == []
    finally:
        display.declare(CodedFolder)


def test_rows_another_actor_admitted_refer_to_nothing_for_this_one(lab):
    """An evaluation for another actor inside this one keeps its rows: neither inside its block
    nor after it are they this actor's."""
    other = SubjectRef.of("auth/user", "other")
    lab.write_relationships([grant("lab/note", 1, "viewer", other)])
    theirs = list(LabNote.objects.with_actor(other).filter(pk=1))
    with display.referring(MEMBER, LabNote, "read", []):
        with display.referring(other, LabNote, "read", theirs):
            assert list(lab_binding([1])) == []
        assert list(lab_binding([1])) == []


def test_rows_of_another_model_refer_to_nothing(lab):
    """A profile's one-to-one to message 3 is no note's owner 3."""
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1, label="t")])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=3, thread_id=1)])
    ScopeProfile._base_manager.bulk_create([ScopeProfile(id=1, owner_id=3, nick="p")])
    profiles = list(ScopeProfile._base_manager.all())
    with display.referring(MEMBER, ScopeProfile, "read", profiles):
        assert list(lab_binding([3])) == []


def test_a_model_without_display_fields_is_read_by_its_scope(lab):
    display.declare(LabAccount)
    with note_one():
        assert list(lab_binding([1])) == []


TEXT = """
definition auth/user {}
definition blog/textidentityfolder {
    relation reader: auth/user
    permission read = reader
}
definition blog/textidentitypost {
    relation reader: auth/user
    permission read = reader
}
"""


def test_a_target_whose_identity_is_not_its_key_is_read_by_its_scope(settings):
    """A display row shows its identity; one that is not the key the parent holds is a value the
    reader never held."""
    from django.contrib.auth import get_user_model

    from rebac import RelationshipTuple, to_object_ref
    from tests.testapp.models import TextIdentityFolder, TextIdentityPost

    settings.REBAC_DISPLAY_FIELDS = True
    local = install_schema(TEXT)
    author = get_user_model().objects.create(username="author")
    TextIdentityFolder._base_manager.bulk_create(
        [TextIdentityFolder(id=1, public_id="f-1", name="Inbox", author=author)]
    )
    TextIdentityPost._base_manager.bulk_create(
        [TextIdentityPost(id=1, public_id="p-1", title="t", author=author, folder_id=1)]
    )
    folder = TextIdentityFolder._base_manager.get(pk=1)
    post = TextIdentityPost._base_manager.get(pk=1)
    local.write_relationships([RelationshipTuple(to_object_ref(post), "reader", MEMBER)])
    display.declare(TextIdentityFolder, "name")
    try:
        [row] = list(
            TextIdentityPost.objects.with_actor(MEMBER).prefetch_related(
                Prefetch(
                    "folder", queryset=TextIdentityFolder.objects.with_actor(MEMBER).for_display()
                )
            )
        )
        assert row.folder_id == folder.pk and row.folder is None
    finally:
        display.declare(TextIdentityFolder)


# ---------- Field gates and what a delivered row does ----------


def parts_with_messages(actor: SubjectRef = MEMBER) -> list[Any]:
    return list(
        ScopePart.objects.with_actor(actor)
        .order_by("pk")
        .prefetch_related(
            Prefetch("message", queryset=ScopeMessage.objects.with_actor(actor).for_display())
        )
    )


def test_a_gated_display_field_is_not_shown(lab):
    """The account declares its name and its secret; the policy gates the secret (for everyone):
    the gate decides it and the declaration does not, so it is neither shown nor read."""
    display.declare(LabAccount, "name", "secret")
    assert display.fields(LabAccount) == {"name"}
    found, sqls = statements(lambda: owners(LabNote.objects.with_actor(MEMBER)))
    assert found == [(1, "Alice", None, True)]
    assert label_read_of(sqls) == LABEL_READ


FOLDERS = """
definition auth/user {}
definition blog/folder {
    relation editor: auth/user
    permission read = editor
    permission write = editor
}
definition blog/post {
    relation reader: auth/user
    permission read = reader
}
"""


@pytest.fixture
def folders():
    """Folder 2, "child" of kind "sub" under folder 1, holds post 1, which the member reads."""
    local = install_schema(FOLDERS)
    Folder._base_manager.bulk_create(
        [
            Folder(id=1, name="root", kind="top"),
            Folder(id=2, name="child", kind="sub", parent_id=1),
        ]
    )
    Post._base_manager.bulk_create([Post(id=1, title="post", folder_id=2)])
    local.write_relationships([grant("blog/post", 1, "reader")])
    display.declare(Folder, "name")
    yield local
    display.declare(Folder)


def folder_of_post() -> Any:
    [post] = list(
        Post.objects.with_actor(MEMBER).prefetch_related(
            Prefetch("folder", queryset=Folder.objects.with_actor(MEMBER).for_display())
        )
    )
    return post.folder


def test_a_delivered_row_behaves_as_a_joined_display_row(folders, settings):
    settings.REBAC_DISPLAY_FIELDS = True
    with CaptureQueriesContext(connection) as captured:
        folder = folder_of_post()
    # The hidden columns are not read: the statement selects the key and the name.
    shown = captured.captured_queries[-1]["sql"]
    assert shown.startswith(
        'SELECT "testapp_folder"."id" AS "id", "testapp_folder"."name" AS "name" '
        'FROM "testapp_folder" WHERE'
    )
    assert display.is_display_row(folder) and folder.actor() == MEMBER
    # Hidden fields read as absent and are named as redacted; a hidden relation leads nowhere.
    assert (folder.pk, folder.name, folder.kind, folder.is_active) == (2, "child", None, None)
    assert redacted_fields(folder) == {"kind", "is_active", "parent"}
    assert folder.parent_id is None and folder.parent is None
    # Its reverse relations are read by their scope, as any row's.
    assert [post.pk for post in folder.posts.all()] == [1]
    assert list(folder.children.all()) == []
    # Refreshing reads what the row shows again; the hidden fields stay None.
    folder.refresh_from_db()
    assert (folder.name, folder.kind, folder.parent_id) == ("child", None, None)
    # Saving needs the actor's own write permission, and writes no hidden column.
    folder.name = "renamed"
    with pytest.raises(PermissionDenied):
        folder.save()
    folders.write_relationships([grant("blog/folder", 2, "editor")])
    folder = folder_of_post()
    folder.name = "renamed"
    folder.save()
    stored = Folder._base_manager.get(pk=2)
    assert (stored.name, stored.kind, stored.is_active, stored.parent_id) == (
        "renamed",
        "sub",
        True,
        1,
    )
    # Naming a hidden field in update_fields fails closed, as for a redacted field.
    with pytest.raises(Exception, match="kind"):
        folder.save(update_fields=["kind"])


def test_omit_mode_omits_the_hidden_fields(folders, settings):
    settings.REBAC_DISPLAY_FIELDS = True
    settings.REBAC_FIELD_READ_MODE = "omit"
    folder = folder_of_post()
    assert getattr(folder, "_rebac_omitted_fields", frozenset()) >= {"kind", "is_active", "parent"}


# Every bulk write that takes rows and names their fields, given the rows, the fields and
# folder 1 as the member reads it (for its related manager).
BULK_UPDATES: dict[str, Callable[[list[Any], list[str], Any], Any]] = {
    "bulk_update": lambda rows, fields, _root: Folder.objects.with_actor(MEMBER).bulk_update(
        rows, fields
    ),
    "abulk_update": lambda rows, fields, _root: async_to_sync(
        Folder.objects.with_actor(MEMBER).abulk_update
    )(rows, fields),
    "bulk_update under sudo": lambda rows, fields, _root: Folder.objects.sudo(
        reason="test.hidden-writes"
    ).bulk_update(rows, fields),
    "bulk_update of a related manager": lambda rows, fields, root: root.children.bulk_update(
        rows, fields
    ),
    "bulk_update of the base manager": lambda rows, fields, _root: Folder._base_manager.bulk_update(
        rows, fields
    ),
}
UPSERTS: dict[str, Callable[[list[Any], list[str]], Any]] = {
    "under sudo": lambda rows, fields: Folder.objects.sudo(reason="test.hidden-writes").bulk_create(
        rows, update_conflicts=True, unique_fields=["id"], update_fields=fields
    ),
    "of the base manager": lambda rows, fields: Folder._base_manager.bulk_create(
        rows, update_conflicts=True, unique_fields=["id"], update_fields=fields
    ),
}


def editable_display_folder(folders: LocalBackend) -> Any:
    """Folder 2 as the member was shown it, a display row: its parent is hidden, None on the row
    and 1 in the table. The member edits folders 1 and 2, and reads them."""
    folders.write_relationships([grant("blog/folder", n, "editor") for n in (1, 2)])
    folder = folder_of_post()
    folder.name = "renamed"
    return folder


@pytest.mark.parametrize("how", sorted(BULK_UPDATES))
@pytest.mark.parametrize("hidden", ["parent", "parent_id", "kind"])
def test_a_bulk_update_that_names_a_hidden_field_is_refused_before_any_statement(
    folders, settings, how, hidden
):
    """A hidden field holds None on the row, not the table's value: a write that names one is
    refused, whatever its actor or bypass, before any statement, with the model, the row and the
    field named. Naming only what the row shows writes."""
    settings.REBAC_DISPLAY_FIELDS = True
    folder = editable_display_folder(folders)
    root = Folder.objects.with_actor(MEMBER).get(pk=1)
    name = "parent" if hidden == "parent_id" else hidden
    with CaptureQueriesContext(connection) as captured:
        with pytest.raises(PermissionDenied, match=rf"testapp\.Folder row 2 has {name} hidden"):
            BULK_UPDATES[how]([folder], ["name", hidden], root)
    assert captured.captured_queries == []
    stored = Folder._base_manager.get(pk=2)
    assert (stored.name, stored.kind, stored.parent_id) == ("child", "sub", 1)
    BULK_UPDATES[how]([folder], ["name"], root)
    stored = Folder._base_manager.get(pk=2)
    assert (stored.name, stored.kind, stored.parent_id) == ("renamed", "sub", 1)


# Every form of bulk_create(), given a queryset and the rows.
BULK_CREATES: dict[str, Callable[[Any, Any], Any]] = {
    "insert": lambda queryset, rows: queryset.bulk_create(rows),
    "abulk_create": lambda queryset, rows: async_to_sync(queryset.abulk_create)(rows),
    "ignore_conflicts": lambda queryset, rows: queryset.bulk_create(rows, ignore_conflicts=True),
    "update_conflicts": lambda queryset, rows: queryset.bulk_create(
        rows, update_conflicts=True, unique_fields=["id"], update_fields=["name"]
    ),
}
# The querysets of folders that take rows to insert.
INSERTING: dict[str, Callable[[], Any]] = {
    "an actor": lambda: Folder.objects.with_actor(MEMBER),
    "sudo": lambda: Folder.objects.sudo(reason="test.hidden-writes"),
    "the base manager": lambda: Folder._base_manager.all(),
}


@pytest.mark.parametrize("key", [2, None])
@pytest.mark.parametrize("queryset", sorted(INSERTING))
@pytest.mark.parametrize("form", sorted(BULK_CREATES))
def test_a_display_row_is_not_bulk_created(folders, settings, form, queryset, key):
    """``bulk_create()`` of a list that holds a display row would store its hidden fields' None as
    data, as the copy idiom does: refused before any statement, in every form and through any
    manager, the rows the caller built beside it included."""
    settings.REBAC_DISPLAY_FIELDS = True
    folder = editable_display_folder(folders)
    folder.pk = key
    built = Folder(id=5, name="built", kind="top")
    refused = (
        r"testapp\.Folder row 2 has is_active, kind, parent hidden .*bulk_create\(\) would insert"
    )
    with CaptureQueriesContext(connection) as captured:
        with pytest.raises(PermissionDenied, match=refused):
            BULK_CREATES[form](INSERTING[queryset](), [built, folder])
    assert captured.captured_queries == []
    assert Folder._base_manager.count() == 2
    assert stored_folder() == ("child", "sub", True, 1)


@pytest.mark.parametrize("queryset", ["sudo", "the base manager"])
def test_rows_the_caller_built_are_bulk_created_from_a_generator(folders, queryset):
    """Rows built by the caller carry no hidden field: read once from a generator, inserted."""
    INSERTING[queryset]().bulk_create(Folder(id=n, name=f"built {n}", kind="top") for n in (5, 6))
    assert list(Folder._base_manager.filter(pk__gt=4).values_list("name", flat=True)) == [
        "built 5",
        "built 6",
    ]


@pytest.mark.parametrize("save", ["save", "asave"])
@pytest.mark.parametrize("scope", ["actor", "sudo"])
def test_a_save_that_names_a_hidden_field_is_refused_under_any_scope(
    folders, settings, save, scope
):
    """``save()`` and ``asave()`` read the same record: naming a hidden field is refused, under the
    row's actor and under its own bypass; naming none leaves hidden fields out."""
    settings.REBAC_DISPLAY_FIELDS = True
    folder = editable_display_folder(folders)

    def saved(**kwargs: Any) -> None:
        if scope == "sudo":
            folder.sudo(reason="test.hidden-writes")
        if save == "asave":
            async_to_sync(folder.asave)(**kwargs)
        else:
            folder.save(**kwargs)

    with pytest.raises(PermissionDenied, match="has parent hidden"):
        saved(update_fields=["name", "parent"])
    assert Folder._base_manager.get(pk=2).parent_id == 1
    saved()
    stored = Folder._base_manager.get(pk=2)
    assert (stored.name, stored.kind, stored.parent_id) == ("renamed", "sub", 1)


def stored_folder() -> tuple[Any, ...]:
    row = Folder._base_manager.get(pk=2)
    return (row.name, row.kind, row.is_active, row.parent_id)


# Every way a save that names no fields reaches the table, given the row.
SAVES_NAMING_NO_FIELD: dict[str, Callable[[Any], Any]] = {
    "save": lambda row: row.save(),
    "asave": lambda row: async_to_sync(row.asave)(),
    "save under sudo": lambda row: row.sudo(reason="test.hidden-writes").save(),
    "save after assigning None": lambda row: (setattr(row, "parent", None), row.save()),
    "save after reading the hidden relation": lambda row: (row.parent, row.save()),
    "save_base": lambda row: row.save_base(),
    "save_base raw": lambda row: row.save_base(raw=True),
}


@pytest.mark.parametrize("how", sorted(SAVES_NAMING_NO_FIELD))
@pytest.mark.parametrize(
    "shown", [("name",), ("name", "kind", "is_active")], ids=["name", "all but the parent"]
)
def test_a_save_that_names_no_fields_writes_what_the_row_shows(folders, settings, how, shown):
    """However the save reaches the table, a hidden field that holds None is left out and keeps
    its stored value. ``save_base()`` called directly stored None over it: a NULL parent, or a
    failure on the kind's NOT NULL. A None assigned to a hidden field cannot be told from the
    None it was hidden with, and is left out too."""
    settings.REBAC_DISPLAY_FIELDS = True
    display.declare(Folder, *shown)
    folder = editable_display_folder(folders)
    assert folder.parent_id is None
    SAVES_NAMING_NO_FIELD[how](folder)
    assert stored_folder() == ("renamed", "sub", True, 1)


# A save that would insert a row from the display row, given the key it is given first.
INSERTS: list[tuple[str, Any]] = [
    *((f"save(force_insert=True), key {key}", key) for key in (2, 5, None)),
    *((f"save_base(force_insert=True), key {key}", key) for key in (2, 5)),
    *((f"marked as being added, key {key}", key) for key in (2, 5, None)),
    *((f"save(), key {key}", key) for key in (5, None)),
    *((f"save_base(), key {key}", key) for key in (5, None)),
    *((f"save_base(raw=True), key {key}", key) for key in (5,)),
]


@pytest.mark.parametrize(("how", "key"), INSERTS, ids=[how for how, _key in INSERTS])
def test_a_display_row_is_not_inserted_as_a_new_row(folders, settings, how, key):
    """A save that would insert a row from a display row (forced, a row marked as being added, a
    key other than the one loaded) is refused before any statement: it would store the hidden
    fields' None as data in the new row. The forced insert raised ``ValueError`` or wrote nothing,
    the copy idiom stored the copy, a changed key wrote nothing."""
    settings.REBAC_DISPLAY_FIELDS = True
    display.declare(Folder, "name", "kind", "is_active")
    folder = editable_display_folder(folders).sudo(reason="test.hidden-writes")
    folder.pk = key
    folder._state.adding = how.startswith("marked")
    save: Callable[..., Any] = folder.save_base if how.startswith("save_base") else folder.save
    kwargs = {"force_insert": True} if "force_insert" in how else {}
    if "raw" in how:
        kwargs["raw"] = True
    with CaptureQueriesContext(connection) as captured:
        with pytest.raises(
            PermissionDenied,
            match=r"row 2 has parent hidden .* would insert it as a new row",
        ):
            save(**kwargs)
    assert captured.captured_queries == []
    assert Folder._base_manager.count() == 2
    assert stored_folder() == ("child", "sub", True, 1)


@pytest.mark.parametrize("how", ["save", "asave", "save_base", "save_base raw"])
def test_a_save_of_a_display_row_with_nothing_changed_writes_nothing(folders, settings, how):
    """With no shown field changed and none named, nothing is written and neither ``pre_save``
    nor ``post_save`` is sent: Django's ``save()`` returns so for empty ``update_fields``, and a
    direct ``save_base()``, raw or not, returns so too, where it wrote every column."""
    from django.db.models.signals import post_save, pre_save

    settings.REBAC_DISPLAY_FIELDS = True
    folders.write_relationships([grant("blog/folder", 2, "editor")])
    folder = folder_of_post()
    sent: list[Any] = []

    def received(sender: Any, **kwargs: Any) -> None:
        sent.append(sender)

    pre_save.connect(received, sender=Folder, weak=False)
    post_save.connect(received, sender=Folder, weak=False)
    try:
        with CaptureQueriesContext(connection) as captured:
            SAVES_NAMING_NO_FIELD[how](folder)
    finally:
        pre_save.disconnect(received, sender=Folder)
        post_save.disconnect(received, sender=Folder)
    assert captured.captured_queries == [] and sent == []
    assert stored_folder() == ("child", "sub", True, 1)


def other_folder(folders: LocalBackend) -> Any:
    """Folder 3, which the member edits and reads."""
    Folder._base_manager.bulk_create([Folder(id=3, name="other", kind="top")])
    folders.write_relationships([grant("blog/folder", 3, "editor")])
    return Folder.objects.with_actor(MEMBER).get(pk=3)


def assign_an_object_saved_after(row: Any, _other: Any) -> None:
    """The row's parent is a new folder, saved after it was assigned: the row's column is None
    until Django's save() prepares it from the object."""
    unsaved = Folder(name="new", kind="top")
    row.parent = unsaved
    unsaved.sudo(reason="test.hidden-writes").save()


# A value given to a hidden field of the row.
ASSIGNMENTS: dict[str, Callable[[Any, Any], Any]] = {
    "the relation": lambda row, other: setattr(row, "parent", other),
    "its column": lambda row, other: setattr(row, "parent_id", other.pk),
    "a hidden column": lambda row, _other: setattr(row, "kind", "top"),
    "an object saved after it was assigned": assign_an_object_saved_after,
}


@pytest.mark.parametrize(
    "how",
    sorted(
        set(SAVES_NAMING_NO_FIELD)
        - {"save after assigning None", "save after reading the hidden relation"}
    ),
)
@pytest.mark.parametrize("assignment", sorted(ASSIGNMENTS))
def test_a_save_that_names_no_fields_refuses_a_hidden_field_given_a_value(
    folders, settings, assignment, how
):
    """A save that names no fields writes no hidden field, so one the caller gave a value is
    refused, before any statement, where the save reported success and wrote nothing of it."""
    settings.REBAC_DISPLAY_FIELDS = True
    folder = editable_display_folder(folders)
    ASSIGNMENTS[assignment](folder, other_folder(folders))
    name = "kind" if assignment == "a hidden column" else "parent"
    with CaptureQueriesContext(connection) as captured:
        with pytest.raises(
            PermissionDenied, match=rf"row 2 has {name} hidden .*, and holds a value given to it"
        ):
            SAVES_NAMING_NO_FIELD[how](folder)
    assert captured.captured_queries == []
    assert stored_folder() == ("child", "sub", True, 1)


def test_a_related_managers_add_of_a_display_row_is_refused(folders, settings):
    """``add(bulk=False)`` sets the row's foreign key and saves it naming no fields: the hidden
    foreign key was given a value, and the save refuses it. It reported success and left the row
    under folder 1. (Django's ``add()`` opens its block without a savepoint, so the test opens
    one, as a request's own transaction would end.)"""
    settings.REBAC_DISPLAY_FIELDS = True
    folder = editable_display_folder(folders)
    other = other_folder(folders)
    with pytest.raises(
        PermissionDenied, match=r"has parent hidden .*, and holds a value given to it"
    ):
        with transaction.atomic():
            other.children.add(folder, bulk=False)
    assert stored_folder() == ("child", "sub", True, 1)


def test_a_related_managers_remove_of_a_display_row_raises_djangos_error(folders, settings):
    """``remove()`` compares the row's foreign key with the manager's row: on a display row it
    reads None, so Django raises its own ``DoesNotExist``, with or without ``bulk``, and writes
    nothing."""
    settings.REBAC_DISPLAY_FIELDS = True
    folder = editable_display_folder(folders)
    root = Folder.objects.with_actor(MEMBER).get(pk=1)
    for bulk in (False, True):
        with pytest.raises(Folder.DoesNotExist, match="is not related to"):
            root.children.remove(folder, bulk=bulk)
    assert stored_folder() == ("child", "sub", True, 1)


def test_a_multi_table_display_rows_save_writes_what_it_shows_in_each_table(lab):
    """A shop showing its till: a save that names no fields writes the till, and the parent
    table's name, secret and keeper, hidden, keep their values, whether through ``save()`` or
    ``save_base()``; Django's save of the parent table is part of the same ``save_base``. A
    value given to a hidden field of either table is refused."""
    shop_world()
    display.declare(LabShop, "till")
    for save in ("save", "save_base", "save_base raw"):
        shop = note_shops().shop.sudo(reason="test.hidden-writes")
        assert display.is_display_row(shop) and redacted_fields(shop) == {
            "name",
            "secret",
            "keeper",
        }
        shop.till = f"till by {save}"
        if save == "save":
            shop.save()
        else:
            shop.save_base(raw=save.endswith("raw"))
        place = LabPlace._base_manager.get(pk=1)
        assert (place.name, place.secret, place.keeper_id) == ("corner", "p-secret", 2)
        assert LabShop._base_manager.get(pk=1).till == f"till by {save}"
    for hidden, value in (("secret", "new secret"), ("keeper_id", 3), ("name", "renamed")):
        shop = note_shops().shop.sudo(reason="test.hidden-writes")
        setattr(shop, hidden, value)
        with pytest.raises(PermissionDenied, match="holds a value given to it"):
            shop.save()
    place = LabPlace._base_manager.get(pk=1)
    assert (place.name, place.secret, place.keeper_id) == ("corner", "p-secret", 2)


def test_loaddata_writes_what_its_fixture_gives(folders, settings, tmp_path, monkeypatch):
    """``loaddata`` saves through Django's own ``Model.save_base(raw=True)``, never the model's:
    its rows are built from the fixture, hide nothing, and are written whole."""
    from django.core.management import call_command

    from rebac import RebacMixin

    def never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("loaddata reached the library's save_base")

    settings.REBAC_DISPLAY_FIELDS = True
    fixture = tmp_path / "folders.json"
    fixture.write_text(
        '[{"model": "testapp.folder", "pk": 2, "fields": '
        '{"name": "loaded", "kind": "top", "is_active": false, "parent": null}}]'
    )
    monkeypatch.setattr(RebacMixin, "save_base", never)
    call_command("loaddata", str(fixture), verbosity=0)
    assert stored_folder() == ("loaded", "top", False, None)


def names(*fields: str) -> Any:
    """Field names given as a generator, which can be read once."""
    yield from fields


# Every write that takes the names of the fields it writes, given the rows and the names.
NAMED_WRITES: dict[str, Callable[[list[Any], Any], Any]] = {
    "bulk_update": lambda rows, fields: Folder.objects.with_actor(MEMBER).bulk_update(rows, fields),
    "bulk_update under sudo": lambda rows, fields: Folder.objects.sudo(
        reason="test.hidden-writes"
    ).bulk_update(rows, fields),
    "bulk_update of the base manager": lambda rows, fields: Folder._base_manager.bulk_update(
        rows, fields
    ),
    "save": lambda rows, fields: rows[0].save(update_fields=fields),
    "save_base": lambda rows, fields: rows[0].save_base(update_fields=fields),
}


@pytest.mark.parametrize("how", sorted(NAMED_WRITES))
def test_field_names_given_as_a_generator_are_read_once(folders, settings, how):
    """The guard and Django read the same names: a generator naming a hidden field is refused,
    and one naming what the row shows writes it."""
    settings.REBAC_DISPLAY_FIELDS = True
    folder = editable_display_folder(folders)
    with pytest.raises(PermissionDenied, match="has parent hidden"):
        NAMED_WRITES[how]([folder], names("name", "parent"))
    NAMED_WRITES[how]([folder], names("name"))
    assert stored_folder() == ("renamed", "sub", True, 1)


@pytest.mark.parametrize("how", sorted(UPSERTS))
def test_the_update_fields_of_a_conflicting_bulk_create_are_read_once(folders, settings, how):
    """Given as a generator, the names were read by the guard, and Django then found none and
    raised ``ValueError``; they are read once. A display row in the list is refused as a copy."""
    settings.REBAC_DISPLAY_FIELDS = True
    UPSERTS[how]([Folder(id=2, name="upserted", kind="sub", parent_id=1)], names("name"))
    assert stored_folder() == ("upserted", "sub", True, 1)
    folder = editable_display_folder(folders)
    with pytest.raises(PermissionDenied, match="would insert it as a new row"):
        UPSERTS[how]([folder], names("name", "parent"))
    assert stored_folder() == ("upserted", "sub", True, 1)


def test_update_or_create_reads_its_row_again(folders, settings):
    """``update_or_create`` writes a row it reads itself, by the queryset's scope, never the row a
    value came from: the member reads folder 2 whole, so a value taken from its display row is
    the caller's own value and is written, ``None`` included. A field its fresh read hides is
    refused (``test_field_read_gates``)."""
    settings.REBAC_DISPLAY_FIELDS = True
    folders.write_relationships([grant("blog/folder", 2, "editor")])
    folder = folder_of_post()
    _row, created = Folder.objects.with_actor(MEMBER).update_or_create(
        pk=2, defaults={"parent": folder.parent}
    )
    assert not created and Folder._base_manager.get(pk=2).parent_id is None


def test_refreshing_a_display_row_reads_what_it_shows_alone(folders, settings):
    """The refresh selects the key and the display column, no other; model code that runs as
    Django builds the refreshed values finds no hidden value; the hidden fields stay None; the
    prefetched relations are forgotten as in a refresh of the whole row. Naming a hidden field
    is refused."""
    settings.REBAC_DISPLAY_FIELDS = True
    folder = folder_of_post()
    models.prefetch_related_objects(
        [folder], Prefetch("posts", queryset=Post.objects.with_actor(MEMBER))
    )
    assert "posts" in folder._prefetched_objects_cache
    Folder._base_manager.filter(pk=2).update(name="moved")
    seen: list[Any] = []

    def kept(sender: Any, instance: Any, **kwargs: Any) -> None:
        seen.append({name: instance.__dict__.get(name) for name in ("kind", "parent_id")})

    post_init.connect(kept, sender=Folder)
    try:
        with CaptureQueriesContext(connection) as captured:
            folder.refresh_from_db()
    finally:
        post_init.disconnect(kept, sender=Folder)
    [refresh] = [query["sql"] for query in captured.captured_queries]
    assert refresh.startswith(
        'SELECT "testapp_folder"."id", "testapp_folder"."name" FROM "testapp_folder" WHERE'
    )
    assert seen == [{"kind": None, "parent_id": None}]
    assert (folder.name, folder.kind, folder.parent_id) == ("moved", None, None)
    assert redacted_fields(folder) == {"kind", "is_active", "parent"}
    assert folder._prefetched_objects_cache == {}
    for named in (["kind"], ["name", "parent_id"]):
        with pytest.raises(PermissionDenied, match="refresh_from_db"):
            folder.refresh_from_db(fields=named)
    folder.refresh_from_db(fields=["name"])
    assert folder.name == "moved"


def test_a_display_row_equals_and_hashes_as_its_real_row(folders, settings):
    """Django compares rows by model and key: a display row and the row read whole are one item
    in a set or a dict keyed by instance, whichever was put there first."""
    settings.REBAC_DISPLAY_FIELDS = True
    folder = folder_of_post()
    whole = Folder._base_manager.get(pk=2)
    assert folder == whole and hash(folder) == hash(whole)
    [kept] = {folder, whole}
    assert kept is folder


# ---------- Both storage layouts, random application-shaped policies ----------


def label_read(actor: SubjectRef) -> dict[int, Any]:
    rows = messages(ScopeThread.objects.with_actor(actor).for_display(), actor=actor)
    return {
        row.pk: None if (thread := related(row, "thread")) is None else (thread.pk, thread.label)
        for row in rows
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("policy_seed", seeds(12, fast=3))
def test_delivered_labels_are_exactly_those_of_threads_readable_messages_refer_to(
    settings, storage, policy_seed
):
    """On random policies, the request delivers the threads of the messages the actor reads, with
    their labels; with the setting off the same read delivers the readable threads, whole."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    schema, tuples, labels = application_shaped(policy_seed)
    local = install_schema(schema)
    seed()
    label_threads(labels)
    if tuples:
        local.write_relationships(tuples)
    display.declare(ScopeThread, "label")
    try:
        stored = dict(ScopeThread._base_manager.values_list("pk", "label"))
        threads = dict(ScopeMessage._base_manager.values_list("pk", "thread_id"))
        for actor in (SubjectRef.of("auth/user", "admin"), MEMBER):
            settings.REBAC_DISPLAY_FIELDS = False
            scoped = label_read(actor)
            readable = set(ScopeThread.objects.with_actor(actor).values_list("pk", flat=True))
            settings.REBAC_DISPLAY_FIELDS = True
            shown = label_read(actor)
            assert shown.keys() == scoped.keys(), policy_seed
            assert shown == {pk: (threads[pk], stored[threads[pk]]) for pk in shown}, policy_seed
            assert scoped == {
                pk: (threads[pk], stored[threads[pk]]) if threads[pk] in readable else None
                for pk in scoped
            }, policy_seed
    finally:
        display.declare(ScopeThread)


# ---------- What the mechanism changes, stated ----------


def part_view(actor: SubjectRef) -> dict[int, Any]:
    """Each part's message as the read delivered it: every concrete field, or None."""
    names = [field.attname for field in ScopeMessage._meta.concrete_fields]
    return {
        part.pk: None
        if (message := related(part, "message")) is None
        else {name: getattr(message, name) for name in names}
        for part in parts_with_messages(actor)
    }


def test_the_mechanisms_feature_loss_is_stated(world, settings, messages_shown):
    """Against the scoped read the mechanism changes two things, and only those: a message the
    actor may not read is shown by its key where it was absent, and one it may read loses its
    hidden fields. ``compare_mechanisms`` counts them as two items lost and two gained, and two
    keys ``changed``; the test names them."""
    world.write_relationships(
        [grant("scope/message", 1, "direct", OTHER)]
        + [grant("scope/part", n, "direct", OTHER) for n in (1, 2)]
    )
    report = compare_mechanisms(
        lambda: part_view(OTHER), {"REBAC_DISPLAY_FIELDS": (False, True)}, repeats=1
    )
    assert [row["result"] for row in report] == ["baseline", "differs"]
    assert (report[1]["lost"], report[1]["gained"], report[1]["changed"]) == (2, 2, 2)
    assert report[1]["sql_kb"] < report[0]["sql_kb"]
    settings.REBAC_DISPLAY_FIELDS = False
    before = part_view(OTHER)
    settings.REBAC_DISPLAY_FIELDS = True
    after = part_view(OTHER)
    assert before == {1: {"id": 1, "thread_id": 1}, 2: None}
    assert after == {1: {"id": 1, "thread_id": None}, 2: {"id": 2, "thread_id": None}}


def test_compare_mechanisms_compares_rows_by_what_they_loaded(world, messages_shown):
    """A run that returns the rows themselves: message 1, read whole without the mechanism and as
    a display row with it, is one key ``changed``, though Django finds the two rows equal."""
    world.write_relationships(
        [grant("scope/message", 1, "direct", OTHER), grant("scope/part", 1, "direct", OTHER)]
    )

    def rows() -> dict[int, Any]:
        return {part.pk: related(part, "message") for part in parts_with_messages(OTHER)}

    report = compare_mechanisms(rows, {"REBAC_DISPLAY_FIELDS": (False, True)}, repeats=1)
    assert [row["result"] for row in report] == ["baseline", "differs"]
    assert (report[1]["lost"], report[1]["gained"], report[1]["changed"]) == (1, 1, 1)
    report = compare_mechanisms(rows, {"REBAC_DISPLAY_FIELDS": (False, False)}, repeats=1)
    assert [row["result"] for row in report] == ["baseline", "same"]
