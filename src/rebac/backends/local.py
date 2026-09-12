"""LocalBackend — pure-Django REBAC evaluation.

Evaluates permission expressions by walking the in-memory schema tree against
rows in the `Relationship` table. Implementation strategy:

  - For `accessible()`: BFS over the relation graph, materialising candidate
    resource_ids per visited relation. Final result is the union/intersection
    of contributors per the expression operators.
  - For `check_access()`: same walk, but bounded by the specific resource_id.
  - Recursion depth bounded by `REBAC_DEPTH_LIMIT`.

Read querysets use lazy ORM predicates for acyclic, non-caveated permissions;
unsupported expressions retain the conservative Python evaluator. Explicit
`accessible()` calls still enumerate resource IDs. Both paths use the effective
schema and run through Django on supported databases.

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
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from threading import Lock
from typing import Any
from weakref import WeakSet

from django.db import models
from django.db.models import QuerySet

from ..conf import app_settings
from ..errors import PermissionDepthExceeded, SchemaError
from ..field_backing import (
    ResolvedConstBacking,
    ResolvedFieldBacking,
    resolve_const_backing,
    resolve_field_backing,
)
from ..resources import model_resource_type
from ..schema.ast import (
    BUILTIN_ACTOR_TYPES,
    AllowedSubject,
    ConstBinding,
    Definition,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermExpr,
    PermNil,
    PermRef,
    Relation,
    Schema,
)
from ..schema.cache import SchemaScope, SchemaSnapshot
from ..schema.walker import (
    WalkContext,
)
from ..schema.walker import (
    builtin_actor_matches as _builtin_actor_matches,
)
from ..schema.walker import (
    eval_expr as _walk_eval_expr,
)
from ..schema.walker import (
    find_relation as _find_relation,
)
from ..schema.walker import (
    relationship_row_allowed_by_relation as _row_allowed_by_relation,
)
from ..schema.walker import (
    subject_allowed_by_relation as _subject_allowed_by_relation,
)
from ..schema.walker import (
    tri_and as _and,
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
from .local_query import LocalQueryScope, UnsupportedScope

_backend_registry_lock = Lock()
_db_loaded_backends: WeakSet[LocalBackend] = WeakSet()
_relationship_generation = 0
_schema_operation_scope: ContextVar[SchemaScope | None] = ContextVar(
    "rebac_schema_operation", default=None
)


def _schema_operation[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Reuse one schema within a public backend operation and nested reads."""

    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        token = None
        if _schema_operation_scope.get() is None:
            token = _schema_operation_scope.set(SchemaScope())
        try:
            return method(*args, **kwargs)
        finally:
            if token is not None:
                scope = _schema_operation_scope.get()
                if scope is not None:
                    scope.clear()
                _schema_operation_scope.reset(token)

    return wrapped


class LocalBackend(Backend):
    """Recursive-CTE-style evaluator implemented as a bounded graph walk."""

    kind = "local"

    @staticmethod
    def _validate_consistency(consistency: Consistency | None, at_zookie: Zookie | None) -> None:
        """Validate read options without filtering away newer relationship rows.

        Local reads use the current state visible to the Django connection.
        Row write timestamps cannot reconstruct historical updates/deletes.
        """
        if consistency is Consistency.AT_EXACT_SNAPSHOT:
            raise ValueError("LocalBackend does not support exact historical snapshots")
        if at_zookie is None:
            return
        if at_zookie.backend != "local":
            raise ValueError(
                f"LocalBackend cannot consume a Zookie from backend "
                f"{at_zookie.backend!r}. Drain or translate the token at "
                f"the boundary where backends switched."
            )
        try:
            int(at_zookie.token)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"LocalBackend Zookie token must be a numeric xid; got {at_zookie.token!r}"
            ) from exc

    def __init__(self) -> None:
        self._schema_lock = Lock()
        self._schema: Schema | None = None
        self._schema_is_manual = False
        self._schema_generation = 0
        self._schema_invalidation_generation = 0
        # Counter used as a stable monotonic xid on backends (e.g. SQLite test
        # mode) without `txid_current()`.
        self._xid_counter = 0
        with _backend_registry_lock:
            _db_loaded_backends.add(self)

    # ---------- Schema management ----------

    def set_schema(self, schema: Schema) -> None:
        """Install the in-memory schema. Called by the sync command."""
        from ..schema.parser import validate_schema

        field_backing_errors = [
            error
            for error in validate_schema(schema)
            if "field-backed relation" in error or "field backing" in error
        ]
        if field_backing_errors:
            raise SchemaError("; ".join(field_backing_errors))
        with self._schema_lock:
            self._schema = schema
            self._schema_is_manual = True
            self._schema_generation += 1

    def schema(self) -> Schema:
        return self._schema_snapshot().schema

    def _schema_snapshot(self) -> SchemaSnapshot:
        from django.db import DEFAULT_DB_ALIAS, connections
        from django.utils import timezone

        from ..evaluator import current_evaluator

        evaluator = current_evaluator()
        with self._schema_lock:
            if self._schema_is_manual and self._schema is not None:
                return SchemaSnapshot(
                    self._schema,
                    None,
                    self._schema_generation,
                    self._schema_invalidation_generation,
                )
            connection = connections[DEFAULT_DB_ALIAS]
            # Manual transaction management has no on_commit lifecycle to
            # observe. Preserve operation-only snapshots in that mode.
            scoped = connection.get_autocommit() or (
                connection.in_atomic_block and connection.commit_on_exit
            )
            scope = (
                evaluator._schema_scope
                if evaluator is not None and scoped
                else _schema_operation_scope.get()
            )
            snapshots = scope.snapshots(connection) if scope is not None else None
            snapshot = snapshots.get(self) if snapshots is not None else None
            if (
                snapshot is not None
                and snapshot.invalidation_generation == self._schema_invalidation_generation
                and (snapshot.expires_at is None or snapshot.expires_at > timezone.now())
            ):
                return snapshot
            schema, expires_at = self._load_schema_from_db()
            self._schema = schema
            self._schema_is_manual = False
            self._schema_generation += 1
            snapshot = SchemaSnapshot(
                schema, expires_at, self._schema_generation, self._schema_invalidation_generation
            )
            if snapshots is not None:
                snapshots[self] = snapshot
            return snapshot

    def _cache_generation(self) -> tuple[int, int] | None:
        """Return a decision generation, or bypass time/transaction-sensitive caching."""
        from django.db import connection

        if connection.in_atomic_block or (
            connection.connection is not None and not connection.get_autocommit()
        ):
            return None
        snapshot = self._schema_snapshot()
        schema = snapshot.schema
        if any(relation.with_expiration for d in schema.definitions for relation in d.relations):
            # A relationship deadline can pass with no write or schema change.
            # Dependency-specific expiry tracking is not implemented yet.
            return None
        return snapshot.generation, _relationship_generation

    def mark_schema_stale(self) -> None:
        """Drop a DB-loaded schema cache after Schema* row changes."""
        with self._schema_lock:
            if not self._schema_is_manual:
                self._schema = None
                self._schema_invalidation_generation += 1

    def _load_schema_from_db(self) -> tuple[Schema, datetime | None]:
        from django.db.models import Prefetch, Q
        from django.utils import timezone

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
            ConstBinding,
            Definition,
            FieldBinding,
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
                backing: FieldBinding | ConstBinding | None = None
                if r.backing:
                    kind = str(r.backing.get("kind", "fk"))
                    if kind == "const":
                        backing = ConstBinding(target_id=str(r.backing["target_id"]))
                    elif kind == "fk":
                        backing = FieldBinding(attname=str(r.backing["attname"]), kind="fk")
                    else:
                        raise SchemaError(
                            f"{d.resource_type}#{r.name}: unknown relation backing kind {kind!r}"
                        )
                relations.append(Relation(r.name, allowed, r.with_expiration, backing))
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
        overrides = list(
            SchemaOverride.objects.filter(
                Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())
            )
            .select_related("target_ct")
            .order_by("kind", "created_at", "pk")
        )
        expires_at = min(
            (row.expires_at for row in overrides if row.expires_at is not None),
            default=None,
        )
        return compose(baseline, overrides), expires_at

    # ---------- Public API ----------

    @_schema_operation
    def check_access(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> CheckResult:
        self._validate_consistency(consistency, at_zookie)
        return self._check_access(subject, action, resource, context)

    def _check_access(
        self,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: dict[str, Any] | None,
    ) -> CheckResult:
        # Empty resource_id → model-level check (any row of this type the subject
        # has the action on). First honour resource-independent grants — built-in
        # actor terms (``authenticated`` / ``anonymous``) and const arrows — by
        # evaluating the permission against an empty row: a create/list permission
        # like ``create = authenticated`` has no accessible row yet but still grants
        # (this is the gate the pre_save create signal relies on). Otherwise fall
        # back to "is the accessible() set non-empty?".
        if not resource.resource_id:
            definition = self.schema().get_definition(resource.resource_type)
            permission = (
                self.schema().get_permission(resource.resource_type, action)
                if definition is not None
                else None
            )
            if (
                definition is not None
                and permission is not None
                and self._eval_permission(
                    expr=permission.expression,
                    definition=definition,
                    resource_id="",
                    subject=subject,
                    depth=0,
                    context=context,
                    missing=set(),
                )
            ):
                return CheckResult.has()
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
            if _find_relation(definition, action) is None:
                return CheckResult.no(reason=f"unknown action: {resource.resource_type}#{action}")
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

    @_schema_operation
    def queryset_filter(
        self,
        *,
        model: type[models.Model],
        subject: SubjectRef,
        action: str,
        using: str,
    ) -> models.Q | None:
        resource_type = model_resource_type(model)
        if resource_type is None:
            return None
        try:
            return LocalQueryScope(self, subject, using).predicate(model, action, resource_type)
        except UnsupportedScope:
            return None

    @_schema_operation
    def accessible(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource_type: str,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> Iterable[str]:
        self._validate_consistency(consistency, at_zookie)
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
        context: dict[str, Any] | None = None,
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
            if _find_relation(definition, action) is None:
                return []
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

    @_schema_operation
    def grants_all(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource_type: str,
        context: dict[str, Any] | None = None,
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

    @_schema_operation
    def lookup_subjects(
        self,
        *,
        resource: ObjectRef,
        action: str,
        subject_type: str,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> Iterable[SubjectRef]:
        # Minimal forward lookup — direct relation rows only. Walking through
        # subject sets / arrows for reverse lookup is deferred to v0.2.
        from ..models import active_relationship_model

        self._validate_consistency(consistency, at_zookie)
        RelationshipModel = active_relationship_model()

        permission = self.schema().get_permission(resource.resource_type, action)
        definition = self.schema().get_definition(resource.resource_type)
        if definition is None:
            return []
        relation_by_name = {r.name: r for r in definition.relations}
        relation_names = []
        if permission is None:
            if action in relation_by_name:
                relation_names = [action]
        else:
            relation_names = sorted(
                _collect_direct_relations(permission.expression) & relation_by_name.keys()
            )

        if not relation_names:
            return []

        synthetic_subjects: list[SubjectRef] = []
        stored_relation_names: list[str] = []
        for relation_name in relation_names:
            relation_def = relation_by_name[relation_name]
            field_backing = self._resolve_declared_field_backing(definition, relation_def)
            if field_backing is not None:
                if subject_type != field_backing.target_resource_type:
                    continue
                values = field_backing.source_model._base_manager.filter(
                    **field_backing.source_filter(resource.resource_id)
                ).values_list(field_backing.target_values_path(), flat=True)
                for value in values:
                    if value is not None:
                        synthetic_subjects.append(
                            SubjectRef.of(field_backing.target_resource_type, str(value))
                        )
                continue
            const_backing = self._resolve_declared_const_backing(definition, relation_def)
            if const_backing is not None:
                # One fixed subject holds the relation on every resource of this
                # type; surface it when the caller asked for that subject type.
                if subject_type == const_backing.target_resource_type:
                    synthetic_subjects.append(
                        SubjectRef.of(const_backing.target_resource_type, const_backing.target_id)
                    )
                continue
            stored_relation_names.append(relation_name)

        candidates = set(synthetic_subjects)
        if stored_relation_names:
            rows = _filter_active(
                RelationshipModel.objects.filter(
                    resource_type=resource.resource_type,
                    resource_id=resource.resource_id,
                    relation__in=stored_relation_names,
                    subject_type=subject_type,
                )
            )
            candidates.update(
                SubjectRef.of(r.subject_type, r.subject_id, r.optional_subject_relation)
                for r in rows
                if _row_allowed_by_relation(relation_by_name[r.relation], r)
            )
        # Relation rows are candidate sources, never proof of the requested
        # permission. Apply intersections, exclusions and caveats before
        # returning the subjects, including candidates from synthetic backing.
        return [
            candidate
            for candidate in sorted(candidates, key=str)
            if self._check_access(candidate, action, resource, context).allowed
        ]

    @_schema_operation
    def write_relationships(self, writes: Iterable[RelationshipTuple]) -> Zookie:
        from django.db import transaction

        from ..models import active_relationship_model

        RelationshipModel = active_relationship_model()

        rows = list(writes)
        for tup in rows:
            self._validate_relationship_tuple(tup)
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
        # so the token witnesses every row written by this batch. Newer
        # relationship rows remain visible to reads carrying this token.
        # An empty batch returns a token for the current local clock.
        if max_xid == 0:
            return self._zookie()
        _advance_relationship_generation()
        return Zookie(self.kind, str(max_xid))

    @_schema_operation
    def delete_relationships(self, filter_: RelationshipFilter) -> Zookie:
        from django.db import transaction

        from ..models import active_relationship_model

        RelationshipModel = active_relationship_model()
        backed = self._field_backed_relation_for_filter(filter_.resource_type, filter_.relation)
        if backed is not None:
            resource_type, relation = backed
            raise self._backed_write_error(resource_type, relation)

        with transaction.atomic():
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
            if filter_.caveat_name:
                qs = qs.filter(caveat_name=filter_.caveat_name)
            qs.delete()
        _advance_relationship_generation()
        return self._zookie()

    @_schema_operation
    def delete_relationship(self, tuple_: RelationshipTuple) -> Zookie:
        # Local-only convenience verb: exact-match delete for one tuple shape
        # (treats empty optional_subject_relation / caveat_name as exact
        # values, where ``delete_relationships`` treats them as wildcards).
        # Has no direct SpiceDB equivalent; SpiceDB expresses the same intent
        # via ``WriteRelationships`` with ``OPERATION_DELETE``. Planned to
        # lower through that path in 0.4 once the ABC takes operation-shaped
        # updates — see ARCHITECTURE.md.
        from django.db import transaction

        from ..models import active_relationship_model

        RelationshipModel = active_relationship_model()
        definition = self.schema().get_definition(tuple_.resource.resource_type)
        if definition is not None:
            relation = _find_relation(definition, tuple_.relation)
            if relation is not None and relation.backing is not None:
                raise self._backed_write_error(tuple_.resource.resource_type, relation)
        with transaction.atomic():
            RelationshipModel.objects.filter(
                resource_type=tuple_.resource.resource_type,
                resource_id=tuple_.resource.resource_id,
                relation=tuple_.relation,
                subject_type=tuple_.subject.subject_type,
                subject_id=tuple_.subject.subject_id,
                optional_subject_relation=tuple_.subject.optional_relation,
                caveat_name=tuple_.caveat_name,
            ).delete()
        _advance_relationship_generation()
        return self._zookie()

    # ---------- Internal evaluation ----------

    def _eval_permission(
        self,
        expr: PermExpr,
        definition: Definition,
        resource_id: str,
        subject: SubjectRef,
        depth: int,
        context: dict[str, Any] | None = None,
        missing: set[str] | None = None,
    ) -> bool | None:
        """Tri-state permission evaluation.

        Returns:
            True  — permission unconditionally allowed.
            False — permission unconditionally denied.
            None  — at least one path is conditional on caveat params not yet
                    supplied; the union of missing names is added to
                    `missing` (caller-owned set).

        Implementation delegates the AST walk to :func:`rebac.schema.walker.eval_expr`;
        :meth:`_walk_resolve_relation` and :meth:`_walk_resolve_arrow` supply
        the DB-backed direct-relation and arrow-hop resolution.
        """
        if missing is None:
            missing = set()
        ctx = WalkContext(
            schema=self.schema(),
            subject=subject,
            context=context,
            missing=missing,
            depth_limit=app_settings.REBAC_DEPTH_LIMIT,
            resolve_relation=self._walk_resolve_relation,
            resolve_arrow=self._walk_resolve_arrow,
        )
        return _walk_eval_expr(
            expr,
            definition=definition,
            resource_id=resource_id,
            depth=depth,
            ctx=ctx,
        )

    def _walk_resolve_relation(
        self,
        ctx: WalkContext,
        definition: Definition,
        resource_id: str,
        relation: str,
        depth: int,
    ) -> bool | None:
        return self._has_direct_relation(
            resource_type=definition.resource_type,
            resource_id=resource_id,
            relation=relation,
            subject=ctx.subject,
            depth=depth,
            context=ctx.context,
            missing=ctx.missing,
        )

    def _walk_resolve_arrow(
        self,
        ctx: WalkContext,
        definition: Definition,
        resource_id: str,
        via: str,
        target: str,
        depth: int,
    ) -> bool | None:
        from ..models import active_relationship_model

        via_relation = _find_relation(definition, via)
        if via_relation is None:
            return False

        field_backing = self._resolve_declared_field_backing(definition, via_relation)
        if field_backing is not None:
            return self._walk_field_backed_arrow(
                ctx=ctx,
                resource_id=resource_id,
                field_backing=field_backing,
                target=target,
                depth=depth,
            )

        const_backing = self._resolve_declared_const_backing(definition, via_relation)
        if const_backing is not None:
            return self._walk_const_arrow(
                ctx=ctx,
                const_backing=const_backing,
                target=target,
                depth=depth,
            )

        RelationshipModel = active_relationship_model()
        targets = RelationshipModel.objects.filter(
            resource_type=definition.resource_type,
            resource_id=resource_id,
            relation=via,
        )
        saw_conditional = False
        for row in _filter_active(targets):
            if not _row_allowed_by_relation(via_relation, row):
                continue
            # The hop row itself may carry a caveat — evaluate it before
            # walking through to the target type.
            hop = self._evaluate_row_caveat(row, ctx.context, ctx.missing)
            if hop is False:
                continue
            target_def = ctx.schema.get_definition(row.subject_type)
            if target_def is None:
                continue
            inner = self._eval_permission_on(
                permission_name=target,
                definition=target_def,
                resource_id=row.subject_id,
                subject=ctx.subject,
                depth=depth + 1,
                context=ctx.context,
                missing=ctx.missing,
            )
            combined = _and(hop, inner)
            if combined is True:
                return True
            if combined is None:
                saw_conditional = True
        if saw_conditional:
            return None
        return False

    def _walk_field_backed_arrow(
        self,
        ctx: WalkContext,
        resource_id: str,
        field_backing: ResolvedFieldBacking,
        target: str,
        depth: int,
    ) -> bool | None:
        qs = field_backing.source_model._base_manager.filter(
            **field_backing.source_filter(resource_id)
        )
        target_values = list(qs.values_list(field_backing.target_values_path(), flat=True))
        saw_conditional = False
        for target_id in target_values:
            if target_id is None:
                continue
            target_def = ctx.schema.get_definition(field_backing.target_resource_type)
            if target_def is None:
                continue
            inner = self._eval_permission_on(
                permission_name=target,
                definition=target_def,
                resource_id=str(target_id),
                subject=ctx.subject,
                depth=depth + 1,
                context=ctx.context,
                missing=ctx.missing,
            )
            if inner is True:
                return True
            if inner is None:
                saw_conditional = True
        if saw_conditional:
            return None
        return False

    def _walk_const_arrow(
        self,
        ctx: WalkContext,
        const_backing: ResolvedConstBacking,
        target: str,
        depth: int,
    ) -> bool | None:
        # The arrow target object is fixed by the schema, so there is no row to
        # read: evaluate `target` on the constant object directly. The same
        # `const:default` is shared by every source object — that is the point.
        target_def = ctx.schema.get_definition(const_backing.target_resource_type)
        if target_def is None:
            return False
        return self._eval_permission_on(
            permission_name=target,
            definition=target_def,
            resource_id=const_backing.target_id,
            subject=ctx.subject,
            depth=depth + 1,
            context=ctx.context,
            missing=ctx.missing,
        )

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
            via_relation = _find_relation(definition, expr.via)
            if via_relation is None:
                return False
            const_backing = resolve_const_backing(definition, via_relation)
            if const_backing is None:
                # Field-backed / stored arrows resolve to a per-row target, so
                # they cannot grant the whole type uniformly — defer to the
                # per-row enumeration in `accessible()`.
                return False
            # A const arrow points every row at the same fixed object, so the
            # subject grants the entire type iff it holds `target` on that one
            # object. One check — no per-row enumeration, no `id__in` filter
            # (which would otherwise list the whole table). Conditional/None
            # is treated conservatively as "not a blanket grant".
            target_def = self.schema().get_definition(const_backing.target_resource_type)
            if target_def is None:
                return False
            return (
                self._eval_permission_on(
                    permission_name=expr.target,
                    definition=target_def,
                    resource_id=const_backing.target_id,
                    subject=subject,
                    depth=0,
                )
                is True
            )
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
        context: dict[str, Any] | None = None,
        missing: set[str] | None = None,
    ) -> bool | None:
        permission = next((p for p in definition.permissions if p.name == permission_name), None)
        if permission is None:
            # Treat as direct relation lookup.
            if _find_relation(definition, permission_name) is None:
                return False
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
        context: dict[str, Any] | None = None,
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

        definition = self.schema().get_definition(resource_type)
        if definition is None:
            return False
        relation_def = _find_relation(definition, relation)
        if relation_def is None:
            return False

        field_backing = self._resolve_declared_field_backing(definition, relation_def)
        if field_backing is not None:
            if not _subject_allowed_by_relation(relation_def, subject):
                return False
            return field_backing.source_model._base_manager.filter(
                **field_backing.source_filter(resource_id),
                **field_backing.target_filter(subject),
            ).exists()

        const_backing = self._resolve_declared_const_backing(definition, relation_def)
        if const_backing is not None:
            # Every row behaves as if it held `#<relation> @ <const target>`, so
            # the relation is held only by that fixed subject. No tuple, no query.
            if not _subject_allowed_by_relation(relation_def, subject):
                return False
            return subject == SubjectRef.of(
                const_backing.target_resource_type, const_backing.target_id
            )

        RelationshipModel = active_relationship_model()

        rows = RelationshipModel.objects.filter(
            resource_type=resource_type,
            resource_id=resource_id,
            relation=relation,
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
            if not _row_allowed_by_relation(relation_def, row):
                continue
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
                if not _row_allowed_by_relation(relation_def, row):
                    continue
                verdict = self._evaluate_row_caveat(row, context, missing)
                if verdict is True:
                    return True
                if verdict is None:
                    saw_conditional = True

        # Subject-set rows: e.g. `viewer @ auth/group:eng#member`. Walk the
        # group's `member` relation and see if subject is a member.
        for row in rows.exclude(optional_subject_relation=""):
            if not _row_allowed_by_relation(relation_def, row):
                continue
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
        context: dict[str, Any] | None,
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

    @staticmethod
    def _maybe_using(manager: Any, using: str | None) -> Any:
        """Route a manager/queryset to ``using`` when a DB alias is pinned.

        ``None`` keeps the model's routed/default database — the behaviour of
        the ``accessible()`` / ``check_access()`` entry points, which pass no
        alias. The lazy queryset compiler passes the queryset's own alias so
        tuple-grant resolution reads relationship rows from the same database
        its EXISTS subqueries join against (see ``local_query.ConvertedRelationIds``).

        Note: this consistency covers the resolution helpers only. Sub-branches
        that dispatch into the tri-state evaluator — subject-set membership
        (``_has_direct_relation``), the negative arm of an exclusion and
        const-arrow targets (``_eval_permission`` / ``_eval_permission_on``) —
        still resolve against the default database; see
        ``docs/ARCHITECTURE.md`` (multi-database resolution).
        """
        return manager if using is None else manager.using(using)

    def _resources_for_expr(
        self,
        expr: PermExpr,
        definition: Definition,
        subject: SubjectRef,
        depth: int,
        cache: dict[tuple[str, str], set[str] | None],
        context: dict[str, Any] | None = None,
        seen: frozenset[str] = frozenset(),
        using: str | None = None,
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
                    using=using,
                )
            sub_perm = next((p for p in definition.permissions if p.name == expr.name), None)
            if sub_perm is not None and expr.name not in seen:
                return self._resources_for_expr(
                    sub_perm.expression,
                    definition,
                    subject,
                    depth,
                    cache,
                    context,
                    seen | {expr.name},
                    using,
                )
            return set()
        if isinstance(expr, PermArrow):
            from ..models import active_relationship_model

            RelationshipModel = active_relationship_model()

            via_rel = _find_relation(definition, expr.via)
            if via_rel is None:
                return set()
            field_backing = self._resolve_declared_field_backing(definition, via_rel)
            if field_backing is not None:
                return self._resources_via_field_backed_arrow(
                    field_backing=field_backing,
                    target=expr.target,
                    subject=subject,
                    depth=depth,
                    cache=cache,
                    context=context,
                    using=using,
                )
            const_backing = self._resolve_declared_const_backing(definition, via_rel)
            if const_backing is not None:
                return self._resources_via_const_arrow(
                    const_backing=const_backing,
                    target=expr.target,
                    subject=subject,
                    depth=depth,
                    context=context,
                    using=using,
                )
            results: set[str] = set()
            target_types = sorted({s.type for s in via_rel.allowed_subjects})
            for target_type in target_types:
                target_def = self.schema().get_definition(target_type)
                if target_def is None:
                    continue
                target_resource_ids = self._compute_accessible_for(
                    target_type, expr.target, target_def, subject, depth + 1, cache, context, using
                )
                if not target_resource_ids:
                    continue
                rows = self._maybe_using(RelationshipModel.objects, using).filter(
                    resource_type=definition.resource_type,
                    relation=expr.via,
                    subject_type=target_type,
                    subject_id__in=list(target_resource_ids),
                )
                for r in _filter_active(rows):
                    if not _row_allowed_by_relation(via_rel, r):
                        continue
                    # Hop-row caveat must evaluate True (silent on conditional).
                    sink: set[str] = set()
                    if self._evaluate_row_caveat(r, context, sink) is True:
                        results.add(r.resource_id)
            return results
        if isinstance(expr, PermBinOp):
            left = self._resources_for_expr(
                expr.left, definition, subject, depth, cache, context, seen, using
            )
            if expr.op == "-":
                # Enumerating the RHS drops conditional matches and cannot
                # enumerate built-in actor grants. Subtracting that incomplete
                # set would grant access. Recheck candidates with the shared
                # tri-state walker so only an unconditional exclusion result
                # survives, including exclusions nested under arrows/unions.
                return {
                    resource_id
                    for resource_id in left
                    if self._eval_permission(expr, definition, resource_id, subject, depth, context)
                    is True
                }
            right = self._resources_for_expr(
                expr.right, definition, subject, depth, cache, context, seen, using
            )
            if expr.op == "+":
                return left | right
            if expr.op == "&":
                return left & right
            raise ValueError(f"unknown operator: {expr.op}")
        raise TypeError(f"unknown PermExpr: {expr!r}")

    def _resources_via_field_backed_arrow(
        self,
        *,
        field_backing: ResolvedFieldBacking,
        target: str,
        subject: SubjectRef,
        depth: int,
        cache: dict[tuple[str, str], set[str] | None],
        context: dict[str, Any] | None,
        using: str | None = None,
    ) -> set[str]:
        target_def = self.schema().get_definition(field_backing.target_resource_type)
        if target_def is None:
            return set()
        target_resource_ids = self._compute_accessible_for(
            field_backing.target_resource_type,
            target,
            target_def,
            subject,
            depth + 1,
            cache,
            context,
            using,
        )
        if not target_resource_ids:
            return set()
        rows = self._maybe_using(field_backing.source_model._base_manager, using).filter(
            **field_backing.target_in_filter(target_resource_ids)
        )
        return {
            str(value) for value in rows.values_list(field_backing.source_values_path(), flat=True)
        }

    def _resources_via_const_arrow(
        self,
        *,
        const_backing: ResolvedConstBacking,
        target: str,
        subject: SubjectRef,
        depth: int,
        context: dict[str, Any] | None,
        using: str | None = None,
    ) -> set[str]:
        # The target object is fixed, so this is one check, not an enumeration:
        # either the subject holds `target` on `const:default` — in which case
        # every row of the source type is reachable — or none is. Returning the
        # whole id set is the intended "covers any <type>" semantics; it is the
        # same cost as any grant that lets a subject see the entire table.
        target_def = self.schema().get_definition(const_backing.target_resource_type)
        if target_def is None:
            return set()
        granted = self._eval_permission_on(
            permission_name=target,
            definition=target_def,
            resource_id=const_backing.target_id,
            subject=subject,
            depth=depth + 1,
            context=context,
            missing=None,
        )
        if granted is not True:
            return set()
        return {
            str(value)
            for value in self._maybe_using(
                const_backing.source_model._base_manager, using
            ).values_list(const_backing.source_values_path(), flat=True)
        }

    def _compute_accessible_for(
        self,
        resource_type: str,
        action: str,
        definition: Definition,
        subject: SubjectRef,
        depth: int,
        cache: dict[tuple[str, str], set[str] | None],
        context: dict[str, Any] | None = None,
        using: str | None = None,
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
                using=using,
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
                target_perm.expression, definition, subject, depth, cache, context, using=using
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
        context: dict[str, Any] | None = None,
        using: str | None = None,
    ) -> set[str]:
        if depth > app_settings.REBAC_DEPTH_LIMIT:
            raise PermissionDepthExceeded(f"Depth limit {app_settings.REBAC_DEPTH_LIMIT} exceeded")
        from ..models import active_relationship_model

        definition = self.schema().get_definition(resource_type)
        if definition is None:
            return set()
        relation_def = _find_relation(definition, relation)
        if relation_def is None:
            return set()

        field_backing = self._resolve_declared_field_backing(definition, relation_def)
        if field_backing is not None:
            if not _subject_allowed_by_relation(relation_def, subject):
                return set()
            rows = self._maybe_using(field_backing.source_model._base_manager, using).filter(
                **field_backing.target_filter(subject)
            )
            return {
                str(value)
                for value in rows.values_list(field_backing.source_values_path(), flat=True)
            }

        const_backing = self._resolve_declared_const_backing(definition, relation_def)
        if const_backing is not None:
            # The relation is held only by the fixed const target; when the
            # subject is that target, every row of the source type carries it.
            if not _subject_allowed_by_relation(relation_def, subject):
                return set()
            if subject != SubjectRef.of(
                const_backing.target_resource_type, const_backing.target_id
            ):
                return set()
            return {
                str(value)
                for value in self._maybe_using(
                    const_backing.source_model._base_manager, using
                ).values_list(const_backing.source_values_path(), flat=True)
            }

        RelationshipModel = active_relationship_model()

        result: set[str] = set()
        # Local sink: caveat-conditional rows feeding accessible() are
        # excluded silently, so the missing-param names go nowhere.
        sink: set[str] = set()

        # Direct rows
        direct = self._maybe_using(RelationshipModel.objects, using).filter(
            resource_type=resource_type,
            relation=relation,
            subject_type=subject.subject_type,
            subject_id=subject.subject_id,
            optional_subject_relation=subject.optional_relation,
        )
        if not _subject_allowed_by_relation(relation_def, subject):
            direct = direct.none()
        for r in _filter_active(direct):
            if not _row_allowed_by_relation(relation_def, r):
                continue
            if self._evaluate_row_caveat(r, context, sink) is True:
                result.add(r.resource_id)

        # Wildcard rows
        if not subject.optional_relation and _subject_allowed_by_relation(
            relation_def, SubjectRef.of(subject.subject_type, "*")
        ):
            wildcard = self._maybe_using(RelationshipModel.objects, using).filter(
                resource_type=resource_type,
                relation=relation,
                subject_type=subject.subject_type,
                subject_id="*",
            )
            for r in _filter_active(wildcard):
                if not _row_allowed_by_relation(relation_def, r):
                    continue
                if self._evaluate_row_caveat(r, context, sink) is True:
                    result.add(r.resource_id)

        # Subject-set rows: e.g. resources granted to `auth/group:X#member`
        # require the subject to actually be a member of group X.
        if not any(allowed.relation for allowed in relation_def.allowed_subjects):
            return result
        subject_set_rows = (
            self._maybe_using(RelationshipModel.objects, using)
            .filter(resource_type=resource_type, relation=relation)
            .exclude(optional_subject_relation="")
        )
        for row in subject_set_rows:
            if not _row_allowed_by_relation(relation_def, row):
                continue
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

    def _validate_relationship_tuple(self, tup: RelationshipTuple) -> None:
        definition = self.schema().get_definition(tup.resource.resource_type)
        if definition is None:
            raise ValueError(f"unknown resource type: {tup.resource.resource_type}")
        relation = _find_relation(definition, tup.relation)
        if relation is None:
            raise ValueError(f"unknown relation: {tup.resource.resource_type}#{tup.relation}")
        if relation.backing is not None:
            raise self._backed_write_error(tup.resource.resource_type, relation)
        if not _subject_allowed_by_relation(relation, tup.subject):
            raise ValueError(
                f"subject {tup.subject} is not allowed for "
                f"{tup.resource.resource_type}#{tup.relation}"
            )
        if not _subject_allowed_by_relation(relation, tup.subject, caveat_name=tup.caveat_name):
            raise ValueError(
                f"caveat {tup.caveat_name!r} is not allowed for subject {tup.subject} on "
                f"{tup.resource.resource_type}#{tup.relation}"
            )
        if tup.caveat_name and self.schema().get_caveat(tup.caveat_name) is None:
            raise ValueError(f"unknown caveat: {tup.caveat_name}")
        if tup.expires_at is not None and not relation.with_expiration:
            raise ValueError(
                f"expiration is not allowed for {tup.resource.resource_type}#{tup.relation}"
            )

    def _backed_write_error(self, resource_type: str, relation: Relation) -> SchemaError:
        if isinstance(relation.backing, ConstBinding):
            return SchemaError(
                f"relation `{relation.name}` on `{resource_type}` is const-backed "
                f"(rebac:const={relation.backing.target_id}); it is synthetic and holds no tuples"
            )
        model_hint = resource_type
        field_hint = (
            relation.backing.attname
            if isinstance(relation.backing, FieldBinding)
            else relation.name
        )
        definition = self.schema().get_definition(resource_type)
        if definition is not None:
            field_backing = resolve_field_backing(definition, relation)
            if field_backing is not None:
                model_hint = field_backing.source_model.__name__
                field_hint = field_backing.field.name
        return SchemaError(
            f"relation `{relation.name}` on `{resource_type}` is field-backed; "
            f"set `{model_hint}.{field_hint}` instead"
        )

    def _resolve_declared_field_backing(
        self,
        definition: Definition,
        relation: Relation,
    ) -> ResolvedFieldBacking | None:
        if not isinstance(relation.backing, FieldBinding):
            return None
        field_backing = resolve_field_backing(definition, relation)
        if field_backing is None:
            raise SchemaError(
                f"{definition.resource_type}#{relation.name}: "
                "field-backed relation could not be resolved; run `manage.py check --tag rebac`"
            )
        return field_backing

    def _resolve_declared_const_backing(
        self,
        definition: Definition,
        relation: Relation,
    ) -> ResolvedConstBacking | None:
        if not isinstance(relation.backing, ConstBinding):
            return None
        const_backing = resolve_const_backing(definition, relation)
        if const_backing is None:
            raise SchemaError(
                f"{definition.resource_type}#{relation.name}: "
                "const-backed relation could not be resolved; run `manage.py check --tag rebac`"
            )
        return const_backing

    def _field_backed_relation_for_filter(
        self,
        resource_type: str,
        relation_name: str,
    ) -> tuple[str, Relation] | None:
        if not relation_name:
            return None
        definitions: list[Definition] = []
        if resource_type:
            definition = self.schema().get_definition(resource_type)
            if definition is not None:
                definitions.append(definition)
        else:
            definitions.extend(self.schema().definitions)
        for definition in definitions:
            relation = _find_relation(definition, relation_name)
            if relation is not None and relation.backing is not None:
                return definition.resource_type, relation
        return None


# ---------- Module-level helpers ----------


def _advance_relationship_generation() -> None:
    """Invalidate decisions across backend instances and nested evaluator scopes."""
    global _relationship_generation
    with _backend_registry_lock:
        _relationship_generation += 1


def mark_db_loaded_schemas_stale() -> None:
    """Invalidate all live LocalBackend instances backed by Schema* DB rows."""
    with _backend_registry_lock:
        backends = list(_db_loaded_backends)
    for backend in backends:
        backend.mark_schema_stale()


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


def _filter_active(qs: QuerySet[Any]) -> QuerySet[Any]:
    """Exclude expired rows. Postgres-friendly via a parameterised filter."""
    from django.db.models import Q
    from django.utils import timezone

    return qs.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))


def _is_active(row: object) -> bool:
    expires_at = getattr(row, "expires_at", None)
    if expires_at is None:
        return True
    from django.utils import timezone

    return bool(expires_at > timezone.now())
