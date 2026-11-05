"""Bounded row decisions and inline overflow follow live permission data."""

import re
from contextlib import nullcontext

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection, models, transaction
from django.db.models import Exists, OuterRef, Prefetch, Q, Subquery
from django.test.utils import CaptureQueriesContext, isolate_apps

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    evaluator_scope,
    sudo,
    to_object_ref,
)
from rebac.compile import read
from rebac.evaluator import PermissionEvaluator, current_evaluator
from rebac.field_backing import foreign_key_kept, resolve_field_backing
from rebac.models import active_relationship_model
from rebac.schema import parse_zed
from rebac.testing import install_schema
from tests.testapp.models import (
    Attachment,
    Folder,
    NativeParentLinkedChild,
    NativeParentLinkedResource,
    Post,
    TextIdentityFolder,
    VirtualFolder,
)

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
ACTOR = SubjectRef.of("auth/user", "alice")


@pytest.fixture(params=["inline", "decided"])
def plan(request, monkeypatch):
    if request.param == "inline":
        original = read._scope_statement

        def inline(operation, model, key, *, decide=True, candidates=None):
            return original(operation, model, key, decide=False, candidates=candidates)

        monkeypatch.setattr(read, "_scope_statement", inline)
    with evaluator_scope() if request.param == "decided" else nullcontext():
        yield request.param


def visible(model):
    return set(model.objects.with_actor(ACTOR).values_list("pk", flat=True))


def assert_rebac_ctes_defined(queries):
    tables = set(connection.introspection.table_names())
    for query in queries:
        sql = query["sql"]
        references = set(re.findall(r'(?:FROM|JOIN) "(rebac_[a-z0-9_]+)"', sql)) - tables
        definitions = set(
            re.findall(r'"(rebac_[a-z0-9_]+)" AS (?:NOT MATERIALIZED |MATERIALIZED )?\(', sql)
        )
        assert references <= definitions, (references - definitions, sql)


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


def test_hierarchy_and_arrow_scope_prepares_at_first_evaluation(tree, plan):
    _local, child, leaf, post = tree
    with CaptureQueriesContext(connection) as construction:
        scope = Post.objects.with_actor(ACTOR).scoped()
    assert len(construction) == 0
    assert scope.exists()
    with CaptureQueriesContext(connection) as queries:
        sql = str(scope.query)
    assert len(queries) == 0
    assert "SELECT" in sql
    assert set(Folder.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {child.pk, leaf.pk}
    assert list(scope.values_list("pk", flat=True)) == [post.pk]


def test_small_hierarchy_scope_statement_stays_bounded(tree, plan):
    _local, _child, _leaf, post = tree
    with CaptureQueriesContext(connection) as queries:
        assert list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == [post.pk]
    sql = queries[-1]["sql"]
    assert len(queries) == (1 if plan == "inline" else 2)
    assert ('"folder_id" IN (SELECT' in sql) == (plan == "inline")
    assert len(sql) < 25_000


@pytest.mark.pg_delta
@pytest.mark.parametrize("memberships", [2, 12])
@pytest.mark.parametrize(
    "shape",
    [
        "exists",
        "subquery",
        "related",
        "union",
        "aggregate",
        "distinct_count",
        "sliced_count",
        "sliced_prefetch",
    ],
)
def test_embedded_decided_scope_defines_every_reference(memberships, shape, plan):
    local = install_schema("""
        definition auth/user {}
        definition auth/group {
            relation member: auth/user
        }
        definition blog/folder {
            relation viewer: auth/group#member
            permission read = viewer
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->read
        }
    """)
    with sudo(reason="embedded decided scope fixture"):
        folders = Folder.objects.bulk_create([Folder(name=f"folder-{i}") for i in range(14)])
        Post.objects.bulk_create(
            [Post(title=f"post-{i}", folder=folder) for i, folder in enumerate(folders)]
        )
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("auth/group", f"group-{i}"), "member", ACTOR)
            for i in range(memberships)
        ]
        + [
            RelationshipTuple(
                to_object_ref(folder),
                "viewer",
                SubjectRef.of("auth/group", f"group-{i}", "member"),
            )
            for i, folder in enumerate(folders[:memberships])
        ]
    )
    with CaptureQueriesContext(connection) as preparation:
        scoped = Post.objects.with_actor(ACTOR).scoped()
        correlated = scoped.filter(folder_id=OuterRef("pk"))
        if shape == "exists":
            outer = Folder._base_manager.filter(Exists(correlated))
        elif shape == "subquery":
            outer = Folder._base_manager.filter(pk=Subquery(correlated.values("folder_id")[:1]))
        elif shape == "related":
            outer = Folder._base_manager.filter(posts__in=scoped).distinct()
        elif shape == "union":
            arm = Folder._base_manager.filter(Exists(correlated)).values_list("pk", flat=True)
            outer = arm.union(
                Folder._base_manager.filter(pk=-1).values_list("pk", flat=True), all=True
            )
        else:
            outer = None
    with CaptureQueriesContext(connection) as queries:
        if shape == "aggregate":
            result = Folder._base_manager.aggregate(
                total=models.Count("pk", filter=Q(Exists(correlated)))
            )["total"]
            assert result == memberships
        elif shape == "distinct_count":
            assert Post.objects.with_actor(ACTOR).distinct().count() == memberships
        elif shape == "sliced_count":
            assert Post.objects.with_actor(ACTOR)[:1000].count() == memberships
        elif shape == "sliced_prefetch":
            prefetched = Folder._base_manager.order_by("pk").prefetch_related(
                Prefetch(
                    "posts",
                    queryset=Post.objects.with_actor(ACTOR).order_by("pk")[:2],
                    to_attr="seen",
                )
            )
            assert [folder.pk for folder in prefetched if folder.seen] == [
                folder.pk for folder in folders[:memberships]
            ]
        else:
            assert outer is not None
            assert sorted(outer.values_list("pk", flat=True)) == sorted(
                folder.pk for folder in folders[:memberships]
            )
    assert_rebac_ctes_defined([*preparation, *queries])
    if shape == "exists":
        if plan == "inline":
            assert len(preparation) == 0
        assert len(queries) == 1


@pytest.mark.pg_delta
def test_owned_decided_scope_defines_array_cte(plan):
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user
            permission read = viewer
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->read
        }
    """)
    with sudo(reason="owned decided scope fixture"):
        folder = Folder.objects.create(name="allowed")
        post = Post.objects.create(title="allowed", folder=folder)
    local.write_relationships([RelationshipTuple(to_object_ref(folder), "viewer", ACTOR)])
    with CaptureQueriesContext(connection) as queries:
        assert list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == [post.pk]
    assert_rebac_ctes_defined(queries)
    if connection.vendor == "postgresql":
        assert any('FROM "rebac_decided_' in query["sql"] for query in queries) == (
            plan == "decided"
        )


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
def test_memoized_decided_witness_stays_defined_when_embedded(tree):
    _local, _child, leaf, post = tree
    with evaluator_scope() as evaluator:
        assert list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == [post.pk]
        assert evaluator.stats()["row_decision_entries"] > 0
        scoped = Post.objects.with_actor(ACTOR).scoped()
        outer = Folder._base_manager.filter(Exists(scoped.filter(folder_id=OuterRef("pk"))))
        with CaptureQueriesContext(connection) as queries:
            assert list(outer.values_list("pk", flat=True)) == [leaf.pk]
    assert_rebac_ctes_defined(queries)
    if connection.vendor == "postgresql":
        assert any('FROM "rebac_decided_' in query["sql"] for query in queries)


@pytest.mark.pg_delta
@pytest.mark.parametrize("shape", ["standalone", "pk_in", "exists"])
def test_every_row_gate_carries_decided_witness_ctes(shape):
    local = install_schema("""
        definition auth/user {}
        definition org/role {
            relation member: auth/user
        }
        definition blog/folder {
            relation viewer: auth/user
            permission read = viewer
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            relation admin: org/role // rebac:const=admin
            permission read = folder->read + admin->member
        }
    """)
    admin = SubjectRef.of("auth/user", "admin")
    with sudo(reason="every row gate fixture"):
        folders = Folder.objects.bulk_create([Folder(name=f"folder-{i}") for i in range(6)])
        posts = Post.objects.bulk_create(
            [Post(title=f"post-{i}", folder=folder) for i, folder in enumerate(folders)]
        )
    local.write_relationships(
        [RelationshipTuple(ObjectRef("org/role", "admin"), "member", admin)]
        + [RelationshipTuple(to_object_ref(folder), "viewer", admin) for folder in folders[:2]]
    )

    with CaptureQueriesContext(connection) as queries:
        if shape == "standalone":
            result = sorted(Post.objects.with_actor(admin).values_list("pk", flat=True))
            expected = sorted(post.pk for post in posts)
        elif shape == "pk_in":
            with evaluator_scope():
                result = sorted(
                    Post.objects.with_actor(admin)
                    .filter(pk__in={posts[0].pk, posts[3].pk})
                    .values_list("pk", flat=True)
                )
            expected = sorted([posts[0].pk, posts[3].pk])
        else:
            scoped = Post.objects.with_actor(admin).scoped()
            result = sorted(
                Folder._base_manager.filter(
                    Exists(scoped.filter(folder_id=OuterRef("pk")))
                ).values_list("pk", flat=True)
            )
            expected = sorted(folder.pk for folder in folders)
    assert result == expected
    assert_rebac_ctes_defined(queries)
    if connection.vendor == "postgresql" and shape != "pk_in":
        assert any('FROM "rebac_decided_' in query["sql"] for query in queries)
    if shape == "exists":
        assert len(queries) == 3


@pytest.mark.pg_delta
def test_decided_row_ids_have_bounded_statement_bytes_per_member():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user
            permission read = viewer
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->read
        }
    """)
    with sudo(reason="row size fixture"):
        folders = Folder.objects.bulk_create([Folder(name=f"f{i}") for i in range(200)])
        Post.objects.bulk_create(
            [Post(title=f"p{i}", folder=folder) for i, folder in enumerate(folders)]
        )

    def measure(count):
        local.write_relationships(
            [
                RelationshipTuple(to_object_ref(folder), "viewer", ACTOR)
                for folder in folders[:count]
            ]
        )
        read.reset()
        with CaptureQueriesContext(connection) as queries:
            scope = Post.objects.with_actor(ACTOR).scoped()
            result = scope.count()
        _sql, params = scope.query.sql_with_params()
        return result, len(queries[-1]["sql"].encode()), queries[-1]["sql"], params

    small_count, small_bytes, small_sql, small_params = measure(10)
    large_count, large_bytes, large_sql, large_params = measure(200)
    assert (small_count, large_count) == (10, 200)
    # One PostgreSQL array literal (or two SQLite IN occurrences), not two
    # separately expanded PostgreSQL lists.
    assert (large_bytes - small_bytes) / 190 < 16
    if connection.vendor == "postgresql":
        assert "= ANY(" in small_sql and "= ANY(" in large_sql
        assert len(re.findall(r'"rebac_decided_[^\"]+" AS \(', small_sql)) == 1
        assert len(re.findall(r'"rebac_decided_[^\"]+" AS \(', large_sql)) == 1
        # The row array is the only array: zero verdict keys add no term.
        assert sorted(len(param) for param in small_params if isinstance(param, list)) == [10]
        assert sorted(len(param) for param in large_params if isinstance(param, list)) == [200]


@pytest.mark.pg_delta
def test_decided_id_array_has_one_parameter_and_constant_sql_bytes():
    from django.db.models import F

    from rebac.compile.predicate import _bound_in

    def compile_ids(count):
        return Folder._base_manager.filter(
            _bound_in(F("pk"), range(1, count + 1))
        ).query.sql_with_params()

    short_sql, short_params = compile_ids(10)
    long_sql, long_params = compile_ids(5_000)
    if connection.vendor == "postgresql":
        assert "= ANY(%s)" in short_sql
        assert len(short_params) == len(long_params) == 1
        assert len(short_params[0]) == 10
        assert len(long_params[0]) == 5_000
        assert len(short_sql.encode()) == len(long_sql.encode())
    else:
        assert len(short_params) == 10
        assert len(long_params) == 5_000
        assert (len(long_sql.encode()) - len(short_sql.encode())) / 4_990 < 8


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("memberships", [1, 30])
def test_evaluator_row_witness_is_self_contained_across_statements(memberships):
    local = install_schema("""
        definition auth/user {}
        definition auth/group { relation member: auth/user | auth/group#member }
        definition blog/folder {
            relation viewer: auth/user | auth/group#member
            permission read = viewer
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            relation viewer: auth/user
            permission read = viewer + folder->read
        }
    """)
    with sudo(reason="memo witness fixture"):
        folders = Folder.objects.bulk_create([Folder(name=f"f{i}") for i in range(20)])
        posts = Post.objects.bulk_create(
            [Post(title=f"p{i}", folder=folders[i % 20]) for i in range(40)]
        )
    edges = []
    for index in range(memberships):
        group = ObjectRef("auth/group", f"g{index}")
        edges.append(RelationshipTuple(group, "member", ACTOR))
        if index < 10:
            edges.append(
                RelationshipTuple(
                    to_object_ref(folders[index]),
                    "viewer",
                    SubjectRef.of("auth/group", f"g{index}", "member"),
                )
            )
    local.write_relationships(edges)
    read.reset()
    expected = sorted(post.pk for post in posts if post.folder_id in {f.pk for f in folders[:10]})
    if memberships == 1:
        expected = sorted(post.pk for post in posts if post.folder_id == folders[0].pk)
    with evaluator_scope() as evaluator:
        first = sorted(Post.objects.with_actor(ACTOR).values_list("pk", flat=True))
        assert evaluator._row_decisions
        assert local.check_access(
            subject=ACTOR, action="read", resource=to_object_ref(posts[0])
        ).allowed
        again = sorted(Post.objects.with_actor(ACTOR).values_list("pk", flat=True))
        filtered = sorted(
            Post.objects.with_actor(ACTOR).filter(pk__gte=posts[20].pk).values_list("pk", flat=True)
        )
    assert first == again == expected
    assert filtered == [pk for pk in expected if pk >= posts[20].pk]


def _statement_bytes(model, monkeypatch, *, inline=False):
    with monkeypatch.context() as patcher:
        if inline:
            patcher.setattr(read._Rows, "rows", lambda self, key: None)
            patcher.setattr(read._Operation, "sets", lambda self, verdicts: None)
            patcher.setattr(read, "const_facts", lambda *args: ())
            read.reset()
        with evaluator_scope(), CaptureQueriesContext(connection) as queries:
            model.objects.with_actor(ACTOR).count()
    return len(queries[-1]["sql"].encode())


def test_decided_tree_statement_stays_near_inline(monkeypatch):
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
    if connection.vendor == "postgresql":
        # One short-list CTE costs about 580 bytes before its byte savings.
        assert decided <= inline + 600, (decided, inline)
    else:
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


def test_decided_three_level_chain_stays_near_inline(chain, monkeypatch):
    decided = _statement_bytes(Attachment, monkeypatch)
    inline = _statement_bytes(Attachment, monkeypatch, inline=True)
    print(f"chain SQL bytes: decided={decided}, inline={inline}")
    if connection.vendor == "postgresql":
        # This fixture has two short lists and a self-contained memo witness.
        assert decided <= inline + 800, (decided, inline)
    else:
        assert decided < inline, (decided, inline)


def test_no_caveat_keys_render_no_verdict_array_term(chain):
    with evaluator_scope(), CaptureQueriesContext(connection) as queries:
        Attachment.objects.with_actor(ACTOR).count()
    sql = queries[-1]["sql"]
    assert '"caveat_name"' in sql
    assert not re.search(r'"caveat_key"\s*=\s*ANY\(', sql)


def test_the_row_limit_is_what_one_statement_may_bind(chain, monkeypatch):
    monkeypatch.setattr(read, "_ROW_LIMIT", 1)
    operation = read._Operation.begin(chain, ACTOR, "default")
    key = ("test/attachment", "read")
    read._scope_statement(operation, Attachment, key)
    decisions = operation.rows(operation.verdicts(key))._found.values()
    assert sum(len(decision.ids) for decision in decisions if decision is not None) <= 1
    assert Attachment.objects.with_actor(ACTOR).count() == 1


def test_abandoned_row_decision_releases_nested_budget(tree, monkeypatch):
    local, _child, _leaf, _post = tree
    operation = read._Operation.begin(local, ACTOR, "default")
    verdicts = operation.verdicts(("blog/post", "read"))
    decisions = operation.rows(verdicts)
    target = ("blog/folder", "read")
    nested = ("synthetic/nested", "read")
    monkeypatch.setattr(read, "_ROW_LIMIT", 2)
    original = read.Compiler.holds
    inserted = []

    def with_late_nested_decision(self, key, at, bound=read.Bound.LOWER):
        result = original(self, key, at, bound)
        if self.rows is decisions and self.parametric and key == target and not inserted:
            decisions._found[nested] = read._Decision(("nested",), models.Q())
            inserted.append(True)
        return result

    monkeypatch.setattr(read.Compiler, "holds", with_late_nested_decision)
    assert decisions.rows(target) is None
    assert inserted
    assert nested not in decisions._found
    assert decisions.room == 2


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
    built = []
    original = read._Compiled.as_sql
    original_holds = read.Compiler.holds

    def count_compilation(self, compiler, connection):
        compiled.append(True)
        return original(self, compiler, connection)

    def count_build(self, key, at, bound=read.Bound.LOWER):
        built.append(True)
        return original_holds(self, key, at, bound)

    monkeypatch.setattr(read._Compiled, "as_sql", count_compilation)
    monkeypatch.setattr(read.Compiler, "holds", count_build)
    resource = to_object_ref(post)
    assert local.check_access(subject=ACTOR, action="read", resource=resource).allowed
    first = len(compiled)
    first_build = len(built)
    assert local.check_access(subject=ACTOR, action="read", resource=resource).allowed
    assert len(compiled) == first
    assert len(built) == first_build
    other_shape = SubjectRef.of("auth/group", "readers")
    assert not local.check_access(subject=other_shape, action="read", resource=resource).allowed
    assert len(compiled) > first
    second = len(compiled)
    local.set_schema(
        parse_zed(schema.replace("permission read = viewer", "permission read = viewer + viewer"))
    )
    assert local.check_access(subject=ACTOR, action="read", resource=resource).allowed
    assert len(compiled) > second


def test_const_fact_values_key_the_point_statement_cache():
    local = install_schema("""
        definition auth/user {}
        definition test/role { relation member: auth/user }
        definition blog/post {
            relation admin: test/role // rebac:const=admin
            permission read = admin->member
        }
    """)
    with sudo(reason="fact cache fixture"):
        post = Post.objects.create(title="fact")
    resource = to_object_ref(post)
    membership = RelationshipTuple(ObjectRef("test/role", "admin"), "member", ACTOR)
    read.reset()
    assert not local.check_access(subject=ACTOR, action="read", resource=resource).allowed
    local.write_relationships([membership])
    assert local.check_access(subject=ACTOR, action="read", resource=resource).allowed
    local.delete_relationship(membership)
    assert not local.check_access(subject=ACTOR, action="read", resource=resource).allowed


def test_three_reachable_facts_do_not_add_point_or_scope_reads():
    local = install_schema("""
        definition auth/user {}
        definition site/role { relation member: auth/user }
        definition blog/folder {
            relation viewer: auth/user
            relation admin: site/role // rebac:const=admin
            relation auditor: site/role // rebac:const=auditor
            relation support: site/role // rebac:const=support
            permission read = viewer + admin->member + auditor->member + support->member
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            relation viewer: auth/user
            permission read = viewer + folder->read
        }
    """)
    with sudo(reason="fact query count fixture"):
        folder = Folder.objects.create(name="facts")
        post = Post.objects.create(title="facts", folder=folder)
    local.write_relationships([RelationshipTuple(to_object_ref(post), "viewer", ACTOR)])
    resource = to_object_ref(post)
    assert local.check_access(subject=ACTOR, action="read", resource=resource).allowed
    with CaptureQueriesContext(connection) as point_queries:
        assert local.check_access(subject=ACTOR, action="read", resource=resource).allowed
    with CaptureQueriesContext(connection) as scope_queries:
        assert Post.objects.with_actor(ACTOR).count() == 1
    assert len(point_queries) == 1
    assert len(scope_queries) == 1


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


def test_decided_scope_statement_reuses_matching_outcome(tree):
    _local, _child, _leaf, post = tree
    read.reset()
    assert visible(Post) == {post.pk}
    first = read.statement_cache_stats()
    assert first["entries"] == 1
    native = tuple(read._native_outcomes.values())
    assert native
    assert visible(Post) == {post.pk}
    second = read.statement_cache_stats()
    assert second["hits"] == first["hits"] + 1
    assert second["misses"] == first["misses"]
    assert tuple(read._native_outcomes.values()) == native


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


@pytest.mark.django_db(transaction=True)
def test_decided_folder_memo_survives_ten_post_writes_but_not_folder_write(tree):
    _local, child, _leaf, post = tree
    with evaluator_scope():
        assert visible(Post) == {post.pk}
        reads = []
        for _ in range(10):
            with sudo(reason="unrelated model write"):
                Post.objects.filter(pk=post.pk).update(folder_id=post.folder_id)
            with CaptureQueriesContext(connection) as queries:
                assert visible(Post) == {post.pk}
            reads.append(len(queries))
        # Ten writes to the root model do not re-decide the folder node.
        assert reads == [1] * 10
        with sudo(reason="decided model write"):
            Folder.objects.filter(pk=child.pk).update(parent_id=child.parent_id)
        with CaptureQueriesContext(connection) as queries:
            assert visible(Post) == {post.pk}
        assert len(queries) == 2


@pytest.mark.django_db(transaction=True)
def test_proxy_write_invalidates_concrete_model_decision(tree):
    _local, child, _leaf, post = tree
    with sudo(reason="proxy fixture"):
        other = Folder.objects.create(name="other")
        second = Post.objects.create(title="second", folder=other)
    scope = evaluator_scope()
    with scope:
        assert visible(Post) == {post.pk}
        with sudo(reason="proxy backed write"):
            VirtualFolder._base_manager.filter(pk=other.pk).update(parent=child)
        assert visible(Post) == {post.pk, second.pk}


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
    with evaluator_scope():
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


@pytest.mark.django_db(transaction=True)
def test_memoized_nested_decision_witnesses_raw_sql_revocation():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation parent: blog/folder // rebac:field=parent
            relation viewer: auth/user
            permission view = viewer
            permission read = parent->view
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->read
        }
    """)
    with sudo(reason="nested memo witness fixture"):
        top = Folder.objects.create(name="top")
        child = Folder.objects.create(name="child", parent=top)
        post = Post.objects.create(title="child", folder=child)
    local.write_relationships([RelationshipTuple(to_object_ref(top), "viewer", ACTOR)])
    with evaluator_scope() as evaluator:
        assert visible(Post) == {post.pk}
        assert evaluator.stats()["row_decision_entries"] > 0
        table = active_relationship_model()._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(f'DELETE FROM "{table}" WHERE "relation" = %s', ["viewer"])
        assert visible(Post) == set()


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


def test_dangling_unconstrained_fk_does_not_grant(tree, monkeypatch, plan):
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
def test_inline_exclusion_respects_depth_limit(settings, model, plan):
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
