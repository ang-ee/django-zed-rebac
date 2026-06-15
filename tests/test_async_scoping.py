"""Async ORM scoping — REBAC actor scoping holds across Django's async ORM.

Django implements almost every async QuerySet method (``aget`` / ``acount`` /
``aexists`` / ``afirst`` / ``aupdate`` / ``adelete`` / ``acreate`` /
``__aiter__`` / ``ain_bulk`` / …) as a ``sync_to_async`` wrapper around the sync
method ``RebacQuerySet`` already overrides, so scoping is inherited for free and
the ``current_actor()`` ContextVar carries into the worker thread. The two
methods that bypass the sync path — ``aiterator()`` (builds the row iterable
directly) and ``aggregate()`` / ``aaggregate()`` (computes against the query
without materialising) — are overridden in ``RebacQuerySet`` to re-apply scope.

These tests lock that contract in. The leak regressions (``aiterator`` /
``aaggregate``) are the ones that would silently re-open if either override is
dropped; the rest are guards in case a future Django stops wrapping sync.

Async tests use ``transaction=True`` because the async wrappers run the sync ORM
on asgiref's worker thread — pytest-django's default rollback-wrapped
transaction would deadlock that thread on sqlite.
"""

from __future__ import annotations

import asyncio

import pytest
from django.db.models import Count

from rebac import (
    MissingActorError,
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    actor_context,
    backend,
    sudo,
    write_relationships,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed

SCHEMA_TEXT = """
definition auth/user {}
definition blog/post {
    relation owner: auth/user
    permission read   = owner
    permission write  = owner
    permission delete = owner
    permission create = owner
}
"""


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


def _user(username: str):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username=username, is_active=True)


def _post(title: str = "secret"):
    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        return Post.objects.create(title=title)


def _grant_owner(user, post) -> None:
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(post.pk)),
                relation="owner",
                subject=SubjectRef.of("auth/user", str(user.pk)),
            )
        ]
    )


def _ref(user) -> SubjectRef:
    return SubjectRef.of("auth/user", str(user.pk))


# ---------- methods that route through sync overrides (regression guards) ----------


@pytest.mark.django_db(transaction=True)
def test_aget_scopes_to_owner() -> None:
    from tests.testapp.models import Post

    post = _post()
    alice, bob = _user("alice"), _user("bob")
    _grant_owner(alice, post)

    async def go() -> tuple[object, object]:
        got = await Post.objects.as_user(alice).aget(pk=post.pk)
        with pytest.raises(Post.DoesNotExist):
            await Post.objects.as_user(bob).aget(pk=post.pk)
        return got.pk, "bob-denied"

    assert asyncio.run(go()) == (post.pk, "bob-denied")


@pytest.mark.django_db(transaction=True)
def test_async_for_scopes_to_owner() -> None:
    from tests.testapp.models import Post

    post = _post()
    alice, bob = _user("alice"), _user("bob")
    _grant_owner(alice, post)

    async def collect(actor) -> list[int]:
        return [p.pk async for p in Post.objects.as_user(actor)]

    assert asyncio.run(collect(alice)) == [post.pk]
    assert asyncio.run(collect(bob)) == []


@pytest.mark.django_db(transaction=True)
def test_acount_and_aexists_respect_scope() -> None:
    from tests.testapp.models import Post

    post = _post()
    alice, bob = _user("alice"), _user("bob")
    _grant_owner(alice, post)

    async def go() -> tuple[int, int, bool, bool]:
        return (
            await Post.objects.as_user(alice).acount(),
            await Post.objects.as_user(bob).acount(),
            await Post.objects.as_user(alice).filter(pk=post.pk).aexists(),
            await Post.objects.as_user(bob).filter(pk=post.pk).aexists(),
        )

    assert asyncio.run(go()) == (1, 0, True, False)


@pytest.mark.django_db(transaction=True)
def test_ambient_actor_carries_into_async_worker() -> None:
    from tests.testapp.models import Post

    post = _post()
    alice, bob = _user("alice"), _user("bob")
    _grant_owner(alice, post)

    async def aget_as(user) -> str:
        with actor_context(_ref(user)):
            try:
                got = await Post.objects.aget(pk=post.pk)
                return f"got:{got.pk}"
            except Post.DoesNotExist:
                return "denied"

    assert asyncio.run(aget_as(alice)) == f"got:{post.pk}"
    assert asyncio.run(aget_as(bob)) == "denied"


@pytest.mark.django_db(transaction=True)
def test_strict_mode_raises_in_async_without_actor() -> None:
    from tests.testapp.models import Post

    post = _post()

    async def go() -> object:
        return await Post.objects.aget(pk=post.pk)

    with pytest.raises(MissingActorError):
        asyncio.run(go())


# ---------- the two real leaks: aiterator + aggregate (regressions) ----------


@pytest.mark.django_db(transaction=True)
def test_aiterator_respects_scope() -> None:
    from tests.testapp.models import Post

    post = _post()
    alice, bob = _user("alice"), _user("bob")
    _grant_owner(alice, post)

    async def collect(actor) -> list[int]:
        return [p.pk async for p in Post.objects.as_user(actor).aiterator()]

    assert asyncio.run(collect(alice)) == [post.pk]
    # Regression: before the aiterator override this leaked the row to bob.
    assert asyncio.run(collect(bob)) == []


@pytest.mark.django_db(transaction=True)
def test_aiterator_stamps_actor_on_instances() -> None:
    from tests.testapp.models import Post

    post = _post()
    alice = _user("alice")
    _grant_owner(alice, post)

    async def collect() -> list[Post]:
        return [p async for p in Post.objects.as_user(alice).aiterator()]

    rows = asyncio.run(collect())
    assert [r.pk for r in rows] == [post.pk]
    # Stamped actor lets a subsequent instance.save() check against the same actor.
    assert rows[0]._rebac_actor == _ref(alice)


@pytest.mark.django_db(transaction=True)
def test_aaggregate_respects_scope() -> None:
    from tests.testapp.models import Post

    post = _post()
    alice, bob = _user("alice"), _user("bob")
    _grant_owner(alice, post)

    async def count_for(actor) -> int:
        return (await Post.objects.as_user(actor).aaggregate(n=Count("pk")))["n"]

    assert asyncio.run(count_for(alice)) == 1
    # Regression: before the aggregate override this counted all rows for bob.
    assert asyncio.run(count_for(bob)) == 0


@pytest.mark.django_db
def test_aggregate_respects_scope_sync() -> None:
    from tests.testapp.models import Post

    post = _post()
    alice, bob = _user("alice"), _user("bob")
    _grant_owner(alice, post)

    assert Post.objects.as_user(alice).aggregate(n=Count("pk"))["n"] == 1
    # The sync hole aaggregate exposed: aggregate now scopes too.
    assert Post.objects.as_user(bob).aggregate(n=Count("pk"))["n"] == 0


@pytest.mark.django_db
def test_aggregate_raises_in_strict_mode_without_actor() -> None:
    from tests.testapp.models import Post

    _post()
    with pytest.raises(MissingActorError):
        Post.objects.aggregate(n=Count("pk"))


# ---------- async writes still enforce ----------


@pytest.mark.django_db(transaction=True)
def test_aupdate_denies_rows_actor_cannot_write() -> None:
    from tests.testapp.models import Post

    post = _post()
    alice, bob = _user("alice"), _user("bob")
    _grant_owner(alice, post)

    async def go() -> None:
        await Post.objects.as_user(bob).filter(pk=post.pk).aupdate(title="hijacked")

    with pytest.raises(PermissionDenied):
        asyncio.run(go())
    post.refresh_from_db()
    assert post.title != "hijacked"


@pytest.mark.django_db(transaction=True)
def test_acreate_enforced_in_async() -> None:
    from tests.testapp.models import Post

    # No actor in strict mode -> the pre_save check raises through the async path,
    # proving acreate does not bypass enforcement by running on the loop.
    async def create_no_actor() -> object:
        return await Post.objects.acreate(title="x")

    with pytest.raises(MissingActorError):
        asyncio.run(create_no_actor())

    # A sudo bracket carries into the async worker and lets the create through.
    async def create_under_sudo() -> object:
        with sudo(reason="async-create-test"):
            return await Post.objects.acreate(title="ok")

    created = asyncio.run(create_under_sudo())
    assert created.pk is not None
