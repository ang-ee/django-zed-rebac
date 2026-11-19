"""Related rows a policy already decides: structural delivery (proposal 0020).

A prefetch reads children by the foreign key that names their parents. Where the
policy says that holding an action on the parent grants the child's action

    definition scope/part {
        relation message: scope/message // rebac:field=message
        permission read = message->read
    }

every child of a parent the actor was just admitted to holds its own action,
and its permission statement decides nothing. With ``REBAC_STRUCTURAL_PREFETCH``
on, the child queryset of such a prefetch is read without that statement. Its
rows are still stamped with the actor, so field gates apply as ever.

Three things must hold, and each is checked where the child statement is built:

- the parents were read by a queryset scoped for this actor and action, in the
  prefetch that is running now (``delivering``), and the child query is bound
  to those parents by a mandatory ``fk IN (…)`` condition (``_bound_parents``);
- the policy proves the implication (``parent_implies_children``);
- the policy carries no override, which the proof does not read.

Anything else falls back to the scoped read. The setting is off by default:
whether a parent read moments ago may admit its children is a consistency
decision (an evaluator scope inside one repeatable-read snapshot makes it
exact), and the library does not take it for the application.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from django.db import models
from django.db.models.expressions import Col
from django.db.models.fields.related_lookups import RelatedExact, RelatedIn
from django.db.models.lookups import Exact, In
from django.db.models.sql.where import WhereNode

from .resources import model_resource_type
from .schema.ast import Definition, FieldBinding, PermArrow, PermBinOp, PermExpr, PermRef
from .types import SubjectRef

# The proofs of one policy, by edge. Bounded: a policy change starts a new key.
_PROOFS: dict[tuple[Any, ...], bool] = {}
_PROOF_LIMIT = 4096


@dataclass
class _Delivery:
    """The rows one prefetch has admitted so far, for one actor."""

    actor: SubjectRef
    # (model, action) -> primary keys the actor holds the action on.
    rows: dict[tuple[type[models.Model], str], set[Any]] = field(default_factory=dict)
    # How many child statements were read without their permission statement.
    delivered: int = 0


_delivery: ContextVar[_Delivery | None] = ContextVar("rebac_structural_delivery", default=None)


def enabled() -> bool:
    from .conf import app_settings

    return bool(app_settings.REBAC_STRUCTURAL_PREFETCH)


@contextmanager
def delivering(
    actor: SubjectRef, model: type[models.Model], action: str, rows: list[Any]
) -> Iterator[_Delivery]:
    """Admit ``rows`` for the prefetches that run inside the block.

    ``rows`` were just read by a queryset scoped for ``actor`` and ``action``.
    A prefetch that is itself read inside another one adds its rows to the
    delivery already open for the same actor.
    """
    current = _delivery.get()
    if current is not None and current.actor == actor:
        _admit(current, model, action, rows)
        yield current
        return
    opened = _Delivery(actor)
    _admit(opened, model, action, rows)
    token = _delivery.set(opened)
    try:
        yield opened
    finally:
        _delivery.reset(token)


def admit(actor: SubjectRef, model: type[models.Model], action: str, rows: list[Any]) -> None:
    """Add rows read inside a running prefetch to it: a nested level's own parents.

    Django reads every level of ``a__b`` from the root's prefetch, so the rows
    of ``a`` have no prefetch of their own to open a delivery with.
    """
    current = _delivery.get()
    if current is not None and current.actor == actor:
        _admit(current, model, action, rows)


def _admit(delivery: _Delivery, model: type[models.Model], action: str, rows: list[Any]) -> None:
    admitted = delivery.rows.setdefault((model, action), set())
    admitted.update(row.pk for row in rows if isinstance(row, models.Model))


def delivers(query: Any, model: type[models.Model], actor: SubjectRef, action: str) -> bool:
    """Whether ``query`` reads only children the running prefetch already admitted."""
    delivery = _delivery.get()
    if delivery is None or delivery.actor != actor:
        return False
    if query.combined_queries or query.is_sliced or query.model is not model:
        return False
    policy = _policy()
    if policy is None:
        return False
    for fk, values in _bound_parents(query, model):
        parent = fk.related_model
        if not isinstance(parent, type) or not fk.target_field.primary_key:
            continue
        for (admitted_model, parent_action), pks in delivery.rows.items():
            if admitted_model is not parent or not values <= pks:
                continue
            if parent_implies_children(
                policy,
                child_model=model,
                fk_name=fk.name,
                parent_model=parent,
                child_action=action,
                parent_action=parent_action,
            ):
                delivery.delivered += 1
                return True
    return False


def _policy() -> Any:
    from .backends import backend
    from .backends.local import LocalBackend
    from .compile.read import _policy as policy_of

    local = backend()
    if not isinstance(local, LocalBackend):
        return None
    return policy_of(local)


def _bound_parents(
    query: Any, model: type[models.Model]
) -> Iterator[tuple[models.ForeignKey[Any, Any], set[Any]]]:
    """The foreign keys ``query`` binds to literal parents, with those parents' keys.

    Only conditions every row must meet are read: the positive ``AND`` path of
    the WHERE clause, on the query's own table. A subquery, an expression or a
    condition under ``OR`` or ``NOT`` proves nothing and is passed over.
    """
    alias = query.get_initial_alias()
    pending: list[Any] = [query.where]
    while pending:
        node = pending.pop()
        if type(node) is WhereNode:
            if node.connector == "AND" and not node.negated:
                pending.extend(node.children)
            continue
        if type(node) not in (In, RelatedIn, Exact, RelatedExact):
            continue
        column = node.lhs
        if type(column) is not Col or column.alias != alias:
            continue
        fk = column.target
        if not isinstance(fk, models.ForeignKey) or fk.model is not model:
            continue
        value = node.rhs
        values = value if type(node) in (In, RelatedIn) else (value,)
        if type(values) not in (list, tuple) or not values:
            continue
        if any(hasattr(item, "resolve_expression") or item is None for item in values):
            continue
        yield fk, set(values)


def parent_implies_children(
    policy: Any,
    *,
    child_model: type[models.Model],
    fk_name: str,
    parent_model: type[models.Model],
    child_action: str,
    parent_action: str,
) -> bool:
    """Whether the policy grants ``child_action`` on every child of a parent held under ``parent_action``.

    True when ``child_action`` is a union one of whose arms is
    ``relation->parent_action``, for a relation backed by exactly that foreign
    key, unfiltered, whose only subject is the parent type. A union only adds
    grants, so the other arms cannot take the grant away. An intersection or an
    exclusion above the arm can, and proves nothing. A permission named in the
    union contributes its own arms.
    """
    key = (
        policy.key,
        child_model._meta.label_lower,
        fk_name,
        parent_model._meta.label_lower,
        child_action,
        parent_action,
    )
    found = _PROOFS.get(key)
    if found is None:
        found = _proves(policy, child_model, fk_name, parent_model, child_action, parent_action)
        if len(_PROOFS) >= _PROOF_LIMIT:
            _PROOFS.clear()
        _PROOFS[key] = found
    return found


def _proves(
    policy: Any,
    child_model: type[models.Model],
    fk_name: str,
    parent_model: type[models.Model],
    child_action: str,
    parent_action: str,
) -> bool:
    from .field_backing import resolve_field_backing

    # An override retags arms the walk below would read as plain unions.
    if policy.tagged.arms or policy.tagged.sites:
        return False
    child_type = model_resource_type(child_model)
    parent_type = model_resource_type(parent_model)
    if not child_type or not parent_type:
        return False
    schema = policy.schema
    definition = schema.get_definition(child_type)
    permission = schema.get_permission(child_type, child_action)
    if definition is None or permission is None:
        return False
    if schema.get_permission(parent_type, parent_action) is None:
        return False
    relations = {relation.name: relation for relation in definition.relations}
    for arm in _union_arms(definition, permission.expression, frozenset({child_action})):
        if not isinstance(arm, PermArrow) or arm.target != parent_action:
            continue
        relation = relations.get(arm.via)
        if relation is None or not isinstance(relation.backing, FieldBinding):
            continue
        if relation.backing.path != fk_name or relation.backing.filters:
            continue
        if len(relation.allowed_subjects) != 1:
            continue
        [allowed] = relation.allowed_subjects
        if allowed.type != parent_type or allowed.relation or allowed.wildcard or allowed.id:
            continue
        resolved = resolve_field_backing(definition, relation)
        if (
            resolved is not None
            and resolved.source_model is child_model
            and resolved.target_model is parent_model
            and resolved.generic is None
            and resolved.single_valued
            and not resolved.filters
        ):
            return True
    return False


def _union_arms(definition: Definition, expr: PermExpr, seen: frozenset[str]) -> Iterator[PermExpr]:
    """The arms of a union, a named permission of the same definition opened into its own."""
    if isinstance(expr, PermBinOp) and expr.op == "+":
        yield from _union_arms(definition, expr.left, seen)
        yield from _union_arms(definition, expr.right, seen)
        return
    if isinstance(expr, PermRef) and expr.name not in seen:
        named = next((item for item in definition.permissions if item.name == expr.name), None)
        if named is not None:
            yield from _union_arms(definition, named.expression, seen | {expr.name})
            return
    yield expr
