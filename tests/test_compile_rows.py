"""Bounded row decisions and inline overflow follow live permission data."""

import pytest
from django.db import connection, models
from django.test.utils import CaptureQueriesContext, isolate_apps

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.compile import read
from rebac.field_backing import foreign_key_kept, resolve_field_backing
from rebac.schema import parse_zed
from rebac.testing import install_schema
from tests.testapp.models import (
    Folder,
    NativeParentLinkedChild,
    NativeParentLinkedResource,
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


def test_hierarchy_and_arrow_scope_is_prepared(tree):
    _local, child, leaf, post = tree
    scope = Post.objects.with_actor(ACTOR).scoped()
    with CaptureQueriesContext(connection) as queries:
        sql = str(scope.query)
    assert len(queries) == 0
    assert "SELECT" in sql
    assert set(Folder.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {child.pk, leaf.pk}
    assert list(scope.values_list("pk", flat=True)) == [post.pk]


def test_small_hierarchy_binds_ids_and_keeps_statement_bounded(tree):
    _local, child, leaf, post = tree
    with CaptureQueriesContext(connection) as queries:
        assert list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == [post.pk]
    sql = queries[-1]["sql"]
    assert f'"folder_id" IN ({child.pk}, {leaf.pk})' in sql
    assert len(sql) < 25_000


def test_overflow_keeps_inline_fallback(tree, monkeypatch):
    _local, child, leaf, post = tree
    monkeypatch.setattr(read, "_ROW_LIMIT", 1)
    with CaptureQueriesContext(connection) as queries:
        assert list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == [post.pk]
    assert f'"folder_id" IN ({child.pk}, {leaf.pk})' not in queries[-1]["sql"]


def test_evaluator_reuses_decided_rows_without_a_second_read(tree):
    _local, _child, _leaf, post = tree
    with evaluator_scope():
        with CaptureQueriesContext(connection) as first:
            assert list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == [post.pk]
        with CaptureQueriesContext(connection) as second:
            assert list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == [post.pk]
    assert len(first) > 1
    assert len(second) == 1


def test_owned_model_write_invalidates_decided_rows(tree):
    _local, child, _leaf, post = tree
    with sudo(reason="owned row generation fixture"):
        other = Folder.objects.create(name="other")
        new_post = Post.objects.create(title="other", folder=other)
    with evaluator_scope():
        assert set(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {post.pk}
        with sudo(reason="owned row generation update"):
            Folder.objects.filter(pk=other.pk).update(parent=child)
        assert set(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {
            post.pk,
            new_post.pk,
        }


def test_revocation_after_decision_fails_witness(tree, monkeypatch):
    local, child, _leaf, _post = tree
    original = read._Rows._decide
    changed = []

    def decide_then_revoke(self, key):
        found = original(self, key)
        if key == ("blog/folder", "read") and not changed:
            changed.append(True)
            local.delete_relationship(RelationshipTuple(to_object_ref(child), "viewer", ACTOR))
        return found

    monkeypatch.setattr(read._Rows, "_decide", decide_then_revoke)
    assert not Post.objects.with_actor(ACTOR).exists()
    assert changed


def test_raw_sql_write_in_evaluator_scope_hides_and_never_grants(tree):
    _local, child, leaf, post = tree
    with sudo(reason="raw SQL witness fixture"):
        other = Folder.objects.create(name="other")
        new_post = Post.objects.create(title="new", folder=other)
    with evaluator_scope():
        assert set(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {post.pk}
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE "testapp_folder" SET "parent_id" = %s WHERE "id" = %s',
                [other.pk, leaf.pk],
            )
        # The old set is still bound, but its witness re-reads the moved row.
        assert not Post.objects.with_actor(ACTOR).exists()
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE "testapp_folder" SET "parent_id" = %s WHERE "id" = %s',
                [child.pk, other.pk],
            )
        # A new member cannot appear in a previously decided lower bound.
        assert new_post.pk not in set(Post.objects.with_actor(ACTOR).values_list("pk", flat=True))


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


BACKED = """
definition auth/user {}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation child: blog/folder // rebac:field=children
    relation viewer: auth/user
    permission read = viewer + parent->read
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation top: blog/folder // rebac:field=folder__parent
    relation shelf: blog/folder // rebac:field=collections
    permission read = folder->read
}
definition test/nativeparentlinkedresource {}
definition test/nativeparentlinkedchild {
    relation viewer: auth/user
    permission read = viewer
}
definition test/nativeparentlinkedrecord {
    relation child: test/nativeparentlinkedchild // rebac:field=child
    permission read = child->read
}
"""


def backing(schema, type_, name):
    definition = schema.get_definition(type_)
    relation = next(r for r in definition.relations if r.name == name)
    return resolve_field_backing(definition, relation)


def test_only_a_constrained_forward_foreign_key_proves_its_target(monkeypatch):
    schema = parse_zed(BACKED)
    forward = backing(schema, "blog/post", "folder")
    assert forward.keeps_target("default")
    # Django may read a reverse path without the target's table.
    assert not backing(schema, "blog/folder", "child").keeps_target("default")
    # A multi-table row is its own and its parent's: both links must be kept.
    record = backing(schema, "test/nativeparentlinkedrecord", "child")
    assert record.keeps_target("default")
    link = NativeParentLinkedChild._meta.parents[NativeParentLinkedResource]
    monkeypatch.setattr(link, "db_constraint", False)
    assert not record.keeps_target("default")
    unconstrained(monkeypatch, Post, "folder")
    assert not forward.keeps_target("default")
    monkeypatch.undo()
    # A constraint is taken to exist only on a table Django manages.
    monkeypatch.setattr(Post._meta, "managed", False)
    assert not forward.keeps_target("default")
    monkeypatch.undo()
    monkeypatch.setattr(connection.features, "supports_foreign_keys", False)
    assert not forward.keeps_target("default")
    monkeypatch.undo()
    # Every forward key on a longer path must be kept, not only the last.
    longer = backing(schema, "blog/post", "top")
    assert longer.keeps_target("default")
    unconstrained(monkeypatch, Post, "folder")
    assert not longer.keeps_target("default")
    monkeypatch.undo()
    assert not backing(schema, "blog/post", "shelf").keeps_target("default")


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


@isolate_apps("tests.testapp")
def test_a_reference_is_kept_only_when_every_ancestor_link_is(monkeypatch):
    class Base(models.Model):
        class Meta:
            app_label = "testapp"

    class Middle(Base):
        class Meta:
            app_label = "testapp"

    class Leaf(Middle):
        class Meta:
            app_label = "testapp"

    class LeafProxy(Leaf):
        class Meta:
            app_label = "testapp"
            proxy = True

    class Carrier(models.Model):
        leaf = models.ForeignKey(Leaf, on_delete=models.CASCADE)
        proxy = models.ForeignKey(LeafProxy, on_delete=models.CASCADE, related_name="+")

        class Meta:
            app_label = "testapp"

    fields = [Carrier._meta.get_field("leaf"), Carrier._meta.get_field("proxy")]
    assert all(foreign_key_kept(field, "default") for field in fields)
    # A leaf's row is its own, its parent's and its grandparent's together.
    for child, parent in ((Leaf, Middle), (Middle, Base)):
        with monkeypatch.context() as patched:
            patched.setattr(child._meta.parents[parent], "db_constraint", False)
            assert not any(foreign_key_kept(field, "default") for field in fields)
    assert all(foreign_key_kept(field, "default") for field in fields)


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


def unconstrained(monkeypatch, model, name):
    """Treat the foreign key as one the database does not constrain."""
    monkeypatch.setattr(model._meta.get_field(name), "db_constraint", False)
