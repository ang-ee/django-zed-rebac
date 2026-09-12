"""Compile non-caveated, acyclic local permissions into lazy ORM predicates."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from django.db import models
from django.db.models import Exists, F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast, Now

from .._id import resource_id_attr
from ..conf import app_settings
from ..schema.ast import (
    BUILTIN_ACTOR_TYPES,
    AllowedSubject,
    Definition,
    PermArrow,
    PermBinOp,
    PermExpr,
    PermNil,
    PermRef,
    Relation,
)
from ..schema.walker import builtin_actor_matches, find_relation, subject_allowed_by_relation
from ..types import SubjectRef

if TYPE_CHECKING:
    from ..models.relationship import RelationshipQuerySet, RelationshipRegistryQuerySet
    from .local import LocalBackend


class UnsupportedScope(Exception):
    """Use the existing evaluator for the entire permission expression."""


def _truth(value: bool) -> Q:
    return Q(Value(value, output_field=models.BooleanField()))


class LocalQueryScope:
    """Compile schema membership without enumerating the matching resources.

    ``identity`` names the resource ID in the current query, or a fixed ID for
    constant arrows. Nested tuple queries use storage-owned wire aliases.
    """

    def __init__(self, backend: LocalBackend, subject: SubjectRef, using: str) -> None:
        # Models are registered after the backend module is imported by Django.
        from ..models import active_relationship_model

        self.backend = backend
        self.schema = backend.schema()
        self.subject = subject
        self.using = using
        rows = cast(
            "RelationshipQuerySet | RelationshipRegistryQuerySet",
            active_relationship_model().objects.using(using),
        )
        self.relationships = rows.with_wire_ids()

    def predicate(self, model: type[models.Model], action: str, resource_type: str) -> Q:
        return self.permission(resource_type, action, model, resource_id_attr(model), frozenset())

    def permission(
        self,
        resource_type: str,
        action: str,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
    ) -> Q:
        key = (resource_type, action)
        if key in seen or len(seen) > app_settings.REBAC_DEPTH_LIMIT:
            raise UnsupportedScope
        definition = self.schema.get_definition(resource_type)
        if definition is None:
            return _truth(False)
        permission = self.schema.get_permission(resource_type, action)
        expr = permission.expression if permission is not None else PermRef(action)
        return self.branch(expr, definition, model, identity, seen | {key})

    def expression(
        self,
        expr: PermExpr,
        definition: Definition,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
    ) -> Q:
        if isinstance(expr, PermNil):
            return _truth(False)
        if isinstance(expr, PermRef):
            if expr.name in BUILTIN_ACTOR_TYPES:
                return _truth(builtin_actor_matches(expr.name, self.subject))
            relation = find_relation(definition, expr.name)
            if relation is not None:
                return self.relation(definition, relation, model, identity, seen)
            permission = self.schema.get_permission(definition.resource_type, expr.name)
            if permission is None:
                return _truth(False)
            return self.permission(definition.resource_type, expr.name, model, identity, seen)
        if isinstance(expr, PermBinOp):
            left = self.branch(expr.left, definition, model, identity, seen)
            right = self.branch(expr.right, definition, model, identity, seen)
            if expr.op == "+":
                return left | right
            if expr.op == "&":
                return left & right
            if expr.op == "-":
                return left & ~right
            raise UnsupportedScope
        if isinstance(expr, PermArrow):
            relation = find_relation(definition, expr.via)
            if relation is None:
                return _truth(False)
            return self.relation(definition, relation, model, identity, seen, target=expr.target)
        raise UnsupportedScope

    def branch(
        self,
        expr: PermExpr,
        definition: Definition,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
    ) -> Q:
        if isinstance(expr, PermRef) and self.schema.get_permission(
            definition.resource_type, expr.name
        ):
            return self.permission(definition.resource_type, expr.name, model, identity, seen)
        return self.expression(expr, definition, model, identity, seen)

    @staticmethod
    def reference(identity: str | Value) -> Any:
        return (
            Cast(OuterRef(identity), models.TextField()) if isinstance(identity, str) else identity
        )

    def relation(
        self,
        definition: Definition,
        relation: Relation,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
        *,
        target: str | None = None,
    ) -> Q:
        if any(allowed.with_caveat for allowed in relation.allowed_subjects):
            raise UnsupportedScope
        backing = self.backend._resolve_declared_field_backing(definition, relation)
        if backing is not None:
            source = backing.source_model._base_manager.using(self.using)
            if target is None:
                if not subject_allowed_by_relation(relation, self.subject):
                    return _truth(False)
                condition = Q(**backing.target_filter(self.subject))
            else:
                destination = backing.target_model._base_manager.using(self.using).filter(
                    self.permission(
                        backing.target_resource_type,
                        target,
                        backing.target_model,
                        backing.target_id_attr,
                        seen,
                    )
                )
                condition = Q(
                    **{
                        f"{backing.target_values_path()}__in": Subquery(
                            destination.order_by().values(backing.target_id_attr)
                        )
                    }
                )
            # Direct local columns preserve their native indexes. Related-ID
            # projections stay inside EXISTS, so authorization adds no joins
            # to the caller's query and cannot multiply aggregate rows.
            if model is backing.source_model and backing.target_id_attr == "pk":
                return condition
            return Q(
                Exists(
                    source.alias(
                        _scope_resource_id=Cast(F(backing.source_id_attr), models.TextField())
                    )
                    .filter(_scope_resource_id=self.reference(identity))
                    .filter(condition)
                )
            )
        const = self.backend._resolve_declared_const_backing(definition, relation)
        if const is not None:
            if target is None:
                return _truth(
                    self.subject == SubjectRef.of(const.target_resource_type, const.target_id)
                )
            return self.permission(
                const.target_resource_type,
                target,
                None,
                Value(const.target_id),
                seen,
            )
        rows = self.relationships.filter(
            **{
                "resource_type": definition.resource_type,
                "resource_id": self.reference(identity),
                "relation": relation.name,
                "caveat_name": "",
            }
        )
        if relation.with_expiration:
            rows = rows.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=Now()))
        else:
            rows = rows.filter(expires_at__isnull=True)
        allowed_rows = _truth(False)
        for allowed in relation.allowed_subjects:
            shape = self.subject_shape(allowed)
            if target is not None:
                member = self.permission(allowed.type, target, None, "_scope_subject_id", seen)
            else:
                member = Q(
                    subject_type=self.subject.subject_type,
                    subject_id=self.subject.subject_id,
                    optional_subject_relation=self.subject.optional_relation,
                )
                if allowed.wildcard and not self.subject.optional_relation:
                    member |= _truth(self.subject.subject_type == allowed.type)
                if allowed.relation:
                    member |= self.permission(
                        allowed.type,
                        allowed.relation,
                        None,
                        "_scope_subject_id",
                        seen,
                    )
            allowed_rows |= shape & member
        return Q(Exists(rows.filter(allowed_rows)))

    @staticmethod
    def subject_shape(allowed: AllowedSubject) -> Q:
        condition = Q(subject_type=allowed.type, optional_subject_relation=allowed.relation)
        if allowed.wildcard:
            return condition & Q(subject_id="*")
        condition &= ~Q(subject_id="*")
        if allowed.id:
            condition &= Q(subject_id=allowed.id)
        return condition
