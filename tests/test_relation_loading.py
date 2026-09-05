"""Permission-aware relation loading helpers."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from django.db.models import F

from rebac import (
    MissingActorError,
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    actor_context,
    backend,
    sudo,
)
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
def test_rebac_select_related_redacts_and_tags_every_copy_of_shared_related_row(alice):
    from tests.testapp.models import Post

    backend().set_schema(
        parse_zed(
            SCHEMA_TEXT.replace(
                "permission read = owner + viewer",
                """
            permission read = owner + viewer
            permission read__name = owner
        """,
                1,
            )
        )
    )
    folder = _folder("private name")
    posts = [_post("first", folder=folder), _post("second", folder=folder)]
    _grant("blog/folder", folder.pk, "viewer", alice)
    for post in posts:
        _grant("blog/post", post.pk, "viewer", alice)

    rows = list(Post.objects.as_user(alice).on_field_deny("redact").rebac_select_related("folder"))

    assert len(rows) == 2
    assert rows[0].folder is not rows[1].folder
    assert [row.folder.name for row in rows] == [None, None]
    assert [row.folder.actor() for row in rows] == [SubjectRef.of("auth/user", str(alice.pk))] * 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("with_ambient_actor", [False, True])
def test_aiterator_sudo_does_not_bypass_selected_related_guards(alice, with_ambient_actor):
    from tests.testapp.models import Post

    folder = _folder("private")
    post = _post("visible", folder=folder)

    async def collect():
        qs = Post.objects.sudo(reason="test.root-only").rebac_select_related("folder")
        return [row async for row in qs.filter(pk=post.pk).aiterator()]

    if with_ambient_actor:
        with actor_context(SubjectRef.of("auth/user", str(alice.pk))):
            with pytest.raises(PermissionDenied):
                asyncio.run(collect())
    else:
        with pytest.raises(MissingActorError):
            asyncio.run(collect())


@pytest.mark.django_db
def test_sudo_does_not_bypass_selected_related_projection_guard(alice):
    from tests.testapp.models import Post

    folder = _folder("private")
    _post("visible", folder=folder)

    with actor_context(SubjectRef.of("auth/user", str(alice.pk))):
        with pytest.raises(PermissionDenied):
            list(
                Post.objects.sudo(reason="test.root-only")
                .rebac_select_related("folder")
                .values("folder__name")
            )


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
@pytest.mark.parametrize("project", [False, True])
def test_rebac_select_related_rejects_related_field_annotation(alice, project):
    from tests.testapp.models import Post

    folder = _folder("private")
    post = _post("visible", folder=folder)
    _grant("blog/post", post.pk, "viewer", alice)
    # Root-only sudo exercises the annotation guard independently from normal
    # instance joins. The related projection cannot inherit that bypass.
    qs = (
        Post.objects.sudo(reason="test.root-only")
        .rebac_select_related("folder")
        .annotate(copied_name=F("folder__name"))
    )
    if project:
        qs = qs.values("id", "copied_name")

    with actor_context(SubjectRef.of("auth/user", str(alice.pk))):
        with pytest.raises(PermissionDenied):
            list(qs)


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
