"""Inline permission scopes follow live tuple and backing rows."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.testing import install_schema
from tests.testapp.models import (
    Folder,
    Post,
    TextIdentityFolder,
)

pytestmark = pytest.mark.django_db
ACTOR = SubjectRef.of("auth/user", "alice")


def visible(model):
    return set(model.objects.with_actor(ACTOR).values_list("pk", flat=True))


SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    permission read = viewer + parent->read
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
"""


@pytest.fixture
def tree():
    local = install_schema(SCHEMA)
    with sudo(reason="inline fixture"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
        leaf = Folder.objects.create(name="leaf", parent=child)
        post = Post.objects.create(title="leaf", folder=leaf)
    local.write_relationships([RelationshipTuple(to_object_ref(child), "viewer", ACTOR)])
    return local, child, leaf, post


def test_hierarchy_and_arrow_are_inline(tree):
    _local, child, leaf, post = tree
    scope = Post.objects.with_actor(ACTOR).scoped()
    with CaptureQueriesContext(connection) as queries:
        sql = str(scope.query)
    assert len(queries) == 0
    assert "SELECT" in sql
    assert set(Folder.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {child.pk, leaf.pk}
    assert list(scope.values_list("pk", flat=True)) == [post.pk]


def test_inline_hierarchy_respects_depth_limit(tree, settings):
    _local, child, leaf, _post = tree
    settings.REBAC_DEPTH_LIMIT = 0
    assert set(Folder.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {child.pk}
    settings.REBAC_DEPTH_LIMIT = 1
    assert set(Folder.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {child.pk, leaf.pk}


def test_tuple_write_is_visible_to_next_statement_in_evaluator_scope(tree):
    local, child, leaf, post = tree
    with evaluator_scope():
        assert list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == [post.pk]
        local.delete_relationship(RelationshipTuple(to_object_ref(child), "viewer", ACTOR))
        assert not Post.objects.with_actor(ACTOR).exists()
        local.write_relationships([RelationshipTuple(to_object_ref(leaf), "viewer", ACTOR)])
        assert list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == [post.pk]


def test_dangling_unconstrained_fk_does_not_grant(tree, monkeypatch):
    _local, child, leaf, post = tree
    monkeypatch.setattr(Post._meta.get_field("folder"), "db_constraint", False)
    with sudo(reason="unconstrained FK fixture"):
        child_post = Post.objects.create(title="child", folder=child)
    Folder._base_manager.filter(pk=leaf.pk)._raw_delete(connection.alias)
    try:
        assert set(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {child_post.pk}
    finally:
        Post._base_manager.filter(pk=post.pk)._raw_delete(connection.alias)


def test_dangling_unconstrained_self_fk_ends_hierarchy(tree, monkeypatch):
    _local, child, leaf, post = tree
    monkeypatch.setattr(Folder._meta.get_field("parent"), "db_constraint", False)
    assert set(Folder.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {
        child.pk,
        leaf.pk,
    }
    Folder._base_manager.filter(pk=child.pk)._raw_delete(connection.alias)
    try:
        assert not Folder.objects.with_actor(ACTOR).exists()
    finally:
        Post._base_manager.filter(pk=post.pk)._raw_delete(connection.alias)
        Folder._base_manager.filter(pk=leaf.pk).update(parent=None)


def test_text_identity_hierarchy_follows_rows_after_identity_moves(django_user_model):
    local = install_schema("""
        definition auth/user {}
        definition blog/textidentityfolder {
            relation parent: blog/textidentityfolder // rebac:field=parent
            relation viewer: auth/user
            permission read = viewer + parent->read
        }
    """)
    author = django_user_model.objects.create(username="inline-author")
    with sudo(reason="text identity fixture"):
        seed = TextIdentityFolder.objects.create(public_id="seed", name="seed", author=author)
        leaf = TextIdentityFolder.objects.create(
            public_id="leaf", name="leaf", author=author, parent=seed
        )
        other = TextIdentityFolder.objects.create(public_id="other", name="other", author=author)
    local.write_relationships([RelationshipTuple(to_object_ref(seed), "viewer", ACTOR)])
    assert set(TextIdentityFolder.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {
        seed.pk,
        leaf.pk,
    }
    with sudo(reason="text identity move"):
        TextIdentityFolder.objects.filter(pk=leaf.pk).update(public_id="renamed")
        TextIdentityFolder.objects.filter(pk=other.pk).update(public_id="leaf")
    assert set(TextIdentityFolder.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {
        seed.pk,
        leaf.pk,
    }


BANNED = """
definition auth/user {}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    relation banned: auth/user
    permission read = (viewer + parent->read) - banned
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
"""


@pytest.mark.parametrize("model", [Folder, Post])
def test_inline_exclusion_respects_depth_limit(settings, model):
    settings.REBAC_DEPTH_LIMIT = 1
    local = install_schema(BANNED)
    with sudo(reason="test.fixture"):
        chain = [Folder.objects.create(name="root")]
        for name in ("a", "b", "c"):
            chain.append(Folder.objects.create(name=name, parent=chain[-1]))
        posts = [Post.objects.create(title=folder.name, folder=folder) for folder in chain]
    local.write_relationships([RelationshipTuple(to_object_ref(chain[0]), "viewer", ACTOR)])
    rows = chain if model is Folder else posts
    within = {row.pk for row in rows[:2]}
    assert visible(model) == within


def test_rows_that_share_an_identity_are_all_read_inline(monkeypatch):
    # Nothing makes an identity attribute unique: two rows can answer to one.
    monkeypatch.setattr(Folder._meta, "rebac_id_attr", "name", raising=False)
    local = install_schema(SCHEMA)
    with sudo(reason="test.fixture"):
        first = Folder.objects.create(name="seed")
        second = Folder.objects.create(name="seed")
        under = Folder.objects.create(name="under", parent=second)
        Folder.objects.create(name="other")
    local.write_relationships(
        [RelationshipTuple(ObjectRef("blog/folder", "seed"), "viewer", ACTOR)]
    )
    assert visible(Folder) == {first.pk, second.pk, under.pk}
