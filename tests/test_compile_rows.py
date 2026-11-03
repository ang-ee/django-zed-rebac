"""Bounded row decisions and inline overflow follow live permission data."""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection, models, transaction
from django.test.utils import CaptureQueriesContext, isolate_apps

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.compile import read
from rebac.evaluator import PermissionEvaluator, current_evaluator
from rebac.field_backing import foreign_key_kept, resolve_field_backing
from rebac.schema import parse_zed
from rebac.testing import install_schema
from tests.testapp.models import (
    Attachment,
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


def _statement_bytes(model, monkeypatch, *, inline=False):
    with monkeypatch.context() as patcher:
        if inline:
            patcher.setattr(read._Rows, "rows", lambda self, key: None)
            patcher.setattr(read._Operation, "sets", lambda self, verdicts: None)
            patcher.setattr(read, "const_facts", lambda *args: ())
            read.reset()
        with CaptureQueriesContext(connection) as queries:
            model.objects.with_actor(ACTOR).count()
    return len(queries[-1]["sql"].encode())


def test_decided_tree_statement_is_smaller_than_inline(monkeypatch):
    schema = SCHEMA.replace(
        "permission read = folder->read",
        "relation top: blog/folder // rebac:field=folder__parent\n"
        "    permission read = folder->read + top->read",
    )
    local = install_schema(schema)
    with sudo(reason="tree size fixture"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
        leaf = Folder.objects.create(name="leaf", parent=child)
        Post.objects.create(title="leaf", folder=leaf)
    local.write_relationships([RelationshipTuple(to_object_ref(child), "viewer", ACTOR)])
    decided = _statement_bytes(Post, monkeypatch)
    inline = _statement_bytes(Post, monkeypatch, inline=True)
    print(f"tree SQL bytes: decided={decided}, inline={inline}")
    assert decided < inline, (decided, inline)


@pytest.fixture
def chain():
    schema = """
    definition auth/user {}
    definition auth/group { relation member: auth/user | auth/group#member }
    definition site/role { relation member: auth/user | auth/group#member }
    definition blog/folder {
        relation parent: blog/folder // rebac:field=parent
        relation viewer: auth/user | auth/group#member
        relation admin: site/role // rebac:const=admin
        permission read = viewer + parent->read + admin->member
    }
    definition blog/post {
        relation folder: blog/folder // rebac:field=folder
        relation viewer: auth/user | auth/group#member
        relation admin: site/role // rebac:const=admin
        permission read = viewer + folder->read + admin->member
    }
    definition test/attachment {
        relation target: blog/post // rebac:field=target
        relation viewer: auth/user | auth/group#member
        permission read = viewer + target->read
    }
    """
    local = install_schema(schema)
    with sudo(reason="chain size fixture"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
        post = Post.objects.create(title="post", folder=child)
        Attachment.objects.create(
            content_type=ContentType.objects.get_for_model(Post), object_id=post.pk
        )
    group = ObjectRef("auth/group", "readers")
    local.write_relationships(
        [
            RelationshipTuple(group, "member", ACTOR),
            RelationshipTuple(to_object_ref(root), "viewer", SubjectRef(group, "member")),
        ]
    )
    return local


def test_decided_three_level_chain_is_smaller_than_inline(chain, monkeypatch):
    decided = _statement_bytes(Attachment, monkeypatch)
    inline = _statement_bytes(Attachment, monkeypatch, inline=True)
    print(f"chain SQL bytes: decided={decided}, inline={inline}")
    assert decided < inline, (decided, inline)


def test_the_row_limit_is_what_one_statement_may_bind(chain, monkeypatch):
    monkeypatch.setattr(read, "_ROW_LIMIT", 1)
    operation = read._Operation.begin(chain, ACTOR, "default")
    key = ("test/attachment", "read")
    read._scope_statement(operation, Attachment, key)
    decisions = operation.rows(operation.verdicts(key))._found.values()
    assert sum(len(decision.ids) for decision in decisions if decision is not None) <= 1
    assert Attachment.objects.with_actor(ACTOR).count() == 1


def test_undecided_point_sql_compiles_once_per_revision_and_actor_shape(monkeypatch):
    schema = """
    definition auth/user {}
    definition auth/group {}
    definition blog/post {
        relation viewer: auth/user | auth/group
        permission read = viewer
    }
    """
    local = install_schema(schema)
    with sudo(reason="kept point fixture"):
        post = Post.objects.create(title="kept")
    local.write_relationships([RelationshipTuple(to_object_ref(post), "viewer", ACTOR)])
    read.reset()
    compiled = []
    original = read._Compiled.as_sql

    def count_compilation(self, compiler, connection):
        compiled.append(True)
        return original(self, compiler, connection)

    monkeypatch.setattr(read._Compiled, "as_sql", count_compilation)
    resource = to_object_ref(post)
    assert local.check_access(subject=ACTOR, action="read", resource=resource).allowed
    first = len(compiled)
    assert local.check_access(subject=ACTOR, action="read", resource=resource).allowed
    assert len(compiled) == first
    other_shape = SubjectRef.of("auth/group", "readers")
    assert not local.check_access(subject=other_shape, action="read", resource=resource).allowed
    assert len(compiled) > first
    second = len(compiled)
    local.set_schema(
        parse_zed(schema.replace("permission read = viewer", "permission read = viewer + viewer"))
    )
    assert local.check_access(subject=ACTOR, action="read", resource=resource).allowed
    assert len(compiled) > second


@pytest.mark.django_db(transaction=True)
def test_rolled_back_tuple_write_does_not_blank_evaluator_scope():
    local = install_schema(SCHEMA)
    with sudo(reason="rollback fixture"):
        allowed = Folder.objects.create(name="allowed")
        denied = Folder.objects.create(name="denied")
        post = Post.objects.create(title="allowed", folder=allowed)
        other = Post.objects.create(title="denied", folder=denied)
    local.write_relationships([RelationshipTuple(to_object_ref(allowed), "viewer", ACTOR)])

    class Rollback(Exception):
        pass

    with evaluator_scope() as evaluator:
        assert visible(Post) == {post.pk}
        before = evaluator.stats()["row_decision_entries"]
        with pytest.raises(Rollback), transaction.atomic():
            local.write_relationships([RelationshipTuple(to_object_ref(denied), "viewer", ACTOR)])
            assert visible(Post) == {post.pk, other.pk}
            assert evaluator.stats()["row_decision_entries"] == before
            raise Rollback
        assert visible(Post) == {post.pk}


@pytest.mark.django_db(transaction=True)
def test_kept_rows_are_the_actors_own_and_cleared_at_scope_exit():
    local = install_schema(SCHEMA)
    bob = SubjectRef.of("auth/user", "bob")
    with sudo(reason="actor-specific rows fixture"):
        first = Folder.objects.create(name="first")
        second = Folder.objects.create(name="second")
        first_post = Post.objects.create(title="first", folder=first)
        second_post = Post.objects.create(title="second", folder=second)
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(first), "viewer", ACTOR),
            RelationshipTuple(to_object_ref(second), "viewer", bob),
        ]
    )
    evaluator = PermissionEvaluator()
    with evaluator_scope(evaluator):
        assert visible(Post) == {first_post.pk}
        assert set(Post.objects.with_actor(bob).values_list("pk", flat=True)) == {second_post.pk}
        assert evaluator.stats()["row_decision_entries"] >= 2
        assert evaluator.stats()["row_decision_ids"] >= 2
    assert current_evaluator() is None
    assert evaluator.stats()["row_decision_entries"] == 0
    assert evaluator.stats()["row_decision_ids"] == 0


@pytest.mark.django_db(transaction=True)
def test_kept_row_ids_obey_evaluator_budget():
    local = install_schema(SCHEMA)
    with sudo(reason="row budget fixture"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
        post = Post.objects.create(title="bounded", folder=child)
    local.write_relationships([RelationshipTuple(to_object_ref(root), "viewer", ACTOR)])
    with evaluator_scope(PermissionEvaluator(max_size=1)) as evaluator:
        assert visible(Post) == {post.pk}
        assert evaluator.stats()["row_decision_ids"] <= 1


def test_decided_rows_are_not_kept_outside_evaluator_scope(tree):
    _local, _child, _leaf, post = tree
    read.reset()
    assert visible(Post) == {post.pk}
    assert current_evaluator() is None
    assert not read._statements


@pytest.mark.django_db(transaction=True)
def test_expiring_grant_is_rechecked_after_preparation(monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    import rebac.clock

    schema = SCHEMA.replace(
        "definition auth/user {}", "use expiration\ndefinition auth/user {}"
    ).replace("relation viewer: auth/user", "relation viewer: auth/user with expiration")
    local = install_schema(schema)
    with sudo(reason="expiry fixture"):
        folder = Folder.objects.create(name="expiring")
        post = Post.objects.create(title="expiring", folder=folder)
    now = timezone.now()
    deadline = now + timedelta(minutes=1)
    local.write_relationships(
        [RelationshipTuple(to_object_ref(folder), "viewer", ACTOR, expires_at=deadline)]
    )
    monkeypatch.setattr(rebac.clock, "application_now", lambda: now)
    with evaluator_scope():
        prepared = Post.objects.with_actor(ACTOR).scoped()
        assert list(prepared.values_list("pk", flat=True)) == [post.pk]
        monkeypatch.setattr(rebac.clock, "application_now", lambda: deadline)
        assert not prepared.all().exists()


@pytest.mark.django_db(transaction=True)
def test_overflow_keeps_inline_fallback(tree, monkeypatch):
    _local, child, leaf, post = tree
    monkeypatch.setattr(read, "_ROW_LIMIT", 1)
    with evaluator_scope() as evaluator, CaptureQueriesContext(connection) as queries:
        assert list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == [post.pk]
        assert evaluator.stats()["row_decision_entries"] == 0
    assert f'"folder_id" IN ({child.pk}, {leaf.pk})' not in queries[-1]["sql"]


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
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
