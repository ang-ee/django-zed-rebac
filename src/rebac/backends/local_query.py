"""Compile non-caveated, acyclic local permissions into lazy ORM predicates."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from django.db import connections, models
from django.db.models import Exists, F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast
from django.utils import timezone

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


class ConvertedRelationIds(models.Expression):
    """Apply a resource field's Python conversion to tuple-derived grants only.

    Deferring until SQL compilation preserves revocation for scoped subqueries.
    The surrounding permission tree (including field ownership) stays in SQL.
    Django builds the subquery and performs every ID conversion itself.
    """

    def __init__(
        self,
        scope: LocalQueryScope,
        definition: Definition,
        relation: Relation,
        model: type[models.Model],
        identity: str,
        target: str | None,
    ) -> None:
        super().__init__()
        self.scope = scope
        self.definition = definition
        self.relation = relation
        self.model = model
        self.id_attr = identity
        self.target = target

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        # Resolve tuple-derived grants from the SAME database the surrounding
        # predicate joins against (``self.scope.using``), not the default alias,
        # so multi-database scoping stays consistent with the native EXISTS
        # subqueries. Sub-branches that dispatch into the tri-state evaluator
        # still resolve against the default DB — see ``docs/ARCHITECTURE.md``
        # (multi-database resolution).
        using = self.scope.using
        if self.target is None:
            ids = self.scope.backend._resources_via_relation(
                resource_type=self.definition.resource_type,
                relation=self.relation.name,
                subject=self.scope.subject,
                depth=0,
                cache={},
                using=using,
            )
        else:
            ids = self.scope.backend._resources_for_expr(
                PermArrow(self.relation.name, self.target),
                self.definition,
                self.scope.subject,
                0,
                {},
                using=using,
            )
        rows = (
            self.model._base_manager.using(connection.alias)
            .filter(**{f"{self.id_attr}__in": sorted(ids)})
            .order_by()
            .values(self.id_attr)
        )
        sql, params = compiler.compile(Subquery(rows))
        return str(sql), tuple(params)


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
            if model is not backing.source_model and not self.native_identity(
                backing.source_model, backing.source_id_attr
            ):
                raise UnsupportedScope
            if target is None:
                if not subject_allowed_by_relation(relation, self.subject):
                    return _truth(False)
                source = backing.queryset(subject=self.subject, using=self.using)
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
                target_ids = destination.order_by().values_list(
                    backing.target_id_attr, flat=True
                )
                source = backing.queryset(target_ids=target_ids, using=self.using)
            return Q(
                Exists(
                    source.alias(
                        _scope_resource_id=Cast(F(backing.source_id_attr), models.TextField())
                    )
                    .filter(_scope_resource_id=self.reference(identity))
                )
            )
        attribute = self.backend._resolve_declared_attribute_backing(definition, relation)
        if attribute is not None:
            if (
                model is not None
                and isinstance(identity, str)
                and not self.native_identity(model, identity)
            ):
                raise UnsupportedScope
            if isinstance(identity, Value):
                resource_id = str(identity.value)
                resource_match = _truth(attribute.applies_to(resource_id))
            elif isinstance(identity, str):
                if attribute.resource is None:
                    if not self.native_identity(attribute.target_model, attribute.field.name):
                        raise UnsupportedScope
                    if isinstance(attribute.field, (models.CharField, models.TextField)):
                        resource_id = self.reference(identity)
                    elif model is not None:
                        source_field = (
                            model._meta.pk if identity == "pk" else model._meta.get_field(identity)
                        )
                        if source_field.get_internal_type() != attribute.field.get_internal_type():
                            raise UnsupportedScope
                        resource_id = OuterRef(identity)
                    else:
                        # Stored subject ids are wire strings. Comparing them to
                        # a non-text model column needs that field's Python/DB
                        # conversion, which cannot be expressed safely here.
                        raise UnsupportedScope
                    resource_match = _truth(True)
                else:
                    resource_id = attribute.resource
                    if model is not None:
                        source_field = (
                            model._meta.pk if identity == "pk" else model._meta.get_field(identity)
                        )
                        try:
                            source_field.get_prep_value(resource_id)
                        except (TypeError, ValueError):
                            raise UnsupportedScope from None
                    resource_match = Q(**{identity: resource_id})
            else:
                raise UnsupportedScope
            if target is None:
                if not subject_allowed_by_relation(relation, self.subject):
                    return _truth(False)
                target_condition = attribute.target_filter(resource_id, self.subject)
            else:
                target_condition = attribute.subjects_filter(resource_id) & self.permission(
                    attribute.target_resource_type,
                    target,
                    attribute.target_model,
                    attribute.target_id_attr,
                    seen,
                )
            targets = attribute.target_model._base_manager.using(self.using).filter(
                target_condition
            )
            derived = resource_match & Q(Exists(targets))
            if attribute.resource is None:
                return derived
            stored = self._stored_relation(definition, relation, identity, seen, target=target)
            if isinstance(identity, Value):
                fallback = _truth(str(identity.value) != attribute.resource)
            else:
                fallback = ~Q(**{identity: attribute.resource})
            return derived | (fallback & stored)
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
        if (
            model is not None
            and isinstance(identity, str)
            and not self.native_identity(model, identity)
        ):
            # Validate every reachable branch before selecting the conversion
            # fallback. A downstream caveat cannot collapse to a false RHS of
            # an exclusion; unsupported graphs must use the tri-state evaluator
            # for the entire permission expression.
            self.relation(definition, relation, None, Value(""), seen, target=target)
            return Q(
                **{
                    f"{identity}__in": ConvertedRelationIds(
                        self,
                        definition,
                        relation,
                        model,
                        identity,
                        target,
                    )
                }
            )
        return self._stored_relation(definition, relation, identity, seen, target=target)

    def _stored_relation(
        self,
        definition: Definition,
        relation: Relation,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
        *,
        target: str | None,
    ) -> Q:
        """Compile the persisted-tuple path shared by ordinary and fallback reads."""

        rows = self.relationships.filter(
            **{
                "resource_type": definition.resource_type,
                "resource_id": self.reference(identity),
                "relation": relation.name,
                "caveat_name": "",
            }
        )
        if relation.with_expiration:
            # Bind the app-server clock (``timezone.now()``) rather than the
            # database clock (``Now()``): the graph/enumeration path filters
            # expiry with ``timezone.now()`` (see ``local._filter_active``), so
            # using the DB clock here would let the two evaluation strategies
            # disagree inside the app/DB clock-skew window and break the
            # ``accessible() == queryset`` parity contract.
            rows = rows.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
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

    def native_identity(self, model: type[models.Model], identity: str) -> bool:
        field = model._meta.pk if identity == "pk" else model._meta.get_field(identity)
        return (
            isinstance(field, (models.CharField, models.TextField, models.IntegerField))
            and field.concrete
            and not field.get_db_converters(connections[self.using])
        )

    @staticmethod
    def subject_shape(allowed: AllowedSubject) -> Q:
        condition = Q(subject_type=allowed.type, optional_subject_relation=allowed.relation)
        if allowed.wildcard:
            return condition & Q(subject_id="*")
        condition &= ~Q(subject_id="*")
        if allowed.id:
            condition &= Q(subject_id=allowed.id)
        return condition
