"""Tests for ``rebac.backends.auth.RebacBackend``.

Three branches matter for admin / DRF compatibility:

- Object-level ``has_perm(perm, obj)`` — the historical case, hits
  the engine via :func:`backend().has_access`.
- Model-level ``has_perm(perm)`` — admin's changelist / "Add" button
  use this; we map ``"<app>.<verb>_<model>"`` → ``rebac_resource_type``
  and call ``has_access`` with empty ``resource_id``.
- ``has_module_perms(app_label)`` — admin app index; True if the
  user has *any* read-accessible row in any model of the app.

Plus the corner cases: anonymous, inactive, superuser bypass,
unmappable perm strings.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    backend,
    sudo,
    write_relationships,
)
from rebac.backends import reset_backend
from rebac.backends.auth import RebacBackend
from rebac.schema import parse_zed

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation owner: auth/user
    permission read = owner
    permission write = owner
}
definition blog/post {
    relation owner: auth/user
    relation reader: auth/user
    relation creator: auth/user
    permission read = owner + reader
    permission write = owner
    permission delete = owner
    permission create = creator
}
"""


@pytest.fixture(autouse=True)
def _setup_schema(db):
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA))  # type: ignore[attr-defined]
    yield
    reset_backend()


@pytest.fixture
def user(db):
    User = get_user_model()
    return User.objects.create_user(username="alice", email="alice@example.com", password="x")


@pytest.fixture
def other_user(db):
    User = get_user_model()
    return User.objects.create_user(username="bob", email="bob@example.com", password="x")


@pytest.fixture
def post(db, user):
    from tests.testapp.models import Post

    with sudo(reason="seed test post for has_perm tests"):
        post = Post.objects.create(title="Hello", body="world")
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(post.pk)),
                relation="owner",
                subject=SubjectRef.of("auth/user", str(user.pk)),
            )
        ]
    )
    return post


# ---------- Object-level (historical path) ----------


def test_has_perm_object_level_owner_can_change(user, post):
    backend_ = RebacBackend()
    assert backend_.has_perm(user, "testapp.change_post", obj=post) is True


def test_has_perm_object_level_non_owner_denied(other_user, post):
    backend_ = RebacBackend()
    assert backend_.has_perm(other_user, "testapp.change_post", obj=post) is False


def test_has_perm_inactive_user_always_denied(user, post):
    user.is_active = False
    user.save(update_fields=["is_active"])
    backend_ = RebacBackend()
    assert backend_.has_perm(user, "testapp.change_post", obj=post) is False


# ---------- Model-level (admin changelist / "Add") ----------


def test_has_perm_model_level_returns_true_when_user_has_any_access(user, post):
    backend_ = RebacBackend()
    # User owns post, so they have read on at least one blog/post row.
    assert backend_.has_perm(user, "testapp.view_post") is True
    assert backend_.has_perm(user, "testapp.change_post") is True


def test_has_perm_model_level_returns_false_for_unrelated_user(other_user):
    backend_ = RebacBackend()
    assert backend_.has_perm(other_user, "testapp.view_post") is False
    assert backend_.has_perm(other_user, "testapp.change_post") is False


def test_has_perm_model_level_unknown_app_defers(user):
    backend_ = RebacBackend()
    # No "ghost" app — backend returns False so the next backend in
    # AUTHENTICATION_BACKENDS gets a shot.
    assert backend_.has_perm(user, "ghost.view_thing") is False


def test_has_perm_model_level_model_without_rebac_type_defers(user):
    """A model that exists but doesn't declare ``rebac_resource_type``
    should not produce an authoritative answer — defer to the next
    backend rather than synthesising a "blog/permission" namespace."""
    backend_ = RebacBackend()
    assert backend_.has_perm(user, "auth.change_user") is False


def test_has_perm_unparseable_perm_string_defers(user):
    backend_ = RebacBackend()
    # No verb prefix in the codename → can't map to a REBAC action.
    assert backend_.has_perm(user, "testapp.weirdname") is False
    assert backend_.has_perm(user, "noperiod") is False


def test_has_perm_unknown_verb_prefix_defers(user):
    backend_ = RebacBackend()
    # ``approve_post`` doesn't map to view/change/delete/add.
    assert backend_.has_perm(user, "testapp.approve_post") is False


# ---------- has_module_perms ----------


def test_has_module_perms_true_when_user_has_any_post_access(user, post):
    backend_ = RebacBackend()
    assert backend_.has_module_perms(user, "testapp") is True


def test_has_module_perms_false_for_unrelated_user(other_user):
    backend_ = RebacBackend()
    assert backend_.has_module_perms(other_user, "testapp") is False


def test_has_module_perms_unknown_app_returns_false(user):
    backend_ = RebacBackend()
    assert backend_.has_module_perms(user, "ghost_app") is False


def test_has_module_perms_app_without_rebac_models_returns_false(user):
    """``django.contrib.auth`` has User/Group/Permission — none declare
    ``rebac_resource_type`` in the test settings, so module perms can't
    resolve and the backend falls back to False."""
    backend_ = RebacBackend()
    assert backend_.has_module_perms(user, "auth") is False


# ---------- Superuser + inactive ----------


@override_settings(REBAC_SUPERUSER_BYPASS=True)
def test_superuser_bypass_grants_module_and_perm(user, post):
    user.is_superuser = True
    user.save(update_fields=["is_superuser"])
    backend_ = RebacBackend()
    assert backend_.has_perm(user, "testapp.change_post") is True
    assert backend_.has_perm(user, "testapp.change_post", obj=post) is True
    assert backend_.has_module_perms(user, "testapp") is True


def test_superuser_bypass_disabled_still_routes_through_engine(user, post):
    """REBAC_SUPERUSER_BYPASS is False in the test settings — even
    superusers must have explicit grants."""
    user.is_superuser = True
    user.save(update_fields=["is_superuser"])
    backend_ = RebacBackend()
    # Owner relation already exists from the fixture, so True. The
    # point is the answer comes from the engine, not the bypass.
    assert backend_.has_perm(user, "testapp.change_post", obj=post) is True


def test_inactive_user_denied_module_perms(user, post):
    user.is_active = False
    user.save(update_fields=["is_active"])
    backend_ = RebacBackend()
    assert backend_.has_module_perms(user, "testapp") is False


# ---------- Edge cases: None user, unresolvable obj, REBAC denial ----------


def test_has_perm_none_user_returns_false():
    """Some middleware paths leave ``request.user = None`` early in
    the chain; the backend must not crash."""
    backend_ = RebacBackend()
    assert backend_.has_perm(None, "testapp.change_post") is False


def test_has_perm_object_level_unresolvable_obj_defers(user):
    """An object that can't resolve to an ObjectRef returns False
    rather than crashing the auth chain."""
    backend_ = RebacBackend()
    assert backend_.has_perm(user, "testapp.change_post", obj=object()) is False


def test_has_perm_add_verb_routes_to_create_action(user, post):
    """The ``add`` codename maps to the REBAC ``create`` action — the
    "Add Post" button in admin uses ``has_perm("app.add_post")``.
    Without a ``creator`` relation the user can't create; once granted
    they can."""
    backend_ = RebacBackend()
    # The fixture grants only ``owner``; ``creator`` is separate.
    assert backend_.has_perm(user, "testapp.add_post") is False

    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(post.pk)),
                relation="creator",
                subject=SubjectRef.of("auth/user", str(user.pk)),
            )
        ]
    )
    assert backend_.has_perm(user, "testapp.add_post") is True


# ---------- authenticate / get_user contract ----------


def test_authenticate_returns_none():
    """RebacBackend never claims identity — it composes with whatever
    backend the project uses for sign-in."""
    assert RebacBackend().authenticate(None, username="x", password="y") is None


def test_get_user_returns_none():
    assert RebacBackend().get_user(1) is None
