"""Schema graphs: what the permission compiler evaluates, and their components.

``dispatch_graph`` is the dependency graph of a policy as the compiler
dispatches it (``rebac.compile.program`` adds its recursive components);
``live_types`` reads it backwards from the relations backed by model columns.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from .ast import (
    BUILTIN_ACTOR_TYPES,
    AttributeBinding,
    ConstBinding,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermExpr,
    PermRef,
    Schema,
)


def strongly_connected_components[K: Any](
    edges: Mapping[K, Iterable[K]],
) -> tuple[tuple[K, ...], ...]:
    """Tarjan components in dependency order; absent targets terminate edges.

    Nodes must be sortable and hashable. Explicit DFS frames avoid Python's
    recursion limit for long chains of permissions or definitions.
    """
    indices: dict[K, int] = {}
    low: dict[K, int] = {}
    stack: list[K] = []
    on_stack: set[K] = set()
    result: list[tuple[K, ...]] = []

    def enter(key: K) -> tuple[K, Iterator[K]]:
        indices[key] = low[key] = len(indices)
        stack.append(key)
        on_stack.add(key)
        return key, iter(sorted(edges[key]))

    for root in sorted(edges):
        if root in indices:
            continue
        frames = [enter(root)]
        while frames:
            key, children = frames[-1]
            for dep in children:
                if dep not in edges:
                    continue
                if dep not in indices:
                    frames.append(enter(dep))
                    break
                if dep in on_stack:
                    low[key] = min(low[key], indices[dep])
            else:
                frames.pop()
                if low[key] == indices[key]:
                    members = []
                    while True:
                        member = stack.pop()
                        on_stack.remove(member)
                        members.append(member)
                        if member == key:
                            break
                    result.append(tuple(sorted(members)))
                if frames:
                    parent = frames[-1][0]
                    low[parent] = min(low[parent], low[key])
    return tuple(result)


type Key = tuple[str, str]


@dataclass(frozen=True, slots=True)
class Dependency:
    target: Key
    negative: bool = False
    traverses: bool = False


def _expr_dependencies(
    schema: Schema, type_: str, expr: PermExpr, negative: bool = False
) -> list[Dependency]:
    if isinstance(expr, PermRef):
        if expr.name in BUILTIN_ACTOR_TYPES:
            return []
        return [Dependency((type_, expr.name), negative)]
    if isinstance(expr, PermArrow):
        definition = schema.get_definition(type_)
        relation = (
            next((r for r in definition.relations if r.name == expr.via), None)
            if definition
            else None
        )
        if relation is None:
            return []
        # Several allowed subject shapes can dispatch this one arrow to the
        # same definition.  They are alternatives, not multiple recursive
        # uses of the arrow in the expression.
        return list(
            dict.fromkeys(
                Dependency((allowed.type, expr.target), negative, traverses=True)
                for allowed in relation.allowed_subjects
            )
        )
    if isinstance(expr, PermBinOp):
        return _expr_dependencies(schema, type_, expr.left, negative) + _expr_dependencies(
            schema, type_, expr.right, negative ^ (expr.op == "-")
        )
    return []


def _arrow_vias(type_: str, expr: PermExpr) -> list[Key]:
    if isinstance(expr, PermArrow):
        return [(type_, expr.via)]
    if isinstance(expr, PermBinOp):
        return _arrow_vias(type_, expr.left) + _arrow_vias(type_, expr.right)
    return []


def dispatch_graph(
    schema: Schema,
) -> tuple[dict[Key, tuple[Dependency, ...]], dict[Key, tuple[Key, ...]]]:
    """What the compiler evaluates to decide each relation and permission.

    A relation dispatches to the relations of its subject sets; a permission
    to the relations and permissions of its type that it names, to its
    arrows' targets on each subject type, and (``via``) to the relations its
    arrows follow. A subject without a relation is compared by its id: the
    compiler evaluates nothing of its type.
    """
    dependencies: dict[Key, tuple[Dependency, ...]] = {}
    via_dependencies: dict[Key, tuple[Key, ...]] = {}
    for definition in schema.definitions:
        type_ = definition.resource_type
        for relation in definition.relations:
            dependencies[(type_, relation.name)] = tuple(
                dict.fromkeys(
                    Dependency((allowed.type, allowed.relation), traverses=True)
                    for allowed in relation.allowed_subjects
                    if allowed.relation
                )
            )
        for permission in definition.permissions:
            dependencies[(type_, permission.name)] = tuple(
                _expr_dependencies(schema, type_, permission.expression)
            )
            via_dependencies[(type_, permission.name)] = tuple(
                _arrow_vias(type_, permission.expression)
            )
    return dependencies, via_dependencies


def live_types(schema: Schema) -> frozenset[str]:
    """The types of the relations and permissions from which ``dispatch_graph``
    reaches a field- or attribute-backed relation or a constant with filters
    (``rebac.schema.live_backed_resource_types``).
    """
    dependencies, via_dependencies = dispatch_graph(schema)
    readers: dict[Key, set[Key]] = {}
    for key, deps in dependencies.items():
        for dep in deps:
            readers.setdefault(dep.target, set()).add(key)
    for key, vias in via_dependencies.items():
        for via in vias:
            readers.setdefault(via, set()).add(key)
    pending = [
        (definition.resource_type, relation.name)
        for definition in schema.definitions
        for relation in definition.relations
        if isinstance(relation.backing, (FieldBinding, AttributeBinding))
        or (isinstance(relation.backing, ConstBinding) and relation.backing.filters)
    ]
    live = set(pending)
    while pending:
        for reader in readers.get(pending.pop(), ()):
            if reader not in live:
                live.add(reader)
                pending.append(reader)
    return frozenset(type_ for type_, _name in live)
