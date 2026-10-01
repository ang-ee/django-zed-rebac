"""The dependency graph of a policy, as the permission compiler dispatches it.

The graph is about authorization dispatch, not model rows.  It includes
subject-set membership and arrow targets, so that a recursive component is
the set of nodes one statement may have to unroll together.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core import checks

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
    stored_sets: frozenset[Key]

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
                            "contains an exclusion dependency (rebac.E016)."
                        )
                    if len(internal) > 1:
                        raise SchemaError(
                            f"Recursive permission component {sorted(component)!r} is nonlinear (rebac.E016)."
                        )
                (recursive if traverses else alias_cycles).update(members)
        return cls(
            schema,
            dependencies,
            via_dependencies,
            components,
            frozenset(recursive),
            frozenset(alias_cycles),
            _stored_sets(schema),
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


def _stored_sets(schema: Schema) -> frozenset[Key]:
    """The relations used as subject sets whose membership tuples alone decide.

    A relation qualifies when it is stored and every subject set it admits
    qualifies too: the sets an actor belongs to are then a closure over the
    tuple table, with no model column involved.
    """
    relations = {
        (definition.resource_type, relation.name): relation
        for definition in schema.definitions
        for relation in definition.relations
    }
    stored = {
        (allowed.type, allowed.relation)
        for relation in relations.values()
        for allowed in relation.allowed_subjects
        if allowed.relation
    }
    stored = {key for key in stored if key in relations and relations[key].backing is None}
    while True:
        dropped = {
            key
            for key in stored
            if any(
                allowed.relation
                and (allowed.type, allowed.relation) in relations
                and (allowed.type, allowed.relation) not in stored
                for allowed in relations[key].allowed_subjects
            )
        }
        if not dropped:
            return frozenset(stored)
        stored -= dropped


def program_errors(schema: Schema) -> list[checks.CheckMessage]:
    """What makes the compiler refuse ``schema``, as system-check messages."""
    try:
        CompileProgram.build(schema)
    except SchemaError as exc:
        return [
            checks.Error(
                str(exc),
                hint="Keep exclusions outside a recursive component, and use the "
                "recursive permission once in each expression of the component.",
                id="rebac.E016",
            )
        ]
    return []
