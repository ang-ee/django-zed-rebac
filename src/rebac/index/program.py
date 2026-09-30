"""Immutable schema-sized plans for the permission index."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cached_property
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, cast

from django.core import checks
from django.core.exceptions import FieldDoesNotExist
from django.db import connections, models

from .._id import model_identity_fields, resource_id_attr
from ..composition import TaggedComposition, compose, compose_tagged
from ..conf import app_settings
from ..errors import SchemaError
from ..field_backing import (
    _relation_path,
    resolve_attribute_backing,
    resolve_const_backing,
    resolve_field_backing,
)
from ..resources import model_for_resource_type, model_for_subject_type
from ..schema.ast import (
    BUILTIN_ACTOR_TYPES,
    AttributeBinding,
    ConstBinding,
    Definition,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermExpr,
    Permission,
    PermNil,
    PermRef,
    Schema,
)
from ..schema.graph import strongly_connected_components

if TYPE_CHECKING:
    from ..backends.local import LocalBackend
    from ..models import SchemaOverride

Key = tuple[str, str]
# Bump when derivation changes the rows a program produces; an index derived
# under another format must be rebuilt.
INDEX_FORMAT = 1


@dataclass(frozen=True)
class NodeSpec:
    type: str
    name: str
    kind: Literal["relation", "mono", "and", "minus"]
    expr: PermExpr | None
    operands: tuple[str, str] | None
    deps: frozenset[Key]
    holder_deps: frozenset[Key]
    stratum: int
    recursive: bool
    deadline: datetime | None = None
    override: str = ""


@dataclass(frozen=True)
class WatchSpec:
    model_label: str
    fields: frozenset[str]
    resource_types: frozenset[str]
    is_mixin: bool
    model: type[models.Model]


@dataclass(frozen=True)
class IndexProgram:
    revision: str
    nodes: Mapping[Key, NodeSpec]
    strata: tuple[tuple[Key, ...], ...]
    watched: Mapping[str, WatchSpec]
    userset_relations: frozenset[Key]
    baseline: Schema
    overrides: tuple[SchemaOverride, ...] = ()

    @cached_property
    def digest(self) -> str:
        """Identifies the rows this program derives and the reads it compiles."""
        from ..schema.serialization import digest

        return digest(
            {
                "format": INDEX_FORMAT,
                "nodes": [
                    [
                        node.type,
                        node.name,
                        node.kind,
                        repr(node.expr),
                        node.operands,
                        node.deadline,
                        node.override,
                    ]
                    for _, node in sorted(self.nodes.items())
                ],
                "usersets": sorted(self.userset_relations),
            }
        )[:32]

    @cached_property
    def type_level_nodes(self) -> frozenset[Key]:
        constants = {
            (definition.resource_type, relation.name)
            for definition in self.baseline.definitions
            for relation in definition.relations
            if isinstance(relation.backing, ConstBinding) and not relation.backing.filters
        }
        possible = set(constants)

        def can_emit(type_: str, expr: PermExpr) -> bool:
            if isinstance(expr, PermRef):
                return expr.name in BUILTIN_ACTOR_TYPES or (type_, expr.name) in possible
            if isinstance(expr, PermArrow):
                return (type_, expr.via) in constants
            if isinstance(expr, PermBinOp):
                return can_emit(type_, expr.left) or can_emit(type_, expr.right)
            return False

        while True:
            found = {
                key
                for key, node in self.nodes.items()
                if (
                    node.kind == "mono" and node.expr is not None and can_emit(node.type, node.expr)
                )
                or (
                    node.kind in {"and", "minus"}
                    and node.operands is not None
                    and (node.type, node.operands[0]) in possible
                )
            }
            if found <= possible:
                return frozenset(possible)
            possible.update(found)

    @cached_property
    def userset_only_relations(self) -> frozenset[Key]:
        """Userset relations no node reads: they have no grant rows.

        A permission that names the relation, an arrow that targets it and a
        site that takes it as an operand all read its rows. Without any of
        them the relation is read from the membership closure, and a write
        to it changes no grant.
        """
        referenced: set[Key] = set()
        for node in self.nodes.values():
            referenced.update(node.holder_deps)
            if node.operands is not None:
                referenced.update((node.type, operand) for operand in node.operands)
        return self.userset_relations - referenced

    @cached_property
    def arrow_vias(self) -> frozenset[Key]:
        """The relations whose edges an arrow follows."""

        def vias(expr: PermExpr | None) -> set[str]:
            if isinstance(expr, PermArrow):
                return {expr.via}
            if isinstance(expr, PermBinOp):
                return vias(expr.left) | vias(expr.right)
            return set()

        return frozenset(
            (node.type, via) for node in self.nodes.values() for via in vias(node.expr)
        )

    @cached_property
    def recaveated(self) -> frozenset[str]:
        """Caveats an override can redefine, whatever its deadline."""
        from ..composition import recaveat_targets

        return recaveat_targets(self.overrides)

    @cached_property
    def conditional_nodes(self) -> frozenset[Key]:
        conditional = {
            (definition.resource_type, relation.name)
            for definition in self.baseline.definitions
            for relation in definition.relations
            if any(subject.with_caveat for subject in relation.allowed_subjects)
        }
        while True:
            added = {key for key, node in self.nodes.items() if node.deps & conditional}
            if added <= conditional:
                return frozenset(conditional)
            conditional.update(added)

    @cached_property
    def reverse(self) -> Mapping[Key, frozenset[Key]]:
        reverse: dict[Key, set[Key]] = {}
        for key, node in self.nodes.items():
            for dep in sorted(node.deps):
                reverse.setdefault(dep, set()).add(key)
        return MappingProxyType({key: frozenset(value) for key, value in sorted(reverse.items())})

    def dependents(self, keys: Iterable[Key]) -> frozenset[Key]:
        seen = set(keys)
        pending = sorted(seen)
        while pending:
            for dependent in sorted(self.reverse.get(pending.pop(), ())):
                if dependent not in seen:
                    seen.add(dependent)
                    pending.append(dependent)
        return frozenset(seen)

    @cached_property
    def _held(self) -> Mapping[Key, frozenset[Key]]:
        held: dict[Key, set[Key]] = {key: set() for key in self.nodes}
        while True:
            changed = False
            for key, node in self.nodes.items():
                if node.kind != "mono":
                    continue
                found = set(held[key])
                for dep in node.holder_deps:
                    target = self.nodes.get(dep)
                    if target is None:
                        continue
                    if target.kind in {"and", "minus"}:
                        found.add(dep)
                    else:
                        found.update(held[dep])
                if found != held[key]:
                    held[key] = found
                    changed = True
            if not changed:
                return MappingProxyType(
                    {key: frozenset(value) for key, value in sorted(held.items())}
                )

    def held_sites(self, key: Key) -> frozenset[Key]:
        return self._held.get(key, frozenset())

    def lookups(self, key: Key) -> int:
        memo: dict[Key, int] = {}

        def count(node_key: Key) -> int:
            if node_key not in memo:
                total = 1
                for site in sorted(self.held_sites(node_key)):
                    spec = self.nodes[site]
                    assert spec.operands is not None
                    total += count((spec.type, spec.operands[0]))
                    total += count((spec.type, spec.operands[1]))
                memo[node_key] = total
            return memo[node_key]

        return count(key)

    def schema_at(self, now: datetime) -> Schema:
        active = (row for row in self.overrides if row.expires_at is None or row.expires_at > now)
        return compose(self.baseline, active)


def _expr_deps(expr: PermExpr, definition: Definition) -> frozenset[Key]:
    type_ = definition.resource_type
    if isinstance(expr, PermRef):
        return frozenset() if expr.name in BUILTIN_ACTOR_TYPES else frozenset({(type_, expr.name)})
    if isinstance(expr, PermArrow):
        relation = next((r for r in definition.relations if r.name == expr.via), None)
        return frozenset(
            {(type_, expr.via)}
            | ({(s.type, expr.target) for s in relation.allowed_subjects} if relation else set())
        )
    if isinstance(expr, PermBinOp):
        return _expr_deps(expr.left, definition) | _expr_deps(expr.right, definition)
    return frozenset()


def _holder_deps(expr: PermExpr, definition: Definition) -> frozenset[Key]:
    type_ = definition.resource_type
    if isinstance(expr, PermRef):
        return frozenset() if expr.name in BUILTIN_ACTOR_TYPES else frozenset({(type_, expr.name)})
    if isinstance(expr, PermArrow):
        relation = next((r for r in definition.relations if r.name == expr.via), None)
        return (
            frozenset((s.type, expr.target) for s in relation.allowed_subjects)
            if relation
            else frozenset()
        )
    if isinstance(expr, PermBinOp):
        return _holder_deps(expr.left, definition) | _holder_deps(expr.right, definition)
    return frozenset()


def _fold_nil(expr: PermExpr, tags: TaggedComposition | None) -> PermExpr:
    if not isinstance(expr, PermBinOp):
        return expr
    left = _fold_nil(expr.left, tags)
    right = _fold_nil(expr.right, tags)
    timed_site = (
        tags is not None
        and (tag := tags.sites.get(id(expr))) is not None
        and tag.deadline is not None
    )
    result: PermExpr
    if timed_site:
        result = (
            expr if left is expr.left and right is expr.right else PermBinOp(expr.op, left, right)
        )
    elif expr.op == "-" and isinstance(right, PermNil):
        result = left
    elif expr.op in {"&", "-"} and isinstance(left, PermNil):
        result = PermNil()
    elif expr.op == "&" and isinstance(right, PermNil):
        result = PermNil()
    elif expr.op == "+" and isinstance(left, PermNil):
        result = right
    elif expr.op == "+" and isinstance(right, PermNil):
        result = left
    else:
        result = (
            expr if left is expr.left and right is expr.right else PermBinOp(expr.op, left, right)
        )
    if tags is not None:
        if tag := tags.arms.get(id(expr)):
            tags.arms[id(result)] = tag
        if tag := tags.sites.get(id(expr)):
            if isinstance(result, PermBinOp) and result.op in {"&", "-"}:
                tags.sites[id(result)] = tag
    return result


def _internal(permission: str, *content: str) -> str:
    """Name an internal node by what it means, so the name outlives renumbering."""
    from ..schema.serialization import digest

    return f"{permission}.{digest(content)[:12]}"


def _compile_permission(
    nodes: dict[Key, NodeSpec],
    definition: Definition,
    permission: Permission,
    tags: TaggedComposition | None,
) -> None:
    """Add the permission, its sites and their operands, children first."""
    type_ = definition.resource_type
    arms = tags.arms if tags is not None else {}
    sites = tags.sites if tags is not None else {}

    def mono(expr: PermExpr, name: str | None = None) -> str:
        lowered = lower(expr, root=True)
        tag = arms.get(id(expr))
        if name is None:
            name = _internal(permission.name, "mono", repr(lowered), tag.override if tag else "")
        nodes[type_, name] = NodeSpec(
            type_,
            name,
            "mono",
            lowered,
            None,
            _expr_deps(lowered, definition),
            _holder_deps(lowered, definition),
            -1,
            False,
            tag.deadline if tag else None,
            tag.override if tag else "",
        )
        return name

    def operand(expr: PermExpr) -> str:
        if (
            isinstance(expr, PermRef)
            and expr.name not in BUILTIN_ACTOR_TYPES
            and id(expr) not in arms
        ):
            return expr.name
        return mono(expr)

    def site(expr: PermBinOp) -> str:
        left, right = operand(expr.left), operand(expr.right)
        tag = sites.get(id(expr))
        name = _internal(permission.name, expr.op, left, right, tag.override if tag else "")
        nodes[type_, name] = NodeSpec(
            type_,
            name,
            "and" if expr.op == "&" else "minus",
            None,
            (left, right),
            frozenset({(type_, left), (type_, right)}),
            frozenset(),
            -1,
            False,
            tag.deadline if tag else None,
            tag.override if tag else "",
        )
        return name

    def lower(expr: PermExpr, *, root: bool = False) -> PermExpr:
        if not root and id(expr) in arms:
            return PermRef(operand(expr))
        if isinstance(expr, PermBinOp):
            if expr.op in {"&", "-"}:
                return PermRef(site(expr))
            return PermBinOp("+", lower(expr.left), lower(expr.right))
        return expr

    mono(_fold_nil(permission.expression, tags), permission.name)


def _derivation(nodes: Mapping[Key, NodeSpec]) -> dict[Key, frozenset[Key]]:
    """What a node's rows are derived from: references, arrows and operands.

    A relation's rows come from its edges. Its subject sets are held by
    reference, so they are dependencies of maintenance and of conditions, not
    of derivation.
    """
    return {
        key: frozenset() if node.kind == "relation" else node.deps for key, node in nodes.items()
    }


def _compile_nodes(
    schema: Schema, tags: TaggedComposition | None = None
) -> tuple[Mapping[Key, NodeSpec], tuple[tuple[Key, ...], ...]]:
    nodes: dict[Key, NodeSpec] = {}
    for definition in sorted(schema.definitions, key=lambda d: d.resource_type):
        type_ = definition.resource_type
        for relation in sorted(definition.relations, key=lambda r: r.name):
            deps = frozenset((s.type, s.relation) for s in relation.allowed_subjects if s.relation)
            nodes[type_, relation.name] = NodeSpec(
                type_, relation.name, "relation", None, None, deps, frozenset(), -1, False
            )
        for permission in sorted(definition.permissions, key=lambda p: p.name):
            _compile_permission(nodes, definition, permission, tags)
    strata = strongly_connected_components({key: node.deps for key, node in nodes.items()})
    for number, stratum in enumerate(strata):
        recursive = len(stratum) > 1 or any(key in nodes[key].deps for key in stratum)
        for key in stratum:
            nodes[key] = replace(nodes[key], stratum=number, recursive=recursive)
    return MappingProxyType(dict(sorted(nodes.items()))), strata


def _cycle(
    graph: Mapping[Key, frozenset[Key]], component: tuple[Key, ...], start: Key
) -> list[Key]:
    allowed = set(component)
    pending = [(start, [start])]
    seen = {start}
    while pending:
        key, path = pending.pop()
        for dep in sorted(graph[key] & allowed, reverse=True):
            if dep == start:
                return [*path, start]
            if dep not in seen:
                seen.add(dep)
                pending.append((dep, [*path, dep]))
    raise AssertionError("recursive SCC has no cycle through its member")


def program_errors(
    schema: Schema, tags: TaggedComposition | None = None
) -> list[checks.CheckMessage]:
    nodes, strata = _compile_nodes(schema, tags)
    errors: list[checks.CheckMessage] = []
    for type_, name in sorted(nodes):
        if len(name) > 64:
            errors.append(
                checks.Error(
                    f"Permission-index node {type_}#{name} exceeds the 64-character column limit.",
                    hint="Shorten the permission name; an internal node adds 13 characters.",
                    id="rebac.E016",
                )
            )
    graph = _derivation(nodes)
    for component in strongly_connected_components(graph):
        cyclic = len(component) > 1 or any(key in graph[key] for key in component)
        offending = [k for k in component if cyclic and nodes[k].kind in {"and", "minus"}]
        if offending:
            key = offending[0]
            cycle = " -> ".join(f"{t}#{n}" for t, n in _cycle(graph, component, key))
            origin = f" introduced by override {nodes[key].override}" if nodes[key].override else ""
            errors.append(
                checks.Error(
                    f"Intersection or exclusion in recursive permission cycle: {cycle}{origin}. "
                    "This is a deliberate LocalBackend divergence in 0.23.0 (D1).",
                    hint="Move &/- outside the recursive component.",
                    id="rebac.E016",
                )
            )
    if not errors:
        temporary = IndexProgram("", nodes, strata, MappingProxyType({}), frozenset(), schema)
        limit = app_settings.REBAC_INDEX_LOOKUP_LIMIT
        if type(limit) is int and limit > 0:
            for definition in schema.definitions:
                for permission in definition.permissions:
                    key = (definition.resource_type, permission.name)
                    count = temporary.lookups(key)
                    if count > limit:
                        plan = ", ".join(f"{t}#{n}" for t, n in sorted(temporary.held_sites(key)))
                        errors.append(
                            checks.Error(
                                f"Permission-index read plan {key[0]}#{key[1]} has {count} "
                                f"lookups (limit {limit}); sites: {plan}",
                                id="rebac.E019",
                            )
                        )
    return errors


class _Watches:
    def __init__(self) -> None:
        self.fields: dict[str, set[str]] = {}
        self.types: dict[str, set[str]] = {}
        self.models: dict[str, type[models.Model]] = {}

    def model(self, model: type[models.Model], resource_type: str) -> None:
        label = model._meta.label_lower
        self.models[label] = model
        self.fields.setdefault(label, set())
        self.types.setdefault(label, set()).add(resource_type)
        pk = model._meta.pk
        if pk is not None:
            self.field(model, pk, resource_type)
        for parent, link in sorted(
            model._meta.parents.items(), key=lambda pair: pair[0]._meta.label_lower
        ):
            self.model(parent, resource_type)
            if link is not None:
                self.field(model, link, resource_type)

    def field(self, model: type[models.Model], field: Any, resource_type: str) -> None:
        for owner in (model, getattr(field, "model", model)):
            label = owner._meta.label_lower
            self.models[label] = owner
            self.types.setdefault(label, set()).add(resource_type)
            names = self.fields.setdefault(label, set())
            names.update(
                value
                for attr in ("name", "attname", "column")
                if (value := getattr(field, attr, None))
            )

    def identity(self, model: type[models.Model], attr: str, resource_type: str) -> None:
        self.model(model, resource_type)
        try:
            field, _ = model_identity_fields(model, attr)
        except ValueError, FieldDoesNotExist:
            return  # E014 reports the unsupported identity separately.
        self.field(model, field, resource_type)

    def path(self, model: type[models.Model], path: str, resource_type: str) -> None:
        self.model(model, resource_type)

        def visit(model: type[models.Model], field: Any, prefix: str) -> None:
            self.field(model, field, resource_type)
            if not field.is_relation:
                return
            # Django resolves forward, reverse, M2M, and MTI joins here, including
            # the through model and to_field columns. No parallel path resolver.
            for info in getattr(field, "path_infos", ()):
                for options in (info.from_opts, info.to_opts):
                    self.model(options.model, resource_type)
                join = info.join_field
                forward = getattr(join, "field", join)
                for left, right in getattr(forward, "related_fields", ()):
                    self.field(left.model, left, resource_type)
                    self.field(right.model, right, resource_type)
            target = getattr(field, "related_model", None)
            if not isinstance(target, type) or not issubclass(target, models.Model):
                return
            # GenericRelation's object-id join also depends on content_type.
            for attr in ("object_id_field_name", "content_type_field_name"):
                name = getattr(field, attr, None)
                if name:
                    self.field(target, target._meta.get_field(name), resource_type)
            self.model(target, resource_type)

        _relation_path(model, path, lookup=True, visit=visit)

    def freeze(self) -> Mapping[str, WatchSpec]:
        from ..mixins import RebacTrackedMixin

        return MappingProxyType(
            {
                label: WatchSpec(
                    label,
                    frozenset(self.fields[label]),
                    frozenset(self.types[label]),
                    issubclass(self.models[label], RebacTrackedMixin),
                    self.models[label],
                )
                for label in sorted(self.models)
            }
        )


def watched_for(schema: Schema) -> Mapping[str, WatchSpec]:
    """Resolve every backing's mutable columns without querying model rows."""
    watches = _Watches()
    for definition in sorted(schema.definitions, key=lambda d: d.resource_type):
        type_ = definition.resource_type
        for relation in sorted(definition.relations, key=lambda r: r.name):
            field = resolve_field_backing(definition, relation)
            if field is not None:
                watches.identity(field.source_model, field.source_id_attr, type_)
                watches.identity(field.target_model, field.target_id_attr, type_)
                watches.path(field.source_model, field.path, type_)
                for lookup in sorted(field.filters):
                    watches.path(field.source_model, lookup, type_)
            attribute = resolve_attribute_backing(definition, relation)
            if attribute is not None:
                watches.identity(attribute.target_model, attribute.target_id_attr, type_)
                watches.path(attribute.target_model, attribute.field.name, type_)
                for lookup in sorted(attribute.filters):
                    watches.path(attribute.target_model, lookup, type_)
            const = resolve_const_backing(definition, relation)
            if const is not None:
                watches.identity(const.source_model, resource_id_attr(const.source_model), type_)
                for lookup in sorted(const.filters):
                    watches.path(const.source_model, lookup, type_)
    return watches.freeze()


def codec_fields(schema: Schema) -> tuple[tuple[type[models.Model], str], ...]:
    """Identity/attribute columns requiring codecs, excluding fixed anchors."""
    result: dict[tuple[str, str], tuple[type[models.Model], str]] = {}

    def add(model: type[models.Model], attr: str) -> None:
        result[model._meta.label_lower, attr] = (model, attr)

    for definition in sorted(schema.definitions, key=lambda d: d.resource_type):
        model = model_for_resource_type(definition.resource_type)
        if model is not None:
            add(model, resource_id_attr(model))
        for relation in definition.relations:
            if isinstance(relation.backing, (FieldBinding, AttributeBinding)):
                for allowed in relation.allowed_subjects:
                    target = model_for_subject_type(allowed.type)
                    if target is not None:
                        add(*target)
            field = resolve_field_backing(definition, relation)
            if field is not None:
                add(field.source_model, field.source_id_attr)
                add(field.target_model, field.target_id_attr)
            attribute = resolve_attribute_backing(definition, relation)
            if attribute is not None:
                add(attribute.target_model, attribute.target_id_attr)
                if (
                    isinstance(relation.backing, AttributeBinding)
                    and relation.backing.resource is None
                ):
                    add(attribute.target_model, attribute.field.name)
    return tuple(result[key] for key in sorted(result))


def _usersets(schema: Schema) -> frozenset[Key]:
    # Includes backed target relations, not just tuple-backed membership.
    return frozenset(
        (s.type, s.relation)
        for d in schema.definitions
        for r in d.relations
        for s in r.allowed_subjects
        if s.relation
    )


def program_for(backend: LocalBackend, *, using: str, now: datetime | None = None) -> IndexProgram:
    from ..models import SchemaOverride

    connection = connections[using]
    snapshot = None
    if not backend._schema_is_manual:
        from .read import pinned_snapshot

        snapshot = pinned_snapshot(using)
        if snapshot is not None and snapshot.revision:
            with backend._schema_lock:
                facts = backend._schema_facts_memo.get(snapshot.generation)
                if facts is not None:
                    for key, entry in facts.programs.items():
                        if key[0] == using and key[1] == snapshot.revision:
                            return entry
    for _attempt in range(3):
        baseline: Schema | None = None
        manual = backend._schema_is_manual
        pinned = snapshot is not None and snapshot.baseline is not None and bool(snapshot.revision)
        if manual:
            baseline = backend.schema()
            revision = backend._manual_schema_revision()
            overrides: list[SchemaOverride] = []
        elif pinned:
            assert snapshot is not None and snapshot.baseline is not None
            baseline = snapshot.baseline
            revision = cast(str, snapshot.revision)
            overrides = cast(list[SchemaOverride], list(snapshot.overrides))
        else:
            loaded_revision = backend._read_schema_revision(connection)
            if loaded_revision is None:
                raise SchemaError(
                    "Permission-index program requires a schema revision; run rebac sync"
                )
            revision = loaded_revision
            overrides = list(
                SchemaOverride.objects.using(using)
                .select_related("target_ct")
                .order_by("kind", "created_at", "pk")
            )
        signature = tuple(
            (
                r.pk,
                r.kind,
                cast(int, cast(Any, r).target_ct_id),
                r.target_pk,
                r.expression,
                r.created_at,
                r.expires_at,
            )
            for r in overrides
        )
        transactional = connection.in_atomic_block
        if not manual and not pinned and transactional:
            baseline, _deadline = backend._load_schema_from_db(using, overrides=())
        fingerprint = repr(baseline) if transactional else None
        key = (using, revision, signature, fingerprint)
        with backend._schema_lock:
            generation = (
                snapshot.generation
                if pinned and snapshot is not None
                else backend._schema_generation
            )
            facts = backend._schema_facts_memo.get(generation)
            cached = facts.programs.get(key) if facts is not None else None
        if cached is not None:
            if manual or pinned or backend._read_schema_revision(connection) == revision:
                return replace(cached) if transactional else cached
            continue
        if not manual and not pinned and not transactional:
            baseline, _deadline = backend._load_schema_from_db(using, overrides=())
        assert baseline is not None
        tagged = compose_tagged(baseline, overrides)
        errors = program_errors(tagged.schema, tagged)
        if errors:
            raise SchemaError("; ".join(str(error) for error in errors))
        nodes, strata = _compile_nodes(tagged.schema, tagged)
        program = IndexProgram(
            revision,
            nodes,
            strata,
            watched_for(baseline),
            _usersets(baseline),
            baseline,
            tuple(overrides),
        )
        if not manual and not pinned and backend._read_schema_revision(connection) != revision:
            continue
        from ..backends.local import _SchemaFacts

        with backend._schema_lock:
            generation = (
                snapshot.generation
                if pinned and snapshot is not None
                else backend._schema_generation
            )
            current = manual or (
                generation != -1
                and generation == backend._schema_generation
                and (not pinned or (using, revision) in backend._schema_snapshots)
            )
            if current:
                facts = backend._schema_facts_memo.setdefault(generation, _SchemaFacts(generation))
                facts.programs[key] = program
        return program
    raise SchemaError("Schema changed repeatedly while compiling the permission-index program")
