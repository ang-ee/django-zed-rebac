"""Strict mode: no terminal queryset or instance operation runs without an actor."""

from __future__ import annotations

import asyncio

import pytest
from django.db.models import Count

from rebac import MissingActorError, backend, sudo
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.testapp.models import AuthoredPost, Post

SCHEMA_TEXT = """
definition auth/user {}
definition blog/post {
    relation owner: auth/user
    permission read = owner
    permission write = owner
    permission delete = owner
    permission create = owner
}
definition blog/authoredpost {
    relation owner: auth/user
    permission read = owner
    permission write = owner
    permission delete = owner
    permission create = owner
}
"""


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


@pytest.fixture
def post(db):
    from django.contrib.auth import get_user_model

    author = get_user_model().objects.create(username="author")
    with sudo(reason="test.fixture"):
        AuthoredPost.objects.create(title="dated", author=author)
        created = Post.objects.create(title="hidden")
        return Post.objects.get(pk=created.pk)


async def _drain(rows):
    return [row async for row in rows]


def _bulk_update(post):
    post.title = "changed"
    return Post.objects.bulk_update([post], ["title"])


QUERYSET_TERMINALS = {
    "list": lambda post: list(Post.objects.all()),
    "count": lambda post: Post.objects.count(),
    "exists": lambda post: Post.objects.exists(),
    "get": lambda post: Post.objects.get(pk=post.pk),
    "first": lambda post: Post.objects.first(),
    "last": lambda post: Post.objects.last(),
    "earliest": lambda post: Post.objects.earliest("pk"),
    "latest": lambda post: Post.objects.latest("pk"),
    "values": lambda post: list(Post.objects.values("title")),
    "values_list": lambda post: list(Post.objects.values_list("title", flat=True)),
    "iterator": lambda post: list(Post.objects.iterator()),
    "aiterator": lambda post: asyncio.run(_drain(Post.objects.all().aiterator())),
    "in_bulk": lambda post: Post.objects.in_bulk([post.pk]),
    "in_bulk_all": lambda post: Post.objects.in_bulk(),
    "aggregate": lambda post: Post.objects.aggregate(n=Count("pk")),
    "update": lambda post: Post.objects.filter(pk=post.pk).update(title="changed"),
    "delete": lambda post: Post.objects.filter(pk=post.pk).delete(),
    "bulk_update": _bulk_update,
    "create": lambda post: Post.objects.create(title="new"),
    "bulk_create": lambda post: Post.objects.bulk_create([Post(title="new")]),
    "get_or_create": lambda post: Post.objects.get_or_create(title="hidden"),
    "update_or_create": lambda post: Post.objects.update_or_create(
        title="hidden", defaults={"body": "changed"}
    ),
    "contains": lambda post: Post.objects.contains(post),
    "dates": lambda post: list(AuthoredPost.objects.dates("author__date_joined", "day")),
    "datetimes": lambda post: list(AuthoredPost.objects.datetimes("author__date_joined", "day")),
    "explain": lambda post: Post.objects.all().explain(),
    "raw": lambda post: list(Post.objects.raw(f"SELECT * FROM {Post._meta.db_table}")),
}

INSTANCE_TERMINALS = {
    "save": lambda post: post.save(),
    "delete": lambda post: post.delete(),
    "check_access": lambda post: post.check_access("read"),
    "has_access": lambda post: post.has_access("read"),
}


@pytest.mark.parametrize("call", list(QUERYSET_TERMINALS.values()), ids=list(QUERYSET_TERMINALS))
def test_queryset_terminal_without_actor_raises(post, call):
    with pytest.raises(MissingActorError):
        call(post)


@pytest.mark.parametrize("call", list(INSTANCE_TERMINALS.values()), ids=list(INSTANCE_TERMINALS))
def test_instance_operation_without_actor_raises(post, call):
    with pytest.raises(MissingActorError):
        call(post)


def test_rows_are_unchanged_after_refused_writes(post):
    for name in ("update", "delete", "bulk_update", "update_or_create"):
        with pytest.raises(MissingActorError):
            QUERYSET_TERMINALS[name](post)
    with pytest.raises(MissingActorError):
        post.delete()

    with sudo(reason="test.verify"):
        assert list(Post.objects.values_list("title", "body")) == [("hidden", "")]
