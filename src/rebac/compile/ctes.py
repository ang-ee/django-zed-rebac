"""Collect frozen permission id sets at an explicitly owned SELECT boundary.

Only our query factories install the compiler wrapper. Kept statements embed
SQL and parameters, never query graphs. django-cte renders the WITH prefix;
its local recursive helpers are not candidates for collection.
"""

from __future__ import annotations

import pickle
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from typing import Any, cast

from django.apps import apps
from django.core.exceptions import EmptyResultSet
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

# The two compatibility seams needed to preserve django-cte's application
# CTEs while rendering the SELECT body once. No django-cte SQL is reproduced.
from django_cte.query import CTECompiler, _ignore_with_col_aliases

from ..errors import SchemaError

type SQL = tuple[str, tuple[Any, ...]]


def _encode(value: Any) -> bytes:
    buffer = BytesIO()
    encoder = pickle.Pickler(buffer, protocol=5)
    # Equality must not depend on Python object sharing.
    encoder.fast = True
    encoder.dump(value)
    return buffer.getvalue()


def _parameter(value: Any) -> Any:
    from .predicate import ActorNative, _Marker

    if isinstance(value, _Marker):
        return type(value), value.label
    if isinstance(value, ActorNative):
        return type(value), _parameter(value.field.deconstruct())
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
    """Let django-cte place definitions, EXPLAIN and positional parameters."""
    if not body[0]:
        return body
    query = _CompiledQuery(body)
    query.explain_info = explain
    rows: QuerySet[Any] = QuerySet(query=query)
    attached = [
        CTE(
            QuerySet(query=_CompiledQuery((item.sql, item.params))),
            name=item.name,
            materialized=item.materialized,
        )
        for item in definitions
    ]
    if not attached:
        # The frozen body's compiler doesn't render EXPLAIN itself.
        if explain:
            prefix = connection.ops.explain_query_prefix(explain.format, **explain.options)
            return f"{prefix} {body[0]}", body[1]
        return body
    selected = with_cte(*attached, select=rows)
    sql, params = selected.query.get_compiler(connection=connection).as_sql()
    return sql, tuple(params)


@dataclass
class _Statement:
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
                if signature("", item.params) != signature("", previous.params):
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
    statement = _Statement()
    token = _current.set(statement)
    try:
        yield statement
    finally:
        _current.reset(token)


def register(definitions: tuple[Definition, ...]) -> bool:
    statement = _current.get()
    return statement is not None and statement.add(definitions)


def _compile(compiler: Any, method: Callable[[], Any]) -> Any:
    query = compiler.query
    own = getattr(query, "_rebac_id_sets", ())
    if _current.get() is not None:
        if not own or register(own):
            return method()
        # Another binding already owns these names. Its dependencies must
        # shadow the outer binding together, including any embedded scopes.
        with isolated():
            return _render(compiler, method, own)
    if compiler._rebac_cte_root or own:
        return _render(compiler, method, own)
    return method()


def _render(compiler: Any, method: Callable[[], Any], own: tuple[Definition, ...]) -> Any:
    query = compiler.query
    attached = getattr(query, "_with_ctes", ())
    # Skip only django-cte's prefix renderer; the original Django compiler,
    # query object and all its mutation/selection state remain in place.
    body_method: Callable[[], Any] = (
        (
            lambda: super(CTECompiler, compiler).as_sql(
                *compiler._rebac_cte_args, **compiler._rebac_cte_kwargs
            )
        )
        if isinstance(compiler, CTECompiler)
        else method
    )
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
                    elide_empty=getattr(alias, "join_type", None) != LOUTER,
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


class _StatementCompiler:
    """Observe the final compiler, including a later django-cte mixin."""

    def __getattribute__(self, name: str) -> Any:
        method = super().__getattribute__(name)
        if name == "as_sql":

            def as_sql(*args: Any, **kwargs: Any) -> Any:
                self._rebac_cte_args, self._rebac_cte_kwargs = args, kwargs
                return _compile(self, lambda: method(*args, **kwargs))

            return as_sql
        if name == "execute_sql":

            def execute_sql(*args: Any, **kwargs: Any) -> Any:
                # Policy/caveat reads during compilation execute independently.
                with isolated():
                    return method(*args, **kwargs)

            return execute_sql
        return method


_compiler_types: dict[type[Any], type[Any]] = {}
_query_types: dict[tuple[type[Any], type[Any]], type[Any]] = {}


def hook(compiler: Any, *, root: bool) -> Any:
    # Writes keep their original nested WITH on every vendor. In particular
    # MySQL doesn't support WITH ... INSERT, and MTI UPDATE must retain its
    # own related_ids between pre_sql_setup() and get_related_updates().
    if isinstance(compiler, (SQLInsertCompiler, SQLUpdateCompiler, SQLDeleteCompiler)):
        return compiler
    if not isinstance(compiler, _StatementCompiler):
        base = type(compiler)
        if base not in _compiler_types:
            _compiler_types[base] = type(
                # Existing execution/audit wrappers remain outside ours,
                # including when we bypass django-cte's prefix renderer.
                f"RebacStatement{base.__name__}",
                (base, _StatementCompiler),
                {},
            )
        compiler.__class__ = _compiler_types[base]
    compiler._rebac_cte_root = root
    return compiler


def _mix(query: Any, mixin: type[Any]) -> None:
    if not isinstance(query, mixin):
        key = type(query), mixin
        if key not in _query_types:
            _query_types[key] = type(f"RebacIds{type(query).__name__}", (mixin, type(query)), {})
        query.__class__ = _query_types[key]


def root_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    # Django supplies a connection when compiling expressions and UNION
    # branches. Top-level factories and an AggregateQuery's inner SELECT
    # supply only the database alias. The latter owns a local WITH boundary.
    using = args[0] if args else kwargs.get("using")
    connection = args[1] if len(args) > 1 else kwargs.get("connection")
    return using is not None and connection is None


class _RootQuery:
    def get_compiler(self, *args: Any, **kwargs: Any) -> Any:
        compiler = super().get_compiler(*args, **kwargs)  # type: ignore[misc]
        return hook(compiler, root=root_call(args, kwargs))


class _IdSetQuery(_RootQuery):
    _rebac_id_sets: tuple[Definition, ...] = ()

    def combine(self, rhs: Any, connector: Any) -> None:
        super().combine(rhs, connector)  # type: ignore[misc]
        self._rebac_id_sets += getattr(rhs, "_rebac_id_sets", ())


def attach(rows: QuerySet[Any], definitions: tuple[Definition, ...]) -> QuerySet[Any]:
    rows = rows.all()
    query = rows.query
    _mix(query, _IdSetQuery)
    owned = cast(Any, query)
    owned._rebac_id_sets = getattr(query, "_rebac_id_sets", ()) + definitions
    return rows


def share_id_sets[T: QuerySet[Any]](queryset: T) -> T:
    """Opt a plain SELECT root (including with_cte) into permission CTE sharing.

    Return a clone. This changes SQL assembly only, not actor scope or audits.
    Unhooked roots and write statements retain nested definitions.
    """
    result = queryset.all()
    _mix(result.query, _RootQuery)
    return result
