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

GRANT_ID_COLLIDES = (
    "grant_subject_ref joins the bare subject ids with '.' and drops both subject types "
    "(src/rebac/actors.py:291)"
)


@rebac_subject(type="agents/agent", id_attr="slug")
class _Agent:
    def __init__(self, slug: str) -> None:
        self.slug = slug


@rebac_subject(type="agents/bot", id_attr="slug")
class _Bot:
    def __init__(self, slug: str) -> None:
        self.slug = slug


@pytest.mark.xfail(strict=True, reason=GRANT_ID_COLLIDES)
def test_grant_subject_ref_distinguishes_agent_types():
    user = SubjectRef.of("auth/user", "1")

    assert grant_subject_ref(_Agent("7"), user) != grant_subject_ref(_Bot("7"), user)


@pytest.mark.xfail(strict=True, reason=GRANT_ID_COLLIDES)
def test_grant_subject_ref_distinguishes_principal_types():
    agent = SubjectRef.of("agents/agent", "7")

    assert grant_subject_ref(agent, SubjectRef.of("auth/user", "1")) != grant_subject_ref(
        agent, SubjectRef.of("auth/apikey", "1")
    )


@pytest.mark.xfail(strict=True, reason=GRANT_ID_COLLIDES)
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
@pytest.mark.xfail(
    strict=True,
    reason=(
        "compose() never validates override references and _enforced_schema_errors drops "
        "validate_schema's 'undefined reference' error, so the override silently contributes "
        "nothing (src/rebac/composition.py:239, src/rebac/backends/local.py:94)"
    ),
)
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
definition blog/post {
    relation viewer: auth/user | auth/user with enabled | auth/user with expiration
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "has_module_perms admits on any relationship row naming the user, without evaluating "
        "expiry, caveats or exclusions (src/rebac/backends/auth.py:147)"
    ),
)
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
