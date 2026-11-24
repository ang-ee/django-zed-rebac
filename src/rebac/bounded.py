"""A narrow list read decided by its candidate rows (proposal 0025).

Experimental and off by default (``REBAC_BOUNDED_READ``). A compiled scope builds every id
set its permission reaches, however few rows the read's own conditions select: on a wide
policy, the files of one folder cost what a page of every file costs. With the setting on, a
list read whose conditions name only keys the reader already holds and select at most
``LIMIT`` rows is decided by those rows instead:

1. **Parents.** Where its conditions name parents by a foreign key that the running evaluation
   has not admitted, it reads them as the reader would (``_unreadable``: today's statement, one
   for all); a parent not returned keeps the compiled statement.
2. **Candidates.** One statement, ``SELECT <key> FROM <the model's table> WHERE <the read's
   conditions> LIMIT 257``, reads at most ``LIMIT + 1`` primary keys without the permission.
   No join, ordering, slice or projection of the read reaches it, so none of them changes which
   keys it finds.
3. **Decision.** Where ``LIMIT`` or fewer come back, they are every row the conditions select,
   and the walk decides exactly those keys (``rebac.walk.decided_alone``): one small indexed
   lookup per relation the permission reaches, and the compiled statement for a part it cannot
   read, never for the read's own question.
4. **Rows.** The read's own statement runs with the admitted keys in place of its scope: its
   conditions, joins, projection, ordering and slice as written, the slice taken after the
   decision.

Where more than ``LIMIT`` keys come back, or anything here cannot be shown to give what the
compiled statement gives, the read sends its compiled statement, unchanged. It never reads a
second batch of candidates.

Only an evaluation that fills the result cache (``RebacQuerySet._fetch_all``: iteration,
``len()``, ``bool()``, slicing and indexing, ``get()``, ``first()``, ``last()``,
``earliest()``, ``latest()``, ``values()`` and ``values_list()``, a prefetch's child read) is
served, over a scope that evaluation built; ``refusal`` is the rule, read from the query that
evaluation decided. Its conditions must each be on the model's own primary key or a
foreign-key column of its own table, by ``exact`` or ``in`` against literal values, joined by
``AND`` (``_conditions``), and every key they name must be held (``_unheld``): a parent the
reader may read, or a key that a row the running evaluation read for the same actor, on the
same database, refers to. What follows the rows (stamping, structural admission, prefetches,
field redaction, the ``rebac_select_related()`` guards) is the same code on the same rows.

What the compiled statement keeps inside the database and this does not:

- **Keys.** Up to ``LIMIT + 1`` primary keys of rows the actor may not read enter the process,
  and the walk's lookups carry them as statement parameters (a statement log, Django's SQL
  debug log, an error report's frame locals). With ``True`` the walk also reads, of such a row,
  the grants on it that name the actor or everyone and whether it meets a filtered constant's
  filters, and selects no column of it but its key. With ``"identifiers"`` it also reads, of
  each candidate, the foreign keys the permission follows and the subjects of the grants on it
  that name a subject set or a parent: keys of other rows, never a content column.
- **What the statements say.** A reader who can observe the statements a read sends, or its
  latency, learns, for a parent they may read, how many of its children they may not read, up
  to ``LIMIT``; and, from the number and kind of the walk's lookups, what holds on such a child:
  which arms of the permission hold on it, as far as the lookups differ with them. Of a row
  whose key or whose parent the reader does not already hold they learn nothing, but for this:
  with ``"identifiers"``, the walk follows a hidden child's foreign keys and grants to groups,
  and its lookups show what holds on the rows they name.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, cast

from django.db import models
from django.db.models.expressions import Col, ColPairs, OrderBy
from django.db.models.fields.related_lookups import RelatedExact, RelatedIn, RelatedIsNull
from django.db.models.fields.tuple_lookups import TupleIn
from django.db.models.functions import Random
from django.db.models.lookups import Exact, In, IntegerFieldExact, IsNull, Lookup
from django.db.models.query import (
    FlatValuesListIterable,
    ModelIterable,
    NamedValuesListIterable,
    ValuesIterable,
    ValuesListIterable,
)
from django.db.models.sql import Query as SQLQuery
from django.db.models.sql.datastructures import Join
from django.db.models.sql.where import AND, NothingNode, WhereNode

from .conf import app_settings

if TYPE_CHECKING:
    from django.db.models.sql import Query

    from .managers import RebacQuerySet
    from .types import SubjectRef

# The most candidates a read is decided by; one more is read to tell whether there are more.
LIMIT = 256

_log = logging.getLogger("rebac.bounded")

# The row shapes Django builds from a statement's columns, and nothing else.
_ITERABLES = (
    ModelIterable,
    ValuesIterable,
    ValuesListIterable,
    FlatValuesListIterable,
    NamedValuesListIterable,
)


def mode() -> str:
    """``"keys"`` (``True``), ``"identifiers"`` or empty for off."""
    value = app_settings.REBAC_BOUNDED_READ
    if value is True:
        return "keys"
    return "identifiers" if value == "identifiers" else ""


def enabled() -> bool:
    return bool(mode())


def read(queryset: RebacQuerySet[Any], query: Query, *, built: bool) -> Query | None:
    """The query this evaluation of ``queryset`` reads instead of ``query``; ``None`` to read ``query``.

    ``query`` is the one the evaluation decided (``RebacQuerySet._apply_scope_in_place``),
    carrying its decision and its compiled scope; ``built``: the evaluation built that scope
    rather than finding it built. The query returned is the same query with the admitted keys
    in place of that scope, and serves this evaluation alone: the queryset keeps ``query``.
    """
    if _checking.get():
        # The reader's own read of the parents a bounded read names (``_unreadable``).
        return None
    # A scope kept from an earlier evaluation (an eager ``scoped()``, an object read twice) keeps
    # what it pinned then: its policy, and inside an evaluator scope its grant profile and
    # constant-arm witnesses, which only leave rows out. The walk would read them now.
    reason = refusal(queryset, query) if built else "its scope was built by an earlier evaluation"
    if reason is None:
        found = _decided(queryset, query)
        if not isinstance(found, str):
            _log.debug("A read of %s is decided by its candidates.", query.model)
            return found
        reason = found
    _log.debug("A read of %s keeps its statement: %s.", query.model, reason)
    return None


def refusal(queryset: RebacQuerySet[Any], query: Query) -> str | None:
    """Why a read of ``queryset``, decided as ``query``, keeps its compiled statement; ``None``
    where it is decided by its candidates. Read from the decision and the query's structure,
    before any row is read.

    A read is decided by its candidates only where the statement that reads the admitted keys
    returns what the compiled statement returns: every row it reads is one row of the model,
    filtered by conditions on that row and on single rows it joins, so a row is kept exactly
    when its key is kept; and where its conditions are all on keys (``_conditions``), since the
    key read runs them without the permission and what follows shows what it found. Everything
    else keeps the compiled statement, each for its reason.
    """
    from . import structural, walk
    from .actors import enforcing, is_anonymous_actor
    from .managers import (
        _has_opaque_write_expression,
        _nested_queries,
        _ScopeWhere,
        _without_query_scope,
    )
    from .resources import model_resource_type

    decision = getattr(query, "_rebac_scope_for", None)
    if decision is None:
        return "no decision"
    actor, bypass, using = decision
    if bypass:
        return "a bypass reads every row"
    if actor is None:
        return "no actor"
    if not enforcing():
        return "nothing is enforced"
    if queryset._iterable_class not in _ITERABLES:
        # A third-party row class may read the statement in its own way.
        return "its rows are built by an iterable class of its own"
    model = query.model
    resource_type = model_resource_type(model) if model is not None else None
    if model is None or not resource_type:
        return "the model has no resource type"
    if walk._integer_identity(model) is None:
        # The candidates are keys; the walk reads integer keys that are the identity.
        return "the model's identity is not its integer primary key"
    if query.combinator or query.combined_queries:
        # Each operand has a scope of its own.
        return "a set combination"
    if not any(isinstance(child, _ScopeWhere) for child in query.where.children):
        # A structural delivery: no permission statement to replace.
        return "it carries no compiled scope"
    plain = _without_query_scope(query)
    if any(isinstance(node, NothingNode) for node in _nodes(plain.where)):
        # ``none()``, an empty ``__in``: Django sends no statement for it.
        return "it selects nothing"
    if plain.select_for_update:
        # A locking read decides and locks in one statement; this decides in statements that
        # lock nothing, before the rows' statement locks what they admitted.
        return "select_for_update()"
    if plain.distinct_fields:
        return "distinct(*fields)"
    if plain.group_by is not None:
        # Its rows are groups, not rows of the model.
        return "an aggregate or a grouping"
    if plain.extra or plain.extra_tables or plain.extra_order_by:
        return "extra()"
    for join in plain.alias_map.values():
        if not isinstance(join, Join):
            continue
        if join.filtered_relation is not None:
            return "a FilteredRelation"
        if not _single(join.join_field):
            # A row read once for each related row: more statement rows than model rows,
            # some of them tied in any ordering.
            return "a join that can repeat a row"
    if next(_nested_queries(plain), None) is not None:
        # Another statement inside this one, with a scope of its own.
        return "a query embedded in it"
    for annotation in plain.annotations.values():
        if getattr(annotation, "contains_aggregate", False):
            return "an aggregate"
        if getattr(annotation, "contains_over_clause", False):
            return "a window"
    if plain.where.contains_over_clause:
        return "a window"
    if any(
        isinstance(node, Random)
        for expression in (plain.where, *plain.annotations.values())
        for node in _nodes(expression)
    ):
        # The key read and the rows' statement would each draw their own.
        return "a random value"
    if _has_opaque_write_expression(plain):
        return "hand-written SQL"
    conditions = _conditions(plain, model)
    if isinstance(conditions, str):
        # The key read runs these conditions without the permission: what it finds is seen in
        # what follows it. Conditions on keys alone keep that to what the documents state.
        return conditions
    terms, base = _ordering(plain, using)
    for term in terms:
        if any(isinstance(node, Random) for node in term.flatten()):
            return "a random ordering"
        if _has_opaque_write_expression(term) or next(_nested_queries(term), None) is not None:
            return "an ordering by hand-written SQL or another query"
    if not _total(terms, base, model) and not _one_row(plain, model):
        # Rows the ordering leaves tied come in an order neither statement determines, and a
        # slice of them is not determined either.
        return "its ordering leaves ties and its conditions name no single row"
    policy = structural.current_policy()
    if policy is None:
        return "no local backend"
    if getattr(query, "_rebac_scope_policy", None) != policy.key:
        # A scope kept from an evaluation under another policy reads nothing past the
        # policy's revision fence; the walk would read under this one.
        return "its scope was built under another policy"
    if policy.snapshot.overrides or policy.tagged.arms or policy.tagged.sites:
        return "the policy carries an override"
    if actor.optional_relation or actor.subject_id in ("", "*") or is_anonymous_actor(actor):
        return "an actor the walk does not read"
    part = walk.compiler_part(
        policy, resource_type, _action(queryset, model), keys_only=mode() == "keys"
    )
    if part is not None:
        return f"the walk would ask the compiled statement: {part}"
    # Every key the conditions name must be one the reader holds (``_unheld``): otherwise what
    # follows the key read would say what it found among rows the reader does not hold.
    unheld = _unheld(conditions, model, actor, using, policy.key)
    if isinstance(unheld, str):
        return unheld
    return None


def _decided(queryset: RebacQuerySet[Any], query: Query) -> Query | str:
    """The query that reads the admitted keys, or why the compiled statement is read after all."""
    from . import walk
    from .backends import backend
    from .compile.read import scope_admits_nothing
    from .managers import _ScopeWhere, _without_query_scope
    from .resources import model_resource_type

    actor, _bypass, using = query._rebac_scope_for  # type: ignore[attr-defined]
    model = cast(type[models.Model], query.model)
    action = _action(queryset, model)
    local = backend()
    if scope_admits_nothing(backend=local, model=model, action=action, actor=actor, using=using):
        # The actor's grant profile leaves no arm: that statement costs nothing to run.
        return "the compiled scope admits no row"
    plain = _without_query_scope(query)
    conditions = _conditions(plain, model)
    policy_key = getattr(query, "_rebac_scope_policy", None)
    parents = _unheld(cast(Any, conditions), model, actor, using, policy_key)
    if isinstance(parents, str):
        return parents
    unreadable = _unreadable(parents, actor, using)
    if unreadable is not None:
        return f"it names a parent the reader may not read ({unreadable._meta.label})"
    # The key read: the model's own table and the read's conditions, which ``refusal`` has
    # shown are conditions on its keys alone. No join, ordering, slice or projection of the
    # read reaches it, so neither changes which keys it finds.
    probe = SQLQuery(model)
    probe.where = plain.where.clone()
    rows = model._base_manager.db_manager(using).all()
    rows.query = probe
    keys = list(rows.values_list("pk", flat=True)[: LIMIT + 1])
    if len(keys) > LIMIT:
        return f"its conditions select more than {LIMIT} rows"
    wire = {str(key): key for key in keys}
    held = walk.decided_alone(
        backend=local,
        resource_type=cast(str, model_resource_type(model)),
        action=action,
        actor=actor,
        ids=wire,
        using=using,
        policy_key=policy_key,
        keys_only=mode() == "keys",
    )
    if held is None:
        return "the walk would ask the compiled statement for these rows"
    admitted = sorted(wire[value] for value in held)
    restriction = plain.build_where(models.Q(pk__in=admitted))
    plain.where = WhereNode([plain.where, _ScopeWhere(children=[restriction])])
    cast(Any, plain)._rebac_scope_for = query._rebac_scope_for  # type: ignore[attr-defined]
    cast(Any, plain)._rebac_scope_policy = getattr(query, "_rebac_scope_policy", None)
    return plain


# The lookups a condition may compare a key with: Django's own, the integer field's ``exact`` and
# the related-field forms. A subclass of these elsewhere may write other SQL.
_KEY_LOOKUPS = (
    Exact,
    IntegerFieldExact,
    In,
    IsNull,
    RelatedExact,
    RelatedIn,
    RelatedIsNull,
)


def _conditions(query: Query, model: type[models.Model]) -> str | list[tuple[Any, Any]]:
    """Why the conditions of ``query`` are not all of the one form a bounded read accepts; where
    they are, each as ``(field, values)``, ``values`` ``None`` for ``isnull``.

    The form: a condition on the model's own primary key or on a foreign-key column of its own
    table (``thread_id``, or ``thread`` compared with a key), by ``exact``, ``in`` or
    ``isnull``, against literal values, every condition joined to the others by ``AND``. The
    conditions are read from the query's ``WHERE`` clause, wherever they came from: the caller,
    a related manager, a prefetch that binds the parents' keys (a reverse one by their foreign
    key, a forward one by the related rows' own keys), a manager's own default filter. Whether
    the reader holds the keys they name is ``_unheld``'s.
    """
    alias = query.get_initial_alias()
    concrete = model._meta.concrete_model
    found: list[tuple[Any, Any]] = []
    pending: list[Any] = [query.where]
    while pending:
        node = pending.pop()
        if isinstance(node, WhereNode):
            if node.negated:
                return "a negated condition (exclude(), ~Q)"
            if node.connector != AND and len(node.children) > 1:
                return "conditions joined by OR or XOR"
            pending.extend(node.children)
            continue
        if not isinstance(node, Lookup):
            return "a condition that is not a lookup"
        column, values = node.lhs, node.rhs if isinstance(node, In) else (node.rhs,)
        if type(node) is TupleIn:
            # Django's form of a forward foreign key's prefetch, ``pk IN ((1,), (2,))``: read as
            # ``pk IN (1, 2)`` where it names one column.
            if type(column) is not ColPairs or len(column) != 1:
                return "a condition on several columns at once"
            [column] = column.get_cols()
            if not isinstance(values, (list, tuple, set, frozenset)) or any(
                not isinstance(value, tuple) or len(value) != 1 for value in values
            ):
                return "a condition compared with an expression"
            values = [value for (value,) in values]
        elif type(node) not in _KEY_LOOKUPS:
            return f"a condition by the lookup {node.lookup_name!r}, not exact, in or isnull"
        if type(column) is not Col:
            return "a condition on an expression, a transform or an annotation"
        if column.alias != alias:
            return "a condition reached through a join"
        field = column.target
        if field.model is not concrete or not (
            field.primary_key
            or (field.concrete and (field.many_to_one or field.one_to_one) and field.column)
        ):
            return "a condition on a column that is neither the primary key nor a foreign key"
        if not isinstance(values, (list, tuple, set, frozenset)) or any(
            hasattr(value, "resolve_expression") for value in values
        ):
            return "a condition compared with an expression"
        found.append((field, None if isinstance(node, IsNull) else list(values)))
    return found


def _unheld(
    conditions: list[tuple[Any, Any]],
    model: type[models.Model],
    actor: SubjectRef,
    using: str,
    policy_key: Any,
) -> str | dict[type[models.Model], set[Any]]:
    """Why the conditions name a key the reader does not hold; where every key they name is
    held or may be, the parents still to check, by model (proposal 0025).

    A key is held where the running evaluation shows it, for the same actor, database and
    policy (``structural._Delivery``, the record a prefetch keeps while it runs): a primary key
    where a row the evaluation admitted refers to it by a foreign key (a forward prefetch's
    rows), a parent where the evaluation admitted it under its model's default action (a
    reverse prefetch's parents). A parent named otherwise is checked by ``_unreadable``, once
    for all those named; a primary key named otherwise, as a client names it in ``get(pk=…)``,
    is not held. A read with no condition, or one by ``isnull``, names no key it holds.
    """
    from . import structural, walk
    from .resources import model_resource_type

    if not conditions:
        return "it names no key the reader holds: it has no condition"
    delivery = structural._delivery.get()
    if delivery is not None and not delivery.serves(actor):
        delivery = None
    parents: dict[type[models.Model], set[Any]] = {}
    for field, values in conditions:
        if values is None:
            return "it names no key the reader holds: isnull"
        named = {value for value in values if value is not None}
        if field.primary_key:
            referred = (
                delivery.referred.get((model, using), {}).get(policy_key, set())
                if delivery is not None
                else set()
            )
            if not named <= referred:
                return "a key that no row this evaluation read refers to"
            continue
        parent = field.related_model
        if (
            not isinstance(parent, type)
            or not field.target_field.primary_key
            or not model_resource_type(parent)
            or walk._integer_identity(parent) is None
        ):
            return "a foreign key whose parents the reader's rights cannot be read for"
        action = str(getattr(cast(Any, parent)._meta, "rebac_default_action", "read"))
        admitted = (
            delivery.rows.get((parent, using, action), {}).get(policy_key, set())
            if delivery is not None
            else set()
        )
        if named - admitted:
            parents.setdefault(parent, set()).update(named - admitted)
    return parents


# Set while ``_unreadable`` reads the parents a read names: that read is the reader's own read of
# those parents, today's statement, and is not decided by candidates itself.
_checking: ContextVar[bool] = ContextVar("rebac_bounded_checking", default=False)


def _unreadable(
    parents: dict[type[models.Model], set[Any]], actor: SubjectRef, using: str
) -> type[models.Model] | None:
    """The first parent model of which a named row is not one the actor may read; ``None``
    where every one is. One read per parent model, for all its named rows together, by the
    statement the reader's own read of those rows sends today, so that what it sends depends
    only on rows the reader named and whether they may read them."""
    token = _checking.set(True)
    try:
        for parent, keys in sorted(parents.items(), key=lambda item: item[0]._meta.label):
            manager = cast(Any, parent)._default_manager
            if not hasattr(manager, "with_actor"):
                return parent
            readable = set(
                manager.with_actor(actor)
                .using(using)
                .filter(pk__in=sorted(keys))
                .order_by()
                .values_list("pk", flat=True)
            )
            if not keys <= readable:
                return parent
    finally:
        _checking.reset(token)
    return None


def _action(queryset: RebacQuerySet[Any], model: type[models.Model]) -> str:
    return str(queryset._rebac_action or getattr(model._meta, "rebac_default_action", "read"))


def _single(field: Any) -> bool:
    """Whether a join along ``field`` meets at most one row for each row it starts from."""
    if isinstance(field, models.ForeignObjectRel):
        return bool(field.one_to_one)
    return bool(getattr(field, "many_to_one", False) or getattr(field, "one_to_one", False))


def _total(terms: list[Any], base: str, model: type[models.Model]) -> bool:
    """Whether the ordering ends ties: it orders by the model's own primary key column."""
    pk = model._meta.pk
    return any(
        isinstance(term, OrderBy)
        and isinstance(term.expression, Col)
        and term.expression.target is pk
        and term.expression.alias == base
        for term in terms
    )


def _one_row(query: Query, model: type[models.Model]) -> bool:
    """Whether the query's conditions bind a unique column of the model to one literal value,
    so that it selects one row at most: ``get(pk=...)``, ``filter(pk__in=[one])``, a forward
    foreign key's prefetch of one row. Only
    conditions every row must meet are read: the positive ``AND`` path of the WHERE clause,
    on the query's own table."""
    alias = query.get_initial_alias()
    pending: list[Any] = [query.where]
    while pending:
        node = pending.pop()
        if type(node) is WhereNode:
            if node.connector == "AND" and not node.negated:
                pending.extend(node.children)
            continue
        if not isinstance(node, (Exact, In)):
            continue
        column, values = node.lhs, node.rhs if isinstance(node, In) else (node.rhs,)
        if type(node) is TupleIn and type(column) is ColPairs and len(column) == 1:
            # A forward foreign key's prefetch: ``pk IN ((1,),)``.
            [column] = column.get_cols()
            if not isinstance(values, (list, tuple, set)) or any(
                not isinstance(value, tuple) or len(value) != 1 for value in values
            ):
                continue
            values = [value for (value,) in values]
        if type(column) is not Col or column.alias != alias:
            continue
        field = column.target
        if field.model is not model._meta.concrete_model or not (field.primary_key or field.unique):
            continue
        if not isinstance(values, (list, tuple, set)) or any(
            value is None or hasattr(value, "resolve_expression") for value in values
        ):
            continue
        try:
            if len(set(values)) == 1:
                return True
        except TypeError:
            continue
    return False


def _nodes(expression: Any) -> Iterator[Any]:
    """Every expression in ``expression``, a where tree's lookups and their operands included."""
    yield expression
    if isinstance(expression, WhereNode):
        for child in expression.children:
            yield from _nodes(child)
        return
    sources = getattr(expression, "get_source_expressions", None)
    if callable(sources):
        for source in cast(Iterable[Any], sources()):
            if source is not None:
                yield from _nodes(source)


def _ordering(query: Query, using: str) -> tuple[list[Any], str]:
    """The ordering Django compiles for ``query``, resolved, and the alias of its own table."""
    compiler = query.clone().get_compiler(using)
    terms = [
        expression.resolve_expression(compiler.query, allow_joins=True, reuse=None)
        for expression, _ in compiler._order_by_pairs()
    ]
    return terms, query.get_meta().db_table
