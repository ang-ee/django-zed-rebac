"""``REBAC_ENFORCE_PERMISSIONS = False``: nothing is checked, scoped, guarded or redacted."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import (
    MissingActorError,
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    actor_context,
    require_permission,
)
from rebac.backends import backend
from rebac.checks import check_permissions_enforced
from rebac.field_visibility import redacted_fields
from rebac.testing import install_schema
from tests.testapp.models import ScopeMessage, ScopePart, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
MEMBER = SubjectRef.of("auth/user", "member")
OUTSIDER = SubjectRef.of("auth/user", "outsider")

SCHEMA = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
    permission write = reader
    permission delete = reader
    permission create = reader
    permission read__label = nil
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    permission read = thread->read
    permission create = thread->write
    permission write = thread->write
    permission delete = thread->write
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
}
"""


@pytest.fixture
def world():
    """Two threads, the member reads the first; a message and a part under each."""
    local = install_schema(SCHEMA)
    ScopeThread._base_manager.bulk_create(
        [ScopeThread(id=1, label="one"), ScopeThread(id=2, label="two")]
    )
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=1, thread_id=1), ScopeMessage(id=2, thread_id=2)]
    )
    ScopePart._base_manager.bulk_create(
        [ScopePart(id=n, message_id=n, fragment_id=n) for n in (1, 2)]
    )
    local.write_relationships([RelationshipTuple(ObjectRef("scope/thread", "1"), "reader", MEMBER)])
    return local


def permission_statements(statements: list[str]) -> list[str]:
    return [sql for sql in statements if "rebac_" in sql]


def test_enforced_by_default(world):
    assert list(ScopeThread.objects.with_actor(MEMBER).values_list("pk", flat=True)) == [1]
    assert list(ScopeThread.objects.with_actor(OUTSIDER)) == []
    with pytest.raises(MissingActorError):
        list(ScopeThread.objects.all())
    assert not world.check_access(
        subject=OUTSIDER, action="read", resource=ObjectRef("scope/thread", "1")
    ).allowed
    assert check_permissions_enforced() == []


def test_every_read_is_the_plain_read(world, settings):
    settings.REBAC_ENFORCE_PERMISSIONS = False
    with CaptureQueriesContext(connection) as captured:
        # No actor, an actor that holds nothing, an actor that holds something: the same rows.
        assert sorted(ScopeThread.objects.values_list("pk", flat=True)) == [1, 2]
        assert sorted(ScopeThread.objects.with_actor(OUTSIDER).values_list("pk", flat=True)) == [
            1,
            2,
        ]
        with actor_context(MEMBER):
            assert ScopeMessage.objects.count() == 2
            assert ScopePart.objects.filter(message__thread_id=2).exists()
    statements = [query["sql"] for query in captured.captured_queries]
    assert len(statements) == 4 and not permission_statements(statements)


def test_related_rows_load_without_a_guard_or_a_scope(world, settings):
    settings.REBAC_ENFORCE_PERMISSIONS = False
    with CaptureQueriesContext(connection) as captured:
        parts = list(
            ScopePart.objects.with_actor(OUTSIDER)
            .rebac_select_related("message__thread")
            .order_by("pk")
        )
        threads = list(
            ScopeThread.objects.with_actor(OUTSIDER)
            .order_by("pk")
            .rebac_prefetch_related("scopemessage_set__scopepart_set")
        )
    assert [part.message.thread.pk for part in parts] == [1, 2]
    assert [[m.pk for m in t.scopemessage_set.all()] for t in threads] == [[1], [2]]
    statements = [query["sql"] for query in captured.captured_queries]
    # One joined statement, then a thread statement and one per prefetch level: Django's own.
    assert len(statements) == 4 and not permission_statements(statements)


def test_no_field_is_redacted(world, settings):
    settings.REBAC_FIELD_READ_MODE = "redact"
    thread = ScopeThread.objects.with_actor(MEMBER).get(pk=1)
    assert thread.label is None and redacted_fields(thread) == {"label"}
    settings.REBAC_ENFORCE_PERMISSIONS = False
    thread = ScopeThread.objects.with_actor(MEMBER).get(pk=1)
    assert thread.label == "one" and redacted_fields(thread) == frozenset()


def test_every_check_allows(world, settings):
    settings.REBAC_ENFORCE_PERMISSIONS = False
    local = backend()
    hidden = ObjectRef("scope/thread", "2")
    with CaptureQueriesContext(connection) as captured:
        assert local.check_access(subject=OUTSIDER, action="write", resource=hidden).allowed
        assert local.has_access(subject=OUTSIDER, action="delete", resource=hidden)
        assert local.held(
            subject=OUTSIDER, action="read", resource_type="scope/thread", ids=["1", "2", "9"]
        ) == {"1", "2", "9"}
        assert local.held_many(
            subject=OUTSIDER,
            actions=["read", "write"],
            resource_type="scope/thread",
            ids=["1", "2"],
        ) == {"read": {"1", "2"}, "write": {"1", "2"}}
        assert local.grants_all(subject=OUTSIDER, action="read", resource_type="scope/thread")
        assert ScopeThread.objects.get(pk=2).check_access("delete").allowed
    assert not captured.captured_queries[:-1] or not permission_statements(
        [query["sql"] for query in captured.captured_queries]
    )
    assert sorted(
        local.accessible(subject=OUTSIDER, action="read", resource_type="scope/thread")
    ) == ["1", "2"]
    assert list(ScopeThread._base_manager.filter(local.queryset_filter(
        model=ScopeThread, subject=OUTSIDER, action="read", using="default"
    )).values_list("pk", flat=True).order_by("pk")) == [1, 2]  # fmt: skip


def test_every_write_is_allowed_without_an_actor(world, settings):
    settings.REBAC_ENFORCE_PERMISSIONS = False
    # Explicit keys: the fixture's rows were written with theirs, past the sequences.
    thread = ScopeThread.objects.create(id=3, label="three")
    message = ScopeMessage.objects.create(id=3, thread=thread)
    message.thread_id = 2
    message.save()
    ScopeThread.objects.filter(pk=1).update(label="renamed")
    ScopeMessage.objects.filter(pk=message.pk).delete()
    thread.delete()
    assert ScopeThread._base_manager.get(pk=1).label == "renamed"


def test_a_guarded_function_runs(world, settings):
    @require_permission("write", resource_arg="thread")
    def rename(thread):
        return "renamed"

    hidden = ScopeThread._base_manager.get(pk=2)
    with actor_context(OUTSIDER), pytest.raises(Exception):  # noqa: B017
        rename(hidden)
    settings.REBAC_ENFORCE_PERMISSIONS = False
    assert rename(hidden) == "renamed"


def test_relationships_are_still_stored_and_decide_again_when_enforced(world, settings):
    settings.REBAC_ENFORCE_PERMISSIONS = False
    backend().write_relationships(
        [RelationshipTuple(ObjectRef("scope/thread", "2"), "reader", OUTSIDER)]
    )
    assert sorted(ScopeThread.objects.with_actor(OUTSIDER).values_list("pk", flat=True)) == [1, 2]
    settings.REBAC_ENFORCE_PERMISSIONS = True
    assert list(ScopeThread.objects.with_actor(OUTSIDER).values_list("pk", flat=True)) == [2]


def test_the_system_check_says_so(settings):
    settings.REBAC_ENFORCE_PERMISSIONS = False
    [warning] = check_permissions_enforced()
    assert warning.id == "rebac.W011" and "no permission is checked" in warning.msg
