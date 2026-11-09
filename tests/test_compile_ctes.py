"""Statement-wide id sets, with recursive CTEs kept at their own boundary."""

import re
from collections import Counter

import pytest
from django.db import connection
from django.db.models import Count, Exists, OuterRef, Q, QuerySet, Subquery, Value
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_cte import CTE, with_cte

from rebac import RelationshipTuple, SubjectRef, share_id_sets, sudo, to_object_ref
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


def test_different_actor_bindings_do_not_alias(shared_rows):
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
    assert sql.count("WITH RECURSIVE") > 1
    assert max(Counter(_definitions(sql)).values()) == 2
    assert "alice" in params and "bob" in params
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
        Compiler(local.schema(), ALICE, "default"),
        Compiler(local.schema(), ALICE, "default"),
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
        compiler = Compiler(local.schema(), ALICE, "default")
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
    compiler = Compiler(local.schema(), ALICE, "default")
    body = Post._base_manager.filter(title="same").values("pk")
    name = compiler._ids(body).query.get_initial_alias()
    # A model happens to use the generated name as its db_table.
    with monkeypatch.context() as patch:
        patch.setattr(Folder._meta, "db_table", name)
        other = Compiler(local.schema(), ALICE, "default")
        ids = other._ids(body)
        assert ids.query.get_initial_alias() != name
        sql, _ = _sql(other.statement(Post._base_manager.filter(pk__in=ids)))
        assert name not in _assert_shared(sql)


def test_digest_collision_fails_closed(shared_rows, monkeypatch):
    from rebac import SchemaError

    local, _, _ = shared_rows
    # Distinct SQL bodies must never silently become the same definition.
    monkeypatch.setattr("rebac.compile.predicate.id_name", lambda key: "rebac_ids_" + "0" * 16)
    compiler = Compiler(local.schema(), ALICE, "default")
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


def test_one_object_recursive_closure_is_shared_across_constant_arms():
    from rebac import ObjectRef

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
        compiler = Compiler(shared_rows[0].schema(), ALICE, "default")
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
    left, right = (Compiler(shared_rows[0].schema(), ALICE, "default") for _ in range(2))
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
    assert isinstance(query, ctes._RootQuery)
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


@pytest.mark.parametrize("left,right", [(True, 1), (1, 1.0), ((True,), (1,)), ([True], [1])])
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
