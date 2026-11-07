"""The recursive components of a policy, validated for the permission compiler.

The dependency graph is ``rebac.schema.graph.dispatch_graph``: authorization
dispatch, not model rows, including subject-set membership and arrow targets.
A recursive component is the set of nodes one statement may have to unroll
together.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core import checks

from ..errors import SchemaError
from ..schema.ast import Schema
from ..schema.graph import Dependency, Key, dispatch_graph, strongly_connected_components

__all__ = ["CompileProgram", "Key", "program_errors"]


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
        dependencies, via_dependencies = dispatch_graph(schema)
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
