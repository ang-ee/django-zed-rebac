"""Small sets of target rows: decided before a scope statement, witnessed inside it."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.compile import read
from rebac.field_backing import resolve_field_backing
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

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    permission read = viewer + parent->read
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation viewer: auth/user
    permission read = folder->read + viewer
}
"""
ALICE = SubjectRef.of("auth/user", "alice")


@pytest.fixture
def tree():
    local = install_schema(SCHEMA)
    with sudo(reason="test.fixture"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
        leaf = Folder.objects.create(name="leaf", parent=child)
        other = Folder.objects.create(name="other")
        posts = {
            folder.name: Post.objects.create(title=folder.name, folder=folder)
            for folder in (root, child, leaf, other)
        }
    grant = RelationshipTuple(to_object_ref(child), "viewer", ALICE)
    local.write_relationships([grant])
    return local, {"root": root, "child": child, "leaf": leaf, "other": other}, posts, grant


def visible(model):
    return set(model.objects.with_actor(ALICE).values_list("pk", flat=True))


def test_a_hierarchy_is_followed_from_its_seeds_and_bound_as_keys(tree):
    _local, folders, posts, _grant = tree
    with CaptureQueriesContext(connection) as queries:
        assert visible(Folder) == {folders["child"].pk, folders["leaf"].pk}
    scope = queries[-1]["sql"]
    # The scope names the two folders; it does not walk every row's ancestors.
    assert f"IN ({folders['child'].pk}, {folders['leaf'].pk})" in scope
    assert scope.count('JOIN "testapp_folder"') == 0
    with CaptureQueriesContext(connection) as queries:
        assert visible(Post) == {posts["child"].pk, posts["leaf"].pk}
    assert f'"folder_id" IN ({folders["child"].pk}, {folders["leaf"].pk})' in queries[-1]["sql"]


def test_a_set_over_the_limit_stays_inside_the_statement(tree, monkeypatch):
    _local, folders, posts, _grant = tree
    monkeypatch.setattr(read, "_ROW_LIMIT", 1)
    with CaptureQueriesContext(connection) as queries:
        assert visible(Folder) == {folders["child"].pk, folders["leaf"].pk}
        assert visible(Post) == {posts["child"].pk, posts["leaf"].pk}
    assert f"IN ({folders['child'].pk}, {folders['leaf'].pk})" not in queries[-1]["sql"]


def test_a_decided_set_follows_the_depth_limit(tree, settings):
    _local, folders, _posts, _grant = tree
    settings.REBAC_DEPTH_LIMIT = 0
    assert visible(Folder) == {folders["child"].pk}
    settings.REBAC_DEPTH_LIMIT = 1
    assert visible(Folder) == {folders["child"].pk, folders["leaf"].pk}


def after_deciding(monkeypatch, key, change):
    """Run ``change`` once, right after the decision for ``key`` was made."""
    decide = read._Rows._decide
    done = []

    def decide_then_change(self, decided_key):
        decision = decide(self, decided_key)
        if decided_key == key and not done:
            done.append(True)
            change()
        return decision

    monkeypatch.setattr(read._Rows, "_decide", decide_then_change)
    return done


def test_a_grant_revoked_after_the_decision_is_not_read_through(tree, monkeypatch):
    local, _folders, _posts, grant = tree
    done = after_deciding(
        monkeypatch, ("blog/folder", "read"), lambda: local.delete_relationship(grant)
    )
    # The folders were decided while the grant stood; the statement runs after it.
    assert visible(Post) == set()
    assert done
    assert visible(Post) == set()


def test_a_row_moved_out_of_a_decided_hierarchy_is_not_read_through(tree, monkeypatch):
    _local, folders, posts, _grant = tree

    def move():
        Folder._base_manager.filter(pk=folders["leaf"].pk).update(parent=folders["other"])

    done = after_deciding(monkeypatch, ("blog/folder", "read"), move)
    assert visible(Post) == set()
    assert done
    # The next operation decides afresh.
    assert visible(Post) == {posts["child"].pk}
    assert visible(Folder) == {folders["child"].pk}


@pytest.mark.parametrize("model", [Folder, Post])
def test_rows_closed_into_a_cycle_after_the_decision_are_not_read_through(tree, monkeypatch, model):
    _local, folders, posts, _grant = tree
    with sudo(reason="test.fixture"):
        deep = Folder.objects.create(name="deep", parent=folders["leaf"])
        Post.objects.create(title="deep", folder=deep)

    def close():
        # leaf and deep now hang under each other, and under no granted folder.
        Folder._base_manager.filter(pk=folders["leaf"].pk).update(parent=deep)

    still = {folders["child"].pk} if model is Folder else {posts["child"].pk}
    done = after_deciding(monkeypatch, ("blog/folder", "read"), close)
    assert visible(model) <= still
    assert done
    assert visible(model) == still


def test_a_row_moved_deeper_after_the_decision_stays_within_the_depth_limit(
    tree, monkeypatch, settings
):
    _local, folders, _posts, _grant = tree
    settings.REBAC_DEPTH_LIMIT = 1
    with sudo(reason="test.fixture"):
        twin = Folder.objects.create(name="twin", parent=folders["child"])

    def deepen():
        Folder._base_manager.filter(pk=twin.pk).update(parent=folders["leaf"])

    within = {folders["child"].pk, folders["leaf"].pk}
    done = after_deciding(monkeypatch, ("blog/folder", "read"), deepen)
    assert visible(Folder) <= within
    assert done
    assert visible(Folder) == within


def test_a_row_moved_under_another_row_of_the_level_above_is_still_read(tree, monkeypatch):
    local, folders, _posts, _grant = tree
    local.write_relationships([RelationshipTuple(to_object_ref(folders["other"]), "viewer", ALICE)])

    def move():
        Folder._base_manager.filter(pk=folders["leaf"].pk).update(parent=folders["other"])

    done = after_deciding(monkeypatch, ("blog/folder", "read"), move)
    # leaf still hangs one level under a folder that holds the grant.
    assert visible(Folder) == {folders["child"].pk, folders["other"].pk, folders["leaf"].pk}
    assert done


def test_the_witness_of_a_hierarchy_names_each_level(tree):
    _local, folders, _posts, _grant = tree
    with sudo(reason="test.fixture"):
        Folder.objects.create(name="deep", parent=folders["leaf"])
    with CaptureQueriesContext(connection) as queries:
        visible(Folder)
    scope = queries[-1]["sql"]
    # One clause for the seeds and one per level below them: a row is read
    # only while it hangs under a row of the level above its own.
    assert scope.count('"parent_id" IN (') == 2
    assert f'"parent_id" IN ({folders["child"].pk})' in scope
    assert f'"parent_id" IN ({folders["leaf"].pk})' in scope


NAMED = """
definition auth/user {}
definition blog/textidentityfolder {
    relation parent: blog/textidentityfolder // rebac:field=parent
    relation viewer: auth/user
    permission read = viewer + parent->read
}
"""


def test_an_identity_that_moved_to_another_row_is_not_read_through(monkeypatch):
    # The levels are kept by key and the decision is used by identity.
    local = install_schema(NAMED)
    author = get_user_model().objects.create(username="author")
    with sudo(reason="test.fixture"):
        seed = TextIdentityFolder.objects.create(public_id="seed", name="seed", author=author)
        leaf = TextIdentityFolder.objects.create(
            public_id="leaf", name="leaf", author=author, parent=seed
        )
        other = TextIdentityFolder.objects.create(public_id="other", name="other", author=author)
    local.write_relationships([RelationshipTuple(to_object_ref(seed), "viewer", ALICE)])
    assert visible(TextIdentityFolder) == {seed.pk, leaf.pk}

    def rename():
        rows = TextIdentityFolder._base_manager
        rows.filter(pk=leaf.pk).update(public_id="renamed")
        rows.filter(pk=other.pk).update(public_id="leaf")

    done = after_deciding(monkeypatch, ("blog/textidentityfolder", "read"), rename)
    # ``other`` now answers to the identity that was decided for ``leaf``.
    assert visible(TextIdentityFolder) <= {seed.pk, leaf.pk}
    assert done
    assert visible(TextIdentityFolder) == {seed.pk, leaf.pk}


EXPIRING = {
    "hierarchy": """
        use expiration
        definition auth/user {}
        definition blog/folder {
            relation parent: blog/folder // rebac:field=parent
            relation viewer: auth/user with expiration
            permission read = viewer + parent->read
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->read
        }
    """,
    "arrow": """
        use expiration
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user with expiration
            permission read = viewer
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->read
        }
    """,
}


@pytest.mark.parametrize("shape", sorted(EXPIRING))
def test_a_grant_that_expires_after_the_decision_is_not_read_through(monkeypatch, shape):
    clock = {"now": timezone.now()}
    monkeypatch.setattr(timezone, "now", lambda: clock["now"])
    local = install_schema(EXPIRING[shape])
    with sudo(reason="test.fixture"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
        post = Post.objects.create(title="post", folder=child)
    expiry = clock["now"] + timedelta(minutes=1)
    local.write_relationships(
        [RelationshipTuple(to_object_ref(child), "viewer", ALICE, expires_at=expiry)]
    )
    assert visible(Post) == {post.pk}

    def tick():
        clock["now"] = expiry + timedelta(seconds=1)

    done = after_deciding(monkeypatch, ("blog/folder", "read"), tick)
    # The witness is read at the statement's instant, not at the decision's.
    assert visible(Post) == set()
    assert done
    assert visible(Post) == set()


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
    monkeypatch.setattr(connection.features, "supports_foreign_keys", False)
    assert not forward.keeps_target("default")


def unconstrained(monkeypatch, model, name):
    """Treat the foreign key as one the database does not constrain."""
    monkeypatch.setattr(model._meta.get_field(name), "db_constraint", False)


def test_keys_behind_an_unconstrained_reference_are_read_from_the_target_rows(tree, monkeypatch):
    _local, folders, posts, _grant = tree
    unconstrained(monkeypatch, Post, "folder")

    def remove():
        # The row goes and the post keeps naming it: nothing constrains the column.
        Folder._base_manager.filter(pk=folders["leaf"].pk)._raw_delete(connection.alias)

    done = after_deciding(monkeypatch, ("blog/folder", "read"), remove)
    try:
        with CaptureQueriesContext(connection) as queries:
            assert visible(Post) == {posts["child"].pk}
        assert done
        assert (
            f'"folder_id" IN ({folders["child"].pk}, {folders["leaf"].pk})'
            not in (queries[-1]["sql"])
        )
    finally:
        Post._base_manager.filter(pk=posts["leaf"].pk)._raw_delete(connection.alias)


def test_a_hierarchy_over_an_unconstrained_parent_is_not_followed_from_its_seeds(tree, monkeypatch):
    _local, folders, posts, _grant = tree
    unconstrained(monkeypatch, Folder, "parent")
    assert visible(Folder) == {folders["child"].pk, folders["leaf"].pk}

    def remove():
        Folder._base_manager.filter(pk=folders["child"].pk)._raw_delete(connection.alias)

    done = after_deciding(monkeypatch, ("blog/folder", "read"), remove)
    try:
        # leaf names a parent that is gone: it inherits nothing.
        assert visible(Folder) == set()
        assert done
        assert visible(Folder) == set()
    finally:
        Post._base_manager.filter(pk=posts["child"].pk)._raw_delete(connection.alias)
        Folder._base_manager.filter(pk=folders["leaf"].pk).update(parent=None)


def test_point_checks_do_not_decide_sets(tree):
    local, folders, _posts, _grant = tree
    with CaptureQueriesContext(connection) as queries:
        assert local.check_access(
            subject=ALICE, action="read", resource=to_object_ref(folders["leaf"])
        ).allowed
    assert len(queries) == 1
    assert not local.check_access(
        subject=ALICE, action="read", resource=ObjectRef("blog/folder", str(folders["root"].pk))
    ).allowed
