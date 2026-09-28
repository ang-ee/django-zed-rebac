"""Compile non-caveated local permissions into lazy ORM predicates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from django.core.exceptions import FieldDoesNotExist
from django.db import connections, models
from django.db.models import Exists, F, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.expressions import Combinable
from django.db.models.functions import Cast
from django.utils import timezone

from .._id import model_identity_fields, resource_id_attr
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


@dataclass(frozen=True, slots=True)
class _CompileContext:
    depth: int = 0
    boundary: bool = False


_ROOT_CONTEXT = _CompileContext()


def _truth(value: bool) -> Q:
    return Q(Value(value, output_field=models.BooleanField()))


def _concrete_field(model: type[models.Model], identity: str) -> models.Field[Any, Any]:
    """The scalar conversion owner behind a concrete identity lookup."""

    try:
        query_field, scalar_field = model_identity_fields(model, identity)
    except FieldDoesNotExist, ValueError:
        raise UnsupportedScope from None
    if not query_field.concrete:
        raise UnsupportedScope
    return scalar_field


# Field classes whose Python and database conversions are the identity, so a
# correlated SQL comparison agrees with the evaluator's Python comparison.
_NATIVE_FIELD_CLASSES: tuple[type[models.Field[Any, Any]], ...] = (
    models.CharField,
    models.TextField,
    models.IntegerField,
)


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
        # subqueries. The shared evaluator retains this alias on nested walks.
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
        try:
            return self.permission(
                resource_type, action, model, resource_id_attr(model), frozenset()
            )
        except UnsupportedScope:
            from .local_recursive import RecursiveQueryScope, reaches_self_arrow

            if not reaches_self_arrow(self.schema, resource_type, action):
                raise
            return RecursiveQueryScope(self.backend, self.subject, self.using).predicate(
                model, action, resource_type
            )

    def permission(
        self,
        resource_type: str,
        action: str,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
        context: _CompileContext = _ROOT_CONTEXT,
    ) -> Q:
        key = (resource_type, action)
        if key in seen or len(seen) > app_settings.REBAC_DEPTH_LIMIT:
            raise UnsupportedScope
        definition = self.schema.get_definition(resource_type)
        if definition is None:
            return _truth(False)
        permission = self.schema.get_permission(resource_type, action)
        expr = permission.expression if permission is not None else PermRef(action)
        return self.branch(expr, definition, model, identity, seen | {key}, context)

    def expression(
        self,
        expr: PermExpr,
        definition: Definition,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
        context: _CompileContext = _ROOT_CONTEXT,
    ) -> Q:
        if isinstance(expr, PermNil):
            return _truth(False)
        if isinstance(expr, PermRef):
            if expr.name in BUILTIN_ACTOR_TYPES:
                return _truth(builtin_actor_matches(expr.name, self.subject))
            relation = find_relation(definition, expr.name)
            if relation is not None:
                return self.relation(definition, relation, model, identity, seen, context)
            permission = self.schema.get_permission(definition.resource_type, expr.name)
            if permission is None:
                return _truth(False)
            return self.permission(
                definition.resource_type, expr.name, model, identity, seen, context
            )
        if isinstance(expr, PermBinOp):
            left = self.branch(expr.left, definition, model, identity, seen, context)
            right = self.branch(expr.right, definition, model, identity, seen, context)
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
            return self.relation(
                definition, relation, model, identity, seen, context, target=expr.target
            )
        raise UnsupportedScope

    def branch(
        self,
        expr: PermExpr,
        definition: Definition,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
        context: _CompileContext = _ROOT_CONTEXT,
    ) -> Q:
        if isinstance(expr, PermRef) and self.schema.get_permission(
            definition.resource_type, expr.name
        ):
            return self.permission(
                definition.resource_type, expr.name, model, identity, seen, context
            )
        return self.expression(expr, definition, model, identity, seen, context)

    @staticmethod
    def reference(identity: str | Value) -> Any:
        return (
            Cast(OuterRef(identity), models.TextField()) if isinstance(identity, str) else identity
        )

    def _source_by_identity(
        self, queryset: QuerySet[Any], id_attr: str, identity: str | Value
    ) -> QuerySet[Any]:
        """Correlate a backing source with a wire identity in the surrounding query."""
        if not self.native_identity(queryset.model, id_attr):
            raise UnsupportedScope
        source: QuerySet[Any] = queryset.alias(
            _scope_resource_id=Cast(F(id_attr), models.TextField())
        ).filter(_scope_resource_id=self.reference(identity))
        return source

    def relation(
        self,
        definition: Definition,
        relation: Relation,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
        context: _CompileContext = _ROOT_CONTEXT,
        *,
        target: str | None = None,
    ) -> Q:
        if any(allowed.with_caveat for allowed in relation.allowed_subjects):
            raise UnsupportedScope
        backing = self.backend._resolve_declared_field_backing(definition, relation)
        if backing is not None:
            direct_field = (
                backing.field
                if model is backing.source_model
                and isinstance(identity, str)
                and "__" not in backing.path
                and not backing.filters
                and isinstance(backing.field, (models.ForeignKey, models.OneToOneField))
                else None
            )
            if target is None:
                if not subject_allowed_by_relation(relation, self.subject):
                    return _truth(False)
                if direct_field is not None:
                    if backing.targets_identity_directly():
                        return Q(**{direct_field.attname: self.subject.subject_id})
                    destination = backing.target_model._base_manager.using(self.using).filter(
                        **{backing.target_id_attr: self.subject.subject_id}
                    )
                    return Q(
                        **{
                            f"{direct_field.attname}__in": Subquery(
                                destination.order_by().values(direct_field.target_field.name)
                            )
                        }
                    )
                source = backing.queryset(subject=self.subject, using=self.using)
            else:
                destination = backing.target_model._base_manager.using(self.using).filter(
                    self.hop(
                        backing.target_resource_type,
                        target,
                        backing.target_model,
                        backing.target_id_attr,
                        seen,
                        context,
                    )
                )
                if direct_field is not None:
                    return Q(
                        **{
                            f"{direct_field.attname}__in": Subquery(
                                destination.order_by().values(direct_field.target_field.name)
                            )
                        }
                    )
                target_ids = destination.order_by().values_list(backing.target_id_attr, flat=True)
                source = backing.queryset(target_ids=target_ids, using=self.using)
            if model is backing.source_model and isinstance(identity, str):
                source = source.filter(pk=OuterRef("pk"))
            else:
                source = self._source_by_identity(source, backing.source_id_attr, identity)
            return Q(Exists(source))
        attribute = self.backend._resolve_declared_attribute_backing(definition, relation)
        if attribute is not None:
            if (
                model is not None
                and isinstance(identity, str)
                and not self.native_identity(model, identity)
            ):
                raise UnsupportedScope
            resource_id: str | Combinable
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
                        source_field = _concrete_field(model, identity)
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
                        try:
                            _concrete_field(model, identity).get_prep_value(resource_id)
                        except TypeError, ValueError:
                            raise UnsupportedScope from None
                    resource_match = Q(**{identity: resource_id})
            else:
                raise UnsupportedScope
            if target is None:
                if not subject_allowed_by_relation(relation, self.subject):
                    return _truth(False)
                target_condition = attribute.target_filter(resource_id, self.subject)
            else:
                target_condition = attribute.subjects_filter(resource_id) & self.hop(
                    attribute.target_resource_type,
                    target,
                    attribute.target_model,
                    attribute.target_id_attr,
                    seen,
                    context,
                )
            targets = attribute.target_model._base_manager.using(self.using).filter(
                target_condition
            )
            derived = resource_match & Q(Exists(targets))
            if attribute.resource is None:
                return derived
            stored = self._stored_relation(
                definition, relation, identity, seen, context, target=target
            )
            if isinstance(identity, Value):
                fallback = _truth(str(identity.value) != attribute.resource)
            else:
                fallback = ~Q(**{identity: attribute.resource})
            return derived | (fallback & stored)
        const = self.backend._resolve_declared_const_backing(definition, relation)
        if const is not None:
            if target is None:
                constant_match = _truth(
                    self.subject == SubjectRef.of(const.target_resource_type, const.target_id)
                )
            else:
                constant_match = self.hop(
                    const.target_resource_type,
                    target,
                    None,
                    Value(const.target_id),
                    seen,
                    context,
                )
            if const.filters:
                if model is const.source_model and isinstance(identity, str):
                    row_match = Q(**const.filters)
                else:
                    source = self._source_by_identity(
                        const.source_model._base_manager.using(self.using).filter(**const.filters),
                        const.source_id_attr,
                        identity,
                    )
                    row_match = Q(Exists(source))
                return row_match & constant_match
            return constant_match
        if (
            model is not None
            and isinstance(identity, str)
            and not self.native_identity(model, identity)
        ):
            # Validate every reachable branch before selecting the conversion
            # fallback. A downstream caveat cannot collapse to a false RHS of
            # an exclusion; unsupported graphs must use the tri-state evaluator
            # for the entire permission expression.
            self.relation(definition, relation, None, Value(""), seen, context, target=target)
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
        return self._stored_relation(definition, relation, identity, seen, context, target=target)

    def _stored_relation(
        self,
        definition: Definition,
        relation: Relation,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
        context: _CompileContext = _ROOT_CONTEXT,
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
            member = self.subject_membership(allowed, target, seen, context)
            allowed_rows |= shape & member
        return Q(Exists(rows.filter(allowed_rows)))

    def hop(
        self,
        resource_type: str,
        action: str,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
        context: _CompileContext = _ROOT_CONTEXT,
    ) -> Q:
        return self.permission(resource_type, action, model, identity, seen, context)

    def subject_membership(
        self,
        allowed: AllowedSubject,
        target: str | None,
        seen: frozenset[tuple[str, str]],
        context: _CompileContext = _ROOT_CONTEXT,
    ) -> Q:
        if target is not None:
            return self.hop(allowed.type, target, None, "_scope_subject_id", seen, context)
        member = Q(
            subject_type=self.subject.subject_type,
            subject_id=self.subject.subject_id,
            optional_subject_relation=self.subject.optional_relation,
        )
        if allowed.wildcard and not self.subject.optional_relation:
            member |= _truth(self.subject.subject_type == allowed.type)
        if allowed.relation:
            target_definition = self.schema.get_definition(allowed.type)
            if target_definition is None:
                return _truth(False)
            if find_relation(target_definition, allowed.relation) is None:
                raise UnsupportedScope
            member |= self.hop(
                allowed.type,
                allowed.relation,
                None,
                "_scope_subject_id",
                seen,
                context,
            )
        return member

    def native_identity(self, model: type[models.Model], identity: str) -> bool:
        """Whether ``identity`` compares in SQL exactly as the evaluator compares it in Python.

        Requires a concrete text or integer column whose ``to_python`` /
        ``get_prep_value`` are the stock implementations and which has no
        database converters: a transforming subclass (case folding, encoded
        ids) would let the correlated SQL comparison and the Python
        round-trip disagree, so such identities fall back to enumeration.
        """
        try:
            field = _concrete_field(model, identity)
        except UnsupportedScope:
            return False
        stock = next((cls for cls in _NATIVE_FIELD_CLASSES if isinstance(field, cls)), None)
        if stock is None:
            return False
        field_class = type(field)
        return (
            field_class.to_python is stock.to_python
            and field_class.get_prep_value is stock.get_prep_value
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
