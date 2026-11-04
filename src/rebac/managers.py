"""RebacManager + RebacQuerySet.

Per ARCHITECTURE.md § Three actor-resolution paths:
  1. Per-queryset actor/action (.with_actor / .with_action)
  2. Per-queryset sudo (.sudo) — bypass with a mandatory reason
  3. current_actor() ContextVar — populated by middleware / Celery hooks
  4. Fallback — STRICT_MODE=True raises; else system_context()

The actor lives on the queryset instance (NOT a ContextVar). It survives
chaining via `_clone()`; `_fetch_all()` and the iterators stamp it onto every
loaded instance (it is not visible inside `from_db()`).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import AsyncIterator, Collection, Iterable, Iterator
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar, cast, overload

from asgiref.sync import sync_to_async
from django.db import connections, models
from django.db.models.expressions import Col, RawSQL
from django.db.models.sql import Query
from django.db.models.sql.where import ExtraWhere, NothingNode, WhereNode

from ._id import resource_id_attr
from .actors import (
    ActorLike,
    current_sudo_reason,
    grant_subject_ref,
    require_sudo_allowed,
    resolve,
    to_subject_ref,
)
from .actors import current_actor as _current_actor
from .actors import is_sudo as _is_sudo_ambient
from .database import database_context
from .errors import MissingActorError, PermissionDenied
from .field_visibility import (
    apply_field_visibility,
    effective_field_deny_mode,
    gated_read_fields,
    held_ids,
    runtime_field_deny_mode,
    validate_field_deny_mode,
    warn_raise_mode_degrades,
)
from .resources import model_resource_type
from .schema.cache import schema_operation
from .types import FieldDenyMode, SubjectRef

_M = TypeVar("_M", bound=models.Model)

if TYPE_CHECKING:
    from .mixins import RebacMixin


def share_scoped_definitions[T: models.Model](
    outer_queryset: models.QuerySet[T],
) -> models.QuerySet[T]:
    """Share CTE definitions across a plain union of prepared REBAC operands.

    Scoped operands must use one actor, policy and database alias. Unscoped
    operands pass through. The returned queryset leaves the input untouched.
    """
    from .compile.read import share_combined_scopes

    result = outer_queryset.all()
    query = result.query
    if query.combinator != "union" or len(query.combined_queries) < 2:
        return result
    if share_combined_scopes(query) is False:
        raise PermissionDenied("Union operands must have compatible prepared REBAC scopes")
    return result


class _ScopeWhere(WhereNode):
    """A removable authorization restriction, separate from caller predicates."""


class _RebacQuery(Query):
    """Cover expressions which copy QuerySet.query without resolving the QS."""

    _rebac_state: dict[str, Any]
    # Django builds these bounded pk subqueries while combining sliced
    # querysets. They inherit the combination's policy, not a separate actor.
    _rebac_boolean_operand = False

    # The bypass reason of an embedded queryset, audited when the statement it
    # was resolved into is compiled for execution, never when it is built.
    _rebac_pending_bypass_audit: str | None = None

    # Django constructs a query of this class itself only as a piece of the
    # statement it is building: the NOT EXISTS of an exclude() across a to-many
    # relation (Query.split_exclude). A queryset's query gets this class by
    # assignment and never runs __init__, so a query constructed here is not an
    # embedded queryset. It has no scope decision of its own; the queryset
    # that owns the statement scopes it.
    _rebac_statement_part = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rebac_statement_part = True

    def as_sql(self, compiler: Any, connection: Any) -> Any:
        # Compiled before the statement that holds it was checked (its
        # queryset is not a rebac one, or no check ran): checked now.
        with database_context(connection.alias):
            _check_subquery(self)
            reason = self._rebac_pending_bypass_audit
            if reason is not None:
                from .audit import emit_bypass

                emit_bypass(reason, actor=_current_actor())
            return super().as_sql(compiler, connection)

    # A resolved actor-scoped subquery carries what checking it needs, and
    # is checked once: with the statement that holds it, when that statement
    # is checked (sharing its schema read), else when it is compiled.  Django
    # resolves it again while compiling the enclosing statement (a lookup's
    # left-hand side); that changes nothing the check read.
    _rebac_check: tuple[RebacQuerySet[Any], SubjectRef | None] | None = None
    _rebac_checked = False

    @schema_operation
    def resolve_expression(self, *args: Any, **kwargs: Any) -> Self:
        if self._rebac_boolean_operand:
            return cast(Self, super().resolve_expression(*args, **kwargs))
        if self._rebac_statement_part and not hasattr(self, "_rebac_state"):
            return cast(Self, super().resolve_expression(*args, **kwargs))
        clone = cast(Self, self.clone())
        state = getattr(self, "_rebac_state", {})
        queryset: RebacQuerySet[Any] = RebacQuerySet(
            model=self.model, query=clone, using=state.get("_db")
        )
        for name, value in state.items():
            setattr(queryset, name, value)
        applied_here = not queryset._rebac_scope_applied
        # Building an expression runs no query, so it must write no audit row
        # (annotations are built at import time, during app initialisation).
        queryset._apply_scope_in_place(audit=False)
        # The decision the scope was applied with, here or earlier: a queryset
        # evaluated under actor_context() may be embedded after it has closed.
        decision = queryset._rebac_scope_decision
        actor, bypass = decision if decision is not None else queryset.effective_actor(strict=True)
        pending = queryset._bypass_audit_reason() if applied_here and bypass else None

        # A set combination has one actor policy, owned by its root queryset.
        # _scope_query already applied it to each operand (or the root bypass
        # lifted it). Django resolves each operand again below; prevent stale
        # operand metadata from reapplying a different actor or rejecting an
        # intentionally unscoped right-hand operand.
        def resolved_parts(query: Query) -> None:
            for part in query.combined_queries:
                if isinstance(part, _RebacQuery):
                    part._rebac_state = {
                        **state,
                        "_rebac_scope_applied": True,
                        "_rebac_scope_decision": (actor, bypass),
                    }
                    # The combination's own check reads every operand.
                    part._rebac_checked = True
                resolved_parts(part)

        resolved_parts(clone)
        resolved = cast(Self, Query.resolve_expression(clone, *args, **kwargs))
        if not bypass and not self._rebac_checked:
            # This subquery carries its own scope decision, so it answers for
            # what it selects, filters and orders on, whether its scope was
            # applied here or earlier (scoped(), or an evaluation that was
            # refused). It is checked as resolved into the enclosing
            # statement, as Django compiles it; the columns of the outer row it
            # refers to are the outer query's to answer for.
            cast(Any, queryset)._fields = tuple(clone.values_select) or None
            resolved._rebac_check = (queryset, actor)
        if pending is not None:
            resolved._rebac_pending_bypass_audit = pending
        return resolved


def _check_subquery(query: Query) -> None:
    """Check an actor-scoped subquery once, as resolved into its statement."""
    pending = getattr(query, "_rebac_check", None)
    if pending is None or getattr(query, "_rebac_checked", False):
        return
    queryset, actor = pending
    queryset._guard_projected_field_reads(actor, False, query=query)
    cast(Any, query)._rebac_checked = True


def _nested_queries(expression: Any, *, boolean_operands_only: bool = False) -> Iterator[Query]:
    """The queries nested anywhere in an expression, or in a query's clauses."""
    if isinstance(expression, Query):
        group_by = expression.group_by if isinstance(expression.group_by, tuple) else ()
        conditions = [
            getattr(getattr(join, "filtered_relation", None), "resolved_condition", None)
            for join in expression.alias_map.values()
        ]
        parts: Iterable[Any] = (
            expression.where,
            *expression.annotations.values(),
            *expression.select,
            *expression.order_by,
            *group_by,
            *conditions,
            *expression.combined_queries,
        )
    else:
        sources = getattr(expression, "get_source_expressions", None)
        parts = cast(Iterable[Any], sources()) if callable(sources) else ()
    for part in parts:
        if isinstance(part, Query):
            if boolean_operands_only:
                if isinstance(part, _RebacQuery) and part._rebac_boolean_operand:
                    yield part
                # An independently authored subquery owns its own actor; its
                # contents are not operands of the enclosing combination.
                continue
            yield part
        if part is not None:
            yield from _nested_queries(part, boolean_operands_only=boolean_operands_only)


class _Readable(models.Case):
    """A gated column as one actor may read it: its value where the actor holds
    ``read__<field>``, ``NULL`` elsewhere (``RebacQuerySet.readable``).

    The guards trust it only as built.  When it is first resolved its cases
    are rebuilt from the model, field and readers it was made with, so a change
    made to them before reaches no statement, and the query it is resolved
    into must be of that model and resource type.
    """

    rebac_actor: SubjectRef
    rebac_field: str
    rebac_model: type[models.Model]
    rebac_readers: models.Exists
    rebac_resolved = False

    def resolve_expression(
        self,
        query: Any = None,
        allow_joins: bool = True,
        reuse: Any = None,
        summarize: bool = False,
        for_save: bool = False,
    ) -> Any:
        if self.rebac_resolved:
            return super().resolve_expression(query, allow_joins, reuse, summarize, for_save)
        model = getattr(query, "model", None)
        if (
            model is None
            or model._meta.concrete_model is not self.rebac_model._meta.concrete_model
            or model_resource_type(model) != model_resource_type(self.rebac_model)
        ):
            used_in = model.__name__ if model is not None else "an expression"
            raise ValueError(
                f"readable({self.rebac_field!r}) of {self.rebac_model.__name__} is used in "
                f"a query of {used_in}: build it from a queryset of that model."
            )
        built = self.copy()
        built.cases = [models.When(self.rebac_readers, then=models.F(self.rebac_field))]
        built.default = models.Value(None)
        resolved = super(_Readable, built).resolve_expression(
            query, allow_joins, reuse, summarize, for_save
        )
        resolved.rebac_resolved = True
        return resolved


def _without_scope(node: WhereNode) -> WhereNode:
    clone = node.clone()
    clone.children = [
        _without_scope(child) if isinstance(child, WhereNode) else child
        for child in node.children
        if not isinstance(child, _ScopeWhere)
    ]
    return clone


def _without_query_scope(query: Query) -> Query:
    clone = query.clone()
    plan = getattr(clone, "_rebac_subject_plan", None)
    if plan is not None:
        owned = {id(cte) for cte in plan[1]}
        if hasattr(clone, "_with_ctes"):
            cloned_ctes = cast(Any, clone)._with_ctes
            cast(Any, clone)._with_ctes = tuple(cte for cte in cloned_ctes if id(cte) not in owned)
        delattr(clone, "_rebac_subject_plan")
    if hasattr(clone, "_rebac_combined_ctes_shared"):
        delattr(clone, "_rebac_combined_ctes_shared")
    if isinstance(clone, _RebacQuery) and clone._rebac_boolean_operand:
        clone._rebac_check = None
        clone._rebac_checked = False
        clone._rebac_pending_bypass_audit = None
    clone.where = _without_scope(clone.where)
    clone.where = clone.where.replace_expressions(
        {
            operand: _without_query_scope(operand)
            for operand in _nested_queries(clone.where, boolean_operands_only=True)
        }
    )
    clone.combined_queries = tuple(_without_query_scope(part) for part in clone.combined_queries)
    return clone


def _clause_expressions(
    query: Query, using: str, *, clears_ordering: bool | None
) -> tuple[list[Any], bool]:
    """What a query filters, groups, orders and deduplicates on, as Django compiles it.

    For the query and every operand of its set combinations: the ``where``
    clause with the conditions of the filtered relations it joins, the
    explicit ``GROUP BY``, the ``distinct(*fields)`` columns and the ordering,
    all without the library's own scope.  ``clears_ordering`` is how the
    operation treats the query's ordering, as ``Query.clear_ordering(force=)``:
    ``True`` drops it (an existence test, a write), ``False`` keeps it only on
    a sliced or ``distinct(*fields)`` query (a count, an aggregate), ``None``
    keeps it (iteration).  The second value tells whether any of it is SQL
    written by hand.
    """
    scoped = _without_query_scope(query)
    if clears_ordering is not None:
        scoped.clear_ordering(force=clears_ordering)
    expressions: list[Any] = []
    for part in _selections(scoped):
        expressions.extend(_compiled_ordering(part, using))
        if part.combinator:
            # The operands' clauses are compiled, not the combination's own.
            continue
        expressions.append(part.where)
        if isinstance(part.group_by, tuple):
            expressions.extend(part.group_by)
        for join in part.alias_map.values():
            condition = getattr(
                getattr(join, "filtered_relation", None), "resolved_condition", None
            )
            if condition is not None:
                expressions.append(condition)
        resolving = part.clone()
        expressions.extend(resolving.resolve_ref(name) for name in part.distinct_fields)
    return expressions, any(_has_opaque_write_expression(e) for e in expressions)


def _hidden_from(
    actor: SubjectRef | None, model: type[models.Model], names: Iterable[str]
) -> list[str]:
    """The gated fields of ``model`` among ``names`` that some row hides from the actor.

    An actor whose ``read__<field>`` holds on every row of the type (a grant
    that does not depend on the row) reads the field everywhere.
    """
    from .backends import backend

    resource_type = model_resource_type(model)
    return sorted(
        name
        for name in names
        if actor is None
        or resource_type is None
        or not backend().grants_all(
            subject=actor, action=f"read__{name}", resource_type=resource_type
        )
    )


def _compiled_ordering(query: Query, using: str) -> list[Any]:
    """The ordering Django's compiler emits for a query.

    The query's own, else its model's default; an ordering by a relation is
    the related model's default, and ``extra(order_by=...)`` is read as
    Django reads it.  A grouped query drops a model's default ordering.  The
    terms are resolved as ``SQLCompiler.get_order_by`` resolves them, without
    compiling them: a subquery may still refer to its outer query.
    """
    compiler = query.clone().get_compiler(using)
    ordering = [
        expression.resolve_expression(compiler.query, allow_joins=True, reuse=None)
        for expression, _ in compiler._order_by_pairs()
    ]
    if compiler._meta_ordering and query.group_by is not None:
        return []
    return ordering


def _expression_columns(
    expression: Any,
    _inner_aliases: frozenset[str] = frozenset(),
    *,
    reader: SubjectRef | None = None,
) -> Iterable[Col]:
    """Columns of the enclosing row that a resolved ORM expression reads.

    A nested query reads two kinds of column: its own tables' (under its own
    aliases, decided by its own scope) and the enclosing row's, reached through
    ``OuterRef``. Only the latter belong to the enclosing projection, so a
    column resolved against an alias of a nested query is not attributed to it.
    A statement part Django builds for the query itself (the ``NOT EXISTS`` of
    an ``exclude()`` across a to-many relation) has no scope of its own: its
    columns are the enclosing query's, like a join.  A ``readable()`` value
    built for ``reader`` reads only what that actor may read.
    """
    if isinstance(expression, Col):
        if expression.alias not in _inner_aliases:
            yield expression
        return
    if (
        reader is not None
        and isinstance(expression, _Readable)
        and expression.rebac_resolved
        and expression.rebac_actor == reader
    ):
        return
    if isinstance(expression, Query):
        statement_part = getattr(expression, "_rebac_statement_part", False) and not hasattr(
            expression, "_rebac_state"
        )
        inner = (
            _inner_aliases
            if statement_part and not _inner_aliases
            else _inner_aliases | frozenset(expression.alias_map)
        )
        for part in (
            expression.where,
            *expression.annotations.values(),
            *expression.order_by,
            *expression.select,
            *expression.combined_queries,
        ):
            yield from _expression_columns(part, inner, reader=reader)
        return
    sources = getattr(expression, "get_source_expressions", None)
    if callable(sources):
        for source in cast(Iterable[Any], sources()):
            yield from _expression_columns(source, _inner_aliases, reader=reader)


def _selections(query: Query) -> Iterator[Query]:
    """A query and every operand of its set combinations, as Django selects from them.

    A combination returns the columns of each operand.  An operand without a
    projection of its own is given the combination's when it is compiled
    (``SQLCompiler._get_combinator_part_sql``); the same is done here.
    """
    yield query
    selected = query.selected
    for operand in query.combined_queries:
        if selected is not None and operand.selected is None:
            operand = operand.clone()
            operand.set_values(selected)
        yield from _selections(operand)


def _selected_gated(
    model: type[models.Model],
    expressions: Iterable[Any],
    query: Query | None = None,
    *,
    reader: SubjectRef | None = None,
) -> dict[type[models.Model], set[str]]:
    """The gated fields that selected columns and computed values read, by model.

    A column is gated by the model it is read from: the query's own, or one
    the query joins.  A column of the query that encloses ``query`` (an
    ``OuterRef``) is that query's to answer for, and a ``readable()`` value
    built for ``reader`` reads nothing that actor may not read.
    """
    own = gated_read_fields(model)
    outer = frozenset(query.external_aliases) if query is not None else frozenset()
    found: dict[type[models.Model], set[str]] = {}
    for expression in expressions:
        for column in _expression_columns(expression, outer, reader=reader):
            name = column.target.name
            if name in own and _column_on_model_lineage(column, model):
                found.setdefault(model, set()).add(name)
            elif name in gated_read_fields(column.target.model):
                found.setdefault(column.target.model, set()).add(name)
    return found


# The name Django's many-to-many prefetch selects its join key under, followed
# by the column's attname (``ManyRelatedManager.get_prefetch_querysets``).
_PREFETCH_KEY = "_prefetch_related_val_"


def _hand_written_selection(query: Query, using: str) -> bool:
    """Whether the query selects SQL through ``extra()`` that the guard cannot read.

    One use of ``extra(select=...)`` is Django's own: a many-to-many prefetch
    selects the column of the through table that names the row the related
    objects belong to.  It is recognised by what it is, not by its name
    alone: a foreign key column of a model whose table this very statement
    joins, selected as Django writes it and under the name of that column.
    Such an entry reads a key, like any join the ORM builds; a gated column
    of the through model is not one.
    """
    quote = connections[using].ops.quote_name
    # The foreign key columns of the models this statement joins, as Django
    # would select them, by the name Django would select them under.
    keys: dict[str, tuple[str, type[models.Model], str]] = {}
    for join in query.alias_map.values():
        relation: Any = getattr(join, "join_field", None)
        if relation is None or join.table_alias != join.table_name:
            continue
        for model in (relation.related_model, relation.model):
            if not isinstance(model, type) or not issubclass(model, models.Model):
                continue
            if model._meta.db_table != join.table_name:
                continue
            for field in model._meta.concrete_fields:
                if field.remote_field is not None and field.column is not None:
                    keys[f"{_PREFETCH_KEY}{field.attname}"] = (
                        f"{quote(join.table_name)}.{quote(field.column)}",
                        model,
                        field.name,
                    )
    for name, (sql, params) in query.extra_select.items():
        key = keys.get(name)
        if key is None or key[0] != sql or params or key[2] in gated_read_fields(key[1]):
            return True
    return False


# ``Query.deferred_loading`` of a query that loads every field of its model.
_EVERY_FIELD: tuple[frozenset[str], bool] = (frozenset(), True)


def _column_on_model_lineage(column: Col, model: type[models.Model]) -> bool:
    """A parent-table column still belongs to an MTI child's gated fields."""
    return any(column.target is field for field in model._meta.concrete_fields)


def _has_opaque_write_expression(expression: Any) -> bool:
    """Raw SQL can read columns invisible to ORM expression inspection.

    ``RawSQL``, a ``Func`` with a caller-supplied template, and a query that
    carries ``extra()`` SQL all splice literal SQL into the statement, so the
    columns they read cannot be enumerated by :func:`_expression_columns`.
    """
    if isinstance(expression, (RawSQL, ExtraWhere)):
        return True
    if isinstance(expression, Query):
        if expression.extra or expression.extra_tables or expression.extra_order_by:
            return True
        return any(
            _has_opaque_write_expression(part)
            for part in (
                expression.where,
                *expression.annotations.values(),
                *expression.order_by,
                *expression.select,
                *expression.combined_queries,
            )
        )
    # Func, Case and Subquery all accept a caller-supplied template or joiner
    # through ``**extra``; a function name that is not an identifier is SQL too.
    extra = getattr(expression, "extra", None)
    if isinstance(extra, dict) and {"template", "arg_joiner"} & extra.keys():
        return True
    for attribute in ("template", "arg_joiner"):
        # Subquery and friends store a caller-supplied template on the instance.
        own = getattr(expression, "__dict__", {}).get(attribute)
        if own is not None and own != getattr(type(expression), attribute, None):
            return True
    if isinstance(expression, models.Func):
        function = (
            extra.get("function") if isinstance(extra, dict) and "function" in extra else None
        )
        if function is None:
            function = getattr(expression, "function", None)
        if function is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", str(function)):
            return True
    sources = getattr(expression, "get_source_expressions", None)
    return callable(sources) and any(
        _has_opaque_write_expression(source) for source in cast(Iterable[Any], sources())
    )


def _has_gated_subquery(expression: Any) -> bool:
    """Refuse nested SQL reads whose gated columns cannot be checked per row."""
    if isinstance(expression, Query):
        if expression.model is not None and gated_read_fields(expression.model):
            return True
        if any(
            isinstance(related, type) and gated_read_fields(related)
            for join in expression.alias_map.values()
            if (related := getattr(getattr(join, "join_field", None), "related_model", None))
            is not None
        ):
            return True
        return any(
            _has_gated_subquery(part)
            for part in (
                *expression.annotations.values(),
                expression.where,
                *expression.combined_queries,
            )
        )
    sources = getattr(expression, "get_source_expressions", None)
    return bool(
        callable(sources)
        and any(_has_gated_subquery(part) for part in cast(Iterable[Any], sources()))
    )


def _column_relation_path(query: Query, column: Col) -> str:
    names: list[str] = []
    alias = column.alias
    while alias != query.base_table:
        join = query.alias_map.get(alias)
        field = getattr(join, "join_field", None)
        parent = getattr(join, "parent_alias", None)
        if field is None or parent is None:
            return ""
        names.append(field.name)
        alias = parent
    return "__".join(reversed(names))


class _WriteStep(Enum):
    UPDATE = auto()
    UPDATE_CLAUSE = auto()
    DELETE = auto()
    DELETE_CLAUSE = auto()
    CREATE = auto()


def _refuse_moving_edges(
    queryset: TrackedQuerySet[Any], kwargs: dict[str, Any], *, actor_scoped: bool
) -> None:
    """Reject a generic edge move at the point chosen by the write owner."""
    from .actors import current_actor, is_sudo
    from .watch import gate_policy, refuse_moving_edges

    related_owner = queryset._rebac_related_owner
    if actor_scoped or (
        not is_sudo() and (related_owner is not None or current_actor() is not None)
    ):
        refuse_moving_edges(gate_policy(queryset.db), queryset.model, kwargs)


class TrackedQuerySet[T: models.Model](models.QuerySet[T]):
    """Own model writes without applying an actor scope to reads."""

    _actor_scoped_writes: ClassVar[bool] = False

    _rebac_related_owner: models.Model | None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rebac_related_owner = None

    def __getstate__(self) -> dict[str, Any]:
        state = super().__getstate__()
        state["_rebac_related_owner"] = None
        return state

    def _clone(self, **kwargs: Any) -> Self:
        clone: Self = cast(Any, super())._clone(**kwargs)
        clone._rebac_related_owner = self._rebac_related_owner
        return clone

    def _write_scope(self, step: _WriteStep) -> tuple[SubjectRef | None, bool]:
        from .actors import is_sudo, resolve
        from .mixins import RebacMixin
        from .signals import _edge_actor

        if step is _WriteStep.DELETE:
            actor, ambient_bypass = resolve(
                mode="ambient", strict=False, subject_label="Tracked delete"
            )
            return actor, issubclass(self.model, RebacMixin) or ambient_bypass
        if step is _WriteStep.UPDATE:
            related_owner = self._rebac_related_owner
            if related_owner is not None:
                return _edge_actor(related_owner, relation=True)
        return None, is_sudo()

    def _write_rows(self, actor: SubjectRef | None, step: _WriteStep) -> TrackedQuerySet[T]:
        return self

    def _guard_rows(
        self,
        step: _WriteStep,
        actor: SubjectRef | None,
        bypass: bool,
        *,
        rows: TrackedQuerySet[T] | None = None,
        candidates: list[T] | None = None,
        kwargs: dict[str, Any] | None = None,
        watched: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
    ) -> None:
        """Plain tracked rows need only the signal-free create gate."""
        from .actors import is_sudo
        from .mixins import RebacMixin
        from .signals import _gate_backed_rows
        from .watch import gate_policy

        if step is not _WriteStep.CREATE or issubclass(self.model, RebacMixin):
            return
        if candidates is None:
            raise TypeError("bulk_create requires candidate rows")
        policy = gate_policy(self.db) if watched else None
        if update_conflicts and update_fields and not is_sudo() and policy is not None:
            watch = policy.watched.get(self.model._meta.label_lower)
            if watch is not None and any(
                {field.name, field.attname} & set(update_fields) & watch.fields
                for field in self.model._meta.concrete_fields
            ):
                raise PermissionDenied(
                    "Conflicting bulk create cannot check both sides of a backed edge; "
                    "use checked saves or sudo."
                )
        _gate_backed_rows(candidates, using=self.db, bypass=bypass)

    def update(self, **kwargs: Any) -> int:
        from .actors import is_sudo
        from .mixins import RebacMixin
        from .signals import _gate_backed_rows, audit_denials
        from .watch import by_key, model_write, statement_keys

        if self.query.combinator or self.query.is_sliced:
            return models.QuerySet.update(self, **kwargs)
        self._for_write = True
        with (
            audit_denials(using=self.db),
            model_write(model=self.model, using=self.db, names=kwargs) as watched,
        ):
            related_owner = self._rebac_related_owner
            if self._actor_scoped_writes:
                actor, bypass = self._write_scope(_WriteStep.UPDATE)
                if bypass:
                    return models.QuerySet.update(self, **kwargs)
                self._guard_rows(_WriteStep.UPDATE_CLAUSE, actor, bypass)
                backed = watched
            else:
                actor, bypass = None, is_sudo()
                # Tracked roots have no resource gate; a generic edge move
                # must be refused before resolving a related owner's actor.
                _refuse_moving_edges(self, kwargs, actor_scoped=False)
                related_fk_write = any(
                    isinstance(field, (models.ForeignKey, models.OneToOneField))
                    and {field.name, field.attname} & kwargs.keys()
                    for field in self.model._meta.concrete_fields
                )
                backed = watched and (
                    not issubclass(self.model, RebacMixin)
                    or related_owner is not None
                    or related_fk_write
                )
                if not backed or bypass:
                    return models.QuerySet.update(self, **kwargs)
                actor, bypass = self._write_scope(_WriteStep.UPDATE)
                if bypass:
                    return models.QuerySet.update(self, **kwargs)
            rows = self._write_rows(actor, _WriteStep.UPDATE)
            keys = statement_keys(rows)
            # QuerySet.update() sends no model signals, so this internal capture
            # cannot be observed as a signal origin.
            parts = list(by_key(rows, keys))
            if backed:
                for stored in by_key(self.model._base_manager.using(self.db), keys):
                    _gate_backed_rows(
                        stored,
                        using=self.db,
                        names=kwargs,
                        proposed=kwargs,
                        actor=actor,
                        bypass=bypass,
                    )
            if self._actor_scoped_writes:
                # A backed denial takes precedence on actor-scoped writes.
                _refuse_moving_edges(self, kwargs, actor_scoped=True)
            for part in parts:
                self._guard_rows(_WriteStep.UPDATE, actor, bypass, rows=part, kwargs=kwargs)
            written = sum(models.QuerySet.update(part, **kwargs) for part in parts)
            self._result_cache = None
            return written

    def delete(self) -> tuple[int, dict[str, int]]:
        from .mixins import delete_collected, deletion_owner
        from .signals import _gate_backed_rows, audit_denials
        from .watch import by_key, model_write, statement_keys

        if (
            self.query.combinator
            or self.query.is_sliced
            or self.query.distinct_fields
            or getattr(self, "_fields", None) is not None
        ):
            return models.QuerySet.delete(self)
        self._for_write = True
        with audit_denials(using=self.db), model_write(model=self.model, using=self.db) as watched:
            actor, bypass = self._write_scope(_WriteStep.DELETE)
            self._guard_rows(_WriteStep.DELETE_CLAUSE, actor, bypass)
            if bypass or (not watched and not self._actor_scoped_writes):
                with deletion_owner(self, self.db, actor, bypass):
                    result = delete_collected(self, origin=self, using=self.db)
                self._result_cache = None
                return result
            rows = self._write_rows(actor, _WriteStep.DELETE)
            keys = statement_keys(rows)
            # Django's collector forwards each part as pre_delete's origin.
            # Preserve the caller's authority on that queryset.
            parts = list(by_key(self, keys))
            if watched and not self._actor_scoped_writes:
                for stored in by_key(self.model._base_manager.using(self.db), keys):
                    _gate_backed_rows(stored, using=self.db, deleting=True)
            for part in parts:
                self._guard_rows(_WriteStep.DELETE, actor, bypass, rows=part)
            deleted: Counter[str] = Counter()
            for part in parts:
                with deletion_owner(part, self.db, actor, bypass):
                    deleted.update(delete_collected(part, origin=part, using=self.db)[1])
            self._result_cache = None
            return sum(deleted.values()), dict(deleted)

    def bulk_create(
        self,
        objs: Iterable[T],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[T]:
        from .actors import is_sudo
        from .signals import audit_denials
        from .watch import model_write

        self._for_write = True
        candidates = list(objs)
        with audit_denials(using=self.db), model_write(model=self.model, using=self.db) as watched:
            actor, bypass = (
                self._write_scope(_WriteStep.CREATE)
                if self._actor_scoped_writes
                else (None, is_sudo())
            )
            self._guard_rows(
                _WriteStep.CREATE,
                actor,
                bypass,
                candidates=candidates,
                watched=watched,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
            )
            result = models.QuerySet.bulk_create(
                self,
                candidates,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
                unique_fields=unique_fields,
            )
            if self._actor_scoped_writes and actor is not None and not bypass:
                for row in result:
                    row._rebac_actor = actor  # type: ignore[attr-defined]
                    row._rebac_sudo_reason = None  # type: ignore[attr-defined]
            return result

    def bulk_update(
        self, objs: Iterable[T], fields: Iterable[str], batch_size: int | None = None
    ) -> int:
        from .signals import audit_denials
        from .watch import model_write

        self._for_write = True
        field_names = tuple(fields)
        with (
            audit_denials(using=self.db),
            model_write(model=self.model, using=self.db, names=field_names),
        ):
            return models.QuerySet.bulk_update(self, objs, field_names, batch_size=batch_size)


_TrackedManagerBase = models.Manager.from_queryset(TrackedQuerySet)


def _related_manager_owner(manager: object) -> RebacMixin | None:
    """Find a REBAC source on a real related manager, never in router hints."""
    from .mixins import RebacMixin

    owner = getattr(manager, "instance", None)
    return owner if isinstance(owner, RebacMixin) and hasattr(manager, "core_filters") else None


# Keep the same model parameter on Django's generated, generic manager base.
class TrackedManager[M: models.Model](_TrackedManagerBase[M]):  # pyright: ignore[reportInvalidTypeArguments]
    """Unfiltered manager whose writes to columns the policy reads are gated."""

    def get_queryset(self) -> TrackedQuerySet[M]:
        queryset = cast(TrackedQuerySet[M], super().get_queryset())
        queryset._rebac_related_owner = _related_manager_owner(self)
        return queryset


class RebacQuerySet(TrackedQuerySet[_M]):
    """REBAC-aware queryset.

    Use `.with_actor(actor)`, `.as_user(user)`, `.as_agent(agent, on_behalf_of=u)`,
    or `.sudo(reason=...)` to scope. Materialising without any of those AND
    without an ambient actor raises `MissingActorError` when STRICT_MODE is on.

    Generic over the model so the actor verbs preserve the concrete row
    type: ``Post.objects.filter(...).as_user(u).get()`` stays ``Post``.
    """

    # Per-queryset state. Carried through `_clone()`.
    _rebac_actor: SubjectRef | None
    _rebac_relation_actor: SubjectRef | None
    _rebac_relation_scope: bool
    _rebac_action: str | None
    _rebac_sudo_reason: str | None
    _rebac_field_deny: FieldDenyMode | None
    _rebac_select_related_guards: tuple[str, ...]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if type(self._query) is Query:
            self._query.__class__ = _RebacQuery
        self._rebac_actor = None
        self._rebac_relation_actor = None
        self._rebac_relation_scope = False
        self._rebac_action = None
        self._rebac_sudo_reason = None
        self._rebac_sudo_audited = False
        self._rebac_internal_bypass = False
        self._rebac_ambient_bypass = False
        self._rebac_field_deny = None
        self._rebac_select_related_guards = ()
        self._rebac_eager_scope = False
        self._rebac_aggregate_scope = False
        self._rebac_visibility_applied = False
        self._rebac_scope_generation: tuple[int, int] | None = None

    def __getstate__(self) -> dict[str, Any]:
        """Do not transport actor or sudo authority through a pickle."""
        import django

        # Django's implementation calls _fetch_all(), which could run an
        # authorization-sensitive query merely to pickle an inert query.
        state = {**self.__dict__, "_django_version": django.__version__}
        state["_rebac_actor"] = None
        state["_rebac_relation_actor"] = None
        state["_rebac_relation_scope"] = False
        state["_rebac_related_owner"] = None
        state["_rebac_sudo_reason"] = None
        state["_rebac_sudo_audited"] = False
        state["_rebac_internal_bypass"] = False
        state["_rebac_ambient_bypass"] = False
        state["_rebac_scope_applied"] = False
        state["_rebac_scope_generation"] = None
        state["_rebac_scope_decision"] = None
        state["_rebac_eager_scope"] = False
        state["_rebac_visibility_applied"] = False
        state["_prefetch_done"] = False
        state["_result_cache"] = None
        query = state.get("_query")
        if isinstance(query, _RebacQuery):
            query = cast(_RebacQuery, _without_query_scope(query))
            query._rebac_state = {
                name: value
                for name, value in getattr(query, "_rebac_state", {}).items()
                if name
                not in {
                    "_rebac_actor",
                    "_rebac_relation_actor",
                    "_rebac_relation_scope",
                    "_rebac_related_owner",
                    "_rebac_sudo_reason",
                }
            }
            state["_query"] = query
        return state

    def raw(
        self, raw_query: Any, params: Any = (), translations: Any = None, using: Any = None
    ) -> Any:
        _, bypass = self.effective_actor(strict=True)
        self._audit_bypass_once()
        if not bypass:
            raise PermissionDenied("raw() cannot be actor-scoped; use sudo or system_context")
        raw_method = super().raw
        return raw_method(raw_query, params=params, translations=translations, using=using)

    @property
    def query(self) -> Query:
        query = super().query
        if isinstance(query, _RebacQuery):
            query._rebac_state = {
                name: value
                for name, value in self.__dict__.items()
                if name.startswith("_rebac_") or name in ("_db", "_hints", "_for_write")
            }
        return query

    @query.setter
    def query(self, value: Query) -> None:  # pyright: ignore[reportIncompatibleVariableOverride]
        # Match Django's public setter without forcing authorization on query
        # construction, filters, or unused relation declarations.
        from django.db.models.query import ValuesIterable

        if value.values_select:
            self._iterable_class = ValuesIterable
        self._query = value

    @schema_operation
    def resolve_expression(self, *args: Any, **kwargs: Any) -> Query:
        clone = self._clone()
        query = clone.query
        if isinstance(query, _RebacQuery) and query._rebac_boolean_operand:
            # Django's bounded pk subquery for a sliced Boolean operand: the
            # combination scopes and checks it (_scope_query) under the left
            # operand's actor. Its own actor, bypass and audit do not apply,
            # and an operand without an actor is not an error here.
            return cast(Query, super(RebacQuerySet, clone).resolve_expression(*args, **kwargs))
        # Resolution builds an expression; the bypass is exercised, and
        # audited, when the statement it lands in is compiled for execution.
        # The query's own resolution checks what it selects, filters and
        # orders on.
        clone._apply_scope_in_place(audit=False)
        pending = clone._bypass_audit_reason()
        resolved = cast(Query, super(RebacQuerySet, clone).resolve_expression(*args, **kwargs))
        if pending is not None and isinstance(resolved, _RebacQuery):
            resolved._rebac_pending_bypass_audit = pending
        return resolved

    def explain(self, *, format: str | None = None, **options: Any) -> str:
        scoped = self._clone()
        scoped._apply_scope_in_place()
        scoped._audit_bypass_once()
        scoped._guard_clause_field_reads(*scoped.effective_actor(strict=True))
        return super(RebacQuerySet, scoped).explain(format=format, **options)

    # Note: a second `_clone` override below combines the actor + scope-flag
    # propagation; this stub kept for readability.

    # ----- Actor verbs -----

    def with_actor(self, actor: Any) -> Self:
        """Pin a SubjectRef on the queryset. Generic verb — accepts any ActorLike."""
        ref = actor if isinstance(actor, SubjectRef) else to_subject_ref(actor)
        clone = self._clone()
        clone._rebac_actor = ref
        clone._rebac_sudo_reason = None
        clone._rebac_sudo_audited = False
        clone._rebac_internal_bypass = False
        clone._rebac_ambient_bypass = False
        from .relation_loading import rebind_prefetch_actor

        prefetches = cast(Any, clone)._prefetch_related_lookups
        cast(Any, clone)._prefetch_related_lookups = rebind_prefetch_actor(prefetches, ref)
        clone._refresh_scope()
        return clone

    def as_user(self, user: Any) -> Self:
        """Typed shorthand: scope to a Django User."""
        return self.with_actor(to_subject_ref(user))

    def as_agent(self, agent: Any, *, on_behalf_of: Any | None = None) -> Self:
        """Typed shorthand: scope to an agent acting via a Grant."""
        return self.with_actor(grant_subject_ref(agent, on_behalf_of))

    def with_action(self, action: str) -> Self:
        """Pin the REBAC permission used for queryset read scoping."""
        if not action:
            raise ValueError("with_action() requires a non-empty action")
        clone = self._clone()
        clone._rebac_action = action
        clone._refresh_scope()
        return clone

    def on_field_deny(self, mode: FieldDenyMode) -> Self:
        """Override ``REBAC_FIELD_READ_MODE`` for this queryset."""
        if mode == "raise":
            warn_raise_mode_degrades(stacklevel=2)
        clone = self._clone()
        clone._rebac_field_deny = validate_field_deny_mode(mode)
        return clone

    def for_write(self) -> Self:
        """Return a write-target queryset: REBAC row scope kept, field-read
        redaction off.

        Resolving an update/delete target must load every column of the row to
        mutate it, so field-read redaction must not hide columns from the loaded
        instance -- yet the row must still be one the actor may access (row scope
        is applied on materialization as usual). ``for_write()`` is
        ``on_field_deny("allow")`` named for that intent.
        """
        return self.on_field_deny("allow")

    def readable(self, field: str) -> models.Expression:
        """A gated field as this queryset's actor may read it.

        The column on rows where the actor holds ``read__<field>`` and ``NULL``
        elsewhere: the value redaction shows (a conditional permission counts
        as not held).  The field read guards accept it for this actor in a
        filter, an ordering, a grouping or a selection of a query of this
        model.  Under a bypass, or for a field the model's type does not gate,
        it is the column itself.
        """
        model_field = self.model._meta.get_field(field)
        if (
            not getattr(model_field, "concrete", False)
            or model_field.is_relation
            or model_field.name != field
        ):
            raise ValueError(
                f"readable() names a concrete field of {self.model.__name__} that is not "
                f"a relation: {field!r}"
            )
        output_field = cast("models.Field[Any, Any]", model_field).clone()
        output_field.null = True
        actor, bypass = self.effective_actor(strict=True)
        if bypass or actor is None or field not in self._gated_read_fields():
            return models.ExpressionWrapper(models.F(field), output_field=output_field)
        readers = (
            type(self)(model=self.model, using=self.db)
            .with_actor(actor)
            .with_action(f"read__{field}")
            .scoped_for_aggregate()
        )
        mask = models.Exists(readers.filter(pk=models.OuterRef("pk")))
        readable = _Readable(
            models.When(mask, then=models.F(field)),
            default=models.Value(None),
            output_field=output_field,
        )
        readable.rebac_actor = actor
        readable.rebac_field = field
        readable.rebac_model = self.model
        readable.rebac_readers = mask
        return readable

    def scoped(self) -> Self:
        """Return an eagerly scoped clone, including for SQL subquery consumers.

        The resolved actor is pinned to the clone. Caller predicates, database,
        annotations and ordering remain intact; this adds no relationship joins.
        """
        clone = self._clone()
        actor, bypass = clone.effective_actor(strict=True)
        if actor is not None:
            clone._rebac_actor = actor
        elif bypass and _is_sudo_ambient() and not clone.is_sudo():
            clone._rebac_sudo_reason = current_sudo_reason()
            clone._rebac_internal_bypass = True
            clone._rebac_ambient_bypass = True
        clone._rebac_eager_scope = True
        clone._apply_scope_in_place(prepare=False)
        return clone

    def scoped_for_aggregate(self) -> Self:
        """Eager row scope for projection, fail-closed without an actor.

        Instance field redaction cannot operate on aggregate/dict rows. The
        caller must validate allowed projection axes separately. SQL cardinality
        of caller-authored joins is preserved; permission scoping adds no joins.
        """
        clone = self.on_field_deny("allow")
        clone._rebac_aggregate_scope = True
        clone._reset_scope()
        return clone.scoped()

    def _reset_scope(self) -> None:
        # QuerySet.query's public setter switches values_list() to ValuesIterable
        # when values_select is populated. This is an internal SQL replacement;
        # preserve the caller's scalar/tuple/model iterable contract.
        self._query = _without_query_scope(self.query)
        self._rebac_scope_applied = False
        self._rebac_scope_decision = None
        self._rebac_scope_generation = None

    def _refresh_scope(self) -> None:
        self._reset_scope()
        if self._rebac_eager_scope:
            self._apply_scope_in_place()

    def sudo(self, *, reason: str) -> Self:
        """Bypass REBAC for this queryset. Mandatory `reason`."""
        return self._bypass(reason=reason, allow_when_sudo_disabled=False, internal=False)

    def system_context(self, *, reason: str) -> Self:
        return self._bypass(reason=reason, allow_when_sudo_disabled=True, internal=False)

    def _system_capture(self, *, reason: str) -> Self:
        return self._bypass(reason=reason, allow_when_sudo_disabled=True, internal=True)

    def _bypass(self, *, reason: str, allow_when_sudo_disabled: bool, internal: bool) -> Self:
        require_sudo_allowed(reason, allow_when_disabled=allow_when_sudo_disabled)
        clone = self._clone()
        clone._rebac_actor = None
        clone._rebac_sudo_reason = reason
        clone._rebac_sudo_audited = False
        clone._rebac_internal_bypass = internal
        clone._rebac_ambient_bypass = False
        clone._refresh_scope()
        return clone

    def _bypass_audit_reason(self) -> str | None:
        """The reason to audit for this queryset's bypass, or ``None``."""
        if (
            self._rebac_sudo_reason is None
            or (self._rebac_internal_bypass and not self._rebac_ambient_bypass)
            or (self._rebac_ambient_bypass and _is_sudo_ambient())
        ):
            return None
        return self._rebac_sudo_reason

    def _audit_bypass_once(self) -> None:
        with database_context(self.db):
            if self._rebac_sudo_audited:
                return
            reason = self._bypass_audit_reason()
            if reason is None:
                return
            from .audit import emit_bypass

            emit_bypass(reason, actor=_current_actor())
            self._rebac_sudo_audited = True

    def actor(self) -> SubjectRef | None:
        return self._rebac_actor

    def is_sudo(self) -> bool:
        return self._rebac_sudo_reason is not None

    # ----- Relation loading -----

    def rebac_select_related(self, *fields: Any) -> Self:
        """``select_related`` plus a post-fetch guard for REBAC-bound targets."""
        clone = self.select_related(*fields)
        if fields == (None,):
            clone._rebac_select_related_guards = ()
            return clone
        from .relation_loading import selected_related_paths

        paths = (
            tuple(str(field) for field in fields)
            if fields
            else selected_related_paths(
                self.model,
                clone.query.select_related,
            )
        )
        clone._rebac_select_related_guards = tuple(
            dict.fromkeys((*self._rebac_select_related_guards, *paths))
        )
        return clone

    def rebac_prefetch_related(self, *lookups: Any) -> Self:
        """``prefetch_related`` with actor-scoped querysets for protected targets."""
        if lookups == (None,):
            return self.prefetch_related(None)
        from .relation_loading import scope_prefetch_lookups

        scoped = scope_prefetch_lookups(
            self.model,
            lookups,
            actor=self._rebac_actor,
            mode=self._rebac_field_deny,
        )
        return self.prefetch_related(*scoped)

    # ----- Materialisation -----

    def effective_actor(self, *, strict: bool = False) -> tuple[SubjectRef | None, bool]:
        """Return ``(actor_ref, is_unscoped)`` for this queryset.

        A related manager transfers its source instance's actor pin to a fresh
        queryset. Django's shared database-router hints carry no authority.
        The instance's sudo pin does not propagate. A queryset's own actor or sudo
        pin remains authoritative. ``scoped_for_aggregate()`` fails closed
        without an actor in either strict mode. In observer mode (``strict=False``),
        a strict-mode no-actor state returns ``(None, False)`` instead of raising.

        Resolution order: queryset sudo → queryset actor → related instance
        actor → ambient sudo → ambient actor → mode-specific fallback.
        """
        own_sudo = self.is_sudo()
        pinned_actor = self._rebac_actor
        if pinned_actor is None:
            pinned_actor = self._rebac_relation_actor
        return resolve(
            pinned_actor=pinned_actor,
            pinned_sudo=own_sudo,
            mode="relation" if self._rebac_relation_scope and not own_sudo else "row",
            strict=strict,
            subject_label=f"Queryset on {self.model.__name__}",
            fail_closed=self._rebac_aggregate_scope,
        )

    _rebac_scope_applied: bool = False
    # ``(actor, bypass)`` as the scope was applied.
    _rebac_scope_decision: tuple[SubjectRef | None, bool] | None = None

    def _apply_scope_in_place(self, *, audit: bool = True, prepare: bool = True) -> None:
        """Add the compiled permission predicate to this query's WHERE clause.

        The predicate checks each selected row on the queryset's database
        alias. Adding it to the underlying Query also works after ``.get()``
        slices the queryset, when ``.filter()`` would be rejected. Prepared
        decisions are invalidated when tracked relationships or backing rows
        change.
        """
        from .backends import local
        from .watch import row_generation

        generation = (local._relationship_generation, row_generation())
        if self._rebac_scope_applied:
            if self._rebac_scope_generation == generation:
                if prepare:
                    self._prepare_applied_scope()
                return
            # A tracked write can invalidate a prepared lower bound. Rebuild
            # before compilation; raw SQL writes are still checked by witnesses.
            self._reset_scope()
        actor, sudo = self.effective_actor(strict=True)
        if audit:
            self._audit_bypass_once()
        self._rebac_scope_applied = True
        self._rebac_scope_decision = (actor, sudo)
        self._rebac_scope_generation = generation
        if sudo:
            return
        self._scope_query(
            self.query, actor, prepare=prepare and not bool(self.query.combined_queries)
        )
        if self.query.combined_queries:
            from .compile.read import prepare_resolved_filter, share_combined_scopes

            if share_combined_scopes(self.query) is not True:
                if prepare:
                    for part in self.query.combined_queries:
                        prepare_resolved_filter(part.where, part.where, self.db)

    def _prepare_applied_scope(self) -> None:
        from .compile.read import prepare_resolved_filter

        if getattr(self.query, "_rebac_subject_plan", None) is not None:
            return
        parts = self.query.combined_queries or (self.query,)
        for part in parts:
            prepare_resolved_filter(part.where, part.where, self.db)

    @schema_operation
    def _scope_query(
        self,
        query: Query,
        actor: SubjectRef | None,
        *,
        prepare: bool = True,
        fallback: bool = False,
    ) -> None:
        with database_context(self.db):
            for operand in _nested_queries(query.where, boolean_operands_only=True):
                self._scope_query(operand, actor, prepare=prepare, fallback=fallback)
                if isinstance(operand, _RebacQuery):
                    operand._rebac_check = (self, actor)
                    operand._rebac_checked = False
            if query.combined_queries:
                for part in query.combined_queries:
                    self._scope_query(part, actor, prepare=False, fallback=True)
                return
            model = query.model
            if model is None:
                return
            rebac_type = model_resource_type(model)
            if not rebac_type:
                return
            if actor is None:
                # Aggregate projection is fail-closed regardless of strict mode.
                query.where = WhereNode([query.where, _ScopeWhere(children=[NothingNode()])])
                return
            from .backends import backend

            action = str(self._rebac_action or getattr(model._meta, "rebac_default_action", "read"))
            active_backend = backend()
            predicate = active_backend.queryset_filter(
                model=model, subject=actor, action=action, using=self.db
            )
            # `_apply_scope_in_place`, including its embedded-queryset
            # path in `_RebacQuery.resolve_expression`, owns preparation
            # on this resolved alias. The expression keeps the result.
            from .compile.read import prepare_resolved_filter

            assert predicate is not None
            restriction = query.build_where(predicate)
            prepare_resolved_filter(
                restriction, query.where, self.db, prepare=prepare, fallback=fallback
            )
            query.where = WhereNode([query.where, _ScopeWhere(children=[restriction])])

    def _clone(self, **kwargs: Any) -> Self:
        # Django's private clone hook; not in django-stubs.
        clone: Self = cast(Any, super())._clone(**kwargs)
        clone._rebac_actor = self._rebac_actor
        clone._rebac_relation_actor = self._rebac_relation_actor
        clone._rebac_relation_scope = self._rebac_relation_scope
        clone._rebac_action = self._rebac_action
        clone._rebac_sudo_reason = self._rebac_sudo_reason
        clone._rebac_sudo_audited = self._rebac_sudo_audited
        clone._rebac_internal_bypass = self._rebac_internal_bypass
        clone._rebac_ambient_bypass = self._rebac_ambient_bypass
        clone._rebac_field_deny = self._rebac_field_deny
        clone._rebac_select_related_guards = self._rebac_select_related_guards
        clone._rebac_eager_scope = self._rebac_eager_scope
        clone._rebac_aggregate_scope = self._rebac_aggregate_scope
        clone._rebac_scope_applied = self._rebac_scope_applied
        clone._rebac_scope_decision = self._rebac_scope_decision
        clone._rebac_scope_generation = self._rebac_scope_generation
        if not self._rebac_eager_scope:
            clone._reset_scope()
        return clone

    def _merge_sanity_check(self, other: models.QuerySet[_M]) -> None:
        if not isinstance(other, RebacQuerySet):
            raise TypeError("Boolean REBAC combinations require REBAC querysets")
        super()._merge_sanity_check(other)  # type: ignore[misc]

    def _combined_scope(self, combined: models.QuerySet[_M]) -> RebacQuerySet[_M]:
        # Django's empty-query fast paths may return the right operand itself.
        # Preserve its result shape, but apply the left operand's actor policy
        # without mutating either original queryset.
        if isinstance(combined, RebacQuerySet):
            clone = combined._clone()
        else:
            # A sliced left operand makes Django combine through the unscoped
            # base manager. Keep its SQL and row shape on the configured
            # queryset, whose clone owns custom state and an empty result cache.
            clone = self._clone()
            clone._query = combined.query.chain(klass=_RebacQuery)
            clone._iterable_class = combined._iterable_class
            # Django's private projection and related-object state; not in django-stubs.
            cast(Any, clone)._fields = cast(Any, combined)._fields
            cast(Any, clone)._merge_known_related_objects(combined)
        query = clone.query
        if isinstance(query, _RebacQuery):
            query._rebac_boolean_operand = False
        clone._rebac_actor = self._rebac_actor
        clone._rebac_relation_actor = self._rebac_relation_actor
        clone._rebac_relation_scope = self._rebac_relation_scope
        clone._rebac_action = self._rebac_action
        clone._rebac_sudo_reason = self._rebac_sudo_reason
        clone._rebac_eager_scope = self._rebac_eager_scope
        clone._rebac_aggregate_scope = self._rebac_aggregate_scope
        clone._rebac_field_deny = self._rebac_field_deny
        clone._refresh_scope()
        return clone

    def _boolean_operand(self) -> Self:
        if self.query.can_filter():
            return self
        clone = self._clone()
        clone._reset_scope()
        query = clone.query
        if isinstance(query, _RebacQuery):
            query._rebac_boolean_operand = True
        return clone

    @overload
    def __and__(self, other: Self) -> Self: ...

    @overload
    def __and__(self, other: models.QuerySet[_M]) -> RebacQuerySet[_M]: ...

    def __and__(self, other: models.QuerySet[_M]) -> RebacQuerySet[_M]:
        return self._combined_scope(super().__and__(other))

    @overload
    def __or__(self, other: Self) -> Self: ...

    @overload
    def __or__(self, other: models.QuerySet[_M]) -> RebacQuerySet[_M]: ...

    def __or__(self, other: models.QuerySet[_M]) -> RebacQuerySet[_M]:
        left = self._boolean_operand()
        right = other._boolean_operand() if isinstance(other, RebacQuerySet) else other
        return self._combined_scope(super(RebacQuerySet, left).__or__(right))

    @overload
    def __xor__(self, other: Self) -> Self: ...

    @overload
    def __xor__(self, other: models.QuerySet[_M]) -> RebacQuerySet[_M]: ...

    def __xor__(self, other: models.QuerySet[_M]) -> RebacQuerySet[_M]:
        left = self._boolean_operand()
        right = other._boolean_operand() if isinstance(other, RebacQuerySet) else other
        return self._combined_scope(super(RebacQuerySet, left).__xor__(right))

    def _combinator_query(
        self, combinator: str, *other_qs: Any, all: bool = False
    ) -> RebacQuerySet[_M]:
        combined: RebacQuerySet[_M] = super()._combinator_query(combinator, *other_qs, all=all)  # type: ignore[misc]
        combined._refresh_scope()
        if combined._rebac_scope_applied and combined.query.combined_queries:
            from .compile.read import share_combined_scopes

            share_combined_scopes(combined.query)
        return combined

    def _gated_read_fields(self) -> frozenset[str]:
        with database_context(self.db):
            return gated_read_fields(self.model)

    def _effective_field_mode(self) -> FieldDenyMode:
        return effective_field_deny_mode(self._rebac_field_deny)

    @schema_operation
    def _guard_projected_field_reads(
        self,
        actor: SubjectRef | None,
        sudo: bool,
        *,
        expressions: dict[str, Any] | None = None,
        query: Query | None = None,
        clears_ordering: bool | None = None,
    ) -> None:
        with database_context(self.db):
            query = self.query if query is None else query
            self._guard_clause_field_reads(
                actor, sudo, query=query, clears_ordering=clears_ordering
            )
            selected = dict(query.annotation_select)
            if expressions:
                selected.update(expressions)
            # Root bypass never carries across the protected relation boundary.
            self._guard_projected_related_reads(selected, query=query)
            if actor is None or sudo:
                return
            if runtime_field_deny_mode(self._effective_field_mode()) == "allow":
                return
            for part in _selections(query):
                if part.model is None:
                    continue
                if part is not query:
                    computed = dict(part.annotation_select)
                    columns: Iterable[Any] = part.select
                    literal = _hand_written_selection(part, self.db)
                elif query.combinator:
                    # A combination runs its operands; of its own it adds only
                    # the values computed over their rows.
                    computed, columns, literal = dict(expressions or {}), (), False
                else:
                    computed, columns = selected, query.select
                    literal = _hand_written_selection(query, self.db)
                if literal or any(_has_opaque_write_expression(v) for v in computed.values()):
                    raise PermissionDenied(
                        f"Cannot select hand-written SQL on {part.model.__name__} under an actor: "
                        "it can read any column, so field read gates cannot be checked. "
                        "Read it under sudo(reason=...), or select the columns through "
                        "ORM expressions."
                    )
                if part is not query and query.selected is None and part.selected is None:
                    # The rows of an operand become instances of the first
                    # operand's model column by column, so a gated value can land
                    # in a field that is not redacted.  No layout is worked out
                    # here: the operands load every field of that one model.
                    plain = (
                        part.model._meta.concrete_model is self.model._meta.concrete_model
                        and part.deferred_loading == _EVERY_FIELD
                        and query.deferred_loading == _EVERY_FIELD
                    )
                    related = bool(part.select_related or query.select_related)
                    if (not plain or related) and (
                        related or gated_read_fields(self.model) or gated_read_fields(part.model)
                    ):
                        raise PermissionDenied(
                            f"Cannot combine {part.model.__name__} rows into "
                            f"{self.model.__name__} instances under field read enforcement: "
                            "the operands of a combination that returns model instances must "
                            "load every field of one model, without only(), defer() or "
                            "select_related(). Combine values() projections instead."
                        )
                for model, requested in sorted(
                    _selected_gated(
                        part.model, (*columns, *computed.values()), query, reader=actor
                    ).items(),
                    key=lambda item: item[0]._meta.label_lower,
                ):
                    hidden = _hidden_from(actor, model, requested)
                    if not hidden:
                        continue
                    names = ", ".join(f"read__{name}" for name in hidden)
                    raise PermissionDenied(
                        f"Cannot project gated field(s) {names} on {model.__name__}: "
                        "field read enforcement requires model-instance materialisation "
                        "or a projection that omits gated fields."
                    )

    @schema_operation
    def _guard_clause_field_reads(
        self,
        actor: SubjectRef | None,
        sudo: bool,
        *,
        query: Query | None = None,
        clears_ordering: bool | None = None,
    ) -> None:
        """Refuse a query that filters, groups, orders or deduplicates on a gated column.

        First, every actor-scoped subquery the statement holds is checked, in
        this check's schema operation, whatever this query's own actor.

        The database applies the clause to the real value before redaction
        runs on the rows, so the rows matched, their number and their order
        reveal it.  An actor whose permission on the field does not depend on
        the row reads it everywhere and is not refused.
        """
        with database_context(self.db):
            for nested in _nested_queries(self.query if query is None else query):
                _check_subquery(nested)
            if actor is None or sudo:
                return
            if runtime_field_deny_mode(self._effective_field_mode()) == "allow":
                return
            query = self.query if query is None else query
            if query.model is None:
                return
            expressions, literal = _clause_expressions(
                query, self.db, clears_ordering=clears_ordering
            )
            if literal:
                raise PermissionDenied(
                    f"Cannot filter or order by hand-written SQL on {query.model.__name__} under an "
                    "actor: it can read any column, so field read gates cannot be checked. Run it "
                    "under sudo(reason=...), or filter through ORM expressions."
                )
            for model, requested in sorted(
                _selected_gated(query.model, expressions, query, reader=actor).items(),
                key=lambda item: item[0]._meta.label_lower,
            ):
                hidden = _hidden_from(actor, model, requested)
                if hidden:
                    names = ", ".join(f"read__{name}" for name in hidden)
                    raise PermissionDenied(
                        f"Cannot filter or order by gated field(s) {names} on {model.__name__} under "
                        "an actor: the rows matched, their number and their order would reveal the "
                        "values. Filter on another column, or run it under sudo(reason=...)."
                    )

    def _guard_projected_related_reads(self, expressions: dict[str, Any], *, query: Query) -> None:
        with database_context(self.db):
            if not self._rebac_select_related_guards:
                return
            from .relation_loading import _relation_scope, protected_lookup_prefixes

            if _relation_scope(self)[1]:
                return

            paths = {
                path
                for guard in self._rebac_select_related_guards
                for path, _ in protected_lookup_prefixes(self.model, guard)
            }
            guarded = tuple(f"{path}__" for path in sorted(paths))
            raw_fields = getattr(self, "_fields", None)
            related = {
                field
                for field in raw_fields or ()
                if isinstance(field, str) and any(field.startswith(prefix) for prefix in guarded)
            }
            for name, expression in expressions.items():
                for column in _expression_columns(expression):
                    path = _column_relation_path(query, column)
                    if path in paths or any(path.startswith(prefix) for prefix in guarded):
                        related.add(name)
            if not related:
                return
            names = ", ".join(sorted(related)[:5])
            raise PermissionDenied(
                f"Cannot project related field(s) {names}: rebac_select_related() "
                "enforcement requires model-instance materialisation."
            )

    @schema_operation
    def _fetch_all(self) -> None:
        """Keep guards, SQL compilation and nested prefetches in one evaluation."""
        with database_context(self.db):
            native = cast(Any, self)
            fetching = self._result_cache is None
            if fetching:
                self._apply_scope_in_place()
            # Django prefetch caches reuse a population's already-audited read.
            actor, sudo = self.effective_actor(strict=True)
            if fetching:
                self._audit_bypass_once()
                # Rows already held were guarded by the evaluation that read them.
                self._guard_projected_field_reads(actor, sudo)
                self._result_cache = list(native._iterable_class(self))
            if self._result_cache is not None and not self._rebac_visibility_applied:
                if actor is not None and not sudo:
                    for inst in self._result_cache:
                        if isinstance(inst, models.Model):
                            inst._rebac_actor = actor  # type: ignore[attr-defined]
                            inst._rebac_field_deny = self._rebac_field_deny  # type: ignore[attr-defined]
            if native._prefetch_related_lookups and not native._prefetch_done:
                native._prefetch_related_objects()
            if self._result_cache is not None and not self._rebac_visibility_applied:
                if actor is not None and not sudo:
                    apply_field_visibility(
                        self._result_cache,
                        model=self.model,
                        actor=actor,
                        mode=self._effective_field_mode(),
                        using=self.db,
                    )
                self._guard_selected_related_reads()
                self._rebac_visibility_applied = True

    @schema_operation
    def iterator(self, *args: Any, **kwargs: Any) -> Any:
        with database_context(self.db):
            if self._result_cache is None:
                self._apply_scope_in_place()
            actor, sudo = self.effective_actor(strict=True)
            self._audit_bypass_once()
            self._guard_projected_field_reads(actor, sudo)
            rows = list(super().iterator(*args, **kwargs))
            if actor is not None and not sudo:
                for inst in rows:
                    if isinstance(inst, models.Model):
                        inst._rebac_actor = actor  # type: ignore[attr-defined]
                        inst._rebac_field_deny = self._rebac_field_deny  # type: ignore[attr-defined]
                apply_field_visibility(
                    rows,
                    model=self.model,
                    actor=actor,
                    mode=self._effective_field_mode(),
                    using=self.db,
                )
            self._guard_selected_related_reads(rows)
            return iter(rows)

    def _guard_selected_related_reads(self, rows: Iterable[Any] | None = None) -> None:
        with database_context(self.db):
            if not self._rebac_select_related_guards:
                return
            from .relation_loading import _relation_scope, guard_selected_related_instances

            if rows is None:
                rows = self._result_cache or ()
            actor, bypass = _relation_scope(self)
            guard_selected_related_instances(
                rows,
                root_model=self.model,
                paths=self._rebac_select_related_guards,
                actor=actor,
                bypass=bypass,
                mode=self._effective_field_mode(),
                using=self.db,
            )

    # ----- Async iteration: Django's aiterator bypasses the sync overrides -----

    async def aiterator(self, *args: Any, **kwargs: Any) -> AsyncIterator[_M]:
        """Async variant of :meth:`iterator` that preserves REBAC scoping.

        Django's ``QuerySet.aiterator`` builds the row iterable directly rather
        than routing through the (overridden) sync ``iterator`` / ``_fetch_all``
        path, so without this override an ``async for`` over ``aiterator()``
        would bypass actor scoping entirely — an unscoped read. We apply the
        same scope filter, actor stamping, field-visibility redaction, and
        ``rebac_select_related`` guards as the sync path, running the
        DB-touching steps off the event loop via ``sync_to_async`` (same
        posture as the rest of the async surface, which Django itself wraps in
        ``sync_to_async`` around our sync overrides).

        Every other async ORM method (``aget`` / ``acount`` / ``aexists`` /
        ``afirst`` / ``aupdate`` / ``adelete`` / ``acreate`` / ``__aiter__`` /
        ``ain_bulk`` / …) is a ``sync_to_async`` wrapper around the sync method
        we already override, so it inherits scoping for free — only
        ``aiterator`` and :meth:`aggregate` need explicit handling.
        """
        if self._result_cache is None:
            await sync_to_async(self._apply_scope_in_place)()
        actor, sudo = self.effective_actor(strict=True)
        self._audit_bypass_once()
        # DB-backed schemas refresh at operation boundaries. Field discovery
        # and projection guards therefore need the same worker thread as the
        # scope/visibility checks, even when the schema was loaded just above.
        await sync_to_async(self._guard_projected_field_reads)(actor, sudo)
        field_mode = self._effective_field_mode()
        redact = runtime_field_deny_mode(field_mode) != "allow" and bool(
            await sync_to_async(self._gated_read_fields)()
        )
        guard_related = bool(self._rebac_select_related_guards)
        async for inst in super().aiterator(*args, **kwargs):
            if actor is not None and not sudo and isinstance(inst, models.Model):
                inst._rebac_actor = actor  # type: ignore[attr-defined]
                inst._rebac_field_deny = self._rebac_field_deny  # type: ignore[attr-defined]
                if redact:
                    await sync_to_async(apply_field_visibility)(
                        [inst], model=self.model, actor=actor, mode=field_mode, using=self.db
                    )
            if guard_related:
                await sync_to_async(self._guard_selected_related_reads)([inst])
            yield inst

    # ----- Counts / existence respect scope too -----

    def count(self) -> int:
        if self._result_cache is not None:
            return len(self._result_cache)
        self._apply_scope_in_place()
        # Django keeps the ordering of a sliced query.
        self._audit_bypass_once()
        self._guard_counted_reads(*self.effective_actor(strict=True), clears_ordering=False)
        return super().count()

    def exists(self) -> bool:
        if self._result_cache is not None:
            return bool(self._result_cache)
        self._apply_scope_in_place()
        self._audit_bypass_once()
        self._guard_counted_reads(*self.effective_actor(strict=True), clears_ordering=True)
        return super().exists()

    def _guard_counted_reads(
        self, actor: SubjectRef | None, sudo: bool, *, clears_ordering: bool
    ) -> None:
        """A count or an existence test reads what the query filters on, and the
        selected values only when its rows are distinct, grouped or combined."""
        with database_context(self.db):
            query = self.query
            if query.distinct or query.group_by is not None or query.combinator:
                self._guard_projected_field_reads(actor, sudo, clears_ordering=clears_ordering)
            else:
                self._guard_clause_field_reads(actor, sudo, clears_ordering=clears_ordering)

    def aggregate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Aggregate over the actor's scope, not the whole table.

        Django computes ``aggregate`` against ``self.query`` directly, so it
        never triggers ``_fetch_all`` / ``iterator``. Without scoping the WHERE
        clause first, ``SomeModel.objects.as_user(u).aggregate(Count("pk"))``
        would summarise rows ``u`` cannot read (and ``aaggregate`` — a
        ``sync_to_async`` wrapper around this method — would leak the same way).
        Applying scope here fixes both surfaces.
        """
        self._apply_scope_in_place()
        actor, sudo = self.effective_actor(strict=True)
        self._audit_bypass_once()
        if self._rebac_select_related_guards or (
            actor is not None
            and not sudo
            and runtime_field_deny_mode(self._effective_field_mode()) != "allow"
        ):
            query = self.query.clone()
            expressions = {
                f"aggregate_{index}": expression.resolve_expression(query)
                for index, expression in enumerate((*args, *kwargs.values()))
                if callable(getattr(expression, "resolve_expression", None))
            }
            self._guard_projected_field_reads(
                actor, sudo, expressions=expressions, query=query, clears_ordering=False
            )
        return super().aggregate(*args, **kwargs)

    # ----- Write ops: enforce all-or-nothing -----

    def create(self, **kwargs: Any) -> _M:
        """Construct an instance and persist it through ``insert()``."""
        reverse_one_to_one_fields = frozenset(kwargs).intersection(
            self.model._meta._reverse_one_to_one_field_names  # type: ignore[attr-defined]
        )
        if reverse_one_to_one_fields:
            raise ValueError(
                "The following fields do not exist in this model: "
                + ", ".join(sorted(reverse_one_to_one_fields))
            )
        return self.insert(self.model(**kwargs))

    def insert(self, obj: _M) -> _M:
        """Persist a prepared, unsaved instance on this queryset's database.

        This is the seam for callers that prepare instances before persistence,
        such as GraphQL mutation resolvers and forms. Domain factories that
        must run for every insert override this queryset method, not ``create``
        on the manager, so kwargs and prepared-instance creation converge here.
        The save_base owner remains the sole create authorization gate.

        Queryset scope owns the write: any actor or sudo pinned on ``obj``
        itself is replaced by this queryset's actor and sudo reason. A prepared
        instance that carries its own scope saves through ``instance.save()``
        or ``Model.objects.with_actor(actor).insert(obj)``. This public verb is
        unrelated to Django's private bulk ``QuerySet._insert``.
        """
        if type(obj) is not self.model:
            raise TypeError(
                f"{self.model.__name__}.insert() accepts only exact "
                f"{self.model.__name__} instances; got {type(obj).__name__}."
            )
        if not obj._state.adding:
            raise ValueError(f"{self.model.__name__}.insert() requires an unsaved instance.")
        self._for_write = True
        if obj._state.db is not None and obj._state.db != self.db:
            raise ValueError(
                f"{self.model.__name__}.insert() cannot insert an instance bound to "
                f"database {obj._state.db!r} on database {self.db!r}."
            )
        from .mixins import insertion_scope

        actor, unscoped = self.effective_actor(strict=True)
        self._audit_bypass_once()
        obj._rebac_actor = actor  # type: ignore[attr-defined]
        obj._rebac_sudo_reason = None  # type: ignore[attr-defined]
        with insertion_scope(obj, actor, unscoped):
            obj.save(force_insert=True, using=self.db)
        return obj

    _actor_scoped_writes: ClassVar[bool] = True

    def _write_scope(self, step: _WriteStep) -> tuple[SubjectRef | None, bool]:
        actor, bypass = self.effective_actor(strict=True)
        self._audit_bypass_once()
        return actor, bypass

    def _write_rows(self, actor: SubjectRef | None, step: _WriteStep) -> RebacQuerySet[_M]:
        action = "delete" if step is _WriteStep.DELETE else "update"
        return self._rows_in_scope(self._writing_actor(actor), reason=f"rebac.{action}.rows")

    def _guard_rows(
        self,
        step: _WriteStep,
        actor: SubjectRef | None,
        bypass: bool,
        *,
        rows: TrackedQuerySet[_M] | None = None,
        candidates: list[_M] | None = None,
        kwargs: dict[str, Any] | None = None,
        watched: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
    ) -> None:
        from .preflight import _check_new_model
        from .signals import _gate_backed_rows

        if step is _WriteStep.UPDATE_CLAUSE:
            if self.query.distinct_fields:
                raise TypeError("Cannot call update() after .distinct(*fields) under an actor.")
            self._guard_clause_field_reads(actor, bypass, query=self._update_query())
        elif step is _WriteStep.DELETE_CLAUSE:
            self._guard_clause_field_reads(actor, bypass, clears_ordering=True)
        elif step is _WriteStep.UPDATE or step is _WriteStep.DELETE:
            if not isinstance(rows, RebacQuerySet):
                raise TypeError("An actor-scoped write requires a RebacQuerySet.")
            if actor is None or not model_resource_type(self.model):
                return
            rows._guard_bulk_action(actor, "write" if step is _WriteStep.UPDATE else "delete")
            if step is _WriteStep.UPDATE and kwargs is not None:
                rows._guard_bulk_field_writes(actor, kwargs)
                rows._guard_bulk_expression_reads(actor, kwargs)
        elif step is _WriteStep.CREATE:
            if candidates is None:
                raise TypeError("bulk_create requires candidate rows")
            for candidate in candidates:
                if type(candidate) is not self.model:
                    raise TypeError(
                        f"{self.model.__name__}.bulk_create() accepts only exact "
                        f"{self.model.__name__} instances; got {type(candidate).__name__}."
                    )
            _gate_backed_rows(candidates, using=self.db, actor=actor, bypass=bypass)
            rebac_type = model_resource_type(self.model)
            if not bypass and rebac_type:
                if actor is None:
                    raise MissingActorError(
                        f"{self.model.__name__}.bulk_create() called with no actor."
                    )
                for candidate in candidates:
                    if not _check_new_model(candidate, subject=actor, using=self.db).allowed:
                        raise PermissionDenied(
                            f"Denied: {actor} cannot create a proposed {rebac_type} row."
                        )
            if update_conflicts and not bypass and rebac_type:
                raise PermissionDenied(
                    "Actor-scoped bulk_create(update_conflicts=True) cannot check write "
                    "permissions on conflicting rows. Use checked saves or explicit sudo."
                )

    def _update_query(self) -> Query:
        """The query an UPDATE reads without model default ordering."""
        query = self.query.clone()
        query.default_ordering = False
        return query

    def _guard_bulk_action(self, actor: SubjectRef, action: str) -> None:
        with database_context(self.db):
            from .backends import backend

            rebac_type = model_resource_type(self.model)
            if not rebac_type:
                return
            # Pre-fetch ids in scope, intersect with allowed.
            affected = self._affected_resource_ids_for_guard()
            if not affected:
                return
            allowed = set(
                held_ids(
                    backend(),
                    subject=actor,
                    action=action,
                    model=self.model,
                    resource_ids=affected,
                )
            )
            denied = affected - allowed
            if denied:
                raise PermissionDenied(
                    f"Bulk {action}: row outside actor scope. Bulk operations are all-or-nothing."
                )

    def _guard_bulk_field_writes(self, actor: SubjectRef, kwargs: dict[str, Any]) -> None:
        """Per-field write enforcement for bulk ``QuerySet.update()``.

        Mirrors the per-field gate run in ``RebacMixin``'s save gate: for each
        ``f`` in ``kwargs`` whose resource type declares a permission
        named ``write__<f>``, every row in the current scope must pass
        that check too — same all-or-nothing semantics as the
        resource-level check. Any denied row aborts the whole update.

        The resource-level ``write`` check already ran before this gate.
        """
        with database_context(self.db):
            from .backends import backend
            from .schema.walker import field_gated_actions

            rebac_type = model_resource_type(self.model)
            if not rebac_type:
                return
            try:
                schema = backend().schema()
            except Exception:
                return
            definition = schema.get_definition(rebac_type)
            if definition is None:
                return
            declared = field_gated_actions(definition, "write")
            if not declared:
                return

            affected = self._affected_resource_ids_for_guard()
            if not affected:
                return

            meta = self.model._meta
            for raw_name in kwargs.keys():
                # Normalise FK attname → field.name so ``write__folder`` matches
                # an ``update(folder_id=...)`` call.
                try:
                    field = meta.get_field(raw_name)
                    field_name = field.name
                except Exception:
                    field_name = raw_name
                action = f"write__{field_name}"
                if action not in declared:
                    continue
                allowed = set(
                    held_ids(
                        backend(),
                        subject=actor,
                        action=action,
                        model=self.model,
                        resource_ids=affected,
                    )
                )
                denied = affected - allowed
                if denied:
                    raise PermissionDenied(
                        f"Bulk {action}: row outside actor scope. Bulk operations are all-or-nothing."
                    )

    def _guard_bulk_expression_reads(self, actor: SubjectRef, kwargs: dict[str, Any]) -> None:
        with database_context(self.db):
            gated = gated_read_fields(self.model)
            if gated and any(_has_opaque_write_expression(value) for value in kwargs.values()):
                raise PermissionDenied(
                    "Write expression has opaque SQL over a model with gated fields."
                )
            if runtime_field_deny_mode(self._effective_field_mode()) == "allow":
                return
            from .backends import backend

            query = self.query.clone()
            required: set[str] = set()
            for value in kwargs.values():
                resolver = getattr(value, "resolve_expression", None)
                if not callable(resolver):
                    continue
                resolved = resolver(query)
                if _has_gated_subquery(resolved):
                    raise PermissionDenied("Write expression reads a subquery over gated fields.")
                required.update(
                    column.target.name
                    for column in _expression_columns(resolved)
                    if _column_on_model_lineage(column, self.model) and column.target.name in gated
                )
            if not required:
                return
            affected = self._affected_resource_ids_for_guard()
            rebac_type = model_resource_type(self.model)
            assert rebac_type is not None
            for field_name in sorted(required):
                allowed = set(
                    held_ids(
                        backend(),
                        subject=actor,
                        action=f"read__{field_name}",
                        model=self.model,
                        resource_ids=affected,
                    )
                )
                if affected - allowed:
                    raise PermissionDenied(
                        f"Bulk write expression reads denied field read__{field_name}."
                    )

    def _rows_in_scope(self, actor: SubjectRef, *, reason: str) -> RebacQuerySet[_M]:
        """The statement's rows the queryset's scope admits for ``actor``, read
        without the actor's other checks.

        A bulk write under an actor acts on these rows only, the rows
        ``count()`` counts: a row outside the scope is not part of it, so a
        refusal never depends on a row the actor cannot read.
        """
        rows = self._system_capture(reason=reason)
        # The scope stays through the clones that read the keys.
        rows._rebac_eager_scope = True
        rows._scope_query(rows.query, actor)
        return rows

    def _writing_actor(self, actor: SubjectRef | None) -> SubjectRef:
        """The actor a scoped bulk write acts for; a scope with none (an
        aggregate scope outside any actor) writes nothing and raises."""
        if actor is None:
            raise MissingActorError(
                f"Bulk write on {self.model.__name__} without an actor. "
                "Use .with_actor(actor) or .sudo(reason='...')."
            )
        return actor

    def _affected_resource_ids_for_guard(self) -> set[str]:
        """Scan the statement's rows without read-scoping the guard itself: under
        an actor the statement holds only the rows in scope (``_rows_in_scope``)."""
        attr = resource_id_attr(self.model)
        scan = self._system_capture(reason="rebac.bulk-guard")
        return {str(v) for v in scan.values_list(attr, flat=True)}


_RebacManagerBase = models.Manager.from_queryset(RebacQuerySet)


# django-stubs synthesizes a generic manager base; Pyright has no Django plugin.
class RebacManager[M: models.Model](_RebacManagerBase[M]):  # pyright: ignore[reportInvalidTypeArguments]
    """Manager backed by `RebacQuerySet`.

    Built via ``from_queryset`` so the custom queryset methods (and any
    subclass supplied through ``RebacManager.from_queryset(...)``) are
    copied onto the manager. Actor verbs preserve the manager's concrete
    model parameter through queryset chains.
    """

    def get_queryset(self) -> RebacQuerySet[M]:
        """Pin a real related manager's fresh queryset to its REBAC source."""
        queryset = cast(RebacQuerySet[M], super().get_queryset())
        owner = _related_manager_owner(self)
        if owner is not None:
            queryset._rebac_related_owner = owner
            queryset._rebac_relation_actor = owner.actor()
            queryset._rebac_relation_scope = True
        return queryset

    # Static declarations for checkers without Django's from_queryset plugin.
    # Runtime dispatch remains Django's native generated queryset methods.
    if TYPE_CHECKING:

        def with_actor(self, actor: ActorLike | None) -> RebacQuerySet[M]: ...

        def as_user(self, user: Any) -> RebacQuerySet[M]: ...

        def as_agent(self, agent: Any, *, on_behalf_of: Any | None = None) -> RebacQuerySet[M]: ...

        def with_action(self, action: str) -> RebacQuerySet[M]: ...

        def on_field_deny(self, mode: FieldDenyMode) -> RebacQuerySet[M]: ...

        def for_write(self) -> RebacQuerySet[M]: ...

        def readable(self, field: str) -> models.Expression: ...

        def rebac_select_related(self, *fields: Any) -> RebacQuerySet[M]: ...

        def rebac_prefetch_related(self, *lookups: Any) -> RebacQuerySet[M]: ...

        def sudo(self, *, reason: str) -> RebacQuerySet[M]: ...

        def system_context(self, *, reason: str) -> RebacQuerySet[M]: ...

        def scoped(self) -> RebacQuerySet[M]: ...

        def scoped_for_aggregate(self) -> RebacQuerySet[M]: ...

        def actor(self) -> SubjectRef | None: ...

        def is_sudo(self) -> bool: ...

        def effective_actor(self, *, strict: bool = False) -> tuple[SubjectRef | None, bool]: ...

        def insert(self, obj: M) -> M: ...
