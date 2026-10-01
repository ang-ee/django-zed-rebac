"""Execute compiled LocalBackend predicates without a derived permission index."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from django.db import models
from django.db.models import Exists, Expression, F, Q, Subquery, Value
from django.db.models.functions import Now
from django.db.models.lookups import Exact, GreaterThanOrEqual, In, LessThan
from django.utils import timezone

from rebac._id import model_identity_fields, resource_id_attr
from rebac.composition import TaggedComposition, compose_tagged, split_stale_overrides
from rebac.errors import PermissionDepthExceeded
from rebac.field_backing import resolve_attribute_backing, resolve_field_backing
from rebac.index.codec import identity_codec
from rebac.models import active_relationship_model
from rebac.models.generation import SchemaGeneration
from rebac.resources import (
    model_for_resource_type,
    model_for_subject_type,
    model_resource_type,
    stores_rows,
)
from rebac.schema.ast import AttributeBinding, ConstBinding, FieldBinding, Schema
from rebac.schema.cache import SchemaSnapshot, schema_operation
from rebac.schema.walker import find_relation
from rebac.types import CheckResult, ObjectRef, SubjectRef

from . import At, Bound, Compiler
from .conditions import CaveatVerdicts
from .evaluate import named_candidates, residual

if TYPE_CHECKING:
    from rebac.backends.local import LocalBackend


def _schema(backend: LocalBackend) -> tuple[Schema, SchemaSnapshot]:
    snapshot = backend._schema_snapshot()
    return snapshot.schema, snapshot


@dataclass(frozen=True)
class _Plan:
    snapshot: SchemaSnapshot
    tagged: TaggedComposition
    effective: Schema
    selected_at: datetime
    caveat_deadlines: tuple[datetime, ...]


def _plan(backend: LocalBackend) -> _Plan:
    from rebac.models import SchemaOverride

    _effective, snapshot = _schema(backend)
    baseline = snapshot.baseline or snapshot.schema
    overrides, _stale = split_stale_overrides(baseline, list(snapshot.overrides))
    selected_at = timezone.now()
    active = [row for row in overrides if row.expires_at is None or row.expires_at > selected_at]
    # Permission sites retain their SQL deadlines. CEL expressions cannot be
    # selected in SQL, so choose them once and witness that selection interval.
    tagged = compose_tagged(
        baseline,
        [
            row
            for row in overrides
            if row.kind != SchemaOverride.KIND_RECAVEAT
            or row.expires_at is None
            or row.expires_at > selected_at
        ],
    )
    deadlines = tuple(
        sorted(
            {
                row.expires_at
                for row in overrides
                if row.kind == SchemaOverride.KIND_RECAVEAT and row.expires_at is not None
            }
        )
    )
    return _Plan(snapshot, tagged, compose_tagged(baseline, active).schema, selected_at, deadlines)


class _ManualRevision(Expression):
    def __init__(self, backend: LocalBackend) -> None:
        super().__init__(output_field=models.CharField())
        self.backend = backend

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        sql, params = compiler.compile(Value(self.backend._manual_schema_revision()))
        return sql, tuple(params)


def _fence(plan: _Plan, *, backend: LocalBackend, using: str) -> models.QuerySet[Any]:
    """Close a prepared predicate when its policy revision or deadline changes."""

    snapshot = plan.snapshot
    rows = SchemaGeneration.objects.using(using).filter(pk=1)
    if snapshot.revision is None:
        return rows.none()
    if backend._schema_is_manual:
        rows = rows.filter(Exact(Value(snapshot.revision), _ManualRevision(backend)))
    else:
        rows = rows.filter(revision=snapshot.revision)
    for deadline in plan.caveat_deadlines:
        comparison = (
            LessThan(Now(), Value(deadline, output_field=models.DateTimeField()))
            if plan.selected_at < deadline
            else GreaterThanOrEqual(Now(), Value(deadline, output_field=models.DateTimeField()))
        )
        rows = rows.filter(comparison)
    return rows


def _compiler(
    plan: _Plan,
    key: tuple[str, str],
    actor: SubjectRef,
    *,
    context: Mapping[str, Any] | None,
    using: str,
) -> Compiler:
    schema = plan.tagged.schema
    verdicts = CaveatVerdicts.prepare(schema, key, context=context, using=using)
    return Compiler(schema, actor, using, tagged=plan.tagged, verdicts=verdicts, now=Now())


def _point_at(resource: ObjectRef) -> At:
    return At(resource.resource_type, Value(resource.resource_id), None, False)


def _model_at(model: type[models.Model]) -> At:
    identity = resource_id_attr(model)
    _, field = model_identity_fields(model, identity)
    resource_type = model_resource_type(model)
    if resource_type is None:
        raise ValueError(f"{model.__name__} has no REBAC resource type")
    return At(resource_type, F(identity), field, True)


def _point_result(
    *,
    backend: LocalBackend,
    resource: ObjectRef,
    action: str,
    actor: SubjectRef,
    context: Mapping[str, Any] | None,
    using: str,
    lower_only: bool = False,
) -> tuple[bool, bool, bool, _Plan]:
    plan = _plan(backend)
    key = resource.resource_type, action
    compiler = _compiler(plan, key, actor, context=context, using=using)
    at = _point_at(resource)
    lower = compiler.holds(key, at, Bound.LOWER)
    rows = _fence(plan, backend=backend, using=using)
    # Only LOWER may authorize, and it is one SQL statement with every fact
    # witnessed in that statement.  Subsequent probes can only return NO,
    # CONDITIONAL, or a depth error.  This split also bounds SQLite expression
    # depth for recursive policies without compromising READ COMMITTED safety.
    if rows.filter(lower).exists():
        return True, True, False, plan
    if lower_only:
        return False, False, False, plan
    if not rows.filter(compiler.holds(key, at, Bound.UPPER)).exists():
        return False, False, False, plan
    deep = compiler.has_recursion(key) and rows.filter(compiler.depth_unknown(key, at)).exists()
    return False, True, deep, plan


@schema_operation
def check(
    *,
    backend: LocalBackend,
    resource: ObjectRef,
    action: str,
    actor: SubjectRef,
    context: Mapping[str, Any] | None,
    using: str,
) -> CheckResult:
    schema, _snapshot = _schema(backend)
    definition = schema.get_definition(resource.resource_type)
    if definition is None:
        return CheckResult.no(reason=f"unknown resource type: {resource.resource_type}")
    if (
        schema.get_permission(resource.resource_type, action) is None
        and find_relation(definition, action) is None
    ):
        return CheckResult.no(reason=f"unknown action: {resource.resource_type}#{action}")
    if not resource.resource_id:
        # A type-level arm may grant with no finite row. The empty ID is not a
        # valid resource identity, so only row-independent arms can hold there.
        if _point_result(
            backend=backend,
            resource=resource,
            action=action,
            actor=actor,
            context=context,
            using=using,
            lower_only=True,
        )[0]:
            return CheckResult.has()
        ids = accessible_ids(
            backend=backend,
            resource_type=resource.resource_type,
            action=action,
            actor=actor,
            context=context,
            using=using,
        )
        return CheckResult.has() if ids.exists() else CheckResult.no()

    lower, upper, deep, plan = _point_result(
        backend=backend,
        resource=resource,
        action=action,
        actor=actor,
        context=context,
        using=using,
    )
    if lower:
        return CheckResult.has()
    if not upper:
        return CheckResult.no()
    # The residual evaluator is deliberately separate from the authority:
    # only the SQL lower bound above may allow.  A reduced Boolean formula
    # reports only the caveat instances that can still change this result.
    uncertainty = residual(
        schema=plan.effective,
        resource=resource,
        action=action,
        actor=actor,
        context=context,
        using=using,
    )
    if uncertainty.depth_relevant or deep:
        raise PermissionDepthExceeded("Permission path exceeds REBAC_DEPTH_LIMIT")
    if uncertainty.missing:
        return CheckResult.conditional(tuple(sorted(uncertainty.missing)))
    # The metadata pass may have observed a concurrent change after the SQL
    # authority.  It cannot upgrade the result to HAS.
    return CheckResult.no()


def _scope_q_now(
    backend: LocalBackend,
    plan: _Plan,
    model: type[models.Model],
    action: str,
    actor: SubjectRef,
    using: str,
) -> Q:
    resource_type = model_resource_type(model)
    if resource_type is None:
        return Q(pk__in=[])
    key = resource_type, action
    compiler = _compiler(plan, key, actor, context=None, using=using)
    return Q(Exists(_fence(plan, backend=backend, using=using))) & compiler.holds(
        key, _model_at(model), Bound.LOWER
    )


class _LiveScopeIds(Expression):
    """Compile a complete child queryset when a lazy scope statement runs.

    Schema overrides and caveat instances can change after a queryset was
    constructed.  Building the child query here refreshes both without
    introducing joins into an already resolved outer query.  The child still
    carries the policy fence and witnesses all mutable tuple facts in SQL.
    """

    def __init__(
        self,
        backend: LocalBackend,
        model: type[models.Model],
        action: str,
        actor: SubjectRef,
        using: str,
        revision: str | None,
    ) -> None:
        identity = resource_id_attr(model)
        _, field = model_identity_fields(model, identity)
        super().__init__(output_field=field)
        self.backend = backend
        self.model = model
        self.action = action
        self.actor = actor
        self.using = using
        self.revision = revision

    @schema_operation
    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        identity = resource_id_attr(self.model)
        plan = _plan(self.backend)
        rows = self.model._base_manager.using(self.using)
        if plan.snapshot.revision != self.revision:
            ids = rows.none().values(identity)
        else:
            predicate = _scope_q_now(
                self.backend, plan, self.model, self.action, self.actor, self.using
            )
            ids = rows.filter(predicate).order_by().values(identity)
        sql, params = compiler.compile(Subquery(ids))
        return sql, tuple(params)


@schema_operation
def scope_q(
    *,
    backend: LocalBackend,
    model: type[models.Model],
    action: str,
    actor: SubjectRef,
    using: str,
) -> Q:
    if model_resource_type(model) is None:
        return Q(pk__in=[])
    identity = resource_id_attr(model)
    _schema_at_build, snapshot = _schema(backend)
    return Q(
        In(F(identity), _LiveScopeIds(backend, model, action, actor, using, snapshot.revision))
    )


def _tuple_universe(
    resource_type: str,
    *,
    using: str,
) -> Iterable[tuple[models.QuerySet[Any], str]]:
    rows = cast(Any, active_relationship_model().objects.using(using)).index_projection()
    for type_column, id_column in (
        ("resource_type", "resource_id"),
        ("subject_type", "subject_id"),
    ):
        yield (
            rows.filter(**{type_column: resource_type}).exclude(**{f"{id_column}__in": ("", "*")}),
            id_column,
        )


def _universe_parts(
    *, resource_type: str, schema: Schema, using: str
) -> Iterable[tuple[models.QuerySet[Any], str, At]]:
    model = model_for_resource_type(resource_type)
    subject_model = model_for_subject_type(resource_type) if model is None else None
    if model is None and subject_model is not None:
        model = subject_model[0]
    if model is not None and stores_rows(model):
        identity = subject_model[1] if subject_model is not None else resource_id_attr(model)
        codec = identity_codec(model, identity)
        rows = (
            model._base_manager.using(using)
            .order_by()
            .annotate(_rebac_wire_id=codec.to_wire(identity))
            .exclude(_rebac_wire_id__isnull=True)
        )
        if model_resource_type(model) == resource_type:
            yield rows, "_rebac_wire_id", _model_at(model)
        else:
            # A configured User/Group mapping can have no RebacMixin model.
            yield rows, "_rebac_wire_id", At(resource_type, F("_rebac_wire_id"), None, False)
    for rows, id_column in _tuple_universe(resource_type, using=using):
        yield rows, id_column, At(resource_type, F(id_column), None, False)
    for definition in schema.definitions:
        for relation in definition.relations:
            if (
                isinstance(relation.backing, FieldBinding)
                and relation.allowed_subjects
                and relation.allowed_subjects[0].type == resource_type
            ):
                resolved_field = resolve_field_backing(definition, relation)
                if resolved_field is not None:
                    target_codec = identity_codec(
                        resolved_field.target_model, resolved_field.target_id_attr
                    )
                    backing_rows = (
                        resolved_field.queryset(using=using)
                        .order_by()
                        .annotate(
                            _rebac_wire_id=target_codec.to_wire(resolved_field.target_values_path())
                        )
                        .exclude(_rebac_wire_id__isnull=True)
                    )
                    yield (
                        backing_rows,
                        "_rebac_wire_id",
                        At(resource_type, F("_rebac_wire_id"), None, False),
                    )
            if relation.allowed_subjects and relation.allowed_subjects[0].type == resource_type:
                if isinstance(relation.backing, ConstBinding):
                    fixed = relation.backing.target_id
                    rows = SchemaGeneration.objects.using(using).annotate(
                        _rebac_wire_id=Value(fixed, output_field=models.CharField())
                    )
                    yield (
                        rows,
                        "_rebac_wire_id",
                        At(resource_type, F("_rebac_wire_id"), None, False),
                    )
            if definition.resource_type != resource_type or not isinstance(
                relation.backing, AttributeBinding
            ):
                continue
            attribute = resolve_attribute_backing(definition, relation)
            if attribute is None:
                continue
            if attribute.resource is not None:
                rows = SchemaGeneration.objects.using(using).annotate(
                    _rebac_wire_id=Value(attribute.resource, output_field=models.CharField())
                )
            else:
                rows = (
                    attribute.target_model._base_manager.using(using)
                    .filter(**attribute.filters)
                    .annotate(
                        _rebac_wire_id=identity_codec(
                            attribute.target_model, attribute.field.name
                        ).to_wire(attribute.field.name)
                    )
                    .exclude(_rebac_wire_id__isnull=True)
                )
            yield rows, "_rebac_wire_id", At(resource_type, F("_rebac_wire_id"), None, False)


def _accessible_now(
    *,
    backend: LocalBackend,
    plan: _Plan,
    resource_type: str,
    action: str,
    actor: SubjectRef,
    using: str,
    context: Mapping[str, Any] | None = None,
) -> models.QuerySet[Any]:
    key = resource_type, action
    compiler = _compiler(plan, key, actor, context=context, using=using)
    fence = Q(Exists(_fence(plan, backend=backend, using=using)))
    branches = [
        rows.filter(fence & compiler.holds(key, at, Bound.LOWER))
        .order_by()
        .values_list(column, flat=True)
        for rows, column, at in _universe_parts(
            resource_type=resource_type, schema=plan.tagged.schema, using=using
        )
    ]
    if branches:
        return branches[0].union(*branches[1:])
    return cast(
        models.QuerySet[Any],
        active_relationship_model().objects.using(using).none().values_list("pk", flat=True),
    )


@dataclass(frozen=True)
class _AccessibleResources:
    """A lazy enumeration request, prepared afresh at each consumption."""

    backend: LocalBackend
    resource_type: str
    action: str
    actor: SubjectRef
    using: str
    context: Mapping[str, Any] | None
    revision: str | None

    @schema_operation
    def _query(self) -> models.QuerySet[Any]:
        plan = _plan(self.backend)
        if plan.snapshot.revision != self.revision:
            return (
                SchemaGeneration.objects.using(self.using).none().values_list("revision", flat=True)
            )
        return _accessible_now(
            backend=self.backend,
            plan=plan,
            resource_type=self.resource_type,
            action=self.action,
            actor=self.actor,
            using=self.using,
            context=self.context,
        )

    def __iter__(self) -> Iterator[str]:
        return iter(self._query())

    def exists(self) -> bool:
        return self._query().exists()


@schema_operation
def accessible_ids(
    *,
    backend: LocalBackend,
    resource_type: str,
    action: str,
    actor: SubjectRef,
    using: str,
    context: Mapping[str, Any] | None = None,
) -> _AccessibleResources:
    _schema_now, snapshot = _schema(backend)
    return _AccessibleResources(
        backend, resource_type, action, actor, using, context, snapshot.revision
    )


@schema_operation
def grants_all(
    *,
    backend: LocalBackend,
    resource_type: str,
    action: str,
    actor: SubjectRef,
    using: str,
    context: Mapping[str, Any] | None = None,
) -> bool:
    return _point_result(
        backend=backend,
        resource=ObjectRef(resource_type, ""),
        action=action,
        actor=actor,
        context=context,
        using=using,
        lower_only=True,
    )[0]


@schema_operation
def lookup_subjects(
    *,
    backend: LocalBackend,
    resource: ObjectRef,
    action: str,
    subject_type: str,
    using: str,
    context: Mapping[str, Any] | None = None,
) -> list[SubjectRef]:
    schema, _snapshot = _schema(backend)
    candidates: set[SubjectRef] = set()
    rows = cast(Any, active_relationship_model().objects.using(using)).index_projection()
    for id_, relation in (
        rows.filter(subject_type=subject_type)
        .values_list("subject_id", "subject_relation")
        .iterator(chunk_size=1000)
    ):
        if id_:
            candidates.add(SubjectRef.of(subject_type, id_, relation))
    for id_ in (
        rows.filter(resource_type=subject_type)
        .values_list("resource_id", flat=True)
        .iterator(chunk_size=1000)
    ):
        if id_ and id_ != "*":
            candidates.add(SubjectRef.of(subject_type, id_))
    target = model_for_subject_type(subject_type)
    allowed_relations = {
        allowed.relation
        for definition in schema.definitions
        for relation in definition.relations
        for allowed in relation.allowed_subjects
        if allowed.type == subject_type and allowed.relation
    }
    if target is not None and stores_rows(target[0]):
        model, identity = target
        codec = identity_codec(model, identity)
        for id_ in (
            model._base_manager.using(using)
            .annotate(_rebac_wire_id=codec.to_wire(identity))
            .values_list("_rebac_wire_id", flat=True)
            .iterator(chunk_size=1000)
        ):
            if id_:
                candidates.add(SubjectRef.of(subject_type, id_))
                candidates.update(
                    SubjectRef.of(subject_type, id_, relation) for relation in allowed_relations
                )
    for definition in schema.definitions:
        for relation in definition.relations:
            if isinstance(relation.backing, ConstBinding):
                for allowed in relation.allowed_subjects:
                    if allowed.type == subject_type:
                        candidates.add(
                            SubjectRef.of(
                                subject_type, relation.backing.target_id, allowed.relation
                            )
                        )
    named = named_candidates(
        schema=schema,
        resource=resource,
        action=action,
        candidates=candidates,
        using=using,
    )
    return [
        candidate
        for candidate in named
        if _point_result(
            backend=backend,
            resource=resource,
            action=action,
            actor=candidate,
            context=context,
            using=using,
            lower_only=True,
        )[0]
    ]
