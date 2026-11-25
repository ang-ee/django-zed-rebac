"""Collect frozen permission id sets at an explicitly owned SELECT boundary.

Only our query factories install the compiler wrapper. Kept statements embed
SQL and parameters, never query graphs. django-cte renders the WITH prefix;
its local recursive helpers are not candidates for collection.
"""

from __future__ import annotations

import pickle
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from itertools import accumulate, chain
from typing import Any, cast

from django.apps import apps
from django.core.exceptions import EmptyResultSet
from django.db import connections, models
from django.db.models import QuerySet
from django.db.models.sql.compiler import (
    SQLCompiler,
    SQLDeleteCompiler,
    SQLInsertCompiler,
    SQLUpdateCompiler,
)
from django.db.models.sql.constants import LOUTER
from django.db.models.sql.query import Query
from django_cte import CTE, with_cte

# All django-cte private integration lives in this module. See the named
# adapter debt and its pinned compatibility tests in ARCHITECTURE.md.
from django_cte.jitmixin import JITMixin, jit_mixin, jit_mixin_type
from django_cte.join import QJoin
from django_cte.query import CTECompiler, _ignore_with_col_aliases

from .. import clock as application_clock
from ..errors import SchemaError

type SQL = tuple[str, tuple[Any, ...]]


class Marker:
    __slots__ = ("label",)

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"<{self.label}>"


@dataclass(frozen=True, slots=True)
class ActorNative:
    """Placeholder for the actor's id as a value of ``field``."""

    field: models.Field[Any, Any]


def _encode(value: Any) -> bytes:
    buffer = BytesIO()
    encoder = pickle.Pickler(buffer, protocol=5)
    # Equality must not depend on Python object sharing.
    encoder.fast = True
    encoder.dump(value)
    return buffer.getvalue()


def _parameter(value: Any) -> Any:
    if isinstance(value, Marker):
        return type(value), value.label
    if isinstance(value, ActorNative):
        # The registered field identifies the conversion. Its creation default
        # (possibly a lambda) and validators do not participate in SQL binding.
        return type(value), value.field.model._meta.label_lower, value.field.name
    if isinstance(value, Mapping):
        pairs = [(_parameter(key), _parameter(item)) for key, item in value.items()]
        return type(value), tuple(sorted(pairs, key=lambda pair: _encode(pair[0])))
    if isinstance(value, (list, tuple)):
        return type(value), tuple(map(_parameter, value))
    if isinstance(value, (set, frozenset)):
        return type(value), tuple(sorted(_encode(_parameter(v)) for v in value))
    if isinstance(value, (memoryview, bytearray)):
        return bytes(value)
    return value


def signature(sql: str, params: tuple[Any, ...]) -> bytes:
    """Full template digest; names use a short prefix, checked on collection."""
    return sha256(_encode((sql, _parameter(params)))).digest()


def id_sql(query: Query, using: str) -> SQL:
    # Freeze the same compact aliases Django assigns when embedding a query.
    # A fresh outer query makes the names independent of the eventual scope.
    # Resolution clones the Query, UNION branches and nested expression queries;
    # django-cte also clones its helper queries. Django's Query.clone() copies
    # the table_map dict but shares its lists: alias relabeling updates those
    # lists on the caller too. This is not a deep copy of the query graph.
    query = query.resolve_expression(Query(None))
    # Match django-cte's column naming for UNION bodies, on our own clone.
    _ignore_with_col_aliases(query)
    sql, params = query.get_compiler(using=using).as_sql()
    return sql, tuple(params)


def id_name(key: bytes) -> str:
    return "rebac_ids_" + key.hex()[:16]


def table_names() -> set[str]:
    return {model._meta.db_table for model in apps.get_models(include_auto_created=True)}


def safe_name(name: str, tables: set[str]) -> str:
    while name in tables:
        name = "_" + name
    if len(name) > 63:
        raise SchemaError("Cannot allocate a REBAC CTE name within the identifier limit")
    return name


@dataclass(frozen=True, slots=True)
class Definition:
    name: str
    sql: str
    params: tuple[Any, ...]
    template: bytes
    materialized: bool | None = None


class _CompiledQuery(Query):
    """A frozen Django-produced fragment, consumed through public with_cte."""

    def __init__(self, result: SQL) -> None:
        super().__init__(None)
        self.result = result

    def get_compiler(
        self, using: Any = None, connection: Any = None, elide_empty: bool = True
    ) -> Any:
        return _CompiledCompiler(self, connection, using)


class _CompiledCompiler(SQLCompiler):
    def as_sql(self, *args: Any, **kwargs: Any) -> SQL:
        return cast(_CompiledQuery, self.query).result


def assemble(
    body: SQL,
    definitions: tuple[Definition, ...],
    connection: Any,
    explain: Any = None,
    *,
    second: bool = False,
) -> SQL:
    """Reuse django-cte's assembled text; bind fresh parameters in the order of the text.

    ``second`` marks another binding of id sets that the statement around it
    already names (``embedded``).
    """
    if not body[0]:
        return body
    if definitions and _counts_cte_copies(connection):
        # The statement around a second binding cannot be rewritten once a
        # nested WITH defines its names again, so that binding never keeps one.
        budget = 0 if second else _CURSOR_BUDGET
        body, definitions = _in_place(body, definitions, connection.ops.quote_name, budget)
    if not definitions and not explain:
        return body
    sql = _assembled_sql(
        connection.alias,
        connection.vendor,
        body[0],
        tuple((item.name, item.sql, item.materialized) for item in definitions),
        _encode(explain) if explain else None,
    )
    return sql, (*chain.from_iterable(item.params for item in definitions), *body[1])


# ---------- Id sets written in place, where SQLite counts a CTE's copies ----------

# SQLite 3.54 copies a CTE at every FROM item that names it, and refuses the
# statement ("VIEWs and/or subqueries nested too deep") when the cursors it has
# allocated reach SQLITE_LIMIT_EXPR_DEPTH as it resolves a CTE's name. A
# statement that names no CTE is not held to that count.
_SQLITE_COUNTS_CTE_COPIES = (3, 54)
# From this many cursors a statement's id sets are written in place. SQLite's
# limit is 1000 on a standard build. The count is taken from the text and is
# SQLite's own for text the library writes; the margin is for the few cursors
# a text does not show (the tables behind a view). A connection that lowers
# the limit is not followed: compiling a statement opens none.
_CURSOR_BUDGET = 900
# Past either of these a statement stays as it is written, for SQLite to
# refuse. Every copy of a set repeats its cursors and its text, so a policy
# whose paths multiply unfolds without bound: SQLite takes seconds and half a
# gigabyte to prepare 12,000 cursors written in place. The widest statement of
# the policy benchmark has 3,800 and is 0.5 MB long.
_CURSOR_CEILING = 8_000
_IN_PLACE_SIZE = 2_000_000
_FROM_ITEM = re.compile(r"\b(?:FROM|JOIN)\b")
# One text of a statement: an id set's quoted name (None for an application
# CTE and for the body), its SQL and how many values it binds.
type _Text = tuple[str | None, str, int]
# A text cut at a reference it makes: the text before it with the positions of
# its values, then what opens the set referred to, that set and what closes it.
type _Piece = tuple[str, tuple[int, ...], tuple[str, int, str] | None]


def _counts_cte_copies(connection: Any) -> bool:
    # Asked of SQLite alone: its version is the linked library's, read without a connection.
    return connection.vendor == "sqlite" and bool(
        connection.get_database_version() >= _SQLITE_COUNTS_CTE_COPIES
    )


def _in_place(
    body: SQL, definitions: tuple[Definition, ...], quote: Callable[[str], str], budget: int
) -> tuple[SQL, tuple[Definition, ...]]:
    """Write each id set where it is named, in a statement of ``budget`` cursors or more.

    Returns the body and the definitions that keep their names: all of them for
    a statement that stays as it is, application CTEs alone otherwise. Values
    follow the text: a set written at several places binds its values at each.
    """
    texts: tuple[_Text, ...] = (
        *(
            # An application CTE has no template: it keeps its name.
            (quote(item.name) if item.template else None, item.sql, len(item.params))
            for item in definitions
        ),
        (None, body[0], len(body[1])),
    )
    unfolded = _unfolded(texts)
    if unfolded is None:
        return body, definitions
    cursors, size = unfolded
    if not budget <= cursors <= _CURSOR_CEILING or size > _IN_PLACE_SIZE:
        return body, definitions
    pool = (*chain.from_iterable(item.params for item in definitions), *body[1])
    *applications, (sql, order) = _placed(texts)
    kept = tuple(
        Definition(
            item.name,
            placed_sql,
            tuple(pool[position] for position in placed_order),
            item.template,
            item.materialized,
        )
        for item, (placed_sql, placed_order) in zip(
            (item for item in definitions if not item.template), applications, strict=True
        )
    )
    return (sql, tuple(pool[position] for position in order)), kept


def _references(texts: tuple[_Text, ...]) -> tuple[re.Pattern[str], dict[str, int]]:
    """A pattern for each placeholder and each use of an id set's name in ``texts``.

    Groups: the FROM or JOIN keyword, the name and the alias of a reference;
    or the name alone and the dot that makes it a column's qualifier (also
    after a FROM that is an operator's, as in IS DISTINCT FROM).
    """
    sets = {name: index for index, (name, _sql, _count) in enumerate(texts) if name}
    names = "|".join(map(re.escape, sets))
    return (
        re.compile(
            rf'%%|%s|\b(FROM|JOIN)\s+({names})(?!\.)(?:\s+(?:AS\s+)?("[^"]*"))?|({names})(\.)?'
        ),
        sets,
    )


@lru_cache(maxsize=512)
def _unfolded(texts: tuple[_Text, ...]) -> tuple[int, int] | None:
    """The cursors SQLite allocates for a statement, and its length written in place.

    Counted as SQLite does: every FROM item of a text, and at each one naming
    an id set, that set's count again. The length adds up the same way. None
    when the statement cannot be written in place.
    """
    token, sets = _references(texts)
    if not sets:
        return None
    cursors: list[int] = []
    sizes: list[int] = []
    for index, (_name, sql, count) in enumerate(texts):
        referred = []
        bound = 0
        for match in token.finditer(sql):
            if match[0] == "%s":
                bound += 1
            elif match[2]:
                referred.append(sets[match[2]])
            elif match[4] and not match[5]:
                # The name is used some other way: a nested WITH that defines
                # it again, for a second binding. Its references are its own.
                return None
        if bound != count or any(other >= index for other in referred):
            # Values that cannot be told apart by their placeholders; or a set
            # that names itself, or one defined after it.
            return None
        cursors.append(len(_FROM_ITEM.findall(sql)) + sum(cursors[other] for other in referred))
        sizes.append(len(sql) + sum(sizes[other] for other in referred))
    stay = [index for index, (name, _sql, _count) in enumerate(texts) if name is None]
    return sum(cursors[index] for index in stay), sum(sizes[index] for index in stay)


@lru_cache(maxsize=64)
def _placed(texts: tuple[_Text, ...]) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """The texts that keep their place (application CTEs, then the body), id sets written in.

    Each is paired with the positions of the values it binds, in text order,
    among the values of ``texts`` taken in their order. Text alone is kept
    here, never a bound value. ``_unfolded`` has accepted ``texts``.
    """
    token, sets = _references(texts)
    starts = tuple(accumulate((count for _name, _sql, count in texts), initial=0))
    cut: list[list[_Piece]] = []
    for index, (_name, sql, _count) in enumerate(texts):
        pieces: list[_Piece] = []
        positions: list[int] = []
        start = 0
        taken = starts[index]
        for match in token.finditer(sql):
            if match[0] == "%s":
                positions.append(taken)
                taken += 1
            elif match[2]:
                # An unaliased set keeps its name, which its columns are qualified by.
                reference = (f"{match[1]} (", sets[match[2]], f") {match[3] or match[2]}")
                pieces.append((sql[start : match.start()], tuple(positions), reference))
                positions = []
                start = match.end()
        pieces.append((sql[start:], tuple(positions), None))
        cut.append(pieces)

    def written(index: int) -> tuple[str, tuple[int, ...]]:
        # One pass over the unfolded text, without a copy of each set on the way.
        parts: list[str] = []
        order: list[int] = []
        unfinished = [(iter(cut[index]), "")]
        while unfinished:
            rest, closing = unfinished[-1]
            for text, bound, reference in rest:
                parts.append(text)
                order += bound
                if reference is not None:
                    opening, inner, inner_closing = reference
                    parts.append(opening)
                    unfinished.append((iter(cut[inner]), inner_closing))
                    break
            else:
                parts.append(closing)
                unfinished.pop()
        return "".join(parts), tuple(order)

    return tuple(written(index) for index, (name, _sql, _count) in enumerate(texts) if name is None)


@lru_cache(maxsize=512)
def _assembled_sql(
    using: str,
    vendor: str,
    body: str,
    definitions: tuple[tuple[str, str, bool | None], ...],
    explain: bytes | None,
) -> str:
    # Only text and rendering options enter this cache, never bound values or
    # query graphs. The cache key includes the database's quoting convention.
    connection = connections[using]
    # Consumers can install a different 5.0.x patch without running our pins.
    # Sentinels verify parameter order independently of text containing nested
    # definitions with the same names. Nothing here executes these parameters.
    order = tuple(object() for _ in range(len(definitions) + 1))
    query = _CompiledQuery((body, (order[-1],)))
    query.explain_info = pickle.loads(explain) if explain else None
    rows: QuerySet[Any] = QuerySet(query=query)
    attached = [
        CTE(
            QuerySet(query=_CompiledQuery((sql, (order[index],)))),
            name=name,
            materialized=materialized,
        )
        for index, (name, sql, materialized) in enumerate(definitions)
    ]
    if not attached:
        # The frozen body's compiler doesn't render EXPLAIN itself.
        if info := query.explain_info:
            prefix = connection.ops.explain_query_prefix(info.format, **info.options)
            return f"{prefix} {body}"
        return body
    selected = with_cte(*attached, select=rows)
    sql, params = selected.query.get_compiler(connection=connection).as_sql()
    if tuple(params) != order or not sql.endswith(body):
        raise SchemaError("django-cte changed statement assembly; review the adapter")
    position = 0
    for name, _sql, _materialized in definitions:
        marker = f"{connection.ops.quote_name(name)} AS "
        found = sql.find(marker, position)
        if found < 0:
            raise SchemaError("django-cte changed definition order; review the adapter")
        position = found + len(marker)
    return cast(str, sql)


def _same_params(left: Sequence[Any], right: Sequence[Any]) -> bool:
    return len(left) == len(right) and all(
        _same_value(a, b) for a, b in zip(left, right, strict=True)
    )


def _same_value(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, (tuple, list)):
        return _same_params(left, right)
    if isinstance(left, Mapping):
        return len(left) == len(right) and all(
            key in right
            and _same_value(value, right[key])
            and any(_same_value(key, other) for other in right)
            for key, value in left.items()
        )
    if isinstance(left, (set, frozenset)):
        return len(left) == len(right) and all(
            any(_same_value(value, other) for other in right) for value in left
        )
    return bool(left == right)


@dataclass
class _Statement:
    now: datetime
    definitions: dict[str, Definition] = field(default_factory=dict)

    def add(self, definitions: tuple[Definition, ...]) -> bool:
        """Atomically accept one binding, or keep its whole nested WITH.

        A dependent set can have unchanged parameters while one of its
        dependencies has a different actor. Never register a partial binding.
        """
        compatible = True
        seen = dict(self.definitions)
        for item in definitions:
            previous = seen.get(item.name)
            if previous is not None:
                if previous.template != item.template:
                    raise SchemaError("Conflicting REBAC id-set CTE definitions")
                if not _same_params(item.params, previous.params):
                    compatible = False
            seen[item.name] = item
        if compatible:
            for item in definitions:
                self.definitions.setdefault(item.name, item)
        return compatible


_current: ContextVar[_Statement | None] = ContextVar("rebac_cte_statement", default=None)


@contextmanager
def isolated() -> Iterator[None]:
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


@contextmanager
def collecting() -> Iterator[_Statement]:
    statement = _Statement(statement_now())
    token = _current.set(statement)
    try:
        yield statement
    finally:
        _current.reset(token)


def statement_now() -> datetime:
    """One clock for all embeddings; isolated executions get a new instant."""
    statement = _current.get()
    return statement.now if statement is not None else application_clock.application_now()


def register(definitions: tuple[Definition, ...]) -> bool:
    statement = _current.get()
    return statement is not None and statement.add(definitions)


def embedded() -> bool:
    """Whether what compiles now is part of a statement that collects id sets."""
    return _current.get() is not None


def _compile(compiler: Any, method: Callable[[], Any], body: Callable[[], Any]) -> Any:
    query = compiler.query
    own = getattr(query, "_rebac_id_sets", ())
    if _current.get() is not None:
        if not own or register(own):
            return method()
        # Another binding already owns these names. Its dependencies must
        # shadow the outer binding together, including any embedded scopes.
        return _render(compiler, body, own)
    if compiler._rebac_cte_root or own:
        return _render(compiler, body, own)
    return method()


def _render(compiler: Any, body_method: Callable[[], Any], own: tuple[Definition, ...]) -> Any:
    query = compiler.query
    attached = getattr(query, "_with_ctes", ())
    explain = query.explain_info
    # Rendered inside a collecting statement only for a binding that statement refused.
    second = embedded()
    # Exactly the metadata bracket used by django-cte. It is restored even
    # on failure; no query graph or compiler.query substitution is involved.
    query.explain_info = None
    try:
        with collecting() as statement:
            if not statement.add(own):
                raise SchemaError("Conflicting REBAC id-set bindings at one query boundary")
            application = []
            for cte in attached:
                body = cte.query.clone()
                _ignore_with_col_aliases(body)
                alias = query.alias_map.get(cte.name)
                child = body.get_compiler(
                    connection=compiler.connection,
                    elide_empty=not isinstance(alias, QJoin) or alias.join_type != LOUTER,
                )
                try:
                    sql, params = child.as_sql()
                except EmptyResultSet:
                    # Django still needs selection metadata for an empty CTE.
                    body_method()
                    raise
                application.append(Definition(cte.name, sql, tuple(params), b"", cte.materialized))
            result = body_method()
            if not result[0]:
                return result
            if any(item.name in statement.definitions for item in application):
                raise SchemaError("An application CTE conflicts with a REBAC id-set name")
            return assemble(
                (result[0], tuple(result[1])),
                (*statement.definitions.values(), *application),
                compiler.connection,
                explain,
                second=second,
            )
    finally:
        query.explain_info = explain


class CompilerMixin(JITMixin):  # type: ignore[misc]  # django-cte is untyped
    """Package seam for compiler mixins; JIT construction stays in this adapter."""


class _StatementCompiler(CompilerMixin):
    """Own the statement boundary before django-cte's prefix renderer."""

    _jit_mixin_prefix = "RebacStatement"

    def as_sql(self, *args: Any, **kwargs: Any) -> Any:
        return _compile(
            self,
            lambda: cast(Any, super(_StatementCompiler, self)).as_sql(*args, **kwargs),
            # Skip only django-cte's prefix; preserve the Django compiler's
            # query and mutation state, including MTI update keys.
            lambda: cast(Any, super(CTECompiler, cast(Any, self))).as_sql(*args, **kwargs),
        )

    def execute_sql(self, *args: Any, **kwargs: Any) -> Any:
        # An execution during compilation owns a separate statement.
        with isolated():
            return cast(Any, super()).execute_sql(*args, **kwargs)


def hook(compiler: Any, *, root: bool) -> Any:
    # Writes keep their original nested WITH on every vendor. In particular
    # MySQL doesn't support WITH ... INSERT, and MTI UPDATE must retain its
    # own related_ids between pre_sql_setup() and get_related_updates().
    if isinstance(compiler, (SQLInsertCompiler, SQLUpdateCompiler, SQLDeleteCompiler)):
        return compiler
    # Apply CTECompiler here, so a later CTEQuery.get_compiler() is a no-op.
    # _StatementCompiler must precede CTECompiler in the final MRO.
    jit_mixin(compiler, CTECompiler)
    jit_mixin(compiler, _StatementCompiler)
    compiler._rebac_cte_root = root
    return compiler


def outer_compiler_mixin(compiler: Any, mixin: type[CompilerMixin], *, root: bool) -> Any:
    """Install an observer outside the final statement/CTE compiler methods."""
    if isinstance(compiler.query, RootQuery):
        hook(compiler, root=root)
    elif hasattr(compiler.query, "_with_ctes"):
        jit_mixin(compiler, CTECompiler)
    return jit_mixin(compiler, mixin)


def root_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    # Django supplies a connection when compiling expressions and UNION
    # branches. Top-level factories and an AggregateQuery's inner SELECT
    # supply only the database alias. The latter owns a local WITH boundary.
    using = args[0] if args else kwargs.get("using")
    connection = args[1] if len(args) > 1 else kwargs.get("connection")
    return using is not None and connection is None


class RootQuery:
    _with_ctes: tuple[Any, ...] = ()

    def get_compiler(self, *args: Any, **kwargs: Any) -> Any:
        compiler = super().get_compiler(*args, **kwargs)  # type: ignore[misc]
        return hook(compiler, root=root_call(args, kwargs))

    def get_aggregation(self, *args: Any, **kwargs: Any) -> Any:
        # Django's AggregateQuery wrapper does not inherit our compiler.
        with isolated():
            return super().get_aggregation(*args, **kwargs)  # type: ignore[misc]


class _SharedRootQuery(JITMixin, RootQuery):  # type: ignore[misc]  # django-cte is untyped
    _jit_mixin_prefix = "RebacIds"


class _IdSetQuery(_SharedRootQuery):
    _jit_mixin_prefix = "RebacIdSet"
    _rebac_id_sets: tuple[Definition, ...] = ()

    def combine(self, rhs: Any, connector: Any) -> None:
        cast(Any, super()).combine(rhs, connector)
        self._rebac_id_sets += getattr(rhs, "_rebac_id_sets", ())


def rebase_query(query: Any, base: type[Query]) -> None:
    """Give a plain Query an execution owner, preserving any JIT opt-ins."""
    if type(query) is Query:
        query.__class__ = base
    elif isinstance(query, JITMixin) and cast(Any, query)._jit_mixin_base is Query:
        query.__class__ = jit_mixin_type(base, *cast(Any, query)._jit_mixins)


def attach(rows: QuerySet[Any], definitions: tuple[Definition, ...]) -> QuerySet[Any]:
    rows = rows.all()
    query = rows.query
    jit_mixin(query, _IdSetQuery)
    owned = cast(Any, query)
    owned._rebac_id_sets = getattr(query, "_rebac_id_sets", ()) + definitions
    return rows


def share_id_sets[T: QuerySet[Any]](queryset: T) -> T:
    """Opt a plain SELECT root (including with_cte) into permission CTE sharing.

    Return a clone. This changes SQL assembly only, not actor scope or audits.
    REBAC querysets already share; they do not need this helper. Django's Query
    class swaps for update()/delete() lose the opt-in; writes keep nested WITHs.
    Pickling preserves the opt-in on roots without embedded permission scopes;
    those scopes retain unpicklable backend state. REBAC queryset pickling still
    strips authority. Custom Query roots need an explicit opt-in when executed
    during compilation of another statement; they are not automatically owned.
    """
    result = queryset.all()
    jit_mixin(result.query, _SharedRootQuery)
    return result
