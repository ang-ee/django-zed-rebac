"""Caveat decisions for predicates over live relationship tuples.

The decision for a caveat instance depends on its name, pinned context, the
schema and request context, not on the tuple that contains it.  A tuple written
after preparation is therefore allowed by the lower bound only if its exact
instance was already decided true.  Unknown instances remain in the upper
bound, which is required for sound subtraction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from django.db.models import Q

from rebac.compile import formulas as conditions
from rebac.models import active_relationship_model
from rebac.schema.ast import PermArrow, PermBinOp, PermExpr, PermRef, Relation, Schema

type Key = tuple[str, str]


def reachable_relations(schema: Schema, key: Key) -> frozenset[Key]:
    """Find tuple relations whose caveats can affect ``key``.

    Visit arrows and subject-set memberships as well as permission refs.  The
    visited-node set bounds this metadata walk even when the policy is
    recursive; SQL evaluation handles that recursion separately.
    """

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
    return frozenset(relations)


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

    def condition_q(self, prefix: str, bound: Any) -> Q:
        """The two-valued condition on a tuple row for one bound."""

        name = f"{prefix}caveat_name"
        digest = f"{prefix}caveat_key"
        lower = str(getattr(bound, "value", bound)).lower() == "lower"
        if lower:
            unconditional = Q(**{name: ""})
            if not self.true_keys:
                return unconditional
            return unconditional | Q(**{f"{digest}__in": tuple(sorted(self.true_keys))})
        if not self.false_keys:
            return Q()
        return ~Q(**{f"{digest}__in": tuple(sorted(self.false_keys))})
