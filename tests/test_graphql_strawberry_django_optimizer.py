"""Strawberry-Django optimizer integration with REBAC relation loading."""

from __future__ import annotations

from typing import Any, cast

import pytest

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    actor_context,
    backend,
    sudo,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed

pytest.importorskip(
    "strawberry_django",
    reason="strawberry-graphql-django not installed",
)

import strawberry
import strawberry_django
from strawberry import auto

from rebac.graphql.strawberry import RebacExtension
from rebac.graphql.strawberry_django import RebacDjangoOptimizerExtension
from tests.testapp.models import Folder, Post

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


@strawberry_django.type(Folder)
class FolderType:
    id: auto
    name: auto
    posts: list[PostType]


@strawberry_django.type(Post)
class PostType:
    id: auto
    title: auto
    folder: FolderType | None


@strawberry.type
class Query:
    posts: list[PostType] = strawberry_django.field()
    folders: list[FolderType] = strawberry_django.field()

    @strawberry.field
    def manual_posts(self, info: strawberry.Info) -> list[PostType]:
        return cast(list[PostType], Post.objects.select_related("folder").all())


def _schema():
    return strawberry.Schema(
        query=Query,
        extensions=[RebacExtension, RebacDjangoOptimizerExtension],
    )


@pytest.mark.django_db
def test_optimizer_scopes_root_queryset(alice):
    visible = _post("visible")
    _post("hidden")
    _grant("blog/post", visible.pk, "viewer", alice)

    with actor_context(alice):
        result = _schema().execute_sync("{ posts { title } }")

    assert result.errors is None
    assert result.data == {"posts": [{"title": "visible"}]}


@pytest.mark.django_db
def test_optimizer_keeps_select_related_but_fails_denied_join(alice):
    folder = _folder("private")
    post = _post("visible", folder=folder)
    _grant("blog/post", post.pk, "viewer", alice)

    with actor_context(alice):
        result = _schema().execute_sync("{ manualPosts { title folder { name } } }")

    assert result.errors
    assert "outside actor scope" in result.errors[0].message
    assert result.data is None


@pytest.mark.django_db
def test_optimizer_returns_readable_joined_object(alice):
    folder = _folder("readable")
    post = _post("visible", folder=folder)
    _grant("blog/post", post.pk, "viewer", alice)
    _grant("blog/folder", folder.pk, "viewer", alice)

    with actor_context(alice):
        result = _schema().execute_sync("{ manualPosts { title folder { name } } }")

    assert result.errors is None
    assert result.data == {"manualPosts": [{"title": "visible", "folder": {"name": "readable"}}]}


@pytest.mark.django_db
def test_optimizer_scopes_reverse_prefetch_and_reflects_revoke(alice):
    folder = _folder("root")
    visible = _post("visible", folder=folder)
    hidden = _post("hidden", folder=folder)
    _grant("blog/folder", folder.pk, "viewer", alice)
    _grant("blog/post", visible.pk, "viewer", alice)
    _grant("blog/post", hidden.pk, "viewer", alice)
    schema = _schema()

    with actor_context(alice):
        first = schema.execute_sync("{ folders { name posts { title } } }")

    assert first.errors is None
    assert first.data == {
        "folders": [{"name": "root", "posts": [{"title": "visible"}, {"title": "hidden"}]}]
    }

    from rebac import delete_relationship

    delete_relationship(
        RelationshipTuple(
            resource=ObjectRef("blog/post", str(hidden.pk)),
            relation="viewer",
            subject=SubjectRef.of("auth/user", str(alice.pk)),
        )
    )

    with actor_context(alice):
        second = schema.execute_sync("{ folders { name posts { title } } }")

    assert second.errors is None
    assert second.data == {"folders": [{"name": "root", "posts": [{"title": "visible"}]}]}
