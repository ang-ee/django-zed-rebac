"""Resolved mandatory identities use candidate-root SQL without a separate check."""

import re
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, models
from django.db.models import Exists, OuterRef, Prefetch, Q, Subquery, Value
from django.db.models.expressions import ColPairs
from django.db.models.fields.tuple_lookups import TupleIn
from django.db.models.lookups import Exact, In
from django.test import override_settings
from django.test.utils import CaptureQueriesContext, isolate_apps
from django.utils import timezone

from rebac import (
    CheckItem,
    ObjectRef,
    RebacMixin,
    RelationshipTuple,
    SubjectRef,
    evaluator_scope,
    sudo,
    to_object_ref,
)
from rebac.compile import read
from rebac.compile.predicate import Compiler
from rebac.errors import SchemaError
from rebac.managers import share_scoped_definitions
from rebac.schema import parse_zed
from rebac.testing import install_schema
from rebac.types import PermissionResult, RelationshipFilter
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


@pytest.fixture(params=["inline", "decided"])
def plan(request, monkeypatch):
    if request.param == "inline":
        monkeypatch.setattr(read, "_scope_decide", lambda *_args, **_kwargs: False)
    with evaluator_scope() if request.param == "decided" else nullcontext():
        yield request.param


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
def test_non_point_predicates_use_a_scope_statement(records, paths, shape, plan):
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


def test_nonunique_native_integer_identity_keeps_general_scope(records, paths, monkeypatch, plan):
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
def test_candidate_limit_falls_back_before_large_lists_double_bind(records, paths, size, plan):
    _local, allowed, _denied, _grant = records
    ids = [allowed.pk, *range(allowed.pk + 2, allowed.pk + size + 1)]
    assert list(Post.objects.with_actor(ALICE).filter(pk__in=ids).values_list("pk", flat=True)) == [
        allowed.pk
    ]
    assert paths["candidates"][-1] == (tuple(ids) if size <= read._CANDIDATE_LIMIT else None)


def test_distinct_candidate_lists_reuse_one_statement_shape(records):
    _local, _allowed, _denied, _grant = records
    read.reset()
    for index in range(200):
        list(
            Post.objects.with_actor(ALICE)
            .filter(pk__in=[index + 1, index + 1001])
            .values_list("pk", flat=True)
        )
    stats = read.statement_cache_stats()
    assert stats["entries"] == 1
    assert stats["misses"] == 1
    assert stats["hits"] == 199


def test_candidate_statement_reuses_actor_shape_and_clock(records, monkeypatch):
    _local, allowed, denied, _grant = records
    now = timezone.now()
    monkeypatch.setattr("rebac.clock.application_now", lambda: now)
    read.reset()
    assert list(Post.objects.with_actor(ALICE).filter(pk__in=[allowed.pk, denied.pk])) == [allowed]
    first = read.statement_cache_stats()
    monkeypatch.setattr("rebac.clock.application_now", lambda: now + timedelta(days=1))
    assert list(Post.objects.with_actor(BOB).filter(pk__in=[denied.pk, allowed.pk])) == []
    second = read.statement_cache_stats()
    assert second["misses"] == first["misses"]
    assert second["hits"] > first["hits"]


def test_bulk_300_ids_compiles_one_point_shape(records):
    local, allowed, _denied, _grant = records
    read.reset()
    ids = range(allowed.pk - 150, allowed.pk + 150)
    items = [CheckItem(ALICE, "read", ObjectRef("blog/post", str(index))) for index in ids]
    answers = read.check_many(backend=local, items=items, using="default")
    assert {index for index, answer in zip(ids, answers, strict=True) if answer.allowed} == {
        allowed.pk
    }
    stats = read.statement_cache_stats()
    assert stats["misses"] == 1
    assert stats["hits"] >= 299


def test_statement_cache_byte_budget_refuses_oversize_entry(records):
    _local, allowed, _denied, _grant = records
    with override_settings(REBAC_STATEMENT_CACHE_BYTES=100):
        read.reset()
        for _ in range(2):
            assert list(
                Post.objects.with_actor(ALICE).filter(pk=allowed.pk).values_list("pk", flat=True)
            ) == [allowed.pk]
        stats = read.statement_cache_stats()
        assert stats["entries"] == stats["bytes"] == stats["hits"] == 0
        assert stats["misses"] == 2


def test_statement_cache_evicts_by_bytes(records):
    _local, allowed, denied, _grant = records
    read.reset()
    list(Post.objects.with_actor(ALICE).filter(pk=allowed.pk))
    first_size = read.statement_cache_stats()["bytes"]
    with override_settings(REBAC_STATEMENT_CACHE_BYTES=first_size + 100):
        read.reset()
        list(Post.objects.with_actor(ALICE).filter(pk=allowed.pk))
        list(Post.objects.with_actor(ALICE).filter(pk__gte=allowed.pk, pk__lte=denied.pk))
        stats = read.statement_cache_stats()
        assert stats["entries"] == 1
        assert stats["bytes"] <= first_size + 100


def test_alternating_revisions_keep_recent_statements(records, monkeypatch):
    local, allowed, _denied, _grant = records
    read.reset()
    list(Post.objects.with_actor(ALICE).filter(pk=allowed.pk))
    assert read.statement_cache_stats()["entries"] == 1
    snapshot = local._schema_snapshot(using="default")
    monkeypatch.setattr(
        local,
        "_schema_snapshot",
        lambda *args, **kwargs: snapshot._replace(revision="next-revision"),
    )
    read._policy(local, "default")
    stats = read.statement_cache_stats()
    assert stats["entries"] == 1
    assert stats["evictions"] == 0
    monkeypatch.setattr(local, "_schema_snapshot", lambda *args, **kwargs: snapshot)
    read._policy(local, "default")
    assert read.statement_cache_stats()["entries"] == 1
    assert read.statement_cache_stats()["evictions"] == 0
    assert list(Post.objects.with_actor(ALICE).filter(pk=allowed.pk)) == [allowed]
    assert read.statement_cache_stats()["hits"] == 1


def test_decided_row_cache_rebinds_actor_and_grant_changes():
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
    with sudo(reason="decided row cache fixture"):
        first_folder = Folder.objects.create(name="first")
        second_folder = Folder.objects.create(name="second")
        first = Post.objects.create(title="first", folder=first_folder)
        second = Post.objects.create(title="second", folder=second_folder)
    first_grant = RelationshipTuple(to_object_ref(first_folder), "viewer", ALICE)
    second_grant = RelationshipTuple(to_object_ref(second_folder), "viewer", BOB)
    local.write_relationships([first_grant, second_grant])

    def visible(actor):
        with evaluator_scope():
            return list(Post.objects.with_actor(actor).values_list("pk", flat=True))

    read.reset()
    assert visible(ALICE) == [first.pk]
    cold = read.statement_cache_stats()
    assert visible(BOB) == [second.pk]
    warm = read.statement_cache_stats()
    assert warm["hits"] == cold["hits"] + 1
    assert warm["misses"] == cold["misses"]
    local.delete_relationship(first_grant)
    assert visible(ALICE) == []
    empty = read.statement_cache_stats()
    assert empty["misses"] == warm["misses"] + 1
    moved = RelationshipTuple(to_object_ref(second_folder), "viewer", ALICE)
    local.write_relationships([moved])
    assert visible(ALICE) == [second.pk]
    assert read.statement_cache_stats()["hits"] == empty["hits"] + 1


def test_decided_row_cache_reuses_nonempty_array_shape():
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
    with sudo(reason="decided array cache fixture"):
        folders = [Folder.objects.create(name=f"folder-{index}") for index in range(3)]
        posts = [
            Post.objects.create(title=f"post-{index}", folder=folder)
            for index, folder in enumerate(folders)
        ]
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(folders[0]), "viewer", ALICE),
            RelationshipTuple(to_object_ref(folders[1]), "viewer", BOB),
            RelationshipTuple(to_object_ref(folders[2]), "viewer", BOB),
        ]
    )

    def visible(actor):
        with evaluator_scope():
            return set(Post.objects.with_actor(actor).values_list("pk", flat=True))

    read.reset()
    assert visible(ALICE) == {posts[0].pk}
    before = read.statement_cache_stats()
    assert visible(BOB) == {posts[1].pk, posts[2].pk}
    after = read.statement_cache_stats()
    if connection.vendor == "postgresql":
        assert after["hits"] == before["hits"] + 1
        assert after["misses"] == before["misses"]
    else:
        assert after["misses"] == before["misses"] + 1


@pytest.mark.slow
def test_decided_row_cache_buckets_forty_actor_outcomes():
    """The review's 1..40 folder grants share seven padded SQL shapes."""
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
    with sudo(reason="bucketed decided cache fixture"):
        folders = [Folder.objects.create(name=f"folder-{index}") for index in range(40)]
        posts = [
            Post.objects.create(title=f"post-{index}", folder=folder)
            for index, folder in enumerate(folders[:4])
        ]
    actors = [SubjectRef.of("auth/user", f"bucket-{index}") for index in range(40)]
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(folder), "viewer", actor)
            for index, actor in enumerate(actors)
            for folder in folders[: index + 1]
        ]
    )
    read.reset()
    for index, actor in enumerate(actors):
        with evaluator_scope():
            visible = set(Post.objects.with_actor(actor).values_list("pk", flat=True))
        assert visible == {post.pk for post in posts[: index + 1]}
    stats = read.statement_cache_stats()
    print(
        "DECIDED_BUCKET_STATS",
        connection.vendor,
        stats,
        "max_key_bytes",
        max(len(repr(key).encode()) for key in read._statements),
    )
    if connection.vendor == "postgresql":
        assert (stats["entries"], stats["hits"], stats["misses"]) == (1, 39, 1)
    else:
        assert (stats["entries"], stats["hits"], stats["misses"]) == (7, 33, 7)
    assert stats["bytes"] == sum(
        read._statement_size(key, value) for key, value in read._statements.items()
    )
    assert all(
        read._statement_size(key, value) >= len(repr(key).encode())
        for key, value in read._statements.items()
    )


def test_decided_warm_hit_skips_render_probes_and_compilation(monkeypatch):
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
    with sudo(reason="warm decided cache fixture"):
        folders = [Folder.objects.create(name=f"folder-{index}") for index in range(2)]
        posts = [
            Post.objects.create(title=f"post-{index}", folder=folder)
            for index, folder in enumerate(folders)
        ]
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(folders[0]), "viewer", ALICE),
            RelationshipTuple(to_object_ref(folders[1]), "viewer", BOB),
        ]
    )
    calls = {"probes": 0, "compiles": 0, "holds": 0}
    original_probe = Compiler.decide_native_render
    original_holds = Compiler.holds
    original_statement = read._Operation.statement

    def probe(self, render):
        calls["probes"] += 1
        return original_probe(self, render)

    def holds(self, *args, **kwargs):
        calls["holds"] += 1
        return original_holds(self, *args, **kwargs)

    def statement(self, key, verdicts, build, **kwargs):
        def counted():
            calls["compiles"] += 1
            return build()

        return original_statement(self, key, verdicts, counted, **kwargs)

    monkeypatch.setattr(Compiler, "decide_native_render", probe)
    monkeypatch.setattr(Compiler, "holds", holds)
    monkeypatch.setattr(read._Operation, "statement", statement)

    def visible(actor):
        with evaluator_scope():
            return list(Post.objects.with_actor(actor).values_list("pk", flat=True))

    read.reset()
    assert visible(ALICE) == [posts[0].pk]
    assert calls["probes"] > 0 and calls["compiles"] == 1
    calls.update(probes=0, compiles=0, holds=0)
    before = read.statement_cache_stats()
    assert visible(BOB) == [posts[1].pk]
    after = read.statement_cache_stats()
    assert after["hits"] == before["hits"] + 1
    assert calls["holds"] > 0
    assert calls["probes"] == calls["compiles"] == 0


def test_point_row_compiler_binds_decisions_before_evaluator_list(monkeypatch):
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
    with sudo(reason="point decided binding fixture"):
        folder = Folder.objects.create(pk=84_000_001, name="allowed")
        post = Post.objects.create(pk=84_000_002, title="allowed", folder=folder)
    local.write_relationships([RelationshipTuple(to_object_ref(folder), "viewer", ALICE)])
    original = read._Operation.compiler
    point_compilers = []

    def compiler(self, verdicts, **kwargs):
        if kwargs.get("rows") is not None and kwargs.get("parametric", True):
            point_compilers.append(kwargs.get("bind_decisions", False))
        return original(self, verdicts, **kwargs)

    monkeypatch.setattr(read._Operation, "compiler", compiler)
    read.reset()
    with evaluator_scope():
        assert local.check_access(
            subject=ALICE, action="read", resource=to_object_ref(post)
        ).allowed
        assert list(Post.objects.with_actor(ALICE).values_list("pk", flat=True)) == [post.pk]
    assert point_compilers and all(point_compilers)
    for kept in read._statements.values():
        if isinstance(kept, read._Kept):
            assert str(folder.pk) not in kept.sql
            assert str(folder.pk) not in map(str, kept.params)


@pytest.mark.parametrize("point_first", [False, True])
def test_decided_warm_hit_rechecks_revoked_grant(monkeypatch, point_first):
    """A grant revoked after preparation cannot survive the kept witness."""
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
    with sudo(reason="warm revocation fixture"):
        folders = [Folder.objects.create(name=f"folder-{index}") for index in range(2)]
        posts = [
            Post.objects.create(title=f"post-{index}", folder=folder)
            for index, folder in enumerate(folders)
        ]
    second_grant = RelationshipTuple(to_object_ref(folders[1]), "viewer", BOB)
    local.write_relationships(
        [RelationshipTuple(to_object_ref(folders[0]), "viewer", ALICE), second_grant]
    )
    read.reset()
    with evaluator_scope():
        if point_first:
            assert local.check_access(
                subject=ALICE, action="read", resource=to_object_ref(posts[0])
            ).allowed
        assert list(Post.objects.with_actor(ALICE).values_list("pk", flat=True)) == [posts[0].pk]
    before = read.statement_cache_stats()
    original = read._Rows._decide
    changed = []

    def decide_then_revoke(self, key):
        found = original(self, key)
        if key == ("blog/folder", "read") and self.operation.actor == BOB and not changed:
            changed.append(True)
            local.delete_relationship(second_grant)
        return found

    monkeypatch.setattr(read._Rows, "_decide", decide_then_revoke)
    with evaluator_scope():
        assert not Post.objects.with_actor(BOB).exists()
    after = read.statement_cache_stats()
    assert changed
    assert after["hits"] == before["hits"] + 1
    assert after["misses"] == before["misses"]


def test_decided_set_cache_rebinds_support_and_member_ids():
    local = install_schema("""
        definition auth/user {}
        definition auth/group {
            relation member: auth/user
        }
        definition blog/post {
            relation viewer: auth/group#member
            permission read = viewer
        }
    """)
    with sudo(reason="decided set cache fixture"):
        first = Post.objects.create(title="first")
        second = Post.objects.create(title="second")
    first_group = ObjectRef("auth/group", "first")
    second_group = ObjectRef("auth/group", "second")
    local.write_relationships(
        [
            RelationshipTuple(first_group, "member", ALICE),
            RelationshipTuple(second_group, "member", BOB),
            RelationshipTuple(to_object_ref(first), "viewer", SubjectRef(first_group, "member")),
            RelationshipTuple(to_object_ref(second), "viewer", SubjectRef(second_group, "member")),
        ]
    )

    def visible(actor):
        with evaluator_scope():
            return list(Post.objects.with_actor(actor).values_list("pk", flat=True))

    read.reset()
    assert visible(ALICE) == [first.pk]
    cold = read.statement_cache_stats()
    assert visible(BOB) == [second.pk]
    warm = read.statement_cache_stats()
    assert warm["hits"] == cold["hits"] + 1
    assert warm["misses"] == cold["misses"]
    local.delete_relationship(RelationshipTuple(first_group, "member", ALICE))
    assert visible(ALICE) == []
    empty = read.statement_cache_stats()
    assert empty["misses"] == warm["misses"] + 1
    local.write_relationships([RelationshipTuple(second_group, "member", ALICE)])
    assert visible(ALICE) == [second.pk]
    assert read.statement_cache_stats()["hits"] == empty["hits"] + 1


def test_decided_native_projection_choice_is_pinned_on_hit(records):
    _local, allowed, _denied, _grant = records
    read.reset()

    def visible():
        with evaluator_scope():
            return list(Post.objects.with_actor(ALICE).values_list("pk", flat=True))

    assert visible() == [allowed.pk]
    native = tuple(read._native_outcomes.values())
    assert native and any(native)
    first = read.statement_cache_stats()
    assert visible() == [allowed.pk]
    second = read.statement_cache_stats()
    assert second["hits"] == first["hits"] + 1
    assert second["misses"] == first["misses"]
    assert tuple(read._native_outcomes.values()) == native


@pytest.mark.parametrize(
    "shape",
    ["point", "bounded", "direct", "row", "set", "caveat", "evaluator_page"],
)
def test_cached_statement_has_no_first_actor_literals_and_matches_uncached(shape):
    local = install_schema("""
        caveat active(flag bool) { flag }
        definition auth/user {}
        definition auth/group { relation member: auth/user }
        definition blog/folder {
            relation viewer: auth/user
            permission read = viewer
        }
        definition blog/post {
            relation viewer: auth/user
            relation folder: blog/folder // rebac:field=folder
            relation group_viewer: auth/group#member
            relation caveated: auth/user with active
            permission direct = viewer
            permission read = folder->read
            permission shared = group_viewer
            permission caveated_read = caveated
        }
    """)
    actors = (SubjectRef.of("auth/user", "cache-a"), SubjectRef.of("auth/user", "cache-b"))
    folders = []
    posts = []
    with sudo(reason="cached statement invariant fixture"):
        for index in range(2):
            folder = Folder.objects.create(pk=82_000_000 + index * 10, name=f"folder-{index}")
            folders.append(folder)
            posts.append(
                [
                    Post.objects.create(
                        pk=83_000_000 + index * 10 + offset,
                        title=f"post-{index}-{offset}",
                        folder=folder,
                    )
                    for offset in range(2)
                ]
            )
    grants = []
    for index, actor in enumerate(actors):
        group = ObjectRef("auth/group", f"cache-group-{index}")
        grants.extend(
            [
                RelationshipTuple(to_object_ref(folders[index]), "viewer", actor),
                RelationshipTuple(group, "member", actor),
            ]
        )
        for post in posts[index]:
            grants.extend(
                [
                    RelationshipTuple(to_object_ref(post), "viewer", actor),
                    RelationshipTuple(
                        to_object_ref(post), "group_viewer", SubjectRef(group, "member")
                    ),
                    RelationshipTuple(
                        to_object_ref(post),
                        "caveated",
                        actor,
                        caveat_name="active",
                        caveat_context={"flag": True},
                    ),
                ]
            )
    local.write_relationships(grants)

    def visible(index):
        action = {
            "point": "direct",
            "bounded": "direct",
            "direct": "direct",
            "row": "read",
            "set": "shared",
            "caveat": "caveated_read",
            "evaluator_page": "read",
        }[shape]
        rows = Post.objects.with_actor(actors[index]).with_action(action)
        if shape == "point":
            rows = rows.filter(pk=posts[index][0].pk)
        elif shape == "bounded":
            rows = rows.filter(pk__in=[post.pk for post in posts[index]])
        if shape == "evaluator_page":
            with evaluator_scope():
                return set(rows.values_list("pk", flat=True)), rows.all().count()
        return set(rows.values_list("pk", flat=True))

    read.reset()
    first = visible(0)
    expected = {posts[0][0].pk} if shape == "point" else {post.pk for post in posts[0]}
    assert first == ((expected, 2) if shape == "evaluator_page" else expected)
    kept = [value for value in read._statements.values() if isinstance(value, read._Kept)]
    assert kept
    first_ids = {folders[0].pk, *(post.pk for post in posts[0])}
    first_strings = {actors[0].subject_id, "cache-group-0"}
    for statement in kept:
        assert actors[0].subject_id not in statement.sql
        assert all(
            re.search(rf"\b{identifier}\b", statement.sql) is None for identifier in first_ids
        )
        assert all(
            value not in first_ids and value not in first_strings
            for param in statement.params
            for value in (param if isinstance(param, list | tuple) else (param,))
            if isinstance(value, int | str)
        )
    before = read.statement_cache_stats()
    cached = visible(1)
    after = read.statement_cache_stats()
    assert after["hits"] > before["hits"]
    assert after["misses"] == before["misses"]
    original = read._Operation.statement

    def uncached(self, key, verdicts, build, **kwargs):
        return original(self, key, verdicts, build, **{**kwargs, "cache": False})

    with patch.object(read._Operation, "statement", uncached):
        assert cached == visible(1)
    second = {post.pk for post in posts[1]}
    if shape == "point":
        second = {posts[1][0].pk}
    assert cached == ((second, 2) if shape == "evaluator_page" else second)


def test_decided_scope_rekeys_on_depth_setting(records):
    local, allowed, _denied, _grant = records
    read.reset()
    with evaluator_scope():
        assert list(Post.objects.with_actor(ALICE).values_list("pk", flat=True)) == [allowed.pk]
    old_shape = read._Operation.begin(local, ALICE, "default").shape
    assert read.statement_cache_stats()["entries"] == 1
    with override_settings(REBAC_DEPTH_LIMIT=7):
        assert read._Operation.begin(local, ALICE, "default").shape != old_shape
        with evaluator_scope():
            assert list(Post.objects.with_actor(ALICE).values_list("pk", flat=True)) == [allowed.pk]
        assert read.statement_cache_stats()["entries"] == 2


@isolate_apps("tests.testapp")
def test_late_resource_model_preparation_rebuilds_model_index(records, monkeypatch):
    local, _allowed, _denied, _grant = records
    read.reset()
    captures = []
    capture = read._ModelIndex.capture

    def observed():
        index = capture()
        captures.append(index)
        return index

    monkeypatch.setattr(read._ModelIndex, "capture", observed)
    read._Operation.begin(local, ALICE, "default")
    assert len(captures) == 1
    with evaluator_scope():
        list(Post.objects.with_actor(ALICE).values_list("pk", flat=True))
    assert read.statement_cache_stats()["entries"] == 1

    class LateResource(RebacMixin):
        name = models.CharField(max_length=20)

        class Meta:
            app_label = "testapp"
            rebac_resource_type = "blog/late"

    assert LateResource._meta.rebac_resource_type == "blog/late"
    assert read.statement_cache_stats()["entries"] == 0
    from rebac import resources

    original_types = resources._model_types

    def with_late_model(prefix):
        yield from original_types(prefix)
        yield f"{prefix}blog/late", LateResource

    monkeypatch.setattr(resources, "_model_types", with_late_model)
    operation = read._Operation.begin(local, ALICE, "default")
    assert len(captures) == 2
    assert operation.model_index.resource("blog/late") is LateResource


def test_caveat_verdict_values_bind_into_reused_scope():
    local = install_schema("""
        caveat positive(value int) { value > 0 }
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user with positive
            permission read = viewer
        }
    """)
    with sudo(reason="caveat cache fixture"):
        first = Post.objects.create(title="first")
        second = Post.objects.create(title="second")
    edge = RelationshipTuple(
        to_object_ref(first),
        "viewer",
        ALICE,
        caveat_name="positive",
        caveat_context={"value": 1},
    )
    local.write_relationships([edge])
    read.reset()
    assert list(Post.objects.with_actor(ALICE).values_list("pk", flat=True)) == [first.pk]
    first_stats = read.statement_cache_stats()
    local.delete_relationship(edge)
    local.write_relationships(
        [
            RelationshipTuple(
                to_object_ref(second),
                "viewer",
                ALICE,
                caveat_name="positive",
                caveat_context={"value": 2},
            )
        ]
    )
    assert list(Post.objects.with_actor(ALICE).values_list("pk", flat=True)) == [second.pk]
    second_stats = read.statement_cache_stats()
    assert second_stats["hits"] == first_stats["hits"] + 1
    assert second_stats["misses"] == first_stats["misses"]


@pytest.mark.parametrize("false_bans", range(6))
def test_padded_false_verdicts_never_drop_unconditional_ban(false_bans):
    local = install_schema("""
        caveat positive(value int) { value > 0 }
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user
            relation banned: auth/user | auth/user with positive
            permission read = viewer - banned
        }
    """)
    with sudo(reason="padded exclusion fixture"):
        post = Post.objects.create(title="excluded")
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(post), "viewer", ALICE),
            RelationshipTuple(to_object_ref(post), "banned", ALICE),
            *[
                RelationshipTuple(
                    to_object_ref(post),
                    "banned",
                    SubjectRef.of("auth/user", f"other-{index}"),
                    caveat_name="positive",
                    caveat_context={"value": -index - 1},
                )
                for index in range(false_bans)
            ],
        ]
    )
    assert list(Post.objects.with_actor(ALICE).values_list("pk", flat=True)) == []
    result = local.check_access(subject=ALICE, action="read", resource=to_object_ref(post))
    assert not result.allowed and result.result is PermissionResult.NO_PERMISSION
    assert list(Post.objects.with_actor(ALICE).filter(pk=post.pk)) == []


def test_lookup_subjects_compiles_one_shape_for_many_actors(records):
    local, allowed, _denied, _grant = records
    others = [SubjectRef.of("auth/user", f"viewer-{index}") for index in range(20)]
    local.write_relationships(
        [RelationshipTuple(to_object_ref(allowed), "viewer", actor) for actor in others]
    )
    read.reset()
    found = read.lookup_subjects(
        backend=local,
        resource=to_object_ref(allowed),
        action="read",
        subject_type="auth/user",
        using="default",
    )
    assert set(found) == {ALICE, *others}
    stats = read.statement_cache_stats()
    assert stats["misses"] == 1
    assert stats["hits"] >= len(others)


def test_second_render_binds_without_beginning_new_operation(records, monkeypatch):
    _local, allowed, denied, _grant = records
    rows = Post.objects.with_actor(ALICE).filter(pk__in=[allowed.pk, denied.pk]).scoped()
    original = read._Operation.begin
    calls = []

    def begin(cls, *args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(read._Operation, "begin", classmethod(begin))
    rows.query.sql_with_params()
    first = len(calls)
    rows.query.sql_with_params()
    assert first > 0
    assert len(calls) == first


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
