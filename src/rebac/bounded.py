"""A narrow list read decided by its candidate rows (proposal 0025).

Experimental and off by default (``REBAC_BOUNDED_READ``). A compiled scope builds every id
set its permission reaches, however few rows the read's own conditions select: on a wide
policy, the files of one folder cost what a page of every file costs. With the setting on, a
list read whose conditions select at most ``LIMIT`` rows is decided by those rows instead:

1. **Candidates.** The read's own conditions, with no permission, no ordering and no slice,
   select at most ``LIMIT + 1`` primary keys: one statement that selects the key column alone.
2. **Decision.** Where ``LIMIT`` or fewer come back, they are every row the conditions select,
   and the walk decides exactly those keys (``rebac.walk.decided_alone``): one small indexed
   lookup per relation the permission reaches, and never the compiled statement.
3. **Rows.** The read's own statement runs with the admitted keys in place of its scope: its
   conditions, joins, projection, ordering and slice as written, the slice taken after the
   decision.

Where more than ``LIMIT`` keys come back, or anything here cannot be shown to give what the
compiled statement gives, the read sends its compiled statement, unchanged. It never reads a
second batch of candidates.

Only an evaluation that fills the result cache (``RebacQuerySet._fetch_all``: iteration,
``len()``, ``bool()``, slicing and indexing, ``get()``, ``first()``, ``last()``,
``earliest()``, ``latest()``, ``in_bulk()``, ``values()`` and ``values_list()``, a prefetch's
child read) is served, over a scope that evaluation built; ``refusal`` is the rule, read from
the query that evaluation decided. What follows the rows (stamping, structural admission,
prefetches, field redaction, the ``rebac_select_related()`` guards) is the same code on the
same rows.

Two things cross a boundary the compiled statement keeps. **Keys**: up to ``LIMIT + 1``
primary keys of rows the actor may not read enter the process, and the walk's lookups carry
them as statement parameters (in a statement log, Django's SQL debug log, an error report's
frame locals). With ``True`` no other column of such a row is read: the walk decides only a
permission it reads from the candidates' keys alone (grants that name the actor or everyone,
constants, filtered constants). With ``"identifiers"`` the walk also reads, of each candidate,
the foreign keys the permission follows and the subjects of the grants on it that name a
subject set or a parent, and the lookups carry those too: keys of other rows, never content.
**Timing**: whether more than ``LIMIT`` rows, hidden ones included, match the read's
conditions shows as latency.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any, cast

from django.db import models
from django.db.models.expressions import Col, OrderBy
from django.db.models.functions import Random
from django.db.models.lookups import Exact, In
from django.db.models.query import (
    FlatValuesListIterable,
    ModelIterable,
    NamedValuesListIterable,
    ValuesIterable,
    ValuesListIterable,
)
from django.db.models.sql.datastructures import Join
from django.db.models.sql.where import NothingNode, WhereNode

from .conf import app_settings

if TYPE_CHECKING:
    from django.db.models.sql import Query

    from .managers import RebacQuerySet

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
    when its key is kept. Everything else keeps the compiled statement, each for its reason.
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
        # The candidates' statement would lock rows the actor may not read.
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
    probe = plain.clone()
    probe.clear_ordering(force=True)
    probe.clear_limits()
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
        policy_key=getattr(query, "_rebac_scope_policy", None),
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
    so that it selects one row at most: ``get(pk=...)``, ``filter(pk__in=[one])``. Only
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
        column = node.lhs
        if type(column) is not Col or column.alias != alias:
            continue
        field = column.target
        if field.model is not model._meta.concrete_model or not (field.primary_key or field.unique):
            continue
        values = node.rhs if isinstance(node, In) else (node.rhs,)
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
