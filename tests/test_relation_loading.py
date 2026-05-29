"""Permission-aware relation loading helpers."""

from __future__ import annotations

from typing import Any

import pytest

from rebac import ObjectRef, PermissionDenied, RelationshipTuple, SubjectRef, backend, sudo
from rebac.backends import reset_backend
from rebac.schema import parse_zed

SCHEMA_TEXT = """
definition auth/user {}
definition blog/folder {
    relation owner: auth/user
    relation viewer: auth/user
    permission read = owner + viewer
}
definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user
    permission read = owner + viewer
}
"""


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


@pytest.fixture
def alice(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username="alice", is_active=True)


def _grant(resource_type: str, resource_id: object, relation: str, user: Any) -> None:
    from rebac import write_relationships

    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef(resource_type, str(resource_id)),
                relation=relation,
                subject=SubjectRef.of("auth/user", str(user.pk)),
            )
        ]
    )


def _folder(name: str):
    from tests.testapp.models import Folder

    with sudo(reason="test.fixture"):
        return Folder.objects.create(name=name)


def _post(title: str, folder=None):
    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        return Post.objects.create(title=title, folder=folder)


@pytest.mark.django_db
def test_rebac_select_related_preserves_join_and_raises_for_denied_related(alice):
    from tests.testapp.models import Post

    folder = _folder("private")
    post = _post("visible", folder=folder)
    _grant("blog/post", post.pk, "viewer", alice)

    qs = Post.objects.as_user(alice).rebac_select_related("folder").filter(pk=post.pk)
    assert "JOIN" in str(qs.query)

    with pytest.raises(PermissionDenied):
        qs.get()


@pytest.mark.django_db
def test_rebac_select_related_tags_readable_related_without_extra_query(
    alice, django_assert_num_queries
):
    from tests.testapp.models import Post

    folder = _folder("readable")
    post = _post("visible", folder=folder)
    _grant("blog/post", post.pk, "viewer", alice)
    _grant("blog/folder", folder.pk, "viewer", alice)

    row = Post.objects.as_user(alice).rebac_select_related("folder").get(pk=post.pk)

    assert row.folder.actor() == SubjectRef.of("auth/user", str(alice.pk))
    with django_assert_num_queries(0):
        assert row.folder.name == "readable"


@pytest.mark.django_db
def test_rebac_select_related_skips_guard_when_target_grants_all(alice):
    from tests.testapp.models import Post

    backend().set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition blog/folder {
                permission read = authenticated
            }
            definition blog/post {
                relation viewer: auth/user
                permission read = viewer
            }
            """
        )
    )
    folder = _folder("globally readable")
    post = _post("visible", folder=folder)
    _grant("blog/post", post.pk, "viewer", alice)

    row = Post.objects.as_user(alice).rebac_select_related("folder").get(pk=post.pk)

    assert row.folder.name == "globally readable"


@pytest.mark.django_db
def test_rebac_select_related_still_guards_inside_ambient_sudo(alice):
    from tests.testapp.models import Post

    folder = _folder("private")
    post = _post("visible", folder=folder)
    _grant("blog/post", post.pk, "viewer", alice)

    with sudo(reason="test.ambient"):
        with pytest.raises(PermissionDenied):
            Post.objects.as_user(alice).rebac_select_related("folder").get(pk=post.pk)


@pytest.mark.django_db
def test_rebac_select_related_rejects_related_field_projection(alice):
    from tests.testapp.models import Post

    folder = _folder("private")
    post = _post("visible", folder=folder)
    _grant("blog/post", post.pk, "viewer", alice)

    with pytest.raises(PermissionDenied):
        list(Post.objects.as_user(alice).rebac_select_related("folder").values("folder__name"))


@pytest.mark.django_db
def test_rebac_prefetch_related_scopes_reverse_relation(alice):
    from tests.testapp.models import Folder

    folder = _folder("root")
    visible = _post("visible", folder=folder)
    hidden = _post("hidden", folder=folder)
    _grant("blog/folder", folder.pk, "viewer", alice)
    _grant("blog/post", visible.pk, "viewer", alice)

    row = Folder.objects.as_user(alice).rebac_prefetch_related("posts").get(pk=folder.pk)

    assert [post.title for post in row.posts.all()] == ["visible"]
    assert hidden.title == "hidden"


@pytest.mark.django_db
def test_rebac_prefetch_related_scopes_nested_protected_prefix(alice):
    from tests.testapp.models import Folder

    root = _folder("root")
    hidden_child = _folder("hidden child")
    hidden_child.parent = root
    with sudo(reason="test.fixture"):
        hidden_child.save()
    _post("child post", folder=hidden_child)
    _grant("blog/folder", root.pk, "viewer", alice)

    row = Folder.objects.as_user(alice).rebac_prefetch_related("children__posts").get(pk=root.pk)

    assert list(row.children.all()) == []
