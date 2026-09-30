"""Frozen 0.22.1 scalar walker, captured before switching production reads.

Independent differential oracle. No index or enumeration shortcuts.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db.models import QuerySet

from rebac.backends.local import LocalBackend
from rebac.conf import app_settings
from rebac.errors import PermissionDepthExceeded, SchemaError
from rebac.field_backing import (
    ResolvedAttributeBacking,
    ResolvedConstBacking,
    ResolvedFieldBacking,
    resolve_attribute_backing,
    resolve_const_backing,
    resolve_field_backing,
)
from rebac.schema.ast import (
    AttributeBinding,
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
from rebac.schema.walker import WalkContext
from rebac.schema.walker import eval_expr as _walk_eval_expr
from rebac.schema.walker import find_relation as _find_relation
from rebac.schema.walker import relationship_row_allowed_by_relation as _row_allowed_by_relation
from rebac.schema.walker import subject_allowed_by_relation as _subject_allowed_by_relation
from rebac.schema.walker import tri_and as _and
from rebac.types import CheckResult, Consistency, ObjectRef, RelationshipTuple, SubjectRef, Zookie


class WalkerOracle(LocalBackend):
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
        from rebac.models import active_relationship_model

        del consistency, at_zookie
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
                values = field_backing.queryset(resource_id=resource.resource_id).values_list(
                    field_backing.target_values_path(), flat=True
                )
                for value in values:
                    if value is not None:
                        synthetic_subjects.append(
                            SubjectRef.of(field_backing.target_resource_type, str(value))
                        )
                continue
            attribute_backing = self._resolve_declared_attribute_backing(definition, relation_def)
            if attribute_backing is not None:
                if not attribute_backing.applies_to(resource.resource_id):
                    stored_relation_names.append(relation_name)
                    continue
                if subject_type != attribute_backing.target_resource_type:
                    continue
                synthetic_subjects.extend(
                    SubjectRef.of(attribute_backing.target_resource_type, str(value))
                    for value in attribute_backing.subject_ids(resource.resource_id)
                    if value is not None
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
                self._maybe_using(RelationshipModel.objects, None).filter(
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
            if self.check_access(
                subject=candidate, action=action, resource=resource, context=context
            ).allowed
        ]

    def check_access(self, *, subject, action, resource, context=None, **kwargs):
        definition = self.schema().get_definition(resource.resource_type)
        if definition is None:
            return CheckResult.no()
        missing = set()
        value = self._eval_permission_on(
            action,
            definition,
            resource.resource_id,
            subject,
            0,
            context,
            missing,
            kwargs.get("using"),
        )
        if not resource.resource_id:
            if value is True or any(
                self.check_access(
                    subject=subject,
                    action=action,
                    resource=ObjectRef(resource.resource_type, resource_id),
                    context=context,
                    **kwargs,
                ).allowed
                for resource_id in self._oracle_resource_ids(
                    resource.resource_type, kwargs.get("using")
                )
                if resource_id
            ):
                return CheckResult.has()
            return CheckResult.no()
        if value is None:
            return CheckResult.conditional(missing=tuple(sorted(missing)))
        return CheckResult.has() if value else CheckResult.no()

    def _oracle_resource_ids(self, resource_type, using):
        from rebac._id import resource_id_attr
        from rebac.models import active_relationship_model
        from rebac.resources import model_for_resource_type

        rows = self._maybe_using(active_relationship_model().objects, using).filter(
            resource_type=resource_type
        )
        result = {row.resource_id for row in rows}
        model = model_for_resource_type(resource_type)
        if model is not None:
            result.update(
                str(value)
                for value in self._maybe_using(model._base_manager, using).values_list(
                    resource_id_attr(model), flat=True
                )
            )
        return result

    def _eval_permission(
        self,
        expr: PermExpr,
        definition: Definition,
        resource_id: str,
        subject: SubjectRef,
        depth: int,
        context: dict[str, Any] | None = None,
        missing: set[str] | None = None,
        using: str | None = None,
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
            using=using,
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
            using=ctx.using,
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
        from rebac.models import active_relationship_model

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
        attribute_backing = self._resolve_declared_attribute_backing(definition, via_relation)
        if attribute_backing is not None and attribute_backing.applies_to(resource_id):
            return self._walk_attribute_backed_arrow(
                ctx=ctx,
                resource_id=resource_id,
                attribute_backing=attribute_backing,
                target=target,
                depth=depth,
            )

        const_backing = self._resolve_declared_const_backing(definition, via_relation)
        if const_backing is not None:
            if not const_backing.matches(resource_id, using=ctx.using):
                return False
            return self._walk_const_arrow(
                ctx=ctx,
                const_backing=const_backing,
                target=target,
                depth=depth,
            )

        RelationshipModel = active_relationship_model()
        targets = self._maybe_using(RelationshipModel.objects, ctx.using).filter(
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
                using=ctx.using,
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
        target_def = ctx.schema.get_definition(field_backing.target_resource_type)
        if target_def is None:
            return False
        qs = field_backing.queryset(resource_id=resource_id, using=ctx.using)
        target_values = list(qs.values_list(field_backing.target_values_path(), flat=True))
        saw_conditional = False
        for target_id in target_values:
            if target_id is None:
                continue
            inner = self._eval_permission_on(
                permission_name=target,
                definition=target_def,
                resource_id=str(target_id),
                subject=ctx.subject,
                depth=depth + 1,
                context=ctx.context,
                missing=ctx.missing,
                using=ctx.using,
            )
            if inner is True:
                return True
            if inner is None:
                saw_conditional = True
        if saw_conditional:
            return None
        return False

    def _walk_attribute_backed_arrow(
        self,
        ctx: WalkContext,
        resource_id: str,
        attribute_backing: ResolvedAttributeBacking,
        target: str,
        depth: int,
    ) -> bool | None:
        target_definition = ctx.schema.get_definition(attribute_backing.target_resource_type)
        if target_definition is None:
            return False
        saw_conditional = False
        for target_id in attribute_backing.subject_ids(resource_id, using=ctx.using):
            if target_id is None:
                continue
            verdict = self._eval_permission_on(
                permission_name=target,
                definition=target_definition,
                resource_id=str(target_id),
                subject=ctx.subject,
                depth=depth + 1,
                context=ctx.context,
                missing=ctx.missing,
                using=ctx.using,
            )
            if verdict is True:
                return True
            if verdict is None:
                saw_conditional = True
        return None if saw_conditional else False

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
            using=ctx.using,
        )

    def _eval_permission_on(
        self,
        permission_name: str,
        definition: Definition,
        resource_id: str,
        subject: SubjectRef,
        depth: int,
        context: dict[str, Any] | None = None,
        missing: set[str] | None = None,
        using: str | None = None,
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
                using=using,
            )
        return self._eval_permission(
            permission.expression, definition, resource_id, subject, depth, context, missing, using
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
        using: str | None = None,
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
        from rebac.models import active_relationship_model

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
            return field_backing.queryset(
                resource_id=resource_id, subject=subject, using=using
            ).exists()

        attribute_backing = self._resolve_declared_attribute_backing(definition, relation_def)
        if attribute_backing is not None and attribute_backing.applies_to(resource_id):
            if not _subject_allowed_by_relation(relation_def, subject):
                return False
            return attribute_backing.has_subject(resource_id, subject, using=using)

        const_backing = self._resolve_declared_const_backing(definition, relation_def)
        if const_backing is not None:
            # Every row behaves as if it held `#<relation> @ <const target>`, so
            # the relation is held only by that fixed subject on matching rows.
            if not _subject_allowed_by_relation(relation_def, subject):
                return False
            return subject == SubjectRef.of(
                const_backing.target_resource_type, const_backing.target_id
            ) and const_backing.matches(resource_id, using=using)

        RelationshipModel = active_relationship_model()

        rows = self._maybe_using(RelationshipModel.objects, using).filter(
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
                using=using,
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
        from rebac.caveats import evaluate as eval_caveat

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
        its EXISTS subqueries join against.

        Nested evaluator walks retain the pinned alias. Alias-free public
        calls keep the boundary documented in ARCHITECTURE's multi-database section.
        """
        return manager if using is None else manager.using(using)

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

    def _resolve_declared_attribute_backing(
        self,
        definition: Definition,
        relation: Relation,
    ) -> ResolvedAttributeBacking | None:
        if not isinstance(relation.backing, AttributeBinding):
            return None
        backing = resolve_attribute_backing(definition, relation)
        if backing is None:
            raise SchemaError(
                f"{definition.resource_type}#{relation.name}: attribute-backed "
                "relation could not be resolved; run `manage.py check --tag rebac`"
            )
        return backing


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


class _MemoryRows:
    """The tiny queryset subset used by the frozen scalar walker, without a DB."""

    def __init__(self, rows):
        self.rows = tuple(rows)

    def __iter__(self):
        return iter(self.rows)

    @staticmethod
    def _matches(row, predicate):
        from django.db.models import Q

        if isinstance(predicate, Q):
            results = [_MemoryRows._matches(row, item) for item in predicate.children]
            value = any(results) if predicate.connector == "OR" else all(results)
            return not value if predicate.negated else value
        key, expected = predicate
        name, _, op = key.partition("__")
        value = getattr(row, name)
        if op == "in":
            return value in expected
        if op == "isnull":
            return (value is None) == expected
        if op == "gt":
            return value is not None and value > expected
        if op:
            raise AssertionError(f"Unsupported oracle lookup: {key}")
        return value == expected

    def filter(self, *args, **kwargs):
        predicates = (*args, *kwargs.items())
        return _MemoryRows(
            row
            for row in self.rows
            if all(self._matches(row, predicate) for predicate in predicates)
        )

    def exclude(self, **kwargs):
        return _MemoryRows(
            row for row in self.rows if not all(self._matches(row, item) for item in kwargs.items())
        )


class MemoryWalkerOracle(WalkerOracle):
    """Feed in-memory tuples to the frozen methods, preserving their control flow.

    Tests freeze django.utils.timezone.now externally. Resolved backing edges
    must already be supplied as tuples; unfiltered const bindings need no ORM.
    """

    def __init__(self, schema: Schema, tuples: Iterable[RelationshipTuple]):
        from types import SimpleNamespace

        super().__init__()
        self._memory_schema = schema
        self._rows = _MemoryRows(
            SimpleNamespace(
                resource_type=row.resource.resource_type,
                resource_id=row.resource.resource_id,
                relation=row.relation,
                subject_type=row.subject.subject_type,
                subject_id=row.subject.subject_id,
                optional_subject_relation=row.subject.optional_relation,
                caveat_name=row.caveat_name,
                caveat_context=row.caveat_context,
                expires_at=row.expires_at,
            )
            for row in tuples
        )

    def schema(self):
        return self._memory_schema

    def _maybe_using(self, manager, using):
        return self._rows

    def _resolve_declared_field_backing(self, definition, relation):
        return None

    def _resolve_declared_attribute_backing(self, definition, relation):
        return None

    def _resolve_declared_const_backing(self, definition, relation):
        from types import SimpleNamespace

        if not isinstance(relation.backing, ConstBinding) or relation.backing.filters:
            return None
        return SimpleNamespace(
            target_resource_type=relation.allowed_subjects[0].type,
            target_id=relation.backing.target_id,
            matches=lambda resource_id, using=None: True,
        )
