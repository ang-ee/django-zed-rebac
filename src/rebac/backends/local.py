"""LocalBackend — pure-Django REBAC evaluation.

Evaluates permission expressions by walking the in-memory schema tree against
rows in the `Relationship` table. Implementation strategy:

  - For `accessible()`: BFS over the relation graph, materialising candidate
    resource_ids per visited relation. Final result is the union/intersection
    of contributors per the expression operators.
  - For `check_access()`: same walk, but bounded by the specific resource_id.
  - Recursion depth bounded by `REBAC_DEPTH_LIMIT`.

This is intentionally a clean Python implementation — fully correct against
the SpiceDB semantics for the subset of the schema language the parser
accepts. A recursive-CTE optimisation path is layered on for `accessible()`
when `REBAC_PK_IN_THRESHOLD` is exceeded; for v0.1 we use the Python walk
with prefetched relationship rows. The same code path runs on Postgres / MySQL
/ SQLite identically.

Caveats are tri-state:

  - True path  → row matches.
  - False path → row treated as if absent.
  - None path  → caveat is conditional (required params not supplied).
    `_eval_permission` / `_has_direct_relation` return `None` and accumulate
    the union of required-but-missing param names into the caller's
    `missing` set. `check_access` surfaces CONDITIONAL when no unconditional
    path matches; `accessible` silently excludes conditional rows
    (read-side conservative).
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from typing import Any
from weakref import WeakSet

from ..actors import is_anonymous_actor
from ..conf import app_settings
from ..errors import PermissionDepthExceeded
from ..schema.ast import (
    BUILTIN_ACTOR_TYPES,
    AllowedSubject,
    Definition,
    PermArrow,
    PermBinOp,
    PermExpr,
    PermNil,
    PermRef,
    Relation,
    Schema,
)
from ..types import (
    CheckResult,
    Consistency,
    ObjectRef,
    RelationshipFilter,
    RelationshipTuple,
    SubjectRef,
    Zookie,
)
from .base import Backend

_backend_registry_lock = Lock()
_db_loaded_backends: WeakSet[LocalBackend] = WeakSet()

# Per-evaluation freshness cutoff. When non-None, every Relationship
# queryset inside _has_direct_relation / _resources_via_relation /
# _resources_for_expr is narrowed by ``written_at_xid__lte=cutoff`` so
# `Consistency.AT_LEAST_AS_FRESH(zookie)` semantics hold across the
# whole walk without threading the value through every internal call.
# ContextVar (not instance state) because LocalBackend is a singleton
# reused across requests / async tasks; each task gets its own slot.
_freshness_xid: ContextVar[int | None] = ContextVar("rebac_local_freshness_xid", default=None)


@contextmanager
def _freshness_scope(xid: int | None) -> Iterator[None]:
    """Bracket every internal read with ``written_at_xid <= xid`` when set."""
    token = _freshness_xid.set(xid)
    try:
        yield
    finally:
        _freshness_xid.reset(token)


class LocalBackend(Backend):
    """Recursive-CTE-style evaluator implemented as a bounded graph walk."""

    kind = "local"

    @staticmethod
    def _validate_zookie(at_zookie: Zookie | None) -> int | None:
        """Validate the backend kind and return the freshness xid cutoff.

        Returns ``None`` when no zookie was supplied. Raises ``ValueError``
        when the zookie was emitted by a different backend (a SpiceDB
        token handed here would be interpreted as a numeric xid with
        garbage semantics; fail loudly).
        """
        if at_zookie is None:
            return None
        if at_zookie.backend != "local":
            raise ValueError(
                f"LocalBackend cannot consume a Zookie from backend "
                f"{at_zookie.backend!r}. Drain or translate the token at "
                f"the boundary where backends switched."
            )
        try:
            return int(at_zookie.token)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"LocalBackend Zookie token must be a numeric xid; got {at_zookie.token!r}"
            ) from exc

    def _apply_freshness(self, qs: Any) -> Any:
        """Narrow a Relationship queryset by the ambient freshness cutoff.

        No-op when no scope is open. Applied at every relationship-table
        read in the evaluation walk so any path through the engine
        honours the floor uniformly.
        """
        cutoff = _freshness_xid.get()
        if cutoff is None:
            return qs
        return qs.filter(written_at_xid__lte=cutoff)

    def __init__(self) -> None:
        self._schema_lock = Lock()
        self._schema: Schema | None = None
        self._schema_is_manual = False
        # Counter used as a stable monotonic xid on backends (e.g. SQLite test
        # mode) without `txid_current()`.
        self._xid_counter = 0
        with _backend_registry_lock:
            _db_loaded_backends.add(self)

    # ---------- Schema management ----------

    def set_schema(self, schema: Schema) -> None:
        """Install the in-memory schema. Called by the sync command."""
        with self._schema_lock:
            self._schema = schema
            self._schema_is_manual = True

    def schema(self) -> Schema:
        with self._schema_lock:
            if self._schema is None:
                # Lazy load from DB-stored Schema* rows. Schema model signals
                # mark DB-loaded backends stale when those rows change in this
                # process, avoiding schema-table reads on every permission check.
                self._schema = self._load_schema_from_db()
                self._schema_is_manual = False
            return self._schema

    def mark_schema_stale(self) -> None:
        """Drop a DB-loaded schema cache after Schema* row changes."""
        with self._schema_lock:
            if not self._schema_is_manual:
                self._schema = None

    def _load_schema_from_db(self) -> Schema:
        from django.db.models import Prefetch

        from ..composition import compose
        from ..models import (
            SchemaCaveat,
            SchemaDefinition,
            SchemaOverride,
            SchemaPermission,
            SchemaRelation,
        )
        from ..schema.ast import (
            Caveat,
            CaveatParam,
            Definition,
            Permission,
            Relation,
            Schema,
        )
        from ..schema.parser import parse_permission_expression

        # Bake the per-relation order_by into Prefetch so the prefetch cache
        # is reused; a bare `d.relations.all().order_by(...)` per definition
        # is N+1 on schema load.
        defs: list[Definition] = []
        defs_qs = SchemaDefinition.objects.prefetch_related(
            Prefetch("relations", queryset=SchemaRelation.objects.order_by("name")),
            Prefetch("permissions", queryset=SchemaPermission.objects.order_by("name")),
        ).order_by("resource_type")
        for d in defs_qs:
            relations = []
            for r in d.relations.all():
                allowed = tuple(
                    AllowedSubject(
                        type=item["type"],
                        relation=item.get("relation", ""),
                        wildcard=item.get("wildcard", False),
                        with_caveat=item.get("with_caveat", ""),
                        id=item.get("id", ""),
                    )
                    for item in (r.allowed_subjects or [])
                )
                relations.append(Relation(r.name, allowed, r.with_expiration))
            permissions: list[Permission] = []
            for p in d.permissions.all():
                expr = parse_permission_expression(p.expression)
                permissions.append(Permission(p.name, expr, p.expression))
            defs.append(Definition(d.resource_type, tuple(relations), tuple(permissions)))

        caveats = []
        for c in SchemaCaveat.objects.order_by("name"):
            params = tuple(CaveatParam(p["name"], p["type"]) for p in (c.params or []))
            caveats.append(Caveat(c.name, params, c.expression))

        baseline = Schema(definitions=defs, caveats=caveats)

        # Tier-2: apply SchemaOverride composition. `compose()` is the
        # single source of determinism (it re-sorts disables by
        # (created_at, pk) per kind), so the loader-side order_by is just
        # cosmetic; we keep it for readable EXPLAIN plans.
        overrides = list(SchemaOverride.objects.all().order_by("kind", "created_at", "pk"))
        return compose(baseline, overrides)

    # ---------- Public API ----------

    def check_access(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: dict | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> CheckResult:
        cutoff = self._validate_zookie(at_zookie)
        with _freshness_scope(cutoff):
            return self._check_access(subject, action, resource, context)

    def _check_access(
        self,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: dict | None,
    ) -> CheckResult:
        # Empty resource_id → model-level check (any row of this type the subject
        # has the action on). Treat as "is the accessible() set non-empty?".
        if not resource.resource_id:
            try:
                next(
                    iter(
                        self._accessible(
                            subject=subject,
                            action=action,
                            resource_type=resource.resource_type,
                            context=context,
                        )
                    )
                )
            except StopIteration:
                return CheckResult.no()
            return CheckResult.has()

        definition = self.schema().get_definition(resource.resource_type)
        if definition is None:
            return CheckResult.no(reason=f"unknown resource type: {resource.resource_type}")

        permission = self.schema().get_permission(resource.resource_type, action)
        # Per-call accumulator for "missing caveat parameter" names. Populated
        # by tri-state row scans below; surfaced via CheckResult.conditional.
        missing: set[str] = set()
        if permission is None:
            # No permission expression — fall back to checking the relation directly.
            allowed = self._has_direct_relation(
                resource_type=resource.resource_type,
                resource_id=resource.resource_id,
                relation=action,
                subject=subject,
                depth=0,
                context=context,
                missing=missing,
            )
        else:
            allowed = self._eval_permission(
                expr=permission.expression,
                definition=definition,
                resource_id=resource.resource_id,
                subject=subject,
                depth=0,
                context=context,
                missing=missing,
            )
        if allowed is True:
            return CheckResult.has()
        if allowed is None:
            return CheckResult.conditional(missing=tuple(sorted(missing)))
        return CheckResult.no()

    def accessible(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource_type: str,
        context: dict | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> Iterable[str]:
        cutoff = self._validate_zookie(at_zookie)
        with _freshness_scope(cutoff):
            return self._accessible(
                subject=subject,
                action=action,
                resource_type=resource_type,
                context=context,
            )

    def _accessible(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource_type: str,
        context: dict | None = None,
    ) -> list[str]:
        # accessible() is read-side conservative for caveats: rows whose caveat
        # evaluates to False OR is conditional (missing params) are excluded
        # silently. Callers wanting to learn about conditional rows should use
        # check_access() against a specific resource id.
        definition = self.schema().get_definition(resource_type)
        if definition is None:
            return []
        permission = self.schema().get_permission(resource_type, action)
        # Cycle-breaker for self-referential traversals (e.g. folder.parent->read).
        # Mapping `(resource_type, action)` -> currently-resolving sentinel or set.
        cache: dict[tuple[str, str], set[str] | None] = {}
        if permission is None:
            return list(
                self._resources_via_relation(
                    resource_type=resource_type,
                    relation=action,
                    subject=subject,
                    depth=0,
                    cache=cache,
                    context=context,
                )
            )
        return list(
            self._resources_for_expr(
                expr=permission.expression,
                definition=definition,
                subject=subject,
                depth=0,
                cache=cache,
                context=context,
            )
        )

    def grants_all(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource_type: str,
        context: dict | None = None,
    ) -> bool:
        """Return True when the schema grants every row of a type.

        `accessible()` can enumerate relationship-derived grants, but a
        schema-level term like `permission list = authenticated` has no
        relationship rows to enumerate. QuerySet integration uses this
        predicate to skip adding an id filter when the permission expression
        itself grants the whole resource type to the actor.
        """
        del context
        definition = self.schema().get_definition(resource_type)
        if definition is None:
            return False
        permission = self.schema().get_permission(resource_type, action)
        if permission is None:
            return False
        return self._expr_grants_all(
            permission.expression,
            definition,
            subject,
            seen=set(),
        )

    def lookup_subjects(
        self,
        *,
        resource: ObjectRef,
        action: str,
        subject_type: str,
        context: dict | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> Iterable[SubjectRef]:
        # Minimal forward lookup — direct relation rows only. Walking through
        # subject sets / arrows for reverse lookup is deferred to v0.2.
        from ..models import active_relationship_model

        cutoff = self._validate_zookie(at_zookie)
        RelationshipModel = active_relationship_model()

        permission = self.schema().get_permission(resource.resource_type, action)
        relation_names = []
        if permission is None:
            relation_names = [action]
        else:
            relation_names = sorted(_collect_direct_relations(permission.expression))

        if not relation_names:
            return []

        with _freshness_scope(cutoff):
            rows = self._apply_freshness(
                RelationshipModel.objects.filter(
                    resource_type=resource.resource_type,
                    resource_id=resource.resource_id,
                    relation__in=relation_names,
                    subject_type=subject_type,
                )
            )
            return [
                SubjectRef.of(r.subject_type, r.subject_id, r.optional_subject_relation)
                for r in rows
            ]

    def write_relationships(self, writes: Iterable[RelationshipTuple]) -> Zookie:
        from django.db import transaction

        from ..models import active_relationship_model

        RelationshipModel = active_relationship_model()

        rows = list(writes)
        max_xid = 0
        with transaction.atomic():
            for tup in rows:
                xid = self._next_xid()
                if xid > max_xid:
                    max_xid = xid
                RelationshipModel.objects.update_or_create(
                    resource_type=tup.resource.resource_type,
                    resource_id=tup.resource.resource_id,
                    relation=tup.relation,
                    subject_type=tup.subject.subject_type,
                    subject_id=tup.subject.subject_id,
                    optional_subject_relation=tup.subject.optional_relation,
                    caveat_name=tup.caveat_name,
                    defaults={
                        "caveat_context": tup.caveat_context or None,
                        "expires_at": tup.expires_at,
                        "written_at_xid": xid,
                    },
                )
        # Zookie token == the maximum xid actually written in the batch,
        # so ``at_least_as_fresh(zookie)`` reads include every row this
        # call produced and exclude every row written strictly later.
        # An empty batch returns the most-recent watermark (or 0 on a
        # fresh backend) — never advances the clock.
        if max_xid == 0:
            return self._zookie()
        return Zookie(self.kind, str(max_xid))

    def delete_relationships(self, filter_: RelationshipFilter) -> Zookie:
        from ..models import active_relationship_model

        RelationshipModel = active_relationship_model()

        qs = RelationshipModel.objects.all()
        if filter_.resource_type:
            qs = qs.filter(resource_type=filter_.resource_type)
        if filter_.resource_id:
            qs = qs.filter(resource_id=filter_.resource_id)
        if filter_.relation:
            qs = qs.filter(relation=filter_.relation)
        if filter_.subject_type:
            qs = qs.filter(subject_type=filter_.subject_type)
        if filter_.subject_id:
            qs = qs.filter(subject_id=filter_.subject_id)
        if filter_.optional_subject_relation:
            qs = qs.filter(optional_subject_relation=filter_.optional_subject_relation)
        qs.delete()
        return self._zookie()

    def delete_relationship(self, tuple_: RelationshipTuple) -> Zookie:
        from ..models import active_relationship_model

        RelationshipModel = active_relationship_model()
        RelationshipModel.objects.filter(
            resource_type=tuple_.resource.resource_type,
            resource_id=tuple_.resource.resource_id,
            relation=tuple_.relation,
            subject_type=tuple_.subject.subject_type,
            subject_id=tuple_.subject.subject_id,
            optional_subject_relation=tuple_.subject.optional_relation,
            caveat_name=tuple_.caveat_name,
        ).delete()
        return self._zookie()

    # ---------- Internal evaluation ----------

    def _eval_permission(
        self,
        expr: PermExpr,
        definition: Definition,
        resource_id: str,
        subject: SubjectRef,
        depth: int,
        context: dict | None = None,
        missing: set[str] | None = None,
    ) -> bool | None:
        """Tri-state permission evaluation.

        Returns:
            True  — permission unconditionally allowed.
            False — permission unconditionally denied.
            None  — at least one path is conditional on caveat params not yet
                    supplied; the union of missing names is added to
                    `missing` (caller-owned set).
        """
        # Depth counts dispatch hops (arrow walks + subject-set traversals),
        # not expression-tree shape. Binary operators don't increment depth.
        if depth > app_settings.REBAC_DEPTH_LIMIT:
            raise PermissionDepthExceeded(f"Depth limit {app_settings.REBAC_DEPTH_LIMIT} exceeded")
        if missing is None:
            missing = set()
        if isinstance(expr, PermNil):
            return False
        if isinstance(expr, PermRef):
            if expr.name in BUILTIN_ACTOR_TYPES:
                return _builtin_actor_matches(expr.name, subject)
            relation = _find_relation(definition, expr.name)
            if relation is not None:
                return self._has_direct_relation(
                    resource_type=definition.resource_type,
                    resource_id=resource_id,
                    relation=expr.name,
                    subject=subject,
                    depth=depth,
                    context=context,
                    missing=missing,
                )
            sub_perm = next((p for p in definition.permissions if p.name == expr.name), None)
            if sub_perm is not None:
                return self._eval_permission(
                    sub_perm.expression,
                    definition,
                    resource_id,
                    subject,
                    depth,
                    context,
                    missing,
                )
            return False
        if isinstance(expr, PermArrow):
            from ..models import active_relationship_model

            RelationshipModel = active_relationship_model()

            via = _find_relation(definition, expr.via)
            if via is None:
                return False
            targets = self._apply_freshness(
                RelationshipModel.objects.filter(
                    resource_type=definition.resource_type,
                    resource_id=resource_id,
                    relation=expr.via,
                )
            )
            saw_conditional = False
            for row in targets:
                # The hop row itself may carry a caveat — evaluate it before
                # walking through to the target type.
                hop = self._evaluate_row_caveat(row, context, missing)
                if hop is False:
                    continue
                target_def = self.schema().get_definition(row.subject_type)
                if target_def is None:
                    continue
                inner = self._eval_permission_on(
                    permission_name=expr.target,
                    definition=target_def,
                    resource_id=row.subject_id,
                    subject=subject,
                    depth=depth + 1,
                    context=context,
                    missing=missing,
                )
                # Combine hop AND inner.
                combined = _and(hop, inner)
                if combined is True:
                    return True
                if combined is None:
                    saw_conditional = True
            if saw_conditional:
                return None
            return False
        if isinstance(expr, PermBinOp):
            left = self._eval_permission(
                expr.left, definition, resource_id, subject, depth, context, missing
            )
            if expr.op == "+":
                if left is True:
                    return True
                right = self._eval_permission(
                    expr.right, definition, resource_id, subject, depth, context, missing
                )
                return _or(left, right)
            if expr.op == "&":
                if left is False:
                    return False
                right = self._eval_permission(
                    expr.right, definition, resource_id, subject, depth, context, missing
                )
                return _and(left, right)
            if expr.op == "-":
                if left is False:
                    return False
                right = self._eval_permission(
                    expr.right, definition, resource_id, subject, depth, context, missing
                )
                return _minus(left, right)
            raise ValueError(f"unknown operator: {expr.op}")
        raise TypeError(f"unknown PermExpr: {expr!r}")

    def _expr_grants_all(
        self,
        expr: PermExpr,
        definition: Definition,
        subject: SubjectRef,
        *,
        seen: set[tuple[str, str]],
    ) -> bool:
        if isinstance(expr, PermNil):
            return False
        if isinstance(expr, PermRef):
            if expr.name in BUILTIN_ACTOR_TYPES:
                return _builtin_actor_matches(expr.name, subject)
            relation = _find_relation(definition, expr.name)
            if relation is not None:
                return False
            key = (definition.resource_type, expr.name)
            if key in seen:
                return False
            sub_perm = next((p for p in definition.permissions if p.name == expr.name), None)
            if sub_perm is None:
                return False
            return self._expr_grants_all(
                sub_perm.expression,
                definition,
                subject,
                seen={*seen, key},
            )
        if isinstance(expr, PermArrow):
            return False
        if isinstance(expr, PermBinOp):
            left = self._expr_grants_all(expr.left, definition, subject, seen=seen)
            if expr.op == "+":
                return left or self._expr_grants_all(
                    expr.right,
                    definition,
                    subject,
                    seen=seen,
                )
            if expr.op == "&":
                return left and self._expr_grants_all(
                    expr.right,
                    definition,
                    subject,
                    seen=seen,
                )
            if expr.op == "-":
                return False
            raise ValueError(f"unknown operator: {expr.op}")
        raise TypeError(f"unknown PermExpr: {expr!r}")

    def _eval_permission_on(
        self,
        permission_name: str,
        definition: Definition,
        resource_id: str,
        subject: SubjectRef,
        depth: int,
        context: dict | None = None,
        missing: set[str] | None = None,
    ) -> bool | None:
        permission = next((p for p in definition.permissions if p.name == permission_name), None)
        if permission is None:
            # Treat as direct relation lookup.
            return self._has_direct_relation(
                resource_type=definition.resource_type,
                resource_id=resource_id,
                relation=permission_name,
                subject=subject,
                depth=depth,
                context=context,
                missing=missing,
            )
        return self._eval_permission(
            permission.expression, definition, resource_id, subject, depth, context, missing
        )

    def _has_direct_relation(
        self,
        resource_type: str,
        resource_id: str,
        relation: str,
        subject: SubjectRef,
        depth: int,
        context: dict | None = None,
        missing: set[str] | None = None,
    ) -> bool | None:
        """Tri-state direct-relation lookup.

        Returns True / False as before; returns None if the only matching row
        is conditional (caveat params missing), accumulating those names in
        the caller-owned `missing` set.
        """
        # Subject-set rows count as a dispatch hop, so callers add 1 there;
        # the entry guard catches runaway recursion.
        if depth > app_settings.REBAC_DEPTH_LIMIT:
            raise PermissionDepthExceeded(f"Depth limit {app_settings.REBAC_DEPTH_LIMIT} exceeded")
        if missing is None:
            missing = set()
        from ..models import active_relationship_model

        RelationshipModel = active_relationship_model()

        rows = self._apply_freshness(
            RelationshipModel.objects.filter(
                resource_type=resource_type,
                resource_id=resource_id,
                relation=relation,
            )
        )
        saw_conditional = False
        # Direct subject match
        direct = _filter_active(
            rows.filter(
                subject_type=subject.subject_type,
                subject_id=subject.subject_id,
                optional_subject_relation=subject.optional_relation,
            )
        )
        for row in direct:
            verdict = self._evaluate_row_caveat(row, context, missing)
            if verdict is True:
                return True
            if verdict is None:
                saw_conditional = True

        # Wildcard match on (subject_type, "*"). Only valid for direct subject
        # types (not subject sets).
        if not subject.optional_relation:
            wildcard = _filter_active(
                rows.filter(
                    subject_type=subject.subject_type,
                    subject_id="*",
                )
            )
            for row in wildcard:
                verdict = self._evaluate_row_caveat(row, context, missing)
                if verdict is True:
                    return True
                if verdict is None:
                    saw_conditional = True

        # Subject-set rows: e.g. `viewer @ auth/group:eng#member`. Walk the
        # group's `member` relation and see if subject is a member.
        for row in rows.exclude(optional_subject_relation=""):
            if not _is_active(row):
                continue
            hop = self._evaluate_row_caveat(row, context, missing)
            if hop is False:
                continue
            inner = self._has_direct_relation(
                resource_type=row.subject_type,
                resource_id=row.subject_id,
                relation=row.optional_subject_relation,
                subject=subject,
                depth=depth + 1,
                context=context,
                missing=missing,
            )
            combined = _and(hop, inner)
            if combined is True:
                return True
            if combined is None:
                saw_conditional = True
        if saw_conditional:
            return None
        return False

    def _evaluate_row_caveat(
        self,
        row: object,
        context: dict | None,
        missing: set[str],
    ) -> bool | None:
        """Evaluate a Relationship row's caveat (if any). Tri-state.

        Returns True if the row has no caveat or its caveat evaluates True;
        False if the caveat evaluates False (row treated as absent);
        None if required parameters are missing (caller surfaces CONDITIONAL).
        Adds missing param names to the caller's `missing` set.
        """
        caveat_name = getattr(row, "caveat_name", "") or ""
        if not caveat_name:
            return True
        caveat = self.schema().get_caveat(caveat_name)
        if caveat is None:
            # Schema doesn't know about this caveat — fail closed.
            return False
        from ..caveats import evaluate as eval_caveat

        static_ctx = getattr(row, "caveat_context", None) or {}
        verdict, miss = eval_caveat(caveat, static_ctx, context)
        if verdict is None:
            missing.update(miss)
            return None
        return verdict

    def _resources_for_expr(
        self,
        expr: PermExpr,
        definition: Definition,
        subject: SubjectRef,
        depth: int,
        cache: dict[tuple[str, str], set[str] | None],
        context: dict | None = None,
    ) -> set[str]:
        if depth > app_settings.REBAC_DEPTH_LIMIT:
            raise PermissionDepthExceeded(f"Depth limit {app_settings.REBAC_DEPTH_LIMIT} exceeded")
        if isinstance(expr, PermNil):
            return set()
        if isinstance(expr, PermRef):
            if expr.name in BUILTIN_ACTOR_TYPES:
                # `accessible()` has no model table from which to enumerate a
                # schema-level grant. QuerySet integration can treat a matching
                # built-in actor term as "no row-level narrowing"; direct
                # backend enumeration remains relationship-row based.
                return set()
            relation = _find_relation(definition, expr.name)
            if relation is not None:
                return self._resources_via_relation(
                    resource_type=definition.resource_type,
                    relation=expr.name,
                    subject=subject,
                    depth=depth,
                    cache=cache,
                    context=context,
                )
            sub_perm = next((p for p in definition.permissions if p.name == expr.name), None)
            if sub_perm is not None:
                return self._resources_for_expr(
                    sub_perm.expression, definition, subject, depth, cache, context
                )
            return set()
        if isinstance(expr, PermArrow):
            from ..models import active_relationship_model

            RelationshipModel = active_relationship_model()

            via_rel = _find_relation(definition, expr.via)
            if via_rel is None:
                return set()
            results: set[str] = set()
            target_types = sorted({s.type for s in via_rel.allowed_subjects})
            for target_type in target_types:
                target_def = self.schema().get_definition(target_type)
                if target_def is None:
                    continue
                target_resource_ids = self._compute_accessible_for(
                    target_type, expr.target, target_def, subject, depth + 1, cache, context
                )
                if not target_resource_ids:
                    continue
                rows = self._apply_freshness(
                    RelationshipModel.objects.filter(
                        resource_type=definition.resource_type,
                        relation=expr.via,
                        subject_type=target_type,
                        subject_id__in=list(target_resource_ids),
                    )
                )
                for r in _filter_active(rows):
                    # Hop-row caveat must evaluate True (silent on conditional).
                    sink: set[str] = set()
                    if self._evaluate_row_caveat(r, context, sink) is True:
                        results.add(r.resource_id)
            return results
        if isinstance(expr, PermBinOp):
            left = self._resources_for_expr(expr.left, definition, subject, depth, cache, context)
            right = self._resources_for_expr(expr.right, definition, subject, depth, cache, context)
            if expr.op == "+":
                return left | right
            if expr.op == "&":
                return left & right
            if expr.op == "-":
                return left - right
            raise ValueError(f"unknown operator: {expr.op}")
        raise TypeError(f"unknown PermExpr: {expr!r}")

    def _compute_accessible_for(
        self,
        resource_type: str,
        action: str,
        definition: Definition,
        subject: SubjectRef,
        depth: int,
        cache: dict[tuple[str, str], set[str] | None],
        context: dict | None = None,
    ) -> set[str]:
        """Memoised entry into `_resources_for_expr` keyed by (type, action).

        Self-referential schemas (folder.parent -> folder.read) terminate via
        a fix-point: while one (type, action) walk is in flight, recursive
        re-entrants see an empty seed and return; once the outer call settles
        the cache holds the closed set.
        """
        key = (resource_type, action)
        if key in cache:
            cached = cache[key]
            return cached if cached is not None else set()
        # Sentinel value while computing — re-entrants get empty.
        cache[key] = None
        target_perm = next((p for p in definition.permissions if p.name == action), None)
        if target_perm is None:
            result = self._resources_via_relation(
                resource_type=resource_type,
                relation=action,
                subject=subject,
                depth=depth,
                cache=cache,
                context=context,
            )
            cache[key] = result
            return result
        # Fix-point: re-evaluate until the set stops growing. For most schemas
        # the first iteration is final; recursive ones (folder.parent->folder.read)
        # converge in O(graph diameter) steps.
        prev: set[str] = set()
        for _ in range(app_settings.REBAC_DEPTH_LIMIT + 1):
            cache[key] = prev
            current = self._resources_for_expr(
                target_perm.expression, definition, subject, depth, cache, context
            )
            if current == prev:
                break
            prev = current
        cache[key] = prev
        return prev

    def _resources_via_relation(
        self,
        resource_type: str,
        relation: str,
        subject: SubjectRef,
        depth: int,
        cache: dict[tuple[str, str], set[str] | None] | None = None,
        context: dict | None = None,
    ) -> set[str]:
        if depth > app_settings.REBAC_DEPTH_LIMIT:
            raise PermissionDepthExceeded(f"Depth limit {app_settings.REBAC_DEPTH_LIMIT} exceeded")
        from ..models import active_relationship_model

        RelationshipModel = active_relationship_model()

        result: set[str] = set()
        # Local sink: caveat-conditional rows feeding accessible() are
        # excluded silently, so the missing-param names go nowhere.
        sink: set[str] = set()

        # Direct rows
        direct = self._apply_freshness(
            RelationshipModel.objects.filter(
                resource_type=resource_type,
                relation=relation,
                subject_type=subject.subject_type,
                subject_id=subject.subject_id,
                optional_subject_relation=subject.optional_relation,
            )
        )
        for r in _filter_active(direct):
            if self._evaluate_row_caveat(r, context, sink) is True:
                result.add(r.resource_id)

        # Wildcard rows
        if not subject.optional_relation:
            wildcard = self._apply_freshness(
                RelationshipModel.objects.filter(
                    resource_type=resource_type,
                    relation=relation,
                    subject_type=subject.subject_type,
                    subject_id="*",
                )
            )
            for r in _filter_active(wildcard):
                if self._evaluate_row_caveat(r, context, sink) is True:
                    result.add(r.resource_id)

        # Subject-set rows: e.g. resources granted to `auth/group:X#member`
        # require the subject to actually be a member of group X.
        subject_set_rows = self._apply_freshness(
            RelationshipModel.objects.filter(
                resource_type=resource_type, relation=relation
            ).exclude(optional_subject_relation="")
        )
        for row in subject_set_rows:
            if not _is_active(row):
                continue
            hop = self._evaluate_row_caveat(row, context, sink)
            if hop is not True:
                continue
            inner = self._has_direct_relation(
                resource_type=row.subject_type,
                resource_id=row.subject_id,
                relation=row.optional_subject_relation,
                subject=subject,
                depth=depth + 1,
                context=context,
                missing=sink,
            )
            if inner is True:
                result.add(row.resource_id)
        return result

    # ---------- helpers ----------

    def _next_xid(self) -> int:
        self._xid_counter += 1
        return int(time.time_ns()) + self._xid_counter

    def _zookie(self) -> Zookie:
        return Zookie(self.kind, str(self._next_xid()))


# ---------- Module-level helpers ----------


def mark_db_loaded_schemas_stale() -> None:
    """Invalidate all live LocalBackend instances backed by Schema* DB rows."""
    with _backend_registry_lock:
        backends = list(_db_loaded_backends)
    for backend in backends:
        backend.mark_schema_stale()


def _find_relation(definition: Definition, name: str) -> Relation | None:
    for r in definition.relations:
        if r.name == name:
            return r
    return None


def _builtin_actor_matches(name: str, subject: SubjectRef) -> bool:
    """Match the bare schema keywords ``anonymous`` / ``authenticated``.

    ``anonymous`` matches exactly the canonical anonymous SubjectRef
    (``REBAC_ANONYMOUS_TYPE:*``, default ``auth/anonymous:*``).
    ``authenticated`` matches any other subject with a real id — every
    subject that isn't the anonymous singleton.

    Delegates the anonymous shape to :func:`actors.is_anonymous_actor` so the
    two surfaces (actor layer and engine) cannot desynchronize if a future
    change tightens what "anonymous" means.
    """
    anonymous = is_anonymous_actor(subject)
    if name == "anonymous":
        return anonymous
    if name == "authenticated":
        # Anything that isn't the anonymous singleton — including
        # subject-set rows (``auth/group:eng#member``) and other wildcard
        # subjects — counts as authenticated. The id check guards against
        # degenerate empty-string ids.
        return not anonymous and subject.subject_id != ""
    return False


def _collect_direct_relations(expr: PermExpr) -> set[str]:
    """Walk a permission expression and collect bottom-most relation names.

    Used by `lookup_subjects` to know which relation rows to inspect.
    """
    if isinstance(expr, PermNil):
        return set()
    if isinstance(expr, PermRef):
        return {expr.name}
    if isinstance(expr, PermArrow):
        return set()  # arrows route through other definitions; reverse-lookup is deferred
    if isinstance(expr, PermBinOp):
        return _collect_direct_relations(expr.left) | _collect_direct_relations(expr.right)
    return set()


# ---------- Tri-state operators ----------
#
# `None` means "conditional on caveat params not yet supplied" — short-circuit
# where possible (True absorbs OR; False absorbs AND), otherwise propagate
# CONDITIONAL up to the caller. Mirrors SpiceDB's caveat semantics.


def _or(left: bool | None, right: bool | None) -> bool | None:
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


def _and(left: bool | None, right: bool | None) -> bool | None:
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def _minus(left: bool | None, right: bool | None) -> bool | None:
    # `a - b` ≡ `a AND NOT b`. None on the left absorbs through AND when the
    # right side denies; otherwise we don't know the answer.
    if left is False:
        return False
    if left is None and right is True:
        return False  # whatever 'left' resolves to, '- True' kills it.
    if left is None:
        return None
    # left is True
    if right is True:
        return False
    if right is False:
        return True
    return None


def _filter_active(qs: object) -> object:
    """Exclude expired rows. Postgres-friendly via a parameterised filter."""
    from django.db.models import Q
    from django.utils import timezone

    return qs.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))  # type: ignore[attr-defined]


def _is_active(row: object) -> bool:
    expires_at = getattr(row, "expires_at", None)
    if expires_at is None:
        return True
    from django.utils import timezone

    return expires_at > timezone.now()
