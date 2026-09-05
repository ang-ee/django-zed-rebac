"""Tests for the configurable resource_id / subject_id attribute.

Covers the resolution chain documented in ``rebac/_id.py``:

    Meta.rebac_id_attr  →  REBAC_RESOURCE_ID_ATTR  →  "pk"

Both halves (resource side via signals + manager, subject side via
``to_subject_ref``) are exercised so a regression at either call site
shows up here.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from rebac import (
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    backend,
    resource_id_attr,
    subject_id_attr,
    sudo,
    to_subject_ref,
    write_relationships,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed

SCHEMA_TEXT = """
definition auth/user {}
definition blog/post {
    relation owner: auth/user
    permission read = owner
    permission write = owner
    permission delete = owner
}
definition blog/sluggedpost {
    relation owner: auth/user
    permission read = owner
    permission write = owner
    permission delete = owner
}
"""


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


# ---------------------------------------------------------------------------
# Helper resolution
# ---------------------------------------------------------------------------


def test_resource_id_attr_defaults_to_pk():
    from tests.testapp.models import Post

    assert resource_id_attr(Post) == "pk"


def test_resource_id_attr_honours_per_model_meta():
    from tests.testapp.models import SluggedPost

    assert resource_id_attr(SluggedPost) == "slug"


@override_settings(REBAC_RESOURCE_ID_ATTR="public_id")
def test_resource_id_attr_falls_through_to_setting():
    from tests.testapp.models import Post

    # No Meta.rebac_id_attr on Post → setting wins.
    assert resource_id_attr(Post) == "public_id"


@override_settings(REBAC_RESOURCE_ID_ATTR="public_id")
def test_per_model_meta_wins_over_setting():
    from tests.testapp.models import SluggedPost

    # SluggedPost.Meta.rebac_id_attr = "slug" — overrides the setting.
    assert resource_id_attr(SluggedPost) == "slug"


# ---------------------------------------------------------------------------
# subject_id_attr — actor-side resolution
# ---------------------------------------------------------------------------


def test_subject_id_attr_defaults_to_pk():
    from django.contrib.auth import get_user_model

    assert subject_id_attr(get_user_model()) == "pk"


@override_settings(REBAC_USER_ID_ATTR="username")
def test_subject_id_attr_honours_setting():
    from django.contrib.auth import get_user_model

    assert subject_id_attr(get_user_model()) == "username"


# ---------------------------------------------------------------------------
# to_subject_ref — User branch reads the configured attr
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(REBAC_USER_ID_ATTR="username")
def test_to_subject_ref_user_uses_setting():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    u = User.objects.create(username="alice", is_active=True)
    ref = to_subject_ref(u)
    assert ref.subject_type == "auth/user"
    assert ref.subject_id == "alice"


@pytest.mark.django_db
def test_to_subject_ref_user_defaults_to_pk():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    u = User.objects.create(username="alice", is_active=True)
    ref = to_subject_ref(u)
    assert ref.subject_id == str(u.pk)


# ---------------------------------------------------------------------------
# Signals — resource_id reflects the configured attr on writes
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_pre_save_uses_pk_by_default():
    """Existing pk-default consumers stay green — no regression."""
    from tests.testapp.models import Post

    user = _make_user("alice")
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="hello")
    _grant_owner_pk(user, post, "blog/post")

    # Update fires pre_save with action="write"; check passes only if
    # the engine asks about resource id == post.pk.
    Post.objects.with_actor(user).filter(pk=post.pk).update(title="updated")
    post.refresh_from_db()
    assert post.title == "updated"


@pytest.mark.django_db
def test_pre_save_uses_meta_attr_when_set():
    from tests.testapp.models import SluggedPost

    user = _make_user("alice")
    with sudo(reason="test.fixture"):
        post = SluggedPost.objects.create(slug="hello-world", title="hi")

    # Grant relationship keyed on slug — what the engine should ask for.
    _grant_owner(user, "blog/sluggedpost", post.slug)

    post.title = "updated"
    post._rebac_actor = to_subject_ref(user)  # propagate actor to instance
    post.save()

    post.refresh_from_db()
    assert post.title == "updated"


@pytest.mark.django_db
def test_pre_save_denies_when_grant_uses_wrong_id():
    """Sanity check on the pre_save path — granting under the pk
    instead of the slug must NOT authorise a save on a slug-keyed model.
    """
    from tests.testapp.models import SluggedPost

    user = _make_user("alice")
    with sudo(reason="test.fixture"):
        post = SluggedPost.objects.create(slug="hello-world", title="hi")

    # WRONG: grant uses pk instead of slug.
    _grant_owner(user, "blog/sluggedpost", str(post.pk))

    post.title = "updated"
    post._rebac_actor = to_subject_ref(user)
    with pytest.raises(PermissionDenied):
        post.save()


# ---------------------------------------------------------------------------
# Manager — _apply_scope_in_place filters by the configured column
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_manager_filters_by_pk_by_default():
    from tests.testapp.models import Post

    user = _make_user("alice")
    with sudo(reason="test.fixture"):
        p1 = Post.objects.create(title="a")
        Post.objects.create(title="b")  # not granted

    _grant_owner_pk(user, p1, "blog/post")

    visible = list(Post.objects.with_actor(user).values_list("pk", flat=True))
    assert visible == [p1.pk]


@pytest.mark.django_db
def test_manager_filters_by_meta_attr_when_set():
    from tests.testapp.models import SluggedPost

    user = _make_user("alice")
    with sudo(reason="test.fixture"):
        SluggedPost.objects.create(slug="visible", title="a")
        SluggedPost.objects.create(slug="hidden", title="b")  # not granted

    _grant_owner(user, "blog/sluggedpost", "visible")

    visible = list(SluggedPost.objects.with_actor(user).values_list("slug", flat=True))
    assert visible == ["visible"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(username: str):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username=username, is_active=True)


def _grant_owner_pk(user, instance, resource_type: str) -> None:
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef(resource_type, str(instance.pk)),
                relation="owner",
                subject=SubjectRef.of("auth/user", str(user.pk)),
            )
        ]
    )


def _grant_owner(user, resource_type: str, resource_id: str) -> None:
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef(resource_type, resource_id),
                relation="owner",
                subject=SubjectRef.of("auth/user", str(user.pk)),
            )
        ]
    )
