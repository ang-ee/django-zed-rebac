from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from rebac import (
    LocalBackend,
    ObjectRef,
    RelationshipTuple,
    SchemaError,
    SubjectRef,
    evaluator_scope,
)
from rebac.schema import parse_zed
from rebac.types import RelationshipFilter

SCHEMA = """
definition auth/user {
    relation self: auth/user
    permission effective_member = self
}
definition test/role {
    relation member: auth/user // rebac:attribute={"field":"is_staff","resource":"admin","value":true,"filters":{"is_active":true}}
    permission access = member
}
definition test/doc {
    relation reader: auth/user#effective_member
    permission read = reader
}
"""


@pytest.fixture
def live_backend(db):
    backend = LocalBackend()
    backend.set_schema(parse_zed(SCHEMA))
    return backend


def test_fixed_attribute_owns_only_its_anchor(live_backend):
    user = get_user_model().objects.create_user(
        username="staff", is_staff=True, is_active=True
    )
    subject = SubjectRef.of("auth/user", str(user.pk))
    assert live_backend.has_access(
        subject=subject, action="access", resource=ObjectRef("test/role", "admin")
    )
    assert list(live_backend.lookup_subjects(
        resource=ObjectRef("test/role", "admin"),
        action="member",
        subject_type="auth/user",
    )) == [subject]
    assert set(live_backend.accessible(
        subject=subject, action="access", resource_type="test/role"
    )) == {"admin"}

    live_backend.write_relationships([
        RelationshipTuple(ObjectRef("test/role", "editor"), "member", subject)
    ])
    assert live_backend.has_access(
        subject=subject, action="access", resource=ObjectRef("test/role", "editor")
    )
    assert set(
        live_backend.accessible(
            subject=subject, action="access", resource_type="test/role"
        )
    ) == {"admin", "editor"}
    assert list(
        live_backend.lookup_subjects(
            resource=ObjectRef("test/role", "editor"),
            action="member",
            subject_type="auth/user",
        )
    ) == [subject]
    with pytest.raises(SchemaError, match="attribute-backed"):
        live_backend.write_relationships([
            RelationshipTuple(ObjectRef("test/role", "admin"), "member", subject)
        ])
    with pytest.raises(SchemaError, match="attribute-backed"):
        live_backend.delete_relationships(RelationshipFilter(
            resource_type="test/role", resource_id="admin", relation="member"
        ))


def test_subject_set_dispatch_accepts_a_permission_name(live_backend):
    user = get_user_model().objects.create_user(username="reader")
    subject = SubjectRef.of("auth/user", str(user.pk))
    live_backend.write_relationships([
        RelationshipTuple(ObjectRef("auth/user", "set"), "self", subject),
        RelationshipTuple(
            ObjectRef("test/doc", "one"),
            "reader",
            SubjectRef.of("auth/user", "set", "effective_member"),
        ),
    ])
    assert live_backend.has_access(
        subject=subject, action="read", resource=ObjectRef("test/doc", "one")
    )


@pytest.mark.django_db(transaction=True)
def test_live_backings_disable_evaluator_result_caching():
    backend = LocalBackend()
    backend.set_schema(parse_zed(SCHEMA))
    user = get_user_model().objects.create_user(
        username="cache-staff", is_staff=True, is_active=True
    )
    subject = SubjectRef.of("auth/user", str(user.pk))
    resource = ObjectRef("test/role", "admin")

    with evaluator_scope() as evaluator:
        assert evaluator.check(
            backend, subject=subject, action="access", resource=resource
        ).allowed
        get_user_model().objects.filter(pk=user.pk).update(is_staff=False)
        assert not evaluator.check(
            backend, subject=subject, action="access", resource=resource
        ).allowed
