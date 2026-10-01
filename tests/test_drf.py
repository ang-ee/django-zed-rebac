from __future__ import annotations

from types import SimpleNamespace

import pytest

from rebac import ObjectRef, RelationshipTuple, SubjectRef, actor_context, backend, sudo
from rebac.backends import reset_backend
from rebac.drf import RebacFilterBackend, RebacPermission
from rebac.schema import parse_zed
from tests.backend_setup import atomic_source_write, install_schema

SCHEMA_TEXT = """
definition auth/user {}

definition agents/grant {
    relation valid: auth/user
}

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
    install_schema(backend(), parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


@pytest.mark.django_db
def test_drf_prefers_current_actor_over_request_user_for_permissions_and_filtering() -> None:
    from django.contrib.auth import get_user_model

    from tests.testapp.models import Post

    alice = atomic_source_write(get_user_model().objects.create, username="alice", is_active=True)
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

    user = atomic_source_write(get_user_model().objects.create_user, username="custom-action")
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
        method="DELETE",
        user=atomic_source_write(
            get_user_model().objects.create_user, username="partial-action-map"
        ),
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

    user = atomic_source_write(get_user_model().objects.create_user, username=f"method-{method}")
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
        method="POST",
        user=atomic_source_write(get_user_model().objects.create_user, username="unmapped"),
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
        method="GET",
        user=atomic_source_write(get_user_model().objects.create_user, username="empty-list"),
    )
    view = SimpleNamespace(queryset=Post.objects.all())
    assert RebacPermission().has_permission(request, view)
    assert list(RebacFilterBackend().filter_queryset(request, view.queryset, view)) == []


# ---------------------------------------------------------------------------
# A real ModelViewSet (ARCHITECTURE.md § Surface integrations — DRF)
# ---------------------------------------------------------------------------

VIEWSET_SCHEMA = """
definition auth/user {}

definition blog/post {
    relation owner: auth/user
    relation reader: auth/user
    permission read = owner + reader
    permission write = owner
    permission delete = owner
    permission create = authenticated
}
"""


def _viewset():
    from rest_framework import serializers, viewsets

    from tests.testapp.models import Post

    class PostSerializer(serializers.ModelSerializer):
        class Meta:
            model = Post
            fields = ("id", "title")

    class PostViewSet(viewsets.ModelViewSet):
        queryset = Post.objects.all()
        serializer_class = PostSerializer
        permission_classes = (RebacPermission,)
        filter_backends = (RebacFilterBackend,)

    return PostViewSet


@pytest.fixture
def viewset_schema(db):
    install_schema(backend(), parse_zed(VIEWSET_SCHEMA))


@pytest.fixture
def api(viewset_schema):
    """Dispatch through ``ActorMiddleware``, as a deployed request would be."""
    from django.contrib.auth.models import AnonymousUser
    from rest_framework.test import APIRequestFactory, force_authenticate

    from rebac.middleware import ActorMiddleware

    factory = APIRequestFactory()
    viewset = _viewset()

    def call(user, method, *, pk=None, data=None):
        path = "/posts/" if pk is None else f"/posts/{pk}/"
        request = getattr(factory, method)(path, data, format="json")
        request.user = user or AnonymousUser()
        if user is not None:
            force_authenticate(request, user=user)
        if pk is None:
            actions = {"get": "list", "post": "create"}
        else:
            actions = {
                "get": "retrieve",
                "put": "update",
                "patch": "partial_update",
                "delete": "destroy",
            }
        view = viewset.as_view(actions)
        kwargs = {} if pk is None else {"pk": pk}
        return ActorMiddleware(lambda request: view(request, **kwargs))(request)

    return call


@pytest.fixture
def people(viewset_schema):
    from django.contrib.auth import get_user_model

    from tests.testapp.models import Post

    User = get_user_model()
    alice = atomic_source_write(User.objects.create_user, username="vs-alice")
    bob = atomic_source_write(User.objects.create_user, username="vs-bob")
    with sudo(reason="test.fixture"):
        alices = Post.objects.create(title="Alice's")
        bobs = Post.objects.create(title="Bob's")
    backend().write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(alices.pk)),
                relation="owner",
                subject=SubjectRef.of("auth/user", str(alice.pk)),
            ),
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(bobs.pk)),
                relation="owner",
                subject=SubjectRef.of("auth/user", str(bob.pk)),
            ),
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(alices.pk)),
                relation="reader",
                subject=SubjectRef.of("auth/user", str(bob.pk)),
            ),
        ]
    )
    return SimpleNamespace(alice=alice, bob=bob, alices=alices, bobs=bobs)


def _titles(pk):
    from tests.testapp.models import Post

    return list(
        Post.objects.sudo(reason="test.assert").filter(pk=pk).values_list("title", flat=True)
    )


def test_viewset_list_is_scoped(api, people) -> None:
    alice_list = api(people.alice, "get")
    assert alice_list.status_code == 200
    assert [row["title"] for row in alice_list.data] == ["Alice's"]
    bob_list = api(people.bob, "get")
    assert sorted(row["title"] for row in bob_list.data) == ["Alice's", "Bob's"]


def test_viewset_anonymous_list_is_empty_not_denied(api, people) -> None:
    response = api(None, "get")
    assert response.status_code == 200
    assert response.data == []


def test_viewset_retrieve_of_an_unreadable_row_is_not_found(api, people) -> None:
    assert api(people.alice, "get", pk=people.bobs.pk).status_code == 404
    readable = api(people.bob, "get", pk=people.alices.pk)
    assert readable.status_code == 200
    assert readable.data["title"] == "Alice's"


def test_viewset_create_allowed_and_denied(api, people) -> None:
    from tests.testapp.models import Post

    created = api(people.alice, "post", data={"title": "Fresh"})
    assert created.status_code == 201, created.data
    assert _titles(created.data["id"]) == ["Fresh"]

    before = Post.objects.sudo(reason="test.assert").count()
    denied = api(None, "post", data={"title": "Anonymous"})
    assert denied.status_code == 403
    assert Post.objects.sudo(reason="test.assert").count() == before


def test_viewset_update_allowed_and_denied(api, people) -> None:
    updated = api(people.alice, "patch", pk=people.alices.pk, data={"title": "Renamed"})
    assert updated.status_code == 200, updated.data
    assert _titles(people.alices.pk) == ["Renamed"]

    # Bob may read Alice's post but not write it.
    denied = api(people.bob, "put", pk=people.alices.pk, data={"title": "Hijacked"})
    assert denied.status_code == 403
    assert _titles(people.alices.pk) == ["Renamed"]
    # Alice cannot even see Bob's post.
    assert api(people.alice, "patch", pk=people.bobs.pk, data={"title": "x"}).status_code == 404


def test_viewset_destroy_allowed_and_denied(api, people) -> None:
    denied = api(people.bob, "delete", pk=people.alices.pk)
    assert denied.status_code == 403
    assert _titles(people.alices.pk) == ["Alice's"]

    deleted = api(people.alice, "delete", pk=people.alices.pk)
    assert deleted.status_code == 204
    assert _titles(people.alices.pk) == []


@pytest.mark.django_db
def test_filter_queryset_without_a_subject_is_empty() -> None:
    from rebac import MissingActorError
    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        Post.objects.create(title="Hidden")
    request = SimpleNamespace(method="GET")
    scoped = RebacFilterBackend().filter_queryset(request, Post.objects.all(), SimpleNamespace())
    assert scoped.query.is_empty()
    # Strict mode may refuse the actor-less queryset outright; it never yields rows.
    try:
        rows = list(scoped)
    except MissingActorError:
        rows = []
    assert rows == []


@pytest.mark.django_db
def test_filter_queryset_leaves_non_rebac_querysets_alone() -> None:
    from django.contrib.auth import get_user_model

    User = get_user_model()
    atomic_source_write(User.objects.create_user, username="plain")
    queryset = User.objects.all()
    request = SimpleNamespace(method="GET", user=None)
    assert RebacFilterBackend().filter_queryset(request, queryset, SimpleNamespace()) is queryset


@pytest.mark.django_db
@pytest.mark.parametrize("user", [None, object()], ids=["no-user", "unresolvable-user"])
def test_permission_requires_a_resolved_actor(user) -> None:
    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="Needs an actor")
    request = SimpleNamespace(method="GET", user=user)
    view = SimpleNamespace(action="list", queryset=Post.objects.all())
    permission = RebacPermission()
    assert not permission.has_permission(request, view)
    view.action = "retrieve"
    assert not permission.has_object_permission(request, view, post)
