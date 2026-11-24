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

- the parents were read by a queryset scoped for this actor and action, from the
  database the children are read from, in the prefetch that is running now
  (``delivering``), and the child query is bound to those parents by a mandatory
  ``fk IN (…)`` condition (``_bound_parents``);
- the policy proves the implication, and the child's read does not reach the arm's
  target inside a recursion it has already entered, where it would read the target
  with less of the depth limit than the parent was admitted with
  (``parent_implies_children``);
- the policy carries no override of any kind, which the proof does not read.

Only the evaluation that reads the child rows inside the running prefetch is
delivered (``RebacQuerySet._fetch_all``), and the delivery serves that evaluation
alone: the queryset keeps no delivered query, so a count, an aggregate,
``iterator()``, an eager ``scoped()`` or an embedding, inside the prefetch or
after it, carries its statement. The record of admitted parents serves only the
thread, and the asyncio task in it, that runs the prefetch, until it ends
(``_Delivery.serves``).

Anything else falls back to the scoped read. The setting is off by default:
whether a parent read moments ago may admit its children is a consistency
decision, and the library does not take it for the application. Inside one
repeatable-read snapshot the children's statement reads the data the parents'
did, but at its own instant: a deadline (a grant's expiry, a caveat that reads
the time, an override's) that falls between the two statements is read as
passed by the second and not by the first.
"""

from __future__ import annotations

import asyncio
import threading
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
from .schema.ast import (
    Definition,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermExpr,
    Permission,
    PermRef,
    is_builtin_reference,
    member,
)
from .types import SubjectRef

# The proofs of one policy, by edge. Bounded: a policy change starts a new key.
_PROOFS: dict[tuple[Any, ...], bool] = {}
_PROOF_LIMIT = 4096


def _current_task() -> object | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        # No event loop runs in this thread (the sync ORM, a worker thread).
        return None


@dataclass
class _Delivery:
    """The rows one prefetch has admitted so far, for one actor.

    The record lives in a context variable, and a context copied while the prefetch
    runs (a signal receiver's ``copy_context()``, a task or a thread started then)
    carries it elsewhere and beyond the prefetch. It serves only the thread, and the
    asyncio task in it, that opened it, and only until the prefetch that opened it
    ends (``serves``): anywhere else, and after, nothing is delivered or admitted.
    """

    actor: SubjectRef
    # (model, database, action) -> primary keys the actor holds the action on: a
    # row is admitted on the database it was read from, and delivers children there.
    rows: dict[tuple[type[models.Model], str | None, str], set[Any]] = field(default_factory=dict)
    # How many child statements were read without their permission statement.
    delivered: int = 0
    thread: int = field(default_factory=threading.get_ident)
    task: object | None = field(default_factory=_current_task)
    closed: bool = False

    def serves(self, actor: SubjectRef) -> bool:
        """Whether this record may deliver or admit for ``actor`` here and now."""
        return (
            not self.closed
            and self.actor == actor
            and self.thread == threading.get_ident()
            and self.task is _current_task()
        )


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
    if current is not None and current.serves(actor):
        _admit(current, model, action, rows)
        yield current
        return
    opened = _Delivery(actor)
    _admit(opened, model, action, rows)
    token = _delivery.set(opened)
    try:
        yield opened
    finally:
        # Closed first: a context copied inside the block keeps the record.
        opened.closed = True
        _delivery.reset(token)


def admit(actor: SubjectRef, model: type[models.Model], action: str, rows: list[Any]) -> None:
    """Add rows read inside a running prefetch to it: a nested level's own parents.

    Django reads every level of ``a__b`` from the root's prefetch, so the rows
    of ``a`` have no prefetch of their own to open a delivery with.
    """
    current = _delivery.get()
    if current is not None and current.serves(actor):
        _admit(current, model, action, rows)


def _admit(delivery: _Delivery, model: type[models.Model], action: str, rows: list[Any]) -> None:
    for row in rows:
        if isinstance(row, models.Model):
            delivery.rows.setdefault((model, row._state.db, action), set()).add(row.pk)


def delivers(
    query: Any, model: type[models.Model], actor: SubjectRef, action: str, *, using: str
) -> bool:
    """Whether ``query``, read from database ``using``, reads only children the running
    prefetch already admitted there."""
    delivery = _delivery.get()
    if delivery is None or not delivery.serves(actor):
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
        for (admitted_model, database, parent_action), pks in delivery.rows.items():
            if admitted_model is not parent or database != using or not values <= pks:
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
    """The foreign keys ``query`` binds to literal parents, with those parents' keys."""
    for column, values in bound_fields(query, model):
        if isinstance(column, models.ForeignKey):
            yield column, values


def bound_fields(
    query: Any, model: type[models.Model]
) -> Iterator[tuple[models.Field[Any, Any], set[Any]]]:
    """The columns of ``model`` that ``query`` binds to literal values, with those values.

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
        if not isinstance(fk, models.Field) or fk.model is not model:
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

    # The proof reads the policy's text. An override makes what is evaluated depend on
    # more than the text: an arm or a site holds until its deadline, a recaveat replaces
    # a caveat's expression until its own.
    if _carries_override(policy):
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
    for arm, opened in _opened_union_arms(
        definition, permission.expression, frozenset({child_action})
    ):
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
            and not _deeper_in_recursion(
                policy.program, child_type, opened, parent_type, parent_action
            )
        ):
            return True
    return False


def _carries_override(policy: Any) -> bool:
    """Whether the policy is composed with a stored override, of any kind."""
    return bool(policy.snapshot.overrides or policy.tagged.arms or policy.tagged.sites)


def _deeper_in_recursion(
    program: Any, child_type: str, opened: frozenset[str], parent_type: str, target: str
) -> bool:
    """Whether the child reads the arm's target inside a recursion its own read has entered.

    A recursion is read to ``REBAC_DEPTH_LIMIT`` hops counted from where its component
    was entered on the way. The parent was admitted with ``target``'s component entered
    at the parent. When the child's action, or a permission its union opens on the way
    to the arm (``opened``), is in that component, the child's read has entered it
    already: it reads ``target`` across the delivered edge, one hop into the component,
    with one hop less of the limit, and admits fewer children than the parent's
    admission covers. Outside the component, the child enters it afresh at the parent
    and reads ``target`` with the whole limit, as the parent was read.
    """
    key = (parent_type, target)
    if key not in program.recursive:
        return False
    component = program.components.get(key, frozenset())
    return any((child_type, name) in component for name in opened)


def _opened(definition: Definition, name: str) -> PermExpr | None:
    """The expression a name stands for inside an expression, if it stands for one.

    Read as the compiler reads it: a built-in name is the actor's state whatever the
    definition declares, a relation is itself, a permission is its expression.
    """
    if is_builtin_reference(name):
        return None
    found = member(definition, name)
    return found.expression if isinstance(found, Permission) else None


def _union_arms(definition: Definition, expr: PermExpr, seen: frozenset[str]) -> Iterator[PermExpr]:
    """The arms of a union, a named permission of the same definition opened into its own.

    A name is opened as the compiler reads it: a built-in name is the actor's state,
    never a permission declared under it.
    """
    for arm, _opened_names in _opened_union_arms(definition, expr, seen):
        yield arm


def _opened_union_arms(
    definition: Definition, expr: PermExpr, seen: frozenset[str]
) -> Iterator[tuple[PermExpr, frozenset[str]]]:
    """``_union_arms``, each arm with the names opened on the way to it, ``seen`` included."""
    if isinstance(expr, PermBinOp) and expr.op == "+":
        yield from _opened_union_arms(definition, expr.left, seen)
        yield from _opened_union_arms(definition, expr.right, seen)
        return
    if isinstance(expr, PermRef) and expr.name not in seen:
        named = _opened(definition, expr.name)
        if named is not None:
            yield from _opened_union_arms(definition, named, seen | {expr.name})
            return
    yield expr, seen
