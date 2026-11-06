"""Permission-aware relation loading helpers."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import F, Prefetch
from django.test import override_settings

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
from rebac.evaluator import evaluator_scope
from rebac.schema import parse_zed
from tests.backend_setup import atomic_source_write, install_schema

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
definition blog/authoredpost {
    relation owner: auth/user
    relation viewer: auth/user
    permission read = owner + viewer
    permission read__title = owner
}
"""


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


@pytest.fixture
def alice(db):
    from django.contrib.auth import get_user_model

    return atomic_source_write(get_user_model().objects.create, username="alice", is_active=True)


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
def test_relation_actor_public_result_is_actor_or_none(alice):
    from rebac.relation_loading import relation_actor
    from tests.testapp.models import Post

    ref = SubjectRef.of("auth/user", str(alice.pk))
    pinned = relation_actor(Post.objects.as_user(alice))
    assert isinstance(pinned, SubjectRef)
    assert pinned == ref
    with actor_context(ref):
        assert relation_actor(Post.objects.all()) == ref
        with sudo(reason="test.relation-actor"):
            assert relation_actor(Post.objects.all()) is None
            assert relation_actor(Post.objects.as_user(alice)) == ref


@pytest.mark.django_db
@pytest.mark.parametrize("strict", [True, False])
def test_relation_actor_default_manager_without_ambient_actor_is_none(strict):
    from rebac.relation_loading import relation_actor
    from tests.testapp.models import Post

    with override_settings(REBAC_STRICT_MODE=strict):
        assert relation_actor(Post.objects.all()) is None


@pytest.mark.django_db
@override_settings(REBAC_STRICT_MODE=False)
def test_related_managers_read_without_actor_in_non_strict_mode():
    from tests.testapp.models import Post

    folder = _folder("root")
    post = _post("child", folder=folder)
    with sudo(reason="test.fixture"):
        post.collections.add(folder)

    assert [row.pk for row in post.collections.all()] == [folder.pk]
    assert [row.pk for row in folder.posts.all()] == [post.pk]
    assert [row.pk for row in Post.objects.all()] == [post.pk]


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

    install_schema(
        backend(),
        parse_zed(
            SCHEMA_TEXT.replace(
                "permission read = owner + viewer",
                """
            permission read = owner + viewer
            permission read__name = owner
        """,
                1,
            )
        ),
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


def _lazy_guard_fixture(alice):
    """A post alice reads, in a folder only bob reads."""
    from django.contrib.auth import get_user_model

    bob = atomic_source_write(get_user_model().objects.create, username="bob", is_active=True)
    folder = _folder("bob's folder")
    post = _post("alice's post", folder=folder)
    _grant("blog/post", post.pk, "owner", alice)
    _grant("blog/folder", folder.pk, "viewer", bob)
    return bob, post


@pytest.mark.django_db
@pytest.mark.parametrize("consumer", ["none", "bob"])
def test_iterator_guards_selected_related_rows_with_the_actor_at_call_time(alice, consumer):
    from tests.testapp.models import Post

    bob, post = _lazy_guard_fixture(alice)
    with actor_context(SubjectRef.of("auth/user", str(alice.pk))):
        rows = Post.objects.rebac_select_related("folder").filter(pk=post.pk).iterator(chunk_size=1)
    # Consumed after the actor context closed (a streaming response), or under
    # another actor: the guard still answers for alice, who cannot read the folder.
    if consumer == "bob":
        with actor_context(SubjectRef.of("auth/user", str(bob.pk))):
            with pytest.raises(PermissionDenied):
                list(rows)
    else:
        with pytest.raises(PermissionDenied):
            list(rows)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("consumer", ["none", "bob"])
def test_aiterator_guards_selected_related_rows_with_the_actor_at_call_time(alice, consumer):
    from tests.testapp.models import Post

    bob, post = _lazy_guard_fixture(alice)
    with actor_context(SubjectRef.of("auth/user", str(alice.pk))):
        rows = Post.objects.rebac_select_related("folder").filter(pk=post.pk).aiterator()

    async def collect():
        return [row async for row in rows]

    if consumer == "bob":
        with actor_context(SubjectRef.of("auth/user", str(bob.pk))):
            with pytest.raises(PermissionDenied):
                asyncio.run(collect())
    else:
        with pytest.raises(PermissionDenied):
            asyncio.run(collect())


@pytest.mark.django_db
def test_iterator_consumed_after_its_actor_context_reads_the_pinned_scope(alice):
    from tests.testapp.models import Folder, Post

    folder = _folder("alice's folder")
    post = _post("alice's post", folder=folder)
    _grant("blog/post", post.pk, "owner", alice)
    _grant("blog/folder", folder.pk, "viewer", alice)
    with actor_context(SubjectRef.of("auth/user", str(alice.pk))):
        rows = Post.objects.rebac_select_related("folder").filter(pk=post.pk).iterator(chunk_size=1)
    [row] = list(rows)  # No MissingActorError mid-stream.
    assert isinstance(row.folder, Folder) and row.folder.pk == folder.pk


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

    install_schema(
        backend(),
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
        ),
    )
    folder = _folder("globally readable")
    post = _post("visible", folder=folder)
    _grant("blog/post", post.pk, "viewer", alice)

    row = Post.objects.as_user(alice).rebac_select_related("folder").get(pk=post.pk)

    assert row.folder.name == "globally readable"


@pytest.mark.django_db
def test_pinned_actor_select_related_still_guards_inside_ambient_sudo(alice):
    from tests.testapp.models import Post

    folder = _folder("private")
    post = _post("visible", folder=folder)
    _grant("blog/post", post.pk, "viewer", alice)

    with sudo(reason="test.ambient"):
        with pytest.raises(PermissionDenied):
            Post.objects.as_user(alice).rebac_select_related("folder").get(pk=post.pk)


@pytest.mark.django_db
def test_ambient_sudo_bypasses_selected_related_guard(alice):
    from tests.testapp.models import Post

    folder = _folder("private")
    post = _post("visible", folder=folder)
    with actor_context(SubjectRef.of("auth/user", str(alice.pk))):
        queryset = Post.objects.rebac_select_related("folder")
        with sudo(reason="test.ambient-related"):
            row = queryset.get(pk=post.pk)
            projected = list(queryset.filter(pk=post.pk).values_list("folder__name", flat=True))

    assert row.folder.name == "private"
    assert projected == ["private"]


@pytest.mark.django_db
def test_select_related_without_actor_raises_in_non_strict_mode():
    from tests.testapp.models import Post

    folder = _folder("private")
    post = _post("visible", folder=folder)
    with override_settings(REBAC_STRICT_MODE=False):
        with pytest.raises(MissingActorError):
            Post.objects.rebac_select_related("folder").get(pk=post.pk)


@pytest.mark.django_db
def test_select_related_projection_without_actor_raises_in_non_strict_mode():
    from tests.testapp.models import Post

    folder = _folder("private")
    post = _post("visible", folder=folder)
    with override_settings(REBAC_STRICT_MODE=False):
        with pytest.raises(PermissionDenied):
            list(
                Post.objects.rebac_select_related("folder")
                .filter(pk=post.pk)
                .values("folder__name")
            )


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
def test_ambient_sudo_bypasses_prefetched_related_scope(alice):
    from tests.testapp.models import Folder

    folder = _folder("root")
    visible = _post("visible", folder=folder)
    _post("hidden", folder=folder)
    _grant("blog/folder", folder.pk, "viewer", alice)
    _grant("blog/post", visible.pk, "viewer", alice)

    with actor_context(SubjectRef.of("auth/user", str(alice.pk))):
        queryset = Folder.objects.rebac_prefetch_related("posts")
        with sudo(reason="test.ambient-prefetch"):
            row = queryset.get(pk=folder.pk)
            titles = sorted(post.title for post in row.posts.all())

    assert titles == ["hidden", "visible"]


@pytest.mark.django_db
def test_pinned_actor_keeps_prefetched_scope_inside_ambient_sudo(alice):
    from tests.testapp.models import Folder

    folder = _folder("root")
    visible = _post("visible", folder=folder)
    _post("hidden", folder=folder)
    _grant("blog/folder", folder.pk, "viewer", alice)
    _grant("blog/post", visible.pk, "viewer", alice)

    with sudo(reason="test.ambient-prefetch"):
        row = Folder.objects.as_user(alice).rebac_prefetch_related("posts").get(pk=folder.pk)

    assert [post.title for post in row.posts.all()] == ["visible"]


@pytest.mark.django_db
def test_actor_pinned_after_prefetch_scopes_related_rows_inside_ambient_sudo(alice):
    from tests.testapp.models import Folder

    bob = atomic_source_write(get_user_model().objects.create, username="bob", is_active=True)
    folder = _folder("root")
    visible = _post("visible", folder=folder)
    hidden = _post("hidden", folder=folder)
    _grant("blog/folder", folder.pk, "viewer", alice)
    _grant("blog/post", visible.pk, "viewer", alice)
    _grant("blog/post", hidden.pk, "viewer", bob)

    with actor_context(SubjectRef.of("auth/user", str(bob.pk))):
        queryset = Folder.objects.rebac_prefetch_related("posts").as_user(alice)
        with sudo(reason="test.ambient-prefetch"):
            row = queryset.get(pk=folder.pk)

    assert [post.title for post in row.posts.all()] == ["visible"]


def _split_related_posts(alice):
    bob = atomic_source_write(get_user_model().objects.create, username="bob", is_active=True)
    folder = _folder("root")
    alice_post = _post("alice post", folder=folder)
    bob_post = _post("bob post", folder=folder)
    _grant("blog/folder", folder.pk, "viewer", alice)
    _grant("blog/post", alice_post.pk, "viewer", alice)
    _grant("blog/post", bob_post.pk, "viewer", bob)
    return bob, folder, alice_post, bob_post


@pytest.mark.django_db
def test_prefetch_does_not_transfer_instance_hint_authority_to_reused_queryset(alice):
    from tests.testapp.models import Folder, Post

    bob, folder, _alice_post, bob_post = _split_related_posts(alice)
    posts_qs = Post.objects.all()
    with actor_context(bob):
        list(
            Folder.objects.as_user(alice)
            .filter(pk=folder.pk)
            .prefetch_related(Prefetch("posts", queryset=posts_qs))
        )
        assert [post.pk for post in posts_qs] == [bob_post.pk]
        with sudo(reason="test.hint-bypass"):
            assert {post.pk for post in posts_qs.all()} == {
                _alice_post.pk,
                bob_post.pk,
            }


@pytest.mark.django_db
def test_db_manager_instance_hint_does_not_pin_actor(alice):
    from tests.testapp.models import Folder, Post

    bob, folder, _alice_post, bob_post = _split_related_posts(alice)
    pinned_folder = Folder.objects.as_user(alice).get(pk=folder.pk)
    hinted = Post.objects.db_manager(hints={"instance": pinned_folder}).all()

    with actor_context(bob):
        assert list(hinted.values_list("pk", flat=True)) == [bob_post.pk]


@pytest.mark.django_db
def test_plain_prefetched_related_manager_uses_root_instance_pin(alice):
    from tests.testapp.models import Folder

    bob, folder, alice_post, _bob_post = _split_related_posts(alice)
    with actor_context(bob):
        [loaded] = list(
            Folder.objects.as_user(alice).filter(pk=folder.pk).prefetch_related("posts")
        )
        assert [post.pk for post in loaded.posts.all()] == [alice_post.pk]


@pytest.mark.django_db
@override_settings(REBAC_STRICT_MODE=True)
def test_iterator_prefetched_related_manager_uses_pinned_root_actor(alice):
    from tests.testapp.models import Folder

    bob, folder, alice_post, _bob_post = _split_related_posts(alice)
    with actor_context(bob):
        rows = list(
            Folder.objects.as_user(alice)
            .filter(pk=folder.pk)
            .prefetch_related("posts")
            .iterator(chunk_size=1)
        )
        assert [post.pk for post in rows[0].posts.all()] == [alice_post.pk]


@pytest.mark.django_db
def test_iterator_prefetches_one_chunk_at_a_time():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from tests.testapp.models import Folder, Post

    folders = [_folder(f"chunk {index}") for index in range(3)]
    for folder in folders:
        _post(f"post in {folder.name}", folder=folder)
    posts = connection.ops.quote_name(Post._meta.db_table)
    with sudo(reason="test.iterator-chunks"):
        rows = (
            Folder.objects.filter(pk__in=[folder.pk for folder in folders])
            .order_by("pk")
            .prefetch_related("posts")
            .iterator(chunk_size=1)
        )
        with CaptureQueriesContext(connection) as first:
            next(rows)
        with CaptureQueriesContext(connection) as rest:
            assert len(list(rows)) == 2
    assert sum(posts in query["sql"] for query in first) == 1
    assert sum(posts in query["sql"] for query in rest) == 2


@pytest.mark.django_db(transaction=True)
@override_settings(REBAC_STRICT_MODE=True)
def test_aiterator_prefetched_related_manager_uses_pinned_root_actor(alice):
    from tests.testapp.models import Folder

    bob, folder, alice_post, _bob_post = _split_related_posts(alice)
    queryset = Folder.objects.as_user(alice).filter(pk=folder.pk).prefetch_related("posts")

    async def collect():
        return [row async for row in queryset.aiterator(chunk_size=1)]

    with actor_context(bob):
        rows = asyncio.run(collect())
        assert [post.pk for post in rows[0].posts.all()] == [alice_post.pk]


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


@pytest.fixture
def authored_folder(alice):
    from tests.testapp.models import AuthoredPost

    folder = _folder("root")
    _grant("blog/folder", folder.pk, "viewer", alice)
    posts = []
    for index in range(3):
        author = atomic_source_write(get_user_model().objects.create, username=f"author-{index}")
        author.groups.add(Group.objects.create(name=f"group-{index}"))
        with sudo(reason="test.fixture"):
            post = AuthoredPost.objects.create(title=f"post-{index}", folder=folder, author=author)
        if index < 2:
            _grant("blog/authoredpost", post.pk, "viewer", alice)
            posts.append(post)
    return folder, posts


@pytest.mark.django_db
@pytest.mark.parametrize("tail", ["author", "author__groups"])
def test_rebac_prefetch_preserves_unprotected_tail(
    alice, authored_folder, tail, django_assert_num_queries
):
    from tests.testapp.models import Folder

    folder, visible = authored_folder
    row = (
        Folder.objects.as_user(alice)
        .rebac_prefetch_related(f"authored_posts__{tail}")
        .get(pk=folder.pk)
    )

    with django_assert_num_queries(0):
        posts = sorted(row.authored_posts.all(), key=lambda post: post.pk)
        assert [post.pk for post in posts] == [post.pk for post in visible]
        assert [post.author.username for post in posts] == ["author-0", "author-1"]
        if tail.endswith("groups"):
            assert [[group.name for group in post.author.groups.all()] for post in posts] == [
                ["group-0"],
                ["group-1"],
            ]


@pytest.mark.django_db
def test_rebac_prefetch_preserves_unprotected_custom_queryset_and_to_attr(
    alice, authored_folder, django_assert_num_queries
):
    from tests.testapp.models import Folder

    folder, _ = authored_folder
    author_queryset = get_user_model().objects.filter(username="author-0")
    lookup = Prefetch("authored_posts__author", queryset=author_queryset, to_attr="selected_author")
    row = Folder.objects.as_user(alice).rebac_prefetch_related(lookup).get(pk=folder.pk)

    assert lookup.queryset is author_queryset
    assert lookup.prefetch_to == "authored_posts__selected_author"
    with django_assert_num_queries(0):
        posts = sorted(row.authored_posts.all(), key=lambda post: post.pk)
        assert posts[0].selected_author.username == "author-0"
        assert posts[1].selected_author is None


@pytest.mark.django_db
def test_rebac_prefetch_protected_terminal_keeps_scoped_custom_queryset(
    alice, authored_folder, django_assert_num_queries
):
    from tests.testapp.models import AuthoredPost, Folder

    folder, visible = authored_folder
    queryset = AuthoredPost.objects.filter(title__in=["post-0", "post-2"])
    lookup = Prefetch("authored_posts", queryset=queryset, to_attr="selected_posts")
    row = Folder.objects.as_user(alice).rebac_prefetch_related(lookup).get(pk=folder.pk)

    assert lookup.queryset is queryset
    assert queryset.actor() is None
    with django_assert_num_queries(0):
        assert [post.pk for post in row.selected_posts] == [visible[0].pk]
        assert row.selected_posts[0].actor() == SubjectRef.of("auth/user", str(alice.pk))


@pytest.mark.django_db(transaction=True)
def test_rebac_prefetch_tail_preserves_each_protected_prefix_and_field_gate(
    alice, authored_folder, django_assert_num_queries
):
    from tests.testapp.models import AuthoredPost, Folder

    root = _folder("outer root")
    child, visible = authored_folder
    hidden_child = _folder("denied child")
    with sudo(reason="test.fixture"):
        child.parent = root
        child.save()
        hidden_child.parent = root
        hidden_child.save()
        hidden_path_post = AuthoredPost.objects.create(
            title="readable behind denied folder", folder=hidden_child, author=alice
        )
    _grant("blog/folder", root.pk, "viewer", alice)
    _grant("blog/authoredpost", hidden_path_post.pk, "viewer", alice)

    # Permission decisions deliberately bypass caches inside transactions. Use
    # native autocommit to measure the prefetched graph within one request scope.
    with evaluator_scope():
        row = (
            Folder.objects.as_user(alice)
            .on_field_deny("redact")
            .rebac_prefetch_related("children__authored_posts__author__groups")
            .get(pk=root.pk)
        )
        with django_assert_num_queries(0):
            children = list(row.children.all())
            assert [folder.pk for folder in children] == [child.pk]
            posts = sorted(children[0].authored_posts.all(), key=lambda post: post.pk)
            assert [post.pk for post in posts] == [post.pk for post in visible]
            assert [post.title for post in posts] == [None, None]
            assert [post.actor() for post in posts] == [
                SubjectRef.of("auth/user", str(alice.pk))
            ] * 2
            assert [[group.name for group in post.author.groups.all()] for post in posts] == [
                ["group-0"],
                ["group-1"],
            ]


@pytest.mark.django_db
def test_rebac_prefetch_unprotected_tail_does_not_inherit_root_sudo(authored_folder):
    from tests.testapp.models import Folder

    folder, _ = authored_folder
    with pytest.raises(MissingActorError):
        (
            Folder.objects.sudo(reason="test.root-only")
            .rebac_prefetch_related("authored_posts__author")
            .get(pk=folder.pk)
        )


@pytest.mark.django_db
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_materialized_field_visibility_checks_only_fetched_ids(
    alice, monkeypatch, settings, storage
):
    from tests.testapp.models import AuthoredPost

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA_TEXT))
    with sudo(reason="test.fixture"):
        readable = AuthoredPost.objects.create(title="visible", author=alice)
        denied = AuthoredPost.objects.create(title="private", author=alice)
        unrelated = AuthoredPost.objects.create(title="not fetched", author=alice)
    for post in (readable, denied, unrelated):
        _grant("blog/authoredpost", post.pk, "viewer", alice)
    for post in (readable, unrelated):
        _grant("blog/authoredpost", post.pk, "owner", alice)
    active = backend()
    original = active.held_many
    requested = []

    def membership(**kwargs):
        requested.append((tuple(kwargs["actions"]), tuple(kwargs["ids"])))
        return original(**kwargs)

    monkeypatch.setattr(active, "held_many", membership)
    monkeypatch.setattr(active, "accessible", lambda **kwargs: pytest.fail("unbounded enumeration"))
    monkeypatch.setattr(
        active, "grants_all", lambda **kwargs: pytest.fail("bounded rows need no universal proof")
    )
    rows = list(
        AuthoredPost.objects.as_user(alice)
        .on_field_deny("redact")
        .filter(pk__in=(readable.pk, denied.pk))
    )
    assert {row.pk: row.title for row in rows} == {readable.pk: "visible", denied.pk: None}
    assert requested == [(("read__title",), (str(readable.pk), str(denied.pk)))]
