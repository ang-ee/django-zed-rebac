"""Statement-wide id sets, with recursive CTEs kept at their own boundary."""

import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace

import pytest
from django.db import connection
from django.db.models import Count, Exists, OuterRef, Q, QuerySet, Subquery, Value
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_cte import CTE, with_cte

from rebac import (
    CheckItem,
    RelationshipTuple,
    SubjectRef,
    share_id_sets,
    sudo,
    to_object_ref,
)
from rebac.compile import ctes
from rebac.compile.predicate import Compiler
from rebac.models import PermissionAuditEvent
from rebac.testing import install_schema
from tests.reference_harness import _snapshot
from tests.reference_model import ReferenceModel
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]

ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")
SCHEMA = """
    definition auth/user {}
    definition auth/group {
        relation member: auth/user
    }
    definition blog/folder {
        relation viewer: auth/user | auth/group#member
        permission read = viewer
    }
    definition blog/post {
        relation folder: blog/folder // rebac:field=folder
        relation viewer: auth/user
        relation blocked: auth/user
        permission read = (viewer + folder->read) - blocked
    }
"""


def _id_pattern():
    marker = "rebac_ids_marker"
    left, right = connection.ops.quote_name(marker).split(marker)
    return re.escape(left) + r"(_*rebac_ids_[0-9a-f]{16})" + re.escape(right)


def _definitions(sql):
    return re.findall(_id_pattern() + r" AS \(", sql)


def _assert_shared(sql, expected=None):
    definitions = _definitions(sql)
    assert definitions
    assert all(count == 1 for count in Counter(definitions).values())
    if expected is not None:
        assert set(definitions) == expected
    # Every reference is backed by a definition somewhere in this statement.
    assert set(re.findall(_id_pattern(), sql)) == set(definitions)
    positions = {match[1]: match.start() for match in re.finditer(_id_pattern() + r" AS \(", sql)}
    for reference in re.finditer(r"FROM " + _id_pattern(), sql):
        assert positions[reference[1]] < reference.start()
    assert sql.startswith("WITH RECURSIVE ")
    return set(definitions)


def _sql(rows):
    return rows.query.sql_with_params()


@pytest.fixture
def shared_rows():
    local = install_schema(SCHEMA)
    with sudo(reason="shared CTE fixtures"):
        first = Folder.objects.create(name="alice")
        second = Folder.objects.create(name="bob")
        posts = [
            Post.objects.create(title="inherited", folder=first),
            Post.objects.create(title="other actor", folder=second),
            Post.objects.create(title="direct"),
            Post.objects.create(title="excluded", folder=first),
            Post.objects.create(title="hidden"),
        ]
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(first), "viewer", ALICE),
            RelationshipTuple(to_object_ref(second), "viewer", BOB),
            RelationshipTuple(to_object_ref(posts[2]), "viewer", ALICE),
            RelationshipTuple(to_object_ref(posts[3]), "blocked", ALICE),
        ]
    )
    schema, tuples, universe = _snapshot(local, using="default")
    reference = ReferenceModel(schema, tuples, now=timezone.now())
    reference.resources.update(universe)
    return local, reference, posts


def _expected(reference, actor=ALICE):
    return {
        int(pk)
        for pk in reference.accessible(subject=actor, action="read", resource_type="blog/post")
    }


@pytest.mark.parametrize("count", [1, 5])
@pytest.mark.parametrize("embedding", ["exists", "in", "subquery"])
@pytest.mark.parametrize("plain_root", [False, True])
def test_independent_scopes_define_each_id_set_once(shared_rows, count, embedding, plain_root):
    _, reference, _ = shared_rows
    standalone = Post.objects.with_actor(ALICE).scoped().order_by().values("pk")
    definitions = _assert_shared(_sql(standalone)[0])
    rows = (
        share_id_sets(Post._base_manager.all())
        if plain_root
        else Post.objects.with_actor(ALICE).scoped()
    )
    for index in range(count):
        # Independent scope applications, not repeated use of one Query object.
        scoped = Post.objects.with_actor(ALICE).scoped().order_by()
        if embedding == "in":
            rows = rows.filter(pk__in=scoped.values("pk"))
        elif embedding == "exists":
            rows = rows.filter(Exists(scoped.filter(pk=OuterRef("pk"))))
        else:
            name = f"visible_{index}"
            rows = rows.annotate(
                **{name: Subquery(scoped.filter(pk=OuterRef("pk")).values("pk")[:1])}
            )
            rows = rows.filter(**{f"{name}__isnull": False})
    rows = rows.annotate(body_parameter=Value("body parameter")).order_by("pk")
    projected = rows.values_list("pk", "body_parameter")
    sql, params = _sql(projected)
    _assert_shared(sql, definitions)
    assert sql.count("WITH RECURSIVE") == 1
    assert params.index("alice") < params.index("body parameter")
    assert sql.count("%s") == len(params)
    expected = _expected(reference)
    assert {pk for pk, marker in projected if marker == "body parameter"} == expected
    assert set(standalone.values_list("pk", flat=True)) == expected
    # Cloning, rendering and a second execution cannot consume a query's CTEs.
    assert _sql(projected.all()) == (sql, params)
    assert {pk for pk, _ in projected.all()} == expected


def test_different_actor_bindings_do_not_alias(shared_rows, monkeypatch):
    from rebac.compile import read

    original = read._Kept.render
    fallbacks = []

    def render(self, bound):
        values = tuple(bound)
        collector = ctes._current.get()
        before = dict(collector.definitions) if collector is not None else None
        sql, params = original(self, values)
        if collector is not None and self.definitions and sql.startswith("(WITH RECURSIVE "):
            # A conflicting actor binding reaches _Kept.render's fallback,
            # retaining every parameter and atomically leaving the outer sets.
            assert collector.definitions == before
            assert (sql, params) == (self.sql, values)
            fallbacks.append((sql, params))
        return sql, params

    monkeypatch.setattr(read._Kept, "render", render)
    _, reference, _ = shared_rows
    rows = (
        share_id_sets(Post._base_manager.all())
        .annotate(
            alice=Exists(Post.objects.with_actor(ALICE).filter(pk=OuterRef("pk"))),
            bob=Exists(Post.objects.with_actor(BOB).filter(pk=OuterRef("pk"))),
        )
        .order_by("pk")
    )
    sql, params = _sql(rows.values("pk", "alice", "bob"))
    assert sql.startswith("WITH RECURSIVE ")
    assert "alice" in params and "bob" in params
    if ctes._counts_cte_copies(connection):
        # The second binding is written in place, whatever its size: the
        # statement around it could not be rewritten past a name defined twice.
        assert sql.count("WITH RECURSIVE") == 1
        assert max(Counter(_definitions(sql)).values()) == 1
    else:
        assert sql.count("WITH RECURSIVE") > 1
        assert max(Counter(_definitions(sql)).values()) == 2
        assert fallbacks
        assert any("bob" in params for _, params in fallbacks)
    expected_alice, expected_bob = _expected(reference), _expected(reference, BOB)
    for pk, alice, bob in rows.values_list("pk", "alice", "bob"):
        assert alice == (pk in expected_alice)
        assert bob == (pk in expected_bob)
    # Run each scope alone afterwards: the previous collector must be gone.
    for actor, expected in ((ALICE, expected_alice), (BOB, expected_bob)):
        scope = Post.objects.with_actor(actor).scoped().values_list("pk", flat=True)
        _assert_shared(_sql(scope)[0])
        assert set(scope) == expected


def test_union_branches_and_nested_related_scopes_share_definitions(shared_rows):
    _, reference, _ = shared_rows
    base = Post.objects.with_actor(ALICE).scoped().order_by()
    expected_definitions = _assert_shared(_sql(base.values("pk"))[0])
    branches = [
        Post.objects.with_actor(ALICE).filter(title=title).scoped().order_by().values("pk")
        for title in ("inherited", "direct", "hidden")
    ]
    combined = branches[0].union(*branches[1:])
    _assert_shared(_sql(combined)[0], expected_definitions)
    assert set(combined.values_list("pk", flat=True)) == _expected(reference)
    # A plain outer query, a protected folder query, and protected post queries.
    folders = Folder.objects.with_actor(ALICE).filter(
        Exists(Post.objects.with_actor(ALICE).filter(folder_id=OuterRef("pk")))
    )
    rows = (
        share_id_sets(Post._base_manager.all())
        .filter(pk__in=combined)
        .filter(Q(folder_id__in=folders.values("pk")) | Q(folder__isnull=True))
    )
    _assert_shared(_sql(rows)[0])
    assert set(rows.values_list("pk", flat=True)) == _expected(reference)


def test_aggregate_wrapper_and_application_cte_keep_one_prefix(shared_rows):
    _, reference, _ = shared_rows
    scope = Post.objects.with_actor(ALICE).scoped().order_by().values("pk")
    app_cte = CTE(Post._base_manager.filter(pk__in=scope).values("pk"), name="application_posts")
    selected = QuerySet(model=Post, query=app_cte.queryset().query)
    rows = share_id_sets(with_cte(app_cte, select=selected)).filter(pk__in=scope)
    sql, _ = _sql(rows)
    _assert_shared(sql)
    assert sql.count("WITH RECURSIVE") == 1
    assert sql.index(connection.ops.quote_name("application_posts") + " AS (") > sql.index(
        "rebac_ids_"
    )
    assert set(rows.values_list("pk", flat=True)) == _expected(reference)
    # DISTINCT and slicing make Django add a plain AggregateQuery wrapper.
    aggregate = Post._base_manager.filter(pk__in=scope).distinct()[:20]
    with CaptureQueriesContext(connection) as captured:
        assert aggregate.aggregate(total=Count("pk")) == {"total": len(_expected(reference))}
    statements = [item["sql"] for item in captured if _definitions(item["sql"])]
    assert len(statements) == 1
    assert statements[0].count("WITH RECURSIVE") == 1
    assert _definitions(statements[0])


def test_empty_application_cte_preserves_compiler_metadata(shared_rows):
    empty = CTE(Post._base_manager.none().values("pk"), name="empty_application_posts")
    selected = QuerySet(model=Post, query=empty.queryset().query)
    rows = share_id_sets(with_cte(empty, select=selected)).values("pk")
    compiler = rows.query.get_compiler(using="default")
    assert list(compiler.execute_sql()) == []
    assert compiler.col_count == 1
    assert compiler.select is not None
    assert list(rows) == []
    assert rows.count() == 0


def test_insert_subqueries_keep_nested_id_sets(shared_rows, django_user_model):
    scope = Post.objects.with_actor(ALICE).filter(title="direct").values("title")[:1]
    with CaptureQueriesContext(connection) as captured:
        user = django_user_model.objects.create(
            username=Subquery(scope), first_name=Subquery(scope.all())
        )
    inserts = [
        item["sql"]
        for item in captured
        if _definitions(item["sql"]) and "INSERT INTO" in item["sql"]
    ]
    assert len(inserts) == 1
    assert inserts[0].startswith("INSERT INTO")
    assert inserts[0].count("WITH RECURSIVE") == 2
    user.refresh_from_db()
    assert user.username == user.first_name == "direct"


def test_recursive_subject_sets_stay_inside_shared_id_sets():
    local = install_schema(
        SCHEMA.replace(
            "relation member: auth/user", "relation member: auth/user | auth/group#member"
        )
    )
    with sudo(reason="recursive shared CTE fixture"):
        folder = Folder.objects.create(name="group")
        post = Post.objects.create(title="recursive", folder=folder)
    first = SubjectRef.of("auth/group", "first", "member")
    second = SubjectRef.of("auth/group", "second", "member")
    local.write_relationships(
        [
            RelationshipTuple(first.object, "member", ALICE),
            RelationshipTuple(first.object, "member", second),
            RelationshipTuple(second.object, "member", first),
            RelationshipTuple(to_object_ref(folder), "viewer", second),
        ]
    )
    standalone = Post.objects.with_actor(ALICE).scoped().values("pk")
    sql, _ = _sql(standalone)
    definitions = _assert_shared(sql)
    rows = (
        share_id_sets(Post._base_manager.all())
        .filter(pk__in=standalone)
        .filter(Exists(Post.objects.with_actor(ALICE).filter(pk=OuterRef("pk"))))
    )
    combined, _ = _sql(rows)
    _assert_shared(combined, definitions)
    # Recursive helpers are still nested, and not repeated by each scope.
    recursive_definition = connection.ops.quote_name("rebac_sets") + " AS ("
    assert recursive_definition in combined
    assert combined.count(recursive_definition) == sql.count(recursive_definition)
    assert set(rows.values_list("pk", flat=True)) == {post.pk}
    schema, tuples, _ = _snapshot(local, using="default")
    assert (
        ReferenceModel(schema, tuples, now=timezone.now())
        .check(resource=to_object_ref(post), subject=ALICE, action="read")
        .allowed
    )


def test_recursive_path_keeps_its_local_cte_reference():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation parent: blog/folder // rebac:field={"path":"parent","filters":{"is_active":true}}
            relation viewer: auth/user
            permission read = viewer + parent->read
        }
    """)
    with sudo(reason="correlated path CTE fixture"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
        Folder.objects.create(name="hidden")
    local.write_relationships([RelationshipTuple(to_object_ref(root), "viewer", ALICE)])
    rows = Folder._base_manager.annotate(
        alice=Exists(Folder.objects.with_actor(ALICE).filter(pk=OuterRef("pk"))),
        bob=Exists(Folder.objects.with_actor(BOB).filter(pk=OuterRef("pk"))),
    )
    sql, _ = _sql(rows)
    # The selection of reached ids reads its own recursive path CTE. It
    # cannot become a top-level id set referring to an out-of-scope name.
    assert "rebac_path" in sql
    assert "WITH RECURSIVE" in sql
    for pk, alice, bob in rows.values_list("pk", "alice", "bob"):
        assert alice == (pk in {root.pk, child.pk})
        assert not bob


@pytest.mark.parametrize("application_cte", [False, True])
def test_compilation_and_shared_reads_preserve_bypass_audits(shared_rows, application_cte):
    reason = "shared CTE embedded bypass"
    scope = Post.objects.with_actor(ALICE).scoped().values("pk")
    bypass = Post.objects.sudo(reason=reason).filter(pk=OuterRef("pk"))
    rows = (
        share_id_sets(Post._base_manager.all())
        .filter(pk__in=scope)
        .annotate(first=Exists(bypass), second=Exists(bypass))
    )
    if application_cte:
        app = CTE(Post._base_manager.values("pk"), name="audit_posts")
        rows = with_cte(app, select=rows)
    events = PermissionAuditEvent.objects.filter(
        kind=PermissionAuditEvent.KIND_SUDO_BYPASS, reason=reason
    )
    _assert_shared(_sql(rows)[0])
    assert events.count() == 0
    assert list(rows)
    assert events.count() == 1


def test_content_names_are_stable_and_include_parameters(shared_rows):
    local, _, _ = shared_rows
    left, right = (
        Compiler(local.schema(), ALICE, "default", parametric=True),
        Compiler(local.schema(), ALICE, "default", parametric=True),
    )
    ids = Post._base_manager.filter(title="same").order_by().values("pk")
    # Give one compiler an unrelated id set first: registration order is not identity.
    left._ids(Post._base_manager.filter(title="earlier").values("pk"))
    left_name = left._ids(ids).query.get_initial_alias()
    assert right._ids(ids.all()).query.get_initial_alias() == left_name
    assert right._ids(ids.filter(body="different")).query.get_initial_alias() != left_name
    assert len(left_name) <= 63
    assert ctes.signature("query", ({"a": [1, 2], "b": "value"},)) == ctes.signature(
        "query", ({"b": "value", "a": [1, 2]},)
    )


def test_independent_predicate_compilers_share_in_dependency_order(shared_rows):
    local, reference, _ = shared_rows
    rows = share_id_sets(Post._base_manager.all())
    for _ in range(4):
        compiler = Compiler(local.schema(), ALICE, "default", parametric=True)
        first = compiler._ids(
            Post._base_manager.filter(title__in=("direct", "inherited")).values("pk")
        )
        second = compiler._ids(Post._base_manager.filter(pk__in=first).values("pk"))
        statement = compiler.statement(Post._base_manager.filter(pk__in=second).values("pk"))
        rows = rows.filter(pk__in=statement)
    sql, _ = _sql(rows)
    assert len(_assert_shared(sql)) == 2
    assert sql.count("WITH RECURSIVE") == 1
    assert set(rows.values_list("pk", flat=True)) == _expected(reference)


def test_content_name_avoids_model_tables(shared_rows, monkeypatch):
    local, _, _ = shared_rows
    compiler = Compiler(local.schema(), ALICE, "default", parametric=True)
    body = Post._base_manager.filter(title="same").values("pk")
    name = compiler._ids(body).query.get_initial_alias()
    # A model happens to use the generated name as its db_table.
    with monkeypatch.context() as patch:
        patch.setattr(Folder._meta, "db_table", name)
        other = Compiler(local.schema(), ALICE, "default", parametric=True)
        ids = other._ids(body)
        assert ids.query.get_initial_alias() != name
        sql, _ = _sql(other.statement(Post._base_manager.filter(pk__in=ids)))
        assert name not in _assert_shared(sql)


def test_digest_collision_fails_closed(shared_rows, monkeypatch):
    from rebac import SchemaError

    local, _, _ = shared_rows
    # Distinct SQL bodies must never silently become the same definition.
    monkeypatch.setattr("rebac.compile.predicate.id_name", lambda key: "rebac_ids_" + "0" * 16)
    compiler = Compiler(local.schema(), ALICE, "default", parametric=True)
    compiler._ids(Post._base_manager.filter(title="first").values("pk"))
    with pytest.raises(SchemaError, match="Conflicting REBAC"):
        compiler._ids(Post._base_manager.filter(title="second").values("pk"))


def test_collector_is_reset_after_compilation_error(shared_rows, monkeypatch):
    scope = Post.objects.with_actor(ALICE).scoped().values("pk")
    original = ctes._Statement.add

    def fail(self, definitions):
        original(self, definitions)
        raise RuntimeError("compile failed after registering a CTE")

    # Warm the template so the failure is in bound statement assembly.
    _sql(scope)
    with monkeypatch.context() as patch:
        patch.setattr(ctes._Statement, "add", fail)
        with pytest.raises(RuntimeError, match="compile failed"):
            _sql(scope)
    assert ctes._current.get() is None
    _assert_shared(_sql(scope)[0])
    assert set(scope.values_list("pk", flat=True)) == _expected(shared_rows[1])


def test_empty_subject_set_in_negated_arm_keeps_outer_row():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            permission ban = nil
        }
        definition blog/post {
            relation viewer: auth/user
            relation folders: blog/folder // rebac:field=collections
            permission read = viewer - folders->ban
        }
    """)
    actor = SubjectRef.of("auth/user", "empty-cte")
    with sudo(reason="empty CTE fixture"):
        folder = Folder.objects.create(name="folder")
        post = Post.objects.create(title="visible")
        post.collections.add(folder)
    local.write_relationships([RelationshipTuple(to_object_ref(post), "viewer", actor)])
    assert list(Post.objects.with_actor(actor).values_list("pk", flat=True)) == [post.pk]
    assert local.check_access(subject=actor, action="read", resource=to_object_ref(post)).allowed


def test_unhooked_plain_root_retains_nested_with(shared_rows):
    scope = Post.objects.with_actor(ALICE).values("pk")
    rows = Post._base_manager.filter(pk__in=scope).filter(pk__in=scope.all())
    original_sql, _ = _sql(rows)
    assert original_sql.startswith("SELECT ")
    assert original_sql.count("WITH RECURSIVE") == 2
    shared = share_id_sets(rows)
    _assert_shared(_sql(shared)[0])
    assert _sql(rows)[0] == original_sql
    assert set(rows.values_list("pk", flat=True)) == set(shared.values_list("pk", flat=True))
    from django.db.models.sql.query import Query

    assert Query.get_compiler.__module__ == "django.db.models.sql.query"


@pytest.mark.parametrize("application_cte", [False, True])
def test_explain_keeps_one_prefix_and_restores_metadata(shared_rows, application_cte):
    scope = Post.objects.with_actor(ALICE).scoped().values("pk")
    if application_cte:
        app = CTE(Post._base_manager.filter(pk__in=scope).values("pk"), name="explain_posts")
        rows = share_id_sets(with_cte(app, select=Post._base_manager.filter(pk__in=scope)))
    else:
        rows = Post.objects.with_actor(ALICE).filter(pk__in=scope).scoped()
    before = _sql(rows)
    with CaptureQueriesContext(connection) as captured:
        assert rows.explain()
    explained = [item["sql"] for item in captured if item["sql"].startswith("EXPLAIN")]
    assert len(explained) == 1
    assert explained[0].count("WITH RECURSIVE") == 1
    assert all(count == 1 for count in Counter(_definitions(explained[0])).values())
    assert _sql(rows) == before


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_write_roots_keep_scoped_subqueries_and_select_only_allowed_rows(shared_rows, operation):
    _, reference, posts = shared_rows
    expected = _expected(reference)
    scope = Post.objects.with_actor(ALICE).values("pk")
    rows = share_id_sets(Post._base_manager.filter(pk__in=scope).filter(pk__in=scope.all()))
    with sudo(reason="CTE write regression"), CaptureQueriesContext(connection) as captured:
        if operation == "update":
            assert rows.update(title="changed") == len(expected)
            assert (
                set(Post._base_manager.filter(title="changed").values_list("pk", flat=True))
                == expected
            )
        else:
            rows.delete()
            assert (
                set(Post._base_manager.values_list("pk", flat=True))
                == {p.pk for p in posts} - expected
            )
    writes = [q["sql"] for q in captured if operation.upper() in q["sql"]]
    assert writes


@pytest.mark.parametrize("hooked", [False, True])
@pytest.mark.parametrize("operation", ["update", "delete"])
def test_mti_write_keeps_parent_update_keys(shared_rows, django_user_model, hooked, operation):
    """The old query swap made the parent UPDATE rename *both* children."""
    from tests.testapp.models import NativeParentLinkedChild, NativeParentLinkedResource

    owner = django_user_model.objects.create_user(username="cte-mti")
    with sudo(reason="CTE multi-table fixture"):
        first = NativeParentLinkedChild.objects.create(name="first", owner=owner)
        second = NativeParentLinkedChild.objects.create(name="untouched", owner=owner)
    # Keep an application WITH on the actual MTI write, as in the corruption
    # reproducer. It embeds scopes but need not scope this unrelated model.
    scope = Post.objects.with_actor(ALICE).values("pk")
    cte = CTE(Post._base_manager.filter(pk__in=scope).values("pk"), name="mti_visible")
    rows = with_cte(cte, select=NativeParentLinkedChild._base_manager.filter(pk=first.pk))
    if hooked:
        rows = share_id_sets(rows)
    with sudo(reason="CTE multi-table write"):
        if operation == "update":
            # Updating both tables gives django-cte a nonempty child UPDATE
            # as well as Django's separate parent UPDATE.
            # SQLite reports -1 for a WITH-prefixed UPDATE; assert the rows.
            rows.update(name="changed", owner_id=owner.pk)
            assert NativeParentLinkedResource._base_manager.get(pk=first.pk).name == "changed"
        else:
            rows.delete()
            assert not NativeParentLinkedResource._base_manager.filter(pk=first.pk).exists()
    assert NativeParentLinkedResource._base_manager.get(pk=second.pk).name == "untouched"
    assert NativeParentLinkedChild._base_manager.get(pk=second.pk).name == "untouched"


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_one_object_recursive_closure_is_shared_across_constant_arms(settings, storage):
    from rebac import ObjectRef

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema("""
        definition auth/user {}
        definition org/role {
            relation direct: auth/user
            relation includes: org/role
            permission member = direct + includes->member
        }
        definition blog/folder {
            relation admin: org/role // rebac:const=admin
            permission read = admin->member
        }
        definition blog/post {
            relation admin: org/role // rebac:const=admin
            relation viewer: auth/user
            permission read = admin->member + viewer
        }
    """)
    with sudo(reason="one-object closure fixture"):
        post = Post.objects.create(title="role visible")
        folder = Folder.objects.create(name="role visible")
    local.write_relationships(
        [
            RelationshipTuple(
                ObjectRef("org/role", "admin"), "includes", SubjectRef.of("org/role", "child")
            ),
            RelationshipTuple(ObjectRef("org/role", "child"), "direct", ALICE),
        ]
    )
    rows = Post.objects.with_actor(ALICE)
    for _ in range(4):
        rows = rows.filter(Exists(Post.objects.with_actor(ALICE).filter(pk=OuterRef("pk"))))
    rows = rows.filter(Exists(Folder.objects.with_actor(ALICE).filter(pk=folder.pk))).scoped()
    sql, _ = _sql(rows)
    _assert_shared(sql)
    assert sql.count(connection.ops.quote_name("rebac_reached") + " AS (") == 1
    assert list(rows.values_list("pk", flat=True)) == [post.pk]
    schema, tuples, universe = _snapshot(local, using="default")
    reference = ReferenceModel(schema, tuples, now=timezone.now())
    reference.resources.update(universe)
    assert set(rows.values_list("pk", flat=True)) == _expected(reference)
    assert not Post.objects.with_actor(BOB).exists()


def test_kept_sql_is_stable_and_embedding_never_renders_django(shared_rows, monkeypatch):
    from django.db.models.sql.query import Query

    from rebac.compile import read

    sql = _sql(Post.objects.with_actor(ALICE).scoped().values("pk"))[0]
    for actor in (BOB, ALICE):
        assert _sql(Post.objects.with_actor(actor).scoped().values("pk"))[0] == sql
        read.reset()
        assert _sql(Post.objects.with_actor(actor).scoped().values("pk"))[0] == sql
    kept = [
        item
        for item in read._statements.values()
        if isinstance(item, read._Kept) and item.definitions
    ]
    assert kept

    # A warm embed does only parameter binding and registration. No ORM
    # compiler factory, even for the already frozen CTE bodies, is called.
    def forbidden(*args, **kwargs):
        raise AssertionError("kept statement re-rendered Django")

    monkeypatch.setattr(Query, "get_compiler", forbidden)
    for item in kept:
        with ctes.collecting():
            assert item.render(item.params)[0] == f"({item.body})"
        assert not hasattr(item, "query")
        assert "sql" not in item.__slots__


def test_statement_cache_evicts_by_count(shared_rows, monkeypatch):
    from rebac.compile import read

    read.reset()
    monkeypatch.setattr(read, "_LIMIT", 2)
    for actor in (ALICE, BOB):
        for model in (Folder, Post):
            rows = model.objects.with_actor(actor).scoped().values("pk")
            _sql(rows)
            assert len(read._statements) <= 2
    assert set(Post.objects.with_actor(ALICE).values_list("pk", flat=True)) == _expected(
        shared_rows[1]
    )


def test_independent_collectors_are_thread_local(shared_rows, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local

    barrier = Barrier(2)
    state = local()
    scopes = [Post.objects.with_actor(actor).scoped().values("pk") for actor in (ALICE, BOB)]
    expected = [_sql(scope) for scope in scopes]
    original = ctes._Statement.add

    def interleave(self, definitions):
        result = original(self, definitions)
        if definitions and not getattr(state, "waited", False):
            state.waited = True
            barrier.wait(timeout=5)
            assert ctes._current.get() is self
        return result

    monkeypatch.setattr(ctes._Statement, "add", interleave)

    def compile_scope(scope):
        result = _sql(scope)
        assert ctes._current.get() is None
        _assert_shared(result[0])
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(compile_scope, scopes)) == expected


def test_name_collision_between_templates_raises_at_root(shared_rows, monkeypatch):
    from rebac import SchemaError

    monkeypatch.setattr("rebac.compile.predicate.id_name", lambda key: "rebac_ids_" + "0" * 16)
    rows = Post._base_manager.all()
    for title in ("first", "second"):
        compiler = Compiler(shared_rows[0].schema(), ALICE, "default", parametric=True)
        ids = compiler._ids(Post._base_manager.filter(title=title).values("pk"))
        rows = rows.filter(pk__in=compiler.statement(Post._base_manager.filter(pk__in=ids)))
    with pytest.raises(SchemaError, match="Conflicting REBAC"):
        _sql(share_id_sets(rows))


def test_application_name_collision_raises(shared_rows):
    from rebac import SchemaError

    scope = Post.objects.with_actor(ALICE).scoped().values("pk")
    name = _definitions(_sql(scope)[0])[0]
    cte = CTE(Post._base_manager.values("pk"), name=name)
    rows = share_id_sets(with_cte(cte, select=Post._base_manager.filter(pk__in=scope)))
    with pytest.raises(SchemaError, match="application CTE conflicts"):
        _sql(rows)


@pytest.mark.parametrize("operator", ["or", "and"])
def test_predicate_statement_clones_and_boolean_combination_keeps_both_sets(shared_rows, operator):
    left, right = (
        Compiler(shared_rows[0].schema(), ALICE, "default", parametric=True) for _ in range(2)
    )
    original = Post._base_manager.all()
    first = left._ids(original.filter(title="direct").values("pk"))
    second = right._ids(original.filter(title="inherited").values("pk"))
    left_rows = left.statement(original.filter(pk__in=first))
    right_rows = right.statement(original.filter(pk__in=second))
    assert not hasattr(original.query, "_rebac_id_sets")
    rows = left_rows | right_rows if operator == "or" else left_rows & right_rows
    assert len(_assert_shared(_sql(rows)[0])) == 2
    expected = _expected(shared_rows[1]) if operator == "or" else set()
    assert set(rows.values_list("pk", flat=True)) == expected


def test_empty_body_does_not_emit_bare_with():
    item = ctes.Definition("rebac_ids_empty", "SELECT %s", (1,), b"empty")
    assert ctes.assemble(("", ()), (item,), connection) == ("", ())


@pytest.mark.parametrize("hooked", [False, True])
def test_plain_union_root_and_aggregate_keep_valid_cte_boundaries(shared_rows, hooked):
    scope = Post.objects.with_actor(ALICE).scoped().order_by().values("pk")
    plain = Post._base_manager.filter(title="no matching post").order_by().values("pk")
    rows = plain.union(scope, scope.all())
    if hooked:
        rows = share_id_sets(rows)
        _assert_shared(_sql(rows)[0])
    else:
        assert not _sql(rows)[0].startswith("WITH")
    assert set(rows.values_list("pk", flat=True)) == _expected(shared_rows[1])
    assert rows.count() == len(_expected(shared_rows[1]))


@pytest.mark.parametrize("application_cte", [False, True])
@pytest.mark.parametrize("query_only", [False, True])
def test_shared_roots_pickle_and_retain_sharing(shared_rows, application_cte, query_only):
    import pickle

    rows = Post._base_manager.filter(title__in=("direct", "inherited"))
    if application_cte:
        app = CTE(Post._base_manager.values("pk"), name="pickle_posts")
        rows = with_cte(app, select=rows)
    rows = share_id_sets(rows)
    restored = pickle.loads(pickle.dumps(rows.query if query_only else rows))
    query = restored if query_only else restored.query
    assert isinstance(query, ctes.RootQuery)
    assert query.sql_with_params() == _sql(rows)
    # Embed fresh scopes in the restored root: a with_cte round-trip used to
    # silently discard the opt-in, emitting two nested WITHs here.
    scope = Post.objects.with_actor(ALICE).scoped().values("pk")
    selected = QuerySet(model=Post, query=query).filter(pk__in=scope).filter(pk__in=scope.all())
    _assert_shared(_sql(selected)[0])
    assert set(selected.values_list("pk", flat=True)) == _expected(shared_rows[1])


def test_actor_identity_default_is_not_part_of_a_sql_signature(monkeypatch, django_user_model):
    from tests.testapp.models import AuthoredPost

    actor = SubjectRef.of("auth/user", "1")
    local = install_schema("""
        definition auth/user {}
        definition blog/authoredpost {
            relation owner: auth/user // rebac:field=author
            permission read = owner
        }
        definition blog/folder {
            relation posts: blog/authoredpost // rebac:field=authored_posts
            permission read = posts->read
        }
    """)
    with sudo(reason="lambda identity default fixture"):
        owner = django_user_model.objects.create(pk=1, username="lambda-default")
        folder = Folder.objects.create(name="owned")
        AuthoredPost.objects.create(title="visible", folder=folder, author=owner)
    monkeypatch.setattr(django_user_model._meta.pk, "default", lambda: 1)
    rows = Folder.objects.with_actor(actor).scoped().values("pk")
    _assert_shared(_sql(rows)[0])
    assert list(rows.values_list("pk", flat=True)) == [folder.pk]
    assert local.check_access(subject=actor, action="read", resource=to_object_ref(folder)).allowed


@pytest.mark.parametrize(
    "left,right",
    [
        (True, 1),
        (1, 1.0),
        ((True,), (1,)),
        ([True], [1]),
        ({"a": 1}, {"a": True}),
        ({1: "a"}, {True: "a"}),
        ({1}, {True}),
        (frozenset({1}), frozenset({True})),
        ({"a": [{(1,)}]}, {"a": [{(True,)}]}),
    ],
)
def test_bound_parameter_comparison_preserves_types(left, right):
    first = ctes.Definition("rebac_ids_types", "SELECT %s", (left,), b"template")
    second = ctes.Definition(first.name, first.sql, (right,), first.template)
    with ctes.collecting() as statement:
        assert statement.add((first,))
        assert not statement.add((second,))
        assert statement.add((first,))
        assert tuple(statement.definitions.values()) == (first,)


@pytest.mark.parametrize("shared_plain", [False, True])
def test_expiring_scopes_share_one_clock_and_stable_sql(monkeypatch, shared_plain):
    from datetime import timedelta

    from rebac import ObjectRef

    local = install_schema(
        SCHEMA.replace("relation member: auth/user", "relation member: auth/user with expiration")
    )
    instant = timezone.now()
    with sudo(reason="statement clock fixture"):
        folder = Folder.objects.create(name="expires")
        post = Post.objects.create(title="expires", folder=folder)
    local.write_relationships(
        [
            RelationshipTuple(
                to_object_ref(folder), "viewer", SubjectRef.of("auth/group", "g", "member")
            ),
            RelationshipTuple(
                ObjectRef("auth/group", "g"),
                "member",
                ALICE,
                expires_at=instant + timedelta(seconds=5),
            ),
        ]
    )
    rows = (
        share_id_sets(Post._base_manager.all()) if shared_plain else Post.objects.with_actor(ALICE)
    )
    for _ in range(4):
        rows = rows.filter(Exists(Post.objects.with_actor(ALICE).filter(pk=OuterRef("pk"))))
    if not shared_plain:
        rows = rows.scoped()
    _sql(rows)  # Warm all templates before observing clock bindings.
    calls = []

    def tick():
        now = instant + timedelta(seconds=len(calls))
        calls.append(now)
        return now

    monkeypatch.setattr("rebac.clock.application_now", tick)
    first_sql, first_params = _sql(rows)
    second_sql, second_params = _sql(rows)
    assert calls == [instant, instant + timedelta(seconds=1)]
    assert first_sql == second_sql
    _assert_shared(first_sql)
    assert first_sql.count("WITH RECURSIVE") == 1
    prepared = connection.ops.adapt_datetimefield_value(instant)
    assert first_params.count(prepared) > 0
    assert len(first_params) == len(second_params)
    assert {b for a, b in zip(first_params, second_params, strict=True) if a != b} == {
        connection.ops.adapt_datetimefield_value(instant + timedelta(seconds=1))
    }
    assert list(rows.values_list("pk", flat=True)) == [post.pk]
    monkeypatch.setattr("rebac.clock.application_now", lambda: instant + timedelta(seconds=10))
    assert list(rows.values_list("pk", flat=True)) == []
    assert _sql(rows)[0] == first_sql


def test_batched_point_probes_share_sets_and_isolate_outer_collector(shared_rows):
    from rebac import CheckItem

    local, reference, posts = shared_rows
    with ctes.collecting() as outer, CaptureQueriesContext(connection) as captured:
        answers = local.check_bulk_permissions(
            [CheckItem(ALICE, "read", to_object_ref(post)) for post in posts]
        )
        assert not outer.definitions
    assert {p.pk for p, answer in zip(posts, answers, strict=True) if answer.allowed} == _expected(
        reference
    )
    probes = [q["sql"] for q in captured if 'AS "item0"' in q["sql"]]
    assert len(probes) == 1
    _assert_shared(probes[0])
    assert probes[0].count("WITH RECURSIVE") == 1


@pytest.mark.parametrize("materialized", [None, True, False])
def test_empty_left_joined_application_cte_matches_django_cte(shared_rows, materialized):
    from django.db.models.sql.constants import LOUTER

    empty = CTE(
        Post._base_manager.none().values("pk"), name="empty_left_posts", materialized=materialized
    )
    joined = empty.join(Post._base_manager.all(), pk=empty.col.pk, _join_type=LOUTER).annotate(
        empty_pk=empty.col.pk
    )
    plain = with_cte(empty, select=joined).order_by("pk").values_list("pk", "empty_pk")
    shared = share_id_sets(plain)
    assert _sql(shared) == _sql(plain)
    expected = [(post.pk, None) for post in shared_rows[2]]
    assert list(shared) == list(plain) == expected


@pytest.mark.parametrize("how", ["distinct_count", "sliced_count", "plain_count", "exists", "list"])
@pytest.mark.parametrize("root", ["rebac", "shared"])
def test_execution_inside_outer_collector(shared_rows, how, root):
    rows = Post.objects.with_actor(ALICE).scoped()
    if root == "shared":
        rows = share_id_sets(QuerySet(model=Post)).filter(pk__in=rows.values("pk"))
    run = {
        "distinct_count": lambda: rows.values("pk").distinct().count(),
        "sliced_count": lambda: rows.values("pk")[:50].count(),
        "plain_count": lambda: rows.count(),
        "exists": lambda: rows.exists(),
        "list": lambda: len(list(rows.values_list("pk", flat=True))),
    }[how]
    baseline = run()
    with ctes.collecting() as outer:
        inside = run()
    assert inside == baseline
    assert not outer.definitions


@pytest.mark.parametrize("how", ["exists", "list"])
def test_plain_root_with_rebac_subquery_inside_outer_collector(shared_rows, how):
    rows = Folder._base_manager.filter(
        Exists(Post.objects.with_actor(ALICE).filter(folder=OuterRef("pk")))
    )
    run = {"exists": rows.exists, "list": lambda: sorted(rows.values_list("pk", flat=True))}[how]
    baseline = run()
    with ctes.collecting() as outer:
        inside = run()
    assert inside == baseline
    assert not outer.definitions


@pytest.mark.parametrize("root", ["plain", "shared", "cte", "shared_cte", "cte_shared"])
@pytest.mark.parametrize("execute", [False, True])
def test_embedded_bypass_audit_timing_on_plain_root(shared_rows, root, execute):
    from django.contrib.contenttypes.models import ContentType

    inner = Exists(Post.objects.sudo(reason="embedded review").filter(pk=OuterRef("pk")))
    rows = ContentType.objects.all()
    app = CTE(ContentType.objects.values("pk"), name="audit_types")
    if root in {"shared", "shared_cte"}:
        rows = share_id_sets(rows)
    if root in {"cte", "shared_cte", "cte_shared"}:
        rows = with_cte(app, select=rows)
    if root == "cte_shared":
        rows = share_id_sets(rows)
    rows = rows.filter(inner)
    before = PermissionAuditEvent.objects.count()
    if execute:
        list(rows)
    else:
        str(rows.query)
    after = PermissionAuditEvent.objects.count()
    assert after - before == (1 if execute else 0)


def test_assembled_sql_cache_keeps_text_and_fresh_parameter_order(shared_rows, monkeypatch):
    from dataclasses import replace

    from django.db.models.sql.query import ExplainInfo

    body_rows = Post._base_manager.filter(title="body").values("pk")
    definition_rows = Post._base_manager.filter(title="definition").values("pk")
    body = _sql(body_rows)
    definition_sql, definition_params = _sql(definition_rows)
    definition = ctes.Definition("cached_posts", definition_sql, definition_params, b"template")
    expected = _sql(with_cte(CTE(definition_rows, name=definition.name), select=body_rows))
    ctes._assembled_sql.cache_clear()
    try:
        assert ctes.assemble(body, (definition,), connection) == expected
        info = ctes._assembled_sql.cache_info()
        changed = replace(definition, params=("new definition",))
        assert ctes.assemble((body[0], ("new body",)), (changed,), connection) == (
            expected[0],
            ("new definition", "new body"),
        )
        assert ctes._assembled_sql.cache_info().misses == info.misses
        assert ctes._assembled_sql.cache_info().hits == info.hits + 1

        # A reused alias with a different engine must render its own prefix.
        with monkeypatch.context() as patch:
            patch.setattr(connection, "vendor", "cache_test_vendor")
            patch.setattr(connection.ops, "quote_name", lambda name: f"[{name}]")
            switched = ctes.assemble(body, (definition,), connection)
            assert switched[0] != expected[0]
            assert "[cached_posts] AS" in switched[0]
            assert switched[1] == expected[1]
        assert ctes.assemble(body, (definition,), connection) == expected

        for altered in (
            replace(definition, name="renamed_posts"),
            replace(
                definition, sql=_sql(definition_rows.filter(pk__gt=0))[0], params=("definition", 0)
            ),
            replace(definition, materialized=True),
        ):
            assert ctes.assemble(body, (altered,), connection)[0] != expected[0]
        explained = ctes.assemble(body, (definition,), connection, ExplainInfo(None, {}))
        assert explained[0].startswith(connection.ops.explain_query_prefix(None))
        assert explained[1] == expected[1]
        assert ctes.assemble(body, (definition,), connection) == expected

        for i in range(513):
            ctes.assemble(body, (replace(definition, name=f"cached_posts_{i}"),), connection)
        assert ctes._assembled_sql.cache_info().currsize == 512
    finally:
        ctes._assembled_sql.cache_clear()


@pytest.mark.parametrize("root", ["cte_outer", "rebac"])
def test_pickled_query_retains_sharing_with_outer_mixin(shared_rows, root):
    import pickle

    rows = share_id_sets(Post.objects.with_actor(ALICE).all())
    if root == "cte_outer":
        app = CTE(Post._base_manager.values("pk"), name="pickle_outer_posts")
        rows = with_cte(app, select=share_id_sets(Post._base_manager.all()))
    restored = pickle.loads(pickle.dumps(rows.query))
    assert isinstance(restored, ctes.RootQuery)
    scope = Post.objects.with_actor(ALICE).scoped().values("pk")
    rows = QuerySet(model=Post, query=restored).filter(pk__in=scope).filter(pk__in=scope.all())
    _assert_shared(_sql(rows)[0])


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("embedding", ["exists", "in"])
@pytest.mark.parametrize("how", ["exists", "list", "distinct_count", "sliced_count"])
def test_actor_scoped_subquery_owns_plain_outer_execution(shared_rows, shared, embedding, how):
    from django.contrib.contenttypes.models import ContentType

    scope = Post.objects.with_actor(ALICE)
    if embedding == "exists":
        rows = ContentType.objects.filter(Exists(scope.filter(pk=OuterRef("pk"))))
    else:
        rows = ContentType.objects.filter(pk__in=scope.values("pk"))
    if shared:
        rows = share_id_sets(rows)
    run = {
        "exists": rows.exists,
        "list": lambda: sorted(rows.values_list("pk", flat=True)),
        "distinct_count": lambda: rows.values("pk").distinct().count(),
        "sliced_count": lambda: rows.values("pk")[:50].count(),
    }[how]
    audits = PermissionAuditEvent.objects.count()
    baseline = run()
    with ctes.collecting() as outer:
        assert run() == baseline
        assert not outer.definitions
    assert PermissionAuditEvent.objects.count() == audits


@pytest.mark.parametrize("select", ["actor", "sudo", "all"])
@pytest.mark.parametrize("attach", [False, True])
@pytest.mark.parametrize("root_first", [False, True])
def test_root_factory_with_cte_rebac_queryset(shared_rows, select, attach, root_first):
    from rebac import actor_context

    rows = {
        "actor": lambda: Post.objects.with_actor(ALICE),
        "sudo": lambda: Post.objects.sudo(reason="CTE root MRO"),
        "all": Post.objects.all,
    }[select]()
    app = CTE(Post._base_manager.values("pk"), name="app_mro")

    def root(queryset):
        return ctes.attach(queryset, ()) if attach else share_id_sets(queryset)

    rows = with_cte(app, select=root(rows)) if root_first else root(with_cte(app, select=rows))
    with actor_context(ALICE):
        sql, _params = _sql(rows.scoped())
        assert "app_mro" in sql
        assert set(rows.values_list("pk", flat=True)) == (
            {post.pk for post in shared_rows[2]} if select == "sudo" else _expected(shared_rows[1])
        )


def test_accessible_sql_stable_across_actors_and_expiration_instants(monkeypatch):
    from datetime import timedelta

    from rebac import ObjectRef

    instant = timezone.now()
    local = install_schema("""
        definition auth/user {}
        definition auth/group { relation member: auth/user with expiration }
        definition blog/post {
            relation viewer: auth/user | auth/group#member
            permission read = viewer
        }
    """)
    with sudo(reason="enumeration text fixture"):
        posts = [Post.objects.create(title=str(i)) for i in range(3)]
    local.write_relationships(
        [
            RelationshipTuple(
                to_object_ref(posts[0]), "viewer", SubjectRef.of("auth/group", "g", "member")
            ),
            RelationshipTuple(
                ObjectRef("auth/group", "g"),
                "member",
                ALICE,
                expires_at=instant + timedelta(days=1),
            ),
            RelationshipTuple(
                ObjectRef("auth/group", "g"), "member", BOB, expires_at=instant + timedelta(days=1)
            ),
            RelationshipTuple(to_object_ref(posts[1]), "viewer", BOB),
        ]
    )
    texts, bindings = [], []
    for index, actor in enumerate((ALICE, ALICE, BOB)):
        monkeypatch.setattr(
            "rebac.clock.application_now", lambda now=instant + timedelta(seconds=index): now
        )
        sqls, params = [], []

        def capture(execute, sql, values, many, context, *, sqls=sqls, params=params):
            if "rebac_relationship" in sql:
                sqls.append(sql)
                params.append(values)
            return execute(sql, values, many, context)

        with connection.execute_wrapper(capture):
            found = set(local.accessible(subject=actor, action="read", resource_type="blog/post"))
        assert found == (
            {str(posts[0].pk), str(posts[1].pk)} if actor == BOB else {str(posts[0].pk)}
        )
        assert sqls
        texts.append(tuple(sqls))
        bindings.append(tuple(params))
    assert texts[0] == texts[1] == texts[2]
    assert bindings[0] != bindings[1] != bindings[2]


# ---------- Id sets written in place, where SQLite counts a CTE's copies ----------

sqlite_only = pytest.mark.skipif(
    connection.vendor != "sqlite", reason="SQLite's limit on the copies of a CTE"
)
LEAF, MIDDLE = "rebac_ids_00000000000000aa", "rebac_ids_00000000000000bb"


@contextmanager
def _written_in_place(monkeypatch, budget=0):
    """As on SQLite 3.54: a statement at or past ``budget`` cursors has no id set by name."""
    with monkeypatch.context() as patch:
        patch.setattr(ctes, "_counts_cte_copies", lambda connection: True)
        patch.setattr(ctes, "_CURSOR_BUDGET", budget)
        yield


def _names_no_id_set(sql):
    """No id set is defined or selected from by name (a column may still be qualified by one)."""
    return not re.search(_id_pattern() + r" AS \(|(?:FROM|JOIN) " + _id_pattern() + r"(?!\.)", sql)


def _handwritten():
    leaf = ctes.Definition(LEAF, 'SELECT %s AS "v", \'100%%\' AS "p"', ("leaf",), b"leaf")
    middle = ctes.Definition(
        MIDDLE, f'SELECT %s || "U0"."v" || %s AS "v" FROM "{LEAF}" "U0"', ("<", ">"), b"middle"
    )
    # An application CTE: it has no template, and reads a set it gives no alias.
    application = ctes.Definition(
        "application", f'SELECT %s || "v" AS "v" FROM "{MIDDLE}"', ("app:",), b""
    )
    # A set under an alias written with AS, a set without one, and a set's
    # column after a FROM that is not a clause's.
    body = (
        f'SELECT %s, "application"."v", "V0"."v", "{LEAF}"."v", "{LEAF}"."p", %s'
        f' FROM "application" INNER JOIN "{MIDDLE}" AS "V0" ON (1 = 1)'
        f' INNER JOIN "{LEAF}" ON (1 = 1)'
        f' WHERE %s = %s AND "V0"."v" IS DISTINCT FROM "{LEAF}"."v"',
        ("first", "last", 1, 1),
    )
    return body, (leaf, middle, application)


def _rows(statement):
    with connection.cursor() as cursor:
        cursor.execute(*statement)
        return cursor.fetchall()


@sqlite_only
def test_values_follow_the_text_of_a_statement_written_in_place(monkeypatch):
    body, definitions = _handwritten()
    named = ctes.assemble(body, definitions, connection)
    assert named[1] == ("leaf", "<", ">", "app:", "first", "last", 1, 1)
    with _written_in_place(monkeypatch):
        placed = ctes.assemble(body, definitions, connection)
    sql, params = placed
    # The application CTE keeps its name and comes first; every id set is
    # written where it was named, as often as it was named, with its values.
    assert params == ("app:", "<", ">", "leaf", "first", "last", "<", ">", "leaf", "leaf", 1, 1)
    assert sql.count("WITH RECURSIVE") == 1 and sql.startswith('WITH RECURSIVE "application" AS (')
    assert _names_no_id_set(sql)
    # A set named without an alias keeps its name for the columns it qualifies.
    assert f') "{LEAF}" ON (1 = 1)' in sql and f') "{MIDDLE}")' in sql
    assert ') "V0" ON (1 = 1)' in sql and sql.endswith(f'IS DISTINCT FROM "{LEAF}"."v"')
    assert sql.count("'100%%'") == 3
    expected = [("first", "app:<leaf>", "<leaf>", "leaf", "100%", "last")]
    assert _rows(named) == _rows(placed) == expected


@sqlite_only
def test_a_statement_within_the_budget_keeps_its_text(monkeypatch):
    body, definitions = _handwritten()
    named = ctes.assemble(body, definitions, connection)
    # Five FROM items are written and the middle set's one is read twice; the
    # FROM of IS DISTINCT FROM is counted with them, on the safe side.
    texts = (*((f'"{d.name}"' if d.template else None, d.sql, len(d.params)) for d in definitions),)
    leaf, middle, application = definitions
    # Its length in place, counting each text once for each place it is written.
    size = len(body[0]) + len(application.sql) + 2 * len(middle.sql) + 3 * len(leaf.sql)
    assert ctes._unfolded((*texts, (None, body[0], len(body[1])))) == (7, size)
    with _written_in_place(monkeypatch, budget=8):
        assert ctes.assemble(body, definitions, connection) == named
    with _written_in_place(monkeypatch, budget=7):
        assert ctes.assemble(body, definitions, connection) != named


@sqlite_only
@pytest.mark.parametrize(
    "why",
    [
        "a second binding",
        "a set that names itself",
        "values without placeholders",
        "too many cursors",
        "too long",
    ],
)
def test_a_statement_that_cannot_be_written_in_place_keeps_its_names(monkeypatch, why):
    body, definitions = _handwritten()
    leaf, middle, application = definitions
    if why == "a second binding":
        # Another binding of the same set kept its nested WITH: the references
        # inside it are its own, and the text does not tell them from the rest.
        nested = (
            f'(WITH RECURSIVE "{LEAF}" AS (SELECT %s AS "v") SELECT "U0"."v" FROM "{LEAF}" "U0")'
        )
        body = (body[0] + f" AND %s IN {nested}", (*body[1], "other", "other"))
    elif why == "a set that names itself":
        recursive = replace(leaf, sql=leaf.sql + f' UNION SELECT "v", "p" FROM "{LEAF}"')
        definitions = (recursive, middle, application)
    elif why == "values without placeholders":
        definitions = (replace(leaf, params=("leaf", "stray")), middle, application)
    elif why == "too many cursors":
        monkeypatch.setattr(ctes, "_CURSOR_CEILING", 5)
    else:
        monkeypatch.setattr(ctes, "_IN_PLACE_SIZE", 400)
    named = (
        ctes.assemble(body, definitions, connection)
        if why != "values without placeholders"
        else None
    )
    with _written_in_place(monkeypatch):
        assert ctes._in_place(body, definitions, connection.ops.quote_name, 0) == (
            body,
            definitions,
        )
        if named is not None:
            assert ctes.assemble(body, definitions, connection, second=True) == named


@sqlite_only
def test_a_policy_whose_paths_multiply_is_not_written_out(monkeypatch):
    # Forty sets, each read twice by the next and binding nothing (arrows over
    # model fields): 2 ** 40 copies of the first. It is counted, never built.
    names = [f"rebac_ids_{level:016x}" for level in range(40)]
    definitions = [ctes.Definition(names[0], 'SELECT "id" FROM "testapp_post"', (), b"0")]
    for level in range(1, 40):
        below = names[level - 1]
        sql = (
            f'SELECT "U0"."id" FROM "{below}" "U0" WHERE "U0"."id" IN'
            f' (SELECT "U1"."id" FROM "{below}" "U1")'
        )
        definitions.append(ctes.Definition(names[level], sql, (), str(level).encode()))
    body = (f'SELECT "V0"."id" FROM "{names[-1]}" "V0"', ())
    monkeypatch.setattr(ctes, "_placed", None)
    named = ctes.assemble(body, tuple(definitions), connection)
    assert named[0].startswith("WITH RECURSIVE ") and len(named[0]) < 10_000
    with _written_in_place(monkeypatch):
        assert ctes.assemble(body, tuple(definitions), connection) == named


def _read_statements(shared_rows):
    """Reads that embed permission scopes each way a statement can."""
    scope = Post.objects.with_actor(ALICE).scoped().order_by().values("pk")
    branches = [
        Post.objects.with_actor(ALICE).filter(title=title).scoped().order_by().values("pk")
        for title in ("inherited", "direct", "hidden")
    ]
    application = CTE(
        Post._base_manager.filter(pk__in=scope).values("pk"), name="application_posts"
    )
    return {
        "scoped root": Post.objects.with_actor(ALICE)
        .scoped()
        .order_by("pk")
        .values_list("pk", "title"),
        "shared root": share_id_sets(Post._base_manager.all())
        .filter(Exists(scope.filter(pk=OuterRef("pk"))), pk__in=scope.all())
        .annotate(seen=Subquery(scope.filter(pk=OuterRef("pk"))[:1]), marker=Value("body"))
        .order_by("pk")
        .values_list("pk", "seen", "marker"),
        "plain root": Post._base_manager.filter(pk__in=scope)
        .filter(pk__in=scope.all())
        .order_by("pk")
        .values_list("pk", flat=True),
        "union": branches[0].union(*branches[1:]).order_by("pk"),
        "two actors": share_id_sets(Post._base_manager.all())
        .annotate(
            alice=Exists(Post.objects.with_actor(ALICE).filter(pk=OuterRef("pk"))),
            bob=Exists(Post.objects.with_actor(BOB).filter(pk=OuterRef("pk"))),
        )
        .order_by("pk")
        .values_list("pk", "alice", "bob"),
        "application CTE": share_id_sets(
            with_cte(application, select=QuerySet(model=Post, query=application.queryset().query))
        )
        .filter(pk__in=scope)
        .order_by("pk")
        .values_list("pk", flat=True),
    }


@sqlite_only
def test_reads_written_in_place_return_the_rows_of_the_named_form(shared_rows, monkeypatch):
    local, reference, posts = shared_rows
    expected, expected_bob = _expected(reference), _expected(reference, BOB)
    statements = _read_statements(shared_rows)
    named = {label: (_sql(rows), list(rows)) for label, rows in statements.items()}
    assert [pk for pk, _title in named["scoped root"][1]] == sorted(expected)
    assert [(pk, pk in expected, pk in expected_bob) for pk, _, _ in named["two actors"][1]] == (
        named["two actors"][1]
    )
    items = [
        CheckItem(actor, "read", to_object_ref(post)) for post in posts for actor in (ALICE, BOB)
    ]
    checked = [answer.allowed for answer in local.check_bulk_permissions(items)]
    with _written_in_place(monkeypatch):
        for label, rows in statements.items():
            rows = rows.all()
            sql, params = _sql(rows)
            assert sql != named[label][0][0], label
            assert _names_no_id_set(sql), label
            assert sql.count("WITH RECURSIVE") == (label == "application CTE"), label
            assert sql.count("%s") == len(params), label
            assert list(rows) == named[label][1], label
        assert [a.allowed for a in local.check_bulk_permissions(items)] == checked
        for actor, held in ((ALICE, expected), (BOB, expected_bob)):
            found = local.accessible(subject=actor, action="read", resource_type="blog/post")
            assert {int(pk) for pk in found} == held
        with CaptureQueriesContext(connection) as captured:
            assert statements["scoped root"].explain()
        assert [q["sql"] for q in captured if q["sql"].startswith("EXPLAIN")]
    # Past the context a statement has its names again: nothing kept was rewritten.
    for label, rows in statements.items():
        assert _sql(rows.all()) == named[label][0], label
    # Under the budget, where the database counts copies: the text of the named
    # form, but for a second binding, which is written in place at any size.
    with monkeypatch.context() as patch:
        patch.setattr(ctes, "_counts_cte_copies", lambda connection: True)
        for label, rows in statements.items():
            if label != "two actors":
                assert _sql(rows.all()) == named[label][0], label
        both = statements["two actors"].all()
        assert _sql(both)[0].count("WITH RECURSIVE") == 1
        assert max(Counter(_definitions(_sql(both)[0])).values()) == 1
        assert list(both) == named["two actors"][1]


@sqlite_only
@pytest.mark.parametrize("operation", ["update", "delete"])
def test_writes_scoped_in_place_select_only_allowed_rows(shared_rows, monkeypatch, operation):
    _, reference, posts = shared_rows
    expected = _expected(reference)
    scope = Post.objects.with_actor(ALICE).values("pk")
    rows = Post._base_manager.filter(pk__in=scope).filter(pk__in=scope.all())
    with (
        _written_in_place(monkeypatch),
        sudo(reason="in-place write"),
        CaptureQueriesContext(connection) as captured,
    ):
        if operation == "update":
            assert rows.update(title="changed") == len(expected)
            kept = set(Post._base_manager.filter(title="changed").values_list("pk", flat=True))
            assert kept == expected
        else:
            rows.delete()
            left = set(Post._base_manager.values_list("pk", flat=True))
            assert left == {post.pk for post in posts} - expected
    writes = [q["sql"] for q in captured if q["sql"].startswith(operation.upper())]
    assert writes and all(_names_no_id_set(sql) and "WITH RECURSIVE" not in sql for sql in writes)


def test_only_sqlite_from_3_54_has_id_sets_written_in_place():
    counted = connection.vendor == "sqlite" and sqlite3.sqlite_version_info >= (3, 54)
    assert ctes._counts_cte_copies(connection) is counted
