"""What the library accepts as a policy, and what it refuses.

System checks for settings, schemas and models; the actor classes and timed
overrides a policy can name; the condition algebra; and the stored rows a
policy is compared with (tuples and registry identities).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError, models, router
from django.test import override_settings
from django.utils import timezone

from rebac import backend, clock
from rebac import checks as system_checks
from rebac.actors import anonymous_actor, is_anonymous_actor
from rebac.backends import reset_backend
from rebac.backends.local import LocalBackend
from rebac.compile import formulas
from rebac.compile.program import CompileProgram, program_errors
from rebac.composition import compose
from rebac.errors import SchemaError
from rebac.models.resource import RebacResource
from rebac.schema.ast import (
    AllowedSubject,
    AttributeBinding,
    Definition,
    FieldBinding,
    Relation,
    Schema,
)
from rebac.schema.parser import parse_zed
from rebac.schema.walker import builtin_actor_matches, subject_allowed_by_relation
from rebac.types import ObjectRef, RelationshipTuple, SubjectRef

from .backend_setup import install_schema
from .testapp.models import Post

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def _backing_schema(type_, target, backing, *, name="member"):
    return Schema(
        definitions=[
            Definition(type_, (Relation(name, (AllowedSubject(target),), backing=backing),), ())
        ]
    )


def _schema_checks(monkeypatch, schema, *, models=()):
    from django.apps import apps

    monkeypatch.setattr(system_checks, "_schema_on_alias", lambda using: schema)
    monkeypatch.setattr(apps, "get_models", lambda **kwargs: models)
    return system_checks.check_policy_models(databases=["default"])


def _has(actor, action, resource):
    return backend().has_access(subject=actor, action=action, resource=resource)


# ---------- Settings ----------


@pytest.mark.parametrize("value", ["auth.User", [17], ["missing.NoModel"], ["malformed"]])
def test_e018_invalid_tracking_configuration(settings, value):
    settings.REBAC_TRACKED_MODELS = value
    assert [e.id for e in system_checks.check_tracked_models_setting()] == ["rebac.E018"]


# ---------- When the schema checks run ----------


def test_schema_checks_do_not_query_without_explicit_databases(monkeypatch):
    monkeypatch.setattr(
        system_checks,
        "_schema_on_alias",
        Mock(side_effect=AssertionError("unexpected schema query")),
    )
    assert system_checks.check_policy_models() == []
    assert system_checks.check_policy_models(databases=[]) == []


@override_settings(REBAC_BACKEND="spicedb")
def test_schema_checks_are_local_backend_only():
    assert system_checks.check_policy_models(databases=["default"]) == []


def test_schema_checks_defer_unavailable_schema(monkeypatch):
    monkeypatch.setattr(system_checks, "_schema_on_alias", lambda using: None)
    assert system_checks.check_policy_models(databases=["default"]) == []


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
    assert system_checks._schema_on_alias("default") is None
    executor.migration_plan.return_value = []
    with pytest.raises(SchemaError, match="old format"):
        system_checks._schema_on_alias("default")


def test_schema_check_defers_missing_schema_tables(monkeypatch):
    from rebac import backends

    active = LocalBackend()
    monkeypatch.setattr(backends, "backend", lambda: active)
    monkeypatch.setattr(
        active, "_load_schema_from_db", Mock(side_effect=DatabaseError("not migrated"))
    )
    assert system_checks._schema_on_alias("default") is None


# ---------- E014: identities that cannot be compared with stored tuples ----------


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


def test_e014_reports_unsupported_target_identity_even_if_backing_resolution_fails(monkeypatch):
    from .testapp.models import PropertyIdentityFolder

    schema = _backing_schema("blog/post", "test/propertyfolder", FieldBinding("folder"))
    issues = _schema_checks(monkeypatch, schema, models=(Post, PropertyIdentityFolder))
    assert any(e.id == "rebac.E014" and "public_identity" in e.msg for e in issues)


# ---------- E015: one database alias for models and relationships ----------


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


# ---------- E016: recursion the compiler refuses ----------

RECURSIVE = """
    definition auth/user {{}}
    definition doc {{
        relation a: auth/user
        relation parent: doc
        permission read = {expression}
    }}
"""


@pytest.mark.parametrize(
    "expression,reason",
    [
        ("a - read", "exclusion dependency"),
        ("a - parent->read", "exclusion dependency"),
        ("parent->read + parent->read", "nonlinear"),
    ],
)
def test_e016_refuses_exclusion_and_nonlinear_use_of_a_recursive_permission(expression, reason):
    errors = program_errors(parse_zed(RECURSIVE.format(expression=expression)))
    assert len(errors) == 1
    assert errors[0].id == "rebac.E016"
    assert "('doc', 'read')" in errors[0].msg
    assert reason in errors[0].msg


@pytest.mark.parametrize("expression", ["read & a", "read - a", "parent->read & a"])
def test_e016_accepts_a_recursive_permission_used_once_outside_an_exclusion(expression):
    assert program_errors(parse_zed(RECURSIVE.format(expression=expression))) == []


@pytest.mark.django_db
@pytest.mark.parametrize("expression", ["read & a", "read - a", "parent->read & a"])
def test_recursion_without_a_base_case_grants_nobody(expression):
    install_schema(backend(), parse_zed(RECURSIVE.format(expression=expression)))
    alice = SubjectRef.of("auth/user", "alice")
    root, child = ObjectRef("doc", "root"), ObjectRef("doc", "child")
    backend().write_relationships(
        [
            RelationshipTuple(root, "a", alice),
            RelationshipTuple(child, "a", alice),
            RelationshipTuple(child, "parent", SubjectRef.of("doc", "root")),
        ]
    )
    for resource in (root, child):
        assert not _has(alice, "read", resource)
    assert list(backend().accessible(subject=alice, action="read", resource_type="doc")) == []


def test_e016_accepts_recursion_over_an_acyclic_set_operation():
    schema = parse_zed("""
        definition auth/user {}
        definition doc {
            relation a: auth/user
            relation b: auth/user
            relation parent: doc
            permission direct = a - b
            permission read = direct + parent->read
        }
    """)
    assert program_errors(schema) == []
    assert CompileProgram.build(schema).recursive == {("doc", "read")}


def test_e016_follows_subject_sets_into_a_recursive_component():
    schema = """
        definition auth/user {{}}
        definition doc {{
            relation viewer: auth/user | doc#read
            relation banned: auth/user
            permission read = {expression}
        }}
    """
    # The subject set puts ``viewer`` and ``read`` on one cycle. An exclusion of
    # a relation outside the cycle is accepted; excluding the cycle is not.
    assert program_errors(parse_zed(schema.format(expression="viewer - banned"))) == []
    errors = program_errors(parse_zed(schema.format(expression="banned - viewer")))
    assert [error.id for error in errors] == ["rebac.E016"]
    assert "exclusion dependency" in errors[0].msg


def test_e016_system_check_delegates_on_and_off(monkeypatch):
    bad = parse_zed("definition doc { permission read = authenticated - read }")
    assert any(e.id == "rebac.E016" for e in _schema_checks(monkeypatch, bad))
    good = parse_zed("definition doc { permission read = authenticated }")
    assert not any(e.id == "rebac.E016" for e in _schema_checks(monkeypatch, good))


def _row(pk, kind, expression, deadline=None):
    return SimpleNamespace(
        pk=pk,
        kind=kind,
        expression=expression,
        expires_at=deadline,
        created_at=NOW + timedelta(seconds=pk),
    )


@pytest.mark.parametrize(
    "kind,expression,refused",
    [
        ("tighten", "authenticated", False),
        ("disable", "parent->read", True),
        ("extend", "parent->read", True),
    ],
)
def test_e016_refuses_an_invalid_cycle_introduced_by_an_override(
    monkeypatch, kind, expression, refused
):
    from rebac import composition

    baseline = parse_zed("definition doc { relation parent: doc permission read = parent->read }")
    assert program_errors(baseline) == []
    # Keep the real composition; replace only its lookup of the target rows.
    monkeypatch.setattr(
        composition, "_group_overrides", lambda rows: ({("permission", "doc", "read"): rows}, {})
    )
    composed = compose(baseline, [_row(1, kind, expression)])
    issues = _schema_checks(monkeypatch, composed)
    assert any(e.id == "rebac.E016" for e in issues) is refused
    if refused:
        with pytest.raises(SchemaError, match="Recursive permission component"):
            CompileProgram.build(composed)
    else:
        CompileProgram.build(composed)


# ---------- E018: models on a backing path need a write owner ----------


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
    assert "RebacTrackedMixin" in errors[0].hint and "REBAC_TRACKED_MODELS" in errors[0].hint
    settings.REBAC_TRACKED_MODELS = ["testapp.BackingStage"]
    assert not any(e.id == "rebac.E018" for e in _schema_checks(monkeypatch, schema, models=models))
    assert not any(e.id == "rebac.E018" for e in _schema_checks(monkeypatch, Schema()))


@pytest.mark.parametrize("explicit", [False, True])
def test_user_backing_is_tracked_implicitly_and_explicitly(monkeypatch, settings, explicit):
    settings.REBAC_TRACKED_MODELS = [get_user_model()._meta.label] if explicit else []
    schema = _backing_schema("kind", "auth/user", AttributeBinding("username"))
    assert not any(e.id == "rebac.E018" for e in _schema_checks(monkeypatch, schema))


# ---------- Builtin actor classes and wildcards ----------

CLASSES = """
    definition auth/user {}
    definition auth/group { relation member: auth/user }
    definition doc {
        relation viewer: auth/user | auth/user:* | auth/group#member
        permission for_authenticated = authenticated
        permission for_anonymous = anonymous
        permission view = viewer
    }
"""


@pytest.mark.django_db
@pytest.mark.parametrize("id_,relation", list(product(["", "*", "alice"], ["", "member"])))
@pytest.mark.parametrize("type_", ["auth/user", "auth/group", "auth/anonymous"])
def test_builtin_classes_match_walker(type_, id_, relation):
    install_schema(backend(), parse_zed(CLASSES))
    actor = SubjectRef.of(type_, id_, relation)
    resource = ObjectRef("doc", "1")
    for name in ("authenticated", "anonymous"):
        assert _has(actor, f"for_{name}", resource) == builtin_actor_matches(name, actor)
    assert _has(actor, "for_anonymous", resource) == is_anonymous_actor(actor)


@pytest.mark.django_db
@override_settings(REBAC_ANONYMOUS_TYPE="visitors/guest", REBAC_TYPE_PREFIX="tenant/")
def test_anonymous_class_honors_configured_type_and_prefix():
    install_schema(backend(), parse_zed(CLASSES))
    resource = ObjectRef("doc", "1")
    assert anonymous_actor() == SubjectRef.of("tenant/visitors/guest", "*")
    assert _has(anonymous_actor(), "for_anonymous", resource)
    assert not _has(anonymous_actor(), "for_authenticated", resource)
    assert not _has(SubjectRef.of("tenant/visitors/guest", "other"), "for_anonymous", resource)
    assert not _has(SubjectRef.of("auth/anonymous", "*"), "for_anonymous", resource)
    assert _has(
        SubjectRef.of("tenant/visitors/guest", "*", "member"), "for_authenticated", resource
    )


@pytest.mark.django_db
def test_wildcards_match_only_concrete_actors_of_their_type():
    relation = Relation("viewer", (AllowedSubject("auth/user", wildcard=True),))
    assert subject_allowed_by_relation(relation, SubjectRef.of("auth/user", "*"))
    assert not subject_allowed_by_relation(relation, SubjectRef.of("auth/user", "*", "member"))
    install_schema(backend(), parse_zed(CLASSES))
    public, private, shared = (ObjectRef("doc", name) for name in ("public", "private", "shared"))
    backend().write_relationships(
        [
            RelationshipTuple(public, "viewer", SubjectRef.of("auth/user", "*")),
            RelationshipTuple(private, "viewer", SubjectRef.of("auth/user", "alice")),
            RelationshipTuple(shared, "viewer", SubjectRef.of("auth/group", "team", "member")),
        ]
    )
    for type_, id_, suffix in product(
        ["auth/user", "auth/group"], ["", "alice", "*"], ["", "member"]
    ):
        actor = SubjectRef.of(type_, id_, suffix)
        assert _has(actor, "view", public) == (type_ == "auth/user" and not suffix)
    # A concrete subject and a subject set name one holder each, not a class.
    assert _has(SubjectRef.of("auth/user", "alice"), "view", private)
    assert not _has(SubjectRef.of("auth/user", "bob"), "view", private)
    assert _has(SubjectRef.of("auth/group", "team", "member"), "view", shared)
    assert not _has(SubjectRef.of("auth/group", "other", "member"), "view", shared)
    assert not _has(SubjectRef.of("auth/group", "team"), "view", shared)


# ---------- Overrides with deadlines ----------


def _stored_schema(relations, permissions, caveats=()):
    """Store a ``test/doc`` policy through the write owners, so overrides apply."""
    from rebac.models import SchemaCaveat, SchemaDefinition, SchemaPermission, SchemaRelation
    from rebac.models.schema_write import schema_index_write

    reset_backend()
    with schema_index_write("default"):
        stored = {
            name: SchemaCaveat.objects.create(
                name=name, params=[{"name": "ok", "type": "bool"}], expression=expression
            )
            for name, expression in caveats
        }
        SchemaDefinition.objects.create(resource_type="auth/user")
        definition = SchemaDefinition.objects.create(resource_type="test/doc")
        for name, allowed in relations.items():
            SchemaRelation.objects.create(
                definition=definition, name=name, allowed_subjects=[allowed]
            )
        for name, expression in permissions.items():
            stored[name] = SchemaPermission.objects.create(
                definition=definition, name=name, expression=expression
            )
    return stored


def _override(target, kind, expression, *, expires_at=None, target_pk=None):
    from django.contrib.contenttypes.models import ContentType

    from rebac.models import SchemaOverride

    return SchemaOverride.objects.create(
        kind=kind,
        target_ct=ContentType.objects.get_for_model(type(target)),
        target_pk=target.pk if target_pk is None else target_pk,
        expression=expression,
        reason="test",
        expires_at=expires_at,
    )


def _user(name):
    return SubjectRef.of("auth/user", name)


def _readers(action, names, resource):
    """Who of ``names`` holds ``action``, by point check and by enumeration."""
    checked = {name for name in names if _has(_user(name), action, resource)}
    listed = {
        name
        for name in names
        if resource.resource_id
        in backend().accessible(
            subject=_user(name), action=action, resource_type=resource.resource_type
        )
    }
    assert checked == listed
    return checked


@pytest.mark.django_db
def test_timed_overrides_compose_distinct_deadlines_and_permanent_arms():
    user = {"type": "auth/user"}
    stored = _stored_schema({"a": user, "b": user, "c": user}, {"read": "a", "view": "read"})
    doc = ObjectRef("test/doc", "one")
    backend().write_relationships(
        [
            RelationshipTuple(doc, "a", _user("a")),
            RelationshipTuple(doc, "b", _user("b")),
            RelationshipTuple(doc, "c", _user("c")),
            RelationshipTuple(doc, "a", _user("ac")),
            RelationshipTuple(doc, "c", _user("ac")),
        ]
    )
    t1, t2 = timezone.now() + timedelta(hours=1), timezone.now() + timedelta(hours=2)
    _override(stored["read"], "extend", "b", expires_at=t1)
    _override(stored["read"], "disable", "c", expires_at=t2)
    _override(stored["read"], "extend", "c")
    names = ["a", "b", "c", "ac"]
    # ((a + b) + c) - c, then (a + c) - c, then a + c.
    for instant, expected in (
        (t1 - timedelta(seconds=1), {"a", "b"}),
        (t1, {"a"}),
        (t2 - timedelta(seconds=1), {"a"}),
        (t2, {"a", "c", "ac"}),
    ):
        with patch("django.utils.timezone.now", return_value=instant):
            assert _readers("read", names, doc) == expected
            assert _readers("view", names, doc) == expected


@pytest.mark.django_db
def test_overrides_with_one_deadline_and_an_unknown_target():
    stored = _stored_schema({}, {"read": "anonymous"})
    doc = ObjectRef("test/doc", "one")
    deadline = timezone.now() + timedelta(hours=1)
    _override(stored["read"], "extend", "authenticated", expires_at=deadline)
    _override(stored["read"], "disable", "anonymous", expires_at=deadline)
    _override(
        stored["read"],
        "extend",
        "authenticated",
        expires_at=deadline + timedelta(hours=1),
        target_pk=stored["read"].pk + 1000,
    )
    # (anonymous + authenticated) - anonymous until the deadline, anonymous after.
    for instant, user, anonymous in (
        (deadline - timedelta(seconds=1), True, False),
        (deadline, False, True),
    ):
        with patch("django.utils.timezone.now", return_value=instant):
            assert _has(_user("alice"), "read", doc) is user
            assert _has(anonymous_actor(), "read", doc) is anonymous


@pytest.mark.django_db
def test_timed_override_supports_naive_datetime_bounds(settings):
    settings.USE_TZ = False
    stored = _stored_schema({}, {"read": "anonymous"})
    doc = ObjectRef("test/doc", "one")
    deadline = timezone.now() + timedelta(hours=1)
    assert deadline.tzinfo is None
    _override(stored["read"], "extend", "authenticated", expires_at=deadline)
    for instant, allowed in ((deadline - timedelta(seconds=1), True), (deadline, False)):
        with patch("django.utils.timezone.now", return_value=instant):
            assert _has(_user("alice"), "read", doc) is allowed


@pytest.mark.django_db
def test_recaveat_equal_to_baseline_can_mask_an_earlier_override():
    stored = _stored_schema(
        {"viewer": {"type": "auth/user", "with_caveat": "gate"}},
        {"read": "viewer"},
        caveats=[("gate", "ok")],
    )
    doc = ObjectRef("test/doc", "one")
    backend().write_relationships(
        [RelationshipTuple(doc, "viewer", _user("alice"), "gate", {"ok": True})]
    )
    deadline = timezone.now() + timedelta(hours=1)
    _override(stored["gate"], "recaveat", "!ok")
    _override(stored["gate"], "recaveat", "ok", expires_at=deadline)
    # The later row wins while it lasts, although it restates the baseline.
    for instant, allowed in ((deadline - timedelta(seconds=1), True), (deadline, False)):
        with patch("django.utils.timezone.now", return_value=instant):
            assert _has(_user("alice"), "read", doc) is allowed


# ---------- Conditions ----------


@pytest.mark.parametrize("left,right", list(product((True, False, None), repeat=2)))
@pytest.mark.parametrize("operator", ["and", "or"])
def test_condition_algebra_retains_three_state_results_and_what_they_need(
    monkeypatch, left, right, operator
):
    from rebac.schema.walker import tri_and, tri_or

    schema = parse_zed("caveat l(x int) { x > 0 } caveat r(y int) { y > 0 }")
    verdicts = {"l": left, "r": right}

    def evaluate(caveat, pinned, context):
        return verdicts[caveat.name], {caveat.name} if verdicts[caveat.name] is None else set()

    monkeypatch.setattr(formulas, "evaluate_caveat", evaluate)
    a, b = formulas.leaf("l", {}), formulas.leaf("r", {})
    expected = {"and": tri_and, "or": tri_or}[operator](left, right)
    result, missing = formulas.evaluate({operator: [a, b]}, schema, {})
    assert result is expected
    # A conditional result needs its unknown operands; a decided one, nothing.
    needed = {name for name in verdicts if verdicts[name] is None} if result is None else set()
    assert missing == frozenset(needed)
    assert formulas.evaluate({operator: [b, a]}, schema, {}) == (result, missing)


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


# ---------- Schema graph and the stored policy ----------


def test_arrow_dependencies_include_all_allowed_types_and_subject_sets():
    program = CompileProgram.build(
        parse_zed("""
        definition auth/user {}
        definition folder { relation member: auth/user permission read = member }
        definition group { relation member: auth/user permission read = member }
        definition doc {
            relation parent: folder | group#member
            permission read = parent->read
        }
    """)
    )
    assert {dep.target for dep in program.dependencies["doc", "read"]} == {
        ("folder", "read"),
        ("group", "read"),
    }
    assert program.via_dependencies["doc", "read"] == (("doc", "parent"),)
    assert {dep.target for dep in program.dependencies["doc", "parent"]} == {("group", "member")}
    assert program.reachable(("doc", "read")) == {
        ("doc", "read"),
        ("doc", "parent"),
        ("folder", "read"),
        ("folder", "member"),
        ("group", "read"),
        ("group", "member"),
    }
    assert not program.recursive


def test_multi_type_component_is_recursive_in_any_definition_order():
    schema = parse_zed("""
        definition a { relation parent: b permission read = parent->read }
        definition b { relation parent: a permission read = parent->read }
    """)
    program = CompileProgram.build(schema)
    reverse = CompileProgram.build(Schema(definitions=list(reversed(schema.definitions))))
    assert program.recursive == reverse.recursive == {("a", "read"), ("b", "read")}
    assert program.components == reverse.components
    assert program.components["a", "read"] == {("a", "read"), ("b", "read")}


def test_manual_digest_is_computed_only_by_set_schema(monkeypatch):
    from rebac.schema import serialization

    digest = Mock(wraps=serialization.digest)
    monkeypatch.setattr(serialization, "digest", digest)
    active = LocalBackend()
    schema = parse_zed("definition doc { permission read = authenticated }")
    active.set_schema(schema)
    expected = active._manual_schema_revision()
    for _ in range(3):
        assert active._schema_snapshot().revision == expected
        assert active._manual_schema_revision() == expected
    digest.assert_called_once_with(repr(schema))
    active.set_schema(parse_zed("definition doc { permission read = anonymous }"))
    assert active._manual_schema_revision() != expected
    assert active._schema_snapshot().revision == active._manual_schema_revision()


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


def test_database_alias_is_validated_by_command_parser():
    from django.core.management.base import CommandError

    from rebac.management.commands.rebac import Command

    parser = Command().create_parser("manage.py", "rebac")
    with pytest.raises(CommandError, match="invalid choice"):
        parser.parse_args(["sync", "--database", "missing-database-alias"])


# ---------- Stored tuples ----------


def _validate_tuple(*, resource_id="ordinary", expires_at=None):
    schema = parse_zed("""
        use expiration
        definition auth/user {}
        definition storage/object { relation viewer: auth/user with expiration }
    """)
    LocalBackend()._validate_relationship_tuple(
        RelationshipTuple(
            ObjectRef("storage/object", resource_id),
            "viewer",
            SubjectRef.of("auth/user", "1"),
            expires_at=expires_at,
        ),
        schema=schema,
    )


@pytest.mark.parametrize("use_tz", [True, False])
def test_expiration_bounds_and_validation(settings, use_tz):
    settings.USE_TZ = use_tz
    zone = UTC if use_tz else None
    assert clock.TIME_MIN == datetime(1000, 1, 2, tzinfo=zone)
    assert clock.TIME_MAX == datetime(9999, 12, 30, tzinfo=zone)
    assert (clock.application_now().tzinfo is not None) is use_tz
    _validate_tuple(expires_at=None)
    _validate_tuple(expires_at=clock.TIME_MIN + timedelta(microseconds=1))
    _validate_tuple(expires_at=clock.TIME_MAX - timedelta(microseconds=1))
    for value in (
        clock.TIME_MIN,
        clock.TIME_MAX,
        clock.TIME_MIN - timedelta(days=1),
        clock.TIME_MAX + timedelta(days=1),
    ):
        with pytest.raises(ValueError):
            _validate_tuple(expires_at=value)
    with pytest.raises(ValueError, match="USE_TZ"):
        _validate_tuple(expires_at=datetime(2026, 1, 1, tzinfo=None if use_tz else UTC))


def test_wildcard_is_reserved_for_subjects_not_resource_ids():
    _validate_tuple(resource_id="ordinary")
    with pytest.raises(ValueError, match="resource IDs"):
        _validate_tuple(resource_id="*")


def test_resource_id_cannot_be_empty():
    with pytest.raises(ValueError, match="cannot be empty"):
        _validate_tuple(resource_id="")


# ---------- Registry identities ----------


@pytest.mark.django_db
def test_registry_interning_batches_and_recovers_every_identity(monkeypatch):
    original = models.QuerySet.bulk_create
    sizes = []

    def record(queryset, objects, **kwargs):
        if queryset.model is RebacResource:
            sizes.append(len(objects))
            # Identities that already exist are not an error.
            assert kwargs.get("ignore_conflicts") or kwargs.get("update_conflicts")
            assert kwargs["batch_size"] <= 200
        return original(queryset, objects, **kwargs)

    monkeypatch.setattr(models.QuerySet, "bulk_create", record)
    pairs = [("storage/registry", str(i)) for i in range(450)]
    first = RebacResource.upsert_refs_bulk(pairs + pairs)
    assert len(first) == 450
    assert sizes == [200, 200, 50]
    assert RebacResource.upsert_refs_bulk(list(reversed(pairs))) == first
    assert RebacResource.objects.filter(resource_type="storage/registry").count() == 450
    assert RebacResource.upsert_refs_bulk([]) == {}


@pytest.mark.django_db(transaction=True)
def test_registry_interning_missing_result_rolls_back(monkeypatch):
    original = models.QuerySet.values_list
    original_create = models.QuerySet.bulk_create

    def missing(queryset, *fields, **kwargs):
        if queryset.model is RebacResource:
            queryset = queryset.none()
        return original(queryset, *fields, **kwargs)

    monkeypatch.setattr(models.QuerySet, "values_list", missing)

    def without_returned_keys(queryset, objects, **kwargs):
        result = original_create(queryset, objects, **kwargs)
        if queryset.model is RebacResource:
            for row in objects:
                row.pk = None
        return result

    monkeypatch.setattr(models.QuerySet, "bulk_create", without_returned_keys)
    with pytest.raises(SchemaError, match="failed to retrieve"):
        RebacResource.upsert_refs_bulk([("storage/missing-registry", "one")])
    assert not RebacResource.objects.filter(resource_type="storage/missing-registry").exists()


# ---------- No database-specific SQL ----------


def test_library_uses_public_orm_without_database_sql():
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
