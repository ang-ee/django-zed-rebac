"""Structural delivery under an implied action (proposal 0022) keeps the scope rule.

A queryset's scope is decided at every evaluation, and a delivery serves the one evaluation that
reads its rows inside the running prefetch (``tests/test_security_scope_decision.py``). Here the
same cases with the parents admitted under an action the policy proves implies the arm's, and
the policy a delivery is decided under: a parent's admission proves something only under the
policy its own scope was built under, which an eager ``scoped()`` queryset keeps.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.db import connection
from django.db.models import Exists, OuterRef, Prefetch
from django.test.utils import CaptureQueriesContext

from rebac import actor_context, structural
from rebac.testing import install_schema
from tests.test_security_failed_scope import LAYOUTS, MEMBER, OUTSIDER, install
from tests.testapp.models import ScopeMessage, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]

# The thread's ``inbox`` is its ``read``: the prover proves ``inbox => read``, so messages, whose
# arm is ``thread->read``, are delivered below threads admitted under ``inbox``.
IMPLIED = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
    permission inbox = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    permission read = thread->read
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
}
"""


def message_statements(sqls: list[str]) -> list[str]:
    return [sql for sql in sqls if 'FROM "testapp_scopemessage"' in sql]


def _delivering_a_revoked_parent() -> Any:
    """A running prefetch that admitted thread 1 under ``inbox``, which the member does not
    hold (read a moment ago, say, and revoked since)."""
    [thread] = ScopeThread._base_manager.filter(pk=1)
    return structural.delivering(MEMBER, ScopeThread, "inbox", [thread])


def _children() -> Any:
    """Messages of thread 1, pinning no actor: decided by the context they are read in."""
    return ScopeMessage.objects.filter(thread_id__in=[1])


@pytest.fixture(params=LAYOUTS)
def implied(request, settings):
    """Threads 1-3, message ``n`` in thread ``n``; the member reads thread 2."""
    local = install(settings, schema=IMPLIED, storage=request.param)
    settings.REBAC_STRUCTURAL_PREFETCH = True
    return local


def test_children_are_delivered_below_parents_admitted_under_an_implying_action(implied):
    rows = (
        ScopeThread.objects.with_actor(MEMBER)
        .with_action("inbox")
        .prefetch_related(Prefetch("scopemessage_set", queryset=ScopeMessage.objects.all()))
    )
    with actor_context(MEMBER), CaptureQueriesContext(connection) as captured:
        found = {t.pk: [m.pk for m in t.scopemessage_set.all()] for t in rows}
    assert found == {2: [2]}
    [child] = message_statements([q["sql"] for q in captured.captured_queries])
    assert "rebac_" not in child


def test_a_delivered_read_reused_under_another_actor_is_decided_for_it(implied):
    """Inside the prefetch and after it: the delivery serves the member alone, and the queryset
    keeps no delivered query."""
    rows = _children()
    with _delivering_a_revoked_parent() as delivery:
        with actor_context(MEMBER):
            assert [m.pk for m in rows] == [1]
        assert delivery.delivered == 1
        with actor_context(OUTSIDER):
            assert [m.pk for m in rows.all()] == []
            assert rows.all().count() == 0
    with actor_context(OUTSIDER):
        assert [m.pk for m in rows.all()] == []
    with actor_context(MEMBER):
        assert [m.pk for m in rows.all()] == []


def test_a_delivered_read_embedded_afterwards_is_decided_where_it_is_embedded(implied):
    rows = _children()
    with _delivering_a_revoked_parent():
        with actor_context(MEMBER):
            assert [m.pk for m in rows] == [1]
            everything = ScopeThread.objects.sudo(reason="test.implied-embedded-inside")
            inside = list(everything.annotate(found=Exists(rows)).values_list("found", flat=True))
    with actor_context(MEMBER):
        everything = ScopeThread.objects.sudo(reason="test.implied-embedded")
        after = list(everything.annotate(found=Exists(rows)).values_list("found", flat=True))
        correlated = list(
            everything.annotate(
                found=Exists(ScopeMessage.objects.filter(thread_id=OuterRef("pk")))
            ).values_list("pk", "found")
        )
    assert not any(inside) and not any(after)
    assert dict(correlated) == {1: False, 2: True, 3: False}


def test_a_delivered_read_iterated_in_another_context_reads_its_statement(implied):
    """``iterator()`` is decided where it is called, and is never delivered: called inside the
    prefetch and consumed after it, or the other way round."""
    rows = _children()
    with actor_context(MEMBER):
        with _delivering_a_revoked_parent() as delivery:
            called_inside = rows.iterator()
        assert list(called_inside) == []
        called_outside = rows.all().iterator()
        with _delivering_a_revoked_parent() as again:
            assert list(called_outside) == []
    assert (delivery.delivered, again.delivered) == (0, 0)


def test_a_delivered_evaluation_that_raised_leaves_no_decision(implied):
    from rebac import PermissionDenied

    rows = _children().rebac_select_related("thread")
    with actor_context(MEMBER):
        with _delivering_a_revoked_parent():
            with pytest.raises(PermissionDenied):
                list(rows)
        assert rows._result_cache is None
        assert rows.count() == 0
        assert list(rows) == []


# ---------- The policy a delivery is decided under ----------

# Before: the member reads thread 2 under ``reader``. After: ``read`` and ``inbox`` are ``other``,
# which nobody holds, and a message reads ``thread->read`` in both. Under ``exact`` the threads are
# admitted under ``read``; under ``implied`` under ``inbox``, which the policy after proves
# implies ``read``.
BEFORE = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation other: auth/user
    permission read = reader
    permission inbox = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    permission read = thread->read
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
}
"""
AFTER = BEFORE.replace(
    "permission read = reader\n    permission inbox = reader",
    "permission read = other\n    permission inbox = other",
)


@pytest.mark.parametrize("storage", LAYOUTS)
@pytest.mark.parametrize("action", ["read", "inbox"], ids=["exact", "implied"])
def test_parents_scoped_before_a_policy_change_deliver_nothing_after_it(settings, storage, action):
    """An eager scope keeps the statement it was built with. Its threads, admitted under the
    policy before, delivered under the policy after the message of thread 2, which neither the
    policy after (nobody holds ``other``) grants. The children are delivered only under the
    policy the parents' scope was built under: here they are read by their statement."""
    install(settings, schema=BEFORE, storage=storage)
    settings.REBAC_STRUCTURAL_PREFETCH = True
    parents = (
        ScopeThread.objects.with_actor(MEMBER)
        .with_action(action)
        .scoped()
        .prefetch_related(
            Prefetch("scopemessage_set", queryset=ScopeMessage.objects.with_actor(MEMBER))
        )
    )
    install_schema(AFTER)
    with CaptureQueriesContext(connection) as captured:
        found = {t.pk: [m.pk for m in t.scopemessage_set.all()] for t in parents}
    assert found == {2: []}
    [child] = message_statements([q["sql"] for q in captured.captured_queries])
    assert "rebac_" in child


@pytest.mark.parametrize("action", ["read", "inbox"], ids=["exact", "implied"])
def test_under_one_policy_throughout_an_eager_scope_delivers(settings, action):
    install(settings, schema=BEFORE)
    settings.REBAC_STRUCTURAL_PREFETCH = True
    parents = (
        ScopeThread.objects.with_actor(MEMBER)
        .with_action(action)
        .scoped()
        .prefetch_related(
            Prefetch("scopemessage_set", queryset=ScopeMessage.objects.with_actor(MEMBER))
        )
    )
    with CaptureQueriesContext(connection) as captured:
        found = {t.pk: [m.pk for m in t.scopemessage_set.all()] for t in parents}
    assert found == {2: [2]}
    [child] = message_statements([q["sql"] for q in captured.captured_queries])
    assert "rebac_" not in child


def test_parents_admitted_under_another_policy_key_deliver_nothing(implied):
    """The record keeps the parents' primary keys per policy key, under the base's
    ``(model, database, action)``."""
    [thread] = ScopeThread._base_manager.filter(pk=2)
    query = ScopeMessage.objects.filter(thread_id__in=[2]).query
    current = structural.policy_key()

    def delivers() -> bool:
        return structural.delivers(query, ScopeMessage, MEMBER, "read", using="default")

    with structural.delivering(MEMBER, ScopeThread, "inbox", [thread], policy=("other",)) as one:
        assert not delivers()
        assert one.rows == {(ScopeThread, "default", "inbox"): {("other",): {2}}}
    with structural.delivering(MEMBER, ScopeThread, "inbox", [thread]) as two:
        assert delivers()
        assert two.rows == {(ScopeThread, "default", "inbox"): {current: {2}}}


def test_the_scoped_query_carries_the_policy_it_was_built_under(implied, settings):
    """Beside its decision, and only with the setting on; a query whose scope is removed
    carries neither."""
    from rebac.managers import _without_query_scope

    eager = ScopeThread.objects.with_actor(MEMBER).scoped()
    assert eager.query._rebac_scope_policy == structural.policy_key()
    assert "_rebac_scope_policy" not in vars(_without_query_scope(eager.query))
    settings.REBAC_STRUCTURAL_PREFETCH = False
    assert ScopeThread.objects.with_actor(MEMBER).scoped().query._rebac_scope_policy is None


def test_a_part_below_a_delivered_message_is_delivered_under_the_same_policy(implied):
    """Two levels: the messages delivered under the threads are admitted in turn, with the
    key of the policy their own scope was built under."""
    rows = (
        ScopeThread.objects.with_actor(MEMBER)
        .with_action("inbox")
        .rebac_prefetch_related("scopemessage_set__scopepart_set")
    )
    with CaptureQueriesContext(connection) as captured:
        found = {
            t.pk: {m.pk: [p.pk for p in m.scopepart_set.all()] for m in t.scopemessage_set.all()}
            for t in rows
        }
    assert found == {2: {2: [2]}}
    sqls = [q["sql"] for q in captured.captured_queries]
    assert all("rebac_" not in sql for sql in message_statements(sqls))
    assert all("rebac_" not in sql for sql in sqls if 'FROM "testapp_scopepart"' in sql)


# ---------- Only the conditions every row meets bind parents ----------

# A folder's ``see`` is its parent's ``read``: a folder admitted under ``read`` delivers the
# folders whose ``parent`` is it.
BINDINGS = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    permission read = thread->read
}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    permission read = viewer
    permission see = parent->read
}
"""


@pytest.fixture
def bindings(settings):
    """The member reads thread 1 and folder 1. Message ``n`` is in thread ``n``; folder 2 is in
    folder 1, folder 3 in folder 2."""
    from tests.mechanism_policies import grant
    from tests.testapp.models import Folder

    local = install_schema(BINDINGS)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1), ScopeThread(id=2)])
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=1, thread_id=1), ScopeMessage(id=2, thread_id=2)]
    )
    Folder._base_manager.bulk_create([Folder(id=n, name=f"f{n}") for n in (1, 2, 3)])
    Folder._base_manager.filter(pk=2).update(parent_id=1)
    Folder._base_manager.filter(pk=3).update(parent_id=2)
    local.write_relationships(
        [grant("scope/thread", 1, "reader"), grant("blog/folder", 1, "viewer")]
    )
    return local


def test_a_binding_under_or_admits_no_other_row(bindings):
    """``thread_id IN (1) OR pk = 2``, evaluated inside a delivery of thread 1: the condition
    under ``OR`` binds no parent, so the statement decides, and message 2 (thread 2) stays out."""
    from django.db.models import Q

    [thread] = ScopeThread._base_manager.filter(pk=1)
    rows = ScopeMessage.objects.with_actor(MEMBER).filter(Q(thread_id__in=[1]) | Q(pk=2))
    with structural.delivering(MEMBER, ScopeThread, "read", [thread]) as delivery:
        assert sorted(m.pk for m in rows) == [1]
    assert delivery.delivered == 0


def test_a_binding_on_a_joined_row_binds_no_parent(bindings):
    """``parent__parent_id IN (1)`` binds the grandparent, on a joined row: it proves nothing about
    the folder's own parent, so the statement decides, and folder 3 (whose parent, folder 2, the
    member may not read) stays out."""
    from tests.testapp.models import Folder

    [folder] = Folder._base_manager.filter(pk=1)
    rows = Folder.objects.with_actor(MEMBER).with_action("see").filter(parent__parent_id__in=[1])
    with structural.delivering(MEMBER, Folder, "read", [folder]) as delivery:
        assert [f.pk for f in rows] == []
    assert delivery.delivered == 0
    direct = Folder.objects.with_actor(MEMBER).with_action("see").filter(parent_id__in=[1])
    with structural.delivering(MEMBER, Folder, "read", [folder]) as delivery:
        assert [f.pk for f in direct] == [2]
    assert delivery.delivered == 1
