"""Caveat decisions for predicates over live relationship tuples.

The decision for a caveat instance depends on its name, pinned context, the
schema and request context, not on the tuple that contains it.  A tuple written
after preparation is therefore allowed by the lower bound only if its exact
instance was already decided true.  Unknown instances remain in the upper
bound, which is required for sound subtraction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from django.db import models
from django.db.models import Expression, F, Q
from django.db.models.expressions import Combinable

from rebac.compile import formulas as conditions
from rebac.models import active_relationship_model
from rebac.schema.ast import PermArrow, PermBinOp, PermExpr, PermRef, Relation, Schema

type Key = tuple[str, str]


@dataclass(frozen=True, slots=True)
class VerdictSlot:
    kind: str
    index: int | None = None


class _VerdictIn(Expression):
    """Bind caveat verdict IDs without specializing SQL to their values."""

    def __init__(self, path: str, kind: str, count: int) -> None:
        super().__init__(output_field=models.BooleanField())
        self.lhs: Expression | F = F(path)
        self.kind = kind
        self.count = count

    def get_source_expressions(self) -> list[Expression | F]:
        return [self.lhs]

    def set_source_expressions(self, exprs: Sequence[Combinable | Expression]) -> None:
        self.lhs = cast(Expression | F, exprs[0])

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        lhs, params = compiler.compile(self.lhs)
        if connection.vendor == "postgresql":
            return f"{lhs} = ANY(%s)", (*params, VerdictSlot(self.kind))
        placeholders = ", ".join("%s" for _ in range(self.count))
        return (
            f"{lhs} IN ({placeholders})",
            (*params, *(VerdictSlot(self.kind, index) for index in range(self.count))),
        )


def _verdict_class(count: int, using: str) -> int | str:
    from django.db import connections

    if count == 0:
        return 0
    return "array" if connections[using].vendor == "postgresql" else 1 << (count - 1).bit_length()


def reachable_relations(schema: Schema, key: Key) -> frozenset[Key]:
    """Find tuple relations whose caveats can affect ``key``.

    Visit arrows and subject-set memberships as well as permission refs.  The
    visited-node set bounds this metadata walk even when the policy is
    recursive; SQL evaluation handles that recursion separately.
    """

    cached = schema._reachable_relations_cache.get(key)
    if cached is not None:
        return cached
    visited: set[Key] = set()
    relations: set[Key] = set()

    def expression(type_: str, expr: PermExpr) -> None:
        if isinstance(expr, PermRef):
            node((type_, expr.name))
        elif isinstance(expr, PermArrow):
            node((type_, expr.via))
            definition = schema.get_definition(type_)
            if definition is None:
                return
            relation = next((r for r in definition.relations if r.name == expr.via), None)
            if relation is not None:
                for allowed in relation.allowed_subjects:
                    node((allowed.type, expr.target))
        elif isinstance(expr, PermBinOp):
            expression(type_, expr.left)
            expression(type_, expr.right)

    def node(current: Key) -> None:
        if current in visited:
            return
        visited.add(current)
        definition = schema.get_definition(current[0])
        if definition is None:
            return
        permission = next((p for p in definition.permissions if p.name == current[1]), None)
        if permission is not None:
            expression(current[0], permission.expression)
            return
        relation: Relation | None = next(
            (r for r in definition.relations if r.name == current[1]), None
        )
        if relation is None:
            return
        relations.add(current)
        for allowed in relation.allowed_subjects:
            if allowed.relation:
                node((allowed.type, allowed.relation))

    node(key)
    found = frozenset(relations)
    schema._reachable_relations_cache[key] = found
    return found


@dataclass(frozen=True, slots=True)
class CaveatVerdicts:
    """Decided condition keys and the missing inputs of undecided instances."""

    true_keys: frozenset[str]
    false_keys: frozenset[str]
    unknown: Mapping[str, frozenset[str]]

    @classmethod
    def prepare(
        cls,
        schema: Schema,
        key: Key,
        *,
        context: Mapping[str, Any] | None,
        using: str,
    ) -> CaveatVerdicts:
        reached = reachable_relations(schema, key)
        caveated = frozenset(
            (type_, relation)
            for type_, relation in reached
            if (
                (definition := schema.get_definition(type_)) is not None
                and any(
                    r.name == relation
                    and any(allowed.with_caveat for allowed in r.allowed_subjects)
                    for r in definition.relations
                )
            )
        )
        if not caveated:
            return cls(frozenset(), frozenset(), {})

        selected = Q(pk__in=[])
        for type_, relation in sorted(caveated):
            selected |= Q(resource_type=type_, relation=relation)
        rows = (
            cast(Any, active_relationship_model().objects.using(using))
            .filter(selected)
            .exclude(caveat_name="")
            .wire_projection()
            .order_by()
            .values_list("caveat_name", "caveat_context", "caveat_key")
            .distinct()
        )
        seen: set[str] = set()
        true_keys: set[str] = set()
        false_keys: set[str] = set()
        unknown: dict[str, frozenset[str]] = {}
        for name, pinned, stored_key in rows.iterator(chunk_size=1000):
            formula = conditions.leaf(name, pinned)
            # PostgreSQL JSONB may render a stored JSON number differently
            # from the caller's original serialization.  The write owner
            # keeps the opaque key paired with its context atomically, so it
            # remains the stable identity of this exact condition instance.
            if not stored_key or stored_key in seen:
                continue
            seen.add(stored_key)
            verdict, missing = conditions.evaluate(formula, schema, context)
            if verdict is True:
                true_keys.add(stored_key)
            elif verdict is False:
                false_keys.add(stored_key)
            else:
                unknown[stored_key] = missing
        return cls(frozenset(true_keys), frozenset(false_keys), unknown)

    @property
    def empty(self) -> bool:
        """No caveated tuple is in reach: both bounds read the same rows."""
        return not (self.true_keys or self.false_keys or self.unknown)

    def condition_q(self, prefix: str, bound: Any, *, using: str, parametric: bool = False) -> Q:
        """The two-valued condition on a tuple row for one bound."""

        name = f"{prefix}caveat_name"
        digest = f"{prefix}caveat_key"
        lower = str(getattr(bound, "value", bound)).lower() == "lower"
        if lower:
            unconditional = Q(**{name: ""})
            if not parametric:
                return (
                    unconditional | Q(**{f"{digest}__in": tuple(sorted(self.true_keys))})
                    if self.true_keys
                    else unconditional
                )
            size = _verdict_class(len(self.true_keys), using)
            if size == 0:
                return unconditional
            return unconditional | Q(
                _VerdictIn(digest, "true", size if isinstance(size, int) else 1)
            )
        if not parametric:
            return (
                ~Q(**{f"{digest}__in": tuple(sorted(self.false_keys))}) if self.false_keys else Q()
            )
        size = _verdict_class(len(self.false_keys), using)
        if size == 0:
            return Q()
        return ~Q(_VerdictIn(digest, "false", size if isinstance(size, int) else 1))
