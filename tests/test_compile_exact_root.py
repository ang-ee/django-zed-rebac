"""Resolved mandatory identities use candidate-root SQL without a separate check."""

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Exists, OuterRef, Prefetch, Q, Subquery, Value
from django.db.models.expressions import ColPairs
from django.db.models.fields.tuple_lookups import TupleIn
from django.db.models.lookups import Exact, In
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.compile import read
from rebac.errors import SchemaError
from rebac.managers import share_scoped_definitions
from rebac.schema import parse_zed
from rebac.testing import install_schema
from rebac.types import RelationshipFilter
from tests.testapp.models import EncodedPrimaryPost, Folder, Post, TextIdentityPrimaryPost

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")
SCHEMA = """
definition auth/user {}
definition auth/group { relation member: auth/user }
definition blog/folder {
    relation viewer: auth/user
    permission read = viewer
}
definition blog/post {
    relation viewer: auth/user | auth/group#member
    relation banned: auth/user | auth/group#member
    permission read = viewer - banned
}
"""


@pytest.fixture
def records():
    local = install_schema(SCHEMA)
    with sudo(reason="test.fixture"):
        folder = Folder.objects.create(name="container")
        allowed = Post.objects.create(title="allowed", folder=folder)
        denied = Post.objects.create(title="denied", folder=folder)
    grant = RelationshipTuple(to_object_ref(allowed), "viewer", ALICE)
    local.write_relationships([grant])
    return local, allowed, denied, grant


@pytest.fixture
def paths(monkeypatch):
    """Observe which native statement owns the queried model's row decision."""
    calls = {"point": [], "scope": [], "candidates": []}
    point, scope = read._point_statement, read._scope_statement

    def point_statement(operation, key, *args, **kwargs):
        calls["point"].append(key)
        return point(operation, key, *args, **kwargs)

    def scope_statement(operation, model, key, *args, **kwargs):
        calls["scope"].append(key)
        calls["candidates"].append(kwargs.get("candidates"))
        return scope(operation, model, key, *args, **kwargs)

    monkeypatch.setattr(read, "_point_statement", point_statement)
    monkeypatch.setattr(read, "_scope_statement", scope_statement)
    return calls


def _tuple_in(model, rhs):
    field = model._meta.pk
    return TupleIn(ColPairs(model._meta.db_table, [field], [field], field), rhs)


@pytest.mark.parametrize(
    "shape",
    [
        "pk",
        "id",
        "exact",
        "in",
        "in_tuple",
        "tuple_in",
        "tuple_in_lists",
        "tuple_in_multi",
        "and",
        "chain",
    ],
)
def test_native_identity_selectors_use_candidate_sql_and_preserve_denial(records, paths, shape):
    _local, allowed, denied, _grant = records
    for row in (allowed, denied):
        rows = Post.objects.with_actor(ALICE)
        selectors = {
            "pk": Q(pk=row.pk),
            "id": Q(id=row.pk),
            "exact": Q(Exact(Post._meta.pk.get_col(Post._meta.db_table), row.pk)),
            "in": Q(pk__in=[row.pk]),
            "in_tuple": Q(pk__in=(row.pk,)),
            "tuple_in": Q(_tuple_in(Post, [(row.pk,)])),
            "tuple_in_lists": Q(_tuple_in(Post, ([row.pk],))),
            "tuple_in_multi": Q(_tuple_in(Post, [(allowed.pk,), (denied.pk,)])),
            "and": Q(pk=row.pk) & (Q(title="allowed") | Q(title="denied")),
            "chain": Q(title=row.title),
        }
        selected = rows.filter(selectors[shape])
        if shape == "chain":
            selected = selected.filter(pk=row.pk)
        assert list(selected.values_list("pk", flat=True)) == (
            [allowed.pk] if shape == "tuple_in_multi" or row is allowed else []
        )
    assert ("blog/post", "read") not in paths["point"]
    assert ("blog/post", "read") in paths["scope"]
    if shape == "tuple_in_multi":
        assert (allowed.pk, denied.pk) in paths["candidates"]
    else:
        assert (allowed.pk,) in paths["candidates"]
        assert (denied.pk,) in paths["candidates"]


@pytest.mark.parametrize("operation", ["get", "exists", "count", "in_bulk"])
def test_ordinary_queryset_terminals_keep_their_native_contract(records, paths, operation):
    _local, allowed, _denied, _grant = records
    rows = Post.objects.with_actor(ALICE)
    if operation == "get":
        assert rows.get(pk=allowed.pk).pk == allowed.pk
    elif operation == "exists":
        assert rows.filter(pk=allowed.pk).exists()
    elif operation == "count":
        assert rows.filter(pk=allowed.pk).count() == 1
    else:
        assert set(rows.in_bulk([allowed.pk])) == {allowed.pk}
    assert ("blog/post", "read") not in paths["point"]
    assert ("blog/post", "read") in paths["scope"]
    assert (allowed.pk,) in paths["candidates"]


class CustomExact(Exact):
    pass


class CustomIn(In):
    pass


@pytest.mark.parametrize(
    "shape",
    [
        "or",
        "not",
        "xor",
        "multi",
        "duplicates",
        "expression",
        "custom",
        "custom_in",
        "other_column",
        "multiple_with_null",
    ],
)
def test_non_point_predicates_use_a_scope_statement(records, paths, shape):
    _local, allowed, denied, _grant = records
    rows = Post.objects.with_actor(ALICE)
    selectors = {
        "or": Q(pk=allowed.pk) | Q(pk=denied.pk),
        "not": ~Q(pk=denied.pk),
        "xor": Q(pk=allowed.pk) ^ Q(pk=denied.pk),
        "multi": Q(pk__in=[allowed.pk, denied.pk]),
        "duplicates": Q(pk__in=[allowed.pk, allowed.pk]),
        "expression": Q(pk=Value(allowed.pk)),
        "custom": Q(CustomExact(Post._meta.pk.get_col(Post._meta.db_table), allowed.pk)),
        "custom_in": Q(CustomIn(Post._meta.pk.get_col(Post._meta.db_table), [allowed.pk])),
        "other_column": Q(folder_id=allowed.folder_id),
        "multiple_with_null": Q(pk__in=[allowed.pk, None]),
    }
    assert list(rows.filter(selectors[shape]).values_list("pk", flat=True)) == [allowed.pk]
    assert ("blog/post", "read") not in paths["point"]
    assert ("blog/post", "read") in paths["scope"]
    assert paths["candidates"][-1] == ((allowed.pk, denied.pk) if shape == "multi" else None)


def test_clones_reprove_the_selected_identity_and_actor(records, paths):
    local, allowed, denied, _grant = records
    local.write_relationships([RelationshipTuple(to_object_ref(denied), "viewer", BOB)])
    base = Post.objects.with_actor(ALICE).filter(pk=allowed.pk)
    assert list(base.values_list("pk", flat=True)) == [allowed.pk]
    assert not base.with_actor(BOB).exists()
    assert not base.filter(pk=denied.pk).exists()
    assert not base.exclude(pk=allowed.pk).exists()
    assert list(base.all().values_list("pk", flat=True)) == [allowed.pk]


@pytest.mark.parametrize("embedding", ["in", "exists", "subquery"])
@pytest.mark.parametrize("selector", ["exact", "tuple_in"])
def test_relabelled_subquery_aliases_keep_the_exact_inner_scope(
    records, paths, embedding, selector
):
    _local, allowed, denied, _grant = records
    predicate = Q(pk=allowed.pk) if selector == "exact" else Q(_tuple_in(Post, [(allowed.pk,)]))
    inner = Post.objects.with_actor(ALICE).filter(predicate)
    outer = Post._base_manager.order_by("pk")
    if embedding == "in":
        result = outer.filter(pk__in=inner.values("pk"))
    elif embedding == "exists":
        result = outer.filter(Exists(inner.filter(pk=OuterRef("pk"))))
    else:
        result = outer.filter(pk=Subquery(inner.values("pk")[:1]))
    assert list(result.values_list("pk", flat=True)) == [allowed.pk]
    assert denied.pk != allowed.pk
    assert (allowed.pk,) in paths["candidates"]


@pytest.mark.parametrize("selector", ["exact", "tuple_in"])
def test_candidate_scope_keeps_the_original_query_predicates(records, selector):
    _local, allowed, denied, _grant = records
    predicate = Q(pk=allowed.pk) if selector == "exact" else Q(_tuple_in(Post, [(allowed.pk,)]))
    assert not Post.objects.with_actor(ALICE).filter(predicate, title=denied.title).exists()


@pytest.mark.parametrize("selector", ["exact", "tuple_in"])
def test_revocation_after_candidate_compilation_is_witnessed_by_selecting_sql(
    records, monkeypatch, selector
):
    local, allowed, _denied, grant = records
    compile_scope = read._scope_statement
    changed = []

    def revoke(operation, model, key, *args, **kwargs):
        compiled = compile_scope(operation, model, key, *args, **kwargs)
        if key == ("blog/post", "read") and kwargs.get("candidates") and not changed:
            changed.append(True)
            local.delete_relationship(grant)
        return compiled

    monkeypatch.setattr(read, "_scope_statement", revoke)
    predicate = Q(pk=allowed.pk) if selector == "exact" else Q(_tuple_in(Post, [(allowed.pk,)]))
    assert not Post.objects.with_actor(ALICE).filter(predicate).exists()
    assert changed


def test_policy_replacement_during_candidate_compile_cannot_open_a_closed_scope(
    records, monkeypatch
):
    local, allowed, _denied, _grant = records
    local.set_schema(
        parse_zed("definition auth/user {} definition blog/post { permission read = nil }")
    )
    rows = Post.objects.with_actor(ALICE).filter(pk=allowed.pk)
    compile_scope = read._scope_statement
    changed = []

    def replace(operation, model, key, *args, **kwargs):
        if key == ("blog/post", "read") and kwargs.get("candidates") and not changed:
            changed.append(True)
            local.set_schema(
                parse_zed(
                    "definition auth/user {} definition blog/post { permission read = authenticated }"
                )
            )
        return compile_scope(operation, model, key, *args, **kwargs)

    monkeypatch.setattr(read, "_scope_statement", replace)
    assert list(rows) == []
    assert changed


@pytest.mark.parametrize(
    "model,identity", [(TextIdentityPrimaryPost, "17"), (EncodedPrimaryPost, "item-17")]
)
def test_text_and_custom_integer_identity_fields_keep_general_scope(paths, model, identity):
    resource_type = model._meta.rebac_resource_type
    local = install_schema(f"""
        definition auth/user {{}}
        definition {resource_type} {{
            relation viewer: auth/user
            permission read = viewer
        }}
    """)
    author = get_user_model().objects.create(username="author")
    with sudo(reason="test.fixture"):
        row = model.objects.create(pk=identity, title="visible", author=author)
    local.write_relationships([RelationshipTuple(to_object_ref(row), "viewer", ALICE)])
    query = model.objects.with_actor(ALICE).filter(pk=identity).values_list("pk", flat=True)
    if model is EncodedPrimaryPost:
        # The generic compiler's existing rejection is not bypassed or reinterpreted.
        with pytest.raises(SchemaError, match="unsupported encoded identity"):
            list(query)
    else:
        assert list(query) == [identity]
    assert (resource_type, "read") not in paths["point"]
    assert (resource_type, "read") in paths["scope"]


@pytest.mark.parametrize("change", ["revoke_viewer", "add_banned"])
def test_stored_set_changes_after_candidate_compilation_are_witnessed(records, monkeypatch, change):
    local, allowed, _denied, direct = records
    local.delete_relationship(direct)
    viewers = ObjectRef("auth/group", "viewers")
    banned = ObjectRef("auth/group", "banned")
    member = RelationshipTuple(viewers, "member", ALICE)
    local.write_relationships(
        [
            member,
            RelationshipTuple(
                to_object_ref(allowed), "viewer", SubjectRef.of("auth/group", "viewers", "member")
            ),
            RelationshipTuple(
                to_object_ref(allowed), "banned", SubjectRef.of("auth/group", "banned", "member")
            ),
        ]
    )
    compile_scope = read._scope_statement
    changed = []

    def mutate(operation, model, key, *args, **kwargs):
        statement = compile_scope(operation, model, key, *args, **kwargs)
        if key == ("blog/post", "read") and kwargs.get("candidates") and not changed:
            changed.append(True)
            if change == "revoke_viewer":
                local.delete_relationship(member)
            else:
                local.write_relationships([RelationshipTuple(banned, "member", ALICE)])
        return statement

    monkeypatch.setattr(read, "_scope_statement", mutate)
    assert not Post.objects.with_actor(ALICE).filter(pk=allowed.pk).exists()
    assert changed


def test_nonunique_native_integer_identity_keeps_general_scope(records, paths, monkeypatch):
    _local, allowed, denied, _grant = records
    # A native FK attname is a scalar identity, but its values need not be unique.
    monkeypatch.setattr(Post._meta, "rebac_id_attr", "folder_id", raising=False)
    local = install_schema(SCHEMA)
    local.write_relationships(
        [RelationshipTuple(ObjectRef("blog/post", str(allowed.folder_id)), "viewer", ALICE)]
    )
    assert not Post._meta.get_field("folder").unique
    assert set(
        Post.objects.with_actor(ALICE)
        .filter(folder_id=allowed.folder_id)
        .values_list("pk", flat=True)
    ) == {allowed.pk, denied.pk}
    assert ("blog/post", "read") not in paths["point"]
    assert ("blog/post", "read") in paths["scope"]


@pytest.fixture(params=["registry", "denormalized"])
def prefetch_records(request, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    return request.getfixturevalue("records")


def test_native_forward_fk_prefetch_uses_singleton_candidate_and_preserves_denial(
    prefetch_records, paths
):
    local, allowed, _denied, _grant = prefetch_records
    local.write_relationships([RelationshipTuple(to_object_ref(allowed.folder), "viewer", ALICE)])
    for actor in (ALICE, BOB):
        row = (
            Post._base_manager.filter(pk=allowed.pk)
            .prefetch_related(Prefetch("folder", queryset=Folder.objects.with_actor(actor)))
            .get()
        )
        if actor == ALICE:
            assert row.folder.pk == allowed.folder_id
        else:
            assert row.folder is None
    assert ("blog/folder", "read") not in paths["point"]
    assert ("blog/folder", "read") in paths["scope"]
    assert (allowed.folder_id,) in paths["candidates"]


class CustomTupleIn(TupleIn):
    pass


class CustomColPairs(ColPairs):
    pass


@pytest.mark.parametrize(
    "shape",
    [
        "multirow",
        "duplicates",
        "composite",
        "null",
        "bool",
        "string",
        "expression",
        "subquery",
        "custom_lookup",
        "custom_columns",
        "scalar_tuple",
        "other_column",
        "other_alias",
        "or",
        "not",
        "xor",
    ],
)
def test_tuple_identity_shapes_use_only_proven_candidates(records, shape):
    _local, allowed, denied, _grant = records
    field = Post._meta.pk
    alias = Post._meta.db_table
    columns = ColPairs(alias, [field], [field], field)
    selector = _tuple_in(Post, [(allowed.pk,)])
    if shape in {"multirow", "duplicates"}:
        other = denied.pk if shape == "multirow" else allowed.pk
        selector = _tuple_in(Post, [(allowed.pk,), (other,)])
    elif shape == "composite":
        title = Post._meta.get_field("title")
        selector = TupleIn(
            ColPairs(alias, [field, title], [field, title], field), [(allowed.pk, allowed.title)]
        )
    elif shape in {"null", "bool", "string", "expression"}:
        value = {
            "null": None,
            "bool": True,
            "string": str(allowed.pk),
            "expression": Value(allowed.pk),
        }[shape]
        selector = _tuple_in(Post, [(value,)])
    elif shape == "subquery":
        selector = TupleIn(columns, Post._base_manager.filter(pk=allowed.pk).values("pk").query)
    elif shape == "custom_lookup":
        selector = CustomTupleIn(columns, [(allowed.pk,)])
    elif shape == "custom_columns":
        selector = TupleIn(CustomColPairs(alias, [field], [field], field), [(allowed.pk,)])
    elif shape == "scalar_tuple":
        selector = TupleIn((field.get_col(alias),), [(allowed.pk,)])
    elif shape == "other_column":
        foreign = Post._meta.get_field("folder")
        selector = TupleIn(ColPairs(alias, [foreign], [foreign], foreign), [(allowed.folder_id,)])
    elif shape == "other_alias":
        selector = TupleIn(ColPairs("other_alias", [field], [field], field), [(allowed.pk,)])
    predicate = Q(selector)
    if shape == "or":
        predicate |= Q(pk=denied.pk)
    elif shape == "not":
        predicate = ~predicate
    elif shape == "xor":
        predicate ^= Q(pk=denied.pk)
    query = Post._base_manager.filter(predicate).query
    found = read._known_identities(Post, field.get_col(alias), query.where, "default")
    assert found == ((allowed.pk, denied.pk) if shape == "multirow" else None)


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_recursive_identity_sets_have_candidate_bounded_bytes_and_statements(storage, monkeypatch):
    schema = """
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
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        local = install_schema(schema)
        with sudo(reason="test.fixture"):
            allowed_root = Folder.objects.create(name="allowed")
            denied_root = Folder.objects.create(name="denied")
            allowed_child = Folder.objects.create(name="allowed child", parent=allowed_root)
            denied_child = Folder.objects.create(name="denied child", parent=denied_root)
            allowed_leaf = Folder.objects.create(name="allowed leaf", parent=allowed_child)
            denied_leaf = Folder.objects.create(name="denied leaf", parent=denied_child)
            posts = Post.objects.bulk_create(
                [
                    Post(
                        title=f"post {index}",
                        folder=allowed_leaf if index % 2 == 0 else denied_leaf,
                    )
                    for index in range(100)
                ]
            )
        local.write_relationships([RelationshipTuple(to_object_ref(allowed_root), "viewer", ALICE)])

        bounded = []
        original = read._scope_statement

        def observed(operation, model, key, *args, **kwargs):
            if model is Post and kwargs.get("candidates") is not None:
                bounded.append(kwargs["candidates"])
            return original(operation, model, key, *args, **kwargs)

        monkeypatch.setattr(read, "_scope_statement", observed)
        sizes: dict[int, tuple[int, int, int, int]] = {}
        for size in (1, 5, 50):
            ids = [post.pk for post in posts[:size]]
            expected = [post.pk for post in posts[:size:2]]
            with CaptureQueriesContext(connection) as point_queries:
                for index, post in enumerate(posts[:size]):
                    assert list(
                        Post.objects.with_actor(ALICE)
                        .filter(pk=post.pk)
                        .values_list("pk", flat=True)
                    ) == ([post.pk] if index % 2 == 0 else [])
            with CaptureQueriesContext(connection) as set_queries:
                assert (
                    list(
                        Post.objects.with_actor(ALICE)
                        .filter(pk__in=ids)
                        .order_by("pk")
                        .values_list("pk", flat=True)
                    )
                    == expected
                )
            sizes[size] = (
                len(point_queries),
                sum(len(item["sql"].encode()) for item in point_queries),
                len(set_queries),
                len(set_queries[-1]["sql"].encode()),
            )
            assert bounded[-1] == tuple(ids)

        # N separate points cost N statements/proofs. The one candidate-set
        # proof adds only identity literals, not another policy graph per
        # candidate or the collection's accessible-set enumeration.
        assert sizes[5][0] == 5 * sizes[1][0]
        assert sizes[50][0] == 50 * sizes[1][0]
        assert abs(sizes[5][1] - 5 * sizes[1][1]) <= 100 * 5
        assert abs(sizes[50][1] - 50 * sizes[1][1]) <= 100 * 50
        assert sizes[1][2] == sizes[5][2] == sizes[50][2] == 1
        assert 0 <= sizes[5][3] - sizes[1][3] <= 32 * (5 - 1)
        assert 0 < sizes[50][3] - sizes[5][3] <= 32 * (50 - 5)

        # The same candidate statement still observes a revocation on its
        # next execution, including rows reached through the recursive parent.
        local.delete_relationships(
            RelationshipFilter(resource_type="blog/folder", resource_id=str(allowed_root.pk))
        )
        assert (
            not Post.objects.with_actor(ALICE)
            .filter(pk__in=[post.pk for post in posts[:5]])
            .exists()
        )


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_candidate_sql_bytes_do_not_follow_collection_or_group_count(storage):
    schema = """
    definition auth/user {}
    definition auth/group { relation member: auth/user }
    definition blog/folder {
        relation parent: blog/folder // rebac:field=parent
        relation viewer: auth/group#member
        permission read = viewer + parent->read
    }
    definition blog/post {
        relation folder: blog/folder // rebac:field=folder
        permission read = folder->read
    }
    """
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        local = install_schema(schema)
        with sudo(reason="test.fixture"):
            root = Folder.objects.create(name="root")
            child = Folder.objects.create(name="child", parent=root)
            posts = Post.objects.bulk_create(
                [Post(title=f"p{i}", folder=child) for i in range(100)]
            )
        ids = [post.pk for post in posts[:50]]
        sizes: dict[str, list[tuple[int, int]]] = {
            "point": [],
            "set1": [],
            "set5": [],
            "set50": [],
        }
        for stage in (0, 1, 2):
            local.write_relationships(
                [
                    item
                    for group_id in range(stage * 20, (stage + 1) * 20)
                    for item in (
                        RelationshipTuple(ObjectRef("auth/group", f"g{group_id}"), "member", ALICE),
                        RelationshipTuple(
                            to_object_ref(root),
                            "viewer",
                            SubjectRef.of("auth/group", f"g{group_id}", "member"),
                        ),
                    )
                ]
            )
            if stage:
                with sudo(reason="test.fixture"):
                    Post.objects.bulk_create(
                        [Post(title=f"extra {stage} {index}", folder=child) for index in range(200)]
                    )
            for shape, selected in (
                ("point", ids[:1]),
                ("set1", ids[:1]),
                ("set5", ids[:5]),
                ("set50", ids),
            ):
                scope = Post.objects.with_actor(ALICE)
                scope = (
                    scope.filter(pk=selected[0])
                    if shape == "point"
                    else scope.filter(pk__in=selected)
                )
                with CaptureQueriesContext(connection) as queries:
                    assert list(scope.order_by("pk").values_list("pk", flat=True)) == selected
                sizes[shape].append((len(queries), len(queries[-1]["sql"].encode())))
        for measurements in sizes.values():
            assert [statements for statements, _bytes in measurements] == [1, 1, 1]
            assert measurements[0][1] == measurements[1][1] == measurements[2][1]


@pytest.mark.parametrize("size", [read._CANDIDATE_LIMIT, read._CANDIDATE_LIMIT + 1, 17_000])
def test_candidate_limit_falls_back_before_large_lists_double_bind(records, paths, size):
    _local, allowed, _denied, _grant = records
    ids = [allowed.pk, *range(allowed.pk + 2, allowed.pk + size + 1)]
    assert list(Post.objects.with_actor(ALICE).filter(pk__in=ids).values_list("pk", flat=True)) == [
        allowed.pk
    ]
    assert paths["candidates"][-1] == (tuple(ids) if size <= read._CANDIDATE_LIMIT else None)


def test_distinct_candidate_lists_do_not_churn_the_shared_statement_cache(records):
    _local, _allowed, _denied, _grant = records
    read.reset()
    before = len(read._statements)
    for index in range(200):
        list(
            Post.objects.with_actor(ALICE)
            .filter(pk__in=[index + 1, index + 1001])
            .values_list("pk", flat=True)
        )
    assert len(read._statements) == before


def test_candidate_ids_bind_as_one_postgres_array_or_bounded_in(records):
    _local, allowed, denied, _grant = records
    ids = [allowed.pk, denied.pk]
    statements = []

    def capture(execute, sql, params, many, context):
        statements.append((sql, params))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(capture):
        assert list(
            Post.objects.with_actor(ALICE).filter(pk__in=ids).values_list("pk", flat=True)
        ) == [allowed.pk]
    sql, params = statements[-1]
    if connection.vendor == "postgresql":
        assert "= ANY(%s)" in sql
        assert sum(isinstance(param, list) and param == ids for param in params) == 1
    else:
        assert "= ANY(%s)" not in sql
        assert all(not isinstance(param, list) for param in params)


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_bounded_union_operands_take_candidate_proof_before_shared_plan(storage, monkeypatch):
    schema = """
    definition auth/user {}
    definition auth/group { relation member: auth/user | auth/group#member }
    definition blog/folder {
        relation viewer: auth/group#member
        permission read = viewer
    }
    definition blog/post {
        relation folder: blog/folder // rebac:field=folder
        permission read = folder->read
    }
    """
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        local = install_schema(schema)
        with sudo(reason="test.fixture"):
            folder = Folder.objects.create(name="container")
            posts = Post.objects.bulk_create(
                [Post(title=f"post {index}", folder=folder) for index in range(20)]
            )
        group = ObjectRef("auth/group", "readers")
        local.write_relationships(
            [
                RelationshipTuple(group, "member", ALICE),
                RelationshipTuple(
                    to_object_ref(folder),
                    "viewer",
                    SubjectRef.of("auth/group", "readers", "member"),
                ),
            ]
        )
        rows = Post.objects.with_actor(ALICE)
        first = [post.pk for post in posts[:10]]
        second = [post.pk for post in posts[10:]]
        candidates = []
        candidate_sql = []
        original = read._scope_statement

        def observed(operation, model, key, *args, **kwargs):
            compiled = original(operation, model, key, *args, **kwargs)
            if model is Post and kwargs.get("candidates") is not None:
                candidates.append(kwargs["candidates"])
                if not isinstance(compiled, bool):
                    candidate_sql.append(compiled.sql)
            return compiled

        monkeypatch.setattr(read, "_scope_statement", observed)
        union = share_scoped_definitions(
            rows.filter(pk__in=first)
            .values_list("pk", flat=True)
            .union(rows.filter(pk__in=second).values_list("pk", flat=True))
        )
        executed = []

        def capture(execute, sql, params, many, context):
            executed.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            assert sorted(union) == [post.pk for post in posts]
        assert tuple(first) in candidates and tuple(second) in candidates
        assert len(candidate_sql) == 2
        assert all(part in executed[-1] for part in candidate_sql)
