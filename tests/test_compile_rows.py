"""Small sets of target rows: decided before a scope statement, witnessed inside it."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.compile import read
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

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
