from __future__ import annotations

from types import SimpleNamespace

import pytest

from rebac import ObjectRef, RelationshipTuple, SubjectRef, actor_context, backend, sudo
from rebac.backends import reset_backend
from rebac.drf import RebacFilterBackend, RebacPermission
from rebac.schema import parse_zed

SCHEMA_TEXT = """
definition auth/user {}

definition agents/grant {}

definition blog/post {
    relation owner: auth/user | agents/grant#valid
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


@pytest.mark.django_db
def test_drf_prefers_current_actor_over_request_user_for_permissions_and_filtering() -> None:
    from django.contrib.auth import get_user_model

    from tests.testapp.models import Post

    alice = get_user_model().objects.create(username="alice", is_active=True)
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="Visible to Alice")
    backend().write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(post.pk)),
                relation="owner",
                subject=SubjectRef.of("auth/user", str(alice.pk)),
            )
        ]
    )

    request = SimpleNamespace(method="GET", user=alice)
    view = SimpleNamespace(action="list", queryset=Post.objects.all())
    grant_actor = SubjectRef.of("agents/grant", "g1", "valid")

    with actor_context(grant_actor):
        # Empty list access is admitted; row scoping produces [] below.
        assert RebacPermission().has_permission(request, view)
        assert not RebacPermission().has_object_permission(request, view, post)
        scoped = RebacFilterBackend().filter_queryset(request, Post.objects.all(), view)
        assert list(scoped) == []


@pytest.mark.django_db
def test_drf_honors_view_permission_map_for_custom_actions() -> None:
    from django.contrib.auth import get_user_model

    from tests.testapp.models import Post

    user = get_user_model().objects.create_user(username="custom-action")
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="Protected action")
    request = SimpleNamespace(method="POST", user=user)
    view = SimpleNamespace(
        action="publish",
        queryset=Post.objects.all(),
        rebac_action_map={"publish": "write"},
    )

    permission = RebacPermission()
    assert not permission.has_permission(request, view)
    assert not permission.has_object_permission(request, view, post)

    backend().write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(post.pk)),
                relation="owner",
                subject=SubjectRef.of("auth/user", str(user.pk)),
            )
        ]
    )
    assert permission.has_permission(request, view)
    assert permission.has_object_permission(request, view, post)


@pytest.mark.django_db
def test_drf_partial_view_permission_map_retains_default_actions() -> None:
    from django.contrib.auth import get_user_model

    from tests.testapp.models import Post

    request = SimpleNamespace(
        method="DELETE", user=get_user_model().objects.create_user(username="partial-action-map")
    )
    view = SimpleNamespace(
        action="destroy", queryset=Post.objects.all(), rebac_action_map={"publish": "write"}
    )
    assert not RebacPermission().has_permission(request, view)


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
def test_drf_http_methods_enforce_object_permissions(method) -> None:
    from django.contrib.auth import get_user_model

    from tests.testapp.models import Post

    user = get_user_model().objects.create_user(username=f"method-{method}")
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="Protected")
    request = SimpleNamespace(method=method, user=user)
    view = SimpleNamespace(queryset=Post.objects.all())
    assert not RebacPermission().has_object_permission(request, view, post)


@pytest.mark.django_db
def test_drf_unmapped_actions_fail_closed() -> None:
    from django.contrib.auth import get_user_model

    from tests.testapp.models import Post

    request = SimpleNamespace(
        method="POST", user=get_user_model().objects.create_user(username="unmapped")
    )
    view = SimpleNamespace(action="publish", queryset=Post.objects.all())
    permission = RebacPermission()
    assert not permission.has_permission(request, view)
    assert not permission.has_object_permission(request, view, ObjectRef("blog/post", "1"))
    request.method = "PROPFIND"
    del view.action
    assert not permission.has_permission(request, view)
    assert not permission.has_object_permission(request, view, ObjectRef("blog/post", "1"))


@pytest.mark.django_db
def test_drf_empty_http_list_is_admitted_and_scoped() -> None:
    from django.contrib.auth import get_user_model

    from tests.testapp.models import Post

    request = SimpleNamespace(
        method="GET", user=get_user_model().objects.create_user(username="empty-list")
    )
    view = SimpleNamespace(queryset=Post.objects.all())
    assert RebacPermission().has_permission(request, view)
    assert list(RebacFilterBackend().filter_queryset(request, view.queryset, view)) == []
