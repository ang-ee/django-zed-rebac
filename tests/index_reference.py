"""Finite-domain, per-actor semantics for the permission index.

No index code, ORM, or walker is used here. Backed edges are supplied as
tuples. Expressions are evaluated for one actor, so set operations combine
that actor's three-state membership without materializing set algebra.

A conditional result names the parameters it still needs. Unions, arrows and
memberships form a monotone formula over caveats and set operations; it needs
the parameters of the terms its value depends on, found here by trying their
values. A set operation needs what its conditional operands need. Neither
depends on the order of arms or of tuples, and both are within what the
walker reports.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from itertools import product
from typing import Any

from rebac.caveats import evaluate as evaluate_caveat
from rebac.conf import app_settings
from rebac.schema.ast import (
    ConstBinding,
    Definition,
    PermArrow,
    PermBinOp,
    PermExpr,
    PermNil,
    PermRef,
    Relation,
    Schema,
)
from rebac.types import CheckResult, ObjectRef, RelationshipTuple, SubjectRef


def _any(values: Iterable[bool | None]) -> bool | None:
    seen = list(values)
    return True if True in seen else None if None in seen else False


def _all(values: Iterable[bool | None]) -> bool | None:
    seen = list(values)
    return False if False in seen else None if None in seen else True


@dataclass(frozen=True)
class Formula:
    """``and``/``or`` join paths; ``both``/``minus`` are the schema's set operations."""

    op: str
    args: tuple[Formula, ...] = ()
    name: str = ""
    pinned: Mapping[str, Any] | None = None

    @property
    def key(self) -> str:
        if self.op == "caveat":
            return f"{self.name}{json.dumps(dict(self.pinned or {}), sort_keys=True)}"
        return f"{self.op}({','.join(arg.key for arg in self.args)})"

    def _terms(self) -> list[Formula]:
        """The terms of the monotone formula rooted here."""
        if self.op in {"and", "or"}:
            return [term for arg in self.args for term in arg._terms()]
        return [self]

    def _combine(self, values: Mapping[str, bool | None]) -> bool | None:
        if self.op not in {"and", "or"}:
            return values[self.key]
        results = [arg._combine(values) for arg in self.args]
        return _any(results) if self.op == "or" else _all(results)

    def evaluate(
        self, schema: Schema, context: Mapping[str, Any] | None
    ) -> tuple[bool | None, frozenset[str]]:
        """The three-state value, and the parameters a conditional one needs."""
        if self.op in {"true", "false"}:
            return self.op == "true", frozenset()
        if self.op == "caveat":
            caveat = schema.get_caveat(self.name)
            if caveat is None:
                return False, frozenset()
            value, names = evaluate_caveat(caveat, dict(self.pinned or {}), dict(context or {}))
            return value, frozenset(names) if value is None else frozenset()
        if self.op in {"both", "minus"}:
            (left, needs_left), (right, needs_right) = (
                arg.evaluate(schema, context) for arg in self.args
            )
            if self.op == "minus":
                right = None if right is None else not right
            value = _all((left, right))
            if value is not None:
                return value, frozenset()
            return value, (needs_left if left is None else frozenset()) | (
                needs_right if right is None else frozenset()
            )
        terms = {term.key: term.evaluate(schema, context) for term in self._terms()}
        values = {key: value for key, (value, _) in terms.items()}
        value = self._combine(values)
        if value is not None:
            return value, frozenset()
        unknown = [key for key, known in values.items() if known is None]
        needed: frozenset[str] = frozenset()
        for key in unknown:
            others = [other for other in unknown if other != key]
            for trial in product((True, False), repeat=len(others)):
                fixed = {**values, **dict(zip(others, trial, strict=True))}
                if (
                    self._combine({**fixed, key: True}) is True
                    and self._combine({**fixed, key: False}) is False
                ):
                    needed |= terms[key][1]
                    break
        return value, needed


TRUE = Formula("true")
FALSE = Formula("false")


def conjunction(*args: Formula) -> Formula:
    return Formula("and", args)


def disjunction(*args: Formula) -> Formula:
    return Formula("or", args)


class ReferenceModel:
    def __init__(
        self, schema: Schema, tuples: Iterable[RelationshipTuple], *, now: datetime
    ) -> None:
        self.schema = schema
        self.tuples = tuple(tuples)
        self.now = now
        self.resources = {row.resource for row in self.tuples}
        self.resources.update(row.subject.object for row in self.tuples)

    def check(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: Mapping[str, Any] | None = None,
    ) -> CheckResult:
        formula = self._node(resource, action, subject, frozenset())
        value, missing = formula.evaluate(self.schema, context)
        if not resource.resource_id:
            # Model-level checks mean any definite accessible resource, with
            # row-independent grants also working for an empty resource type.
            if value is True or any(
                self.check(
                    subject=subject, action=action, resource=candidate, context=context
                ).allowed
                for candidate in self.resources
                if candidate.resource_type == resource.resource_type and candidate.resource_id
            ):
                return CheckResult.has()
            return CheckResult.no()
        if value is None:
            return CheckResult.conditional(tuple(sorted(missing)))
        return CheckResult.has() if value else CheckResult.no()

    def accessible(self, *, subject: SubjectRef, action: str, resource_type: str) -> set[str]:
        """Enumerate this model's finite resource universe; checks also accept unseen IDs."""
        return {
            resource.resource_id
            for resource in self.resources
            if resource.resource_type == resource_type
            and self.check(subject=subject, action=action, resource=resource).allowed
        }

    def lookup_subjects(
        self, *, resource: ObjectRef, action: str, subject_type: str
    ) -> set[SubjectRef]:
        # Enumeration lists the subjects a tuple, a membership or a backed edge
        # names on a path to the permission, and that the check allows.
        # Matching a class (authenticated, anonymous, a wildcard) names nobody;
        # a stored wildcard subject is listed as itself.
        candidates = {row.subject for row in self.tuples}
        candidates.update(SubjectRef(ref) for ref in self.resources)
        for definition in self.schema.definitions:
            for relation in definition.relations:
                if isinstance(relation.backing, ConstBinding) and not relation.backing.filters:
                    candidates.add(
                        SubjectRef.of(
                            relation.allowed_subjects[0].type,
                            relation.backing.target_id,
                            relation.allowed_subjects[0].relation,
                        )
                    )
        return {
            subject
            for subject in candidates
            if subject.subject_type == subject_type
            and self._named(resource, action, subject, frozenset())
            and self.check(subject=subject, action=action, resource=resource).allowed
        }

    def _named(
        self,
        resource: ObjectRef,
        name: str,
        subject: SubjectRef,
        seen: frozenset[tuple[ObjectRef, str]],
    ) -> bool:
        key = (resource, name)
        definition = self.schema.get_definition(resource.resource_type)
        if key in seen or definition is None:
            return False
        seen = seen | {key}
        permission = next((p for p in definition.permissions if p.name == name), None)
        if permission is not None:
            return self._named_expr(permission.expression, definition, resource, subject, seen)
        relation = next((r for r in definition.relations if r.name == name), None)
        if relation is None:
            return False
        return any(
            row.subject == subject
            or bool(
                row.subject.optional_relation
                and self._named(row.subject.object, row.subject.optional_relation, subject, seen)
            )
            for row in self._rows(resource, relation)
        )

    def _named_expr(
        self,
        expr: PermExpr,
        definition: Definition,
        resource: ObjectRef,
        subject: SubjectRef,
        seen: frozenset[tuple[ObjectRef, str]],
    ) -> bool:
        if isinstance(expr, PermRef):
            if expr.name in {"anonymous", "authenticated"}:
                return False
            return self._named(resource, expr.name, subject, seen)
        if isinstance(expr, PermArrow):
            relation = next((r for r in definition.relations if r.name == expr.via), None)
            return relation is not None and any(
                self._named(row.subject.object, expr.target, subject, seen)
                for row in self._rows(resource, relation)
            )
        if isinstance(expr, PermBinOp):
            # A - B is within A; A & B is within both; A + B is either.
            left = self._named_expr(expr.left, definition, resource, subject, seen)
            if expr.op == "-":
                return left
            return left or self._named_expr(expr.right, definition, resource, subject, seen)
        return False

    def _leaf(self, row: RelationshipTuple) -> Formula:
        return (
            Formula("caveat", name=row.caveat_name, pinned=row.caveat_context)
            if row.caveat_name
            else TRUE
        )

    def _rows(self, resource: ObjectRef, relation: Relation) -> tuple[RelationshipTuple, ...]:
        backing = relation.backing
        if isinstance(backing, ConstBinding) and not backing.filters:
            return (
                RelationshipTuple(
                    resource,
                    relation.name,
                    SubjectRef.of(
                        relation.allowed_subjects[0].type,
                        backing.target_id,
                        relation.allowed_subjects[0].relation,
                    ),
                ),
            )
        return tuple(
            row
            for row in self.tuples
            if row.resource == resource
            and row.relation == relation.name
            and (row.expires_at is None or self.now < row.expires_at)
            and self._allowed(row, relation)
        )

    @staticmethod
    def _allowed(row: RelationshipTuple, relation: Relation) -> bool:
        if row.expires_at is not None and not relation.with_expiration:
            return False
        subject = row.subject
        for allowed in relation.allowed_subjects:
            if allowed.type != subject.subject_type or allowed.with_caveat != row.caveat_name:
                continue
            if allowed.wildcard:
                if subject.subject_id == "*" and not subject.optional_relation:
                    return True
            elif (
                subject.subject_id != "*"
                and (not allowed.id or allowed.id == subject.subject_id)
                and allowed.relation == subject.optional_relation
            ):
                return True
        return False

    def _node(
        self,
        resource: ObjectRef,
        name: str,
        actor: SubjectRef,
        seen: frozenset[tuple[ObjectRef, str]],
    ) -> Formula:
        key = (resource, name)
        if key in seen:
            # Positive cycles denote the union of finite paths. A repeated
            # vertex cannot add a new path; no arbitrary depth bound is needed.
            return FALSE
        definition = self.schema.get_definition(resource.resource_type)
        if definition is None:
            return FALSE
        seen = seen | {key}
        permission = next((p for p in definition.permissions if p.name == name), None)
        if permission is not None:
            return self._expr(permission.expression, definition, resource, actor, seen)
        relation = next((r for r in definition.relations if r.name == name), None)
        if relation is None:
            return FALSE
        rows = self._rows(resource, relation)
        # Preserve the frozen walker's direct / wildcard / userset path order.
        direct = [self._leaf(row) for row in rows if row.subject == actor]
        wildcards = [
            self._leaf(row)
            for row in rows
            if not actor.optional_relation
            and row.subject.subject_type == actor.subject_type
            and row.subject.subject_id == "*"
        ]
        nested = [
            conjunction(
                self._leaf(row),
                self._node(row.subject.object, row.subject.optional_relation, actor, seen),
            )
            for row in rows
            if row.subject.optional_relation
        ]
        return disjunction(*direct, *wildcards, *nested)

    def _expr(
        self,
        expr: PermExpr,
        definition: Definition,
        resource: ObjectRef,
        actor: SubjectRef,
        seen: frozenset[tuple[ObjectRef, str]],
    ) -> Formula:
        if isinstance(expr, PermNil):
            return FALSE
        if isinstance(expr, PermRef):
            if expr.name in {"anonymous", "authenticated"}:
                anonymous = actor == SubjectRef.of(
                    f"{app_settings.REBAC_TYPE_PREFIX or ''}{app_settings.REBAC_ANONYMOUS_TYPE}",
                    "*",
                )
                if expr.name == "anonymous":
                    return TRUE if anonymous else FALSE
                return TRUE if not anonymous and actor.subject_id else FALSE
            return self._node(resource, expr.name, actor, seen)
        if isinstance(expr, PermArrow):
            relation = next((r for r in definition.relations if r.name == expr.via), None)
            if relation is None:
                return FALSE
            return disjunction(
                *(
                    conjunction(
                        self._leaf(row), self._node(row.subject.object, expr.target, actor, seen)
                    )
                    for row in self._rows(resource, relation)
                )
            )
        if isinstance(expr, PermBinOp):
            left = self._expr(expr.left, definition, resource, actor, seen)
            right = self._expr(expr.right, definition, resource, actor, seen)
            if expr.op == "+":
                return disjunction(left, right)
            return Formula({"&": "both", "-": "minus"}[expr.op], (left, right))
        raise TypeError(expr)
