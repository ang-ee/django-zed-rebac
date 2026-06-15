"""Empty-resource_id (create-gate) checks honour resource-independent grants.

``LocalBackend.check_access`` with an empty ``resource_id`` answers the
model-level question "may this subject act on a *new* row of this type?" — the
gate the ``pre_save`` create signal relies on. A permission built from terms
that don't depend on a concrete row — the built-in ``authenticated`` /
``anonymous`` actors, or a const-backed arrow that resolves to a fixed object
regardless of row id — must grant even though no accessible row exists yet.
Relation-based terms (``owner``) still resolve through the ``accessible()``
fallback; they evaluate ``False`` against the empty id and so never spuriously
grant via the row-independent path.
"""

from __future__ import annotations

import pytest

from rebac import (
    LocalBackend,
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    actor_context,
    backend,
)
from rebac.actors import anonymous_actor
from rebac.backends import reset_backend
from rebac.schema import parse_zed

UNIT_SCHEMA = """
definition auth/user {}

definition auth/role {
    relation member: auth/user
}

definition blog/post {
    relation owner: auth/user
    relation admin: auth/role // rebac:const=superadmin
    permission create_authed = authenticated
    permission create_anon   = anonymous
    permission create_owned  = owner
    permission create_admin  = admin->member
}
"""


@pytest.fixture
def be(db):
    b = LocalBackend()
    b.set_schema(parse_zed(UNIT_SCHEMA))
    return b


def _user(id_: str) -> SubjectRef:
    return SubjectRef.of("auth/user", id_)


def _new_post() -> ObjectRef:
    # Empty resource_id => "a not-yet-persisted row of this type".
    return ObjectRef("blog/post", "")


def _check(be: LocalBackend, *, subject: SubjectRef, action: str) -> bool:
    return be.has_access(subject=subject, action=action, resource=_new_post())


# ---------- built-in actor terms ----------


def test_create_authenticated_grants_authenticated_subject(be) -> None:
    assert _check(be, subject=_user("1"), action="create_authed") is True


def test_create_authenticated_denies_anonymous(be) -> None:
    assert _check(be, subject=anonymous_actor(), action="create_authed") is False


def test_create_anonymous_grants_anonymous_subject(be) -> None:
    assert _check(be, subject=anonymous_actor(), action="create_anon") is True


# ---------- relation-based term still routes through the accessible() fallback ----------


def test_create_owner_denies_without_an_owned_row(be) -> None:
    # No row owned anywhere => the empty-id eval is False AND accessible() is empty.
    assert _check(be, subject=_user("1"), action="create_owned") is False


def test_create_owner_grants_via_accessible_fallback(be) -> None:
    # Owning any row makes accessible(create_owned) non-empty -> the fallback grants.
    be.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", "p1"),
                relation="owner",
                subject=_user("1"),
            )
        ]
    )
    assert _check(be, subject=_user("1"), action="create_owned") is True


# ---------- const-backed arrow (universal-admin style) ----------


def test_create_const_admin_grants_member_of_const_role(be) -> None:
    # ``admin`` is const-bound to auth/role:superadmin for every blog/post row, so
    # the arrow resolves regardless of the (empty) resource id.
    be.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("auth/role", "superadmin"),
                relation="member",
                subject=_user("1"),
            )
        ]
    )
    assert _check(be, subject=_user("1"), action="create_admin") is True


def test_create_const_admin_denies_non_member(be) -> None:
    assert _check(be, subject=_user("9"), action="create_admin") is False


# ---------- end-to-end: the pre_save create signal gate ----------

INTEGRATION_SCHEMA = """
definition auth/user {}
definition blog/post {
    relation owner: auth/user
    permission read   = owner
    permission write  = owner
    permission create = authenticated
}
"""


@pytest.fixture
def _global_backend(db):
    reset_backend()
    backend().set_schema(parse_zed(INTEGRATION_SCHEMA))
    yield
    reset_backend()


def _django_user(username: str):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username=username, is_active=True)


@pytest.mark.django_db
def test_authenticated_actor_can_create_through_pre_save_gate(_global_backend) -> None:
    from tests.testapp.models import Post

    alice = _django_user("alice")
    # Without the empty-resource grant this would raise PermissionDenied (alice
    # owns no row, so accessible(create) is empty); create = authenticated grants.
    with actor_context(SubjectRef.of("auth/user", str(alice.pk))):
        post = Post.objects.create(title="hello")
    assert post.pk is not None


@pytest.mark.django_db
def test_anonymous_actor_cannot_create_when_create_is_authenticated(_global_backend) -> None:
    from tests.testapp.models import Post

    with actor_context(anonymous_actor()):
        with pytest.raises(PermissionDenied):
            Post.objects.create(title="nope")
