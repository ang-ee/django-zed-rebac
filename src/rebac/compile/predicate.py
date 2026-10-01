"""Compile schema permission membership into Django ORM predicates.

No application resource IDs are read into Python.  A predicate evaluates at
an identity expression, so a tuple-only object can participate in both grants
and exclusions without a corresponding model row.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, cast

from django.db import models
from django.db.models import Exists, Expression, F, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.functions import Now
from django.db.models.lookups import Exact, In, IsNull, LessThan

from .._id import model_identity_fields, resource_id_attr
from ..composition import TaggedComposition
from ..conf import app_settings
from ..errors import SchemaError
from ..field_backing import (
    ResolvedAttributeBacking,
    ResolvedConstBacking,
    ResolvedFieldBacking,
    resolve_attribute_backing,
    resolve_const_backing,
    resolve_field_backing,
)
from ..index.codec import identity_codec
from ..resources import model_for_resource_type, model_for_subject_type
from ..schema.ast import (
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
from ..schema.walker import builtin_actor_matches
from ..types import SubjectRef
from .program import CompileProgram, Key


class Bound(StrEnum):
    LOWER = "lower"
    UPPER = "upper"

    def opposite(self) -> Bound:
        return Bound.UPPER if self is Bound.LOWER else Bound.LOWER


@dataclass(frozen=True, slots=True)
class At:
    """Object identity in the query currently receiving the compiled Q.

    ``key`` is the scalar field whose value ``ref`` carries.  It can differ
    from this resource type's REBAC identity (a foreign key with ``to_field``).
    ``None`` means a canonical wire ID in a tuple or literal.
    """

    resource_type: str
    ref: Expression | F | OuterRef
    key: models.Field[Any, Any] | None
    row: bool


class CaveatVerdicts(Protocol):
    def condition_q(self, prefix: str, bound: Bound) -> Q: ...


def _truth(value: bool) -> Q:
    return Q(Value(value, output_field=models.BooleanField()))


def _not_null(expr: Expression | F | OuterRef) -> Q:
    return Q(IsNull(expr, False))


def _in(expr: Expression | F | OuterRef, rows: QuerySet[Any]) -> Q:
    return _not_null(expr) & Q(In(expr, Subquery(rows)))


def _before(now: Expression, deadline: datetime) -> Q:
    return Q(LessThan(now, Value(deadline, output_field=models.DateTimeField())))


class Compiler:
    """One predicate compiler for scopes and point membership.

    The caller supplies the same effective schema for every bound of an
    operation.  Caveat verdicts are prepared by the read operation, then
    injected here; the SQL still witnesses each matching tuple at execution.
    """

    def __init__(
        self,
        schema: Schema,
        actor: SubjectRef,
        using: str,
        *,
        tagged: TaggedComposition | None = None,
        verdicts: CaveatVerdicts | None = None,
        now: Expression | None = None,
        depth_limit: int | None = None,
    ) -> None:
        self.schema = tagged.schema if tagged is not None else schema
        self.actor = actor
        self.using = using
        self.tagged = tagged
        self.verdicts = verdicts
        self.now = now if now is not None else Now()
        self.depth_limit = app_settings.REBAC_DEPTH_LIMIT if depth_limit is None else depth_limit
        self.program = CompileProgram.build(self.schema)

    def _tagged_inside(self, expr: PermExpr) -> bool:
        if self.tagged is None:
            return False
        if id(expr) in self.tagged.arms or id(expr) in self.tagged.sites:
            return True
        return isinstance(expr, PermBinOp) and (
            self._tagged_inside(expr.left) or self._tagged_inside(expr.right)
        )

    def _union_arms(self, expr: PermExpr) -> list[PermExpr]:
        """Expose untagged union structure while retaining tagged subtrees."""
        if (
            isinstance(expr, PermBinOp)
            and expr.op == "+"
            and (
                self.tagged is None
                or (id(expr) not in self.tagged.arms and id(expr) not in self.tagged.sites)
            )
        ):
            return self._union_arms(expr.left) + self._union_arms(expr.right)
        return [expr]

    @staticmethod
    def _join_union(arms: list[PermExpr]) -> PermExpr:
        result = arms[0]
        for arm in arms[1:]:
            result = PermBinOp("+", result, arm)
        return result

    def holds(self, key: Key, at: At, bound: Bound = Bound.LOWER) -> Q:
        if key[0] != at.resource_type:
            raise ValueError(f"{key!r} cannot be evaluated at {at.resource_type!r}")
        return self._holds(key, at, bound, {}, depth_possible=True)

    def has_recursion(self, key: Key) -> bool:
        return bool(self.program.reachable(key) & self.program.recursive)

    def depth_unknown(self, key: Key, at: At) -> Q:
        """Cases possible only because a positive structural cycle was cut."""
        if not self.has_recursion(key):
            return _truth(False)
        upper = self._holds(key, at, Bound.UPPER, {}, depth_possible=True)
        upper_without_depth = self._holds(key, at, Bound.UPPER, {}, depth_possible=False)
        lower = self._holds(key, at, Bound.LOWER, {}, depth_possible=True)
        lower_without_depth = self._holds(key, at, Bound.LOWER, {}, depth_possible=False)
        # A recursive frontier may widen U in a positive position or narrow L
        # after subtraction has swapped the bound.  Both directions matter.
        return upper & ~lower & ((upper & ~upper_without_depth) | (lower_without_depth & ~lower))

    def _holds(
        self,
        key: Key,
        at: At,
        bound: Bound,
        visits: Mapping[Key, int],
        *,
        depth_possible: bool,
    ) -> Q:
        if key not in self.program.dependencies:
            return _truth(False)
        count = visits.get(key, 0)
        if count and key in self.program.alias_cycles:
            # An alias-only cycle stays at this identity. Its positive least
            # fixed point has no new path on a revisit, in either bound.
            return _truth(False)
        if count == 0 and key in self.program.recursive:
            flattened = self._flat_self_userset(key, at, bound, depth_possible=depth_possible)
            if flattened is not None:
                return flattened
            flattened = self._flat_self_fk(key, at, bound, depth_possible=depth_possible)
            if flattened is not None:
                return flattened
            flattened = self._flat_self_tuple(key, at, bound, depth_possible=depth_possible)
            if flattened is not None:
                return flattened
        if key in self.program.recursive and count > self.depth_limit:
            return _truth(bound is Bound.UPPER and depth_possible) & _not_null(at.ref)
        if count > len(self.program.dependencies) + self.depth_limit + 1:
            raise SchemaError(f"Permission graph did not terminate at {key!r}")
        updated = dict(visits)
        updated[key] = count + 1
        definition = self.schema.get_definition(key[0])
        if definition is None:
            return _truth(False)
        relation = next((r for r in definition.relations if r.name == key[1]), None)
        if relation is not None:
            return self._relation(
                definition, relation, at, bound, updated, depth_possible=depth_possible
            )
        permission = self.schema.get_permission(*key)
        if permission is None:
            return _truth(False)
        return self._expression(
            definition,
            permission.expression,
            at,
            bound,
            updated,
            depth_possible=depth_possible,
        )

    def _flat_self_userset(
        self,
        key: Key,
        at: At,
        bound: Bound,
        *,
        depth_possible: bool,
    ) -> Q | None:
        """Flatten a self-userset relation to a UNION of reverse tuple hops."""
        definition = self.schema.get_definition(key[0])
        if definition is None:
            return None
        relation = next((r for r in definition.relations if r.name == key[1]), None)
        if relation is None or relation.backing is not None:
            return None
        recursive = [
            allowed
            for allowed in relation.allowed_subjects
            if allowed.type == key[0] and allowed.relation == key[1]
        ]
        if len(recursive) != 1 or any(
            allowed.relation and allowed not in recursive for allowed in relation.allowed_subjects
        ):
            return None
        from ..models import active_relationship_model

        rows = cast(Any, active_relationship_model().objects.using(self.using)).index_projection()
        rows = rows.filter(resource_type=key[0], relation=key[1])
        if relation.with_expiration:
            rows = rows.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=self.now))
        else:
            rows = rows.filter(expires_at__isnull=True)
        if self.verdicts is not None:
            rows = rows.filter(self.verdicts.condition_q("", bound))
        elif bound is Bound.LOWER:
            rows = rows.filter(caveat_name="")
        direct = _truth(False)
        for allowed in relation.allowed_subjects:
            shape = Q(
                subject_type=allowed.type,
                subject_relation=allowed.relation,
                caveat_name=allowed.with_caveat,
            )
            if allowed.wildcard:
                shape &= Q(subject_id="*")
                admitted = (
                    self.actor.subject_type == allowed.type and not self.actor.optional_relation
                )
            else:
                shape &= ~Q(subject_id="*")
                if allowed.id:
                    shape &= Q(subject_id=allowed.id)
                admitted = (
                    self.actor.subject_type == allowed.type
                    and self.actor.optional_relation == allowed.relation
                    and (not allowed.id or self.actor.subject_id == allowed.id)
                )
                shape &= Q(subject_id=self.actor.subject_id)
            if admitted:
                direct |= shape
        direct_ids = rows.filter(direct).order_by().values("resource_id")
        hop = recursive[0]
        edges = rows.filter(
            subject_type=key[0],
            subject_relation=key[1],
            caveat_name=hop.with_caveat,
        ).exclude(subject_id="*")
        if hop.id:
            edges = edges.filter(subject_id=hop.id)
        branches: list[QuerySet[Any]] = [direct_ids]
        current = direct_ids
        for _ in range(self.depth_limit):
            current = (
                edges.filter(subject_id__in=Subquery(current)).order_by().values("resource_id")
            )
            branches.append(current)
        if bound is Bound.UPPER and depth_possible:
            frontier = edges.order_by().values("resource_id")
            for _ in range(self.depth_limit):
                frontier = (
                    edges.filter(subject_id__in=Subquery(frontier)).order_by().values("resource_id")
                )
            branches.append(frontier)
        converted = [self._tuple_identity_rows(at, branch) for branch in branches]
        return _in(at.ref, converted[0].union(*converted[1:]))

    def _flat_self_fk(
        self,
        key: Key,
        at: At,
        bound: Bound,
        *,
        depth_possible: bool,
    ) -> Q | None:
        """Unroll a linear parent->same-permission cycle as flat UNION branches.

        The base is tested at each ancestor by a regular Django path join.
        No nested copy of the recursive body appears in any branch.  Tuple
        grants on a rowless object still contribute at depth zero.
        """
        definition = self.schema.get_definition(key[0])
        permission = self.schema.get_permission(*key)
        model = model_for_resource_type(key[0])
        if definition is None or permission is None or model is None:
            return None
        arms = self._union_arms(permission.expression)
        recursive = [arm for arm in arms if isinstance(arm, PermArrow) and arm.target == key[1]]
        if len(recursive) != 1 or len(arms) < 2 or self._tagged_inside(recursive[0]):
            return None
        recursive_arm = recursive[0]
        base = self._join_union([arm for arm in arms if arm is not recursive_arm])
        relation = next((r for r in definition.relations if r.name == recursive_arm.via), None)
        if relation is None or not isinstance(relation.backing, FieldBinding):
            return None
        resolved = resolve_field_backing(definition, relation)
        if (
            resolved is None
            or resolved.source_model is not model
            or resolved.target_model is not model
            or resolved.filters
            or "__" in resolved.path
            or not isinstance(resolved.field, (models.ForeignKey, models.OneToOneField))
        ):
            return None
        identity = resource_id_attr(model)
        _, field = model_identity_fields(model, identity)
        row_at = At(key[0], F(identity), field, True)
        base_at_point = self._expression(
            definition, base, at, bound, {key: 1}, depth_possible=depth_possible
        )
        base_at_row = self._expression(
            definition, base, row_at, bound, {key: 1}, depth_possible=depth_possible
        )
        source = model._base_manager.using(self.using)
        base_ids = source.filter(base_at_row).order_by().values_list(identity, flat=True)
        branches: list[QuerySet[Any]] = []
        for hops in range(1, self.depth_limit + 1):
            path = "__".join([resolved.path] * hops)
            reachable = (
                source.filter(**{f"{path}__{identity}__in": Subquery(base_ids)})
                .order_by()
                .values_list(identity, flat=True)
            )
            branches.append(reachable)
        inherited = (
            self._model_membership(
                at,
                source.filter(**{f"{identity}__in": Subquery(branches[0].union(*branches[1:]))})
                if branches
                else source.none(),
                model,
                identity,
            )
            if branches
            else _truth(False)
        )
        if bound is Bound.UPPER and depth_possible:
            deep_path = "__".join([resolved.path] * (self.depth_limit + 1))
            deep_rows = source.filter(**{f"{deep_path}__isnull": False})
            inherited |= self._model_membership(at, deep_rows, model, identity)
        return self._union(at, base_at_point, inherited)

    def _flat_self_tuple(
        self,
        key: Key,
        at: At,
        bound: Bound,
        *,
        depth_possible: bool,
    ) -> Q | None:
        """Flat id sets for a linear stored parent relation.

        This deliberately applies only when the base is a positive union of
        stored relations, so every base holder has a tuple resource row.
        Other cycles retain the general bounded compiler.
        """
        definition = self.schema.get_definition(key[0])
        permission = self.schema.get_permission(*key)
        if definition is None or permission is None:
            return None
        if self._tagged_inside(permission.expression):
            return None
        expr = permission.expression
        if not isinstance(expr, PermBinOp) or expr.op != "+":
            return None
        arm: PermArrow | None = None
        base: PermExpr | None = None
        for candidate, remaining in ((expr.left, expr.right), (expr.right, expr.left)):
            if isinstance(candidate, PermArrow) and candidate.target == key[1]:
                arm, base = candidate, remaining
                break
        if arm is None or base is None:
            return None

        def tuple_base(node: PermExpr) -> bool:
            if isinstance(node, PermRef):
                return any(r.name == node.name and r.backing is None for r in definition.relations)
            return (
                isinstance(node, PermBinOp)
                and node.op == "+"
                and tuple_base(node.left)
                and tuple_base(node.right)
            )

        relation = next((r for r in definition.relations if r.name == arm.via), None)
        if (
            relation is None
            or relation.backing is not None
            or len(relation.allowed_subjects) != 1
            or relation.allowed_subjects[0].type != key[0]
            or relation.allowed_subjects[0].relation
            or relation.allowed_subjects[0].id
            or relation.allowed_subjects[0].wildcard
            or not tuple_base(base)
        ):
            return None
        from ..models import active_relationship_model

        tuples = cast(Any, active_relationship_model().objects.using(self.using)).index_projection()
        universe = tuples.filter(resource_type=key[0])
        base_wire = self._expression(
            definition,
            base,
            At(key[0], F("resource_id"), None, False),
            bound,
            {key: 1},
            depth_possible=depth_possible,
        )
        base_point = self._expression(
            definition, base, at, bound, {key: 1}, depth_possible=depth_possible
        )
        base_ids = universe.filter(base_wire).order_by().values("resource_id")
        edges = universe.filter(
            relation=relation.name,
            subject_type=key[0],
            subject_relation="",
            caveat_name=relation.allowed_subjects[0].with_caveat,
        ).exclude(subject_id="*")
        if relation.with_expiration:
            edges = edges.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=self.now))
        else:
            edges = edges.filter(expires_at__isnull=True)
        if self.verdicts is not None:
            edges = edges.filter(self.verdicts.condition_q("", bound))
        elif bound is Bound.LOWER:
            edges = edges.filter(caveat_name="")
        branches: list[QuerySet[Any]] = []
        current = base_ids
        for _ in range(self.depth_limit):
            current = (
                edges.filter(subject_id__in=Subquery(current)).order_by().values("resource_id")
            )
            branches.append(current)
        if bound is Bound.UPPER and depth_possible:
            frontier = edges.order_by().values("resource_id")
            for _ in range(self.depth_limit):
                frontier = (
                    edges.filter(subject_id__in=Subquery(frontier)).order_by().values("resource_id")
                )
            branches.append(frontier)
        if not branches:
            return base_point
        converted = [self._tuple_identity_rows(at, rows) for rows in branches]
        return self._union(at, base_point, _in(at.ref, converted[0].union(*converted[1:])))

    def _expression(
        self,
        definition: Definition,
        expr: PermExpr,
        at: At,
        bound: Bound,
        visits: Mapping[Key, int],
        *,
        depth_possible: bool,
    ) -> Q:
        type_ = definition.resource_type
        if isinstance(expr, PermNil):
            result = _truth(False)
        elif isinstance(expr, PermRef):
            if expr.name in {"authenticated", "anonymous"}:
                result = _truth(builtin_actor_matches(expr.name, self.actor))
            else:
                result = self._holds(
                    (type_, expr.name), at, bound, visits, depth_possible=depth_possible
                )
        elif isinstance(expr, PermArrow):
            relation = next((r for r in definition.relations if r.name == expr.via), None)
            result = (
                self._relation(
                    definition,
                    relation,
                    at,
                    bound,
                    visits,
                    target=expr.target,
                    depth_possible=depth_possible,
                )
                if relation is not None
                else _truth(False)
            )
        elif isinstance(expr, PermBinOp):
            left = self._expression(
                definition, expr.left, at, bound, visits, depth_possible=depth_possible
            )
            tag = self.tagged.sites.get(id(expr)) if self.tagged is not None else None
            live = _before(self.now, tag.deadline) if tag is not None and tag.deadline else None
            if expr.op == "+":
                right = self._expression(
                    definition, expr.right, at, bound, visits, depth_possible=depth_possible
                )
                result = self._union(at, left, right)
            elif expr.op == "&":
                right = self._expression(
                    definition, expr.right, at, bound, visits, depth_possible=depth_possible
                )
                result = left & (right | ~live if live is not None else right)
            elif expr.op == "-":
                anti = self._anti_expression(
                    definition,
                    expr.right,
                    at,
                    bound.opposite(),
                    visits,
                    depth_possible=depth_possible,
                )
                result = left & (anti | ~live if live is not None else anti)
            else:
                raise SchemaError(f"Unknown permission operator {expr.op!r}")
        else:
            raise TypeError(f"Unknown permission expression {type(expr).__name__}")
        arm = self.tagged.arms.get(id(expr)) if self.tagged is not None else None
        return (
            result & _before(self.now, arm.deadline) if arm is not None and arm.deadline else result
        )

    def _anti_expression(
        self,
        definition: Definition,
        expr: PermExpr,
        at: At,
        bound: Bound,
        visits: Mapping[Key, int],
        *,
        depth_possible: bool,
    ) -> Q:
        """De Morgan's rule gives one anti-join per disjunct, never NOT IN."""
        if isinstance(expr, PermBinOp) and expr.op == "+":
            return self._anti_expression(
                definition, expr.left, at, bound, visits, depth_possible=depth_possible
            ) & self._anti_expression(
                definition, expr.right, at, bound, visits, depth_possible=depth_possible
            )

        if at.row:
            model = model_for_resource_type(at.resource_type)
            if model is None:
                return _truth(True)
            identity = resource_id_attr(model)
            positive = self._expression(
                definition, expr, at, bound, visits, depth_possible=depth_possible
            )
            rows = model._base_manager.using(self.using).filter(
                positive, **{identity: OuterRef(identity)}
            )
        else:
            from ..models.generation import SchemaGeneration

            ref = OuterRef(vars(at.ref)["name"]) if isinstance(at.ref, F) else at.ref
            positive = self._expression(
                definition,
                expr,
                At(at.resource_type, ref, at.key, False),
                bound,
                visits,
                depth_possible=depth_possible,
            )
            rows = SchemaGeneration.objects.using(self.using).filter(pk=1).filter(positive)
        return ~Q(Exists(rows))

    def _union(self, at: At, left: Q, right: Q) -> Q:
        """Normalize a row disjunction to one semi-join over a UNION of ids."""
        model = model_for_resource_type(at.resource_type)
        if not at.row or model is None:
            return left | right
        identity = resource_id_attr(model)
        source = model._base_manager.using(self.using)
        left_ids = source.filter(left).order_by().values_list(identity, flat=True)
        right_ids = source.filter(right).order_by().values_list(identity, flat=True)
        return _in(at.ref, left_ids.union(right_ids))

    def _relation(
        self,
        definition: Definition,
        relation: Relation,
        at: At,
        bound: Bound,
        visits: Mapping[Key, int],
        *,
        target: str | None = None,
        depth_possible: bool,
    ) -> Q:
        if isinstance(relation.backing, FieldBinding):
            resolved = resolve_field_backing(definition, relation)
            if resolved is None:
                raise SchemaError(
                    f"Invalid field backing {definition.resource_type}#{relation.name}"
                )
            return self._field_relation(resolved, at, bound, visits, target, depth_possible)
        if isinstance(relation.backing, ConstBinding):
            resolved_const = resolve_const_backing(definition, relation)
            if resolved_const is None:
                raise SchemaError(
                    f"Invalid constant backing {definition.resource_type}#{relation.name}"
                )
            return self._const_relation(resolved_const, at, bound, visits, target, depth_possible)
        if isinstance(relation.backing, AttributeBinding):
            resolved_attr = resolve_attribute_backing(definition, relation)
            if resolved_attr is None:
                raise SchemaError(
                    f"Invalid attribute backing {definition.resource_type}#{relation.name}"
                )
            return self._attribute_relation(
                resolved_attr, relation, at, bound, visits, target, depth_possible
            )
        return self._stored_relation(
            definition, relation, at, bound, visits, target, depth_possible
        )

    def _target_membership(
        self,
        target_type: str,
        relation: str,
        model: type[models.Model],
        identity: str,
        bound: Bound,
        visits: Mapping[Key, int],
        depth_possible: bool,
    ) -> QuerySet[Any]:
        _, field = model_identity_fields(model, identity)
        target_at = At(target_type, F(identity), field, True)
        return model._base_manager.using(self.using).filter(
            self._holds(
                (target_type, relation), target_at, bound, visits, depth_possible=depth_possible
            )
        )

    def _field_relation(
        self,
        resolved: ResolvedFieldBacking,
        at: At,
        bound: Bound,
        visits: Mapping[Key, int],
        target: str | None,
        depth_possible: bool,
    ) -> Q:
        allowed = resolved.relation.allowed_subjects[0]
        if allowed.relation and not self._is_relation(allowed.type, allowed.relation):
            return _truth(False)
        target_path = resolved.target_values_path()
        if target is not None or allowed.relation:
            target_name = target if target is not None else allowed.relation
            targets = self._target_membership(
                allowed.type,
                target_name,
                resolved.target_model,
                resolved.target_id_attr,
                bound,
                visits,
                depth_possible,
            )
            if (
                target is None
                and self.actor.subject_type == allowed.type
                and self.actor.optional_relation == allowed.relation
                and identity_codec(resolved.target_model, resolved.target_id_attr).is_canonical(
                    self.actor.subject_id, using=self.using
                )
            ):
                targets = resolved.target_model._base_manager.using(self.using).filter(
                    self._holds(
                        (allowed.type, target_name),
                        At(
                            allowed.type,
                            F(resolved.target_id_attr),
                            model_identity_fields(resolved.target_model, resolved.target_id_attr)[
                                1
                            ],
                            True,
                        ),
                        bound,
                        visits,
                        depth_possible=depth_possible,
                    )
                    | Q(**{resolved.target_id_attr: self.actor.subject_id})
                )
            source_q = Q(
                **{
                    f"{target_path}__in": Subquery(
                        targets.order_by().values(resolved.target_id_attr)
                    )
                }
            )
        elif self.actor.subject_type == allowed.type and not self.actor.optional_relation:
            if not identity_codec(resolved.target_model, resolved.target_id_attr).is_canonical(
                self.actor.subject_id, using=self.using
            ):
                return _truth(False)
            source_q = Q(**{target_path: self.actor.subject_id})
        else:
            return _truth(False)
        source_q &= Q(**resolved.filters)
        if at.row:
            return source_q
        rows = resolved.source_model._base_manager.using(self.using).filter(source_q)
        return self._model_membership(at, rows, resolved.source_model, resolved.source_id_attr)

    def _const_relation(
        self,
        resolved: ResolvedConstBacking,
        at: At,
        bound: Bound,
        visits: Mapping[Key, int],
        target: str | None,
        depth_possible: bool,
    ) -> Q:
        allowed = resolved.relation.allowed_subjects[0]
        if allowed.relation and not self._is_relation(allowed.type, allowed.relation):
            return _truth(False)
        if target is not None or allowed.relation:
            name = target if target is not None else allowed.relation
            target_q = self._holds(
                (allowed.type, name),
                At(allowed.type, Value(resolved.target_id), None, False),
                bound,
                visits,
                depth_possible=depth_possible,
            )
            if target is None and self.actor == SubjectRef.of(
                allowed.type, resolved.target_id, allowed.relation
            ):
                target_q = _truth(True)
        else:
            target_q = _truth(
                self.actor.subject_type == allowed.type
                and self.actor.subject_id == resolved.target_id
                and not self.actor.optional_relation
            )
        if not resolved.filters:
            return target_q
        local_q = Q(**resolved.filters)
        if at.row:
            return local_q & target_q
        rows = resolved.source_model._base_manager.using(self.using).filter(local_q)
        return (
            self._model_membership(at, rows, resolved.source_model, resolved.source_id_attr)
            & target_q
        )

    def _attribute_relation(
        self,
        resolved: ResolvedAttributeBacking,
        relation: Relation,
        at: At,
        bound: Bound,
        visits: Mapping[Key, int],
        target: str | None,
        depth_possible: bool,
    ) -> Q:
        allowed = relation.allowed_subjects[0]
        if allowed.relation and not self._is_relation(allowed.type, allowed.relation):
            return _truth(False)
        if target is not None or allowed.relation:
            name = target if target is not None else allowed.relation
            subjects = self._target_membership(
                allowed.type,
                name,
                resolved.target_model,
                resolved.target_id_attr,
                bound,
                visits,
                depth_possible,
            )
            if (
                target is None
                and self.actor.subject_type == allowed.type
                and self.actor.optional_relation == allowed.relation
                and identity_codec(resolved.target_model, resolved.target_id_attr).is_canonical(
                    self.actor.subject_id, using=self.using
                )
            ):
                subjects = resolved.target_model._base_manager.using(self.using).filter(
                    self._holds(
                        (allowed.type, name),
                        At(
                            allowed.type,
                            F(resolved.target_id_attr),
                            model_identity_fields(resolved.target_model, resolved.target_id_attr)[
                                1
                            ],
                            True,
                        ),
                        bound,
                        visits,
                        depth_possible=depth_possible,
                    )
                    | Q(**{resolved.target_id_attr: self.actor.subject_id})
                )
        elif self.actor.subject_type == allowed.type and not self.actor.optional_relation:
            if not identity_codec(resolved.target_model, resolved.target_id_attr).is_canonical(
                self.actor.subject_id, using=self.using
            ):
                return _truth(False)
            subjects = resolved.target_model._base_manager.using(self.using).filter(
                **{resolved.target_id_attr: self.actor.subject_id}
            )
        else:
            return _truth(False)
        subjects = subjects.filter(Q(**resolved.filters))
        if resolved.resource is not None:
            subjects = subjects.filter(**{resolved.field.name: resolved.value})
            derived = self._wire_equal(at, resolved.resource) & Q(Exists(subjects))
            fallback = ~self._wire_equal(at, resolved.resource)
            definition = self.schema.get_definition(at.resource_type)
            assert definition is not None
            return self._union(
                at,
                derived,
                fallback
                & self._stored_relation(
                    definition, relation, at, bound, visits, target, depth_possible
                ),
            )
        field = resolved.field
        if isinstance(field, (models.CharField, models.TextField)):
            values = subjects.exclude(**{field.name: ""}).exclude(**{f"{field.name}__isnull": True})
            return _in(at.ref, values.order_by().values(field.name))
        # Convert the wire identity, not the indexed model column.
        if at.key is None:
            converted = identity_codec(resolved.target_model, field.name).to_column(
                cast(Expression, at.ref)
            )
            return _in(
                converted,
                subjects.exclude(**{f"{field.name}__isnull": True}).order_by().values(field.name),
            )
        return _in(
            at.ref,
            subjects.exclude(**{f"{field.name}__isnull": True}).order_by().values(field.name),
        )

    def _stored_relation(
        self,
        definition: Definition,
        relation: Relation,
        at: At,
        bound: Bound,
        visits: Mapping[Key, int],
        target: str | None,
        depth_possible: bool,
    ) -> Q:
        from ..models import active_relationship_model

        rows = cast(Any, active_relationship_model().objects.using(self.using)).index_projection()
        rows = rows.filter(resource_type=definition.resource_type, relation=relation.name)
        if relation.with_expiration:
            rows = rows.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=self.now))
        else:
            rows = rows.filter(expires_at__isnull=True)
        admitted = _truth(False)
        for allowed in relation.allowed_subjects:
            if allowed.relation and not self._is_relation(allowed.type, allowed.relation):
                continue
            shape = Q(
                subject_type=allowed.type,
                subject_relation=allowed.relation,
                caveat_name=allowed.with_caveat,
            )
            if allowed.wildcard:
                shape &= Q(subject_id="*")
            else:
                shape &= ~Q(subject_id="*")
                if allowed.id:
                    shape &= Q(subject_id=allowed.id)
            if target is not None:
                member = self._holds(
                    (allowed.type, target),
                    At(allowed.type, F("subject_id"), None, False),
                    bound,
                    visits,
                    depth_possible=depth_possible,
                )
            elif allowed.relation:
                expanded = self._holds(
                    (allowed.type, allowed.relation),
                    At(allowed.type, F("subject_id"), None, False),
                    bound,
                    visits,
                    depth_possible=depth_possible,
                )
                exact = _truth(
                    self.actor.subject_type == allowed.type
                    and self.actor.optional_relation == allowed.relation
                ) & Q(subject_id=self.actor.subject_id)
                member = expanded | exact
            elif allowed.wildcard:
                member = _truth(
                    self.actor.subject_type == allowed.type and not self.actor.optional_relation
                )
            else:
                member = _truth(
                    self.actor.subject_type == allowed.type
                    and (not allowed.id or self.actor.subject_id == allowed.id)
                    and not self.actor.optional_relation
                ) & Q(subject_id=self.actor.subject_id)
            admitted |= shape & member
        rows = rows.filter(admitted)
        if self.verdicts is not None:
            rows = rows.filter(self.verdicts.condition_q("", bound))
        elif bound is Bound.LOWER:
            rows = rows.filter(caveat_name="")
        return self._tuple_membership(at, rows)

    def _is_relation(self, type_: str, name: str) -> bool:
        definition = self.schema.get_definition(type_)
        return definition is not None and any(r.name == name for r in definition.relations)

    def _tuple_membership(self, at: At, rows: QuerySet[Any]) -> Q:
        return _in(at.ref, self._tuple_identity_rows(at, rows))

    def _tuple_identity_rows(self, at: At, rows: QuerySet[Any]) -> QuerySet[Any]:
        model = model_for_resource_type(at.resource_type)
        identity = resource_id_attr(model) if model is not None else None
        if model is None:
            subject_model = model_for_subject_type(at.resource_type)
            if subject_model is not None:
                model, identity = subject_model
        if at.key is None or model is None:
            return rows.exclude(resource_id__isnull=True).order_by().values("resource_id")
        assert identity is not None
        _, identity_field = model_identity_fields(model, identity)
        converted = rows.annotate(
            _rebac_native_id=identity_codec(model, identity).to_column("resource_id")
        ).exclude(_rebac_native_id__isnull=True)
        if at.key is identity_field:
            return cast(QuerySet[Any], converted.order_by().values("_rebac_native_id"))
        bridge = model._base_manager.using(self.using).filter(
            **{f"{identity}__in": Subquery(converted.order_by().values("_rebac_native_id"))}
        )
        return cast(QuerySet[Any], bridge.order_by().values(at.key.name))

    def _model_membership(
        self,
        at: At,
        rows: QuerySet[Any],
        model: type[models.Model],
        identity: str,
    ) -> Q:
        native = rows.order_by().values(identity)
        if at.key is None:
            # The model column remains native. Convert a wire reference when
            # the identity originates in a tuple or a constant.
            return _in(identity_codec(model, identity).to_column(cast(Expression, at.ref)), native)
        _, canonical = model_identity_fields(model, identity)
        if at.key is canonical:
            return _in(at.ref, native)
        bridge = model._base_manager.using(self.using).filter(
            **{f"{identity}__in": Subquery(native)}
        )
        return _in(at.ref, bridge.order_by().values(at.key.name))

    def _wire_equal(self, at: At, wire_id: str) -> Q:
        if at.key is None:
            return Q(Exact(at.ref, Value(wire_id)))
        model = model_for_resource_type(at.resource_type)
        if model is None:
            return _truth(False)
        _, canonical = model_identity_fields(model, resource_id_attr(model))
        if at.key is canonical:
            return Q(Exact(at.ref, identity_codec(model).to_column(Value(wire_id))))
        target = (
            model._base_manager.using(self.using)
            .filter(**{resource_id_attr(model): wire_id})
            .order_by()
            .values(at.key.name)
        )
        return _in(at.ref, target)
