"""Execute compiled LocalBackend predicates.

A statement is compiled once per policy, permission and actor shape, and kept
as Django's own SQL with placeholders for the actor's id. Permission facts
remain predicates in the statement and are read when it executes.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import batched
from threading import RLock
from typing import TYPE_CHECKING, Any, cast

from django.core.exceptions import EmptyResultSet
from django.db import connections, models
from django.db.models import Exists, Expression, F, Q, Value
from django.db.models.expressions import Col, ColPairs
from django.db.models.fields.related_lookups import RelatedExact
from django.db.models.fields.tuple_lookups import TupleIn
from django.db.models.lookups import Exact, GreaterThanOrEqual, In, IntegerFieldExact, LessThan
from django.db.models.sql.where import WhereNode
from django.utils import timezone

from rebac._id import _NATIVE_INTEGER_FIELDS, model_identity_fields, resource_id_attr
from rebac.codec import identity_codec
from rebac.composition import TaggedComposition, compose_tagged, split_stale_overrides
from rebac.conf import app_settings
from rebac.errors import PermissionDepthExceeded, SchemaError
from rebac.evaluator import _backend_cache_key, _ctx_key, current_evaluator
from rebac.field_backing import resolve_attribute_backing, resolve_field_backing
from rebac.models import active_relationship_model
from rebac.models.generation import SchemaGeneration
from rebac.resources import (
    model_for_resource_type,
    model_for_subject_type,
    model_resource_type,
    stores_rows,
)
from rebac.schema.ast import (
    AttributeBinding,
    ConstBinding,
    FieldBinding,
    PermBinOp,
    PermExpr,
    PermRef,
    Schema,
)
from rebac.schema.cache import SchemaSnapshot, schema_operation
from rebac.schema.walker import find_permission, find_relation
from rebac.types import CheckItem, CheckResult, ObjectRef, SubjectRef

from . import At, Bound, Compiler, ctes
from .conditions import CaveatVerdicts
from .ctes import _Marker
from .evaluate import named_subjects, residual
from .predicate import (
    PointRow,
    _and,
    _Param,
    actor_shape,
    bind,
    clock,
    is_false,
    is_true,
)
from .program import CompileProgram, Key

if TYPE_CHECKING:
    from rebac.backends.local import LocalBackend

_MANUAL_REVISION = _Marker("manual schema revision")
_RESOURCE_WIRE = _Marker("resource wire id")
_LIMIT = 512
# The most literal identities a scoped read is bounded to; a longer list uses
# the general scope (the size crossover, and the vendors' bind limits).
_CANDIDATE_LIMIT = 256
# Items of a bulk check compiled together, and the SQL text one statement of
# their bounds may reach before it is cut.
_BULK = 50
_BULK_SQL = 64_000
_lock = RLock()


@dataclass(frozen=True, slots=True)
class _Kept:
    """One compiled statement.  ``every`` marks a gate that admits every row.

    ``reads`` are the resource types whose facts a decision of the statement
    depends on, for the decision cache (``_constant_witness``).
    """

    sql: str
    params: tuple[Any, ...]
    every: bool = False
    reads: tuple[str, ...] = ()
    body: str | None = None
    definitions: tuple[ctes.Definition, ...] = ()

    def render(self, bound: Iterable[Any]) -> tuple[str, tuple[Any, ...]]:
        values = tuple(bound)
        if not self.definitions:
            return self.sql, values
        definitions = []
        offset = 0
        for item in self.definitions:
            stop = offset + len(item.params)
            definitions.append(
                ctes.Definition(item.name, item.sql, values[offset:stop], item.template)
            )
            offset = stop
        if ctes.register(tuple(definitions)):
            assert self.body is not None
            return f"({self.body})", values[offset:]
        return self.sql, values


_policies: OrderedDict[tuple[Any, ...], _Policy] = OrderedDict()
_statements: OrderedDict[tuple[Any, ...], _Kept | bool] = OrderedDict()


def reset() -> None:
    """Forget every kept policy and statement (settings or registries changed)."""
    with _lock:
        _policies.clear()
        _statements.clear()
        _generic_columns.clear()
    evaluator = current_evaluator()
    if evaluator is not None:
        # The open scope's pinned policy may predate this invalidation.
        evaluator._schema_scope.clear()


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

    @property
    def fence(self) -> tuple[tuple[datetime, bool], ...]:
        """Each deadline the statement witnesses, and whether it lies ahead."""
        return tuple((deadline, self.selected_at < deadline) for deadline in self.caveat_deadlines)


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
    selected_at = timezone.now()
    with _lock:
        kept = _policies.get(key)
        if (
            kept is not None
            and kept.snapshot.schema is snapshot.schema
            # A kept policy has only passed deadlines: it holds while the
            # clock stays past them.
            and all(selected_at >= deadline for deadline in kept.caveat_deadlines)
        ):
            _policies.move_to_end(key)
            return kept
    overrides, _stale = split_stale_overrides(baseline, list(snapshot.overrides))
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
    # Every override deadline still ahead closes a prepared scope: a scope pinned
    # before it selects nothing after it, until it is built again with the new
    # policy. A passed deadline is settled in the composition, except that a
    # recaveat chose its expression by the application clock, so the statement
    # witnesses that the database clock has passed it too.
    ahead = {
        row.expires_at
        for row in overrides
        if row.expires_at is not None and row.expires_at > selected_at
    }
    chosen = {
        row.expires_at
        for row in overrides
        if row.kind == SchemaOverride.KIND_RECAVEAT
        and row.expires_at is not None
        and row.expires_at <= selected_at
    }
    deadlines = tuple(sorted(ahead | chosen))
    policy = _Policy(
        key,
        snapshot,
        tagged,
        compose_tagged(baseline, active).schema,
        CompileProgram.build(tagged.schema),
        selected_at,
        deadlines,
    )
    if not ahead:
        # A deadline ahead will change a prebuilt scope's composition, so a
        # policy with one is prepared afresh at each construction boundary.
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
        clock() if parametric else Value(ctes.statement_now(), output_field=models.DateTimeField())
    )
    for deadline, ahead in policy.fence:
        at = Value(deadline, output_field=models.DateTimeField())
        rows = rows.filter(LessThan(now, at) if ahead else GreaterThanOrEqual(now, at))
    return rows


# ---------- Kept statements ----------


class _Sql(Expression):
    """``EXISTS`` over kept SQL, bound inside its executing statement's collector."""

    def __init__(self, kept: _Kept, operation: _Operation, resource_id: str | None = None) -> None:
        super().__init__(output_field=models.BooleanField())
        self.kept = kept
        self.operation = operation
        self.resource_id = resource_id

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        sql, params = self.kept.render(self.operation.bound(self.kept.params, self.resource_id))
        return f"EXISTS {sql}", params


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
                # A statement's fence carries the policy's deadlines: a policy
                # selected on the other side of one compiles its own statement.
                policy.fence,
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

    def adopt(self, key: Key, verdicts: CaveatVerdicts) -> None:
        """Use verdicts prepared by another operation for the same key and context."""
        self._verdicts[key] = verdicts

    def compiler(
        self,
        verdicts: CaveatVerdicts,
        *,
        parametric: bool = True,
        point: PointRow | None = None,
        fold: Key | None = None,
    ) -> Compiler:
        return Compiler(
            self.policy.schema,
            self.actor,
            self.using,
            tagged=self.policy.tagged,
            verdicts=verdicts,
            program=self.policy.program,
            parametric=parametric,
            point=point,
            fold=fold,
        )

    def gate(self, *, parametric: bool = True) -> models.QuerySet[Any]:
        return _fence(self.policy, backend=self.backend, using=self.using, parametric=parametric)

    def statement(
        self,
        key: tuple[Any, ...],
        verdicts: CaveatVerdicts,
        build: Callable[
            [],
            models.QuerySet[Any]
            | bool
            | tuple[models.QuerySet[Any], bool]
            | tuple[models.QuerySet[Any], bool, tuple[str, ...]],
        ],
        *,
        cache: bool = True,
    ) -> _Kept | bool:
        """Compile SQL, kept when its only varying values are bound parameters.

        ``build`` returns the statement's root (``Compiler.statement``).
        ``cache`` is off for a statement that embeds per-read literals.
        """
        # A statement can be kept when nothing in it varies but the actor's
        # id and the clock: that is, when it carries no caveat verdict list.
        keep = cache and verdicts.empty
        full = (*self.shape, *key)
        if keep:
            with _lock:
                kept = _statements.get(full)
                if kept is not None:
                    _statements.move_to_end(full)
                    return kept
        # Building a template is not part of the SQL statement currently
        # embedding it. In particular, its actor placeholders must not become
        # that outer statement's CTE definitions.
        with ctes.isolated():
            built = build()
        compiled: _Kept | bool
        if isinstance(built, bool):
            compiled = built
        else:
            rows, every, *reads = built if isinstance(built, tuple) else (built, False)
            query = rows.query.clone()
            # Parenthesised, to embed; a set of ids has no order.
            query.subquery = True
            query.clear_ordering(force=True)
            compiler = query.get_compiler(using=self.using)
            try:
                with ctes.collecting() as collected:
                    body, body_params = compiler.as_sql()
                definitions = tuple(collected.definitions.values())
                sql, params = ctes.assemble(
                    (body, tuple(body_params)), definitions, compiler.connection
                )
            except EmptyResultSet:
                # No row can satisfy it: an unpublished policy, or a
                # condition Django proves empty.
                compiled = False
            else:
                compiled = _Kept(
                    f"({sql})",
                    tuple(params),
                    every,
                    reads[0] if reads else (),
                    body=body if definitions else None,
                    definitions=definitions,
                )
        if keep:
            with _lock:
                _statements[full] = compiled
                _statements.move_to_end(full)
                while len(_statements) > _LIMIT:
                    _statements.popitem(last=False)
        return compiled

    def bound(self, params: Iterable[Any], resource_id: str | None = None) -> list[Any]:
        result: list[Any] = []
        for param in bind(params, self.actor, connections[self.using]):
            if param is _MANUAL_REVISION:
                result.append(self.backend._manual_schema_revision())
            elif param is _RESOURCE_WIRE:
                result.append(resource_id)
            else:
                result.append(param)
        return result


def _model_at(model: type[models.Model]) -> At:
    identity = resource_id_attr(model)
    _, identity_field = model_identity_fields(model, identity)
    resource_type = model_resource_type(model)
    if resource_type is None:
        raise ValueError(f"{model.__name__} has no REBAC resource type")
    return At(resource_type, F(identity), identity_field, True)


# ---------- Constant arms ----------


def _constant_statement(operation: _Operation, key: Key) -> _Kept | bool:
    """The policy fence where one of ``key``'s constant arms holds, as SQL.

    ``False`` when ``key`` has none to witness (``Compiler.constant_arms``).
    When it holds, the actor holds ``key`` on every object of its type, so it
    is kept as a gate that admits every row; its ``reads`` are the types the
    constant arms name.
    """
    verdicts = operation.verdicts(key)

    def build() -> bool | tuple[models.QuerySet[Any], bool, tuple[str, ...]]:
        compiler = operation.compiler(verdicts)
        condition = compiler.constant(key)
        if is_false(condition) or is_true(condition):
            # Nothing to witness: a constant arm that holds outright makes the
            # policy's own statement the fence alone.
            return False
        rows = compiler.statement(operation.gate().filter(condition).values("pk"))
        return rows, True, compiler.constant_types(key)

    return operation.statement(("constant", key), verdicts, build)


def _probe_root(using: str) -> models.QuerySet[SchemaGeneration]:
    """A probe executes its own statement, even during another one's compilation."""
    return ctes.share_id_sets(SchemaGeneration.objects.using(using).filter(pk=1))


def _constant_witness(
    operation: _Operation, key: Key, *, recheck: bool = False, fresh: bool = False
) -> bool | None:
    """Whether the actor holds one of ``key``'s constant arms, inside an evaluator scope.

    The evaluator remembers the answer for the scope
    (``PermissionEvaluator._constant_witness``):

    - ``True`` when the actor holds one, decided by the witness statement now,
      or remembered where the decision cache may keep a decision about every
      type the arms name (``rebac.evaluator._backend_cache_key``), ``fresh``
      (a per-call consistency requirement) is not set and ``recheck`` is not.
      It only chooses a statement that evaluates the arms again when it runs.
    - ``False`` when it holds none and the answer may be kept under the same
      rules: a scope may then leave the arms out (``_scope_statement``).
    - ``None`` when it holds none but the answer may not be kept: the policy's
      statement, which evaluates the arms itself, is read, and a remembered
      ``None`` costs no statement. Also outside an evaluator scope, under a
      caveat context, and when ``key`` has no constant arm beside its other
      arms.
    """
    evaluator = current_evaluator()
    if evaluator is None or operation.context is not None:
        return None
    compiled = _constant_statement(operation, key)
    if isinstance(compiled, bool):
        return None

    def decide() -> bool:
        probe = _Sql(compiled, operation)
        return _probe_root(operation.using).filter(Q(probe)).exists()

    generations = tuple(_backend_cache_key(operation.backend, type_) for type_ in compiled.reads)
    kept = None if fresh or None in generations else generations
    return evaluator._constant_witness(
        operation.backend,
        # The same SQL decides on each database alias it runs on.
        (compiled.sql, compiled.params, operation.actor, operation.using),
        kept,
        decide,
        recheck=recheck,
    )


# ---------- Point checks ----------


def _point_statement(
    operation: _Operation,
    key: Key,
    which: str,
    row_types: RowTypes | None = None,
) -> _Kept | bool:
    """One bound of ``key`` at one identity, as SQL; a bool when it needs no rows.

    ``row_types`` are the row's GenericForeignKey content types, when read
    (``_row_types``): the statement is compiled and kept for them, with only
    the arms of those types, and re-reads them.
    """
    verdicts = operation.verdicts(key)

    def build() -> models.QuerySet[Any] | bool:
        at = At(key[0], _Param(_RESOURCE_WIRE, models.TextField()), None, False)
        point = PointRow(at.ref, row_types) if row_types is not None else None
        compiler = operation.compiler(verdicts, point=point)
        if which == "depth":
            condition = compiler.depth_unknown(key, at)
        else:
            condition = compiler.holds(key, at, Bound(which))
        if is_false(condition):
            return False
        if point is not None:
            condition = _and(condition, compiler.point_witness(at))
            if is_false(condition):
                return False
        rows = operation.gate()
        if is_true(condition):
            return rows.values("pk")
        return compiler.statement(rows.filter(condition).values("pk"))

    return operation.statement(("point", key, which, row_types), verdicts, build)


# A point row's content types: each content-type column's attname, with its value.
RowTypes = tuple[tuple[str, Any], ...]

# The content-type columns that prune a permission at a point row, by policy
# and permission (``_type_columns``).
_generic_columns: OrderedDict[tuple[Any, ...], tuple[str, ...]] = OrderedDict()


def _type_columns(policy: _Policy, key: Key) -> tuple[str, ...]:
    """The content-type columns of the GenericForeignKeys that back the
    relations of ``key``'s type which ``key`` reaches; none when it reaches
    none, and the row's types are then not read."""
    memo = (policy.key, key)
    with _lock:
        columns = _generic_columns.get(memo)
    if columns is not None:
        return columns
    definition = policy.schema.get_definition(key[0])
    reached = policy.program.reachable(key)
    found: set[str] = set()
    for relation in definition.relations if definition is not None else ():
        if (key[0], relation.name) not in reached or not isinstance(relation.backing, FieldBinding):
            continue
        resolved = resolve_field_backing(definition, relation)  # type: ignore[arg-type]
        if resolved is not None and resolved.generic is not None:
            field = resolved.source_model._meta.get_field(resolved.generic.ct_field)
            found.add(cast(Any, field).attname)
    columns = tuple(sorted(found))
    with _lock:
        _generic_columns[memo] = columns
        while len(_generic_columns) > _LIMIT:
            _generic_columns.popitem(last=False)
    return columns


def _row_types(
    using: str, resource_type: str, columns: tuple[str, ...], ids: Iterable[str]
) -> dict[str, RowTypes]:
    """The values the rows ``ids`` name have in ``columns``, read by one
    statement per 500 ids; an id that names no row has none."""
    model = model_for_resource_type(resource_type)
    if not columns or model is None or not stores_rows(model):
        return {}
    identity = resource_id_attr(model)
    codec = identity_codec(model, identity)
    _, identity_field = model_identity_fields(model, identity)
    native = {
        identity_field.to_python(value): value
        for value in ids
        if value and codec.is_canonical(value, using=using)
    }
    rows = model._base_manager.using(using)
    values = list(native)
    found: dict[str, RowTypes] = {}
    for start in range(0, len(values), 500):
        chunk = values[start : start + 500]
        for value, *row in rows.filter(**{f"{identity}__in": chunk}).values_list(
            identity, *columns
        ):
            found[native[value]] = tuple(zip(columns, row, strict=True))
    return found


def _key_types(policy: _Policy, using: str, key: Key, ids: Iterable[str]) -> dict[str, RowTypes]:
    """The content types that prune ``key`` on the rows ``ids`` name."""
    return _row_types(using, key[0], _type_columns(policy, key), ids)


def _point(
    operation: _Operation,
    key: Key,
    resource_id: str,
    which: str,
    row_types: RowTypes | None = None,
) -> bool:
    """One bound of ``key`` at one identity, by one statement."""
    compiled = _point_statement(operation, key, which, row_types)
    if isinstance(compiled, bool):
        return compiled and operation.gate(parametric=False).exists()
    probe = _Sql(compiled, operation, resource_id)
    return _probe_root(operation.using).filter(Q(probe)).exists()


def _points(
    asked: Sequence[tuple[_Operation, Key, str]],
    which: str,
    types: Sequence[RowTypes | None],
) -> list[bool]:
    """One bound of several items: each a column of one statement over the generation row.

    A column is the statement ``_point`` would run for the item, with its own
    fence and witness; ``types`` are the items' row types, in order.
    """
    answers = [False] * len(asked)
    statements: list[dict[str, _Sql]] = [{}]
    size = 0
    parameter_count = 1  # The generation-row primary key in the outer query.
    parameter_limit = connections[asked[0][0].using].features.max_query_params if asked else None
    fenced: bool | None = None
    for position, (operation, key, resource_id) in enumerate(asked):
        compiled = _point_statement(operation, key, which, types[position])
        if isinstance(compiled, bool):
            if compiled:
                if fenced is None:
                    fenced = operation.gate(parametric=False).exists()
                answers[position] = fenced
        else:
            parameters = len(compiled.params)
            if statements[-1] and (
                size + len(compiled.sql) > _BULK_SQL
                or (parameter_limit is not None and parameter_count + parameters > parameter_limit)
            ):
                statements.append({})
                size = 0
                parameter_count = 1
            size += len(compiled.sql)
            parameter_count += parameters
            statements[-1][f"item{position}"] = _Sql(compiled, operation, resource_id)
    rows = _probe_root(asked[0][0].using) if asked else None
    for probes in statements:
        if not probes or rows is None:
            continue
        for row in rows.annotate(**probes).values(*probes):
            for name, held in row.items():
                answers[int(name.removeprefix("item"))] = bool(held)
    return answers


def _recursive(operation: _Operation, key: Key) -> bool:
    """Whether a recursion in reach of ``key`` leaves a depth to probe."""
    program = operation.policy.program
    return bool(program.reachable(key) & program.recursive)


def _point_result(
    *,
    backend: LocalBackend,
    resource: ObjectRef,
    action: str,
    actor: SubjectRef,
    context: Mapping[str, Any] | None,
    using: str,
    lower_only: bool = False,
    types: Mapping[str, RowTypes] | None = None,
    witness: bool = False,
    fresh: bool = False,
) -> tuple[bool, bool, bool, _Policy]:
    """``types`` are the row types already read for the resource's id
    (``_key_types``); without them, they are read here. ``witness`` decides
    the permission's constant arms first (``_constant_witness``); ``fresh``
    keeps nothing of that decision."""
    operation = _Operation.begin(backend, actor, using, context)
    key = resource.resource_type, action
    if witness and _constant_witness(operation, key, recheck=True, fresh=fresh):
        # Decided by its own statement just now: a lower bound.
        return True, True, False, operation.policy
    if types is None:
        types = _key_types(operation.policy, using, key, (resource.resource_id,))
    row_types = types.get(resource.resource_id)
    # Only LOWER may authorize. Subsequent probes can only return NO,
    # CONDITIONAL, or a depth error.
    if _point(operation, key, resource.resource_id, "lower", row_types):
        return True, True, False, operation.policy
    recursive = _recursive(operation, key)
    # The bounds differ only where a caveat or a recursion is in reach.
    if lower_only or not (recursive or not operation.verdicts(key).empty):
        return False, False, False, operation.policy
    if not _point(operation, key, resource.resource_id, "upper", row_types):
        return False, False, False, operation.policy
    deep = recursive and _point(operation, key, resource.resource_id, "depth", row_types)
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
    fresh: bool = False,
) -> CheckResult:
    """The three-state answer of one point check.

    ``fresh`` is set for a call with an explicit consistency requirement: the
    evaluator keeps nothing of it (``_constant_witness``).
    """
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
        witness=True,
        fresh=fresh,
    )
    return _answer(
        lower,
        upper,
        deep,
        policy=policy,
        resource=resource,
        action=action,
        actor=actor,
        context=context,
        using=using,
    )


def _answer(
    lower: bool,
    upper: bool,
    deep: bool,
    *,
    policy: _Policy,
    resource: ObjectRef,
    action: str,
    actor: SubjectRef,
    context: Mapping[str, Any] | None,
    using: str,
) -> CheckResult:
    """The three-state answer of one point check from its bounds."""
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


# ---------- Bulk checks ----------


@schema_operation
def check_many(
    *, backend: LocalBackend, items: Sequence[CheckItem], using: str
) -> list[CheckResult]:
    """What ``check`` answers for each item, from statements the items share."""
    schema, _snapshot = _schema(backend)
    answers: dict[int, CheckResult] = {}
    points: list[int] = []
    for index, item in enumerate(items):
        resource = item.resource
        definition = schema.get_definition(resource.resource_type)
        if (
            definition is not None
            and resource.resource_id
            and (
                schema.get_permission(resource.resource_type, item.action) is not None
                or find_relation(definition, item.action) is not None
            )
        ):
            points.append(index)
        else:
            answers[index] = check(
                backend=backend,
                resource=resource,
                action=item.action,
                actor=item.subject,
                context=item.context,
                using=using,
            )
    if points:
        policy = _policy(backend)
        for chunk in batched(points, _BULK, strict=False):
            found = _check_chunk(backend, policy, [items[index] for index in chunk], using)
            answers.update(zip(chunk, found, strict=True))
    return [answers[index] for index in range(len(items))]


def _check_chunk(
    backend: LocalBackend, policy: _Policy, items: Sequence[CheckItem], using: str
) -> list[CheckResult]:
    """The point checks of up to ``_BULK`` items, by three statements at most."""
    operations: dict[tuple[SubjectRef, Hashable], _Operation] = {}
    prepared: dict[tuple[Key, Hashable], CaveatVerdicts] = {}
    asked: list[tuple[_Operation, Key, str]] = []
    for item in items:
        context = _ctx_key(dict(item.context)) if item.context else ()
        operation = operations.get((item.subject, context)) if context is not None else None
        if operation is None:
            operation = _Operation.begin(backend, item.subject, using, item.context, policy)
            if context is not None:
                operations[item.subject, context] = operation
        key = item.resource.resource_type, item.action
        if context is not None and key not in operation._verdicts:
            # Verdicts depend on the permission and the context, not on the actor.
            if (key, context) in prepared:
                operation.adopt(key, prepared[key, context])
            else:
                prepared[key, context] = operation.verdicts(key)
        asked.append((operation, key, item.resource.resource_id))
    # The row types every bound of the chunk compiles with: one read per
    # resource type and set of columns.
    wanted: dict[tuple[str, tuple[str, ...]], set[str]] = {}
    for _operation, key, resource_id in asked:
        columns = _type_columns(policy, key)
        if columns:
            wanted.setdefault((key[0], columns), set()).add(resource_id)
    read = {group: _row_types(using, group[0], group[1], ids) for group, ids in wanted.items()}
    types = [
        read.get((key[0], _type_columns(policy, key)), {}).get(resource_id)
        for _operation, key, resource_id in asked
    ]
    lower = _points(asked, "lower", types)
    # The bounds differ only where a caveat or a recursion is in reach.
    open_: list[tuple[int, bool]] = []
    for position, (operation, key, _resource_id) in enumerate(asked):
        if lower[position]:
            continue
        recursive = _recursive(operation, key)
        if recursive or not operation.verdicts(key).empty:
            open_.append((position, recursive))
    upper = dict(
        zip(
            (position for position, _recursive_ in open_),
            _points(
                [asked[position] for position, _recursive_ in open_],
                "upper",
                [types[position] for position, _recursive_ in open_],
            ),
            strict=True,
        )
    )
    probed = [position for position, recursive in open_ if recursive and upper[position]]
    deep = dict(
        zip(
            probed,
            _points(
                [asked[position] for position in probed],
                "depth",
                [types[position] for position in probed],
            ),
            strict=True,
        )
    )
    return [
        _answer(
            lower[position],
            lower[position] or upper.get(position, False),
            deep.get(position, False),
            policy=policy,
            resource=item.resource,
            action=item.action,
            actor=item.subject,
            context=item.context,
            using=using,
        )
        for position, item in enumerate(items)
    ]


# ---------- Scopes ----------


def _scope_statement(
    operation: _Operation,
    model: type[models.Model],
    key: Key,
    *,
    candidates: tuple[int, ...] | None = None,
    witness: bool | None = None,
) -> _Kept | bool:
    """The ids of ``model`` the actor holds ``key`` on, as SQL.

    ``False`` when there are none.  ``every`` is set when every row qualifies
    and the SQL is only the policy fence. ``candidates`` bounds the rows the
    statement visits to those literal identities. ``witness`` is the answer
    of ``_constant_witness``: when ``True`` the statement is the constant
    arms alone; when ``False`` it is the policy's without them.
    """
    if witness:
        return _constant_statement(operation, key)
    verdicts = operation.verdicts(key)
    label = model._meta.label_lower
    fold = key if witness is False else None

    def build() -> bool | tuple[models.QuerySet[Any], bool]:
        compiler = operation.compiler(verdicts, fold=fold)
        predicate = compiler.holds(key, _model_at(model), Bound.LOWER)
        if is_false(predicate):
            return False
        gate = operation.gate()
        if is_true(predicate):
            return gate.values("pk"), True
        rows = model._base_manager.using(operation.using)
        if candidates is not None:
            rows = rows.filter(**{f"{resource_id_attr(model)}__in": candidates})
        # The row predicate goes last: it holds the nested subqueries.
        rows = rows.filter(Q(Exists(gate)) & predicate)
        return compiler.statement(rows.order_by().values(resource_id_attr(model))), False

    return operation.statement(
        ("scope", label, key, fold is not None), verdicts, build, cache=candidates is None
    )


def _known_identities(
    model: type[models.Model], lhs: Any, where: Any, using: str
) -> tuple[int, ...] | None:
    """Native literal identities mandatory in this resolved query, or no proof.

    Only positive AND paths are traversed. Unknown fields, lookups and expression
    shapes keep the general scope; the original Django predicates are untouched.
    """
    if type(lhs) is not Col:
        return None
    identity = resource_id_attr(model)
    field, scalar = model_identity_fields(model, identity)
    if type(scalar) not in _NATIVE_INTEGER_FIELDS:
        return None
    if field is not scalar and (
        type(field) not in (models.ForeignKey, models.OneToOneField)
        or cast(models.ForeignKey[Any, Any], field).target_field is not scalar
    ):
        return None
    if not field.unique or lhs.target is not field or lhs.output_field is not scalar:
        return None
    codec = identity_codec(model, identity)
    pending = [where]
    while pending:
        node = pending.pop()
        if type(node) is WhereNode:
            if node.connector == "AND" and not node.negated:
                pending.extend(node.children)
            continue
        if type(node) not in (Exact, IntegerFieldExact, RelatedExact, In, TupleIn):
            continue
        column = node.lhs
        value = node.rhs
        if type(node) is TupleIn:
            if (
                type(column) is not ColPairs
                or len(column.targets) != 1
                or len(column.sources) != 1
                or type(value) not in (list, tuple)
                or len(value) != 1
            ):
                continue
            [value] = value
            if type(value) not in (list, tuple) or len(value) != 1:
                continue
            [value] = value
            [column] = column.get_cols()
        if (
            type(column) is not Col
            or column.alias != lhs.alias
            or column.target is not field
            or (column.output_field is not field and column.output_field is not scalar)
        ):
            continue
        if type(node) is In:
            if type(value) not in (list, tuple) or not 0 < len(value) <= _CANDIDATE_LIMIT:
                continue
            values = value
        else:
            values = (value,)
        if any(type(item) is not int for item in values) or len(set(values)) != len(values):
            continue
        try:
            for item in values:
                codec.wire(item, using=using)
        except SchemaError:
            continue
        return tuple(values)
    return None


class _Scope(Expression):
    """A queryset scope with policy, caveat verdicts and constant-arm witness pinned when built.

    ``witness`` is the answer of ``_constant_witness`` when the scope was built.
    """

    def __init__(
        self,
        backend: LocalBackend,
        model: type[models.Model],
        action: str,
        actor: SubjectRef,
        using: str,
        policy: _Policy,
        verdicts: CaveatVerdicts,
        witness: bool | None = None,
    ) -> None:
        super().__init__(output_field=models.BooleanField())
        self.backend = backend
        self.model = model
        self.action = action
        self.actor = actor
        self.using = using
        self.policy = policy
        self.verdicts = verdicts
        self.witness = witness
        self.lhs: Any = F(resource_id_attr(model))

    def get_source_expressions(self) -> list[Any]:
        return [self.lhs]

    def set_source_expressions(self, exprs: Any) -> None:
        [self.lhs] = exprs

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        resource_type = model_resource_type(self.model)
        if resource_type is None:
            return "(1 = 0)", ()
        key = (resource_type, self.action)
        operation = _Operation.begin(self.backend, self.actor, self.using, policy=self.policy)
        operation.adopt(key, self.verdicts)
        # A query that names the identities it reads proves access over
        # exactly those rows; every other shape uses the general scope.
        identities = _known_identities(self.model, self.lhs, compiler.query.where, self.using)
        compiled = _scope_statement(
            operation, self.model, key, candidates=identities, witness=self.witness
        )
        if isinstance(compiled, bool):
            # ``True`` cannot occur: a statement that admits rows is a gate.
            return "(1 = 0)", ()
        sql, params = compiled.render(operation.bound(compiled.params))
        if compiled.every:
            return f"EXISTS {sql}", params
        lhs_sql, lhs_params = compiler.compile(self.lhs)
        return f"({lhs_sql} IN {sql})", (*lhs_params, *params)


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
    return Q(
        scope_expression(backend=backend, model=model, action=action, actor=actor, using=using)
    )


@schema_operation
def scope_expression(
    *,
    backend: LocalBackend,
    model: type[models.Model],
    action: str,
    actor: SubjectRef,
    using: str,
    identity: Expression | None = None,
) -> Expression:
    """The Boolean scope of ``model`` rows, its policy, caveat verdicts and
    constant-arm witness pinned now.

    ``identity`` replaces the scope's own identity column, for a caller that
    resolved it through another query path.
    """
    resource_type = model_resource_type(model)
    if resource_type is None:
        return Value(False, output_field=models.BooleanField())
    key = (resource_type, action)
    operation = _Operation.begin(backend, actor, using, policy=_policy(backend))
    expression = _Scope(
        backend,
        model,
        action,
        actor,
        using,
        operation.policy,
        operation.verdicts(key),
        _constant_witness(operation, key),
    )
    if identity is not None:
        expression.set_source_expressions([identity])
    return expression


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
    rows = cast(Any, active_relationship_model().objects.using(using)).wire_projection()
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
    """A lazy enumeration request, prepared afresh at each consumption.

    ``fresh`` is set for a call with an explicit consistency requirement
    (``_constant_witness``).
    """

    backend: LocalBackend
    resource_type: str
    action: str
    actor: SubjectRef
    using: str
    context: Mapping[str, Any] | None
    revision: str | None
    fresh: bool = False

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
                _Scope(
                    self.backend,
                    model,
                    self.action,
                    self.actor,
                    self.using,
                    policy,
                    verdicts,
                    _constant_witness(operation, key, fresh=self.fresh),
                )
            )
        else:
            condition = gate & compiler.holds(key, _model_at(model), Bound.LOWER)
        rows = model._base_manager.using(self.using).filter(condition)
        branches.append(
            (compiler.statement(rows.order_by().values_list(identity, flat=True)), wire)
        )
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
                    compiler.statement(
                        subject_rows.filter(gate & compiler.holds(key, at, Bound.LOWER))
                        .order_by()
                        .values_list("_rebac_wire_id", flat=True)
                    ),
                    lambda value: value or None,
                )
            )
    for named, column in _named_parts(self.resource_type, schema=policy.schema, using=self.using):
        at = At(self.resource_type, F(column), None, False)
        branches.append(
            (
                compiler.statement(
                    named.filter(gate & compiler.holds(key, at, Bound.LOWER))
                    .order_by()
                    .values_list(column, flat=True)
                    .distinct()
                ),
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
    fresh: bool = False,
) -> _AccessibleResources:
    _schema_now, snapshot = _schema(backend)
    return _AccessibleResources(
        backend, resource_type, action, actor, using, context, snapshot.revision, fresh
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
    """Whether the action holds on every row of the type.

    It holds at the empty id, which no row-specific fact names.  That proves
    it for every row only while such facts can only add to it: an exclusion
    on the row itself can take it away from one row, so it never holds for all.
    """
    schema, _snapshot = _schema(backend)
    if _excludes_on_the_row(schema, resource_type, action):
        return False
    return _point_result(
        backend=backend,
        resource=ObjectRef(resource_type, ""),
        action=action,
        actor=actor,
        context=context,
        using=using,
        lower_only=True,
    )[0]


def _excludes_on_the_row(schema: Schema, resource_type: str, action: str) -> bool:
    """Whether the permission, or one it names on the same type, has an exclusion.

    An arrow is evaluated on another object; an exclusion there takes nothing
    from this row that the arrow's own row-specific relation does not decide.
    """
    definition = schema.get_definition(resource_type)
    if definition is None:
        return False
    seen: set[str] = set()

    def visit(name: str) -> bool:
        if name in seen:
            return False
        seen.add(name)
        permission = find_permission(definition, name)
        return permission is not None and excludes(permission.expression)

    def excludes(expression: PermExpr) -> bool:
        if isinstance(expression, PermBinOp):
            return expression.op == "-" or excludes(expression.left) or excludes(expression.right)
        if isinstance(expression, PermRef):
            return visit(expression.name)
        return False

    return visit(action)


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

    Stored-row candidates share one scoped statement per chunk. Canonical ids
    absent from stored rows and ids of a type without rows share a bulk point
    check. A depth-uncertain id counts as not held instead of raising as
    ``check_access`` does.
    """
    return _held(
        backend=backend,
        resource_type=resource_type,
        action=action,
        actor=actor,
        ids=ids,
        using=using,
        context=context,
    )


@schema_operation
def held_many(
    *,
    backend: LocalBackend,
    resource_type: str,
    actions: Iterable[str],
    actor: SubjectRef,
    ids: Iterable[str],
    using: str,
    context: Mapping[str, Any] | None = None,
) -> dict[str, set[str]]:
    """The ids among ``ids`` the actor holds, per action, deciding equal permissions once.

    A field gate is often an alias of a shared permission: actions whose
    expressions are equal share one decision. Only an unmodified policy shares;
    an override carries action-specific sites and deadlines.
    """
    if isinstance(actions, str):
        raise TypeError("actions must be an iterable of names, not a bare string")
    names = tuple(dict.fromkeys(actions))
    candidates = tuple(dict.fromkeys(ids))
    if not names:
        return {}
    if not candidates:
        return {name: set() for name in names}
    policy = _policy(backend)
    shared = not policy.tagged.arms and not policy.tagged.sites
    decisions: dict[object, set[str]] = {}
    answers: dict[str, set[str]] = {}
    for name in names:
        permission = policy.tagged.schema.get_permission(resource_type, name)
        key: object = (
            ("expression", permission.expression)
            if permission is not None and shared
            else ("action", name)
        )
        if key not in decisions:
            decisions[key] = set(
                _held(
                    backend=backend,
                    resource_type=resource_type,
                    action=name,
                    actor=actor,
                    ids=candidates,
                    using=using,
                    context=context,
                )
            )
        answers[name] = set(decisions[key])
    return answers


def _held(
    *,
    backend: LocalBackend,
    resource_type: str,
    action: str,
    actor: SubjectRef,
    ids: Iterable[str],
    using: str,
    context: Mapping[str, Any] | None,
) -> set[str]:
    pending = set(ids)
    pending.discard("")
    if not pending:
        return set()
    found: set[str] = set()
    model = model_for_resource_type(resource_type)
    if model is not None and stores_rows(model):
        identity = resource_id_attr(model)
        codec = identity_codec(model, identity)
        _, identity_field = model_identity_fields(model, identity)
        native = {
            identity_field.to_python(value): value
            for value in pending
            if codec.is_canonical(value, using=using)
        }
        if not native:
            return set()
        pending = set(native.values())
        if context is None:
            # One statement per chunk: each candidate row with its verdict.
            rows = model._base_manager.using(using).order_by()
            values = list(native)
            scope = scope_q(backend=backend, model=model, action=action, actor=actor, using=using)
            verdict = models.Case(
                models.When(scope, then=models.Value(True)),
                default=models.Value(False),
                output_field=models.BooleanField(),
            )
            for start in range(0, len(values), 500):
                chunk = values[start : start + 500]
                for value, allowed in rows.filter(**{f"{identity}__in": chunk}).values_list(
                    identity, verdict
                ):
                    pending.discard(native[value])
                    if allowed:
                        found.add(native[value])
    if not pending:
        return found
    # Ids that name no row, and every id under a caveat context, are checked
    # in bulk, as check_access answers them.
    candidates = sorted(pending)
    items = [
        CheckItem(actor, action, ObjectRef(resource_type, value), context) for value in candidates
    ]
    try:
        answers = check_many(backend=backend, items=items, using=using)
    except PermissionDepthExceeded:
        # A bulk check raises for its first deep item with no other answer:
        # each item is checked alone, and a deep one is not held.
        for item in items:
            try:
                answer = check_many(backend=backend, items=[item], using=using)[0]
            except PermissionDepthExceeded:
                continue
            if answer.allowed:
                found.add(item.resource.resource_id)
        return found
    found.update(value for value, answer in zip(candidates, answers, strict=True) if answer.allowed)
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
    candidates = list(
        named_subjects(
            schema=schema,
            resource=resource,
            action=action,
            subject_type=subject_type,
            using=using,
        )
    )
    if not candidates:
        return []
    # One read of the resource's content types serves every candidate.
    types = _key_types(
        _policy(backend), using, (resource.resource_type, action), (resource.resource_id,)
    )
    return [
        candidate
        for candidate in candidates
        if _point_result(
            backend=backend,
            resource=resource,
            action=action,
            actor=candidate,
            context=context,
            using=using,
            lower_only=True,
            types=types,
        )[0]
    ]
