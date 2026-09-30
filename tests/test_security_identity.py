"""Identity collisions, override references and module-level admission."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.utils import timezone

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    backend,
    grant_subject_ref,
    rebac_subject,
)
from rebac.backends import reset_backend
from rebac.backends.auth import RebacBackend
from rebac.errors import SchemaError
from rebac.models import SchemaDefinition, SchemaOverride, SchemaPermission, SchemaRelation
from rebac.schema import parse_zed
from tests.backend_setup import install_schema


@rebac_subject(type="agents/agent", id_attr="slug")
class _Agent:
    def __init__(self, slug: str) -> None:
        self.slug = slug


@rebac_subject(type="agents/bot", id_attr="slug")
class _Bot:
    def __init__(self, slug: str) -> None:
        self.slug = slug


def test_grant_subject_ref_distinguishes_agent_types():
    user = SubjectRef.of("auth/user", "1")

    assert grant_subject_ref(_Agent("7"), user) != grant_subject_ref(_Bot("7"), user)


def test_grant_subject_ref_wire_id_is_fixed_and_bounded():
    ref = grant_subject_ref(_Agent("7"), SubjectRef.of("auth/user", "1"))
    assert ref.subject_id == "v2_W69a_dKLXdo1XsDJfYhI-_lTs4sswu0zDcuWVCUHBTc"
    assert ref.optional_relation == "valid"
    long_ref = grant_subject_ref(_Agent("7"), SubjectRef.of("auth/user", "a" * 100))
    assert len(long_ref.subject_id) == 46
    assert "." not in long_ref.subject_id


def test_grant_subject_ref_distinguishes_principal_types():
    agent = SubjectRef.of("agents/agent", "7")

    assert grant_subject_ref(agent, SubjectRef.of("auth/user", "1")) != grant_subject_ref(
        agent, SubjectRef.of("auth/apikey", "1")
    )


def test_grant_subject_ref_distinguishes_dot_split():
    left = grant_subject_ref(SubjectRef.of("agents/agent", "c"), SubjectRef.of("auth/user", "a.b"))
    right = grant_subject_ref(SubjectRef.of("agents/agent", "b.c"), SubjectRef.of("auth/user", "a"))

    assert left != right


def _seed_read_permission() -> SchemaPermission:
    SchemaDefinition.objects.create(resource_type="auth/user")
    definition = SchemaDefinition.objects.create(resource_type="blog/post")
    for name in ("viewer", "banned"):
        SchemaRelation.objects.create(
            definition=definition, name=name, allowed_subjects=[{"type": "auth/user"}]
        )
    return SchemaPermission.objects.create(definition=definition, name="read", expression="viewer")


@pytest.mark.django_db
def test_override_on_defined_relation_takes_effect():
    permission = _seed_read_permission()
    alice = SubjectRef.of("auth/user", "alice")
    post = ObjectRef("blog/post", "p1")
    reset_backend()
    backend().write_relationships(
        [RelationshipTuple(post, "viewer", alice), RelationshipTuple(post, "banned", alice)]
    )
    assert backend().has_access(subject=alice, action="read", resource=post)

    SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_DISABLE,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=permission.pk,
        expression="banned",
        reason="ban",
    )

    assert not backend().has_access(subject=alice, action="read", resource=post)


@pytest.mark.django_db
def test_override_naming_undefined_relation_is_refused():
    permission = _seed_read_permission()

    with pytest.raises((SchemaError, ValidationError)):
        SchemaOverride.objects.create(
            kind=SchemaOverride.KIND_DISABLE,
            target_ct=ContentType.objects.get_for_model(SchemaPermission),
            target_pk=permission.pk,
            expression="blocked",
            reason="ban, with a typo",
        )


MODULE_SCHEMA = """
use expiration
caveat enabled(value bool) { value }
definition auth/user {}
definition auth/group {
    relation member: auth/user
}
definition blog/post {
    relation viewer: auth/user | auth/group#member | auth/user with enabled | auth/user with expiration
    relation banned: auth/user
    permission read = viewer - banned
    permission write = viewer - banned
    permission delete = viewer - banned
    permission create = viewer - banned
}
"""


@pytest.fixture
def module_user(db):
    reset_backend()
    install_schema(backend(), parse_zed(MODULE_SCHEMA))
    yield get_user_model().objects.create_user(username="alice", password="x")
    reset_backend()


def _row(user, relation="viewer", **kwargs) -> RelationshipTuple:
    return RelationshipTuple(
        ObjectRef("blog/post", "p1"),
        relation,
        SubjectRef.of("auth/user", str(user.pk)),
        **kwargs,
    )


def test_has_module_perms_with_a_live_grant(module_user):
    assert not RebacBackend().has_module_perms(module_user, "testapp")
    backend().write_relationships([_row(module_user)])

    assert RebacBackend().has_module_perms(module_user, "testapp")


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"expires_at": timezone.now() - timedelta(minutes=1)}, id="expired"),
        pytest.param(
            {"caveat_name": "enabled", "caveat_context": {"value": False}}, id="caveat-false"
        ),
        pytest.param({"caveat_name": "enabled"}, id="caveat-missing"),
        pytest.param({"relation": "banned"}, id="banned"),
    ],
)
def test_has_module_perms_without_an_effective_grant(module_user, kwargs):
    backend().write_relationships([_row(module_user, **kwargs)])
    subject = SubjectRef.of("auth/user", str(module_user.pk))
    assert not backend().has_access(
        subject=subject, action="read", resource=ObjectRef("blog/post", "p1")
    )
    assert not list(backend().accessible(subject=subject, action="read", resource_type="blog/post"))

    assert not RebacBackend().has_module_perms(module_user, "testapp")


def test_has_module_perms_follows_group_membership(module_user):
    user = SubjectRef.of("auth/user", str(module_user.pk))
    group = ObjectRef("auth/group", "editors")
    backend().write_relationships(
        [
            RelationshipTuple(group, "member", user),
            RelationshipTuple(ObjectRef("blog/post", "p1"), "viewer", SubjectRef(group, "member")),
        ]
    )
    assert RebacBackend().has_module_perms(module_user, "testapp")
