"""Structural delivery delivers nothing while the policy carries an override (proposal 0020).

The proof reads the policy's text. An override makes what is evaluated depend on more than
the text: a permission override holds until its deadline, a recaveat replaces a caveat's
expression until its own. While any override of any kind is stored, every edge keeps the
children's permission statement, whether the override has a deadline, one ahead or one
passed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from rebac import LocalBackend, ObjectRef, RelationshipTuple, SubjectRef, structural
from rebac.compile import read
from rebac.models import (
    SchemaCaveat,
    SchemaDefinition,
    SchemaOverride,
    SchemaPermission,
    SchemaRelation,
)
from tests.testapp.models import ScopeMessage, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
MEMBER = SubjectRef.of("auth/user", "member")
EDGE = {
    "child_model": ScopeMessage,
    "fk_name": "thread",
    "parent_model": ScopeThread,
    "child_action": "read",
    "parent_action": "read",
}


@pytest.fixture
def stored():
    """``scope/thread`` and ``scope/message`` stored as schema rows, with a caveat, so that an
    override of each kind can name a permission or the caveat: a message's ``read`` is its
    thread's ``read``."""
    SchemaDefinition.objects.create(resource_type="auth/user")
    thread = SchemaDefinition.objects.create(resource_type="scope/thread")
    message = SchemaDefinition.objects.create(resource_type="scope/message")
    SchemaCaveat.objects.create(
        name="enabled", params=[{"name": "ok", "type": "bool"}], expression="ok"
    )
    SchemaRelation.objects.create(
        definition=thread, name="reader", allowed_subjects=[{"type": "auth/user"}]
    )
    SchemaRelation.objects.create(
        definition=thread,
        name="cav",
        allowed_subjects=[{"type": "auth/user", "with_caveat": "enabled"}],
    )
    SchemaPermission.objects.create(definition=thread, name="read", expression="cav + reader")
    SchemaRelation.objects.create(
        definition=message,
        name="thread",
        allowed_subjects=[{"type": "scope/thread"}],
        backing={"kind": "fk", "path": "thread"},
    )
    SchemaPermission.objects.create(definition=message, name="read", expression="thread->read")


def store_override(kind: str, expression: str, expires_at=None) -> None:
    """An override of ``kind``: a recaveat of the caveat, any other of the thread's ``read``."""
    if kind == SchemaOverride.KIND_RECAVEAT:
        target, target_pk = SchemaCaveat, SchemaCaveat.objects.get(name="enabled").pk
    else:
        target = SchemaPermission
        target_pk = SchemaPermission.objects.get(
            definition__resource_type="scope/thread", name="read"
        ).pk
    SchemaOverride.objects.create(
        kind=kind,
        target_ct=ContentType.objects.get_for_model(target),
        target_pk=target_pk,
        expression=expression,
        reason="test",
        expires_at=expires_at,
    )


def proven() -> bool:
    return structural.parent_implies_children(read._policy(LocalBackend()), **EDGE)


@pytest.mark.parametrize(
    ("kind", "expression"),
    [
        (SchemaOverride.KIND_EXTEND, "reader"),
        (SchemaOverride.KIND_LOOSEN, "reader"),
        (SchemaOverride.KIND_DISABLE, "reader"),
        (SchemaOverride.KIND_TIGHTEN, "reader"),
        (SchemaOverride.KIND_RECAVEAT, "true"),
    ],
)
@pytest.mark.parametrize("deadline", [None, timedelta(minutes=5), -timedelta(minutes=5)])
def test_no_edge_is_proven_while_any_override_is_stored(stored, kind, expression, deadline):
    assert proven()
    store_override(kind, expression, timezone.now() + deadline if deadline is not None else None)
    assert not proven()


def test_a_recaveat_override_keeps_the_childrens_statement(stored, settings):
    """A recaveat tags no arm of the composed policy; it still stops delivery."""
    # The thread's ``cav`` relation is caveated: the recaveat replaces a caveat in use.
    policy = read._policy(LocalBackend())
    [cav] = [r for r in policy.schema.get_definition("scope/thread").relations if r.name == "cav"]
    assert [subject.with_caveat for subject in cav.allowed_subjects] == ["enabled"]
    settings.REBAC_STRUCTURAL_PREFETCH = True
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1), ScopeThread(id=2)])
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=n, thread_id=1 if n < 3 else 2) for n in (1, 2, 3)]
    )
    LocalBackend().write_relationships(
        [RelationshipTuple(ObjectRef("scope/thread", "1"), "reader", MEMBER)]
    )

    def child_statement() -> tuple[dict[int, list[int]], str]:
        threads = ScopeThread.objects.with_actor(MEMBER).rebac_prefetch_related("scopemessage_set")
        with CaptureQueriesContext(connection) as captured:
            found = {t.pk: sorted(m.pk for m in t.scopemessage_set.all()) for t in threads}
        sql = [query["sql"] for query in captured.captured_queries]
        return found, next(s for s in sql if 'FROM "testapp_scopemessage"' in s)

    found, child = child_statement()
    assert found == {1: [1, 2]}
    assert "rebac_" not in child
    store_override(SchemaOverride.KIND_RECAVEAT, "true", timezone.now() + timedelta(minutes=5))
    found, child = child_statement()
    assert found == {1: [1, 2]}
    assert "rebac_" in child
