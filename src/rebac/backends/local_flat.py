"""Compile bounded recursive predicates without SQL nesting that grows with the bound.

The recursive compiler unrolls each dispatch hop as a correlated ``EXISTS`` or
``IN`` subquery inside the previous hop, so the ORM keeps owning joins, identity
conversions and the relationship storage shapes. That nesting grows with
``REBAC_DEPTH_LIMIT``, and SQLite parses nested SELECTs on a fixed stack and
bounds expression height, so builds differ in the bound they accept.

Positive existentials commute with joins: ``EXISTS(A WHERE p AND EXISTS(B WHERE
q))`` is ``EXISTS(A CROSS JOIN B WHERE p AND q)`` and ``x IN (SELECT y FROM B
WHERE q)`` is ``EXISTS(B WHERE q AND x = y)``. Applying that identity to every
nested existential and distributing over disjunctions yields one existential
SELECT per dispatch path: the bound grows the width of the SQL, never its depth.
Subqueries without nested existentials, and every leaf condition, keep the SQL
the ORM compiled for them; their aliases are renamed with Django's own
relabeling so sibling paths never collide in one FROM clause.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import product
from typing import Any

from django.core.exceptions import EmptyResultSet, FullResultSet
from django.db import models
from django.db.models import Exists, Value
from django.db.models.expressions import Col, Subquery
from django.db.models.lookups import Exact, In
from django.db.models.sql import Query
from django.db.models.sql.where import WhereNode


@dataclass(frozen=True, slots=True)
class _Fragment:
    sql: str
    params: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class _Path:
    """One existential: FROM sources joined, conditions conjoined."""

    sources: tuple[_Fragment, ...] = ()
    conditions: tuple[_Fragment, ...] = ()


def _conjoin(left: list[_Path], right: list[_Path]) -> list[_Path]:
    return [
        _Path(a.sources + b.sources, a.conditions + b.conditions) for a, b in product(left, right)
    ]


def _existential(node: Any) -> tuple[Query, Any] | None:
    """The subquery and, for ``IN``, the compared left-hand side."""

    if isinstance(node, Exact) and node.rhs is True and isinstance(node.lhs, Exists):
        node = node.lhs
    if isinstance(node, Exists):
        return node.query, None
    if isinstance(node, In):
        if isinstance(node.rhs, Subquery):
            return node.rhs.query, node.lhs
        if isinstance(node.rhs, Query):
            return node.rhs, node.lhs
    return None


def _nests(node: Any) -> bool:
    if isinstance(node, WhereNode):
        return any(_nests(child) for child in node.children)
    return _existential(node) is not None


def _columns(node: Any) -> list[Col]:
    """Include correlations inside subqueries (Query has no source expressions)."""

    if isinstance(node, Col):
        return [node]
    if isinstance(node, Query):
        children = [node.where, *node.select, *node.annotations.values()]
    else:
        children = node.get_source_expressions()
    return [col for child in children if child is not None for col in _columns(child)]


def _replace_columns(node: Any, replacements: dict[Col, Col]) -> Any:
    if isinstance(node, Col):
        return replacements.get(node, node)
    if isinstance(node, Query):
        node = node.clone()
        node.where = _replace_columns(node.where, replacements)
        node.select = tuple(_replace_columns(col, replacements) for col in node.select)
        node.annotations = {
            name: _replace_columns(expr, replacements) for name, expr in node.annotations.items()
        }
        return node
    clone = node.copy()
    clone.set_source_expressions(
        [
            _replace_columns(child, replacements) if child is not None else None
            for child in node.get_source_expressions()
        ]
    )
    return clone


class FlatPredicate(models.Expression):
    """Compile a resolved predicate with constant SELECT nesting."""

    def __init__(self, predicate: Any) -> None:
        super().__init__(output_field=models.BooleanField())
        self.expressions = [predicate]

    def get_source_expressions(self) -> list[Any]:
        return self.expressions

    def set_source_expressions(self, exprs: Sequence[Any]) -> None:
        self.expressions = list(exprs)

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        flattener = _Flattener(compiler, connection)
        fragment = flattener.emit(flattener.paths(self.expressions[0]))
        return fragment.sql, fragment.params


class _Flattener:
    def __init__(self, compiler: Any, connection: Any) -> None:
        self.compiler = compiler
        self.connection = connection
        self.subqueries = 0
        self.aliases = set(compiler.query.alias_map) | set(compiler.query.external_aliases)

    def paths(self, node: Any, negated: bool = False) -> list[_Path]:
        """Disjunctive paths of ``node``, distributing only over nested existentials."""

        if isinstance(node, WhereNode) and _nests(node):
            negated ^= node.negated
            conjunction = (node.connector == WhereNode.default) != negated
            result = [_Path()] if conjunction else []
            for child in node.children:
                branch = self.paths(child, negated)
                result = _conjoin(result, branch) if conjunction else result + branch
            return result
        if isinstance(node, Exact) and node.rhs is True and isinstance(node.lhs, Value):
            node = node.lhs
        if isinstance(node, Value) and isinstance(node.value, bool):
            return [_Path()] if node.value != negated else []
        existential = _existential(node)
        if existential is not None and _nests(existential[0].where):
            inner = self.subquery(*existential)
            if not negated:
                return inner
            try:
                fragment = self.emit(inner)
            except EmptyResultSet:
                return [_Path()]
            except FullResultSet:
                return []
            return [_Path(conditions=(_Fragment(f"NOT {fragment.sql}", fragment.params),))]
        try:
            sql, params = self.compiler.compile(node)
        except EmptyResultSet:
            return [_Path()] if negated else []
        except FullResultSet:
            return [] if negated else [_Path()]
        if negated:
            sql = f"NOT ({sql})"
        return [_Path(conditions=(_Fragment(sql, tuple(params)),))]

    def subquery(self, original: Query, lhs: Any) -> list[_Path]:
        """Lift a subquery's FROM clause beside its conditions."""

        query = original.clone()
        occupied = self.aliases | set(query.alias_map)
        while True:
            self.subqueries += 1
            alias = f"fp{self.subqueries}"
            aliases: dict[str | None, str] = {
                name: f"{alias}_{i}" for i, name in enumerate(query.alias_map)
            }
            names = {alias, *aliases.values()}
            if names.isdisjoint(occupied):
                break
        self.aliases.update(names)
        query.change_aliases(aliases)
        compiler = query.get_compiler(connection=self.connection)
        clauses, params = compiler.get_from_clause()
        source = _Fragment(" ".join(clauses), tuple(params))
        selected = compiler.get_select()[0][0][0] if lhs is not None else None
        if len(clauses) > 1:
            # A registry hop has three tables. Keep its storage-owned joins
            # behind one derived source, rather than hitting SQLite's 64-table
            # join limit at bound 24. DISTINCT prevents SQLite from merging
            # these tables back into the outer join; it cannot change EXISTS
            # membership. This also handles multi-table field/path backings.
            columns = {col: None for col in _columns(query) if col.alias in query.alias_map}
            if selected is not None:
                columns.update({col: None for col in _columns(selected)})
            replacements: dict[Col, Col] = {}
            projections: list[str] = []
            quote = self.connection.ops.quote_name
            ordered = sorted(columns, key=lambda c: (c.alias, c.target.column or ""))
            for index, col in enumerate(ordered):
                name = f"c{index}"
                sql, col_params = compiler.compile(col)
                assert not col_params
                projections.append(f"{sql} AS {quote(name)}")
                field = col.target.clone()
                field.column = name
                replacements[col] = Col(alias, field, col.output_field)
            source = _Fragment(
                f"(SELECT DISTINCT {', '.join(projections)} FROM {source.sql}) {quote(alias)}",
                source.params,
            )
            query.where = _replace_columns(query.where, replacements)
            if selected is not None:
                selected = _replace_columns(selected, replacements)
        paths = self.paths(query.where)
        if lhs is not None:
            left = self.compiler.compile(lhs)
            right = self.compiler.compile(selected)
            paths = _conjoin(
                paths,
                [_Path(conditions=(_Fragment(f"{left[0]} = {right[0]}", (*left[1], *right[1])),))],
            )
        return [_Path((source, *path.sources), path.conditions) for path in paths]

    @staticmethod
    def emit(paths: list[_Path]) -> _Fragment:
        if not paths:
            raise EmptyResultSet
        if any(not path.sources and not path.conditions for path in paths):
            raise FullResultSet
        branches: list[str] = []
        params: list[Any] = []
        for path in paths:
            sources = " CROSS JOIN ".join(part.sql for part in path.sources)
            conditions = " AND ".join(f"({part.sql})" for part in path.conditions)
            params.extend(
                value for part in (*path.sources, *path.conditions) for value in part.params
            )
            if not path.sources:
                branches.append(conditions)
            elif not conditions:
                branches.append(f"EXISTS (SELECT 1 FROM {sources})")
            else:
                branches.append(f"EXISTS (SELECT 1 FROM {sources} WHERE {conditions})")
        return _Fragment("(" + " OR ".join(branches) + ")", tuple(params))
