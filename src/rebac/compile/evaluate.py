"""Residual caveat and depth provenance for an uncertain point check.

This evaluator never authorizes.  The compiled SQL lower bound is the sole
allow authority.  Here we name the unresolved inputs only after SQL reported
``LOWER = false, UPPER = true``.  A reduced Boolean decision diagram keeps
formula instances distinct and removes atoms that cannot affect the answer,
regardless of the order in which schema arms or tuples were read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

from django.utils import timezone

from rebac.actors import is_anonymous_actor
from rebac.caveats import evaluate as evaluate_caveat
from rebac.codec import identity_codec
from rebac.conf import app_settings
from rebac.field_backing import (
    resolve_attribute_backing,
    resolve_const_backing,
    resolve_field_backing,
)
from rebac.models import active_relationship_model
from rebac.schema.ast import (
    AttributeBinding,
    ConstBinding,
    Definition,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermExpr,
    PermNil,
    PermRef,
    Relation,
    Schema,
)
from rebac.schema.walker import (
    find_relation,
    relationship_row_allowed_by_relation,
    tri_and,
    tri_minus,
)
from rebac.types import ObjectRef, SubjectRef

from .program import CompileProgram, Key


class _BDD:
    """Canonical ordered Boolean DAG, with terminals 0 and 1."""

    def __init__(self) -> None:
        self.nodes: list[tuple[int, int, int]] = [(-1, 0, 0), (-1, 1, 1)]
        self.unique: dict[tuple[int, int, int], int] = {}
        self.apply_cache: dict[tuple[str, int, int], int] = {}

    def atom(self, variable: int) -> int:
        return self._node(variable, 0, 1)

    def _node(self, variable: int, low: int, high: int) -> int:
        if low == high:
            return low
        key = variable, low, high
        if key not in self.unique:
            self.unique[key] = len(self.nodes)
            self.nodes.append(key)
        return self.unique[key]

    def apply(self, operator: str, left: int, right: int) -> int:
        if operator == "and":
            if left == 0 or right == 0:
                return 0
            if left == 1:
                return right
            if right == 1:
                return left
        if operator == "or":
            if left == 1 or right == 1:
                return 1
            if left == 0:
                return right
            if right == 0:
                return left
        if left == right:
            return left
        key = operator, min(left, right), max(left, right)
        cached = self.apply_cache.get(key)
        if cached is not None:
            return cached
        lv = self.nodes[left][0] if left > 1 else float("inf")
        rv = self.nodes[right][0] if right > 1 else float("inf")
        variable = int(min(lv, rv))

        def branch(node: int, high: bool) -> int:
            if node <= 1 or self.nodes[node][0] != variable:
                return node
            return self.nodes[node][2 if high else 1]

        low = self.apply(operator, branch(left, False), branch(right, False))
        high = self.apply(operator, branch(left, True), branch(right, True))
        result = self._node(variable, low, high)
        self.apply_cache[key] = result
        return result

    def negate(self, node: int) -> int:
        if node <= 1:
            return 1 - node
        variable, low, high = self.nodes[node]
        return self._node(variable, self.negate(low), self.negate(high))

    def relevant(self, root: int) -> frozenset[int]:
        found: set[int] = set()
        visited: set[int] = set()
        pending = [root]
        while pending:
            node = pending.pop()
            if node <= 1 or node in visited:
                continue
            visited.add(node)
            variable, low, high = self.nodes[node]
            found.add(variable)
            pending.extend((low, high))
        return frozenset(found)


@dataclass(frozen=True, slots=True)
class Residual:
    missing: frozenset[str]
    depth_relevant: bool


@dataclass(frozen=True, slots=True)
class _Edge:
    subject: SubjectRef
    caveat_name: str = ""
    caveat_context: Mapping[str, Any] | None = None
    caveat_key: str = ""


class _Evaluator:
    def __init__(
        self,
        schema: Schema,
        actor: SubjectRef,
        context: Mapping[str, Any] | None,
        using: str,
        now: datetime,
    ) -> None:
        self.schema = schema
        self.actor = actor
        self.context = context
        self.using = using
        self.now = now
        self.diagram = _BDD()
        self.variables: dict[str, int] = {}
        self.missing: dict[int, frozenset[str]] = {}
        self.depth: set[int] = set()
        self.depth_limit = app_settings.REBAC_DEPTH_LIMIT
        self.program = CompileProgram.build(schema)
        self.rows_cache: dict[tuple[ObjectRef, str], tuple[_Edge, ...]] = {}

    def _variable(
        self, identity: str, *, missing: frozenset[str] = frozenset(), depth: bool = False
    ) -> int:
        variable = self.variables.setdefault(identity, len(self.variables))
        if missing:
            self.missing[variable] = missing
        if depth:
            self.depth.add(variable)
        return self.diagram.atom(variable)

    def _condition(self, edge: _Edge) -> int:
        if not edge.caveat_name:
            return 1
        caveat = self.schema.get_caveat(edge.caveat_name)
        if caveat is None:
            return 0
        verdict, names = evaluate_caveat(
            caveat, dict(edge.caveat_context or {}), dict(self.context or {})
        )
        if verdict is not None:
            return int(verdict)
        # The key is an opaque write-owned identity.  Recomputing it from a
        # JSONB round trip can merge distinct original numeric payloads.
        return self._variable(f"caveat:{edge.caveat_key}", missing=frozenset(names))

    def _rows(
        self, resource: ObjectRef, relation: Relation, definition: Definition
    ) -> tuple[_Edge, ...]:
        cache_key = resource, relation.name
        if cache_key in self.rows_cache:
            return self.rows_cache[cache_key]
        result: list[_Edge] = []
        backing = relation.backing
        if isinstance(backing, FieldBinding):
            resolved = resolve_field_backing(definition, relation)
            if resolved is not None:
                codec = identity_codec(resolved.target_model, resolved.target_id_attr)
                target_ids = (
                    resolved.queryset(resource_id=resource.resource_id, using=self.using)
                    .order_by()
                    .values_list(resolved.target_values_path(), flat=True)
                    .distinct()
                )
                for value in target_ids.iterator(chunk_size=1000):
                    if value is not None:
                        allowed = relation.allowed_subjects[0]
                        result.append(
                            _Edge(
                                SubjectRef.of(
                                    resolved.target_resource_type,
                                    codec.wire(value, using=self.using),
                                    allowed.relation,
                                )
                            )
                        )
        elif isinstance(backing, AttributeBinding) and relation.has_backing(resource.resource_id):
            resolved_attribute = resolve_attribute_backing(definition, relation)
            if resolved_attribute is not None:
                for value in resolved_attribute.subject_ids(
                    resource.resource_id, using=self.using
                ).iterator(chunk_size=1000):
                    allowed = relation.allowed_subjects[0]
                    result.append(
                        _Edge(
                            SubjectRef.of(
                                resolved_attribute.target_resource_type,
                                str(value),
                                allowed.relation,
                            )
                        )
                    )
        elif isinstance(backing, ConstBinding):
            resolved_const = resolve_const_backing(definition, relation)
            if resolved_const is not None and resolved_const.matches(
                resource.resource_id, using=self.using
            ):
                allowed = relation.allowed_subjects[0]
                result.append(
                    _Edge(SubjectRef.of(allowed.type, backing.target_id, allowed.relation))
                )

        if not relation.has_backing(resource.resource_id):
            rows = (
                cast(Any, active_relationship_model().objects.using(self.using))
                .for_resource(resource.resource_type, resource.resource_id)
                .filter(relation=relation.name)
                .wire_projection()
                .values(
                    "subject_type",
                    "subject_id",
                    "subject_relation",
                    "caveat_name",
                    "caveat_context",
                    "caveat_key",
                    "expires_at",
                )
            )
            for row in rows.iterator(chunk_size=1000):
                if row["expires_at"] is not None and row["expires_at"] <= self.now:
                    continue
                source = _Edge(
                    SubjectRef.of(row["subject_type"], row["subject_id"], row["subject_relation"]),
                    row["caveat_name"],
                    row["caveat_context"],
                    row["caveat_key"],
                )
                candidate_row = SimpleNamespace(
                    subject_type=source.subject.subject_type,
                    subject_id=source.subject.subject_id,
                    optional_subject_relation=source.subject.optional_relation,
                    expires_at=row["expires_at"],
                    caveat_name=source.caveat_name,
                )
                if relationship_row_allowed_by_relation(relation, candidate_row):
                    result.append(source)
        value = tuple(
            sorted(
                result,
                key=lambda edge: (str(edge.subject), edge.caveat_name, str(edge.caveat_context)),
            )
        )
        self.rows_cache[cache_key] = value
        return value

    def node(
        self,
        resource: ObjectRef,
        name: str,
        seen: frozenset[tuple[ObjectRef, str]],
        visits: Mapping[Key, int],
    ) -> int:
        dispatch = resource.resource_type, name
        count = visits.get(dispatch, 0)
        if dispatch in self.program.recursive and count > self.depth_limit:
            return self._variable(f"depth:{resource}#{name}", depth=True)
        key = resource, name
        if key in seen:
            if dispatch in self.program.recursive:
                # A data cycle can keep the bounded SQL upper branch open.
                # Preserve that uncertainty, including under subtraction.
                return self._variable(f"depth:{resource}#{name}", depth=True)
            return 0
        definition = self.schema.get_definition(resource.resource_type)
        if definition is None:
            return 0
        seen = seen | {key}
        visits = {**visits, dispatch: count + 1}
        permission = self.schema.get_permission(resource.resource_type, name)
        if permission is not None:
            return self.expression(permission.expression, definition, resource, seen, visits)
        relation = find_relation(definition, name)
        if relation is None:
            return 0
        result = 0
        for row in self._rows(resource, relation, definition):
            direct = row.subject == self.actor
            wildcard = (
                not self.actor.optional_relation
                and row.subject.subject_type == self.actor.subject_type
                and row.subject.subject_id == "*"
                and not row.subject.optional_relation
            )
            member = (
                self.node(row.subject.object, row.subject.optional_relation, seen, visits)
                if row.subject.optional_relation
                else 0
            )
            arm = self.diagram.apply(
                "and", self._condition(row), 1 if direct or wildcard else member
            )
            result = self.diagram.apply("or", result, arm)
        return result

    def expression(
        self,
        expr: PermExpr,
        definition: Definition,
        resource: ObjectRef,
        seen: frozenset[tuple[ObjectRef, str]],
        visits: Mapping[Key, int],
    ) -> int:
        if isinstance(expr, PermNil):
            return 0
        if isinstance(expr, PermRef):
            if expr.name == "anonymous":
                return int(is_anonymous_actor(self.actor))
            if expr.name == "authenticated":
                return int(not is_anonymous_actor(self.actor) and bool(self.actor.subject_id))
            return self.node(resource, expr.name, seen, visits)
        if isinstance(expr, PermArrow):
            relation = find_relation(definition, expr.via)
            if relation is None:
                return 0
            result = 0
            for row in self._rows(resource, relation, definition):
                target = self.node(row.subject.object, expr.target, seen, visits)
                arm = self.diagram.apply("and", self._condition(row), target)
                result = self.diagram.apply("or", result, arm)
            return result
        if isinstance(expr, PermBinOp):
            left = self.expression(expr.left, definition, resource, seen, visits)
            right = self.expression(expr.right, definition, resource, seen, visits)
            if expr.op == "+":
                return self.diagram.apply("or", left, right)
            if expr.op == "&":
                return self._set_site(expr, resource, left, right, subtract=False)
            if expr.op == "-":
                return self._set_site(expr, resource, left, right, subtract=True)
        raise TypeError(f"Unsupported permission expression: {expr!r}")

    def _set_site(
        self,
        expr: PermBinOp,
        resource: ObjectRef,
        left: int,
        right: int,
        *,
        subtract: bool,
    ) -> int:
        """Preserve the reference's three-state set-operation boundary.

        Monotone path alternatives share caveat atoms.  A named intersection
        or subtraction first settles its operands as three-state values, then
        contributes one opaque site atom to surrounding alternatives.  Thus
        ``A - A`` with undecided A remains conditional, as the reference does.
        """

        left_value = bool(left) if left <= 1 else None
        right_value = bool(right) if right <= 1 else None
        value = tri_minus(left_value, right_value) if subtract else tri_and(left_value, right_value)
        if value is not None:
            return int(value)
        relevant = self.diagram.relevant(left) | self.diagram.relevant(right)
        missing = frozenset().union(
            *(self.missing.get(variable, frozenset()) for variable in relevant)
        )
        return self._variable(
            f"site:{resource}:{id(expr)}",
            missing=missing,
            depth=bool(relevant & self.depth),
        )

    def residual(self, resource: ObjectRef, action: str) -> Residual:
        root = self.node(resource, action, frozenset(), {})
        relevant = self.diagram.relevant(root)
        return Residual(
            missing=frozenset().union(
                *(self.missing.get(variable, frozenset()) for variable in relevant)
            ),
            depth_relevant=bool(relevant & self.depth),
        )

    def named(
        self,
        resource: ObjectRef,
        name: str,
        candidate: SubjectRef,
        seen: frozenset[tuple[ObjectRef, str]] = frozenset(),
    ) -> bool:
        """Whether a source path names this exact subject.

        Builtins and a wildcard's class match name no concrete actor.  A
        stored wildcard itself is named, as are subjects reached through
        userset paths.  Caveat truth is left to the LOWER predicate.
        """

        key = resource, name
        if key in seen:
            return False
        definition = self.schema.get_definition(resource.resource_type)
        if definition is None:
            return False
        seen = seen | {key}
        permission = self.schema.get_permission(resource.resource_type, name)
        if permission is not None:
            return self._named_expr(permission.expression, definition, resource, candidate, seen)
        relation = find_relation(definition, name)
        if relation is None:
            return False
        return any(
            row.subject == candidate
            or (
                bool(row.subject.optional_relation)
                and self.named(
                    row.subject.object,
                    row.subject.optional_relation,
                    candidate,
                    seen,
                )
            )
            for row in self._rows(resource, relation, definition)
        )

    def subjects(
        self,
        resource: ObjectRef,
        name: str,
        subject_type: str,
        seen: frozenset[tuple[ObjectRef, str]] = frozenset(),
    ) -> set[SubjectRef]:
        """Every subject of ``subject_type`` that ``named`` would accept here."""

        key = resource, name
        if key in seen:
            return set()
        definition = self.schema.get_definition(resource.resource_type)
        if definition is None:
            return set()
        seen = seen | {key}
        permission = self.schema.get_permission(resource.resource_type, name)
        if permission is not None:
            return self._subjects_expr(
                permission.expression, definition, resource, subject_type, seen
            )
        relation = find_relation(definition, name)
        if relation is None:
            return set()
        found: set[SubjectRef] = set()
        for row in self._rows(resource, relation, definition):
            if row.subject.subject_type == subject_type and row.subject.subject_id:
                found.add(row.subject)
            if row.subject.optional_relation:
                found |= self.subjects(
                    row.subject.object, row.subject.optional_relation, subject_type, seen
                )
        return found

    def _subjects_expr(
        self,
        expr: PermExpr,
        definition: Definition,
        resource: ObjectRef,
        subject_type: str,
        seen: frozenset[tuple[ObjectRef, str]],
    ) -> set[SubjectRef]:
        if isinstance(expr, PermRef):
            if expr.name in {"anonymous", "authenticated"}:
                return set()
            return self.subjects(resource, expr.name, subject_type, seen)
        if isinstance(expr, PermArrow):
            relation = find_relation(definition, expr.via)
            if relation is None:
                return set()
            found: set[SubjectRef] = set()
            for row in self._rows(resource, relation, definition):
                found |= self.subjects(row.subject.object, expr.target, subject_type, seen)
            return found
        if isinstance(expr, PermBinOp):
            left = self._subjects_expr(expr.left, definition, resource, subject_type, seen)
            if expr.op == "-":
                return left
            return left | self._subjects_expr(expr.right, definition, resource, subject_type, seen)
        return set()

    def _named_expr(
        self,
        expr: PermExpr,
        definition: Definition,
        resource: ObjectRef,
        candidate: SubjectRef,
        seen: frozenset[tuple[ObjectRef, str]],
    ) -> bool:
        if isinstance(expr, PermRef):
            return expr.name not in {"anonymous", "authenticated"} and self.named(
                resource, expr.name, candidate, seen
            )
        if isinstance(expr, PermArrow):
            relation = find_relation(definition, expr.via)
            return relation is not None and any(
                self.named(row.subject.object, expr.target, candidate, seen)
                for row in self._rows(resource, relation, definition)
            )
        if isinstance(expr, PermBinOp):
            left = self._named_expr(expr.left, definition, resource, candidate, seen)
            if expr.op == "-":
                return left
            return left or self._named_expr(expr.right, definition, resource, candidate, seen)
        return False


def residual(
    *,
    schema: Schema,
    resource: ObjectRef,
    action: str,
    actor: SubjectRef,
    context: Mapping[str, Any] | None,
    using: str,
) -> Residual:
    """Report inputs that may change an uncertain SQL result; never allow."""

    return _Evaluator(schema, actor, context, using, timezone.now()).residual(resource, action)


def named_subjects(
    *,
    schema: Schema,
    resource: ObjectRef,
    action: str,
    subject_type: str,
    using: str,
) -> list[SubjectRef]:
    """The subjects of a type that a source path from ``resource`` names.

    Only the tuples, backed columns and constants on paths above this one
    resource are read; no table is enumerated.
    """

    evaluator = _Evaluator(schema, SubjectRef.of("rebac/internal", ""), None, using, timezone.now())
    return sorted(evaluator.subjects(resource, action, subject_type), key=str)
