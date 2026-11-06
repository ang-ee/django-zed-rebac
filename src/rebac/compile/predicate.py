"""Compile schema permission membership into Django ORM predicates.

No application resource IDs are read into Python.  A predicate evaluates at
an identity expression, so a tuple-only object can participate in both grants
and exclusions without a corresponding model row.

Three rules shape every statement:

* a disjunction over a row is one ``id IN (union of id sets)``, never ``OR``
  over subqueries, and a negated disjunction is a conjunction of ``NOT
  EXISTS``;
* an arm over a multi-valued path is its own subquery, so two arms never share
  a join;
* an uncorrelated id set is compiled by Django once and embedded as text
  (``_Compiled``), so building a statement does not re-resolve its subqueries
  at every level.  This is the accepted plan-cache exception of ARCHITECTURE:
  the SQL is Django's own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Protocol, cast

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db import connections, models
from django.db.models import Exists, Expression, F, OuterRef, Q, QuerySet, Value
from django.db.models.expressions import Col
from django.db.models.fields.related_lookups import RelatedExact, RelatedIn
from django.db.models.lookups import Exact, In, IntegerFieldExact, IsNull, LessThan
from django_cte import CTE, with_cte

from .._id import _NATIVE_INTEGER_FIELDS, model_identity_fields, resource_id_attr
from ..actors import is_anonymous_actor
from ..codec import identity_codec
from ..composition import TaggedComposition
from ..conf import app_settings
from ..errors import SchemaError
from ..field_backing import (
    ResolvedAttributeBacking,
    ResolvedConstBacking,
    ResolvedFieldBacking,
    _content_type,
    _relation_path,
    _resolve_field_backing,
    resolve_attribute_backing,
    resolve_const_backing,
    resolve_field_backing,
)
from ..resources import model_for_resource_type, model_for_subject_type
from ..schema.ast import (
    AllowedSubject,
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
from ..types import SubjectRef
from .program import CompileProgram, Key

# Markers kept beside the visit counts of a path through the policy.
_HOPS: Key = ("#hops", "")


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


# ---------- Constants and two-valued connectives ----------

_TRUE: Final = Q(Value(True, output_field=models.BooleanField()))
_FALSE: Final = Q(Value(False, output_field=models.BooleanField()))


def _truth(value: bool) -> Q:
    return _TRUE if value else _FALSE


def is_true(q: Q) -> bool:
    return q is _TRUE


def is_false(q: Q) -> bool:
    return q is _FALSE


def _or(*parts: Q) -> Q:
    kept = [part for part in parts if part is not _FALSE]
    if any(part is _TRUE for part in kept):
        return _TRUE
    if not kept:
        return _FALSE
    result = kept[0]
    for part in kept[1:]:
        result = result | part
    return result


def _and(*parts: Q) -> Q:
    kept = [part for part in parts if part is not _TRUE]
    if any(part is _FALSE for part in kept):
        return _FALSE
    if not kept:
        return _TRUE
    result = kept[0]
    for part in kept[1:]:
        result = result & part
    return result


def _not(q: Q) -> Q:
    if q is _TRUE:
        return _FALSE
    if q is _FALSE:
        return _TRUE
    return ~q


def _one_level_out(ref: Any) -> Any:
    """``ref`` as a subquery one level deeper sees it.

    A column of the enclosing query becomes an ``OuterRef``, and an
    ``OuterRef`` already reaching out reaches one level further
    (``OuterRef(OuterRef(...))``): an exclusion inside an exclusion nests one
    subquery inside another.  References inside an expression are moved the
    same way; a value is left as it is.
    """
    if isinstance(ref, OuterRef):
        return OuterRef(ref)
    if isinstance(ref, F):
        return OuterRef(vars(ref)["name"])
    sources = getattr(ref, "get_source_expressions", None)
    parts: list[Any] = list(cast(Iterable[Any], sources())) if callable(sources) else []
    if not parts:
        return ref
    moved = ref.copy()
    moved.set_source_expressions([None if part is None else _one_level_out(part) for part in parts])
    return moved


@dataclass(frozen=True)
class PointRow:
    """The content types a point check's row has in its GenericForeignKey columns.

    ``ref`` is the statement's reference to the row; ``types`` maps each
    content-type column's attname to its value.  An arm backed by one of those
    columns names a row of its own type only, so on that row the arm of any
    other type cannot hold and is not compiled.
    """

    ref: Any
    types: tuple[tuple[str, Any], ...]

    def may_name(self, resolved: ResolvedFieldBacking, at: At, using: str) -> bool:
        if resolved.generic is None or at.ref is not self.ref:
            return True
        column = resolved.source_model._meta.get_field(resolved.generic.ct_field)
        stored = dict(self.types).get(cast(Any, column).attname, _ABSENT)
        if stored is _ABSENT:
            return True
        content_type = _content_type(resolved.target_model, using)
        return content_type is not None and content_type.pk == stored


_ABSENT: Any = object()


def _not_null(expr: Expression | F | OuterRef) -> Q:
    return Q(IsNull(expr, False))


def _before(now: Expression, deadline: datetime) -> Q:
    return Q(LessThan(now, Value(deadline, output_field=models.DateTimeField())))


class _Compiled(Expression):
    """An uncorrelated id set: compiled by Django once, embedded as text.

    Django resolves a ``Subquery`` again inside every enclosing ``filter()``,
    which makes the cost of building a nested statement grow with its depth
    times its size.  The rows here never refer to an outer query, so their own
    compiler produces the same SQL the nested resolution would.
    """

    subquery = True

    def __init__(self, rows: QuerySet[Any]) -> None:
        super().__init__(output_field=models.TextField())
        self.rows = rows
        self._sql: dict[str, tuple[str, tuple[Any, ...]]] = {}

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        cached = self._sql.get(connection.alias)
        if cached is None:
            query = self.rows.query.clone()
            query.subquery = True
            # An id set has no order; Django 6.1 applies a model's default
            # ordering to a combined query, which an id projection cannot sort by.
            query.clear_ordering(force=True)
            sql, params = query.as_sql(compiler, connection)
            cached = self._sql[connection.alias] = sql, tuple(params)
        return cached


def _one_object(ref: Any) -> bool:
    """The reference is a statement constant: a value or a bound parameter."""
    return isinstance(ref, (Value, _Param))


def _in(expr: Expression | F | OuterRef, rows: QuerySet[Any]) -> Q:
    """Two-valued membership: a NULL reference is not a member."""
    return _not_null(expr) & Q(In(expr, _Compiled(rows)))


# ---------- The actor ----------


class _Marker:
    __slots__ = ("label",)

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"<{self.label}>"


ACTOR_WIRE: Final = _Marker("actor wire id")
CLOCK: Final = _Marker("clock")


@dataclass(frozen=True, slots=True)
class ActorNative:
    """Placeholder for the actor's id as a value of ``field``."""

    field: models.Field[Any, Any]


class _Param(Expression):
    """One statement parameter whose value is bound when the statement runs."""

    def __init__(self, marker: object, output_field: models.Field[Any, Any]) -> None:
        super().__init__(output_field=output_field)
        self.marker = marker

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        return "%s", (self.marker,)


def statement_now() -> datetime:
    """The instant a statement is read at: the application clock, never the database's."""
    from ..clock import application_now

    return application_now()


def clock() -> Expression:
    """The statement's instant, as one parameter bound when the statement runs."""
    return _Param(CLOCK, models.DateTimeField())


def bind(params: Iterable[Any], actor: SubjectRef, connection: Any) -> list[Any]:
    """Replace the actor and clock placeholders of a parametric statement.

    Every occurrence of the clock in one statement gets the same instant: a
    fact that appears under both polarities must not be read at two times.
    """
    bound: list[Any] = []
    now: Any = None
    for param in params:
        if param is ACTOR_WIRE:
            bound.append(actor.subject_id)
        elif param is CLOCK:
            if now is None:
                now = models.DateTimeField().get_db_prep_value(statement_now(), connection, False)
            bound.append(now)
        elif isinstance(param, ActorNative):
            field = param.field
            bound.append(
                field.get_db_prep_value(field.to_python(actor.subject_id), connection, False)
            )
        else:
            bound.append(param)
    return bound


@dataclass(frozen=True, slots=True)
class ActorShape:
    """Everything about the actor that can change the shape of a statement.

    The id itself is a parameter.  ``named_id`` is the id only when the schema
    names it (an allowed subject with an id, a constant target); ``canonical``
    says whether the id is a valid identity of the actor type's own model.
    """

    type: str
    relation: str
    canonical: bool
    named_id: str | None
    authenticated: bool
    anonymous: bool


def named_ids(schema: Schema, type_: str) -> frozenset[str]:
    found: set[str] = set()
    for definition in schema.definitions:
        for relation in definition.relations:
            for allowed in relation.allowed_subjects:
                if allowed.type != type_:
                    continue
                if allowed.id:
                    found.add(allowed.id)
                if isinstance(relation.backing, ConstBinding):
                    found.add(relation.backing.target_id)
    return frozenset(found)


def actor_shape(schema: Schema, actor: SubjectRef, using: str) -> ActorShape:
    target = model_for_subject_type(actor.subject_type)
    canonical = False
    if target is not None:
        try:
            canonical = identity_codec(target[0], target[1]).is_canonical(
                actor.subject_id, using=using
            )
        except SchemaError:
            canonical = False
    anonymous = is_anonymous_actor(actor)
    return ActorShape(
        actor.subject_type,
        actor.optional_relation,
        canonical,
        actor.subject_id if actor.subject_id in named_ids(schema, actor.subject_type) else None,
        not anonymous and actor.subject_id != "",
        anonymous,
    )


def _multi_valued(model: type[models.Model], lookup: str) -> bool:
    """Whether a lookup path crosses a reverse or many-to-many relation."""
    current: Any = model
    for part in lookup.split("__"):
        try:
            field = current._meta.pk if part == "pk" else current._meta.get_field(part)
        except FieldDoesNotExist:
            return False
        if not field.is_relation:
            return False
        if field.many_to_many or field.one_to_many:
            return True
        current = field.related_model
        if not isinstance(current, type):
            return False
    return False


class Compiler:
    """One predicate compiler for scopes and point membership.

    The caller supplies the same effective schema for every bound of an
    operation.  Caveat verdicts are prepared by the read operation, then
    injected here; the SQL still witnesses each matching tuple at execution.

    ``parametric`` compiles the actor's id as a placeholder (see ``bind``), so
    the statement depends only on the actor's shape and can be kept.
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
        program: CompileProgram | None = None,
        parametric: bool = False,
        point: PointRow | None = None,
    ) -> None:
        self.schema = tagged.schema if tagged is not None else schema
        self.actor = actor
        self.using = using
        self.tagged = tagged
        self.verdicts = verdicts
        # The application clock, never the database's.  A parametric
        # statement binds it when it runs; any other reads it now.
        self.now = (
            now
            if now is not None
            else clock()
            if parametric
            else Value(statement_now(), output_field=models.DateTimeField())
        )
        self.depth_limit = app_settings.REBAC_DEPTH_LIMIT if depth_limit is None else depth_limit
        self.program = program if program is not None else CompileProgram.build(self.schema)
        self.parametric = parametric
        self.point = point
        self.shape = actor_shape(self.schema, actor, using)
        self._memo: dict[tuple[Any, ...], Q] = {}
        self._own_identity = model_for_subject_type(actor.subject_type)
        self._cte_tables: set[str] | None = None

    def _cte_name(self, name: str) -> str:
        """A CTE name that no model table of the project already uses."""
        if self._cte_tables is None:
            self._cte_tables = {
                owner._meta.db_table for owner in apps.get_models(include_auto_created=True)
            }
        while name in self._cte_tables:
            name += "_"
        return name

    def _cte_queryset(self, cte: CTE) -> QuerySet[Any]:
        selected = cte.queryset()
        return QuerySet(model=selected.model, query=selected.query, using=self.using)

    # ---------- The actor's id, as a value or as a placeholder ----------

    def _wire(self) -> Any:
        if self.parametric:
            return _Param(ACTOR_WIRE, models.TextField())
        return self.actor.subject_id

    def _native(self, model: type[models.Model], attr: str) -> Any:
        if self.parametric:
            _, field = model_identity_fields(model, attr)
            return _Param(ActorNative(field), field)
        return self.actor.subject_id

    def _canonical(self, model: type[models.Model], attr: str) -> bool:
        if self._own_identity is not None and self._own_identity == (model, attr):
            return self.shape.canonical
        return identity_codec(model, attr).is_canonical(self.actor.subject_id, using=self.using)

    # ---------- Expression structure ----------

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

    # ---------- Entry points ----------

    def holds(self, key: Key, at: At, bound: Bound = Bound.LOWER) -> Q:
        if key[0] != at.resource_type:
            raise ValueError(f"{key!r} cannot be evaluated at {at.resource_type!r}")
        return self._holds(key, at, bound, {}, depth_possible=True)

    def has_recursion(self, key: Key) -> bool:
        return bool(self.program.reachable(key) & self.program.recursive)

    def depth_unknown(self, key: Key, at: At) -> Q:
        """Whether an undecided case is undecided only because a cycle was cut.

        The caller has established that the upper bound holds and the lower
        one does not.  A recursive frontier may have widened the upper bound
        in a positive position, or narrowed the lower one after a subtraction
        swapped the bound: without the frontier the upper bound fails, or the
        lower one holds.
        """
        if not self.has_recursion(key):
            return _FALSE
        upper_without_depth = self._holds(key, at, Bound.UPPER, {}, depth_possible=False)
        lower_without_depth = self._holds(key, at, Bound.LOWER, {}, depth_possible=False)
        return _or(_not(upper_without_depth), lower_without_depth)

    # ---------- Nodes ----------

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
            return _FALSE
        memo = (key, at, bound, frozenset(visits.items()), depth_possible)
        found = self._memo.get(memo)
        if found is None:
            found = self._memo[memo] = self._node(
                key, at, bound, visits, depth_possible=depth_possible
            )
        return found

    def _node(
        self,
        key: Key,
        at: At,
        bound: Bound,
        visits: Mapping[Key, int],
        *,
        depth_possible: bool,
    ) -> Q:
        count = visits.get(key, 0)
        hops = visits.get(_HOPS, 0)
        seen_at: Key = ("#seen", f"{key[0]}#{key[1]}")
        if count and (key in self.program.alias_cycles or visits.get(seen_at) == hops):
            # The node was already entered at this identity: no relation was
            # followed since.  Its positive least fixed point has no new path
            # on a revisit, in either bound.
            return _FALSE
        recursive = key in self.program.recursive
        if count == 0 and recursive:
            for flatten in (
                self._flat_self_userset,
                self._flat_self_fk,
                self._flat_self_path,
                self._flat_self_tuple,
            ):
                flattened = flatten(key, at, bound, depth_possible=depth_possible)
                if flattened is not None:
                    return flattened
        updated = dict(visits)
        if recursive:
            # The bound counts the relations followed since the component was
            # entered, whichever of its nodes they pass through.
            first = min(self.program.components[key])
            entered: Key = ("#entered", f"{first[0]}#{first[1]}")
            if hops - updated.setdefault(entered, hops) > self.depth_limit:
                return _and(_truth(bound is Bound.UPPER and depth_possible), _not_null(at.ref))
        if count > len(self.program.dependencies) + self.depth_limit + 1:
            raise SchemaError(f"Permission graph did not terminate at {key!r}")
        updated[key] = count + 1
        updated[seen_at] = hops
        definition = self.schema.get_definition(key[0])
        if definition is None:
            return _FALSE
        relation = next((r for r in definition.relations if r.name == key[1]), None)
        if relation is not None:
            return self._relation(
                definition, relation, at, bound, updated, depth_possible=depth_possible
            )
        permission = self.schema.get_permission(*key)
        if permission is None:
            return _FALSE
        return self._expression(
            definition,
            permission.expression,
            at,
            bound,
            updated,
            depth_possible=depth_possible,
        )

    # ---------- Recursion: flat forms ----------

    def _tuples(self) -> Any:
        from ..models import active_relationship_model

        return cast(Any, active_relationship_model().objects.using(self.using)).wire_projection()

    def _live_q(self, relation: Relation, bound: Bound) -> Q:
        """A tuple row is not expired and its caveat is one this bound admits."""
        live = Q(expires_at__isnull=True)
        if relation.with_expiration:
            live |= Q(expires_at__gt=self.now)
        if self.verdicts is not None:
            return live & self.verdicts.condition_q("", bound)
        if bound is Bound.LOWER:
            return live & Q(caveat_name="")
        return live

    def _live(self, rows: Any, relation: Relation, bound: Bound) -> Any:
        return rows.filter(self._live_q(relation, bound))

    def _closure(self, seed: QuerySet[Any], edges: Any, hops: int) -> QuerySet[Any]:
        """The wire ids within ``hops`` edges of ``seed``, in a finite recursive CTE."""
        if hops <= 0:
            return seed

        def body(cte: CTE) -> QuerySet[Any]:
            first = seed.annotate(_rebac_depth=Value(0, output_field=models.IntegerField()))
            depth = models.ExpressionWrapper(
                cte.col._rebac_depth, output_field=models.IntegerField()
            )
            step = (
                cte.join(edges, subject_id=cte.col.resource_id)
                .order_by()
                .values("resource_id")
                .annotate(_rebac_depth=depth + Value(1))
                .filter(_rebac_depth__lte=hops)
                .values("resource_id", "_rebac_depth")
            )
            return cast(QuerySet[Any], first.union(step))

        cte = CTE.recursive(body, name=self._cte_name("rebac_closure"))
        return cast(
            QuerySet[Any], with_cte(cte, select=self._cte_queryset(cte).values("resource_id"))
        )

    def _sets_closure(self, seed: QuerySet[Any], edges: Any) -> QuerySet[Any]:
        """Actor-side stored subject sets to a fixed point, data cycles included.

        The union drops rows already reached, so the recursion ends when no
        new set appears.
        """

        def body(cte: CTE) -> QuerySet[Any]:
            step = cte.join(edges, subject_id=cte.col.resource_id).order_by().values("resource_id")
            return seed.union(step)

        cte = CTE.recursive(body, name=self._cte_name("rebac_sets"))
        return cast(
            QuerySet[Any], with_cte(cte, select=self._cte_queryset(cte).values("resource_id"))
        )

    def _not_converged(self, edges: Any, closure: QuerySet[Any], known: Q) -> Q:
        """Whether one more hop reaches an object the closure does not hold.

        When it does not, the closure is complete, data cycles included, and
        the upper bound equals the lower one.
        """
        beyond = edges.filter(subject_id__in=_Compiled(closure)).exclude(
            resource_id__in=_Compiled(closure)
        )
        if known is not _FALSE:
            beyond = beyond.filter(_not(known))
        return Q(Exists(beyond))

    def _wire_membership(self, at: At, relation_rows: Any, closure: QuerySet[Any]) -> Q:
        """``at`` is one of the wire ids in ``closure``."""
        if at.key is None:
            return _in(at.ref, closure)
        return self._tuple_membership(at, relation_rows, Q(resource_id__in=_Compiled(closure)))

    def _self_arrows(
        self, key: Key, definition: Definition, arms: Sequence[PermExpr]
    ) -> list[PermArrow]:
        """The arms that lead back to ``key``: an arrow to the same permission
        through a relation that admits the key's own type.

        ``drive->read`` on a folder is not one of them, whatever its name: it
        reads another type.  A key that also recurses through other nodes has
        no flat form, so its component must be the key alone.
        """
        if self.program.components.get(key) != frozenset({key}):
            return []
        found = []
        for arm in arms:
            if not isinstance(arm, PermArrow) or arm.target != key[1]:
                continue
            relation = next((r for r in definition.relations if r.name == arm.via), None)
            if relation is not None and any(
                allowed.type == key[0] for allowed in relation.allowed_subjects
            ):
                found.append(arm)
        return found

    def _flat_self_userset(
        self,
        key: Key,
        at: At,
        bound: Bound,
        *,
        depth_possible: bool,
    ) -> Q | None:
        """A relation whose subject set is itself: the sets the actor is in.

        The closure starts from the sets that hold the actor directly and
        follows containment upward, so it is the actor's own and small.
        """
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
        every = self._tuples().filter(resource_type=key[0], relation=key[1])
        rows = self._live(every, relation, bound)
        direct = _FALSE
        for allowed in relation.allowed_subjects:
            shape = Q(
                subject_type=allowed.type,
                subject_relation=allowed.relation,
                caveat_name=allowed.with_caveat,
            )
            if allowed.wildcard:
                shape &= Q(subject_id="*")
                admitted = self.shape.type == allowed.type and not self.shape.relation
            else:
                shape &= ~Q(subject_id="*")
                if allowed.id:
                    shape &= Q(subject_id=allowed.id)
                admitted = (
                    self.shape.type == allowed.type
                    and self.shape.relation == allowed.relation
                    and (not allowed.id or self.shape.named_id == allowed.id)
                )
                shape &= Q(subject_id=self._wire())
            if admitted:
                direct = _or(direct, shape)
        if direct is _FALSE:
            return _FALSE
        hop = recursive[0]
        edges = rows.filter(
            subject_type=key[0],
            subject_relation=key[1],
            caveat_name=hop.with_caveat,
        ).exclude(subject_id="*")
        if hop.id:
            edges = edges.filter(subject_id=hop.id)
        closure = self._sets_closure(rows.filter(direct).order_by().values("resource_id"), edges)
        return self._wire_membership(at, every, closure)

    def _self_fk(
        self, key: Key
    ) -> (
        tuple[
            Definition,
            list[PermExpr],
            ResolvedFieldBacking,
            type[models.Model],
            str,
            models.Field[Any, Any],
        ]
        | None
    ):
        """The parts of ``p = base + parent->p`` over a self foreign key, if ``key`` is one."""
        definition = self.schema.get_definition(key[0])
        permission = self.schema.get_permission(*key)
        model = model_for_resource_type(key[0])
        if definition is None or permission is None or model is None:
            return None
        arms = self._union_arms(permission.expression)
        recursive = self._self_arrows(key, definition, arms)
        if len(recursive) != 1 or len(arms) < 2 or self._tagged_inside(recursive[0]):
            return None
        recursive_arm = recursive[0]
        base = [arm for arm in arms if arm is not recursive_arm]
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
        return definition, base, resolved, model, identity, field

    def _flat_self_fk(
        self,
        key: Key,
        at: At,
        bound: Bound,
        *,
        depth_possible: bool,
    ) -> Q | None:
        """A parent->same-permission cycle over a self foreign key.

        A row inherits when one of its ancestors holds the base.  The
        ancestors are a chain of joins on the parent column and the base is
        tested once, against all of them: the statement names the base once
        whatever the bound.  Tuple grants on a rowless object still
        contribute at depth zero.
        """
        parts_of = self._self_fk(key)
        if parts_of is None:
            return None
        definition, base, resolved, model, identity, field = parts_of
        row_at = At(key[0], F(identity), field, True)
        inside = {key: 1}

        def base_at(where: At) -> list[Q]:
            return self._arms(definition, base, where, bound, inside, depth_possible)

        own_row = at.row and model_for_resource_type(at.resource_type) is model and at.key is field
        # The holders are looked up by key, a row and its ancestors at a time,
        # so their base is a plain disjunction: each arm is decided once.
        base_row = _or(*base_at(row_at))
        if base_row is _TRUE:
            return _TRUE
        # An object without a row inherits nothing; it holds its own base only.
        parts = [] if own_row else base_at(at)
        if any(part is _TRUE for part in parts):
            return _TRUE
        source = model._base_manager.using(self.using)
        if base_row is not _FALSE:
            # The parent column holds a value of the key's target field, at
            # every level of the chain.  The chain starts at the row itself.
            parent = cast("models.ForeignKey[Any, Any]", resolved.field)
            target = parent.target_field.name
            chain = [
                OuterRef(target),
                *(
                    OuterRef("__".join([resolved.path] * (hops - 1) + [parent.attname]))
                    for hops in range(1, self.depth_limit + 1)
                ),
            ]
            # The lookup is spelled out: where the key is itself a relation
            # (the parent link of a multi-table child), Django's ``__in`` on
            # the field name converts every list entry as a plain value and
            # refuses an expression.
            ancestors = Q(In(F(target), chain))
            inherits = Q(Exists(source.filter(ancestors).filter(base_row)))
            if bound is Bound.UPPER and depth_possible:
                deep_path = "__".join([resolved.path] * (self.depth_limit + 1))
                inherits = inherits | Q(**{f"{deep_path}__isnull": False})
            if own_row:
                return inherits
            parts.append(self._model_membership(at, source.filter(inherits), model, identity))
        return self._union(at, parts)

    def _flat_self_path(
        self,
        key: Key,
        at: At,
        bound: Bound,
        *,
        depth_possible: bool,
    ) -> Q | None:
        """A parent->same-permission cycle over any backed path to the same model.

        A many-to-many or reverse path has no single ancestor chain, so the
        rows that inherit are a closure: the rows that hold the base, then
        the rows whose path reaches the level below.
        """
        definition = self.schema.get_definition(key[0])
        permission = self.schema.get_permission(*key)
        model = model_for_resource_type(key[0])
        if definition is None or permission is None or model is None:
            return None
        arms = self._union_arms(permission.expression)
        recursive = self._self_arrows(key, definition, arms)
        if len(recursive) != 1 or len(arms) < 2 or self._tagged_inside(recursive[0]):
            return None
        arm = recursive[0]
        base = [other for other in arms if other is not arm]
        relation = next((r for r in definition.relations if r.name == arm.via), None)
        if relation is None or not isinstance(relation.backing, FieldBinding):
            return None
        resolved = resolve_field_backing(definition, relation)
        if (
            resolved is None
            or resolved.generic is not None
            or resolved.source_model is not model
            or resolved.target_model is not model
            or resolved.relation.allowed_subjects[0].relation
        ):
            return None
        identity = resource_id_attr(model)
        _, field = model_identity_fields(model, identity)
        row_at = At(key[0], F(identity), field, True)
        inside = {key: 1}

        def base_at(where: At) -> list[Q]:
            return [
                self._expression(
                    definition, other, where, bound, inside, depth_possible=depth_possible
                )
                for other in base
            ]

        parts = base_at(at)
        if any(part is _TRUE for part in parts):
            return _TRUE
        base_row = self._union(row_at, base_at(row_at))
        if base_row is _FALSE:
            return self._union(at, parts)
        source = model._base_manager.using(self.using)
        seed = source.filter(base_row).order_by().values_list(identity, flat=True)
        target_path = resolved.target_values_path()

        def reaching(level: QuerySet[Any]) -> QuerySet[Any]:
            # Filters and the path share one filter call, hence one join.
            return (
                source.filter(Q(**resolved.filters) & Q(**{f"{target_path}__in": _Compiled(level)}))
                .order_by()
                .values_list(identity, flat=True)
            )

        if bound is Bound.LOWER and self._finite_path(model, identity, resolved, source):
            _, scalar = model_identity_fields(model, identity)

            def path_rows(cte: CTE) -> QuerySet[Any]:
                first = (
                    reaching(seed)
                    .values("pk")
                    .annotate(_rebac_depth=Value(1, output_field=models.IntegerField()))
                )
                previous = models.ExpressionWrapper(cte.col.pk, output_field=scalar)
                depth = models.ExpressionWrapper(
                    cte.col._rebac_depth, output_field=models.IntegerField()
                )
                step = (
                    cte.join(source, Q(**resolved.filters), **{target_path: previous})
                    .values("pk")
                    .annotate(_rebac_depth=depth + Value(1))
                    .filter(_rebac_depth__lte=self.depth_limit)
                    .order_by()
                    .values("pk", "_rebac_depth")
                )
                # Django restores a model's default ordering on a combinator
                # query; a CTE body carries none.
                return cast(QuerySet[Any], first.union(step).order_by())

            cte = CTE.recursive(path_rows, name=self._cte_name("rebac_path"))
            reached = self._cte_queryset(cte).values("pk")
            members = with_cte(cte, select=source.filter(pk__in=_Compiled(reached)))
            parts.append(self._model_membership(at, members, model, identity))
            return self._union(at, parts)
        closure = seed
        for _ in range(self.depth_limit - 1):
            closure = seed.union(reaching(closure))
        inherits = reaching(closure)
        parts.append(
            self._model_membership(
                at, source.filter(**{f"{identity}__in": _Compiled(inherits)}), model, identity
            )
        )
        if bound is Bound.UPPER and depth_possible:
            # One more hop that reaches a row outside the closure and its
            # inheritors means the closure has not converged.
            known = seed.union(inherits)
            beyond = reaching(inherits).exclude(**{f"{identity}__in": _Compiled(known)})
            parts.append(_and(Q(Exists(beyond)), _not_null(at.ref)))
        return self._union(at, parts)

    def _finite_path(
        self,
        model: type[models.Model],
        identity: str,
        resolved: ResolvedFieldBacking,
        source: QuerySet[Any],
    ) -> bool:
        """Whether a lower-bound path recursion lowers to a finite recursive CTE.

        The CTE carries the row's integer primary key and joins it back to the
        path's column, so both must be plain columns of stock integer fields
        compared by Django's own lookups, reached through forward keys only.
        """
        query_field, scalar = model_identity_fields(model, identity)
        if not (
            self.depth_limit > 1
            and query_field is model._meta.pk
            and query_field is scalar
            and type(scalar) in _NATIVE_INTEGER_FIELDS
            and identity != "_rebac_depth"
            and connections[self.using].vendor in ("postgresql", "sqlite")
        ):
            return False
        forward = True

        def hop(_model: type[models.Model], field: object, _prefix: str) -> None:
            nonlocal forward
            forward = forward and type(field) in (models.ForeignKey, models.OneToOneField)

        _relation_path(model, resolved.path, visit=hop)
        if not forward or not resolved.targets_identity_directly():
            return False
        inspected = source.all().query
        own = inspected.resolve_ref(identity)
        target = inspected.resolve_ref(resolved.target_values_path())
        if type(own) is not Col or type(target) is not Col:
            return False
        target_field = target.target
        if not (
            own.target is scalar
            and own.output_field is scalar
            and (
                (target_field is scalar and target.output_field is scalar)
                or (
                    isinstance(target_field, models.ForeignKey)
                    and type(target_field) in (models.ForeignKey, models.OneToOneField)
                    and target_field is resolved.field
                    and target_field.target_field is scalar
                    and (target.output_field is scalar or target.output_field is target_field)
                )
            )
        ):
            return False
        compared = target.output_field
        exact, membership = (
            (RelatedExact, RelatedIn)
            if isinstance(compared, (models.ForeignKey, models.OneToOneField))
            else (IntegerFieldExact, In)
        )
        return (
            scalar.get_lookup("exact") is IntegerFieldExact
            and scalar.get_lookup("in") is In
            and compared.get_lookup("exact") is exact
            and compared.get_lookup("in") is membership
        )

    def _flat_self_tuple(
        self,
        key: Key,
        at: At,
        bound: Bound,
        *,
        depth_possible: bool,
    ) -> Q | None:
        """A parent->same-permission cycle over a stored relation.

        The seed is the resources of the edges whose subject holds the base;
        the closure follows the edges from there.  The base is evaluated at
        the edge's subject, so it may be any expression.  Subjects of the
        relation that have another type do not recurse: the arrow through
        them is one more arm of the base.
        """
        definition = self.schema.get_definition(key[0])
        permission = self.schema.get_permission(*key)
        if definition is None or permission is None:
            return None
        arms = self._union_arms(permission.expression)
        recursive = self._self_arrows(key, definition, arms)
        if len(recursive) != 1 or len(arms) < 2 or self._tagged_inside(recursive[0]):
            return None
        arm = recursive[0]
        base = [other for other in arms if other is not arm]
        relation = next((r for r in definition.relations if r.name == arm.via), None)
        if relation is None or relation.backing is not None:
            return None
        same = [allowed for allowed in relation.allowed_subjects if allowed.type == key[0]]
        other_types = [allowed for allowed in relation.allowed_subjects if allowed.type != key[0]]
        if not same or any(allowed.wildcard for allowed in same):
            return None
        inside = {key: 1}

        def base_at(where: At) -> Q:
            parts = [
                self._expression(
                    definition, other, where, bound, inside, depth_possible=depth_possible
                )
                for other in base
            ]
            if other_types:
                parts.append(
                    self._stored_relation(
                        definition,
                        relation,
                        where,
                        bound,
                        inside,
                        arm.target,
                        depth_possible,
                        only=other_types,
                    )
                )
            return self._union(where, parts)

        point = base_at(at)
        if point is _TRUE:
            return _TRUE
        every = self._tuples().filter(resource_type=key[0], relation=relation.name)
        # An arrow follows the subject's object, whatever relation suffix the
        # edge carries, so every declared shape of the own type is an edge.
        variants = _FALSE
        for allowed in same:
            shape = Q(subject_relation=allowed.relation, caveat_name=allowed.with_caveat)
            if allowed.id:
                shape &= Q(subject_id=allowed.id)
            variants = _or(variants, shape)
        edges = self._live(
            every.filter(variants, subject_type=key[0]).exclude(subject_id="*"),
            relation,
            bound,
        )
        seed = base_at(At(key[0], F("subject_id"), None, False))
        if seed is _FALSE:
            return point
        if at.key is None and _one_object(at.ref) and bound is Bound.LOWER:
            # One object: follow the edges from it.  What it reaches is small,
            # each level is one lookup by resource, and the base is named once.
            holders = edges if seed is _TRUE else edges.filter(seed)
            if self.depth_limit <= 1:
                return _or(point, Q(Exists(holders.filter(self._from(at, None)))))
            if self._forward_recursion(edges):

                def reached_rows(cte: CTE) -> QuerySet[Any]:
                    first = (
                        edges.filter(resource_id=at.ref)
                        .order_by()
                        .values("subject_id")
                        .annotate(_rebac_depth=Value(1, output_field=models.IntegerField()))
                    )
                    depth = models.ExpressionWrapper(
                        cte.col._rebac_depth, output_field=models.IntegerField()
                    )
                    step = (
                        cte.join(edges, resource_id=cte.col.subject_id)
                        .order_by()
                        .values("subject_id")
                        .annotate(_rebac_depth=depth + Value(1))
                        .filter(_rebac_depth__lt=self.depth_limit)
                        .values("subject_id", "_rebac_depth")
                    )
                    return cast(QuerySet[Any], first.union(step))

                cte = CTE.recursive(reached_rows, name=self._cte_name("rebac_reached"))
                reached_cte = self._cte_queryset(cte).values("subject_id")
                matched = with_cte(cte, select=holders.filter(self._from(at, reached_cte)))
                return _or(point, Q(Exists(matched)))
            reached: QuerySet[Any] | None = None
            for _ in range(self.depth_limit - 1):
                reached = edges.filter(self._from(at, reached)).order_by().values("subject_id")
            return _or(point, Q(Exists(holders.filter(self._from(at, reached)))))
        closure = self._closure(
            (edges if seed is _TRUE else edges.filter(seed)).order_by().values("resource_id"),
            edges,
            self.depth_limit - 1,
        )
        result = _or(point, self._wire_membership(at, every, closure))
        if bound is Bound.UPPER and depth_possible:
            known = base_at(At(key[0], F("resource_id"), None, False))
            if known is not _TRUE:
                result = _or(
                    result, _and(self._not_converged(edges, closure, known), _not_null(at.ref))
                )
        return result

    def _forward_recursion(self, edges: Any) -> bool:
        """Whether tuple edges followed from one object lower to a recursive CTE.

        Both wire columns must be plain text columns of one collation, so the
        CTE's subject id compares with the next edge's resource id unchanged.
        """
        if connections[self.using].vendor not in ("postgresql", "sqlite"):
            return False
        columns = tuple(edges.query.resolve_ref(name) for name in ("resource_id", "subject_id"))
        if not all(
            type(column) is Col and column.output_field is column.target for column in columns
        ):
            return False
        resource_field, subject_field = (column.output_field for column in columns)
        return (
            type(resource_field) in (models.CharField, models.TextField)
            and type(subject_field) in (models.CharField, models.TextField)
            and resource_field.db_collation == subject_field.db_collation
        )

    @staticmethod
    def _from(at: At, reached: QuerySet[Any] | None) -> Q:
        """An edge leaves ``at`` or an object already reached from it."""
        step = Q(resource_id=at.ref)
        if reached is not None:
            step |= Q(resource_id__in=_Compiled(reached))
        return step

    # ---------- Expressions ----------

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
            result = _FALSE
        elif isinstance(expr, PermRef):
            if expr.name == "authenticated":
                result = _truth(self.shape.authenticated)
            elif expr.name == "anonymous":
                result = _truth(self.shape.anonymous)
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
                else _FALSE
            )
        elif isinstance(expr, PermBinOp):
            tag = self.tagged.sites.get(id(expr)) if self.tagged is not None else None
            live = _before(self.now, tag.deadline) if tag is not None and tag.deadline else None
            if expr.op == "+":
                arms = self._union_arms(expr)
                if len(arms) == 1:
                    # A tagged union: its own tag applies below, its operands here.
                    arms = [expr.left, expr.right]
                result = self._union(
                    at, self._arms(definition, arms, at, bound, visits, depth_possible)
                )
            elif expr.op == "&":
                left = self._expression(
                    definition, expr.left, at, bound, visits, depth_possible=depth_possible
                )
                right = self._expression(
                    definition, expr.right, at, bound, visits, depth_possible=depth_possible
                )
                result = _and(left, _or(right, _not(live)) if live is not None else right)
            elif expr.op == "-":
                left = self._expression(
                    definition, expr.left, at, bound, visits, depth_possible=depth_possible
                )
                anti = (
                    _FALSE
                    if left is _FALSE and live is None
                    else self._anti_expression(
                        definition,
                        expr.right,
                        at,
                        bound.opposite(),
                        visits,
                        depth_possible=depth_possible,
                    )
                )
                result = _and(left, _or(anti, _not(live)) if live is not None else anti)
            else:
                raise SchemaError(f"Unknown permission operator {expr.op!r}")
        else:
            raise TypeError(f"Unknown permission expression {type(expr).__name__}")
        arm = self.tagged.arms.get(id(expr)) if self.tagged is not None else None
        if arm is not None and arm.deadline:
            return _and(result, _before(self.now, arm.deadline))
        return result

    def _arms(
        self,
        definition: Definition,
        arms: Sequence[PermExpr],
        at: At,
        bound: Bound,
        visits: Mapping[Key, int],
        depth_possible: bool,
    ) -> list[Q]:
        """The arms of a union, compiled until one holds outright.

        Arrows over constants come first: they fold to a decision (a member
        of the admin role), and when one holds nothing else is compiled.
        """

        def constant(arm: PermExpr) -> bool:
            if not isinstance(arm, PermArrow):
                return isinstance(arm, PermRef) and arm.name in ("authenticated", "anonymous")
            relation = next((r for r in definition.relations if r.name == arm.via), None)
            return relation is not None and isinstance(relation.backing, ConstBinding)

        parts: list[Q] = []
        for arm in sorted(arms, key=lambda arm: not constant(arm)):
            part = self._expression(
                definition, arm, at, bound, visits, depth_possible=depth_possible
            )
            if part is _TRUE:
                return [_TRUE]
            parts.append(part)
        return parts

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
        arms = self._union_arms(expr)
        if len(arms) > 1:
            return _and(
                *(
                    self._anti_expression(
                        definition, arm, at, bound, visits, depth_possible=depth_possible
                    )
                    for arm in arms
                )
            )
        rows: Any
        if at.row:
            model = model_for_resource_type(at.resource_type)
            if model is None:
                return _TRUE
            identity = resource_id_attr(model)
            positive = self._expression(
                definition, expr, at, bound, visits, depth_possible=depth_possible
            )
            if positive is _TRUE or positive is _FALSE:
                return _not(positive)
            rows = (
                model._base_manager.using(self.using)
                .filter(**{identity: OuterRef(identity)})
                .filter(positive)
            )
        else:
            from ..models.generation import SchemaGeneration

            ref = _one_level_out(at.ref)
            positive = self._expression(
                definition,
                expr,
                At(at.resource_type, ref, at.key, False),
                bound,
                visits,
                depth_possible=depth_possible,
            )
            if positive is _TRUE or positive is _FALSE:
                return _not(positive)
            rows = SchemaGeneration.objects.using(self.using).filter(pk=1).filter(positive)
        return ~Q(Exists(rows))

    def _union(self, at: At, parts: Sequence[Q]) -> Q:
        """Normalize a row disjunction to one semi-join over a UNION of ids."""
        kept = [part for part in parts if part is not _FALSE]
        if any(part is _TRUE for part in kept):
            return _TRUE
        if not kept:
            return _FALSE
        if len(kept) == 1:
            return kept[0]
        model = model_for_resource_type(at.resource_type)
        if not at.row or model is None:
            return _or(*kept)
        identity = resource_id_attr(model)
        source = model._base_manager.using(self.using)
        ids = [source.filter(part).order_by().values_list(identity, flat=True) for part in kept]
        return _in(at.ref, ids[0].union(*ids[1:]))

    # ---------- Relations ----------

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
        # Following a relation moves to its subjects: one hop.
        visits = {**visits, _HOPS: visits.get(_HOPS, 0) + 1}
        if isinstance(relation.backing, FieldBinding):
            try:
                resolved = _resolve_field_backing(definition, relation)
            except ValueError as exc:
                raise SchemaError(
                    f"Field backing {definition.resource_type}#{relation.name} does not resolve "
                    f"against the models (rebac.E009): {exc}"
                ) from exc
            return self._field_relation(resolved, at, bound, visits, target, depth_possible)
        if isinstance(relation.backing, ConstBinding):
            resolved_const = resolve_const_backing(definition, relation)
            if resolved_const is None:
                raise SchemaError(
                    f"Constant backing {definition.resource_type}#{relation.name} does not resolve "
                    "against the models (rebac.E009)."
                )
            return self._const_relation(resolved_const, at, bound, visits, target, depth_possible)
        if isinstance(relation.backing, AttributeBinding):
            resolved_attr = resolve_attribute_backing(definition, relation)
            if resolved_attr is None:
                raise SchemaError(
                    f"Attribute backing {definition.resource_type}#{relation.name} does not resolve "
                    "against the models (rebac.E009)."
                )
            return self._attribute_relation(
                resolved_attr, relation, at, bound, visits, target, depth_possible
            )
        return self._stored_relation(
            definition, relation, at, bound, visits, target, depth_possible
        )

    def _subject_rows(
        self,
        allowed_type: str,
        name: str | None,
        relation_name: str,
        model: type[models.Model],
        identity: str,
        bound: Bound,
        visits: Mapping[Key, int],
        depth_possible: bool,
    ) -> QuerySet[Any] | bool:
        """The subject-model rows that admit the actor; ``True`` for every row.

        With ``name`` (an arrow target or a subject-set relation) a row admits
        the actor when the actor holds it there, or, for a bare subject set,
        when the actor is that very subject set.  Without it the actor must be
        the row.  A set decided before the statement comes back as its keys.
        """
        rows = model._base_manager.using(self.using)
        own = self.shape.type == allowed_type and self._canonical(model, identity)
        if name is None:
            if not own or self.shape.relation:
                return False
            return rows.filter(**{identity: self._native(model, identity)})
        _, field = model_identity_fields(model, identity)
        itself = bool(relation_name and own and self.shape.relation == relation_name)
        member = self._holds(
            (allowed_type, name),
            At(allowed_type, F(identity), field, True),
            bound,
            visits,
            depth_possible=depth_possible,
        )
        if itself:
            member = _or(member, Q(**{identity: self._native(model, identity)}))
        if member is _FALSE:
            return False
        if member is _TRUE:
            return True
        return rows.filter(member)

    def _field_relation(
        self,
        resolved: ResolvedFieldBacking,
        at: At,
        bound: Bound,
        visits: Mapping[Key, int],
        target: str | None,
        depth_possible: bool,
    ) -> Q:
        if self.point is not None and not self.point.may_name(resolved, at, self.using):
            # The point row names another type: this arm's chain is not built.
            return _FALSE
        allowed = resolved.relation.allowed_subjects[0]
        if allowed.relation and not self._is_relation(allowed.type, allowed.relation):
            return _FALSE
        target_path = resolved.target_values_path()
        name = target if target is not None else (allowed.relation or None)
        subjects = self._subject_rows(
            allowed.type,
            name,
            allowed.relation if target is None else "",
            resolved.target_model,
            resolved.target_id_attr,
            bound,
            visits,
            depth_possible,
        )
        if subjects is False:
            return _FALSE
        # Whether the predicate reads the targets' own rows; otherwise a
        # reference that nothing constrains is checked against them.
        through_rows = True
        if subjects is True:
            source_q = Q(**{f"{target_path}__isnull": False})
            through_rows = False
        elif name is None:
            source_q = Q(
                **{target_path: self._native(resolved.target_model, resolved.target_id_attr)}
            )
            through_rows = False
        else:
            source_q = Q(
                **{
                    f"{target_path}__in": _Compiled(
                        subjects.order_by().values(resolved.target_id_attr)
                    )
                }
            )
        # A GenericForeignKey backing holds the rows of one content type.
        generic = resolved.generic_q(self.using, exists=not through_rows)
        source_q = Q(**resolved.filters) & generic & source_q
        # Two arms over one multi-valued path must not share its join, so
        # only a single-valued lookup may be inlined into the caller's filter.
        single = not _multi_valued(resolved.source_model, resolved.path) and not any(
            _multi_valued(resolved.source_model, lookup) for lookup in resolved.filters
        )
        if at.row and single:
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
            return _FALSE
        if target is not None or allowed.relation:
            name = target if target is not None else allowed.relation
            target_key = (allowed.type, name)
            if (
                target is None
                and self.shape.type == allowed.type
                and self.shape.relation == allowed.relation
                and self.shape.named_id == resolved.target_id
            ):
                target_q = _TRUE
            else:
                target_q = self._holds(
                    target_key,
                    At(allowed.type, Value(resolved.target_id), None, False),
                    bound,
                    visits,
                    depth_possible=depth_possible,
                )
        else:
            target_q = _truth(
                self.shape.type == allowed.type
                and self.shape.named_id == resolved.target_id
                and not self.shape.relation
            )
        if not resolved.filters or target_q is _FALSE:
            return target_q
        local_q = Q(**resolved.filters)
        if at.row:
            return _and(local_q, target_q)
        rows = resolved.source_model._base_manager.using(self.using).filter(local_q)
        return _and(
            self._model_membership(at, rows, resolved.source_model, resolved.source_id_attr),
            target_q,
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
            return _FALSE
        name = target if target is not None else (allowed.relation or None)
        found = self._subject_rows(
            allowed.type,
            name,
            allowed.relation if target is None else "",
            resolved.target_model,
            resolved.target_id_attr,
            bound,
            visits,
            depth_possible,
        )
        if found is False:
            return _FALSE
        every = resolved.target_model._base_manager.using(self.using)
        subjects = (every if found is True else found).filter(Q(**resolved.filters))
        if resolved.resource is not None:
            subjects = subjects.filter(resolved.value_filter(resolved.value))
            derived = self._wire_equal(at, resolved.resource) & Q(Exists(subjects))
            fallback = ~self._wire_equal(at, resolved.resource)
            definition = self.schema.get_definition(at.resource_type)
            assert definition is not None
            return self._union(
                at,
                [
                    derived,
                    _and(
                        fallback,
                        self._stored_relation(
                            definition, relation, at, bound, visits, target, depth_possible
                        ),
                    ),
                ],
            )
        field = resolved.field
        if isinstance(field, (models.CharField, models.TextField)):
            values = subjects.exclude(**{field.name: ""}).exclude(**{f"{field.name}__isnull": True})
            return _in(at.ref, values.order_by().values(field.name))
        present = subjects.exclude(**{f"{field.name}__isnull": True}).order_by().values(field.name)
        # Convert the wire identity, not the indexed model column.
        if at.key is None:
            converted = identity_codec(resolved.target_model, field.name).to_column(
                cast(Expression, at.ref)
            )
            return _in(converted, present)
        return _in(at.ref, present)

    def _stored_relation(
        self,
        definition: Definition,
        relation: Relation,
        at: At,
        bound: Bound,
        visits: Mapping[Key, int],
        target: str | None,
        depth_possible: bool,
        only: Sequence[AllowedSubject] | None = None,
    ) -> Q:
        rows = self._tuples().filter(resource_type=definition.resource_type, relation=relation.name)
        # Several allowed subjects can share one membership test: an arrow
        # looks only at the subject's type, a subject set at its type and
        # relation.  Each test is compiled once and joined with all its shapes,
        # so a recursive arrow stays one reference per level.
        shapes: dict[tuple[str, str, str], Q] = {}
        for allowed in relation.allowed_subjects if only is None else only:
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
                group = ("arrow", allowed.type, "")
            elif allowed.relation:
                group = ("set", allowed.type, allowed.relation)
            elif allowed.wildcard:
                group = ("wildcard", allowed.type, "")
            else:
                group = ("subject", allowed.type, allowed.id)
            shapes[group] = _or(shapes.get(group, _FALSE), shape)
        admitted = _FALSE
        # Tests that nest a subquery go last (see ``_tuple_identity_rows``).
        order = {"subject": 0, "wildcard": 1, "set": 2, "arrow": 3}
        for (kind, allowed_type, detail), shape in sorted(
            shapes.items(), key=lambda item: (order[item[0][0]], item[0][1], item[0][2])
        ):
            subject_at = At(allowed_type, F("subject_id"), None, False)
            if kind == "arrow":
                assert target is not None
                member = self._holds(
                    (allowed_type, target), subject_at, bound, visits, depth_possible=depth_possible
                )
            elif kind == "set":
                member = _or(
                    self._holds(
                        (allowed_type, detail),
                        subject_at,
                        bound,
                        visits,
                        depth_possible=depth_possible,
                    ),
                    _and(
                        _truth(self.shape.type == allowed_type and self.shape.relation == detail),
                        Q(subject_id=self._wire()),
                    ),
                )
            elif kind == "wildcard":
                member = _truth(self.shape.type == allowed_type and not self.shape.relation)
            else:
                member = _and(
                    _truth(
                        self.shape.type == allowed_type
                        and (not detail or self.shape.named_id == detail)
                        and not self.shape.relation
                    ),
                    Q(subject_id=self._wire()),
                )
            admitted = _or(admitted, _and(shape, member))
        if admitted is _FALSE:
            return _FALSE
        return self._tuple_membership(at, self._live(rows, relation, bound), admitted)

    def _is_relation(self, type_: str, name: str) -> bool:
        definition = self.schema.get_definition(type_)
        return definition is not None and any(r.name == name for r in definition.relations)

    # ---------- Identity ----------

    def _tuple_membership(self, at: At, rows: QuerySet[Any], last: Q | None = None) -> Q:
        if at.key is None and _one_object(at.ref):
            # One object: look its tuples up by key instead of building the set.
            rows = rows.filter(resource_id=at.ref)
            return Q(Exists(rows.filter(last) if last is not None else rows))
        return _in(at.ref, self._tuple_identity_rows(at, rows, last))

    def _tuple_identity_rows(
        self, at: At, rows: QuerySet[Any], last: Q | None = None
    ) -> QuerySet[Any]:
        """The resource ids of tuple rows, in the domain of ``at``.

        ``last`` is applied after every other condition.  SQLite measures an
        expression's depth through its subqueries, and a chain of ANDs is
        left-deep, so a nested subquery must be the final conjunct.
        """
        model = model_for_resource_type(at.resource_type)
        identity = resource_id_attr(model) if model is not None else None
        if model is None:
            subject_model = model_for_subject_type(at.resource_type)
            if subject_model is not None:
                model, identity = subject_model
        if at.key is None or model is None:
            rows = rows.exclude(resource_id__isnull=True)
            if last is not None:
                rows = rows.filter(last)
            return rows.order_by().values("resource_id")
        assert identity is not None
        _, identity_field = model_identity_fields(model, identity)
        converted = rows.annotate(
            _rebac_native_id=identity_codec(model, identity).to_column("resource_id")
        ).exclude(_rebac_native_id__isnull=True)
        if last is not None:
            converted = converted.filter(last)
        if at.key is identity_field:
            return cast(QuerySet[Any], converted.order_by().values("_rebac_native_id"))
        bridge = model._base_manager.using(self.using).filter(
            **{f"{identity}__in": _Compiled(converted.order_by().values("_rebac_native_id"))}
        )
        return cast(QuerySet[Any], bridge.order_by().values(at.key.name))

    def point_witness(self, at: At) -> Q:
        """The point row still has the content types its arms were chosen by."""
        point = self.point
        if point is None or at.ref is not point.ref:
            return _TRUE
        model = model_for_resource_type(at.resource_type)
        if model is None:
            return _TRUE
        rows = model._base_manager.using(self.using).filter(**dict(point.types))
        return self._model_membership(at, rows, model, resource_id_attr(model))

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
            converted = identity_codec(model, identity).to_column(cast(Expression, at.ref))
            if _one_object(at.ref):
                # One object: look its row up by key instead of building the set.
                return Q(Exists(rows.order_by().filter(**{identity: converted})))
            return _in(converted, native)
        _, canonical = model_identity_fields(model, identity)
        if at.key is canonical:
            return _in(at.ref, native)
        bridge = model._base_manager.using(self.using).filter(
            **{f"{identity}__in": _Compiled(native)}
        )
        return _in(at.ref, bridge.order_by().values(at.key.name))

    def _wire_equal(self, at: At, wire_id: str) -> Q:
        if at.key is None:
            return Q(Exact(at.ref, Value(wire_id)))
        model = model_for_resource_type(at.resource_type)
        if model is None:
            return _FALSE
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
