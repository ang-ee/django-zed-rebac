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

The parent may have been admitted under another action than the arm names: a
list admits messages under ``read_inbox`` and reads their parts under ``read``,
whose arm is ``message->read``. The children are delivered when the policy
proves that holding the parent's action grants the arm's (``implies``,
proposal 0022).

Three things must hold, and each is checked where the child statement is built:

- the parents were read by a queryset scoped for this actor and action, from the
  database the children are read from, under the policy the children are decided
  under (the key their scope was built under, ``compile.read.policy_key``: parents
  admitted under one policy deliver no child decided under another), in the
  prefetch that is running now (``delivering``), and the child query is bound to
  those parents by a mandatory ``fk IN (…)`` condition (``_bound_parents``);
- the policy proves the implication, and the child's read does not reach the arm's
  target inside a recursion it has already entered, where it would read the target
  with less of the depth limit than the parent was admitted with
  (``parent_implies_children``);
- the policy carries no override of any kind, which the proof does not read.

A name the policy without inheritance through links generates (``lent(P)``,
proposal 0023) is not an action: an edge under one, as the parent's action or the
child's, is refused.

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
from typing import Any, NamedTuple

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
    PermNil,
    PermRef,
    is_builtin_reference,
    member,
    name_conflicts,
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
    # (model, database, action) -> policy key -> primary keys the actor holds the action
    # on: a row is admitted on the database it was read from, and delivers children
    # there, under the policy its own statement was scoped under, and no other.
    rows: dict[tuple[type[models.Model], str | None, str], dict[Any, set[Any]]] = field(
        default_factory=dict
    )
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
    actor: SubjectRef,
    model: type[models.Model],
    action: str,
    rows: list[Any],
    *,
    policy: Any = None,
) -> Iterator[_Delivery]:
    """Admit ``rows`` for the prefetches that run inside the block.

    ``rows`` were just read by a queryset scoped for ``actor`` and ``action``,
    under the policy whose key is ``policy`` (``compile.read.policy_key``, taken
    where their scope was built); ``None`` takes the current one. A prefetch that
    is itself read inside another one adds its rows to the delivery already open
    for the same actor.
    """
    current = _delivery.get()
    if current is not None and current.serves(actor):
        _admit(current, model, action, rows, policy)
        yield current
        return
    opened = _Delivery(actor)
    _admit(opened, model, action, rows, policy)
    token = _delivery.set(opened)
    try:
        yield opened
    finally:
        # Closed first: a context copied inside the block keeps the record.
        opened.closed = True
        _delivery.reset(token)


def admit(
    actor: SubjectRef,
    model: type[models.Model],
    action: str,
    rows: list[Any],
    *,
    policy: Any = None,
) -> None:
    """Add rows read inside a running prefetch to it: a nested level's own parents.

    Django reads every level of ``a__b`` from the root's prefetch, so the rows
    of ``a`` have no prefetch of their own to open a delivery with. ``policy``
    as for ``delivering``.
    """
    current = _delivery.get()
    if current is not None and current.serves(actor):
        _admit(current, model, action, rows, policy)


def policy_key() -> Any:
    """The key of the policy the backend evaluates now; ``None`` without a local backend."""
    from .backends import backend
    from .backends.local import LocalBackend
    from .compile.read import policy_key as key_of

    local = backend()
    return key_of(local) if isinstance(local, LocalBackend) else None


def _admit(
    delivery: _Delivery, model: type[models.Model], action: str, rows: list[Any], policy: Any
) -> None:
    key = policy if policy is not None else policy_key()
    for row in rows:
        if isinstance(row, models.Model):
            admitted = delivery.rows.setdefault((model, row._state.db, action), {})
            admitted.setdefault(key, set()).add(row.pk)


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
        for (admitted_model, database, parent_action), by_policy in delivery.rows.items():
            # The parents' admission proves something only under the policy their own
            # statement was scoped under: the child is decided under that one or not at all.
            pks = by_policy.get(policy.key, set())
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


def current_policy() -> Any:
    """The policy the backend evaluates now; ``None`` without a local backend.

    The object ``parent_implies_children``, ``explain``, ``implies`` and ``edges``
    take, and the one a delivery is decided under: an opaque value, compared by
    the proofs through its key (``compile.read.policy_key``), kept per schema
    snapshot. Experimental (proposal 0022).
    """
    from .backends import backend
    from .backends.local import LocalBackend
    from .compile.read import _policy as policy_of

    local = backend()
    if not isinstance(local, LocalBackend):
        return None
    return policy_of(local)


_policy = current_policy


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
    policy: Any = None,
    *,
    child_model: type[models.Model],
    fk_name: str,
    parent_model: type[models.Model],
    child_action: str,
    parent_action: str,
) -> bool:
    """Whether the policy grants ``child_action`` on every child of a parent held under ``parent_action``.

    True when ``child_action`` is a union one of whose arms is ``relation->T``,
    for a relation backed by exactly that foreign key, unfiltered, whose only
    subject is the parent type, and holding ``parent_action`` on the parent
    grants ``T`` (``implies``; ``T`` is ``parent_action`` itself in the simplest
    case). A union only adds grants, so the other arms cannot take the grant
    away. An intersection or an exclusion above the arm can, and proves nothing.
    A permission named in the union contributes its own arms.

    ``policy`` is ``current_policy()`` when not given; without a local backend
    nothing is proven.
    """
    if policy is None:
        policy = current_policy()
        if policy is None:
            return False
    key = _proof_key(policy, child_model, fk_name, parent_model, child_action, parent_action)
    found = _PROOFS.get(key)
    if found is None:
        found = (
            _refusal(policy, child_model, fk_name, parent_model, child_action, parent_action)
            is None
        )
        _remember(key, found)
    return found


def _proof_key(
    policy: Any,
    child_model: type[models.Model],
    fk_name: str,
    parent_model: type[models.Model],
    child_action: str,
    parent_action: str,
) -> tuple[Any, ...]:
    return (
        policy.key,
        child_model._meta.label_lower,
        fk_name,
        parent_model._meta.label_lower,
        child_action,
        parent_action,
    )


def explain(
    policy: Any = None,
    *,
    child_model: type[models.Model],
    fk_name: str,
    parent_model: type[models.Model],
    child_action: str,
    parent_action: str,
) -> str | None:
    """Why ``parent_implies_children`` refuses an edge, or ``None`` when it proves it.

    ``policy`` is ``current_policy()`` when not given.
    """
    if policy is None:
        policy = current_policy()
        if policy is None:
            return "no local backend"
    return _refusal(policy, child_model, fk_name, parent_model, child_action, parent_action)


class Edge(NamedTuple):
    """A foreign key structural delivery serves, and the parent actions it serves it under.

    Children of ``child_model``, read under ``child_action`` (their read action) and
    bound by ``fk_name`` to parents of ``parent_model`` that a running prefetch
    admitted under one of ``parent_actions``, are read without their permission
    statement (``parent_implies_children``).
    """

    child_model: type[models.Model]
    fk_name: str
    parent_model: type[models.Model]
    child_action: str
    parent_actions: tuple[str, ...]


def edges(policy: Any = None, *, child_action: str | None = None) -> list[Edge]:
    """Every edge structural delivery serves under ``policy`` (``current_policy()`` by default).

    Each installed model with a resource type, read under ``child_action`` (its read
    action, ``Meta.rebac_default_action`` or ``"read"``, by default), and each of its forward
    foreign keys or one-to-one fields to the primary key of another model with a
    resource type, which are the foreign keys ``delivers`` reads, with the parent
    type's permissions under which the policy proves the child's read
    (``parent_implies_children``). An edge no parent action proves is left out:
    ``explain`` says why. Experimental (proposal 0022), for a consumer that pins or
    audits what ``REBAC_STRUCTURAL_PREFETCH`` reads without a statement; it lists
    what the setting would serve whether it is on or not.

    A delivery still needs what only a running read has: parents a queryset scoped
    for the same actor read under that action, in the prefetch that is running, on
    the children's database and under the same policy, and a child query bound to
    them by ``fk IN (…)`` with nothing else that ``delivers`` refuses.

    For tests and tooling, never a request path: it proves every pair of foreign key
    and parent permission (300 to 600 ms on the benchmark's application policy). It
    reads the proofs already kept and keeps none of those it makes, edges and
    implications alike, so the proofs running deliveries keep are not pushed out of
    the cache by it.
    """
    from django.apps import apps

    if policy is None:
        policy = current_policy()
        if policy is None:
            return []
    found: list[Edge] = []
    for child in apps.get_models():
        if not model_resource_type(child):
            continue
        action = child_action or str(getattr(child._meta, "rebac_default_action", "read"))
        for fk in child._meta.get_fields():
            if not isinstance(fk, models.ForeignKey) or fk.model is not child:
                continue
            parent = fk.related_model
            if not isinstance(parent, type) or not fk.target_field.primary_key:
                continue
            parent_type = model_resource_type(parent)
            definition = policy.schema.get_definition(parent_type) if parent_type else None
            if definition is None:
                continue
            token = _keeping.set(False)
            try:
                actions = tuple(
                    permission.name
                    for permission in definition.permissions
                    if parent_implies_children(
                        policy,
                        child_model=child,
                        fk_name=fk.name,
                        parent_model=parent,
                        child_action=action,
                        parent_action=permission.name,
                    )
                )
            finally:
                _keeping.reset(token)
            if actions:
                found.append(Edge(child, fk.name, parent, action, actions))
    return found


# False while ``edges`` runs: it proves every pair once, and keeps none of those proofs.
_keeping: ContextVar[bool] = ContextVar("rebac_structural_keeping", default=True)


def _remember(key: tuple[Any, ...], found: bool) -> None:
    if not _keeping.get():
        return
    if len(_PROOFS) >= _PROOF_LIMIT:
        _PROOFS.clear()
    _PROOFS[key] = found


def _carries_override(policy: Any) -> bool:
    """Whether the policy is composed with a stored override, of any kind."""
    return bool(policy.snapshot.overrides or policy.tagged.arms or policy.tagged.sites)


def _refusal(
    policy: Any,
    child_model: type[models.Model],
    fk_name: str,
    parent_model: type[models.Model],
    child_action: str,
    parent_action: str,
) -> str | None:
    # The proofs read the policy's text. An override makes what is evaluated depend on
    # more than the text: an arm or a site holds until its deadline, a recaveat replaces
    # a caveat's expression until its own (proposal 0022).
    if _carries_override(policy):
        return "the policy carries an override"
    from .links import is_generated

    # A name the policy without inheritance through links generates is not an action
    # (proposal 0023): rows are never admitted under one, nor read under one.
    for name in (parent_action, child_action):
        if is_generated(name):
            return f"{name} is a generated name, not an action"
    child_type = model_resource_type(child_model)
    parent_type = model_resource_type(parent_model)
    if not child_type or not parent_type:
        return "a model without a resource type"
    schema = policy.schema
    for type_ in (child_type, parent_type):
        # A name read two ways: the proof reads names as the compiler does only where
        # each is read one way (``rebac.schema.ast.name_conflicts``).
        declared = schema.get_definition(type_)
        conflicts = name_conflicts(declared) if declared is not None else []
        if conflicts:
            return f"{conflicts[0]}: nothing is proven about {type_}"
    definition = schema.get_definition(child_type)
    permission = schema.get_permission(child_type, child_action)
    if definition is None or permission is None:
        return f"{child_type} has no permission {child_action}"
    if schema.get_permission(parent_type, parent_action) is None:
        return f"{parent_type} has no permission {parent_action}"
    relations = {relation.name: relation for relation in definition.relations}
    refusals: list[str] = []
    for arm, opened in _opened_union_arms(
        definition, permission.expression, frozenset({child_action})
    ):
        if not isinstance(arm, PermArrow):
            continue
        relation = relations.get(arm.via)
        if relation is None or not isinstance(relation.backing, FieldBinding):
            continue
        if relation.backing.path != fk_name:
            continue
        refusal = _edge_refusal(definition, relation, child_model, parent_model, parent_type)
        if refusal is None and _deeper_in_recursion(
            policy.program, child_type, opened, parent_type, arm.target
        ):
            refusal = (
                f"{arm.target} on {parent_type} is recursive with {child_action}: the child "
                "reads it one hop deeper than its parent was admitted at"
            )
        if refusal is None and not implies(policy, parent_type, parent_action, arm.target):
            refusal = f"{parent_action} is not proven to grant {arm.target} on {parent_type}"
        if refusal is None:
            return None
        refusals.append(f"{arm.via}->{arm.target}: {refusal}")
    if refusals:
        return "; ".join(refusals)
    through = {
        name
        for name, relation in relations.items()
        if isinstance(relation.backing, FieldBinding) and relation.backing.path == fk_name
    }
    if _arrows_via(definition, permission.expression, through, frozenset({child_action})):
        return f"an intersection or exclusion stands above the arm through {fk_name}"
    return f"no arm of {child_action}'s union goes through {fk_name}"


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


def _arrows_via(
    definition: Definition, expr: PermExpr, relations: set[str], seen: frozenset[str]
) -> bool:
    """Whether an arrow over one of ``relations`` occurs anywhere in ``expr``, names opened."""
    if isinstance(expr, PermArrow):
        return expr.via in relations
    if isinstance(expr, PermBinOp):
        return _arrows_via(definition, expr.left, relations, seen) or _arrows_via(
            definition, expr.right, relations, seen
        )
    if isinstance(expr, PermRef) and expr.name not in seen:
        named = _opened(definition, expr.name)
        return named is not None and _arrows_via(definition, named, relations, seen | {expr.name})
    return False


def _edge_refusal(
    definition: Definition,
    relation: Any,
    child_model: type[models.Model],
    parent_model: type[models.Model],
    parent_type: str,
) -> str | None:
    """Why a foreign key does not name exactly the parent the relation's edge does, if it does not."""
    from .field_backing import resolve_field_backing

    if relation.backing.filters:
        return "the backing is filtered"
    if len(relation.allowed_subjects) != 1:
        return "the relation admits more than one subject type"
    [allowed] = relation.allowed_subjects
    if allowed.type != parent_type:
        return f"the relation's subject is {allowed.type}"
    if allowed.relation or allowed.wildcard or allowed.id or allowed.with_caveat:
        return "the relation's subject is not one plain object"
    if relation.with_expiration:
        return "the relation expires"
    resolved = resolve_field_backing(definition, relation)
    if (
        resolved is None
        or resolved.source_model is not child_model
        or resolved.target_model is not parent_model
        or resolved.generic is not None
        or not resolved.single_valued
        or resolved.filters
    ):
        return "the backing is not one unfiltered foreign key to the parent"
    return None


# ---------- Implication between two actions of one type (proposal 0022) ----------


def implies(policy: Any, resource_type: str, held: str, granted: str) -> bool:
    """Whether holding ``held`` on a row of ``resource_type`` grants ``granted``, for every actor
    and row, under every state of the data, as the policy is written and compiled.

    The proof is syntactic: it may fail to prove a true implication and must not prove
    a false one. It reads the policy the backend evaluates, and is kept per policy.
    While the policy carries an override, nothing but ``held == granted`` is proven.
    ``_Implication`` lists the rules.
    """
    if held == granted:
        return True
    if _carries_override(policy):
        return False
    key = ("implies", policy.key, resource_type, held, granted)
    found = _PROOFS.get(key)
    if found is None:
        recursive = policy.program.recursive | policy.program.alias_cycles
        found = _Implication(policy.schema, recursive).names(resource_type, held, granted)
        _remember(key, found)
    return found


class _Implication:
    """One search for ``left ⇒ right`` over one schema: each rule is sound, the search is not complete.

    ``e1 ⇒ e2`` reads: every actor granted ``e1`` on a row of the type is granted
    ``e2`` on it, in the compiled reading (a row admitted by ``e1``'s lower bound
    is admitted by ``e2``'s), under any data, caveat context and instant, the
    same for both sides. The rules, each with why it holds:

    1. ``e ⇒ e``, and ``nil ⇒ e``: one expression has one value; ``nil`` grants nothing.
    2. ``e ⇒ a + b`` if ``e ⇒ a`` or ``e ⇒ b``: a union admits whatever either operand does.
    3. ``a + b ⇒ e`` if ``a ⇒ e`` and ``b ⇒ e``: a union admits nothing beyond its operands.
    4. ``a & b ⇒ e`` if ``a ⇒ e`` or ``b ⇒ e``: an intersection admits only what each conjunct does.
    5. ``e ⇒ a & b`` if ``e ⇒ a`` and ``e ⇒ b``: a conjunct ``e`` does not imply is refused.
    6. ``a - d ⇒ b - d'`` if ``a ⇒ b`` and ``d' ⇒ d``: whatever ``d'`` withholds, ``d``
       withholds too; an identical subtrahend is the common case.
    7. ``a - d ⇒ e`` if ``a ⇒ e``: an exclusion admits only what its minuend does.
    8. A name means what it means to the compiler (``rebac.schema.ast.member`` and
       ``is_builtin_reference``): a permission is its expression; a relation, a built-in
       name, an unknown name and an arrow are equal only to themselves (rule 1). That
       holds only on a type where each name is read one way: about a type that declares
       a built-in's name as a relation, or one name twice
       (``rebac.schema.ast.name_conflicts``), nothing is proven but ``held == granted``.
    9. ``r->x ⇒ r->y`` if ``x ⇒ y`` on the subject type, when ``r`` admits exactly one
       subject type, a plain object of it (no subject set, wildcard or fixed id): both
       sides follow the same edges, with the same caveat and expiry, to the same objects.

    Refused: an exclusion added on the right (``e ⇒ b - d`` needs ``e`` and ``d``
    disjoint, which the text does not say); a relation with several subject types, and
    two relations, whatever their subjects; a goal met again while it is being proven:
    the search never assumes what it is proving, so every conclusion it keeps rests on
    a finite proof; anything else.

    Recursion. A recursion is read to ``REBAC_DEPTH_LIMIT`` hops from where its component
    was entered, so text on both sides is read alike only where one side has the whole
    depth. A subtrahend is read at the opposite bound, where a recursion cut at the limit
    counts as holding at its frontier. So, at the lower bound, the granted side never opens
    a recursive name, nor is granted one as a root: each component it names is entered
    afresh, with the whole depth, and the held side has as much or less (with
    ``q = parent->q + viewer``, ``parent->q`` implies ``q``, but its compiled reading
    reaches one hop further than ``q``'s). Inside a subtrahend, neither side does: the
    rule is the simple one, at the cost of a recursion the held side would have opened
    soundly there.
    """

    def __init__(self, schema: Any, recursive: frozenset[tuple[str, str]]) -> None:
        self.schema = schema
        # The keys of recursive components (``CompileProgram``), never opened on the right,
        # nor on either side inside a subtrahend.
        self.recursive = recursive
        # Goals by their text and whether they sit inside a subtrahend: the policy carries
        # no override, and no side that could be read with less depth opens a recursion,
        # so equal text is equal evaluation.
        self._proving: set[tuple[str, PermExpr, PermExpr, bool]] = set()
        self._found: dict[tuple[str, PermExpr, PermExpr, bool], bool] = {}

    def names(self, resource_type: str, held: str, granted: str, subtracted: bool = False) -> bool:
        """``held ⇒ granted`` for two names of ``resource_type``, inside a subtrahend or not."""
        if held == granted:
            return True
        definition = self.schema.get_definition(resource_type)
        if definition is None or (resource_type, granted) in self.recursive:
            return False
        if name_conflicts(definition):
            # A name read two ways: a relation named as a built-in would read as the actor's
            # state once opened, and a name declared twice as two things. Nothing is proven
            # about the type but ``held == granted``.
            return False
        if subtracted and (resource_type, held) in self.recursive:
            return False
        left = _operand(definition, held)
        right = _operand(definition, granted)
        if left is None or right is None:
            return False
        return self._expr(definition, left, right, subtracted)

    def _expr(
        self, definition: Definition, left: PermExpr, right: PermExpr, subtracted: bool
    ) -> bool:
        if isinstance(left, PermNil) or left == right:
            return True
        goal = (definition.resource_type, left, right, subtracted)
        found = self._found.get(goal)
        if found is not None:
            return found
        if goal in self._proving:
            return False
        self._proving.add(goal)
        try:
            found = self._rules(definition, left, right, subtracted)
        finally:
            self._proving.discard(goal)
        self._found[goal] = found
        return found

    def _rules(
        self, definition: Definition, left: PermExpr, right: PermExpr, subtracted: bool
    ) -> bool:
        """Rules 2 to 9 for two expressions of ``definition`` (rule 1 is ``_expr``'s)."""
        if isinstance(left, PermBinOp) and left.op == "+":
            # Rule 3 first: a union implies exactly what both its operands do.
            return self._expr(definition, left.left, right, subtracted) and self._expr(
                definition, left.right, right, subtracted
            )
        if isinstance(right, PermBinOp) and right.op == "+":
            if self._expr(definition, left, right.left, subtracted) or self._expr(
                definition, left, right.right, subtracted
            ):
                return True
        if isinstance(left, PermBinOp) and left.op == "&":
            if self._expr(definition, left.left, right, subtracted) or self._expr(
                definition, left.right, right, subtracted
            ):
                return True
        if isinstance(left, PermBinOp) and left.op == "-":
            if (
                isinstance(right, PermBinOp)
                and right.op == "-"
                and self._expr(definition, left.left, right.left, subtracted)
                # The subtrahends, read at the opposite bound.
                and self._expr(definition, right.right, left.right, True)
            ):
                return True
            if self._expr(definition, left.left, right, subtracted):
                return True
        if isinstance(right, PermBinOp) and right.op == "&":
            if self._expr(definition, left, right.left, subtracted) and self._expr(
                definition, left, right.right, subtracted
            ):
                return True
        if isinstance(left, PermRef) and not (subtracted and self._recursive(definition, left)):
            named = _opened(definition, left.name)
            if named is not None and self._expr(definition, named, right, subtracted):
                return True
        if isinstance(right, PermRef) and not self._recursive(definition, right):
            named = _opened(definition, right.name)
            if named is not None and self._expr(definition, left, named, subtracted):
                return True
        if isinstance(left, PermArrow) and isinstance(right, PermArrow) and left.via == right.via:
            subject = _one_subject_type(definition, left.via)
            if subject is not None and self.names(subject, left.target, right.target, subtracted):
                return True
        return False

    def _recursive(self, definition: Definition, ref: PermRef) -> bool:
        return (definition.resource_type, ref.name) in self.recursive


def _opened(definition: Definition, name: str) -> PermExpr | None:
    """The expression a name stands for inside an expression, if it stands for one.

    Read as the compiler reads it: a built-in name is the actor's state whatever the
    definition declares, a relation is itself, a permission is its expression.
    """
    if is_builtin_reference(name):
        return None
    found = member(definition, name)
    return found.expression if isinstance(found, Permission) else None


def _operand(definition: Definition, name: str) -> PermExpr | None:
    """A name checked or followed to, as an operand: a relation itself, a permission's
    expression, ``None`` if unknown (``rebac.schema.ast.member``)."""
    found = member(definition, name)
    if isinstance(found, Permission):
        return found.expression
    return PermRef(name) if found is not None else None


def _one_subject_type(definition: Definition, via: str) -> str | None:
    """The one type whose plain objects ``via`` admits, if it admits only those."""
    relation = next((item for item in definition.relations if item.name == via), None)
    if relation is None or len(relation.allowed_subjects) != 1:
        return None
    [allowed] = relation.allowed_subjects
    if allowed.relation or allowed.wildcard or allowed.id:
        return None
    return allowed.type


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
