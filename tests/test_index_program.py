"""Program structure, actor-class semantics, and opt-in index diagnostics.

These tests inspect plans and metadata without deriving an index. In particular,
mocked schema row readers exercise composition without schema-write maintenance.
"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError, router
from django.test import override_settings

from rebac import checks as system_checks
from rebac.actors import anonymous_actor, is_anonymous_actor
from rebac.backends.local import LocalBackend
from rebac.errors import SchemaError
from rebac.index import program as compiler
from rebac.index.classes import class_of, matches
from rebac.index.program import codec_fields, program_errors, program_for, watched_for
from rebac.index.terms import AUTHENTICATED, anonymous, type_level, wildcard
from rebac.schema.ast import (
    AllowedSubject,
    AttributeBinding,
    Caveat,
    ConstBinding,
    Definition,
    FieldBinding,
    Relation,
    Schema,
)
from rebac.schema.parser import parse_permission_expression, parse_zed
from rebac.schema.walker import builtin_actor_matches, subject_allowed_by_relation
from rebac.types import SubjectRef

from .testapp.models import (
    AuthoredPost,
    BackingEntry,
    BackingProject,
    BackingQueue,
    BackingRound,
    BackingStage,
    BackingTask,
    Folder,
    NativeParentLinkedChild,
    NativeParentLinkedResource,
    Post,
)

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def _program(text: str):
    active = LocalBackend()
    active.set_schema(parse_zed(text))
    return program_for(active, using="default", now=NOW)


def _backing_schema(type_, target, backing, *, name="member"):
    return Schema(
        definitions=[
            Definition(type_, (Relation(name, (AllowedSubject(target),), backing=backing),), ())
        ]
    )


def _assert_watch(watched, model, fields, resource_type):
    watch = watched[model._meta.label_lower]
    assert fields <= watch.fields
    assert resource_type in watch.resource_types
    assert watch.model_label == model._meta.label_lower
    assert watch.model is model
    return watch


def test_all_relations_are_nodes_even_without_permissions():
    program = _program("definition auth/user {} definition doc { relation reader: auth/user }")
    assert set(program.nodes) == {("doc", "reader")}
    node = program.nodes["doc", "reader"]
    assert node.kind == "relation"
    assert node.expr is None and node.operands is None
    assert not node.recursive
    assert program.strata == ((("doc", "reader"),),)


def _held(program, type_, node):
    """The one site a node refers to, with its operand nodes."""
    expr = program.nodes[type_, node].expr
    site = program.nodes[type_, expr.name]
    return site, [program.nodes.get((type_, name)) for name in site.operands]


def test_internal_nodes_are_named_by_content_and_operands_are_nodes():
    program = _program(
        """
        definition auth/user {}
        definition doc {
            relation a: auth/user
            relation b: auth/user
            relation c: auth/user
            relation parent: doc
            permission read = ((a + b) & (parent->read - c))
        }
    """.replace("parent->read", "parent->a")
    )
    assert program.nodes["doc", "read"].kind == "mono"
    outer, (left, right) = _held(program, "doc", "read")
    assert outer.kind == "and"
    assert left.kind == right.kind == "mono"
    inner, (arrow, banned) = _held(program, "doc", right.name)
    assert inner.kind == "minus"
    assert inner.operands == (arrow.name, "c") and banned.kind == "relation"
    assert arrow.deps == frozenset({("doc", "parent"), ("doc", "a")})
    internal = {name for _, name in program.nodes if "." in name}
    assert internal == {outer.name, left.name, right.name, inner.name, arrow.name}
    assert all(re.fullmatch(r"read\.[0-9a-f]{12}", name) for name in internal)


def test_setop_beneath_union_is_materialized():
    program = _program("""
        definition auth/user {}
        definition doc {
            relation a: auth/user
            relation b: auth/user
            permission read = a + (a - b)
        }
    """)
    assert program.nodes["doc", "read"].kind == "mono"
    (site,) = program.held_sites(("doc", "read"))
    assert program.nodes[site].kind == "minus"
    assert site in program.nodes["doc", "read"].deps


def test_read_plan_counts_held_sites_through_monotone_references():
    program = _program("""
        definition auth/user {}
        definition doc {
            relation a: auth/user
            relation b: auth/user
            relation c: auth/user
            permission inner = a - b
            permission read = inner + (a & c)
        }
    """)
    sites = program.held_sites(("doc", "read"))
    assert len(sites) == 2
    assert all(program.nodes[key].kind in {"and", "minus"} for key in sites)
    assert program.lookups(("doc", "read")) == 5


@pytest.mark.parametrize(
    "expression,sites",
    [
        ("a - nil", 0),
        ("a & nil", 0),
        ("nil & a", 0),
        ("nil - a", 0),
        ("(a - nil) & b", 1),
    ],
)
def test_nil_lowering_precedes_read_plan(expression, sites):
    program = _program(f"""
        definition auth/user {{}}
        definition doc {{ relation a: auth/user relation b: auth/user
            permission read = {expression} }}
    """)
    assert len(program.held_sites(("doc", "read"))) == sites
    assert program.lookups(("doc", "read")) == 1 + 2 * sites


def test_e019_prints_oversized_plan_at_check_time():
    schema = parse_zed("""
        definition auth/user {}
        definition doc {
            relation a: auth/user
            relation b: auth/user
            permission read = (a - b) + (b - a)
        }
    """)
    with override_settings(REBAC_INDEX_LOOKUP_LIMIT=4):
        errors = program_errors(schema)
    assert len(errors) == 1
    assert errors[0].id == "rebac.E019"
    assert "doc#read" in errors[0].msg
    assert "5" in errors[0].msg


@pytest.mark.parametrize("value", [None, True, False, 0, -1, "64", 1.5])
def test_e019_rejects_invalid_lookup_limit(value):
    with override_settings(REBAC_INDEX_LOOKUP_LIMIT=value):
        assert any(e.id == "rebac.E019" for e in system_checks.check_backend_setting())


def test_builtin_and_nil_operands_get_internal_nodes():
    program = _program("definition doc { permission read = authenticated - nil }")
    assert set(program.nodes) == {("doc", "read")}
    assert program.nodes["doc", "read"].deps == frozenset()


@pytest.mark.parametrize("length", [52, 64])
def test_internal_node_column_overflow_is_reported_before_derivation(length):
    name = "p" * length
    source = f"""
        definition auth/user {{}}
        definition doc {{
            relation a: auth/user
            relation b: auth/user
            permission {name} = (a + b) & a
        }}
    """
    errors = program_errors(parse_zed(source))
    assert len(errors) == 2
    assert all(error.id == "rebac.E016" for error in errors)
    assert f"doc#{name}." in errors[0].msg
    assert "64-character" in errors[0].msg
    with pytest.raises(SchemaError, match=r"rebac\.E016.*64-character"):
        _program(source)


def test_internal_node_at_column_limit_keeps_deterministic_names():
    name = "p" * 51
    program = _program(f"""
        definition auth/user {{}}
        definition doc {{ relation a: auth/user relation b: auth/user
            permission {name} = (a + b) & a
        }}
    """)
    site, (left, right) = _held(program, "doc", name)
    assert site.operands == (left.name, "a") and right.kind == "relation"
    assert max(len(node.name) for node in program.nodes.values()) == 64
    assert (
        _program(f"""
        definition auth/user {{}}
        definition doc {{ relation b: auth/user relation a: auth/user
            permission {name} = (a + b) & a
        }}
    """).nodes.keys()
        == program.nodes.keys()
    )


def test_arrow_dependencies_include_all_allowed_types_and_subject_sets():
    program = _program("""
        definition auth/user {}
        definition folder { relation member: auth/user permission read = member }
        definition group { relation member: auth/user permission read = member }
        definition doc {
            relation parent: folder | group#member
            permission read = parent->read
        }
    """)
    assert program.nodes["doc", "read"].deps == frozenset(
        {("doc", "parent"), ("folder", "read"), ("group", "read")}
    )
    assert program.nodes["doc", "parent"].deps == frozenset({("group", "member")})
    assert program.userset_relations == frozenset({("group", "member")})
    assert ("doc", "read") in program.dependents([("group", "member")])


def test_strata_dependencies_precede_dependents_and_recursion_is_marked():
    program = _program("""
        definition auth/user {}
        definition folder {
            relation reader: auth/user
            relation parent: folder
            permission read = reader + parent->read
            permission view = read
        }
    """)
    assert program.nodes["folder", "read"].recursive
    assert not program.nodes["folder", "view"].recursive
    for key, node in program.nodes.items():
        assert key in program.strata[node.stratum]
        for dep in node.deps:
            if dep in program.nodes:
                assert program.nodes[dep].stratum <= node.stratum


def test_multi_type_scc_and_deterministic_definition_order():
    text = """
        definition a { relation parent: b permission read = parent->read }
        definition b { relation parent: a permission read = parent->read }
    """
    schema = parse_zed(text)
    nodes, strata = compiler._compile_nodes(schema)
    reverse_nodes, reverse_strata = compiler._compile_nodes(
        Schema(definitions=list(reversed(schema.definitions)))
    )
    assert nodes == reverse_nodes
    assert strata == reverse_strata
    assert (("a", "read"), ("b", "read")) in strata
    assert nodes["a", "read"].recursive and nodes["b", "read"].recursive


@pytest.mark.parametrize("expression", ["read & a", "read - a", "a - read", "parent->read & a"])
def test_e016_rejects_recursive_setops_including_singletons(expression):
    schema = parse_zed(f"""
        definition auth/user {{}}
        definition doc {{
            relation a: auth/user
            relation parent: doc
            permission read = {expression}
        }}
    """)
    errors = program_errors(schema)
    assert len(errors) == 1
    assert errors[0].id == "rebac.E016"
    assert "doc#read" in errors[0].msg
    assert " -> " in errors[0].msg
    assert "deliberate LocalBackend divergence" in errors[0].msg


def test_recursive_permission_can_depend_on_acyclic_set_operation():
    program = _program("""
        definition auth/user {}
        definition doc {
            relation a: auth/user
            relation b: auth/user
            relation parent: doc
            permission direct = a - b
            permission read = direct + parent->read
        }
    """)
    assert program.nodes["doc", "read"].recursive
    assert not program.nodes["doc", "direct"].recursive


def test_program_and_watch_mappings_are_immutable():
    program = _program("definition doc { permission read = authenticated }")
    with pytest.raises(TypeError):
        program.nodes["doc", "read"] = None
    with pytest.raises(TypeError):
        program.watched["anything"] = None
    with pytest.raises(FrozenInstanceError):
        program.nodes["doc", "read"].recursive = True


def test_no_overrides_use_baseline_schema():
    program = _program("definition doc { permission read = authenticated }")
    assert program.overrides == ()
    assert program.schema_at(NOW) == program.baseline


def _row(pk, kind, expression, deadline=None, *, target_pk=1):
    return SimpleNamespace(
        pk=pk,
        kind=kind,
        expression=expression,
        expires_at=deadline,
        created_at=NOW + timedelta(seconds=pk),
        target_pk=target_pk,
        target_ct_id=1,
    )


def _mock_composition_targets(monkeypatch):
    # Keep the real compose/operator logic; replace only its schema-row lookups.
    from rebac import composition

    def group(rows):
        permissions = [r for r in rows if r.kind != "recaveat" and r.target_pk == 1]
        caveats = [r for r in rows if r.kind == "recaveat" and r.target_pk == 1]
        return ({("permission", "doc", "read"): permissions}, {"gate": caveats})

    monkeypatch.setattr(composition, "_group_overrides", group)


def _database_program(monkeypatch, baseline, rows):
    from rebac.models import SchemaOverride

    active = LocalBackend()
    monkeypatch.setattr(active, "_read_schema_revision", Mock(return_value="a" * 32))
    loader = Mock(return_value=(baseline, None))
    monkeypatch.setattr(active, "_load_schema_from_db", loader)
    query = MagicMock()
    query.filter.return_value = query
    query.select_related.return_value = query
    query.order_by.return_value = query
    query.__iter__.side_effect = lambda: iter(rows)
    monkeypatch.setattr(SchemaOverride.objects, "using", Mock(return_value=query))
    _mock_composition_targets(monkeypatch)
    return active, loader


def test_tagged_overrides_compose_distinct_deadlines_and_permanent_arms(monkeypatch):
    baseline = parse_zed("""
        definition auth/user {}
        definition doc {
            relation a: auth/user relation b: auth/user relation c: auth/user
            permission read = a permission view = read
        }
    """)
    t1, t2 = NOW + timedelta(hours=1), NOW + timedelta(hours=2)
    rows = [_row(1, "extend", "b", t1), _row(2, "disable", "c", t2), _row(3, "extend", "c")]
    active, loader = _database_program(monkeypatch, baseline, rows)
    program = program_for(active, using="default", now=NOW)
    expected = ["((a + b) + c) - c", "(a + c) - c", "a + c"]
    for instant, expression in zip((NOW, t1, t2), expected, strict=True):
        assert program.schema_at(instant).get_permission(
            "doc", "read"
        ).expression == parse_permission_expression(expression)
    assert {node.deadline for node in program.nodes.values() if node.deadline} == {t1, t2}
    assert ("doc", "view") in program.dependents([("doc", "read")])
    assert program.nodes["doc", "read"].kind == "mono"
    loader.assert_called_once_with("default", overrides=())


def test_same_deadline_overrides_and_unknown_targets(monkeypatch):
    baseline = parse_zed("definition doc { permission read = anonymous }")
    deadline = NOW + timedelta(hours=1)
    rows = [
        _row(1, "extend", "authenticated", deadline),
        _row(2, "disable", "anonymous", deadline),
        _row(3, "extend", "authenticated", NOW + timedelta(hours=2), target_pk=99),
    ]
    active, _ = _database_program(monkeypatch, baseline, rows)
    program = program_for(active, using="default", now=NOW)
    assert program.schema_at(NOW).get_permission("doc", "read").expression == (
        parse_permission_expression("(anonymous + authenticated) - anonymous")
    )
    assert program.schema_at(deadline).get_permission("doc", "read").expression == (
        parse_permission_expression("anonymous")
    )


def test_permanent_override_composes_into_program(monkeypatch):
    baseline = parse_zed("definition doc { permission read = anonymous }")
    active, _ = _database_program(monkeypatch, baseline, [_row(1, "extend", "authenticated")])
    program = program_for(active, using="default", now=NOW)
    assert all(node.deadline is None for node in program.nodes.values())
    assert program.schema_at(NOW).get_permission(
        "doc", "read"
    ).expression == parse_permission_expression("anonymous + authenticated")


@override_settings(USE_TZ=False)
def test_tagged_overrides_support_naive_datetime_bounds(monkeypatch):
    baseline = parse_zed("definition doc { permission read = anonymous }")
    deadline = (NOW + timedelta(hours=1)).replace(tzinfo=None)
    active, _ = _database_program(
        monkeypatch, baseline, [_row(1, "extend", "authenticated", deadline)]
    )
    program = program_for(active, using="default", now=NOW.replace(tzinfo=None))
    assert deadline in {node.deadline for node in program.nodes.values()}
    assert program.schema_at(deadline).get_permission("doc", "read").expression == (
        parse_permission_expression("anonymous")
    )


def test_recaveat_equal_to_baseline_can_mask_an_earlier_override(monkeypatch):
    baseline = Schema(
        definitions=[
            Definition(
                "doc", (Relation("viewer", (AllowedSubject("auth/user", with_caveat="gate"),)),), ()
            )
        ],
        caveats=[Caveat("gate", (), "true")],
    )
    deadline = NOW + timedelta(hours=1)
    rows = [_row(1, "recaveat", "false"), _row(2, "recaveat", "true", deadline)]
    active, _ = _database_program(monkeypatch, baseline, rows)
    program = program_for(active, using="default", now=NOW)
    assert program.schema_at(NOW).get_caveat("gate").expression == "true"
    assert program.schema_at(deadline).get_caveat("gate").expression == "false"


def test_deadline_on_unused_caveat_does_not_add_nodes(monkeypatch):
    baseline = Schema(caveats=[Caveat("gate", (), "true")])
    active, _ = _database_program(
        monkeypatch, baseline, [_row(1, "recaveat", "false", NOW + timedelta(hours=1))]
    )
    program = program_for(active, using="default", now=NOW)
    assert not program.nodes
    assert program.schema_at(NOW).get_caveat("gate").expression == "false"
    assert program.schema_at(NOW + timedelta(hours=1)).get_caveat("gate").expression == "true"


def test_override_deadline_retains_history_until_source_deletion(monkeypatch):
    baseline = parse_zed("definition doc { permission read = anonymous }")
    deadline = NOW + timedelta(hours=1)
    rows = [_row(1, "extend", "authenticated", deadline)]
    active, loader = _database_program(monkeypatch, baseline, rows)
    before = program_for(active, using="default", now=NOW)
    assert program_for(active, using="default", now=NOW + timedelta(minutes=1)) is before
    assert program_for(active, using="default", now=deadline) is before
    assert before.schema_at(deadline) == baseline
    rows.clear()  # Deleting the source override discards its tagged arm.
    after = program_for(active, using="default", now=deadline)
    assert before.revision == after.revision
    assert before is not after
    assert after.overrides == ()
    assert after.schema_at(deadline) == baseline
    assert loader.call_count == 2


def test_alias_and_manual_schema_are_part_of_cache_identity(monkeypatch):
    active = LocalBackend()
    active.set_schema(parse_zed("definition doc { permission read = anonymous }"))
    monkeypatch.setattr(
        compiler,
        "connections",
        {
            "one": SimpleNamespace(in_atomic_block=False),
            "two": SimpleNamespace(in_atomic_block=False),
        },
    )
    one = program_for(active, using="one", now=NOW)
    two = program_for(active, using="two", now=NOW)
    assert one is not two
    active.set_schema(parse_zed("definition doc { permission read = authenticated }"))
    changed = program_for(active, using="one", now=NOW)
    assert changed.revision != one.revision


def test_program_rejects_invalid_cycle_introduced_by_override(monkeypatch):
    baseline = parse_zed("definition doc { relation parent: doc permission read = parent->read }")
    active, _ = _database_program(monkeypatch, baseline, [_row(1, "tighten", "authenticated")])
    with pytest.raises(SchemaError, match=r"rebac\.E016"):
        program_for(active, using="default", now=NOW)


def test_multi_hop_and_filter_watches_include_intermediate_plain_models():
    schema = _backing_schema(
        "test/backingqueue",
        "auth/user",
        FieldBinding(
            "tasks__promoted__rounds__entries__responder",
            (
                ("tasks__stage__hidden__isnull", True),
                ("tasks__promoted__rounds__entries__retired_at__isnull", True),
            ),
        ),
    )
    watched = watched_for(schema)
    type_ = "test/backingqueue"
    _assert_watch(watched, BackingQueue, {"id"}, type_)
    _assert_watch(watched, BackingTask, {"queue", "queue_id", "stage", "stage_id"}, type_)
    assert not _assert_watch(watched, BackingStage, {"hidden"}, type_).is_mixin
    assert _assert_watch(watched, BackingProject, {"task_id"}, type_).is_mixin
    _assert_watch(watched, BackingRound, {"project_id"}, type_)
    assert _assert_watch(
        watched, BackingEntry, {"round_id", "responder_id", "retired_at"}, type_
    ).is_mixin
    _assert_watch(watched, get_user_model(), {"id"}, type_)


@pytest.mark.parametrize("reverse", [False, True])
def test_m2m_watches_include_both_through_columns(reverse):
    source, target, path = (
        ("blog/folder", "blog/post", "collected_posts")
        if reverse
        else ("blog/post", "blog/folder", "collections")
    )
    watched = watched_for(_backing_schema(source, target, FieldBinding(path)))
    through = Post._meta.get_field("collections").remote_field.through
    _assert_watch(watched, through, {"post_id", "folder_id"}, source)
    _assert_watch(watched, Post, {"id"}, source)
    _assert_watch(watched, Folder, {"id"}, source)


def test_mti_watches_parent_identity_and_parent_link():
    schema = _backing_schema(
        "test/nativeparentlinkedrecord", "test/nativeparentlinkedchild", FieldBinding("child")
    )
    watched = watched_for(schema)
    type_ = "test/nativeparentlinkedrecord"
    _assert_watch(watched, NativeParentLinkedChild, {"nativeparentlinkedresource_ptr_id"}, type_)
    _assert_watch(watched, NativeParentLinkedResource, {"id"}, type_)


def test_attribute_watches_identity_attribute_and_filter_columns():
    schema = _backing_schema(
        "kind", "auth/user", AttributeBinding("username", filters=(("is_active", True),))
    )
    watched = watched_for(schema)
    assert not _assert_watch(
        watched, get_user_model(), {"id", "username", "is_active"}, "kind"
    ).is_mixin
    fields = {(m._meta.label_lower, attr) for m, attr in codec_fields(schema)}
    assert (get_user_model()._meta.label_lower, "username") in fields


def test_fixed_boolean_attribute_is_watched_but_requires_no_attribute_codec():
    schema = _backing_schema(
        "flags", "auth/user", AttributeBinding("is_staff", resource="staff", value=True)
    )
    watched = watched_for(schema)
    _assert_watch(watched, get_user_model(), {"is_staff"}, "flags")
    assert all(attr != "is_staff" for _model, attr in codec_fields(schema))


def test_const_watches_local_filters_and_source_identity():
    schema = _backing_schema(
        "blog/authoredpost",
        "role",
        ConstBinding("public", (("confirmed", True), ("role__exact", "x"))),
    )
    watched = watched_for(schema)
    assert _assert_watch(
        watched, AuthoredPost, {"id", "confirmed", "role"}, "blog/authoredpost"
    ).is_mixin


def test_userset_inventory_keeps_backed_relations_referenced_by_type():
    schema = _backing_schema("kind", "auth/user", AttributeBinding("username"))
    schema.definitions.append(
        Definition("doc", (Relation("viewer", (AllowedSubject("kind", relation="member"),)),), ())
    )
    active = LocalBackend()
    active.set_schema(schema)
    program = program_for(active, using="default", now=NOW)
    assert ("kind", "member") in program.userset_relations
    assert ("kind", "member") in program.nodes["doc", "viewer"].deps


@pytest.mark.parametrize("id_,relation", list(product(["", "*", "alice"], ["", "member"])))
@pytest.mark.parametrize("type_", ["auth/user", "auth/group", "auth/anonymous"])
def test_builtin_classes_match_walker(type_, id_, relation):
    actor = SubjectRef.of(type_, id_, relation)
    for triple, name in ((AUTHENTICATED, "authenticated"), (anonymous(), "anonymous")):
        cls = class_of(triple)
        assert cls is not None
        assert matches(cls, actor) == builtin_actor_matches(name, actor)
    assert matches(class_of(anonymous()), actor) == is_anonymous_actor(actor)


@override_settings(REBAC_ANONYMOUS_TYPE="visitors/guest", REBAC_TYPE_PREFIX="tenant/")
def test_anonymous_class_honors_configured_type_and_prefix():
    cls = class_of(anonymous())
    assert matches(cls, anonymous_actor())
    assert not matches(cls, SubjectRef.of("tenant/visitors/guest", "other"))
    assert matches(class_of(AUTHENTICATED), SubjectRef.of("tenant/visitors/guest", "*", "member"))


def test_wildcards_match_only_concrete_actors_of_their_type():
    relation = Relation("viewer", (AllowedSubject("auth/user", wildcard=True),))
    assert subject_allowed_by_relation(relation, SubjectRef.of("auth/user", "*"))
    assert not subject_allowed_by_relation(relation, SubjectRef.of("auth/user", "*", "member"))
    cls = class_of(wildcard("auth/user"))
    for type_, id_, suffix in product(
        ["auth/user", "auth/group"], ["", "alice", "*"], ["", "member"]
    ):
        actor = SubjectRef.of(type_, id_, suffix)
        assert matches(cls, actor) == (type_ == "auth/user" and not suffix)
    assert class_of(type_level("auth/user")) is None
    assert class_of(("auth/user", "alice", "")) is None
    assert class_of(("auth/group", "team", "member")) is None


@pytest.mark.parametrize("value", [None, True, False, 0, -1, "256", 1.5])
def test_condition_limit_rejects_invalid_type_or_range(value):
    with override_settings(REBAC_INDEX_CONDITION_LIMIT=value):
        assert any(
            e.id == "rebac.E017" and "CONDITION_LIMIT" in e.msg
            for e in system_checks.check_backend_setting()
        )


@pytest.mark.parametrize("value", [1, 256, 4096])
def test_condition_limit_accepts_positive_integers(value):
    with override_settings(REBAC_INDEX_CONDITION_LIMIT=value):
        assert not any("CONDITION_LIMIT" in e.msg for e in system_checks.check_backend_setting())


def test_index_checks_do_not_query_without_explicit_databases(monkeypatch):
    monkeypatch.setattr(
        system_checks,
        "_index_schema_for_checks",
        Mock(side_effect=AssertionError("unexpected schema query")),
    )
    assert system_checks.check_index_schema() == []
    assert system_checks.check_index_ready() == []
    assert system_checks.check_index_schema(databases=[]) == []


@override_settings(REBAC_BACKEND="spicedb")
def test_index_checks_are_local_backend_only():
    assert system_checks.check_index_schema(databases=["default"]) == []
    assert system_checks.check_index_ready(databases=["default"]) == []


@pytest.mark.parametrize(
    "row,expected",
    [
        (None, True),
        (("a", None, ""), True),
        (("a", "b", "p"), True),
        (("a", "a", "p"), False),
    ],
)
def test_e013_ready_and_stale_revisions(monkeypatch, row, expected):
    from rebac.models.generation import SchemaGeneration

    query = Mock()
    query.filter.return_value.values_list.return_value.first.return_value = row
    using = Mock(return_value=query)
    monkeypatch.setattr(SchemaGeneration.objects, "using", using)
    issues = system_checks.check_index_ready(databases=["default"])
    assert bool(issues) == expected
    assert all(issue.id == "rebac.E013" for issue in issues)
    using.assert_called_once_with("default")


def test_e013_defers_missing_migration_table(monkeypatch):
    from rebac.models.generation import SchemaGeneration

    monkeypatch.setattr(
        SchemaGeneration.objects, "using", Mock(side_effect=DatabaseError("not migrated"))
    )
    assert system_checks.check_index_ready(databases=["default"]) == []


def _schema_checks(monkeypatch, schema, *, models=()):
    from django.apps import apps

    monkeypatch.setattr(system_checks, "_index_schema_for_checks", lambda using: schema)
    monkeypatch.setattr(apps, "get_models", lambda **kwargs: models)
    return system_checks.check_index_schema(databases=["default"])


def test_e014_unsupported_scoped_identity_on_and_off(monkeypatch):
    from .testapp.models import PropertyIdentityFolder

    issues = _schema_checks(monkeypatch, Schema(), models=(PropertyIdentityFolder,))
    assert any(e.id == "rebac.E014" and "public_identity" in e.msg for e in issues)
    issues = _schema_checks(monkeypatch, Schema(), models=(Post,))
    assert not any(e.id == "rebac.E014" for e in issues)


def test_e014_dynamic_boolean_attribute_but_not_fixed_anchor(monkeypatch):
    dynamic = _backing_schema("flags", "auth/user", AttributeBinding("is_staff"))
    issues = _schema_checks(monkeypatch, dynamic)
    assert any(e.id == "rebac.E014" and "is_staff" in e.msg for e in issues)
    fixed = _backing_schema(
        "flags", "auth/user", AttributeBinding("is_staff", resource="staff", value=True)
    )
    assert not any(e.id == "rebac.E014" for e in _schema_checks(monkeypatch, fixed))


def test_e015_router_alias_mismatch_on_and_off(monkeypatch):
    original = router.db_for_write
    monkeypatch.setattr(
        router,
        "db_for_write",
        lambda model, **kwargs: "elsewhere" if model is Post else original(model, **kwargs),
    )
    issues = _schema_checks(monkeypatch, Schema(), models=(Post,))
    assert any(e.id == "rebac.E015" and "elsewhere" in e.msg for e in issues)
    monkeypatch.setattr(router, "db_for_write", original)
    assert not any(
        e.id == "rebac.E015" for e in _schema_checks(monkeypatch, Schema(), models=(Post,))
    )


def test_e016_system_check_delegates_on_and_off(monkeypatch):
    bad = parse_zed("definition doc { permission read = read & authenticated }")
    assert any(e.id == "rebac.E016" for e in _schema_checks(monkeypatch, bad))
    good = parse_zed("definition doc { permission read = authenticated }")
    assert not any(e.id == "rebac.E016" for e in _schema_checks(monkeypatch, good))


def test_e018_rejects_untracked_path_and_accepts_tracking(monkeypatch, settings):
    from tests.testapp.models import BackingQueue, BackingStage, BackingTask

    schema = parse_zed("""
        definition auth/user {}
        definition test/backingqueue { relation viewer: auth/user }
        definition test/backingtask {
            relation queue: test/backingqueue // rebac:field={"path":"queue","filters":{"stage__hidden":false}}
        }
    """)
    settings.REBAC_TRACKED_MODELS = []
    models = (BackingQueue, BackingStage, BackingTask)
    errors = [e for e in _schema_checks(monkeypatch, schema, models=models) if e.id == "rebac.E018"]
    assert len(errors) == 1
    assert BackingStage._meta.label_lower in errors[0].msg
    assert "transaction.atomic()" in errors[0].hint and "ATOMIC_REQUESTS" in errors[0].hint
    settings.REBAC_TRACKED_MODELS = ["testapp.BackingStage"]
    assert not any(e.id == "rebac.E018" for e in _schema_checks(monkeypatch, schema, models=models))
    assert not any(e.id == "rebac.E018" for e in _schema_checks(monkeypatch, Schema()))


@pytest.mark.parametrize("explicit", [False, True])
def test_user_backing_is_tracked_implicitly_and_explicitly(monkeypatch, settings, explicit):
    settings.REBAC_TRACKED_MODELS = [get_user_model()._meta.label] if explicit else []
    schema = _backing_schema("kind", "auth/user", AttributeBinding("username"))
    assert not any(e.id == "rebac.E018" for e in _schema_checks(monkeypatch, schema))


@pytest.mark.parametrize("value", ["auth.User", [17], ["missing.NoModel"], ["malformed"]])
def test_e018_invalid_tracking_configuration(settings, value):
    settings.REBAC_TRACKED_MODELS = value
    assert [e.id for e in system_checks.check_tracked_models_setting()] == ["rebac.E018"]


def test_schema_checks_defer_unavailable_schema(monkeypatch):
    monkeypatch.setattr(system_checks, "_index_schema_for_checks", lambda using: None)
    assert system_checks.check_index_schema(databases=["default"]) == []


def test_e014_reports_unsupported_target_identity_even_if_backing_resolution_fails(monkeypatch):
    from .testapp.models import PropertyIdentityFolder

    schema = _backing_schema("blog/post", "test/propertyfolder", FieldBinding("folder"))
    issues = _schema_checks(monkeypatch, schema, models=(Post, PropertyIdentityFolder))
    assert any(e.id == "rebac.E014" and "public_identity" in e.msg for e in issues)


def test_watched_map_merges_fields_and_resource_types():
    one = _backing_schema("kind", "auth/user", AttributeBinding("username"))
    two = _backing_schema("email", "auth/user", AttributeBinding("email"))
    watched = watched_for(Schema(definitions=one.definitions + two.definitions))
    watch = watched[get_user_model()._meta.label_lower]
    assert watch.resource_types == frozenset({"kind", "email"})
    assert {"id", "username", "email"} <= watch.fields


@pytest.mark.parametrize(
    "backing,target,type_",
    [
        (FieldBinding("folder"), "blog/folder", "blog/post"),
        (ConstBinding("public"), "role", "blog/post"),
    ],
)
def test_userset_inventory_includes_field_and_const_backed_sets(backing, target, type_):
    schema = _backing_schema(type_, target, backing)
    schema.definitions.append(
        Definition("doc", (Relation("viewer", (AllowedSubject(type_, relation="member"),)),), ())
    )
    active = LocalBackend()
    active.set_schema(schema)
    assert (type_, "member") in program_for(active, using="default", now=NOW).userset_relations


def test_publishing_program_never_reconnects_signals(monkeypatch):
    from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete, pre_save

    connections = []
    for signal in (m2m_changed, pre_save, post_save, pre_delete, post_delete):
        connect = Mock(side_effect=AssertionError("program publication reconnected a signal"))
        monkeypatch.setattr(signal, "connect", connect)
        connections.append(connect)
    _program("definition doc { permission read = authenticated }")
    for connect in connections:
        connect.assert_not_called()


def test_program_reuses_compilation_without_sharing_transaction_wrappers(monkeypatch):
    active = LocalBackend()
    active.set_schema(parse_zed("definition doc { permission read = authenticated }"))
    monkeypatch.setattr(compiler, "connections", {"default": SimpleNamespace(in_atomic_block=True)})
    compile_nodes = Mock(wraps=compiler._compile_nodes)
    monkeypatch.setattr(compiler, "_compile_nodes", compile_nodes)
    first = program_for(active, using="default", now=NOW)
    compiled = compile_nodes.call_count
    second = program_for(active, using="default", now=NOW)
    assert first is not second
    assert first.nodes == second.nodes
    assert first.nodes is second.nodes
    assert compiled > 0
    assert compile_nodes.call_count == compiled


def test_atomic_program_cache_keys_actual_payload_when_revision_is_reused(monkeypatch):
    original = parse_zed("definition doc { permission read = authenticated }")
    replacement = parse_zed("definition doc { permission read = anonymous }")
    active, loader = _database_program(monkeypatch, original, [])
    monkeypatch.setattr(compiler, "connections", {"default": SimpleNamespace(in_atomic_block=True)})
    first = program_for(active, using="default", now=NOW)
    # Simulate rollback followed by another transaction-local write using the
    # same revision. Compiled content from the aborted write must not leak.
    loader.return_value = replacement, None
    second = program_for(active, using="default", now=NOW)
    assert first.revision == second.revision
    assert first.nodes != second.nodes
    assert second.baseline == replacement
    loader.return_value = original, None
    restored = program_for(active, using="default", now=NOW)
    assert restored.nodes is first.nodes
    assert restored.baseline == original
    assert loader.call_count == 3


def test_schema_check_defers_unreadable_format_only_with_pending_migrations(monkeypatch):
    from rebac import backends

    active = LocalBackend()
    monkeypatch.setattr(backends, "backend", lambda: active)
    monkeypatch.setattr(active, "_load_schema_from_db", Mock(side_effect=SchemaError("old format")))
    executor = Mock()
    executor.migration_plan.return_value = [object()]
    monkeypatch.setattr(
        "django.db.migrations.executor.MigrationExecutor", Mock(return_value=executor)
    )
    assert system_checks._index_schema_for_checks("default") is None
    executor.migration_plan.return_value = []
    with pytest.raises(SchemaError, match="old format"):
        system_checks._index_schema_for_checks("default")


def test_schema_check_defers_missing_schema_tables(monkeypatch):
    from rebac import backends

    active = LocalBackend()
    monkeypatch.setattr(backends, "backend", lambda: active)
    monkeypatch.setattr(
        active, "_load_schema_from_db", Mock(side_effect=DatabaseError("not migrated"))
    )
    assert system_checks._index_schema_for_checks("default") is None


def test_iterative_schema_components_handle_long_chains_and_finished_branches():
    from rebac.composition import _cycles_in_definition
    from rebac.schema.graph import strongly_connected_components

    edges = {number: {number + 1} for number in range(2500)}
    edges[2500] = set()
    assert strongly_connected_components(edges) == tuple((n,) for n in range(2500, -1, -1))
    edges[2500] = {0}
    assert strongly_connected_components(edges) == (tuple(range(2501)),)
    definition = parse_zed("""
        definition doc {
            permission a = b + c
            permission b = a
            permission c = b
        }
    """).definitions[0]
    assert _cycles_in_definition(definition) == {"a", "b", "c"}


def test_read_plan_and_reverse_adjacency_reuse_compilation(monkeypatch):
    active, _ = _database_program(
        monkeypatch,
        parse_zed("definition doc { permission read = authenticated }"),
        [_row(1, "tighten", "anonymous", deadline=NOW + timedelta(hours=1))],
    )
    program = program_for(active, using="default")
    first = program.nodes
    reverse = program.reverse
    monkeypatch.setattr(compiler, "_compile_nodes", Mock(side_effect=AssertionError("recompiled")))
    assert program_for(active, using="default").nodes is first
    assert program.reverse is reverse
    assert program.dependents([("doc", "read")]) == frozenset({("doc", "read")})


def test_manual_digest_is_computed_only_by_set_schema(monkeypatch):
    from rebac.index.read import _ManualRevision
    from rebac.schema import serialization

    digest = Mock(wraps=serialization.digest)
    monkeypatch.setattr(serialization, "digest", digest)
    active = LocalBackend()
    schema = parse_zed("definition doc { permission read = authenticated }")
    active.set_schema(schema)
    expected = active._manual_schema_revision()
    expression = _ManualRevision(active)
    compiler_stub = SimpleNamespace(compile=lambda value: ("%s", [value.value]))
    for _ in range(3):
        assert expression.as_sql(compiler_stub, None) == ("%s", [expected])
        assert active._schema_snapshot().revision == expected
    digest.assert_called_once_with(repr(schema))
    active.set_schema(parse_zed("definition doc { permission read = anonymous }"))
    assert expression.as_sql(compiler_stub, None)[1] == [active._manual_schema_revision()]
    assert active._manual_schema_revision() != expected


def test_canonical_json_preserves_microseconds_and_key_order():
    from rebac.schema.serialization import canonical_json, digest

    value = NOW.replace(microsecond=123456)
    assert canonical_json({"z": value, "a": "é"}) == (
        '{"a":"é","z":"2026-09-29T00:00:00.123456+00:00"}'
    )
    assert digest({"z": value, "a": "é"}) == digest({"a": "é", "z": value})
    assert digest(value) != digest(value.replace(microsecond=123457))
    with pytest.raises(ValueError):
        canonical_json(float("nan"))


def test_index_uses_public_orm_without_database_sql():
    root = Path(__file__).resolve().parents[1] / "src" / "rebac"
    banned = re.compile(
        r"cursor\.execute\s*\(|\.raw\s*\(|\bRawSQL\s*\(|\.extra\s*\("
        r"|\b_raw_delete\s*\(|\bnames_to_path\s*\(|\bTupleIn\s*\("
        r"|\bCREATE\s+(?:TRIGGER|FUNCTION)\b|\.query\.[A-Za-z_]+\s*="
    )
    for path in root.rglob("*.py"):
        if path.name == "0005_schema_generation.py":
            continue
        assert not banned.search(path.read_text()), path


def test_program_generation_memo_is_invalidated_by_settings():
    active = LocalBackend()
    active.set_schema(parse_zed("definition doc { permission read = authenticated }"))
    before = program_for(active, using="default")
    with override_settings(USE_TZ=False):
        during = program_for(active, using="default")
        assert during is not before
        assert during.schema_at(NOW.replace(tzinfo=None)) == during.baseline
    after = program_for(active, using="default")
    assert after is not during
    assert after.schema_at(NOW) == after.baseline


def test_sync_hash_uses_definition_wire_identity():
    from rebac.management.commands.rebac import Command
    from rebac.models import SchemaDefinition
    from rebac.schema.serialization import digest

    definition = SchemaDefinition(pk=101, resource_type="test/doc")
    payload = {"definition": definition, "name": "read", "expression": "viewer"}
    expected = digest({"definition": "test/doc", "name": "read", "expression": "viewer"})
    assert Command._hash_payload(payload) == expected
    definition.pk = 202
    assert Command._hash_payload(payload) == expected


@pytest.mark.parametrize("command", [["sync"], ["index", "rebuild"], ["index", "verify"]])
def test_database_alias_is_validated_by_command_parser(command):
    from django.core.management.base import CommandError

    from rebac.management.commands.rebac import Command

    parser = Command().create_parser("manage.py", "rebac")
    with pytest.raises(CommandError, match="invalid choice"):
        parser.parse_args([*command, "--database", "missing-database-alias"])


@pytest.mark.parametrize("left,right", list(product((True, False, None), repeat=2)))
@pytest.mark.parametrize("operator", ["and", "or"])
def test_condition_algebra_retains_three_state_results_and_what_they_need(
    monkeypatch, left, right, operator
):
    from rebac.index import conditions
    from rebac.schema.walker import tri_and, tri_or

    schema = parse_zed("caveat l(x int) { x > 0 } caveat r(y int) { y > 0 }")
    verdicts = {"l": left, "r": right}

    def evaluate(caveat, pinned, context):
        return verdicts[caveat.name], {caveat.name} if verdicts[caveat.name] is None else set()

    monkeypatch.setattr(conditions, "evaluate_caveat", evaluate)
    a, b = conditions.leaf("l", {}), conditions.leaf("r", {})
    expected = {"and": tri_and, "or": tri_or}[operator](left, right)
    result, missing = conditions.evaluate({operator: [a, b]}, schema, {})
    assert result is expected
    # A conditional result needs its unknown operands; a decided one, nothing.
    # Subtraction is a site of the program, never a stored formula.
    needed = {name for name in verdicts if verdicts[name] is None} if result is None else set()
    assert missing == frozenset(needed)
    assert conditions.evaluate({operator: [b, a]}, schema, {}) == (result, missing)


SITES = """
    definition auth/user {}
    definition doc {
        relation r1: auth/user relation r2: auth/user relation r3: auth/user
        permission read = r1 & r2
    }
"""


def test_a_site_keeps_its_name_when_an_override_changes_the_expression(monkeypatch):
    baseline = parse_zed(SITES)
    active, _ = _database_program(monkeypatch, baseline, [])
    before = program_for(active, using="default", now=NOW)
    (site,) = before.held_sites(("doc", "read"))
    active, _ = _database_program(monkeypatch, baseline, [_row(1, "disable", "r3")])
    after = program_for(active, using="default", now=NOW)
    (outer,) = after.held_sites(("doc", "read"))
    # The intersection keeps its name and its meaning; the subtraction the
    # override adds has a name of its own.
    assert outer != site
    assert (after.nodes[outer].kind, after.nodes[outer].override) == ("minus", "disable:1")
    assert (after.nodes[site].kind, after.nodes[site].operands) == ("and", ("r1", "r2"))
    assert after.nodes[site].operands == before.nodes[site].operands
    assert site in after.held_sites(("doc", after.nodes[outer].operands[0]))
    assert before.digest != after.digest


def test_equal_overrides_are_distinct_sites(monkeypatch):
    rows = [_row(1, "disable", "r3"), _row(2, "disable", "r3")]
    active, _ = _database_program(monkeypatch, parse_zed(SITES), rows)
    program = program_for(active, using="default", now=NOW)
    sites = [node for node in program.nodes.values() if node.kind == "minus"]
    assert sorted(node.override for node in sites) == ["disable:1", "disable:2"]
    assert len({node.name for node in sites}) == 2


def test_program_digest_follows_meaning_not_declaration_order():
    swapped = SITES.replace(
        "relation r1: auth/user relation r2: auth/user",
        "relation r2: auth/user relation r1: auth/user",
    )
    assert _program(SITES).digest == _program(swapped).digest
    assert _program(SITES).digest != _program(SITES.replace("r1 & r2", "r1 - r2")).digest
    assert _program(SITES).digest != _program(SITES.replace("r1 & r2", "r2 & r1")).digest


def test_subject_sets_are_not_derivation_dependencies():
    schema = parse_zed("""
        definition auth/user {}
        definition doc {
            relation viewer: auth/user | doc#read
            relation banned: auth/user
            permission read = viewer - banned
        }
    """)
    # A grant holds a subject set by reference and nothing follows it, so the
    # site is on no derivation cycle. (Schema validation refuses a subject set
    # that names a permission; the graph rule does not depend on that.)
    assert program_errors(schema) == []
    nodes, _ = compiler._compile_nodes(schema)
    assert nodes["doc", "viewer"].deps == frozenset({("doc", "read")})
