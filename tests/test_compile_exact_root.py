"""Resolved mandatory identity predicates reuse point SQL without a separate check."""

import pytest
from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef, Prefetch, Q, Subquery, Value
from django.db.models.expressions import ColPairs
from django.db.models.fields.tuple_lookups import TupleIn
from django.db.models.lookups import Exact, In

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.compile import read
from rebac.errors import SchemaError
from rebac.schema import parse_zed
from rebac.testing import install_schema
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
    calls = {"point": [], "scope": []}
    point, scope = read._point_statement, read._scope_statement

    def point_statement(operation, key, *args, **kwargs):
        calls["point"].append(key)
        return point(operation, key, *args, **kwargs)

    def scope_statement(operation, model, key):
        calls["scope"].append(key)
        return scope(operation, model, key)

    monkeypatch.setattr(read, "_point_statement", point_statement)
    monkeypatch.setattr(read, "_scope_statement", scope_statement)
    return calls


def _tuple_in(model, rhs):
    field = model._meta.pk
    return TupleIn(ColPairs(model._meta.db_table, [field], [field], field), rhs)


@pytest.mark.parametrize(
    "shape", ["pk", "id", "exact", "in", "in_tuple", "tuple_in", "tuple_in_lists", "and", "chain"]
)
def test_native_identity_selectors_use_point_sql_and_preserve_denial(records, paths, shape):
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
            "and": Q(pk=row.pk) & (Q(title="allowed") | Q(title="denied")),
            "chain": Q(title=row.title),
        }
        selected = rows.filter(selectors[shape])
        if shape == "chain":
            selected = selected.filter(pk=row.pk)
        assert list(selected.values_list("pk", flat=True)) == (
            [allowed.pk] if row is allowed else []
        )
    assert ("blog/post", "read") in paths["point"]
    assert ("blog/post", "read") not in paths["scope"]


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
    assert ("blog/post", "read") in paths["point"]
    assert ("blog/post", "read") not in paths["scope"]


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
def test_unproved_predicates_keep_the_general_scope(records, paths, shape):
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
    assert ("blog/post", "read") in paths["point"]


@pytest.mark.parametrize("selector", ["exact", "tuple_in"])
def test_point_scope_keeps_the_original_query_predicates(records, selector):
    _local, allowed, denied, _grant = records
    predicate = Q(pk=allowed.pk) if selector == "exact" else Q(_tuple_in(Post, [(allowed.pk,)]))
    assert not Post.objects.with_actor(ALICE).filter(predicate, title=denied.title).exists()


@pytest.mark.parametrize("selector", ["exact", "tuple_in"])
def test_revocation_after_point_compilation_is_witnessed_by_selecting_sql(
    records, monkeypatch, selector
):
    local, allowed, _denied, grant = records
    compile_point = read._point_statement
    changed = []

    def revoke(operation, key, *args, **kwargs):
        compiled = compile_point(operation, key, *args, **kwargs)
        if key == ("blog/post", "read") and not changed:
            changed.append(True)
            local.delete_relationship(grant)
        return compiled

    monkeypatch.setattr(read, "_point_statement", revoke)
    predicate = Q(pk=allowed.pk) if selector == "exact" else Q(_tuple_in(Post, [(allowed.pk,)]))
    assert not Post.objects.with_actor(ALICE).filter(predicate).exists()
    assert changed


def test_policy_replacement_during_point_compile_cannot_open_a_closed_scope(records, monkeypatch):
    local, allowed, _denied, _grant = records
    local.set_schema(
        parse_zed("definition auth/user {} definition blog/post { permission read = nil }")
    )
    rows = Post.objects.with_actor(ALICE).filter(pk=allowed.pk)
    compile_point = read._point_statement
    changed = []

    def replace(operation, key, *args, **kwargs):
        if key == ("blog/post", "read") and not changed:
            changed.append(True)
            local.set_schema(
                parse_zed(
                    "definition auth/user {} definition blog/post { permission read = authenticated }"
                )
            )
        return compile_point(operation, key, *args, **kwargs)

    monkeypatch.setattr(read, "_point_statement", replace)
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
def test_stored_set_changes_after_point_compilation_are_witnessed(records, monkeypatch, change):
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
    compile_point = read._point_statement
    changed = []

    def mutate(operation, key, *args, **kwargs):
        statement = compile_point(operation, key, *args, **kwargs)
        if key == ("blog/post", "read") and not changed:
            changed.append(True)
            if change == "revoke_viewer":
                local.delete_relationship(member)
            else:
                local.write_relationships([RelationshipTuple(banned, "member", ALICE)])
        return statement

    monkeypatch.setattr(read, "_point_statement", mutate)
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


def test_native_forward_fk_prefetch_uses_singleton_point_and_preserves_denial(
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
    assert ("blog/folder", "read") in paths["point"]
    assert ("blog/folder", "read") not in paths["scope"]


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
def test_unproved_tuple_identity_shapes_keep_general_proof(records, shape):
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
    assert read._exact_identity(Post, field.get_col(alias), query.where, "default") is None
