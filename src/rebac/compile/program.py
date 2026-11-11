"""The recursive components of a policy, validated for the permission compiler.

The dependency graph is ``rebac.schema.graph.dispatch_graph``: authorization
dispatch, not model rows, including subject-set membership and arrow targets.
A recursive component is the set of nodes one statement may have to unroll
together.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.core import checks

from ..composition import TaggedComposition
from ..errors import SchemaError
from ..field_backing import resolve_field_backing
from ..schema.ast import ConstBinding, PermArrow, PermBinOp, PermExpr, PermRef, Schema
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
    tagged: TaggedComposition | None = None
    _inherited: dict[Key, tuple[Key, ...]] = field(default_factory=dict, compare=False)
    _named_ids: dict[str, frozenset[str]] = field(default_factory=dict, compare=False)

    @classmethod
    def build(cls, schema: Schema, *, tagged: TaggedComposition | None = None) -> CompileProgram:
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
            tagged,
        )

    def inherited_targets(self, root: Key) -> tuple[Key, ...]:
        """Positive single-valued field-arrow reach, derived once per program/root.

        Discovery is metadata only. An invalid backing is left for the actual
        compiler traversal, whose relation owner raises the contracted E009.
        Intersections, exclusions, tagged subtrees and recursion are boundaries.
        """
        if root in self._inherited:
            return self._inherited[root]
        found: set[Key] = set()
        seen: set[Key] = set()

        def expression(key: Key, expr: PermExpr) -> None:
            if self.tagged is not None and (
                id(expr) in self.tagged.arms or id(expr) in self.tagged.sites
            ):
                return
            if isinstance(expr, PermBinOp) and expr.op == "+":
                expression(key, expr.left)
                expression(key, expr.right)
            elif isinstance(expr, PermRef):
                visit((key[0], expr.name))
            elif isinstance(expr, PermArrow):
                definition = self.schema.get_definition(key[0])
                assert definition is not None
                relation = next((r for r in definition.relations if r.name == expr.via), None)
                resolved = resolve_field_backing(definition, relation) if relation else None
                if resolved is None or not resolved.single_valued:
                    return
                target = (resolved.target_resource_type, expr.target)
                if target not in self.recursive:
                    found.add(target)
                    visit(target)

        def visit(key: Key) -> None:
            if key in seen or key in self.recursive:
                return
            seen.add(key)
            permission = self.schema.get_permission(*key)
            if permission is not None:
                expression(key, permission.expression)

        visit(root)
        result = self._inherited[root] = tuple(sorted(found))
        return result

    def named_ids(self, type_: str) -> frozenset[str]:
        """The ids of ``type_`` the policy names, derived once per program/type.

        An allowed subject with an id, or a constant binding's target.
        """
        found = self._named_ids.get(type_)
        if found is not None:
            return found
        names: set[str] = set()
        for definition in self.schema.definitions:
            for relation in definition.relations:
                for allowed in relation.allowed_subjects:
                    if allowed.type != type_:
                        continue
                    if allowed.id:
                        names.add(allowed.id)
                    if isinstance(relation.backing, ConstBinding):
                        names.add(relation.backing.target_id)
        found = self._named_ids[type_] = frozenset(names)
        return found

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
