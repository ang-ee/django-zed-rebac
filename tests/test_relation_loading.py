"""Permission-aware relation loading helpers."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import Prefetch

from rebac import (
    MissingActorError,
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    backend,
    sudo,
)
from rebac.backends import reset_backend
from rebac.evaluator import evaluator_scope
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


@pytest.fixture
def authored_folder(alice):
    from tests.testapp.models import AuthoredPost

    folder = _folder("root")
    _grant("blog/folder", folder.pk, "viewer", alice)
    posts = []
    for index in range(3):
        author = get_user_model().objects.create(username=f"author-{index}")
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


@pytest.mark.django_db
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
