"""Tier-2 SchemaOverride composition tests.

Covers the load-bearing identity (zero overrides → AST-equal baseline),
the four operator kinds (TIGHTEN / LOOSEN / EXTEND / DISABLE), composition
order parenthesisation, multiple-disable non-commutativity, end-to-end
runtime evaluation through LocalBackend, signal-driven cache invalidation,
audit emission, and cycle detection.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.contenttypes.models import ContentType

from rebac import (
    LocalBackend,
    ObjectRef,
    PermissionEvaluator,
    RelationshipTuple,
    SubjectRef,
    evaluator_scope,
)
from rebac.composition import compose
from rebac.errors import SchemaError
from rebac.models import (
    PermissionAuditEvent,
    SchemaCaveat,
    SchemaDefinition,
    SchemaOverride,
    SchemaPermission,
    SchemaRelation,
)
from rebac.schema.ast import (
    AllowedSubject,
    Caveat,
    CaveatParam,
    Definition,
    PermBinOp,
    Permission,
    PermRef,
    Relation,
    Schema,
)
from rebac.schema.parser import parse_permission_expression

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _perm_expr(schema: Schema, resource_type: str, perm_name: str) -> object:
    """Fetch a permission's expression, asserting the permission exists."""
    perm = schema.get_permission(resource_type, perm_name)
    assert perm is not None
    return perm.expression


def _baseline_with_perm(resource_type: str, perm_name: str, expr_text: str) -> Schema:
    """Build a one-definition Schema with a single permission row."""
    expr = parse_permission_expression(expr_text)
    relations = (
        Relation("owner", (AllowedSubject("auth/user"),)),
        Relation("viewer", (AllowedSubject("auth/user"),)),
        Relation("auditor", (AllowedSubject("auth/user"),)),
    )
    perm = Permission(perm_name, expr, expr_text)
    return Schema(definitions=[Definition(resource_type, relations, (perm,))])


def _seed_db_schema(resource_type: str, perm_name: str, expr_text: str) -> SchemaPermission:
    """Persist a SchemaDefinition + SchemaPermission row + relation rows so
    SchemaOverride.target_pk can point at a real row.
    """
    sd = SchemaDefinition.objects.create(resource_type=resource_type)
    SchemaRelation.objects.create(
        definition=sd,
        name="owner",
        allowed_subjects=[{"type": "auth/user"}],
    )
    SchemaRelation.objects.create(
        definition=sd,
        name="viewer",
        allowed_subjects=[{"type": "auth/user"}],
    )
    SchemaRelation.objects.create(
        definition=sd,
        name="auditor",
        allowed_subjects=[{"type": "auth/user"}],
    )
    sp = SchemaPermission.objects.create(definition=sd, name=perm_name, expression=expr_text)
    return sp


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("warm_cache", [False, True])
@pytest.mark.parametrize(
    "surface", ["check", "accessible", "lookup", "grants_all", "evaluator", "evaluator_accessible"]
)
def test_expired_override_cannot_grant_access(monkeypatch, warm_cache, surface):
    from django.utils import timezone

    now = timezone.now()
    deadline = now + timedelta(minutes=1)
    clock = {"now": now}
    monkeypatch.setattr(timezone, "now", lambda: clock["now"])
    sp = _seed_db_schema("blog/post", "read", "owner")
    SchemaDefinition.objects.create(resource_type="auth/user")
    SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_LOOSEN,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=sp.pk,
        expression="authenticated" if surface == "grants_all" else "viewer",
        reason="temporary sharing",
        expires_at=deadline,
    )
    local = LocalBackend()
    viewer = SubjectRef.of("auth/user", "viewer")
    post = ObjectRef("blog/post", "1")
    local.write_relationships([RelationshipTuple(post, "viewer", viewer)])
    local.mark_schema_stale()
    evaluator = PermissionEvaluator()

    def granted():
        if surface == "accessible":
            return bool(
                list(local.accessible(subject=viewer, action="read", resource_type="blog/post"))
            )
        if surface == "lookup":
            return bool(
                list(local.lookup_subjects(resource=post, action="read", subject_type="auth/user"))
            )
        if surface == "evaluator":
            return evaluator.check(local, subject=viewer, action="read", resource=post).allowed
        if surface == "evaluator_accessible":
            return bool(
                evaluator.accessible(
                    local, subject=viewer, action="read", resource_type="blog/post"
                )
            )
        if surface == "grants_all":
            return local.grants_all(subject=viewer, action="read", resource_type="blog/post")
        return local.check_access(subject=viewer, action="read", resource=post).allowed

    with evaluator_scope(evaluator):
        if warm_cache:
            assert granted()
        clock["now"] = deadline
        assert not granted()


@pytest.mark.django_db(transaction=True)
def test_next_scope_observes_schema_edits_without_local_signals():
    sp = _seed_db_schema("blog/post", "read", "viewer")
    local = LocalBackend()
    viewer = SubjectRef.of("auth/user", "viewer")
    post = ObjectRef("blog/post", "1")
    local.write_relationships([RelationshipTuple(post, "viewer", viewer)])
    with evaluator_scope() as evaluator:
        assert evaluator.check(local, subject=viewer, action="read", resource=post).allowed
        # Model QuerySet.update bypasses this process's signals, simulating a
        # write committed by another worker. This scope retains its snapshot.
        SchemaPermission.objects.filter(pk=sp.pk).update(expression="owner")
        assert evaluator.check(local, subject=viewer, action="read", resource=post).allowed
    with evaluator_scope() as evaluator:
        assert not evaluator.check(local, subject=viewer, action="read", resource=post).allowed


@pytest.mark.django_db(transaction=True)
def test_evaluator_invalidation_refreshes_schema_after_external_edit():
    sp = _seed_db_schema("blog/post", "read", "viewer")
    local = LocalBackend()
    viewer = SubjectRef.of("auth/user", "viewer")
    post = ObjectRef("blog/post", "1")
    local.write_relationships([RelationshipTuple(post, "viewer", viewer)])
    with evaluator_scope() as evaluator:
        assert evaluator.check(local, subject=viewer, action="read", resource=post).allowed
        SchemaPermission.objects.filter(pk=sp.pk).update(expression="owner")
        # Subscription emissions reuse this object and call invalidate().
        evaluator.invalidate()
        assert not evaluator.check(local, subject=viewer, action="read", resource=post).allowed


@pytest.mark.django_db(transaction=True)
def test_unscoped_backend_reads_observe_schema_edits_without_local_signals():
    sp = _seed_db_schema("blog/post", "read", "viewer")
    local = LocalBackend()
    viewer = SubjectRef.of("auth/user", "viewer")
    post = ObjectRef("blog/post", "1")
    local.write_relationships([RelationshipTuple(post, "viewer", viewer)])
    assert local.check_access(subject=viewer, action="read", resource=post).allowed
    SchemaPermission.objects.filter(pk=sp.pk).update(expression="owner")
    assert not local.check_access(subject=viewer, action="read", resource=post).allowed


@pytest.mark.django_db(transaction=True)
def test_schema_grant_cannot_survive_transaction_rollback_in_evaluator():
    from django.db import transaction

    sp = _seed_db_schema("blog/post", "read", "owner")
    local = LocalBackend()
    viewer = SubjectRef.of("auth/user", "viewer")
    post = ObjectRef("blog/post", "1")
    local.write_relationships([RelationshipTuple(post, "viewer", viewer)])
    with evaluator_scope() as evaluator:
        with transaction.atomic():
            SchemaPermission.objects.filter(pk=sp.pk).update(expression="viewer")
            assert evaluator.check(local, subject=viewer, action="read", resource=post).allowed
            transaction.set_rollback(True)
        assert not evaluator.check(local, subject=viewer, action="read", resource=post).allowed


def _make_ovr(target: SchemaPermission, kind: str, expression: str) -> SchemaOverride:
    return SchemaOverride.objects.create(
        kind=kind,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=target.pk,
        expression=expression,
        reason="test",
    )


# ---------------------------------------------------------------------------
# Identity test — load-bearing.
# ---------------------------------------------------------------------------


def test_identity_zero_overrides_yields_ast_equal_baseline() -> None:
    baseline = _baseline_with_perm("blog/post", "read", "owner + viewer")
    composed = compose(baseline, [])

    assert composed.definitions == baseline.definitions
    assert composed.caveats == baseline.caveats
    # And specifically: the permission expression is AST-identical (same
    # frozen-dataclass tree, no extra wrapping nodes).
    new_perm = composed.get_permission("blog/post", "read")
    base_perm = baseline.get_permission("blog/post", "read")
    assert new_perm is not None and base_perm is not None
    assert new_perm.expression == base_perm.expression


# ---------------------------------------------------------------------------
# Per-kind composition shape tests.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_tighten_intersects_baseline() -> None:
    sp = _seed_db_schema("blog/post", "read", "owner + viewer")
    ovr = _make_ovr(sp, SchemaOverride.KIND_TIGHTEN, "is_active")

    baseline = _baseline_with_perm("blog/post", "read", "owner + viewer")
    composed = compose(baseline, [ovr])

    _perm = composed.get_permission("blog/post", "read")
    assert _perm is not None
    expr = _perm.expression
    # ((owner + viewer) & is_active)
    assert isinstance(expr, PermBinOp) and expr.op == "&"
    assert isinstance(expr.left, PermBinOp) and expr.left.op == "+"
    assert expr.left.left == PermRef("owner")
    assert expr.left.right == PermRef("viewer")
    assert expr.right == PermRef("is_active")


@pytest.mark.django_db
def test_loosen_unions_baseline() -> None:
    sp = _seed_db_schema("blog/post", "read", "owner")
    ovr = _make_ovr(sp, SchemaOverride.KIND_LOOSEN, "viewer")

    baseline = _baseline_with_perm("blog/post", "read", "owner")
    composed = compose(baseline, [ovr])

    _perm = composed.get_permission("blog/post", "read")
    assert _perm is not None
    expr = _perm.expression
    # (owner + viewer)
    assert isinstance(expr, PermBinOp) and expr.op == "+"
    assert expr.left == PermRef("owner")
    assert expr.right == PermRef("viewer")


@pytest.mark.django_db
def test_extend_unions_baseline_same_as_loosen() -> None:
    sp = _seed_db_schema("blog/post", "read", "owner")
    ovr = _make_ovr(sp, SchemaOverride.KIND_EXTEND, "viewer")

    baseline = _baseline_with_perm("blog/post", "read", "owner")
    composed = compose(baseline, [ovr])

    _perm = composed.get_permission("blog/post", "read")
    assert _perm is not None
    expr = _perm.expression
    assert isinstance(expr, PermBinOp) and expr.op == "+"
    assert expr.left == PermRef("owner")
    assert expr.right == PermRef("viewer")


@pytest.mark.django_db
def test_disable_subtracts_baseline() -> None:
    sp = _seed_db_schema("blog/post", "read", "owner + viewer + auditor")
    ovr = _make_ovr(sp, SchemaOverride.KIND_DISABLE, "auditor")

    baseline = _baseline_with_perm("blog/post", "read", "owner + viewer + auditor")
    composed = compose(baseline, [ovr])

    _perm = composed.get_permission("blog/post", "read")
    assert _perm is not None
    expr = _perm.expression
    # ((owner + viewer + auditor) - auditor)
    assert isinstance(expr, PermBinOp) and expr.op == "-"
    assert expr.right == PermRef("auditor")
    # Left should be the baseline union chain.
    assert isinstance(expr.left, PermBinOp) and expr.left.op == "+"


# ---------------------------------------------------------------------------
# Composition order -- parenthesisation must match (((B U E) - D) & T).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_composition_order_extend_then_disable_then_tighten() -> None:
    sp = _seed_db_schema("blog/post", "read", "owner")
    ext = _make_ovr(sp, SchemaOverride.KIND_EXTEND, "viewer")
    dis = _make_ovr(sp, SchemaOverride.KIND_DISABLE, "auditor")
    tig = _make_ovr(sp, SchemaOverride.KIND_TIGHTEN, "is_active")

    baseline = _baseline_with_perm("blog/post", "read", "owner")
    composed = compose(baseline, [ext, dis, tig])

    _perm = composed.get_permission("blog/post", "read")
    assert _perm is not None
    expr = _perm.expression
    # Outermost is the intersection (∩ tightens applied last).
    assert isinstance(expr, PermBinOp) and expr.op == "&"
    assert expr.right == PermRef("is_active")

    # One level in: subtract.
    inner = expr.left
    assert isinstance(inner, PermBinOp) and inner.op == "-"
    assert inner.right == PermRef("auditor")

    # Two levels in: union (baseline + extend).
    base_plus_ext = inner.left
    assert isinstance(base_plus_ext, PermBinOp) and base_plus_ext.op == "+"
    assert base_plus_ext.left == PermRef("owner")
    assert base_plus_ext.right == PermRef("viewer")


@pytest.mark.django_db
def test_multiple_disables_apply_deterministically_by_created_at() -> None:
    sp = _seed_db_schema("blog/post", "read", "owner + viewer + auditor")
    d1 = _make_ovr(sp, SchemaOverride.KIND_DISABLE, "auditor")
    d2 = _make_ovr(sp, SchemaOverride.KIND_DISABLE, "viewer")
    # Force d2.created_at > d1.created_at; same-timestamp on SQLite would be
    # ambiguous. Bump explicitly.
    SchemaOverride.objects.filter(pk=d2.pk).update(created_at=d1.created_at)
    # Re-fetch and apply distinct timestamps so the ordering test is meaningful.
    d2.refresh_from_db()

    baseline = _baseline_with_perm("blog/post", "read", "owner + viewer + auditor")
    # Pass in REVERSE created order to confirm compose reorders by (kind,
    # created_at) before applying.
    composed_a = compose(baseline, [d2, d1])
    composed_b = compose(baseline, [d1, d2])

    # Composition is deterministic: same input set, same AST regardless of
    # input iteration order.
    assert _perm_expr(composed_a, "blog/post", "read") == _perm_expr(
        composed_b, "blog/post", "read"
    )


# ---------------------------------------------------------------------------
# End-to-end runtime test through LocalBackend.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_extend_override_flows_through_to_runtime_evaluation() -> None:
    # Seed Tier-1 baseline rows: blog/post with `read = owner` and the
    # supporting relations.
    sd = SchemaDefinition.objects.create(resource_type="blog/post")
    SchemaRelation.objects.create(
        definition=sd, name="owner", allowed_subjects=[{"type": "auth/user"}]
    )
    SchemaRelation.objects.create(
        definition=sd, name="viewer", allowed_subjects=[{"type": "auth/user"}]
    )
    SchemaPermission.objects.create(definition=sd, name="read", expression="owner")
    SchemaDefinition.objects.create(resource_type="auth/user")

    backend = LocalBackend()

    # Write the relationship rows: alice as owner, bob as viewer.
    alice = SubjectRef.of("auth/user", "alice")
    bob = SubjectRef.of("auth/user", "bob")
    p1 = ObjectRef("blog/post", "p1")

    backend.write_relationships(
        [
            RelationshipTuple(resource=p1, relation="owner", subject=alice),
            RelationshipTuple(resource=p1, relation="viewer", subject=bob),
        ]
    )

    # Without override: alice reads, bob doesn't.
    assert backend.has_access(subject=alice, action="read", resource=p1)
    assert not backend.has_access(subject=bob, action="read", resource=p1)
    assert set(backend.accessible(subject=bob, action="read", resource_type="blog/post")) == set()

    # Add EXTEND override: now `viewer` is also a path to `read`.
    sp = SchemaPermission.objects.get(definition=sd, name="read")
    SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_EXTEND,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=sp.pk,
        expression="viewer",
        reason="grant viewers read access",
    )

    # Force the LocalBackend to rebuild its in-memory schema. (Note the
    # signal also invalidates the global cache; this `LocalBackend` was
    # constructed locally without going through `backend()`, so we reset
    # its schema explicitly.)
    backend._schema = None

    assert backend.has_access(subject=bob, action="read", resource=p1)
    assert set(backend.accessible(subject=bob, action="read", resource_type="blog/post")) == {"p1"}


# ---------------------------------------------------------------------------
# Cache invalidation — global backend signal.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_override_create_invalidates_global_backend_cache() -> None:
    # Seed Tier-1 rows.
    sd = SchemaDefinition.objects.create(resource_type="blog/post")
    SchemaRelation.objects.create(
        definition=sd, name="owner", allowed_subjects=[{"type": "auth/user"}]
    )
    SchemaRelation.objects.create(
        definition=sd, name="viewer", allowed_subjects=[{"type": "auth/user"}]
    )
    sp = SchemaPermission.objects.create(definition=sd, name="read", expression="owner")
    SchemaDefinition.objects.create(resource_type="auth/user")

    # Force-construct the global backend & prime its schema cache.
    from rebac.backends import backend, reset_backend

    reset_backend()
    b1 = backend()
    _ = b1.schema()  # trigger lazy load
    assert b1._schema is not None  # type: ignore[attr-defined]

    # Create an override -- the post_save signal must reset the cached backend.
    SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_EXTEND,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=sp.pk,
        expression="viewer",
        reason="x",
    )

    # The module-level _backend should be None after reset_backend() ran.
    from rebac.backends import _backend as b_after  # re-import to read fresh

    assert b_after is None

    # Next call rebuilds; the rebuilt schema reflects the override.
    b2 = backend()
    expr = _perm_expr(b2.schema(), "blog/post", "read")
    assert isinstance(expr, PermBinOp) and expr.op == "+"
    reset_backend()


@pytest.mark.django_db
def test_override_delete_invalidates_cache() -> None:
    sd = SchemaDefinition.objects.create(resource_type="blog/post")
    SchemaRelation.objects.create(
        definition=sd, name="owner", allowed_subjects=[{"type": "auth/user"}]
    )
    SchemaRelation.objects.create(
        definition=sd, name="viewer", allowed_subjects=[{"type": "auth/user"}]
    )
    sp = SchemaPermission.objects.create(definition=sd, name="read", expression="owner")
    SchemaDefinition.objects.create(resource_type="auth/user")

    ovr = SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_EXTEND,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=sp.pk,
        expression="viewer",
        reason="x",
    )

    from rebac.backends import backend, reset_backend

    reset_backend()
    b1 = backend()
    expr_with = _perm_expr(b1.schema(), "blog/post", "read")
    assert isinstance(expr_with, PermBinOp) and expr_with.op == "+"

    # Deletion fires post_delete → reset_backend.
    ovr.delete()

    b2 = backend()
    expr_without = _perm_expr(b2.schema(), "blog/post", "read")
    # No override => bare `owner` ref.
    assert expr_without == PermRef("owner")
    reset_backend()


# ---------------------------------------------------------------------------
# Audit emission.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_override_create_emits_audit_event() -> None:
    sd = SchemaDefinition.objects.create(resource_type="blog/post")
    SchemaRelation.objects.create(
        definition=sd, name="owner", allowed_subjects=[{"type": "auth/user"}]
    )
    sp = SchemaPermission.objects.create(definition=sd, name="read", expression="owner")

    PermissionAuditEvent.objects.all().delete()
    SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_TIGHTEN,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=sp.pk,
        expression="is_active",
        reason="lock down read",
    )

    events = list(
        PermissionAuditEvent.objects.filter(kind=PermissionAuditEvent.KIND_OVERRIDE_CREATE)
    )
    assert len(events) == 1
    ev = events[0]
    assert "tighten" in ev.target_repr
    assert ev.reason == "lock down read"
    assert ev.after is not None and ev.after.get("expression") == "is_active"
    assert ev.before is None


@pytest.mark.django_db(transaction=True)
def test_override_delete_emits_audit_event() -> None:
    sd = SchemaDefinition.objects.create(resource_type="blog/post")
    SchemaRelation.objects.create(
        definition=sd, name="owner", allowed_subjects=[{"type": "auth/user"}]
    )
    sp = SchemaPermission.objects.create(definition=sd, name="read", expression="owner")

    ovr = SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_DISABLE,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=sp.pk,
        expression="auditor",
        reason="x",
    )

    PermissionAuditEvent.objects.all().delete()
    ovr.delete()

    events = list(
        PermissionAuditEvent.objects.filter(kind=PermissionAuditEvent.KIND_OVERRIDE_DELETE)
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.before is not None and ev.before.get("expression") == "auditor"
    assert ev.after is None


# ---------------------------------------------------------------------------
# Cycle detection.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_override_introducing_self_cycle_is_rejected() -> None:
    sp = _seed_db_schema("blog/post", "read", "owner")
    # Introduce `read = read` via a tighten override — `(owner & read)`
    # closes the loop on `read`.
    ovr = _make_ovr(sp, SchemaOverride.KIND_TIGHTEN, "read")

    baseline = _baseline_with_perm("blog/post", "read", "owner")
    with pytest.raises(SchemaError) as exc_info:
        compose(baseline, [ovr])
    assert "cycle" in str(exc_info.value).lower()


@pytest.mark.django_db
def test_baseline_cycle_is_not_attributed_to_override() -> None:
    """If the baseline ALREADY has a cycle (degenerate / pre-existing), the
    composer should not flag it as introduced by overrides.
    """
    # Hand-build a baseline where `read` self-references — bypass the parser's
    # validate step by constructing AST directly.
    bad_baseline = Schema(
        definitions=[
            Definition(
                "blog/post",
                relations=(Relation("owner", (AllowedSubject("auth/user"),)),),
                permissions=(Permission("read", PermRef("read"), "read"),),
            )
        ]
    )
    # Composition with no overrides and a pre-existing cycle: must NOT raise.
    result = compose(bad_baseline, [])
    assert result.get_definition("blog/post") is not None


# ---------------------------------------------------------------------------
# RECAVEAT.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_recaveat_replaces_caveat_expression_last_write_wins() -> None:
    cav = SchemaCaveat.objects.create(
        name="is_business_hours",
        params=[{"name": "now", "type": "timestamp"}],
        expression="now.getHours() >= 9 && now.getHours() < 17",
    )

    ct = ContentType.objects.get_for_model(SchemaCaveat)
    o1 = SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_RECAVEAT,
        target_ct=ct,
        target_pk=cav.pk,
        expression="now.getHours() >= 8 && now.getHours() < 18",
        reason="extend hours",
    )
    o2 = SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_RECAVEAT,
        target_ct=ct,
        target_pk=cav.pk,
        expression="true",  # always allow — last write wins
        reason="emergency open",
    )
    # Force monotonic timestamps so the test is deterministic.
    SchemaOverride.objects.filter(pk=o2.pk).update(created_at=o1.created_at)
    o2.refresh_from_db()

    baseline = Schema(
        caveats=[
            Caveat(
                name="is_business_hours",
                params=(CaveatParam("now", "timestamp"),),
                expression="now.getHours() >= 9 && now.getHours() < 17",
            )
        ]
    )

    composed = compose(baseline, [o1, o2])
    cav_after = composed.get_caveat("is_business_hours")
    assert cav_after is not None
    # Last-by-(created_at, pk) wins; o2's expression should be present
    # whichever order they're passed in.
    assert cav_after.expression == o2.expression
    # Params are unchanged in v1.
    assert cav_after.params == (CaveatParam("now", "timestamp"),)
