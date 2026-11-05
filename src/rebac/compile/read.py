"""Execute compiled LocalBackend predicates.

A statement is compiled from a policy, permission and actor shape into
Django SQL. Queryset scopes decide bounded values before rendering. Both
inline and decided shapes are kept with request values bound at execution.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import batched
from threading import RLock
from typing import TYPE_CHECKING, Any, cast

from django.core.exceptions import EmptyResultSet, FieldDoesNotExist
from django.db import connections, models
from django.db.models import Exists, Expression, F, Q, Value
from django.db.models.expressions import Col, ColPairs, Combinable
from django.db.models.fields.related_lookups import RelatedExact
from django.db.models.fields.tuple_lookups import TupleIn
from django.db.models.lookups import Exact, GreaterThanOrEqual, In, IntegerFieldExact, LessThan
from django.db.models.sql.where import WhereNode
from django.utils import timezone
from django_cte import CTE, with_cte

from rebac._id import _NATIVE_INTEGER_FIELDS, model_identity_fields, resource_id_attr
from rebac.codec import identity_codec
from rebac.composition import TaggedComposition, compose_tagged, split_stale_overrides
from rebac.conf import app_settings
from rebac.errors import PermissionDepthExceeded, SchemaError
from rebac.field_backing import resolve_attribute_backing, resolve_field_backing
from rebac.limits import SCOPE_CTE_CANDIDATE_LIMIT
from rebac.models import active_relationship_model
from rebac.models.generation import SchemaGeneration
from rebac.resources import (
    _ModelIndex,
    model_for_resource_type,
    model_for_subject_type,
    model_resource_type,
    stores_rows,
)
from rebac.schema.ast import (
    AttributeBinding,
    ConstBinding,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermExpr,
    PermNil,
    PermRef,
    Schema,
)
from rebac.schema.cache import SchemaSnapshot, schema_operation
from rebac.schema.serialization import digest as schema_digest
from rebac.schema.walker import find_permission, find_relation
from rebac.types import CheckItem, CheckResult, ObjectRef, SubjectRef

from . import At, Bound, Compiler, predicate
from .conditions import CaveatVerdicts, VerdictSlot, _verdict_class
from .evaluate import named_subjects, residual
from .predicate import (
    ActorSets,
    Decided,
    DecidedSlot,
    Fact,
    Support,
    _and,
    _bound_in,
    _BoundIn,
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
_CANDIDATES = object()
_LIMIT = 512
_ROW_LIMIT = 5000
_CANDIDATE_LIMIT = SCOPE_CTE_CANDIDATE_LIMIT
# The M8 field-backed model-identity probe crosses the inline form around
# five memberships on PostgreSQL and seven on SQLite. Four short support
# edges stay under this ceiling; larger sets use the inline closure.
_SET_WITNESS_BUDGET = 2_048
# M1/M8 short-ID probes grew by 412-423 B per edge on PostgreSQL and about
# 432 B on SQLite. Subtracting about 44 B of encoded edge and set IDs leaves
# this fixed SQL cost; the actual encoded values are charged below exactly once.
_SET_SUPPORT_EDGE_BASE_BYTES = 386
_EDGE = (
    "resource_type",
    "resource_id",
    "relation",
    "subject_type",
    "subject_id",
    "subject_relation",
    "caveat_name",
    "caveat_key",
)
# Bulk point checks share one statement for each bound.
_BULK = 50
_BULK_SQL = 64_000
_lock = RLock()


@dataclass(frozen=True, slots=True)
class _Kept:
    """One compiled statement.  ``every`` marks a gate that admits every row."""

    sql: str
    params: tuple[Any, ...]
    every: bool = False


@dataclass(frozen=True, slots=True)
class _CandidateSlot:
    index: int


class _CandidateIn(Expression):
    """One candidate array on PostgreSQL, a padded arity class elsewhere."""

    def __init__(self, lhs: Expression | F, count: int) -> None:
        super().__init__(output_field=models.BooleanField())
        self.lhs = lhs
        self.count = count

    def get_source_expressions(self) -> list[Expression | F]:
        return [self.lhs]

    def set_source_expressions(self, exprs: Sequence[Combinable | Expression]) -> None:
        self.lhs = cast(Expression | F, exprs[0])

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        lhs, params = compiler.compile(self.lhs)
        if connection.vendor == "postgresql":
            return f"{lhs} = ANY(%s)", (*params, _CANDIDATES)
        slots = ", ".join("%s" for _ in range(self.count))
        return f"{lhs} IN ({slots})", (*params, *(_CandidateSlot(i) for i in range(self.count)))


def _candidate_class(using: str, count: int) -> str | int:
    if count == 0:
        return 0
    return "array" if connections[using].vendor == "postgresql" else 1 << (count - 1).bit_length()


_policies: OrderedDict[tuple[Any, ...], _Policy] = OrderedDict()
_statements: OrderedDict[tuple[Any, ...], _Kept | bool] = OrderedDict()
_fact_uses: OrderedDict[tuple[Any, ...], tuple[Fact, ...]] = OrderedDict()
_decision_recipes: OrderedDict[tuple[Any, ...], _DecisionRecipe] = OrderedDict()
_native_outcomes: OrderedDict[tuple[Any, ...], tuple[Any, ...]] = OrderedDict()
_recipe_serial = 0
_statement_bytes = 0
_statement_hits = 0
_statement_misses = 0
_statement_evictions = 0
_seen_revisions: dict[str, OrderedDict[str, None]] = {}


def _bound_params(
    params: Iterable[Any],
    *,
    backend: LocalBackend,
    actor: SubjectRef,
    using: str,
    resource_id: str | None = None,
    candidates: tuple[Any, ...] | None = None,
    verdicts: CaveatVerdicts | None = None,
    operation: _Operation | None = None,
) -> list[Any]:
    result: list[Any] = []
    verdict_keys = (
        {"true": sorted(verdicts.true_keys), "false": sorted(verdicts.false_keys)}
        if verdicts is not None
        else None
    )
    prepared_decisions: dict[
        tuple[tuple[Any, ...], models.Field[Any, Any] | None], Sequence[Any]
    ] = {}
    for param in bind(params, actor, connections[using]):
        if param is _MANUAL_REVISION:
            result.append(backend._manual_schema_revision())
        elif param is _RESOURCE_WIRE:
            result.append(resource_id)
        elif param is _CANDIDATES:
            if candidates is None:
                raise ValueError("candidate array requires bound candidates")
            result.append(list(candidates))
        elif isinstance(param, _CandidateSlot):
            if not candidates:
                raise ValueError("candidate slot requires bound candidates")
            result.append(candidates[min(param.index, len(candidates) - 1)])
        elif isinstance(param, VerdictSlot):
            if verdict_keys is None:
                raise ValueError("verdict slot requires bound verdicts")
            values = verdict_keys[param.kind]
            if param.index is not None and not values:
                raise ValueError("verdict slot requires a nonempty arity class")
            result.append(
                values if param.index is None else values[min(param.index, len(values) - 1)]
            )
        elif isinstance(param, DecidedSlot):
            if operation is None or verdicts is None:
                raise ValueError("decided slot requires the prepared operation")
            source = param.source
            binding = (source, param.field)
            decided_values = prepared_decisions.get(binding)
            if decided_values is None:
                if source[0] == "row":
                    rows = operation.rows(verdicts)
                    found_values = next(
                        (
                            decision.ids
                            for decision in _decision_closure(
                                found for found in rows._found.values() if found is not None
                            )
                            if decision.key == source[1]
                        ),
                        None,
                    )
                    if found_values is None:
                        found_values = rows.rows(source[1])
                    if found_values is None:
                        raise ValueError("decided row slot has no bounded decision")
                    decided_values = found_values
                elif source[0] == "set":
                    sets = operation.sets(verdicts)
                    if sets is None:
                        raise ValueError("decided set slot has no decision")
                    _, key, bound, kind, *rest = source
                    decided_values = sorted(sets.ids(key, bound))
                    if kind == "native":
                        model = operation.model_index.resource(rest[0])
                        if model is None:
                            raise ValueError("decided native slot has no model")
                        identity = resource_id_attr(model)
                        codec = identity_codec(model, identity)
                        _, canonical = model_identity_fields(model, identity)
                        decided_values = [
                            canonical.to_python(value)
                            for value in decided_values
                            if codec.is_canonical(value, using=using)
                        ]
                elif source[0] == "support":
                    sets = operation.sets(verdicts)
                    if sets is None:
                        raise ValueError("decided support slot has no decision")
                    decided_values = (getattr(sets.support[source[1]], source[2]),)
                else:
                    raise ValueError("unknown decided slot")
                if param.field is not None:
                    decided_values = [
                        param.field.get_db_prep_value(value, connections[using], False)
                        for value in decided_values
                    ]
                prepared_decisions[binding] = decided_values
            result.append(
                decided_values[0]
                if source[0] == "support"
                else list(decided_values)
                if param.index is None
                else decided_values[min(param.index, len(decided_values) - 1)]
            )
        else:
            result.append(param)
    return result


def statement_cache_stats() -> dict[str, int]:
    """Process-wide compiled-statement cache usage and counters."""
    with _lock:
        return {
            "entries": len(_statements),
            "bytes": _statement_bytes,
            "hits": _statement_hits,
            "misses": _statement_misses,
            "evictions": _statement_evictions,
        }


def _statement_size(key: tuple[Any, ...], compiled: _Kept | bool) -> int:
    key_bytes = len(repr(key).encode())
    if isinstance(compiled, bool):
        return key_bytes + 1
    return (
        key_bytes
        + len(compiled.sql.encode())
        + sum(len(value) if isinstance(value, bytes | str) else 32 for value in compiled.params)
    )


def _evict_statement() -> None:
    global _statement_bytes, _statement_evictions
    old_key, old = _statements.popitem(last=False)
    _statement_bytes -= _statement_size(old_key, old)
    _statement_evictions += 1


def reset() -> None:
    """Forget every kept policy and statement (settings or registries changed)."""
    from rebac.evaluator import current_evaluator

    global _statement_bytes, _statement_hits, _statement_misses, _statement_evictions
    with _lock:
        _policies.clear()
        _statements.clear()
        _fact_uses.clear()
        _decision_recipes.clear()
        _native_outcomes.clear()
        _seen_revisions.clear()
        _statement_bytes = _statement_hits = _statement_misses = _statement_evictions = 0
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
    watched_models: tuple[tuple[str, frozenset[str]], ...]
    model_indexes: dict[str, tuple[_ModelIndex, tuple[Any, ...]]] = field(
        default_factory=dict, compare=False, repr=False
    )

    @property
    def schema(self) -> Schema:
        return self.tagged.schema


def _schema(backend: LocalBackend, using: str) -> tuple[Schema, SchemaSnapshot]:
    snapshot = backend._schema_snapshot(using=using)
    return snapshot.schema, snapshot


def _policy(backend: LocalBackend, using: str) -> _Policy:
    from rebac.models import SchemaOverride
    from rebac.watch import model_lineage, watched_for

    _effective, snapshot = _schema(backend, using)
    baseline = snapshot.baseline or snapshot.schema
    selected_at = timezone.now()
    overrides_digest = schema_digest(
        [
            (row.pk, row.kind, row.target_ct_id, row.target_pk, row.expression, row.expires_at)
            for row in sorted(snapshot.overrides, key=lambda item: item.pk)
        ]
    )
    deadlines = tuple(
        sorted({row.expires_at for row in snapshot.overrides if row.expires_at is not None})
    )
    epoch = sum(deadline <= selected_at for deadline in deadlines)
    key = (
        using,
        snapshot.revision,
        backend._schema_is_manual,
        snapshot.revision or schema_digest(repr(baseline)),
        overrides_digest,
        epoch,
    )
    with _lock:
        if snapshot.revision is not None:
            recent = _seen_revisions.setdefault(using, OrderedDict())
            recent[snapshot.revision] = None
            recent.move_to_end(snapshot.revision)
            retired = recent.popitem(last=False)[0] if len(recent) > 4 else None
            if retired is not None:
                global _statement_bytes, _statement_evictions
                for stale in tuple(_statements):
                    if stale[0][0] == using and stale[0][1] == retired:
                        _statement_bytes -= _statement_size(stale, _statements.pop(stale))
                        _statement_evictions += 1
                for stale in tuple(_policies):
                    if stale[0] == using and stale[1] == retired:
                        del _policies[stale]
                for stale in tuple(_fact_uses):
                    if stale[0][0] == using and stale[0][1] == retired:
                        del _fact_uses[stale]
                for stale in tuple(_decision_recipes):
                    if stale[0] == using and stale[1] == retired:
                        serial = _decision_recipes.pop(stale).serial
                        for native_key in tuple(_native_outcomes):
                            if native_key[0] == serial:
                                del _native_outcomes[native_key]
        kept = _policies.get(key)
        if kept is not None and kept.snapshot.schema is snapshot.schema:
            _policies.move_to_end(key)
            return kept
    overrides, _stale = split_stale_overrides(baseline, list(snapshot.overrides))
    active = [row for row in overrides if row.expires_at is None or row.expires_at > selected_at]
    # Permission sites retain their SQL deadlines. CEL expressions cannot be
    # selected in SQL, so choose the active override composition for this policy.
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
    policy = _Policy(
        key,
        snapshot,
        tagged,
        compose_tagged(baseline, active).schema,
        CompileProgram.build(tagged.schema),
        selected_at,
        deadlines,
        tuple(
            (member._meta.label_lower, spec.resource_types)
            for spec in watched_for(tagged.schema).values()
            for member in model_lineage(spec.model)
        ),
    )
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
class _Decision:
    ids: tuple[Any, ...]
    witness: Q
    uses: tuple[_Decision, ...] = ()
    sets: ActorSets | None = None
    array: tuple[tuple[str, tuple[Any, ...]], CTE] | None = None
    key: Key | None = None
    deferred: bool = False


@dataclass(frozen=True)
class _DecisionRecipe:
    rows: tuple[Key, ...] = ()
    sets: bool = False
    facts: tuple[Fact, ...] = ()
    memberships: tuple[tuple[Key, Bound, Any], ...] = ()
    serial: int = 0


def _decision_outcome(
    operation: _Operation, verdicts: CaveatVerdicts, recipe: _DecisionRecipe, facts: _Facts | None
) -> tuple[Any, ...]:
    def extent(count: int) -> int:
        return (
            int(count > 0)
            if connections[operation.using].vendor == "postgresql"
            else int(_candidate_class(operation.using, count))
        )

    rows = operation.rows(verdicts)
    for key in recipe.rows:
        rows.rows(key)
    decided = {
        decision.key: extent(len(decision.ids))
        for decision in _decision_closure(
            found for found in rows._found.values() if found is not None
        )
        if decision.key is not None
    }
    row_shape = (
        tuple((key, decided.get(key)) for key in recipe.rows),
        tuple(sorted(decided.items())),
    )
    sets = operation.sets(verdicts) if recipe.sets else None
    set_shape: tuple[Any, ...] | None = None
    if sets is not None:
        set_shape = (
            tuple(
                (
                    key,
                    extent(len(sets.ids(key, Bound.LOWER))),
                    extent(len(sets.ids(key, Bound.UPPER))),
                )
                for key in sorted(sets.keys)
            ),
            tuple(
                (
                    key,
                    bound,
                    extent(
                        sum(
                            identity_codec(model, resource_id_attr(model)).is_canonical(
                                value, using=operation.using
                            )
                            for value in sets.ids(key, bound)
                        )
                    ),
                )
                for key in sorted(sets.keys)
                for bound in (Bound.LOWER, Bound.UPPER)
                if (model := operation.model_index.resource(key[0])) is not None
            ),
            tuple(
                (edge.key, edge.subject_type, edge.subject_relation, edge.caveat_name, edge.own)
                for edge in sets.support
            ),
        )
    return (
        row_shape,
        set_shape,
        tuple((fact, facts[fact]) for fact in recipe.facts) if facts is not None else (),
        tuple(
            (key, bound, value in sets.ids(key, bound)) for key, bound, value in recipe.memberships
        )
        if sets is not None
        else (),
    )


def _decision_closure(decisions: Iterable[_Decision]) -> tuple[_Decision, ...]:
    pending = list(decisions)
    seen: set[int] = set()
    found = []
    while pending:
        decision = pending.pop()
        if id(decision) in seen:
            continue
        seen.add(id(decision))
        found.append(decision)
        pending.extend(decision.uses)
    return tuple(found)


def _decision_size(decision: _Decision) -> int:
    return sum(len(part.ids) for part in _decision_closure((decision,)))


def _memo_eligible(
    backend: LocalBackend,
    resource_type: str,
    *,
    using: str,
    witnessed: bool,
    operation: _Operation | None = None,
    root: Key | None = None,
) -> bool:
    """Admit witnessed decisions in a bound transaction, answers only in autocommit."""
    from rebac.database import read_alias
    from rebac.evaluator import current_evaluator
    from rebac.models import SchemaDefinition

    evaluator = current_evaluator()
    bound = evaluator._decision_connection if evaluator is not None else None
    if bound is not None and connections[using] is not bound:
        return False
    if not witnessed:
        # Returned answers have no statement witness. The check cache owns
        # their live-type and expiration rule, including schema refresh.
        connection = connections[using]
        if connection.in_atomic_block or (
            connection.connection is not None and not connection.get_autocommit()
        ):
            return False
        return backend._cache_generation(resource_type) is not None
    if operation is None or root is None:
        raise ValueError("Witnessed memo eligibility needs an operation and root")
    if operation.policy.caveat_deadlines:
        return False
    for connection in (
        connections[using],
        connections[read_alias(active_relationship_model())],
        connections[read_alias(SchemaDefinition)],
    ):
        if bound is not None and connection is not bound:
            return False
        if connection.in_atomic_block or (
            connection.connection is not None and not connection.get_autocommit()
        ):
            if bound is not connection:
                return False
    for type_, name in operation.policy.program.reachable(root) | {root}:
        definition = operation.policy.schema.get_definition(type_)
        relation = find_relation(definition, name) if definition is not None else None
        if relation is not None and relation.with_expiration:
            return False
    return True


def _row_dependency_key(
    operation: _Operation | LocalBackend, node: Key, *, using: str | None = None
) -> tuple[tuple[str, int], ...]:
    """The written model tables in this node's reachable policy closure.

    Callers with an operation reuse its captured policy. A returned-answer
    memo can pass its backend and alias to use the same dependency owner.
    """
    from rebac.watch import model_lineage, row_generations

    if isinstance(operation, _Operation):
        policy = operation.policy
        model_index = operation.model_index
        alias = operation.using
    else:
        if using is None:
            raise ValueError("A backend dependency key needs a database alias")
        policy = _policy(operation, using)
        model_index = _ModelIndex.capture()
        alias = using
    types = {type_ for type_, _ in policy.program.reachable(node) | {node}}
    labels = {
        member._meta.label_lower
        for type_ in types
        if (model := model_index.resource(type_)) is not None
        for member in model_lineage(model)
    }
    labels.update(
        label for label, resource_types in policy.watched_models if types & resource_types
    )
    return row_generations(labels, using=alias)


def _memo_keys(evaluator: Any, name: tuple[Any, ...]) -> tuple[tuple[Any, ...], ...]:
    """A savepoint memo is valid only inside its own surviving stack."""
    connection = evaluator._decision_connection
    if connection is None:
        return (name,)
    stack = tuple(connection.savepoint_ids)
    return tuple((*name, stack[:depth]) for depth in range(len(stack), -1, -1))


def _memo_key(evaluator: Any, name: tuple[Any, ...]) -> tuple[Any, ...]:
    connection = evaluator._decision_connection
    return name if connection is None else (*name, tuple(connection.savepoint_ids))


class _Facts(Mapping[Fact, bool]):
    """Fixed-object facts decided before rendering and witnessed in SQL."""

    def __init__(self, operation: _Operation, root: Key, verdicts: CaveatVerdicts) -> None:
        self.operation = operation
        self.root = root
        self.verdicts = verdicts
        self.known = frozenset(
            fact
            for member in (operation._shared_keys or (root,))
            for fact in const_facts(operation.policy.schema, operation.policy.program, member)
        )
        self._decided: dict[Fact, bool] = {}

    def __iter__(self) -> Iterator[Fact]:
        return iter(self.known)

    def __len__(self) -> int:
        return len(self.known)

    def __getitem__(self, fact: Fact) -> bool:
        if fact not in self.known:
            raise KeyError(fact)
        if fact not in self._decided:
            self._decided[fact] = self._decide(fact)
        return self._decided[fact]

    def _decide(self, fact: Fact) -> bool:
        from rebac.backends import local
        from rebac.evaluator import _ctx_key, current_evaluator

        operation = self.operation
        context = _ctx_key(dict(operation.context)) if operation.context else ()
        name = (
            operation.policy.key,
            operation.shape,
            operation.actor,
            context,
            fact,
            operation.using,
            local._relationship_generation,
            _row_dependency_key(operation, (fact[0], fact[2])),
        )
        evaluator = current_evaluator()
        eligible = (
            evaluator is not None
            and context is not None
            and not operation._shared_keys
            and _memo_eligible(
                operation.backend,
                fact[0],
                using=operation.using,
                witnessed=True,
                operation=operation,
                root=(fact[0], fact[2]),
            )
        )
        keys = _memo_keys(evaluator, name) if eligible else ()
        hit = next(
            (key for key in keys if evaluator is not None and key in evaluator._fact_decisions),
            None,
        )
        if evaluator is not None and hit is not None:
            evaluator._fact_decisions.move_to_end(hit)
            return bool(evaluator._fact_decisions[hit])
        condition = operation.compiler(self.verdicts, parametric=False).fact_q(fact)
        found = is_true(condition) or (
            not is_false(condition) and operation.gate(parametric=False).filter(condition).exists()
        )
        if evaluator is not None and eligible:
            evaluator._fact_decisions[_memo_key(evaluator, name)] = found
            while len(evaluator._fact_decisions) > evaluator._max_size:
                evaluator._fact_decisions.popitem(last=False)
        return found

    def witness(self, used: Iterable[Fact]) -> Q:
        compiler = self.operation.compiler(
            self.verdicts,
            rows=self.operation.rows(self.verdicts),
            sets=self.operation.sets(self.verdicts),
        )
        if (shared := self.operation.rows(self.verdicts).render_compiler) is not None:
            compiler = shared.fork_shared()
            compiler.facts = None
        parts = []
        for fact in sorted(used, key=str):
            condition = compiler.fact_q(fact)
            parts.append(condition if self[fact] else _not(condition))
        return _and(
            *parts,
            self.operation.rows(self.verdicts).witness(
                compiler.used_rows, witnessed_sets=compiler.sets
            ),
        )


class _Rows:
    """Bounded lower-bound model rows, decided before SQL is rendered."""

    def __init__(self, operation: _Operation, verdicts: CaveatVerdicts, root: Key) -> None:
        self.operation = operation
        self.verdicts = verdicts
        self.root = root
        self._found: dict[Key, _Decision | None] = {}
        self._pending: set[Key] = set()
        self.render_compiler: Compiler | None = None
        self.defer_witness = False

    @property
    def room(self) -> int:
        return _ROW_LIMIT - sum(
            len(found.ids)
            for found in _decision_closure(value for value in self._found.values() if value)
        )

    def _dependencies(self, keys: Iterable[Key]) -> tuple[_Decision, ...]:
        return tuple(found for key in sorted(keys) if (found := self._found.get(key)) is not None)

    def _fits(self, decision: _Decision) -> bool:
        selected = (value for value in self._found.values() if value is not None)
        total = sum(len(part.ids) for part in _decision_closure((*selected, decision)))
        return total <= _ROW_LIMIT

    def rows(self, key: Key) -> tuple[Any, ...] | None:
        # The statement selects the root rows itself. Only an arrow target
        # can be reduced to a bounded population before that statement.
        if key == self.root:
            return None
        if key not in self._found:
            if key in self._pending:
                return None
            self._pending.add(key)
            try:
                self._found[key] = self._memoized_decision(key)
            finally:
                self._pending.remove(key)
        found = self._found[key]
        return Decided(found.ids, ("row", key)) if found is not None else None

    def _memoized_decision(self, key: Key) -> _Decision | None:
        from rebac.backends import local
        from rebac.evaluator import _ctx_key, current_evaluator

        evaluator = current_evaluator()
        context = _ctx_key(dict(self.operation.context)) if self.operation.context else ()
        model = self.operation.model_index.resource(key[0])
        name = (
            self.operation.policy.key,
            self.operation.shape,
            self.operation.actor,
            context,
            model,
            key,
            self.operation.using,
            local._relationship_generation,
            _row_dependency_key(self.operation, key),
        )
        eligible = (
            evaluator is not None
            and context is not None
            and not self.operation._shared_keys
            and _memo_eligible(
                self.operation.backend,
                key[0],
                using=self.operation.using,
                witnessed=True,
                operation=self.operation,
                root=key,
            )
        )
        keys = _memo_keys(evaluator, name) if eligible else ()
        hit = next(
            (
                candidate
                for candidate in keys
                if evaluator is not None and candidate in evaluator._row_decisions
            ),
            None,
        )
        if evaluator is not None and hit is not None:
            evaluator._row_decisions.move_to_end(hit)
            found = cast("_Decision | None", evaluator._row_decisions[hit])
            if found is not None and self._fits(found):
                if self.render_compiler is not None:
                    for decision in _decision_closure((found,)):
                        if decision.array is not None:
                            self.render_compiler.adopt_decided_array(*decision.array)
                return found
        found = self._decide(key)
        if evaluator is not None and eligible and found is not None:
            save_key = _memo_key(evaluator, name)
            previous = evaluator._row_decisions.get(save_key)
            if previous is not None:
                evaluator._row_decision_ids -= _decision_size(previous)
            evaluator._row_decisions[save_key] = found
            evaluator._row_decision_ids += _decision_size(found)
            while (
                evaluator._row_decision_ids > evaluator._max_size
                or len(evaluator._row_decisions) > evaluator._max_size
            ):
                _, evicted = evaluator._row_decisions.popitem(last=False)
                evaluator._row_decision_ids -= _decision_size(evicted)
        return found

    def witness(self, used: Iterable[Key], *, witnessed_sets: ActorSets | None = None) -> Q:
        decisions = _decision_closure(self._dependencies(used))
        if self.defer_witness:
            # The preparation's row IDs are enough to find a kept statement.
            # Only a miss needs fresh witness expressions for SQL rendering.
            pending = list(decisions)
            seen: set[Key] = set()
            while pending:
                found = pending.pop()
                if found.key is not None:
                    if found.key in seen:
                        continue
                    seen.add(found.key)
                    current = self._found.get(found.key, found)
                    if current is not None and current.deferred:
                        current = self._materialize(current)
                        self._found[found.key] = current
                    if current is not None:
                        self._found.setdefault(found.key, current)
                        pending.extend(current.uses)
                else:
                    pending.extend(found.uses)
            decisions = tuple(
                current_found
                for key in sorted(seen)
                if (current_found := self._found.get(key)) is not None
            )
        if self.render_compiler is not None:
            for found in decisions:
                if found.array is not None:
                    self.render_compiler.adopt_decided_array(*found.array)
        parts = [found.witness for found in decisions]
        seen_sets = {id(witnessed_sets)} if witnessed_sets is not None else set()
        for found in decisions:
            sets = found.sets
            if sets is None or id(sets) in seen_sets:
                continue
            seen_sets.add(id(sets))
            compiler = (
                self.render_compiler.fork_shared()
                if self.render_compiler is not None
                else self.operation.compiler(self.verdicts, sets=sets)
            )
            compiler.sets = sets
            parts.append(compiler.sets_witness())
        return _and(*parts)

    def _decide(self, key: Key) -> _Decision | None:
        model = self.operation.model_index.resource(key[0])
        if model is None or not stores_rows(model):
            return None
        held_before = set(self._found)

        def abandon() -> None:
            for nested in self._found.keys() - held_before:
                del self._found[nested]

        operation = self.operation
        source = model._base_manager.using(operation.using).order_by()
        identity = resource_id_attr(model)
        sets = operation.sets(self.verdicts)
        # Preparation executes its own statement. Only the later witness
        # contributes definitions to the union owner's registry.
        compiler = operation.compiler(
            self.verdicts, parametric=False, share_subject_rows=True, rows=self, sets=sets
        )
        member = compiler.holds(key, _model_at(model), Bound.LOWER)
        if is_true(member):
            abandon()
            return None
        if is_false(member):
            return _Decision((), _and(), self._dependencies(compiler.used_rows), sets, key=key)
        gate = Q(Exists(operation.gate(parametric=False)))
        # This query only prepares IDs. Native CTE sharing changes its plan,
        # not its answer, so keep it inline without a render probe.
        selected = compiler.with_subject_ctes(source.filter(gate & member), probe_native=False)
        ids = tuple(selected.values_list(identity, flat=True)[: self.room + 1])
        if len(ids) > self.room:
            abandon()
            return None
        if not ids:
            return _Decision((), _and(), self._dependencies(compiler.used_rows), sets, key=key)
        found = _Decision(
            ids,
            _and(),
            self._dependencies(compiler.used_rows),
            sets,
            key=key,
            deferred=True,
        )
        return found if self.defer_witness else self._materialize(found)

    def _materialize(self, found: _Decision) -> _Decision:
        """Build a missed statement's live recheck for one prepared row set."""
        assert found.key is not None
        key = found.key
        ids = found.ids
        model = self.operation.model_index.resource(key[0])
        assert model is not None
        operation = self.operation
        source = model._base_manager.using(operation.using).order_by()
        identity = resource_id_attr(model)
        sets = operation.sets(self.verdicts)
        compiler_used = {part.key for part in found.uses if part.key is not None}
        # Evaluator-memoized decisions can outlive this statement. Their
        # witnesses must carry their own subject, set and closure CTEs.
        shared_witness = self.render_compiler is not None and bool(operation._shared_keys)
        later = (
            self.render_compiler.fork_shared()
            if shared_witness and self.render_compiler is not None
            else operation.compiler(
                self.verdicts,
                share_subject_rows=True,
                rows=self,
                sets=sets,
                bind_decisions=(
                    self.render_compiler.bind_decisions
                    if self.render_compiler is not None
                    else False
                ),
            )
        )
        if self.render_compiler is not None and later is not self.render_compiler:
            later._value_memberships = self.render_compiler._value_memberships
        # A witness must recheck fixed-object facts. The owner's decided
        # value cannot serve as its own proof after a revocation.
        later.facts = None
        # While rebuilding this key, its prepared IDs cannot prove their own
        # membership. Treat the key as pending so holds() compiles its live arm.
        previous = self._found.pop(key, None)
        was_pending = key in self._pending
        self._pending.add(key)
        try:
            held = later.holds(key, _model_at(model), Bound.LOWER)
        finally:
            if previous is not None:
                self._found[key] = previous
            if not was_pending:
                self._pending.remove(key)
        if is_true(held):
            return _Decision(
                ids, _and(), self._dependencies(compiler_used | later.used_rows), sets, key=key
            )
        array = None
        if self.render_compiler is not None:
            _, identity_field = model_identity_fields(model, identity)
            member_ids = self.render_compiler.decided_member(
                F(identity), Decided(ids, ("row", key)), identity_field
            )
            if connections[operation.using].vendor == "postgresql":
                array = self.render_compiler.decided_array(
                    Decided(ids, ("row", key)), identity_field
                )
        else:
            member_ids = _bound_in(F(identity), ids)
        failing = source.filter(member_ids).filter(_not(held))
        if not shared_witness:
            failing = later.with_subject_ctes(failing)
        return _Decision(
            ids,
            ~Q(Exists(failing)),
            self._dependencies(compiler_used | later.used_rows),
            sets,
            array,
            key,
        )


@dataclass(frozen=True)
class _Operation:
    """What one statement is compiled for: a policy and an actor."""

    backend: LocalBackend
    policy: _Policy
    actor: SubjectRef
    using: str
    context: Mapping[str, Any] | None
    shape: tuple[Any, ...]
    model_index: _ModelIndex
    _verdicts: dict[Key, CaveatVerdicts] = field(default_factory=dict)
    _roots: dict[int, Key] = field(default_factory=dict)
    _sets: dict[int, ActorSets | None] = field(default_factory=dict)
    _rows: dict[int, _Rows] = field(default_factory=dict)
    _bulk: set[int] = field(default_factory=set)
    _shared_keys: set[Key] = field(default_factory=set)

    @classmethod
    def begin(
        cls,
        backend: LocalBackend,
        actor: SubjectRef,
        using: str,
        context: Mapping[str, Any] | None = None,
        policy: _Policy | None = None,
    ) -> _Operation:
        policy = policy if policy is not None else _policy(backend, using)
        with _lock:
            indexed = policy.model_indexes.get(using)
            if indexed is None:
                model_index = _ModelIndex.capture()

                def identity_class(model: type[models.Model]) -> str:
                    attr = resource_id_attr(model)
                    try:
                        return type(
                            model._meta.pk if attr == "pk" else model._meta.get_field(attr)
                        ).__qualname__
                    except FieldDoesNotExist:
                        return type(getattr(model, attr)).__qualname__

                model_fingerprint = tuple(
                    (wire, model._meta.label_lower, model._meta.db_table, identity_class(model))
                    for wire, model in sorted(model_index.models.items())
                )
                indexed = model_index, model_fingerprint
                policy.model_indexes[using] = indexed
        model_index, model_fingerprint = indexed
        return cls(
            backend,
            policy,
            actor,
            using,
            context,
            (
                policy.key,
                actor_shape(policy.schema, actor, using, model_index),
                using,
                connections[using].vendor,
                app_settings.REBAC_DEPTH_LIMIT,
                active_relationship_model()._meta.label_lower,
                app_settings.REBAC_LOCAL_BACKEND_STORAGE,
                app_settings.REBAC_TYPE_PREFIX,
                app_settings.REBAC_USER_TYPE,
                app_settings.REBAC_GROUP_TYPE,
                app_settings.REBAC_ANONYMOUS_TYPE,
                app_settings.REBAC_RESOURCE_ID_ATTR,
                app_settings.REBAC_USER_ID_ATTR,
                model_fingerprint,
            ),
            model_index,
        )

    def verdicts(self, key: Key) -> CaveatVerdicts:
        found = self._verdicts.get(key)
        if found is None:
            found = self._verdicts[key] = CaveatVerdicts.prepare(
                self.policy.schema, key, context=self.context, using=self.using
            )
            self._roots[id(found)] = key
        return found

    def adopt(self, key: Key, verdicts: CaveatVerdicts) -> None:
        """Use verdicts prepared by another operation for the same key and context."""
        self._verdicts[key] = verdicts
        self._roots[id(verdicts)] = key

    def rows(self, verdicts: CaveatVerdicts) -> _Rows:
        token = id(verdicts)
        if token not in self._rows:
            self._rows[token] = _Rows(self, verdicts, self._roots[token])
        return self._rows[token]

    def sets(self, verdicts: CaveatVerdicts) -> ActorSets | None:
        from rebac.backends import local
        from rebac.evaluator import _ctx_key, current_evaluator

        token = id(verdicts)
        if token in self._sets:
            return self._sets[token]
        root = self._roots[token]
        context = _ctx_key(dict(self.context)) if self.context else ()
        name = (
            self.policy.key,
            self.shape,
            self.actor,
            context,
            root,
            self.model_index.resource(root[0]),
            self.using,
            local._relationship_generation,
            _row_dependency_key(self, root),
        )
        evaluator = current_evaluator()
        eligible = (
            evaluator is not None
            and context is not None
            and not self._shared_keys
            and _memo_eligible(
                self.backend,
                root[0],
                using=self.using,
                witnessed=True,
                operation=self,
                root=root,
            )
        )
        keys = _memo_keys(evaluator, name) if eligible else ()
        hit = next(
            (key for key in keys if evaluator is not None and key in evaluator._set_decisions),
            None,
        )
        if evaluator is not None and hit is not None:
            evaluator._set_decisions.move_to_end(hit)
            found = cast("ActorSets | None", evaluator._set_decisions[hit])
        else:
            found = self._decide_sets(root, verdicts)
            if evaluator is not None and eligible:
                evaluator._set_decisions[_memo_key(evaluator, name)] = found
                while len(evaluator._set_decisions) > evaluator._max_size:
                    evaluator._set_decisions.popitem(last=False)
        self._sets[token] = found
        return found

    def sets_compiler(self, verdicts: CaveatVerdicts) -> Compiler:
        """The compiler that reads the tuples of the stored sets."""
        return Compiler(
            self.policy.schema,
            self.actor,
            self.using,
            tagged=self.policy.tagged,
            verdicts=verdicts,
            program=self.policy.program,
            model_index=self.model_index,
        )

    def sets_conditional(self, keys: frozenset[Key]) -> bool:
        """Whether a caveat can make the two bounds of the sets differ."""
        schema = self.policy.schema
        return any(
            allowed.with_caveat
            for type_, name in keys
            if (definition := schema.get_definition(type_)) is not None
            for relation in definition.relations
            if relation.name == name
            for allowed in relation.allowed_subjects
        )

    def _decide_sets(self, key: Key, verdicts: CaveatVerdicts) -> ActorSets | None:
        """Follow the tuple table from the actor until no new set appears.

        One statement per level of nesting, and one more that finds nothing.
        The result is exact, data cycles included: there is no depth bound.
        """
        program = self.policy.program
        keys = program.stored_sets & set().union(
            *(program.reachable(root) | {root} for root in (self._shared_keys or (key,)))
        )
        if not keys:
            return None
        compiler = self.sets_compiler(verdicts)
        conditional = self.sets_conditional(keys)
        tuples = compiler._tuples()
        decided: dict[Bound, dict[Key, frozenset[str]]] = {}
        support: list[Support] = []
        for bound in (Bound.LOWER, Bound.UPPER):
            if bound is Bound.UPPER and not conditional:
                decided[bound] = decided[Bound.LOWER]
                break
            members: dict[Key, set[str]] = {}
            while True:
                step = compiler.sets_step(keys, members, bound)
                if is_false(step):
                    break
                rows = tuples.filter(step)
                within = compiler.sets_within(members, bound)
                if not is_false(within):
                    rows = rows.exclude(within)
                fresh = list(rows.order_by(*_EDGE).values_list(*_EDGE).distinct())
                if not fresh:
                    break
                for type_, resource_id, name, s_type, s_id, s_relation, caveat, digest in fresh:
                    ids = members.setdefault((type_, name), set())
                    if resource_id in ids:
                        continue
                    ids.add(resource_id)
                    if bound is Bound.LOWER:
                        own = (
                            s_type == self.actor.subject_type
                            and s_id == self.actor.subject_id
                            and s_relation == self.actor.optional_relation
                        )
                        support.append(
                            Support(
                                (type_, name),
                                resource_id,
                                s_type,
                                s_id,
                                s_relation,
                                caveat,
                                digest,
                                own,
                            )
                        )
                support_bytes = sum(
                    _SET_SUPPORT_EDGE_BASE_BYTES
                    + sum(
                        len(value.encode())
                        for value in (
                            edge.key[0],
                            edge.key[1],
                            edge.resource_id,
                            edge.subject_type,
                            edge.subject_id,
                            edge.subject_relation,
                            edge.caveat_name,
                            edge.caveat_key,
                        )
                    )
                    for edge in support
                )
                set_bytes = sum(
                    len(value.encode()) + 8 for values in members.values() for value in values
                )
                if support_bytes + set_bytes > _SET_WITNESS_BUDGET:
                    return None
            decided[bound] = {found: frozenset(ids) for found, ids in members.items()}
        return ActorSets(keys, decided[Bound.LOWER], decided[Bound.UPPER], tuple(support), key)

    def compiler(
        self,
        verdicts: CaveatVerdicts,
        *,
        parametric: bool = True,
        share_subject_rows: bool = False,
        rows: _Rows | None = None,
        sets: ActorSets | None = None,
        facts: Mapping[Fact, bool] | None = None,
        cte_owner: str | None = None,
        bind_decisions: bool = False,
    ) -> Compiler:
        return Compiler(
            self.policy.schema,
            self.actor,
            self.using,
            tagged=self.policy.tagged,
            verdicts=verdicts,
            program=self.policy.program,
            model_index=self.model_index,
            parametric=parametric,
            share_subject_rows=share_subject_rows,
            rows=rows,
            sets=sets,
            facts=facts,
            cte_owner=cte_owner,
            bind_decisions=bind_decisions,
        )

    def gate(self, *, parametric: bool = True) -> models.QuerySet[Any]:
        return _fence(self.policy, backend=self.backend, using=self.using, parametric=parametric)

    def statement(
        self,
        key: tuple[Any, ...],
        verdicts: CaveatVerdicts,
        build: Callable[[], models.QuerySet[Any] | bool | tuple[models.QuerySet[Any], bool]],
        *,
        cache: bool = True,
        signature: tuple[Any, ...] = (),
        facts: _Facts | None = None,
        compiler_state: list[Compiler] | None = None,
    ) -> _Kept | bool:
        """Compile and cache SQL whose only varying values are bound parameters.

        The cache switch is used by the before/after benchmark's no-cache phase.
        """
        global _statement_bytes, _statement_hits, _statement_misses, _recipe_serial
        keep = cache
        base = (
            *self.shape,
            *key,
            signature,
            _verdict_class(len(verdicts.true_keys), self.using),
            _verdict_class(len(verdicts.false_keys), self.using),
        )
        decided_scope = bool(key and key[0] == "scope" and signature and signature[0] == "decided")
        outcome: tuple[Any, ...] | None = None
        native: tuple[Any, ...] | None = None
        if keep and decided_scope:
            with _lock:
                recipe = _decision_recipes.get(base)
                if recipe is not None:
                    _decision_recipes.move_to_end(base)
            if recipe is not None:
                outcome = _decision_outcome(self, verdicts, recipe, facts)
                with _lock:
                    native_key = (recipe.serial, outcome)
                    native = _native_outcomes.get(native_key)
                    if native is not None:
                        _native_outcomes.move_to_end(native_key)
        used: tuple[Fact, ...] | None = () if facts is None else None
        if keep and facts is not None and not decided_scope:
            with _lock:
                used = _fact_uses.get(base)
                if used is not None:
                    _fact_uses.move_to_end(base)
        full: tuple[Any, ...] | None
        if decided_scope:
            full = (*base, outcome, native) if outcome is not None and native is not None else None
        elif facts is None:
            full = base
        elif used is not None:
            full = (*base, tuple((fact, facts[fact]) for fact in used))
        else:
            full = None
        if keep and full is not None:
            with _lock:
                kept = _statements.get(full)
                if kept is not None:
                    _statements.move_to_end(full)
                    _statement_hits += 1
                    return kept
                _statement_misses += 1
        elif keep:
            with _lock:
                _statement_misses += 1
        built = build()
        if not decided_scope and (
            any(rows._found for rows in self._rows.values())
            or any(value is not None for value in self._sets.values())
        ):
            keep = False
        if keep and decided_scope:
            found_rows = {
                key for rows in self._rows.values() for key in rows._found if key != rows.root
            }
            observed = tuple(
                (key, bound, value)
                for compiler in compiler_state or ()
                for key, bound, value, _present in compiler._value_memberships
            )
            with _lock:
                previous = _decision_recipes.get(base, _DecisionRecipe())
                if previous.serial == 0:
                    _recipe_serial += 1
                recipe = _DecisionRecipe(
                    tuple(sorted(set(previous.rows) | found_rows)),
                    previous.sets or bool(self._sets),
                    tuple(
                        sorted(set(previous.facts) | set(facts._decided if facts else ()), key=str)
                    ),
                    tuple(dict.fromkeys((*previous.memberships, *observed))),
                    previous.serial or _recipe_serial,
                )
                _decision_recipes[base] = recipe
                _decision_recipes.move_to_end(base)
                while len(_decision_recipes) > _LIMIT:
                    _, old_recipe = _decision_recipes.popitem(last=False)
                    for old_native_key in tuple(_native_outcomes):
                        if old_native_key[0] == old_recipe.serial:
                            del _native_outcomes[old_native_key]
            outcome = _decision_outcome(self, verdicts, recipe, facts)
            native = tuple(
                sorted(
                    (
                        (
                            slot[0]._meta.label_lower,
                            slot[1],
                            slot[2],
                            slot[3],
                            shared,
                        )
                        for compiler in compiler_state or ()
                        for slot, shared in compiler._native_decisions.items()
                    )
                )
            )
            full = (*base, outcome, native)
            with _lock:
                native_key = (recipe.serial, outcome)
                _native_outcomes[native_key] = native
                _native_outcomes.move_to_end(native_key)
                while len(_native_outcomes) > _LIMIT:
                    _native_outcomes.popitem(last=False)
        if keep and facts is not None and not decided_scope:
            used = tuple(sorted(facts._decided, key=str))
            full = (*base, tuple((fact, facts[fact]) for fact in used))
            with _lock:
                _fact_uses[base] = used
                while len(_fact_uses) > _LIMIT:
                    _fact_uses.popitem(last=False)
        compiled: _Kept | bool
        if isinstance(built, bool):
            compiled = built
        else:
            statement_rows, every = built if isinstance(built, tuple) else (built, False)
            try:
                sql, params = _Compiled(statement_rows).as_sql(None, connections[self.using])
            except EmptyResultSet:
                # No row can satisfy it: an unpublished policy, or a
                # condition Django proves empty.
                compiled = False
            else:
                compiled = _Kept(sql, params, every)
        if keep and full is not None:
            with _lock:
                size = _statement_size(full, compiled)
                budget = max(0, int(app_settings.REBAC_STATEMENT_CACHE_BYTES))
                if size <= budget:
                    old = _statements.pop(full, None)
                    if old is not None:
                        _statement_bytes -= _statement_size(full, old)
                    _statements[full] = compiled
                    _statement_bytes += size
                    while len(_statements) > _LIMIT or _statement_bytes > budget:
                        _evict_statement()
        return compiled

    def bound(
        self,
        params: Iterable[Any],
        resource_id: str | None = None,
        candidates: tuple[Any, ...] | None = None,
        verdicts: CaveatVerdicts | None = None,
    ) -> list[Any]:
        return _bound_params(
            params,
            backend=self.backend,
            actor=self.actor,
            using=self.using,
            resource_id=resource_id,
            candidates=candidates,
            verdicts=verdicts,
            operation=self,
        )


def _model_at(model: type[models.Model]) -> At:
    identity = resource_id_attr(model)
    _, identity_field = model_identity_fields(model, identity)
    resource_type = model_resource_type(model)
    if resource_type is None:
        raise ValueError(f"{model.__name__} has no REBAC resource type")
    return At(resource_type, F(identity), identity_field, True)


# ---------- Point checks ----------


def _point_statement(
    operation: _Operation,
    key: Key,
    which: str,
    *,
    decide: bool = True,
) -> _Kept | bool:
    """One bound of ``key`` at one identity, compiled as SQL."""
    verdicts = operation.verdicts(key)
    known = const_facts(operation.policy.schema, operation.policy.program, key)
    # Several independent fixed objects cost one preparation read apiece.
    # The point bound can check all of them together in its own statement.
    one_object = len({fact[:3] for fact in known}) <= 1
    facts = (
        _Facts(operation, key, verdicts)
        if decide and id(verdicts) not in operation._bulk and one_object
        else None
    )
    signature = ("decided",) if decide else ("inline",)

    def build() -> models.QuerySet[Any] | bool:
        sets = operation.sets(verdicts) if decide else None
        decider = operation.rows(verdicts) if decide and one_object else None
        at = At(key[0], _Param(_RESOURCE_WIRE, models.TextField()), None, False)
        compiler = operation.compiler(
            verdicts,
            share_subject_rows=True,
            sets=sets,
            rows=decider,
            facts=facts,
            bind_decisions=decider is not None,
        )
        if decider is not None:
            decider.render_compiler = compiler
        if which == "depth":
            condition = compiler.depth_unknown(key, at)
        else:
            condition = compiler.holds(key, at, Bound(which))
        if is_false(condition):
            return False
        rows = operation.gate()
        if which != "upper":
            rows = rows.filter(
                _and(
                    compiler.sets_witness(),
                    decider.witness(compiler.used_rows, witnessed_sets=sets) if decider else _and(),
                    facts.witness(compiler.used_facts) if facts else _and(),
                )
            )
        selected = (rows if is_true(condition) else rows.filter(condition)).values("pk")
        return compiler.with_subject_ctes(selected)

    return operation.statement(
        ("point", key, which), verdicts, build, signature=signature, facts=facts
    )


def _point(
    operation: _Operation,
    key: Key,
    resource_id: str,
    which: str,
) -> bool:
    """One bound of ``key`` at one identity, by one statement."""
    compiled = _point_statement(operation, key, which)
    if isinstance(compiled, bool):
        return compiled and operation.gate(parametric=False).exists()
    probe = _Sql(
        compiled.sql,
        operation.bound(compiled.params, resource_id, verdicts=operation.verdicts(key)),
    )
    return SchemaGeneration.objects.using(operation.using).filter(pk=1).filter(Q(probe)).exists()


def _points(
    asked: Sequence[tuple[_Operation, Key, str]],
    which: str,
) -> list[bool]:
    """One bound of several items: each a column of one statement over the generation row.

    A column is the statement ``_point`` would run for the item.
    """
    answers = [False] * len(asked)
    statements: list[dict[str, _Sql]] = [{}]
    size = 0
    parameter_count = 1  # The generation-row primary key in the outer query.
    parameter_limit = connections[asked[0][0].using].features.max_query_params if asked else None
    fenced: bool | None = None
    for position, (operation, key, resource_id) in enumerate(asked):
        compiled = _point_statement(operation, key, which)
        if isinstance(compiled, bool):
            if compiled:
                if fenced is None:
                    fenced = operation.gate(parametric=False).exists()
                answers[position] = fenced
        else:
            bound = operation.bound(compiled.params, resource_id, verdicts=operation.verdicts(key))
            if statements[-1] and (
                size + len(compiled.sql) > _BULK_SQL
                or (parameter_limit is not None and parameter_count + len(bound) > parameter_limit)
            ):
                statements.append({})
                size = 0
                parameter_count = 1
            size += len(compiled.sql)
            parameter_count += len(bound)
            statements[-1][f"item{position}"] = _Sql(compiled.sql, bound)
    rows = SchemaGeneration.objects.using(asked[0][0].using).filter(pk=1) if asked else None
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
) -> tuple[bool, bool, bool, _Policy]:
    operation = _Operation.begin(backend, actor, using, context)
    key = resource.resource_type, action
    # Only the lower bound may authorize. Later probes refine a denial.
    if _point(operation, key, resource.resource_id, "lower"):
        return True, True, False, operation.policy
    recursive = _recursive(operation, key)
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
    schema, _snapshot = _schema(backend, using)
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
    *, backend: LocalBackend, items: Sequence[CheckItem], using: str, lower_only: bool = False
) -> list[CheckResult]:
    """What ``check`` answers for each item, from statements the items share."""

    schema, _snapshot = _schema(backend, using)
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
        policy = _policy(backend, using)
        for chunk in batched(points, _BULK, strict=False):
            found = _check_chunk(
                backend, policy, [items[index] for index in chunk], using, lower_only
            )
            answers.update(zip(chunk, found, strict=True))
    return [answers[index] for index in range(len(items))]


def _check_chunk(
    backend: LocalBackend,
    policy: _Policy,
    items: Sequence[CheckItem],
    using: str,
    lower_only: bool = False,
) -> list[CheckResult]:
    """The point checks of up to ``_BULK`` items, with bounded depth probes."""
    from rebac.evaluator import _ctx_key

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
    # A bulk bound already tests every actor in one SQL statement. Expanding
    # each actor's stored sets beforehand would add one read per actor.
    for operation, key, _resource_id in asked:
        verdicts = operation.verdicts(key)
        operation._sets[id(verdicts)] = None
        operation._bulk.add(id(verdicts))
    lower = _points(asked, "lower")
    if lower_only:
        return [CheckResult.has() if held else CheckResult.no() for held in lower]
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
    decide: bool = True,
    candidates: tuple[int, ...] | None = None,
) -> _Kept | bool:
    """The ids of ``model`` the actor holds ``key`` on, as SQL.

    ``False`` when there are none.  ``every`` is set when every row qualifies
    and the SQL is only the policy fence.
    """
    verdicts = operation.verdicts(key)
    label = model._meta.label_lower
    known = const_facts(operation.policy.schema, operation.policy.program, key)
    one_object = len({fact[:3] for fact in known}) <= 1
    facts = _Facts(operation, key, verdicts) if decide and one_object else None
    signature = ("decided",) if decide else ("inline",)
    compiler_state: list[Compiler] = []
    prepared_compiler: Compiler | None = None
    if decide:
        sets = operation.sets(verdicts)
        decider = operation.rows(verdicts) if one_object else None
        prepared_compiler = operation.compiler(
            verdicts,
            share_subject_rows=True,
            rows=decider,
            sets=sets,
            facts=facts,
            bind_decisions=True,
        )
        compiler_state.append(prepared_compiler)
        if decider is not None:
            decider.render_compiler = prepared_compiler
            decider.defer_witness = True

    def build() -> bool | tuple[models.QuerySet[Any], bool]:
        sets = operation.sets(verdicts) if decide else None
        decider = operation.rows(verdicts) if decide and one_object else None
        compiler = prepared_compiler or operation.compiler(
            verdicts,
            share_subject_rows=True,
            rows=decider,
            sets=sets,
            facts=facts,
        )
        predicate = compiler.holds(key, _model_at(model), Bound.LOWER)
        if is_false(predicate):
            return False
        gate = operation.gate().filter(
            _and(
                decider.witness(compiler.used_rows, witnessed_sets=sets) if decider else _and(),
                compiler.sets_witness(),
                facts.witness(compiler.used_facts) if facts else _and(),
            )
        )
        if is_true(predicate):
            return compiler.with_subject_ctes(gate.values("pk")), True
        # The row predicate goes last: it holds the nested subqueries.
        rows = model._base_manager.using(operation.using)
        if candidates is not None:
            candidate_class = _candidate_class(operation.using, len(candidates))
            rows = rows.filter(
                Q(
                    _CandidateIn(
                        F(resource_id_attr(model)),
                        candidate_class if isinstance(candidate_class, int) else 1,
                    )
                )
            )
        rows = rows.filter(Q(Exists(gate)) & predicate)
        return compiler.with_subject_ctes(rows.order_by().values(resource_id_attr(model))), False

    return operation.statement(
        ("scope", label, key),
        verdicts,
        build,
        signature=(
            *signature,
            _candidate_class(operation.using, len(candidates)) if candidates is not None else None,
        ),
        facts=facts,
        compiler_state=compiler_state if decide else None,
    )


def _scope_decide(requested: bool, using: str, *, candidates: bool) -> bool:
    """Choose the witnessed plan at the scope's render boundary."""
    if not requested:
        return False
    if not candidates:
        return True
    from rebac.evaluator import current_evaluator

    evaluator = current_evaluator()
    return bool(evaluator is not None and evaluator._decision_connection is connections[using])


def _shared_scope_predicate(
    model: type[models.Model], key: Key, compiler: Compiler, *, projection: bool = False
) -> Q:
    """Build an operand against the union owner's decisions and CTEs."""
    identity = resource_id_attr(model)
    subjects = compiler._subject_cte(key, model, identity, Bound.LOWER, {}, True)
    if subjects is False:
        return Q(**{f"{identity}__in": ()})
    if isinstance(subjects, models.QuerySet):
        return Q(**{f"{identity}__in": _Compiled(subjects.order_by().values(identity))})
    inline = compiler.holds(key, _model_at(model), Bound.LOWER)
    if (
        not projection
        or is_true(inline)
        or is_false(inline)
        or (compiler.sets is not None and key in compiler.sets.keys)
    ):
        return inline
    # A projection is resolved after Django has assembled the operand's FROM.
    # Keep a field backing's joins inside this independent identity subquery.
    rows = model._base_manager.using(compiler.using).filter(inline).order_by().values(identity)
    return Q(**{f"{identity}__in": _Compiled(rows)})


def _shared_scope_statement(
    operation: _Operation,
    model: type[models.Model],
    predicate: Q,
    gate: models.QuerySet[Any],
) -> _Kept | bool:
    """Compile one scope against the union owner's gate and CTEs."""
    identity = resource_id_attr(model)
    if is_false(predicate):
        return False
    if is_true(predicate):
        rows = gate.values("pk")
    else:
        rows = (
            model._base_manager.using(operation.using)
            .filter(Q(Exists(gate)) & predicate)
            .order_by()
            .values(identity)
        )
    try:
        sql, params = _Compiled(rows).as_sql(None, connections[operation.using])
    except EmptyResultSet:
        return False
    return _Kept(sql, params, is_true(predicate))


def _shareable_scope(scope: _Scope) -> bool:
    """A shared CTE may not freeze a clock or a caveat verdict at build time."""
    policy = scope.policy
    return (
        scope.verdicts.empty
        and not policy.caveat_deadlines
        and not any(
            tag.deadline for tag in (*policy.tagged.arms.values(), *policy.tagged.sites.values())
        )
        and not any(
            relation.with_expiration
            for definition in policy.schema.definitions
            for relation in definition.relations
        )
    )


@dataclass(frozen=True)
class SharedScopePlan:
    """A CTE and the exact permission state pinned by one attached statement."""

    cte: CTE
    model: type[models.Model]
    actor: SubjectRef
    action: str
    using: str
    backend: LocalBackend
    columns: tuple[str, ...]
    policy: _Policy
    verdicts: CaveatVerdicts
    shareable: bool
    actions: tuple[str, ...] = ()
    closure: CTE | None = None
    leaf_ordinals: Mapping[tuple[PermExpr, Bound], int] = field(default_factory=dict)
    owned_ctes: tuple[str, ...] = ()
    candidates: tuple[int, ...] | None = None

    def queryset(self) -> models.QuerySet[Any]:
        return identity_cte_queryset(self.cte, self.model, self.using, self.columns)


def identity_cte_queryset(
    cte: CTE,
    model: type[models.Model],
    using: str,
    columns: tuple[str, ...],
) -> models.QuerySet[Any]:
    """A plain, identity-only queryset over a CTE reference."""
    reference = cte.queryset()
    return (
        models.QuerySet(model=model, query=reference.query.clone(), using=using)
        .order_by()
        .values(*columns)
    )


def attached_scope_plan(
    cte: CTE,
    body: models.QuerySet[Any],
    model: type[models.Model],
    actor: SubjectRef,
    action: str,
    using: str,
    backend: LocalBackend,
    columns: tuple[str, ...],
    candidates: tuple[int, ...] | None = None,
) -> SharedScopePlan:
    """Read the fresh CTE body's pinned policy and caveat verdicts."""

    def scopes(node: Any) -> Iterator[_Scope]:
        if isinstance(node, _Scope):
            yield node
        elif isinstance(node, WhereNode):
            for child in node.children:
                yield from scopes(child)
        else:
            source = getattr(node, "get_source_expressions", None)
            if callable(source):
                for child in cast(Iterable[Any], source()):
                    if child is not None:
                        yield from scopes(child)

    scope = next(scopes(body.query.where))
    return SharedScopePlan(
        cte,
        model,
        actor,
        action,
        using,
        backend,
        columns,
        scope.policy,
        scope.verdicts,
        _shareable_scope(scope),
        candidates=candidates,
    )


def _within_shared_candidates(query: Any, scope: _Scope, plan: SharedScopePlan, using: str) -> bool:
    if plan.candidates is None:
        return True
    found = _known_identities(scope.model, scope.lhs, query.where, using)
    return found is not None and set(found) <= set(plan.candidates)


def matching_shared_scope(query: Any, scope: Any, using: str) -> SharedScopePlan | None:
    """Match only the same identity and pinned, shareable permission state."""
    if not isinstance(scope, _Scope):
        return None
    for plan in getattr(query, "_rebac_shared_scopes", ()):
        if (
            plan.shareable
            and plan.model._meta.concrete_model is scope.model._meta.concrete_model
            and model_resource_type(plan.model) == model_resource_type(scope.model)
            and resource_id_attr(plan.model) == resource_id_attr(scope.model)
            and plan.actor == scope.actor
            and plan.action == scope.action
            and plan.using == using == scope.using
            and plan.backend is scope.backend
            and plan.policy.key == scope.policy.key
            and plan.verdicts == scope.verdicts
            and _within_shared_candidates(query, scope, plan, using)
        ):
            return cast(SharedScopePlan, plan)
    return None


def scope_candidate_ids(
    model: type[models.Model], query: Any, using: str
) -> tuple[int, ...] | None:
    """Accept only one native, mandatory bounded identity lookup on a CTE body."""
    if len(query.where.children) != 1 or query.where.negated or query.where.connector != "AND":
        return None
    lhs = F(resource_id_attr(model)).resolve_expression(query)
    return _known_identities(model, lhs, query.where, using)


def statement_candidate_ids(
    model: type[models.Model], query: Any, using: str
) -> tuple[int, ...] | None:
    """Prove a bounded identity set on the final statement's own row alias."""
    if query.model._meta.concrete_model is not model._meta.concrete_model:
        return None
    lhs = F(resource_id_attr(model)).resolve_expression(query)
    return _known_identities(model, lhs, query.where, using)


def _permission_leaves(
    expr: PermExpr, bound: Bound, found: dict[tuple[PermExpr, Bound], int]
) -> None:
    if isinstance(expr, PermBinOp):
        _permission_leaves(expr.left, bound, found)
        _permission_leaves(expr.right, bound.opposite() if expr.op == "-" else bound, found)
    elif isinstance(expr, (PermRef, PermArrow)):
        found.setdefault((expr, bound), len(found))


def _action_expression(policy: _Policy, resource_type: str, action: str) -> PermExpr:
    definition = policy.schema.get_definition(resource_type)
    if definition is None:
        return PermNil()
    relation = find_relation(definition, action)
    if relation is not None:
        return PermRef(action)
    permission = policy.schema.get_permission(resource_type, action)
    return permission.expression if permission is not None else PermNil()


def _closure_verdict(
    closure: CTE,
    leaves: Mapping[tuple[PermExpr, Bound], int],
    expression: PermExpr,
    model: type[models.Model],
    using: str,
) -> Q:
    """Lower-bound permission algebra over one CTE of distinct leaf truths."""
    reference = closure.queryset()
    rows: models.QuerySet[Any] = models.QuerySet(
        model=model, query=reference.query.clone(), using=using
    )

    def verdict(expr: PermExpr, bound: Bound) -> Q:
        if isinstance(expr, PermNil):
            return Q(Exact(Value(1), Value(0)))
        if isinstance(expr, (PermRef, PermArrow)):
            ordinal = leaves[(expr, bound)]
            return Q(pk__in=rows.filter(_rebac_arm=ordinal).order_by().values("pk"))
        if isinstance(expr, PermBinOp):
            left = verdict(expr.left, bound)
            right = verdict(expr.right, bound.opposite() if expr.op == "-" else bound)
            if expr.op == "+":
                return left | right
            if expr.op == "&":
                return left & right
            return left & ~right
        raise TypeError(f"Unknown permission expression {type(expr).__name__}")

    return verdict(expression, Bound.LOWER)


def attached_permission_plan(
    model: type[models.Model],
    actor: SubjectRef,
    actions: tuple[str, ...],
    using: str,
    backend: LocalBackend,
    columns: tuple[str, ...],
    candidates: tuple[int, ...] | None,
    name: str,
) -> tuple[tuple[CTE, ...], SharedScopePlan] | None:
    """Compile one closure and small per-action verdicts for an attached statement."""
    operation = _Operation.begin(backend, actor, using)
    resource_type = cast(str, model_resource_type(model))
    verdicts = {action: operation.verdicts((resource_type, action)) for action in actions}
    policy = operation.policy
    shareable = (
        all(verdict.empty for verdict in verdicts.values())
        and not policy.caveat_deadlines
        and not policy.tagged.arms
        and not policy.tagged.sites
        and not any(
            relation.with_expiration
            for definition in policy.schema.definitions
            for relation in definition.relations
        )
        and all((resource_type, action) not in policy.program.recursive for action in actions)
    )
    if not shareable:
        return None
    definition = policy.schema.get_definition(resource_type)
    leaves: dict[tuple[PermExpr, Bound], int] = {}
    expressions = {action: _action_expression(policy, resource_type, action) for action in actions}
    for action in actions:
        _permission_leaves(expressions[action], Bound.LOWER, leaves)
    if definition is None or not leaves:
        return None
    compiler = operation.compiler(
        verdicts[actions[0]],
        parametric=False,
        share_subject_rows=True,
        cte_owner="0",
    )
    gate = operation.gate(parametric=False)
    arms: list[models.QuerySet[Any]] = []
    for (leaf, bound), ordinal in leaves.items():
        predicate = compiler._expression(
            definition, leaf, _model_at(model), bound, {}, depth_possible=True
        )
        rows: models.QuerySet[Any] = model._base_manager.using(using)
        if candidates is not None:
            rows = rows.filter(_bound_in(F(resource_id_attr(model)), candidates))
        if is_false(predicate):
            continue
        if not is_true(predicate):
            rows = rows.filter(predicate)
        arm = (
            rows.filter(Exists(gate))
            .order_by()
            .annotate(_rebac_arm=Value(ordinal, output_field=models.IntegerField()))
            .values("pk", "_rebac_arm")
        )
        try:
            arm.query.get_compiler(using=using).as_sql()
        except EmptyResultSet:
            continue
        arms.append(arm)
    if not arms:
        return None
    closure_rows = arms[0].union(*arms[1:], all=True).order_by()
    compiler.decide_native(closure_rows)
    closure = CTE(closure_rows, name=f"{name}_closure")
    body: models.QuerySet[Any] = model._base_manager.using(using)
    if candidates is not None:
        body = body.filter(_bound_in(F(resource_id_attr(model)), candidates))
    body = (
        body.filter(_closure_verdict(closure, leaves, expressions[actions[0]], model, using))
        .order_by()
        .values(*columns)
    )
    cte = CTE(body, name=name)
    plan = SharedScopePlan(
        cte,
        model,
        actor,
        actions[0],
        using,
        backend,
        columns,
        policy,
        verdicts[actions[0]],
        True,
        actions,
        closure,
        leaves,
        tuple(item.name for item in (*compiler.statement_ctes(), closure, cte)),
        candidates,
    )
    return (*compiler.statement_ctes(), closure, cte), plan


def matching_shared_permission(query: Any, scope: Any, using: str) -> Q | None:
    if not isinstance(scope, _Scope):
        return None
    for plan in getattr(query, "_rebac_shared_scopes", ()):
        if (
            plan.closure is not None
            and scope.action in plan.actions
            and plan.model._meta.concrete_model is scope.model._meta.concrete_model
            and model_resource_type(plan.model) == model_resource_type(scope.model)
            and resource_id_attr(plan.model) == resource_id_attr(scope.model)
            and plan.actor == scope.actor
            and plan.using == using == scope.using
            and plan.backend is scope.backend
            and plan.policy.key == scope.policy.key
            and plan.verdicts == scope.verdicts
            and _within_shared_candidates(query, scope, plan, using)
        ):
            return _closure_verdict(
                plan.closure,
                plan.leaf_ordinals,
                _action_expression(
                    plan.policy, cast(str, model_resource_type(plan.model)), scope.action
                ),
                plan.model,
                plan.using,
            )
    return None


def share_combined_scopes(query: Any, *, cte_owner: str = "0") -> bool | None:
    """Hoist common subject sets from scoped UNION operands to their owner."""

    from rebac.expressions import _Permission
    from rebac.managers import _replace_query_ctes

    def in_permissions(node: Any) -> Iterator[_Permission]:
        if isinstance(node, _Permission):
            yield node
        elif isinstance(node, WhereNode):
            for child in node.children:
                yield from in_permissions(child)
        else:
            sources = getattr(node, "get_source_expressions", None)
            if callable(sources):
                for child in cast(Iterable[Any], sources()):
                    yield from in_permissions(child)

    def in_where(node: Any) -> Iterator[_Scope]:
        if isinstance(node, _Scope):
            yield node
        elif isinstance(node, WhereNode):
            for child in node.children:
                yield from in_where(child)
        else:
            sources = getattr(node, "get_source_expressions", None)
            if callable(sources):
                for child in cast(Iterable[Any], sources()):
                    yield from in_where(child)

    part_scopes = [(part, tuple(in_where(part.where))) for part in query.combined_queries]
    owners = [(part, scope) for part, scopes in part_scopes for scope in scopes]
    scopes = [scope for _, scope in owners]
    if not scopes:
        return None
    signature = tuple(id(scope._rebac_scope_token) for scope in scopes)
    if getattr(query, "_rebac_combined_ctes_shared", None) == signature:
        return True
    first = scopes[0]
    if any(scope.actor != first.actor or scope.using != first.using for scope in scopes):
        return False
    if any(scope.policy is not first.policy for scope in scopes):
        return None
    if len(scopes) < 2 or any(not _shareable_scope(scope) for scope in scopes):
        return None
    # Django includes the left queryset's Query as the first union part.
    # Copy every part before relocating CTEs or assigning a shared plan.
    query.combined_queries = tuple(part.clone() for part in query.combined_queries)
    part_scopes = [(part, tuple(in_where(part.where))) for part in query.combined_queries]
    owners = [(part, scope) for part, found in part_scopes for scope in found]
    projections = [
        permission
        for part, _scope in owners
        for expression in (*part.annotations.values(), part.where)
        for permission in in_permissions(expression)
        if permission.actor == first.actor
        and permission.row_model is not None
        and permission.row_model._meta.concrete_model is permission.model._meta.concrete_model
        and part.model is not None
        and part.model._meta.concrete_model is permission.model._meta.concrete_model
    ]
    if projections:
        from rebac.backends import backend

        if backend() is not first.backend:
            projections = []
    old_ctes = {
        id(cte)
        for part in (*query.combined_queries, query)
        for cte in getattr(part, "_rebac_subject_plan", ((), (), {}, None))[1]
    }
    for part in (*query.combined_queries, query):
        if hasattr(part, "_with_ctes"):
            _replace_query_ctes(part, remove_ids=old_ctes)
    operation = _Operation.begin(first.backend, first.actor, first.using, policy=first.policy)
    scope_keys = frozenset(
        (cast(str, model_resource_type(scope.model)), scope.action) for scope in scopes
    )
    projections = [
        permission
        for permission in projections
        if operation.verdicts(
            (cast(str, model_resource_type(permission.model)), permission.action)
        ).empty
    ]
    keys = scope_keys | frozenset(
        (cast(str, model_resource_type(permission.model)), permission.action)
        for permission in projections
    )
    operation._shared_keys.update(keys)
    shared_root: Key = ("__union__", "__union__")
    operation._roots[id(first.verdicts)] = shared_root
    rows = operation.rows(first.verdicts)
    sets = operation.sets(first.verdicts)
    facts = _Facts(operation, shared_root, first.verdicts)
    compiler = operation.compiler(
        first.verdicts,
        parametric=False,
        share_subject_rows=True,
        rows=rows,
        sets=sets,
        facts=facts,
        cte_owner=cte_owner,
    )
    rows.render_compiler = compiler
    predicates: list[tuple[_Scope, Q]] = []
    for scope in scopes:
        key = (cast(str, model_resource_type(scope.model)), scope.action)
        predicates.append((scope, _shared_scope_predicate(scope.model, key, compiler)))
    projection_predicates = {
        permission._rebac_permission_token: _shared_scope_predicate(
            permission.model,
            (cast(str, model_resource_type(permission.model)), permission.action),
            compiler,
            projection=True,
        )
        for permission in projections
    }
    witness = _and(
        rows.witness(compiler.used_rows, witnessed_sets=sets),
        compiler.sets_witness(),
        facts.witness(compiler.used_facts),
    )
    gate = operation.gate(parametric=False).filter(witness)
    if not is_true(witness):
        fence = CTE(gate.values("pk"), name=compiler._cte_name("rebac_gate"))
        compiler._statement_ctes.append(fence)
        gate = compiler._cte_queryset(fence)

    members: list[tuple[_Scope, _Kept | bool]] = []
    for scope, condition in predicates:
        members.append((scope, _shared_scope_statement(operation, scope.model, condition, gate)))
    shared_permissions = {
        token: Q(Exists(gate)) & condition for token, condition in projection_predicates.items()
    }
    ctes = compiler.statement_ctes()
    if ctes:
        with_cte(*ctes, select=models.QuerySet(model=query.model, query=query))
    plan = (tuple(members), ctes, shared_permissions, first.using)
    for part, _scope in owners:
        part._rebac_subject_plan = plan
    query._rebac_subject_plan = plan
    query._rebac_combined_ctes_shared = signature
    return True


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
        if type(node) not in (Exact, IntegerFieldExact, RelatedExact, In, _BoundIn, TupleIn):
            continue
        column = node.lhs
        value = node.rhs
        values = (value,)
        if type(node) is TupleIn:
            if (
                type(column) is not ColPairs
                or len(column.targets) != 1
                or len(column.sources) != 1
                or type(value) not in (list, tuple)
                or not 0 < len(value) <= _CANDIDATE_LIMIT
            ):
                continue
            if any(type(row) not in (list, tuple) or len(row) != 1 for row in value):
                continue
            # Django omits rows holding NULL from the rendered TupleIn.
            values = tuple(row[0] for row in value if row[0] is not None)
            if not values:
                continue
            [column] = column.get_cols()
        if (
            type(column) is not Col
            or column.alias != lhs.alias
            or column.target is not field
            or (column.output_field is not field and column.output_field is not scalar)
        ):
            continue
        if type(node) in (In, _BoundIn):
            if type(value) not in (list, tuple) or not 0 < len(value) <= _CANDIDATE_LIMIT:
                continue
            values = tuple(value)
        if any(type(item) is not int for item in values) or len(set(values)) != len(values):
            continue
        try:
            for item in values:
                codec.wire(item, using=using)
        except SchemaError:
            continue
        return tuple(values)
    return None


def _has_literal_identity_set(lhs: Any, where: Any) -> bool:
    """Leave native ID-set lookups to the point/bounded compiler path."""
    if type(lhs) is not Col:
        return False
    pending = [where]
    while pending:
        node = pending.pop()
        if isinstance(node, WhereNode):
            pending.extend(node.children)
        elif (
            type(node) in (In, _BoundIn)
            and type(node.lhs) is Col
            and node.lhs.alias == lhs.alias
            and node.lhs.target is lhs.target
            and type(node.rhs) in (list, tuple)
            and bool(node.rhs)
        ):
            return True
    return False


class _Scope(Expression):
    """A queryset scope with policy and caveat verdicts pinned when built.

    A caveat instance written later is absent from the lower bound and present
    in the upper bound, so the prepared scope fails closed.
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
        decide: bool = True,
        point_projection: bool = False,
    ) -> None:
        super().__init__(output_field=models.BooleanField())
        self.backend = backend
        self.model = model
        self.action = action
        self.actor = actor
        self.using = using
        self.policy = policy
        self.verdicts = verdicts
        self.decide = decide
        self.point_projection = point_projection
        self.lhs: Any = F(resource_id_attr(model))
        self._prepared: _Kept | bool | None = None
        # A combinator operand may need a render-time decision when its
        # queryset owner did not prepare it first.
        self._render_fallback = False
        # Django resolves a new shallow expression copy on every SQL render.
        # The shared holder lets standalone fallback copies reuse preparation.
        self._plans: dict[tuple[str, str], _Kept | bool] = {}
        self._operations: dict[str, _Operation] = {}
        self._rebac_scope_token = object()

    def _operation(self, using: str) -> _Operation:
        operation = self._operations.get(using)
        if operation is None:
            operation = _Operation.begin(self.backend, self.actor, using, policy=self.policy)
            resource_type = model_resource_type(self.model)
            if resource_type is not None:
                operation.adopt((resource_type, self.action), self.verdicts)
            self._operations[using] = operation
        return operation

    def prepare(self, using: str) -> None:
        """Read bounded decisions on the known alias before SQL rendering."""
        if self._prepared is not None:
            return
        token = using, "scope"
        if token in self._plans:
            self._prepared = self._plans[token]
            return
        operation = self._operation(using)
        resource_type = model_resource_type(self.model)
        if resource_type is None:
            self._prepared = False
            return
        key = resource_type, self.action
        self._prepared = _scope_statement(operation, self.model, key, decide=self.decide)
        self._plans[token] = self._prepared

    def get_source_expressions(self) -> list[Any]:
        return [self.lhs]

    def set_source_expressions(self, exprs: Any) -> None:
        [self.lhs] = exprs

    @schema_operation
    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        resource_type = model_resource_type(self.model)
        if resource_type is None:
            return "(1 = 0)", ()
        identities = _known_identities(self.model, self.lhs, compiler.query.where, self.using)
        plan: Any = getattr(compiler.query, "_rebac_subject_plan", ((), (), {}, None))
        shared = (
            next(
                (
                    sql
                    for scope, sql in plan[0]
                    if scope._rebac_scope_token is self._rebac_scope_token
                ),
                None,
            )
            if plan[0]
            else None
        )
        if identities is not None:
            shared_scope = matching_shared_scope(compiler.query, self, connection.alias)
            if shared_scope is not None:
                member = In(
                    self.lhs,
                    shared_scope.queryset().values(resource_id_attr(self.model)),
                ).resolve_expression(compiler.query)
                return cast(tuple[str, tuple[Any, ...]], compiler.compile(member))
        if identities is not None and len(identities) == 1 and self.point_projection:
            operation = self._operation(connection.alias)
            key = resource_type, self.action
            point = _point_statement(operation, key, "lower", decide=False)
            if isinstance(point, bool):
                return "(1 = 0)", ()
            wire = identity_codec(self.model, resource_id_attr(self.model)).wire(
                identities[0], using=self.using
            )
            return f"EXISTS {point.sql}", tuple(
                operation.bound(point.params, wire, verdicts=self.verdicts)
            )
        # A combinator operand may reach the compiler without its queryset
        # owner. Keep that first-render preparation for later renders.
        inline: _Kept | bool | None = None
        if identities is not None:
            # The root query inside this proof visits only candidate rows.
            # A decision scope may memoise bounded arrow targets, with their
            # witnesses in the statement; otherwise keep the inline predicate.
            decide = _scope_decide(self.decide, connection.alias, candidates=True)
            operation = self._operation(connection.alias)
            key = resource_type, self.action
            inline = _scope_statement(
                operation, self.model, key, decide=decide, candidates=identities
            )
        else:
            # A matching shared scope already proves membership through its
            # statement-local CTE, whether its closure has bounded rows or not.
            shared_scope = (
                matching_shared_scope(compiler.query, self, connection.alias)
                if not _has_literal_identity_set(self.lhs, compiler.query.where)
                else None
            )
            if shared_scope is not None:
                member = In(
                    self.lhs,
                    shared_scope.queryset().values(resource_id_attr(self.model)),
                ).resolve_expression(compiler.query)
                return cast(tuple[str, tuple[Any, ...]], compiler.compile(member))
            if shared is None and self._render_fallback and self._prepared is None:
                self.prepare(connection.alias)
        if shared is None and self._prepared is None and inline is None:
            operation = self._operation(connection.alias)
            key = resource_type, self.action
            inline = _scope_statement(
                operation,
                self.model,
                key,
                decide=False,
            )
        compiled: _Kept | bool | None = (
            inline
            if identities is not None and inline is not None
            else shared
            if shared is not None
            else self._prepared
            if self._prepared is not None
            else inline
        )
        if isinstance(compiled, bool):
            # ``True`` cannot occur: a statement that admits rows is a gate.
            return "(1 = 0)", ()
        assert compiled is not None
        params = _bound_params(
            compiled.params,
            backend=self.backend,
            actor=self.actor,
            using=self.using,
            candidates=identities,
            verdicts=self.verdicts,
            operation=self._operation(connection.alias),
        )
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
    return Q(
        scope_expression(backend=backend, model=model, action=action, actor=actor, using=using)
    )


def prepare_resolved_filter(
    restriction: Any, where: WhereNode, using: str, *, prepare: bool = True, fallback: bool = False
) -> None:
    """Prepare the native point or population plan from the resolved WHERE."""
    if isinstance(restriction, _Scope):
        restriction._render_fallback = fallback
        if prepare:
            if _known_identities(restriction.model, restriction.lhs, where, using) is None:
                restriction.prepare(using)
        return
    children = getattr(restriction, "children", None)
    if children is not None:
        for child in children:
            prepare_resolved_filter(child, where, using, prepare=prepare, fallback=fallback)
        return
    source = getattr(restriction, "get_source_expressions", None)
    if callable(source):
        for child in cast(Iterable[Any], source()):
            if child is not None:
                prepare_resolved_filter(child, where, using, prepare=prepare, fallback=fallback)


@schema_operation
def scope_expression(
    *,
    backend: LocalBackend,
    model: type[models.Model],
    action: str,
    actor: SubjectRef,
    using: str,
    identity: Expression | None = None,
    decide: bool = True,
) -> Expression:
    """Build the native Boolean scope at the caller's chosen snapshot boundary."""
    if model_resource_type(model) is None:
        return Value(False, output_field=models.BooleanField())
    policy = _policy(backend, using)
    resource_type = model_resource_type(model)
    assert resource_type is not None
    verdicts = CaveatVerdicts.prepare(
        policy.schema, (resource_type, action), context=None, using=using
    )
    expression = _Scope(
        backend,
        model,
        action,
        actor,
        using,
        policy,
        verdicts,
        decide,
        point_projection=identity is not None,
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
        policy = _policy(self.backend, self.using)
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
            scope = _Scope(
                self.backend,
                model,
                self.action,
                self.actor,
                self.using,
                policy,
                verdicts,
                decide=False,
            )
            scope.prepare(self.using)
            condition = Q(scope)
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
    _schema_now, snapshot = _schema(backend, using)
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
    """Whether the action holds on every row of the type.

    It holds at the empty id, which no row-specific fact names.  That proves
    it for every row only while such facts can only add to it: an exclusion
    on the row itself can take it away from one row, so it never holds for all.
    """
    schema, _snapshot = _schema(backend, using)
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
    absent from stored rows and model-less ids share a bulk point check.
    A depth-uncertain id counts as not held instead of raising as
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
    """Check a bounded row batch once for each distinct permission expression.

    A field gate may be an alias of a shared permission. Only unmodified
    expressions share a decision: override arms carry action-specific sites.
    """
    if isinstance(actions, str):
        raise TypeError("actions must be an iterable of names, not a bare string")
    names = tuple(dict.fromkeys(actions))
    candidates = tuple(dict.fromkeys(ids))
    if not names:
        return {}
    if not candidates:
        return {name: set() for name in names}
    policy = _policy(backend, using)
    decisions: dict[object, set[str]] = {}
    answers: dict[str, set[str]] = {}
    for name in names:
        permission = policy.tagged.schema.get_permission(resource_type, name)
        # A composed override can carry per-arm deadlines even if its AST is
        # structurally equal to another action's. Keep those sites separate.
        key: object = (
            ("expression", permission.expression)
            if permission is not None and not policy.tagged.arms and not policy.tagged.sites
            else ("action", name)
        )
        if key not in decisions:
            decisions[key] = _held(
                backend=backend,
                resource_type=resource_type,
                action=name,
                actor=actor,
                ids=candidates,
                using=using,
                context=context,
                chunk_size=_CANDIDATE_LIMIT,
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
    chunk_size: int = _CANDIDATE_LIMIT,
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
            rows = model._base_manager.using(using).order_by()
            values = list(native)
            scope = scope_q(backend=backend, model=model, action=action, actor=actor, using=using)
            verdict = models.Case(
                models.When(scope, then=models.Value(True)),
                default=models.Value(False),
                output_field=models.BooleanField(),
            )
            for start in range(0, len(values), chunk_size):
                chunk = values[start : start + chunk_size]
                for value, allowed in rows.filter(**{f"{identity}__in": chunk}).values_list(
                    identity, verdict
                ):
                    pending.discard(native[value])
                    if allowed:
                        found.add(native[value])
    if not pending:
        return found
    candidates = sorted(pending)
    items = [
        CheckItem(actor, action, ObjectRef(resource_type, value), context) for value in candidates
    ]
    try:
        answers = check_many(backend=backend, items=items, using=using)
    except PermissionDepthExceeded:
        # A bulk check raises for the first deep item without returning other
        # answers. Recheck this batch individually so direct grants survive.
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
    schema, _snapshot = _schema(backend, using)
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
    answers = check_many(
        backend=backend,
        items=[CheckItem(candidate, action, resource, context) for candidate in candidates],
        using=using,
        lower_only=True,
    )
    return [
        candidate for candidate, answer in zip(candidates, answers, strict=True) if answer.allowed
    ]
