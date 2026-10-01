"""Execute compiled LocalBackend predicates without a derived permission index.

A statement is compiled once per policy, permission and actor shape, and kept
as Django's own SQL with placeholders for the actor's id (the accepted
plan-cache exception of ARCHITECTURE).  Facts about fixed objects, such as
membership of a constant role, are decided before the statement is compiled
and witnessed inside it, so a decision that no longer holds when the statement
runs yields no rows instead of a stale answer.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import TYPE_CHECKING, Any, cast

from django.db import connections, models
from django.db.models import Exists, Expression, F, Q, Value
from django.db.models.lookups import Exact, GreaterThanOrEqual, LessThan
from django.utils import timezone

from rebac._id import model_identity_fields, resource_id_attr
from rebac.composition import TaggedComposition, compose_tagged, split_stale_overrides
from rebac.conf import app_settings
from rebac.errors import PermissionDepthExceeded, SchemaError
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

from . import At, Bound, Compiler, predicate
from .conditions import CaveatVerdicts
from .evaluate import named_subjects, residual
from .predicate import (
    Fact,
    _and,
    _Compiled,
    _not,
    _Param,
    actor_shape,
    bind,
    clock,
    const_facts,
    is_false,
    is_true,
)
from .program import CompileProgram, Key

if TYPE_CHECKING:
    from rebac.backends.local import LocalBackend

_MANUAL_REVISION = object()
_RESOURCE_WIRE = object()
_LIMIT = 512
_lock = RLock()


@dataclass(frozen=True, slots=True)
class _Kept:
    """One compiled statement.  ``every`` marks a gate that admits every row."""

    sql: str
    params: tuple[Any, ...]
    every: bool = False


_policies: OrderedDict[tuple[Any, ...], _Policy] = OrderedDict()
_statements: OrderedDict[tuple[Any, ...], _Kept | bool] = OrderedDict()


def reset() -> None:
    """Forget every kept policy and statement (settings or registries changed)."""
    with _lock:
        _policies.clear()
        _statements.clear()


# ---------- The policy a statement is compiled for ----------


@dataclass(frozen=True)
class _Policy:
    key: tuple[Any, ...]
    snapshot: SchemaSnapshot
    tagged: TaggedComposition
    effective: Schema
    program: CompileProgram
    selected_at: datetime
    caveat_deadlines: tuple[datetime, ...]

    @property
    def schema(self) -> Schema:
        return self.tagged.schema


def _schema(backend: LocalBackend) -> tuple[Schema, SchemaSnapshot]:
    snapshot = backend._schema_snapshot()
    return snapshot.schema, snapshot


def _policy(backend: LocalBackend) -> _Policy:
    from rebac.models import SchemaOverride

    _effective, snapshot = _schema(backend)
    baseline = snapshot.baseline or snapshot.schema
    key = (
        snapshot.using,
        snapshot.revision,
        backend._schema_is_manual,
        id(baseline),
        tuple(getattr(row, "pk", None) for row in snapshot.overrides),
    )
    with _lock:
        kept = _policies.get(key)
        if kept is not None and kept.snapshot.schema is snapshot.schema:
            _policies.move_to_end(key)
            return kept
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
    policy = _Policy(
        key,
        snapshot,
        tagged,
        compose_tagged(baseline, active).schema,
        CompileProgram.build(tagged.schema),
        selected_at,
        deadlines,
    )
    if not deadlines:
        # A caveat override with a deadline selects its expression by the
        # clock, so such a policy is prepared afresh each time.
        with _lock:
            _policies[key] = policy
            while len(_policies) > _LIMIT:
                _policies.popitem(last=False)
    return policy


def _fence(
    policy: _Policy, *, backend: LocalBackend, using: str, parametric: bool
) -> models.QuerySet[Any]:
    """Close a prepared predicate when its policy revision or deadline changes."""

    snapshot = policy.snapshot
    rows = SchemaGeneration.objects.using(using).filter(pk=1)
    if snapshot.revision is None:
        return rows.none()
    if backend._schema_is_manual:
        current: Expression = (
            _Param(_MANUAL_REVISION, models.CharField())
            if parametric
            else Value(backend._manual_schema_revision())
        )
        rows = rows.filter(Exact(Value(snapshot.revision), current))
    else:
        rows = rows.filter(revision=snapshot.revision)
    now: Expression = (
        clock()
        if parametric
        else Value(predicate.statement_now(), output_field=models.DateTimeField())
    )
    for deadline in policy.caveat_deadlines:
        comparison = (
            LessThan(now, Value(deadline, output_field=models.DateTimeField()))
            if policy.selected_at < deadline
            else GreaterThanOrEqual(now, Value(deadline, output_field=models.DateTimeField()))
        )
        rows = rows.filter(comparison)
    return rows


# ---------- Kept statements ----------


class _Sql(Expression):
    """``EXISTS`` over Django-compiled SQL whose parameters are already bound."""

    def __init__(self, sql: str, params: Iterable[Any]) -> None:
        super().__init__(output_field=models.BooleanField())
        self.sql = sql
        self.params = tuple(params)

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        return f"EXISTS {self.sql}", self.params


@dataclass(frozen=True)
class _Operation:
    """What one statement is compiled for: a policy and an actor."""

    backend: LocalBackend
    policy: _Policy
    actor: SubjectRef
    using: str
    context: Mapping[str, Any] | None
    shape: tuple[Any, ...]
    _verdicts: dict[Key, CaveatVerdicts] = field(default_factory=dict)

    @classmethod
    def begin(
        cls,
        backend: LocalBackend,
        actor: SubjectRef,
        using: str,
        context: Mapping[str, Any] | None = None,
        policy: _Policy | None = None,
    ) -> _Operation:
        policy = policy if policy is not None else _policy(backend)
        return cls(
            backend,
            policy,
            actor,
            using,
            context,
            (
                policy.key,
                actor_shape(policy.schema, actor, using),
                using,
                connections[using].vendor,
                app_settings.REBAC_DEPTH_LIMIT,
                active_relationship_model()._meta.label_lower,
            ),
        )

    def verdicts(self, key: Key) -> CaveatVerdicts:
        found = self._verdicts.get(key)
        if found is None:
            found = self._verdicts[key] = CaveatVerdicts.prepare(
                self.policy.schema, key, context=self.context, using=self.using
            )
        return found

    def compiler(
        self,
        verdicts: CaveatVerdicts,
        facts: Mapping[Fact, bool] | None = None,
        *,
        parametric: bool = True,
    ) -> Compiler:
        return Compiler(
            self.policy.schema,
            self.actor,
            self.using,
            tagged=self.policy.tagged,
            verdicts=verdicts,
            program=self.policy.program,
            parametric=parametric,
            facts=facts,
        )

    def gate(self, *, parametric: bool = True) -> models.QuerySet[Any]:
        return _fence(self.policy, backend=self.backend, using=self.using, parametric=parametric)

    def statement(
        self,
        key: tuple[Any, ...],
        verdicts: CaveatVerdicts,
        build: Callable[[], models.QuerySet[Any] | bool | tuple[models.QuerySet[Any], bool]],
    ) -> _Kept | bool:
        """The SQL of ``build()``; a bool when it needs no rows to be decided."""
        # A statement can be kept when nothing in it varies but the actor's
        # id and the clock: that is, when it carries no caveat verdict list.
        keep = verdicts.empty
        full = (*self.shape, *key)
        if keep:
            with _lock:
                kept = _statements.get(full)
                if kept is not None:
                    _statements.move_to_end(full)
                    return kept
        built = build()
        compiled: _Kept | bool
        if isinstance(built, bool):
            compiled = built
        else:
            rows, every = built if isinstance(built, tuple) else (built, False)
            sql, params = _Compiled(rows).as_sql(None, connections[self.using])
            compiled = _Kept(sql, params, every)
        if keep:
            with _lock:
                _statements[full] = compiled
                while len(_statements) > _LIMIT:
                    _statements.popitem(last=False)
        return compiled

    def bound(self, params: Iterable[Any], resource_id: str | None = None) -> list[Any]:
        result = []
        for param in bind(params, self.actor, connections[self.using]):
            if param is _MANUAL_REVISION:
                result.append(self.backend._manual_schema_revision())
            elif param is _RESOURCE_WIRE:
                result.append(resource_id)
            else:
                result.append(param)
        return result

    # ---------- Facts about fixed objects ----------

    def decide(self, key: Key, verdicts: CaveatVerdicts) -> dict[Fact, bool]:
        """Decide, in one statement, every fixed-object fact ``key`` can use."""
        decided: dict[Fact, bool] = {}
        probes: dict[str, _Sql] = {}
        names: dict[str, Fact] = {}
        for number, fact in enumerate(const_facts(self.policy.schema, self.policy.program, key)):
            compiled = self.statement(
                ("fact", fact), verdicts, lambda fact=fact: self._fact_rows(fact, verdicts)
            )
            if isinstance(compiled, bool):
                decided[fact] = compiled
            else:
                name = f"_rebac_fact_{number}"
                names[name] = fact
                probes[name] = _Sql(compiled.sql, self.bound(compiled.params))
        if probes:
            row = (
                SchemaGeneration.objects.using(self.using)
                .filter(pk=1)
                .annotate(**probes)
                .values(*probes)
                .first()
            )
            for name, fact in names.items():
                decided[fact] = bool(row[name]) if row is not None else False
        return decided

    def _fact_rows(self, fact: Fact, verdicts: CaveatVerdicts) -> models.QuerySet[Any] | bool:
        condition = self.compiler(verdicts).fact_q(fact)
        if is_true(condition) or is_false(condition):
            return is_true(condition)
        return (
            SchemaGeneration.objects.using(self.using).filter(pk=1).filter(condition).values("pk")
        )

    def witness(
        self, used: Iterable[Fact], decided: Mapping[Fact, bool], verdicts: CaveatVerdicts
    ) -> Q:
        """Each decision used must still be true of the statement's own snapshot."""
        inline = self.compiler(verdicts)
        parts = []
        for fact in sorted(used, key=str):
            condition = inline.fact_q(fact)
            parts.append(condition if decided[fact] else _not(condition))
        return _and(*parts)


def _model_at(model: type[models.Model]) -> At:
    identity = resource_id_attr(model)
    _, identity_field = model_identity_fields(model, identity)
    resource_type = model_resource_type(model)
    if resource_type is None:
        raise ValueError(f"{model.__name__} has no REBAC resource type")
    return At(resource_type, F(identity), identity_field, True)


# ---------- Point checks ----------


def _point(operation: _Operation, key: Key, resource_id: str, which: str) -> bool:
    """One bound of ``key`` at one identity, as one statement."""
    verdicts = operation.verdicts(key)

    def build() -> models.QuerySet[Any] | bool:
        compiler = operation.compiler(verdicts)
        at = At(key[0], _Param(_RESOURCE_WIRE, models.TextField()), None, False)
        if which == "depth":
            condition = compiler.depth_unknown(key, at)
        else:
            condition = compiler.holds(key, at, Bound(which))
        if is_false(condition):
            return False
        rows = operation.gate()
        return (rows if is_true(condition) else rows.filter(condition)).values("pk")

    compiled = operation.statement(("point", key, which), verdicts, build)
    if isinstance(compiled, bool):
        return compiled and operation.gate(parametric=False).exists()
    probe = _Sql(compiled.sql, operation.bound(compiled.params, resource_id))
    return SchemaGeneration.objects.using(operation.using).filter(pk=1).filter(Q(probe)).exists()


def _point_result(
    *,
    backend: LocalBackend,
    resource: ObjectRef,
    action: str,
    actor: SubjectRef,
    context: Mapping[str, Any] | None,
    using: str,
    lower_only: bool = False,
) -> tuple[bool, bool, bool, _Policy]:
    operation = _Operation.begin(backend, actor, using, context)
    key = resource.resource_type, action
    # Only LOWER may authorize, and it is one SQL statement with every fact
    # witnessed in that statement.  Subsequent probes can only return NO,
    # CONDITIONAL, or a depth error.
    if _point(operation, key, resource.resource_id, "lower"):
        return True, True, False, operation.policy
    recursive = bool(operation.policy.program.reachable(key) & operation.policy.program.recursive)
    # The bounds differ only where a caveat or a recursion is in reach.
    if lower_only or not (recursive or not operation.verdicts(key).empty):
        return False, False, False, operation.policy
    if not _point(operation, key, resource.resource_id, "upper"):
        return False, False, False, operation.policy
    deep = recursive and _point(operation, key, resource.resource_id, "depth")
    return False, True, deep, operation.policy


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

    lower, upper, deep, policy = _point_result(
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
        schema=policy.effective,
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


# ---------- Scopes ----------


def _scope_statement(operation: _Operation, model: type[models.Model], key: Key) -> _Kept | bool:
    """The ids of ``model`` the actor holds ``key`` on, as SQL.

    ``False`` when there are none.  ``every`` is set when every row qualifies
    and the SQL is only the gate: the policy fence and the fact witnesses.
    """
    verdicts = operation.verdicts(key)
    decided = operation.decide(key, verdicts)
    vector = tuple(sorted((str(fact), value) for fact, value in decided.items()))

    def build() -> bool | tuple[models.QuerySet[Any], bool]:
        compiler = operation.compiler(verdicts, decided)
        predicate = compiler.holds(key, _model_at(model), Bound.LOWER)
        if is_false(predicate):
            return False
        witness = operation.witness(compiler.used_facts, decided, verdicts)
        if is_false(witness):
            return False
        gate = operation.gate()
        if is_true(predicate):
            return (gate if is_true(witness) else gate.filter(witness)).values("pk"), True
        # The row predicate goes last: it holds the nested subqueries.
        condition = Q(Exists(gate))
        if not is_true(witness):
            condition &= witness
        rows = model._base_manager.using(operation.using).filter(condition & predicate)
        return rows.order_by().values(resource_id_attr(model)), False

    return operation.statement(("scope", model._meta.label_lower, key, vector), verdicts, build)


class _Scope(Expression):
    """The scope of one queryset, decided when its statement is compiled.

    Schema overrides, caveat instances and fixed-object facts can change after
    a queryset was constructed, so nothing is prepared before this point.
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
        super().__init__(output_field=models.BooleanField())
        self.backend = backend
        self.model = model
        self.action = action
        self.actor = actor
        self.using = using
        self.revision = revision
        self.lhs: Any = F(resource_id_attr(model))

    def get_source_expressions(self) -> list[Any]:
        return [self.lhs]

    def set_source_expressions(self, expressions: Any) -> None:
        [self.lhs] = expressions

    @schema_operation
    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        resource_type = model_resource_type(self.model)
        policy = _policy(self.backend)
        if resource_type is None or policy.snapshot.revision != self.revision:
            return "(1 = 0)", ()
        operation = _Operation.begin(self.backend, self.actor, self.using, policy=policy)
        compiled = _scope_statement(operation, self.model, (resource_type, self.action))
        if isinstance(compiled, bool):
            # ``True`` cannot occur: a statement that admits rows is a gate.
            return "(1 = 0)", ()
        params = operation.bound(compiled.params)
        if compiled.every:
            return f"EXISTS {compiled.sql}", tuple(params)
        lhs_sql, lhs_params = compiler.compile(self.lhs)
        return f"({lhs_sql} IN {compiled.sql})", (*lhs_params, *params)


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
    _schema_at_build, snapshot = _schema(backend)
    return Q(_Scope(backend, model, action, actor, using, snapshot.revision))


# ---------- Enumeration ----------


def _named_parts(
    resource_type: str, *, schema: Schema, using: str
) -> Iterable[tuple[models.QuerySet[Any], str]]:
    """Identities of the type that no model row carries, as wire-id querysets.

    They are the ids that tuples name at either end, the fixed targets of
    constants, and the containers of attribute backings.  A foreign key can
    only name an existing row, so a field backing adds ids only when its
    target model keeps no rows.
    """
    rows = cast(Any, active_relationship_model().objects.using(using)).index_projection()
    for type_column, id_column in (
        ("resource_type", "resource_id"),
        ("subject_type", "subject_id"),
    ):
        yield (
            rows.filter(**{type_column: resource_type}).exclude(**{f"{id_column}__in": ("", "*")}),
            id_column,
        )
    one_row = SchemaGeneration.objects.using(using).filter(pk=1)
    for definition in schema.definitions:
        for relation in definition.relations:
            targets_type = bool(
                relation.allowed_subjects and relation.allowed_subjects[0].type == resource_type
            )
            if targets_type and isinstance(relation.backing, ConstBinding):
                fixed = Value(relation.backing.target_id, output_field=models.CharField())
                yield one_row.annotate(_rebac_wire_id=fixed), "_rebac_wire_id"
            if targets_type and isinstance(relation.backing, FieldBinding):
                resolved_field = resolve_field_backing(definition, relation)
                if resolved_field is not None and not stores_rows(resolved_field.target_model):
                    codec = identity_codec(
                        resolved_field.target_model, resolved_field.target_id_attr
                    )
                    yield (
                        resolved_field.queryset(using=using)
                        .order_by()
                        .annotate(_rebac_wire_id=codec.to_wire(resolved_field.target_values_path()))
                        .exclude(_rebac_wire_id__isnull=True),
                        "_rebac_wire_id",
                    )
            if definition.resource_type != resource_type or not isinstance(
                relation.backing, AttributeBinding
            ):
                continue
            attribute = resolve_attribute_backing(definition, relation)
            if attribute is None:
                continue
            if attribute.resource is not None:
                container = Value(attribute.resource, output_field=models.CharField())
                yield one_row.annotate(_rebac_wire_id=container), "_rebac_wire_id"
            else:
                yield (
                    attribute.target_model._base_manager.using(using)
                    .filter(**attribute.filters)
                    .annotate(
                        _rebac_wire_id=identity_codec(
                            attribute.target_model, attribute.field.name
                        ).to_wire(attribute.field.name)
                    )
                    .exclude(_rebac_wire_id__isnull=True),
                    "_rebac_wire_id",
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
    def _branches(self) -> list[tuple[models.QuerySet[Any], Callable[[Any], str | None]]]:
        policy = _policy(self.backend)
        if policy.snapshot.revision != self.revision:
            return []
        operation = _Operation.begin(self.backend, self.actor, self.using, self.context, policy)
        return _accessible_branches(self, operation)

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for rows, wire in self._branches():
            for value in rows.iterator(chunk_size=1000):
                converted = wire(value)
                if converted is not None and converted not in seen:
                    seen.add(converted)
                    yield converted

    def exists(self) -> bool:
        return any(rows.exists() for rows, _wire in self._branches())


def _accessible_branches(
    self: _AccessibleResources, operation: _Operation
) -> list[tuple[models.QuerySet[Any], Callable[[Any], str | None]]]:
    """One queryset per part of the type's universe, with its id conversion."""
    policy = operation.policy
    key = self.resource_type, self.action
    verdicts = operation.verdicts(key)
    compiler = operation.compiler(verdicts, parametric=False)
    gate = Q(Exists(operation.gate(parametric=False)))
    branches: list[tuple[models.QuerySet[Any], Callable[[Any], str | None]]] = []

    model = model_for_resource_type(self.resource_type)
    if model is not None and stores_rows(model):
        identity = resource_id_attr(model)
        codec = identity_codec(model, identity)

        def wire(value: Any) -> str | None:
            try:
                return codec.wire(value, using=self.using)
            except SchemaError:
                return None

        if self.context is None:
            condition = Q(
                _Scope(self.backend, model, self.action, self.actor, self.using, self.revision)
            )
        else:
            condition = gate & compiler.holds(key, _model_at(model), Bound.LOWER)
        rows = model._base_manager.using(self.using).filter(condition)
        branches.append((rows.order_by().values_list(identity, flat=True), wire))
    elif model is None:
        # A configured User or Group mapping has rows but no resource model.
        subject = model_for_subject_type(self.resource_type)
        if subject is not None and stores_rows(subject[0]):
            subject_codec = identity_codec(subject[0], subject[1])
            subject_rows = (
                subject[0]
                ._base_manager.using(self.using)
                .order_by()
                .annotate(_rebac_wire_id=subject_codec.to_wire(subject[1]))
                .exclude(_rebac_wire_id__isnull=True)
            )
            at = At(self.resource_type, F("_rebac_wire_id"), None, False)
            branches.append(
                (
                    subject_rows.filter(gate & compiler.holds(key, at, Bound.LOWER))
                    .order_by()
                    .values_list("_rebac_wire_id", flat=True),
                    lambda value: value or None,
                )
            )
    for named, column in _named_parts(self.resource_type, schema=policy.schema, using=self.using):
        at = At(self.resource_type, F(column), None, False)
        branches.append(
            (
                named.filter(gate & compiler.holds(key, at, Bound.LOWER))
                .order_by()
                .values_list(column, flat=True)
                .distinct(),
                lambda value: value or None,
            )
        )
    return branches


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
def held(
    *,
    backend: LocalBackend,
    resource_type: str,
    action: str,
    actor: SubjectRef,
    ids: Iterable[str],
    using: str,
    context: Mapping[str, Any] | None = None,
) -> set[str]:
    """The ids among ``ids`` on which the actor holds the action.

    Ids that are rows of the type's model are tested by one scoped statement
    per chunk; the others (no row, or not a valid identity) one by one.
    """
    pending = set(ids)
    found: set[str] = set()
    model = model_for_resource_type(resource_type)
    if model is not None and stores_rows(model) and context is None and len(pending) > 4:
        identity = resource_id_attr(model)
        codec = identity_codec(model, identity)
        _, identity_field = model_identity_fields(model, identity)
        native = {
            identity_field.to_python(value): value
            for value in pending
            if codec.is_canonical(value, using=using)
        }
        scope = scope_q(backend=backend, model=model, action=action, actor=actor, using=using)
        rows = model._base_manager.using(using)
        values = list(native)
        for start in range(0, len(values), 500):
            chunk = values[start : start + 500]
            present = set(
                rows.filter(**{f"{identity}__in": chunk}).values_list(identity, flat=True)
            )
            allowed = set(
                rows.filter(**{f"{identity}__in": chunk})
                .filter(scope)
                .values_list(identity, flat=True)
            )
            for value in present:
                pending.discard(native[value])
            found.update(native[value] for value in allowed)
    for value in sorted(pending):
        if _point_result(
            backend=backend,
            resource=ObjectRef(resource_type, value),
            action=action,
            actor=actor,
            context=context,
            using=using,
            lower_only=True,
        )[0]:
            found.add(value)
    return found


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
    """The subjects of a type that hold the permission on one resource.

    Candidates are the subjects that tuples, backed columns and constants
    name on a path from the resource; each is then tested by the lower bound.
    """
    schema, _snapshot = _schema(backend)
    return [
        candidate
        for candidate in named_subjects(
            schema=schema,
            resource=resource,
            action=action,
            subject_type=subject_type,
            using=using,
        )
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
