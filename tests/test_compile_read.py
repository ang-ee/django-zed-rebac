"""Focused invariants of live read planning and residual provenance."""

from datetime import timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from rebac import RelationshipTuple, sudo
from rebac.backends.local import LocalBackend
from rebac.compile.conditions import reachable_relations
from rebac.compile.evaluate import _BDD, _Edge, _Evaluator
from rebac.compile.read import accessible_ids, check, scope_q
from rebac.models import (
    SchemaCaveat,
    SchemaDefinition,
    SchemaOverride,
    SchemaPermission,
    SchemaRelation,
)
from rebac.schema import parse_zed
from rebac.testing import install_schema
from rebac.types import ObjectRef, SubjectRef
from tests.testapp.models import Post

pytestmark = pytest.mark.pg_delta


def clocks(monkeypatch):
    clock = {"app": timezone.now(), "sql": timezone.now()}
    clock["sql"] = clock["app"]
    monkeypatch.setattr(timezone, "now", lambda: clock["app"])
    monkeypatch.setattr("rebac.compile.predicate.statement_now", lambda: clock["sql"])
    return clock


def test_reachable_caveat_relations_cross_arrows_and_subject_sets():
    schema = parse_zed(
        """
        caveat active(ok bool) { ok }
        definition auth/user {}
        definition auth/group {
            relation member: auth/user with active | auth/group#member
        }
        definition blog/folder {
            relation viewer: auth/group#member
            permission read = viewer
        }
        definition blog/post {
            relation parent: blog/folder
            permission read = parent->read
        }
        """
    )

    assert reachable_relations(schema, ("blog/post", "read")) == {
        ("blog/post", "parent"),
        ("blog/folder", "viewer"),
        ("auth/group", "member"),
    }


def test_residual_boolean_graph_discards_order_dependent_missing_inputs():
    diagram = _BDD()
    a, b = diagram.atom(0), diagram.atom(1)
    # (a OR b) AND (a OR NOT b) is a: b cannot change the answer.
    left = diagram.apply("or", a, b)
    right = diagram.apply("or", a, diagram.negate(b))
    root = diagram.apply("and", left, right)

    assert diagram.relevant(root) == {0}


def test_residual_boolean_graph_keeps_negative_depth_dependency():
    diagram = _BDD()
    owner, deep_ban = diagram.atom(0), diagram.atom(1)
    root = diagram.apply("and", owner, diagram.negate(deep_ban))

    assert diagram.relevant(root) == {0, 1}


def test_lookup_names_only_source_paths_even_when_permission_is_public(monkeypatch):
    schema = parse_zed(
        """
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user | auth/user:*
            permission read = authenticated + viewer
        }
        """
    )
    resource = ObjectRef("blog/post", "p1")
    alice = SubjectRef.of("auth/user", "alice")
    bob = SubjectRef.of("auth/user", "bob")
    wildcard = SubjectRef.of("auth/user", "*")
    evaluator = _Evaluator(schema, alice, None, "default", timezone.now())
    monkeypatch.setattr(evaluator, "_rows", lambda *_args: (_Edge(wildcard),))

    assert evaluator.named(resource, "read", wildcard)
    assert not evaluator.named(resource, "read", alice)
    assert not evaluator.named(resource, "read", bob)


def test_residual_marks_recursive_ban_cycle_as_depth_relevant(monkeypatch):
    schema = parse_zed(
        """
        definition auth/user {}
        definition blog/folder {
            relation owner: auth/user
            relation parent: blog/folder
            permission read = owner + parent->read
            permission outside = authenticated - read
        }
        """
    )
    resource = ObjectRef("blog/folder", "one")
    evaluator = _Evaluator(
        schema, SubjectRef.of("auth/user", "alice"), None, "default", timezone.now()
    )

    def edges(_resource, relation, _definition):
        if relation.name == "parent":
            return (_Edge(SubjectRef.of("blog/folder", "one")),)
        return ()

    monkeypatch.setattr(evaluator, "_rows", edges)
    assert evaluator.residual(resource, "outside").depth_relevant


@pytest.mark.django_db
def test_reused_scope_sees_new_caveat_instance_at_statement_execution():
    local = install_schema(
        """
        caveat active(flag bool) { flag }
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user with active
            permission read = viewer
        }
        """
    )
    actor = SubjectRef.of("auth/user", "alice")
    with sudo(reason="compiled scope fixture"):
        post = Post._base_manager.create(title="late grant")
    predicate = scope_q(backend=local, model=Post, action="read", actor=actor, using="default")
    rows = Post._base_manager.filter(predicate)
    ids = accessible_ids(
        backend=local, resource_type="blog/post", action="read", actor=actor, using="default"
    )
    assert not rows.exists()
    assert list(ids) == []

    local.write_relationships(
        [
            RelationshipTuple(
                ObjectRef("blog/post", str(post.pk)),
                "viewer",
                actor,
                caveat_name="active",
                caveat_context={"flag": True},
            )
        ]
    )
    assert list(rows.values_list("pk", flat=True)) == [post.pk]
    assert list(ids) == [str(post.pk)]


@pytest.mark.django_db
def test_old_scope_closes_after_policy_revision_changes():
    local = install_schema(
        """
        definition auth/user {}
        definition blog/post { permission read = nil }
        """
    )
    actor = SubjectRef.of("auth/user", "alice")
    with sudo(reason="revision scope fixture"):
        post = Post._base_manager.create(title="new policy")
    old = Post._base_manager.filter(
        scope_q(backend=local, model=Post, action="read", actor=actor, using="default")
    )
    local.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition blog/post { permission read = authenticated }
            """
        )
    )

    assert not old.exists()
    current = Post._base_manager.filter(
        scope_q(backend=local, model=Post, action="read", actor=actor, using="default")
    )
    assert list(current.values_list("pk", flat=True)) == [post.pk]


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["scope", "enumeration"])
def test_revision_change_during_lazy_compilation_cannot_replace_the_pinned_policy(
    monkeypatch, operation
):
    from rebac.compile import read

    local = install_schema("""
        definition auth/user {}
        definition blog/post { permission read = nil }
    """)
    actor = SubjectRef.of("auth/user", "alice")
    with sudo(reason="revision compilation race"):
        Post._base_manager.create(title="denied by original policy")
    if operation == "scope":
        rows = Post._base_manager.filter(
            scope_q(backend=local, model=Post, action="read", actor=actor, using="default")
        )
        seam = "_scope_statement"
    else:
        rows = accessible_ids(
            backend=local, resource_type="blog/post", action="read", actor=actor, using="default"
        )
        seam = "_accessible_branches"
    compile_now = getattr(read, seam)

    def change_policy(*args, **kwargs):
        local.set_schema(
            parse_zed("""
            definition auth/user {}
            definition blog/post { permission read = authenticated }
        """)
        )
        return compile_now(*args, **kwargs)

    monkeypatch.setattr(read, seam, change_policy)
    assert list(rows) == []


@pytest.mark.django_db
def test_reused_scope_recompiles_after_override_deadline(monkeypatch):
    clock = clocks(monkeypatch)
    deadline = clock["app"] + timedelta(minutes=1)
    definition = SchemaDefinition.objects.create(resource_type="blog/post")
    permission = SchemaPermission.objects.create(
        definition=definition, name="read", expression="authenticated"
    )
    SchemaDefinition.objects.create(resource_type="auth/user")
    SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_TIGHTEN,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=permission.pk,
        expression="nil",
        reason="temporary closure",
        expires_at=deadline,
    )
    local = LocalBackend()
    with sudo(reason="override scope fixture"):
        post = Post._base_manager.create(title="visible after deadline")
    actor = SubjectRef.of("auth/user", "alice")
    predicate = scope_q(backend=local, model=Post, action="read", actor=actor, using="default")
    rows = Post._base_manager.filter(predicate)
    assert not rows.exists()

    # A fast application clock cannot remove a restriction still active in SQL.
    clock["app"] = deadline + timedelta(seconds=1)
    assert not rows.exists()

    clock["sql"] = clock["app"]
    assert list(rows.values_list("pk", flat=True)) == [post.pk]


@pytest.mark.django_db
@pytest.mark.parametrize("baseline,override", [("true", "false"), ("false", "true")])
def test_recaveat_verdicts_are_witnessed_in_their_selected_time_interval(
    monkeypatch, baseline, override
):
    from rebac.compile.conditions import CaveatVerdicts

    clock = clocks(monkeypatch)
    before = clock["app"]
    deadline = before + timedelta(minutes=1)
    SchemaDefinition.objects.create(resource_type="auth/user")
    definition = SchemaDefinition.objects.create(resource_type="blog/post")
    caveat = SchemaCaveat.objects.create(name="guarded", params=[], expression=baseline)
    SchemaRelation.objects.create(
        definition=definition,
        name="viewer",
        allowed_subjects=[{"type": "auth/user", "with_caveat": "guarded"}],
    )
    SchemaPermission.objects.create(definition=definition, name="read", expression="viewer")
    SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_RECAVEAT,
        target_ct=ContentType.objects.get_for_model(SchemaCaveat),
        target_pk=caveat.pk,
        expression=override,
        reason="temporary caveat",
        expires_at=deadline,
    )
    local = LocalBackend()
    actor = SubjectRef.of("auth/user", "alice")
    resource = ObjectRef("blog/post", "one")
    local.write_relationships([RelationshipTuple(resource, "viewer", actor, caveat_name="guarded")])

    def allowed():
        return check(
            backend=local,
            resource=resource,
            action="read",
            actor=actor,
            context=None,
            using="default",
        ).allowed

    assert allowed() == (override == "true")
    clock["app"] = deadline
    assert not allowed()  # The SQL clock has not reached the expired-override interval.
    clock["sql"] = deadline
    assert allowed() == (baseline == "true")

    # The preparation clock cannot be sampled again after a deadline crosses.
    clock.update(app=before, sql=before)
    prepare = CaveatVerdicts.prepare

    def crossing(*args, **kwargs):
        result = prepare(*args, **kwargs)
        clock.update(app=deadline, sql=deadline)
        return result

    monkeypatch.setattr(CaveatVerdicts, "prepare", crossing)
    assert not allowed()
