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
    user = get_user_model().objects.create_user(username="staff", is_staff=True, is_active=True)
    subject = SubjectRef.of("auth/user", str(user.pk))
    assert live_backend.has_access(
        subject=subject, action="access", resource=ObjectRef("test/role", "admin")
    )
    assert list(
        live_backend.lookup_subjects(
            resource=ObjectRef("test/role", "admin"),
            action="member",
            subject_type="auth/user",
        )
    ) == [subject]
    assert set(
        live_backend.accessible(subject=subject, action="access", resource_type="test/role")
    ) == {"admin"}

    live_backend.write_relationships(
        [RelationshipTuple(ObjectRef("test/role", "editor"), "member", subject)]
    )
    assert live_backend.has_access(
        subject=subject, action="access", resource=ObjectRef("test/role", "editor")
    )
    assert set(
        live_backend.accessible(subject=subject, action="access", resource_type="test/role")
    ) == {"admin", "editor"}
    assert list(
        live_backend.lookup_subjects(
            resource=ObjectRef("test/role", "editor"),
            action="member",
            subject_type="auth/user",
        )
    ) == [subject]
    with pytest.raises(SchemaError, match="attribute-backed"):
        live_backend.write_relationships(
            [RelationshipTuple(ObjectRef("test/role", "admin"), "member", subject)]
        )
    with pytest.raises(SchemaError, match="attribute-backed"):
        live_backend.delete_relationships(
            RelationshipFilter(resource_type="test/role", resource_id="admin", relation="member")
        )


def test_subject_set_dispatch_accepts_a_permission_name(live_backend):
    user = get_user_model().objects.create_user(username="reader")
    subject = SubjectRef.of("auth/user", str(user.pk))
    live_backend.write_relationships(
        [
            RelationshipTuple(ObjectRef("auth/user", "set"), "self", subject),
            RelationshipTuple(
                ObjectRef("test/doc", "one"),
                "reader",
                SubjectRef.of("auth/user", "set", "effective_member"),
            ),
        ]
    )
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
        assert evaluator.check(backend, subject=subject, action="access", resource=resource).allowed
        get_user_model().objects.filter(pk=user.pk).update(is_staff=False)
        assert not evaluator.check(
            backend, subject=subject, action="access", resource=resource
        ).allowed


CACHE_SCHEMA = (
    SCHEMA
    + """
definition test/static {
    relation viewer: auth/user
    permission read = viewer
}
definition blog/post {
    relation role: test/role // rebac:const=admin
    permission read = role->access
}
"""
)


@pytest.mark.django_db(transaction=True)
def test_types_unreachable_from_live_backing_keep_caching_decisions():
    backend = LocalBackend()
    backend.set_schema(parse_zed(CACHE_SCHEMA))
    user = get_user_model().objects.create_user(username="cache-static")
    subject = SubjectRef.of("auth/user", str(user.pk))
    resource = ObjectRef("test/static", "one")
    backend.write_relationships([RelationshipTuple(resource, "viewer", subject)])

    with evaluator_scope() as evaluator:
        assert evaluator.check(backend, subject=subject, action="read", resource=resource).allowed
        assert evaluator.accessible(
            backend, subject=subject, action="read", resource_type="test/static"
        ) == ("one",)
        assert evaluator.stats()["check_entries"] == 1
        assert evaluator.stats()["accessible_entries"] == 1


@pytest.mark.django_db(transaction=True)
def test_types_reaching_live_backing_through_const_targets_bypass_caching():
    backend = LocalBackend()
    backend.set_schema(parse_zed(CACHE_SCHEMA))
    user = get_user_model().objects.create_user(
        username="cache-banner", is_staff=True, is_active=True
    )
    subject = SubjectRef.of("auth/user", str(user.pk))
    resource = ObjectRef("blog/post", "top")

    with evaluator_scope() as evaluator:
        assert evaluator.check(backend, subject=subject, action="read", resource=resource).allowed
        assert evaluator.accessible(
            backend, subject=subject, action="access", resource_type="test/role"
        ) == ("admin",)
        get_user_model().objects.filter(pk=user.pk).update(is_staff=False)
        assert not evaluator.check(
            backend, subject=subject, action="read", resource=resource
        ).allowed
        assert (
            evaluator.accessible(
                backend, subject=subject, action="access", resource_type="test/role"
            )
            == ()
        )
        assert evaluator.stats()["check_entries"] == 0
        assert evaluator.stats()["accessible_entries"] == 0


@pytest.mark.django_db(transaction=True)
def test_live_type_set_follows_schema_replacement():
    backend = LocalBackend()
    backend.set_schema(parse_zed(CACHE_SCHEMA))
    assert backend._cache_generation("test/static") is not None
    assert backend._cache_generation("test/role") is None

    backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition test/role {
                relation member: auth/user
                permission access = member
            }
            """
        )
    )

    assert backend._cache_generation("test/role") is not None
