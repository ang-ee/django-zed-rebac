"""Collect frozen permission id sets at an explicitly owned SELECT boundary.

Only our query factories install the compiler wrapper. Kept statements embed
SQL and parameters, never query graphs. django-cte renders the WITH prefix;
its local recursive helpers are not candidates for collection.
"""

from __future__ import annotations

import pickle
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from itertools import chain
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
    body: SQL, definitions: tuple[Definition, ...], connection: Any, explain: Any = None
) -> SQL:
    """Reuse django-cte's assembled text; bind fresh parameters in prefix order."""
    if not body[0]:
        return body
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
    query = _CompiledQuery((body, ()))
    query.explain_info = pickle.loads(explain) if explain else None
    rows: QuerySet[Any] = QuerySet(query=query)
    attached = [
        CTE(
            QuerySet(query=_CompiledQuery((sql, ()))),
            name=name,
            materialized=materialized,
        )
        for name, sql, materialized in definitions
    ]
    if not attached:
        # The frozen body's compiler doesn't render EXPLAIN itself.
        if info := query.explain_info:
            prefix = connection.ops.explain_query_prefix(info.format, **info.options)
            return f"{prefix} {body}"
        return body
    selected = with_cte(*attached, select=rows)
    sql, _params = selected.query.get_compiler(connection=connection).as_sql()
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


class _SharedRootQuery(RootQuery, JITMixin):  # type: ignore[misc]  # django-cte is untyped
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
    Unhooked roots and write statements retain nested definitions.
    """
    result = queryset.all()
    jit_mixin(result.query, _SharedRootQuery)
    return result
