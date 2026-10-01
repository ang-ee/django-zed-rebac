"""The dependency graph used by the live permission compiler.

The graph is deliberately about authorization dispatch, not model rows.  It
includes subject-set membership and arrow targets, which the old index plan
could leave out because its membership closure was materialized separately.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import SchemaError
from ..schema.ast import (
    BUILTIN_ACTOR_TYPES,
    PermArrow,
    PermBinOp,
    PermExpr,
    PermRef,
    Schema,
)
from ..schema.graph import strongly_connected_components

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


@dataclass(frozen=True, slots=True)
class CompileProgram:
    """Validated, schema-sized dependencies and recursive components."""

    schema: Schema
    dependencies: dict[Key, tuple[Dependency, ...]]
    via_dependencies: dict[Key, tuple[Key, ...]]
    components: dict[Key, frozenset[Key]]
    recursive: frozenset[Key]
    alias_cycles: frozenset[Key]

    @classmethod
    def build(cls, schema: Schema) -> CompileProgram:
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

        graph = {
            key: {dep.target for dep in deps if dep.target in dependencies}
            for key, deps in dependencies.items()
        }
        components: dict[Key, frozenset[Key]] = {}
        recursive: set[Key] = set()
        alias_cycles: set[Key] = set()
        for members in strongly_connected_components(graph):
            component = frozenset(members)
            for key in members:
                components[key] = component
            if len(members) > 1 or any(key in graph[key] for key in members):
                traverses = False
                for key in members:
                    internal = [dep for dep in dependencies[key] if dep.target in component]
                    traverses |= any(dep.traverses for dep in internal)
                    if any(dep.negative for dep in internal):
                        raise SchemaError(
                            f"Recursive permission component {sorted(component)!r} "
                            "contains an exclusion dependency."
                        )
                    if len(internal) > 1:
                        raise SchemaError(
                            f"Recursive permission component {sorted(component)!r} is nonlinear."
                        )
                (recursive if traverses else alias_cycles).update(members)
        return cls(
            schema,
            dependencies,
            via_dependencies,
            components,
            frozenset(recursive),
            frozenset(alias_cycles),
        )

    def reachable(self, root: Key) -> frozenset[Key]:
        result: set[Key] = set()
        pending = [root]
        while pending:
            key = pending.pop()
            if key in result:
                continue
            result.add(key)
            pending.extend(dep.target for dep in self.dependencies.get(key, ()))
            pending.extend(self.via_dependencies.get(key, ()))
        return frozenset(result)
