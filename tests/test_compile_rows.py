"""Small sets of target rows: decided before a scope statement, witnessed inside it."""

import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, models
from django.test.utils import CaptureQueriesContext, isolate_apps
from django.utils import timezone

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.compile import read
from rebac.field_backing import foreign_key_kept, resolve_field_backing
from rebac.schema import parse_zed
from rebac.testing import install_schema
from tests.testapp.models import (
    CodedFolder,
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


TWO_ARROWS = """
definition auth/user {}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    permission read = viewer + parent->read
    permission open = viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read + folder->open
}
"""


def test_the_row_limit_is_what_one_statement_may_bind(tree, monkeypatch):
    _local, _folders, posts, grant = tree
    local = install_schema(TWO_ARROWS)
    local.write_relationships([grant])
    held = {posts["child"].pk, posts["leaf"].pk}
    bound = []
    for limit in (3, 2):
        monkeypatch.setattr(read, "_ROW_LIMIT", limit)
        with CaptureQueriesContext(connection) as queries:
            assert visible(Post) == held
        bound.append(len(re.findall(r'"folder_id" IN \(\d', queries[-1]["sql"])))
    # Two folders hold ``read`` and one holds ``open``: three rows fit in
    # three, and with two the second set stays a subquery.
    assert bound == [2, 1]


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
    # The witness fails, so the statement selects nothing.
    assert visible(model) == set()
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
    assert visible(Folder) == set()
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
    assert visible(TextIdentityFolder) == set()
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
def test_a_decided_set_is_not_used_inside_its_own_recursion(settings, monkeypatch, model):
    # The exclusion keeps ``read`` out of the self-foreign-key form: its rows
    # are decided by the predicate, to the depth limit.  Bound at the
    # permission's own ``parent->read`` they would add a level to that limit.
    settings.REBAC_DEPTH_LIMIT = 1
    local = install_schema(BANNED)
    with sudo(reason="test.fixture"):
        chain = [Folder.objects.create(name="root")]
        for name in ("a", "b", "c"):
            chain.append(Folder.objects.create(name=name, parent=chain[-1]))
        posts = [Post.objects.create(title=folder.name, folder=folder) for folder in chain]
    local.write_relationships([RelationshipTuple(to_object_ref(chain[0]), "viewer", ALICE)])
    rows = chain if model is Folder else posts
    within = {row.pk for row in rows[:2]}
    assert visible(model) == within
    monkeypatch.setattr(read, "_ROW_LIMIT", 0)
    # Every set inside the statement: the same rows.
    assert visible(model) == within


CODED = """
definition auth/user {}
definition blog/codedfolder {
    relation parent: blog/codedfolder // rebac:field=parent
    relation viewer: auth/user
    permission read = viewer + parent->read
}
"""


def test_a_held_row_that_no_child_can_name_keeps_the_hierarchy(monkeypatch):
    local = install_schema(CODED)
    with sudo(reason="test.fixture"):
        nameless = CodedFolder.objects.create(code=None)
        top = CodedFolder.objects.create(code="top")
        under = CodedFolder.objects.create(code="under", parent=top)
        other = CodedFolder.objects.create(code="other")
    local.write_relationships(
        [RelationshipTuple(to_object_ref(row), "viewer", ALICE) for row in (nameless, top)]
    )
    # The parent column names a row by ``code``; ``nameless`` has none.
    assert visible(CodedFolder) == {nameless.pk, top.pk, under.pk}

    def swap():
        rows = CodedFolder._base_manager
        rows.filter(pk=under.pk).update(parent=None)
        rows.filter(pk=top.pk).update(code="was-top")
        rows.filter(pk=other.pk).update(code="top")
        rows.filter(pk=under.pk).update(parent_id="top")

    done = after_deciding(monkeypatch, ("blog/codedfolder", "read"), swap)
    # ``under`` now hangs under the row that took the name, which holds nothing.
    assert visible(CodedFolder) == set()
    assert done
    assert visible(CodedFolder) == {nameless.pk, top.pk}


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


def test_rows_that_share_an_identity_are_all_kept_in_a_hierarchy(monkeypatch):
    # Nothing makes an identity attribute unique: two rows can answer to one.
    monkeypatch.setattr(Folder._meta, "rebac_id_attr", "name", raising=False)
    local = install_schema(SCHEMA)
    with sudo(reason="test.fixture"):
        first = Folder.objects.create(name="seed")
        second = Folder.objects.create(name="seed")
        under = Folder.objects.create(name="under", parent=second)
        Folder.objects.create(name="other")
    local.write_relationships(
        [RelationshipTuple(ObjectRef("blog/folder", "seed"), "viewer", ALICE)]
    )
    assert visible(Folder) == {first.pk, second.pk, under.pk}
    monkeypatch.setattr(read, "_ROW_LIMIT", 0)
    assert visible(Folder) == {first.pk, second.pk, under.pk}


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


# ---------- Kept within an evaluator scope (proposal 0017) ----------

BOB = SubjectRef.of("auth/user", "bob")


def _folder_reads(queries):
    """The decisions over folders: statements whose root table is the folder table."""
    return [q for q in queries if q["sql"].startswith('SELECT "testapp_folder".')]


def _confirmations(queries):
    return [
        q
        for q in queries
        if q["sql"].startswith("SELECT EXISTS")
        or ('FROM "rebac_schemageneration"' in q["sql"] and "held0" in q["sql"])
    ]


def _listed(sql_queries):
    return [q["sql"] for q in sql_queries if q["sql"].startswith('SELECT "testapp_post".')]


def test_a_scope_binds_the_rows_it_decided_again(tree):
    from rebac import evaluator_scope

    _local, _folders, posts, _grant = tree
    expected = {posts["child"].pk, posts["leaf"].pk}
    with evaluator_scope():
        with CaptureQueriesContext(connection) as first:
            assert visible(Post) == expected
        with CaptureQueriesContext(connection) as second:
            assert visible(Post) == expected
    assert _folder_reads(first)
    # The second statement decides nothing: one statement confirms the kept rows.
    assert _folder_reads(second) == []
    assert len(_confirmations(second)) == 1
    assert _listed(second) == _listed(first)


def test_nothing_is_kept_outside_a_scope(tree):
    _local, _folders, _posts, _grant = tree
    with CaptureQueriesContext(connection) as first:
        visible(Post)
    with CaptureQueriesContext(connection) as second:
        visible(Post)
    assert len(_folder_reads(second)) == len(_folder_reads(first)) > 0
    assert _confirmations(second) == []


def test_a_write_the_scope_sees_drops_the_kept_rows(tree):
    from rebac import evaluator_scope

    _local, folders, posts, _grant = tree
    with evaluator_scope():
        assert visible(Post) == {posts["child"].pk, posts["leaf"].pk}
        with sudo(reason="test.move"):
            Folder.objects.filter(pk=folders["other"].pk).update(parent=folders["child"])
        with CaptureQueriesContext(connection) as after:
            assert visible(Post) == {posts["child"].pk, posts["leaf"].pk, posts["other"].pk}
    assert _folder_reads(after)
    assert _confirmations(after) == []


def _unseen(change):
    """Run ``change`` where the scope cannot see it, as another process would."""
    observers = [
        wrapper
        for wrapper in connection.execute_wrappers
        if type(getattr(wrapper, "__self__", None)).__name__ == "_ConnectionSchemas"
    ]
    for wrapper in observers:
        connection.execute_wrappers.remove(wrapper)
    try:
        with sudo(reason="test.unseen"):
            change()
    finally:
        connection.execute_wrappers[0:0] = observers


def test_a_kept_set_that_no_longer_holds_is_decided_afresh(tree):
    from rebac import evaluator_scope

    _local, folders, posts, _grant = tree
    with evaluator_scope():
        assert visible(Post) == {posts["child"].pk, posts["leaf"].pk}
        _unseen(
            lambda: Folder._base_manager.filter(pk=folders["leaf"].pk).update(
                parent=folders["other"]
            )
        )
        with CaptureQueriesContext(connection) as after:
            # The confirmation fails, the scope drops its rows, the statement
            # decides afresh: not an empty answer for the rest of the scope.
            assert visible(Post) == {posts["child"].pk}
        assert len(_confirmations(after)) == 1
        assert _folder_reads(after)
        assert visible(Folder) == {folders["child"].pk}


def test_a_row_the_scope_cannot_see_appear_waits_for_the_scope(tree):
    from rebac import evaluator_scope

    _local, folders, posts, _grant = tree
    with evaluator_scope():
        assert visible(Post) == {posts["child"].pk, posts["leaf"].pk}
        _unseen(
            lambda: Folder._base_manager.filter(pk=folders["other"].pk).update(
                parent=folders["child"]
            )
        )
        # A kept set is a lower bound: the newly readable row stays hidden.
        assert visible(Post) == {posts["child"].pk, posts["leaf"].pk}
    assert visible(Post) == {posts["child"].pk, posts["leaf"].pk, posts["other"].pk}


def test_kept_rows_are_the_actors_own(tree):
    from rebac import evaluator_scope

    local, folders, posts, _grant = tree
    local.write_relationships([RelationshipTuple(to_object_ref(folders["other"]), "viewer", BOB)])
    alice = {posts["child"].pk, posts["leaf"].pk}
    with evaluator_scope():
        assert visible(Post) == alice
        assert set(Post.objects.with_actor(BOB).values_list("pk", flat=True)) == {posts["other"].pk}
        assert visible(Post) == alice


def test_rows_are_kept_inside_a_request_transaction(tree):
    from django.db import transaction

    from rebac import evaluator_scope

    _local, _folders, posts, _grant = tree
    expected = {posts["child"].pk, posts["leaf"].pk}
    with evaluator_scope(), transaction.atomic():
        assert visible(Post) == expected
        with CaptureQueriesContext(connection) as second:
            assert visible(Post) == expected
        assert _folder_reads(second) == []
        with transaction.atomic():
            # A savepoint is a statement the scope sees: the rows are dropped.
            with CaptureQueriesContext(connection) as nested:
                assert visible(Post) == expected
        assert _folder_reads(nested)


def test_a_fully_consistent_read_decides_afresh(tree):
    from rebac import Consistency, evaluator_scope

    local, _folders, _posts, _grant = tree
    with evaluator_scope():
        local.accessible(subject=ALICE, action="read", resource_type="blog/post")
        with CaptureQueriesContext(connection) as latest:
            local.accessible(
                subject=ALICE,
                action="read",
                resource_type="blog/post",
                consistency=Consistency.FULLY_CONSISTENT,
            )
        with CaptureQueriesContext(connection) as kept:
            local.accessible(subject=ALICE, action="read", resource_type="blog/post")
    assert _folder_reads(latest) and _confirmations(latest) == []
    assert _folder_reads(kept) == [] and len(_confirmations(kept)) == 1


def test_kept_rows_are_bounded_by_the_evaluator_cache_size(tree, settings):
    from rebac import evaluator_scope

    _local, _folders, posts, _grant = tree
    expected = {posts["child"].pk, posts["leaf"].pk}
    # The hierarchy holds two keys: a budget of one keeps nothing.
    settings.REBAC_EVALUATOR_CACHE_SIZE = 1
    with evaluator_scope():
        assert visible(Post) == expected
        with CaptureQueriesContext(connection) as second:
            assert visible(Post) == expected
    assert _folder_reads(second)
    assert _confirmations(second) == []


def _unseen_tuples(monkeypatch, change):
    """Write tuples as another process would: unseen, no generation bump here."""
    from rebac.backends import local as local_backend

    generation = local_backend._relationship_generation
    _unseen(change)
    monkeypatch.setattr(local_backend, "_relationship_generation", generation)


SHARED_SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    permission open = viewer
    permission nested = parent->open
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->open + folder->nested
}
"""


def test_a_kept_set_is_reused_only_with_the_very_sets_it_rests_on(monkeypatch, settings):
    """``nested`` was decided through ``open``; ``open`` is evicted, its grant
    revoked unseen, and decided again: ``nested`` must not be reused beside
    the new ``open``, or its witness would check the old folders."""
    from rebac import evaluator_scope

    local = install_schema(SHARED_SCHEMA)
    with sudo(reason="test.fixture"):
        b1 = Folder.objects.create(name="b1")
        b2 = Folder.objects.create(name="b2")
        a1 = Folder.objects.create(name="a1", parent=b1)
        p1 = Post.objects.create(title="p1", folder=a1)
        p2 = Post.objects.create(title="p2", folder=b2)
    grants = [RelationshipTuple(to_object_ref(folder), "viewer", ALICE) for folder in (b1, b2)]
    local.write_relationships(grants)
    settings.REBAC_EVALUATOR_CACHE_SIZE = 2
    with evaluator_scope():
        assert visible(Post) == {p1.pk, p2.pk}
        _unseen_tuples(monkeypatch, lambda: local.delete_relationship(grants[0]))
        assert visible(Post) == {p2.pk}
    assert visible(Post) == {p2.pk}


GATE_SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation editor: auth/user
    relation parent: blog/folder // rebac:field=parent
    permission write = editor + parent->write
    permission read = write
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation shelf: blog/folder // rebac:field=collections
    permission write = folder->write
    permission read = write
}
"""


def test_a_write_gate_does_not_bind_kept_rows(monkeypatch):
    from rebac import actor_context, evaluator_scope

    local = install_schema(GATE_SCHEMA)
    with sudo(reason="test.fixture"):
        box = Folder.objects.create(name="box")
        g1 = Folder.objects.create(name="g1")
        g2 = Folder.objects.create(name="g2")
        posts = [Post.objects.create(title=f"p{n}", folder=g2) for n in range(5)]
    local.write_relationships(
        [RelationshipTuple(to_object_ref(folder), "editor", ALICE) for folder in (box, g1)]
    )
    with evaluator_scope(), actor_context(ALICE):
        # A read keeps the folders alice may write: box and g1.
        assert (
            set(local.accessible(subject=ALICE, action="write", resource_type="blog/post")) == set()
        )
        _unseen_tuples(
            monkeypatch,
            lambda: local.write_relationships(
                [RelationshipTuple(to_object_ref(g2), "editor", ALICE)]
            ),
        )
        # The gate decides afresh: g2 is alice's now, so are its posts.
        Folder.objects.get(pk=box.pk).collected_posts.add(*posts)
    with sudo(reason="test.verify"):
        assert set(box.collected_posts.values_list("pk", flat=True)) == {p.pk for p in posts}


def test_a_statement_outside_a_transaction_drops_the_kept_rows(tree):
    from rebac import evaluator_scope

    _local, _folders, posts, _grant = tree
    with evaluator_scope():
        assert visible(Post) == {posts["child"].pk, posts["leaf"].pk}
        # An insert into a model the policy does not read, in no transaction:
        # only the statement observer can see it.
        get_user_model().objects.create(username="unrelated")
        with CaptureQueriesContext(connection) as after:
            assert visible(Post) == {posts["child"].pk, posts["leaf"].pk}
    assert _folder_reads(after)
    assert _confirmations(after) == []


def test_an_audited_bypass_keeps_the_kept_rows(tree):
    from rebac import evaluator_scope
    from rebac.models import PermissionAuditEvent

    _local, _folders, posts, _grant = tree
    expected = {posts["child"].pk, posts["leaf"].pk}
    before = PermissionAuditEvent.objects.count()
    with evaluator_scope():
        assert visible(Post) == expected
        # A bypass writes its audit row at once, on the same connection.
        assert Folder.objects.sudo(reason="test.audited").count() == 4
        with CaptureQueriesContext(connection) as after:
            assert visible(Post) == expected
    assert PermissionAuditEvent.objects.count() == before + 1
    assert _folder_reads(after) == []
    assert len(_confirmations(after)) == 1


TEAM_SCHEMA = """
definition auth/user {}
definition work/team {
    relation member: auth/user
}
definition blog/folder {
    relation viewer: auth/user | work/team#member
    permission read = viewer
}
"""


def test_a_read_that_decides_afresh_decides_the_memberships_afresh(monkeypatch):
    from rebac import Consistency, evaluator_scope

    local = install_schema(TEAM_SCHEMA)
    with sudo(reason="test.fixture"):
        mine = Folder.objects.create(name="mine")
        theirs = Folder.objects.create(name="theirs")
    team = ObjectRef("work/team", "t")
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(mine), "viewer", ALICE),
            RelationshipTuple(
                to_object_ref(theirs), "viewer", SubjectRef.of("work/team", "t", "member")
            ),
        ]
    )
    expected = {str(mine.pk), str(theirs.pk)}
    with evaluator_scope():
        assert set(local.accessible(subject=ALICE, action="read", resource_type="blog/folder")) == {
            str(mine.pk)
        }
        _unseen_tuples(
            monkeypatch,
            lambda: local.write_relationships([RelationshipTuple(team, "member", ALICE)]),
        )
        latest = local.accessible(
            subject=ALICE,
            action="read",
            resource_type="blog/folder",
            consistency=Consistency.FULLY_CONSISTENT,
        )
        assert set(latest) == expected
    assert (
        set(local.accessible(subject=ALICE, action="read", resource_type="blog/folder")) == expected
    )
