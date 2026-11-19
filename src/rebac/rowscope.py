"""A queryset's scope as conditions on each of its rows (proposal 0020).

The compiled scope of a list read is ``id IN (the ids the actor holds)``: the
statement builds every id the actor holds on the type, and only then meets the
caller's own conditions. For an actor with many grants reading a few rows that
is the wrong way round, and the database cannot turn it: the id set converts
each granted id to the model's key, and its size is estimated, not known.

With ``REBAC_ROW_SCOPE`` set, the scope is written the other way: for each row
the caller's conditions select, does a grant reach it? Every part of the
permission becomes a condition on the row:

- a stored relation: ``EXISTS`` a live, uncaveated relationship row on this
  row's id that names the actor, or names a subject the same question holds
  for (a subject set, an arrow);
- a foreign-key backing: the column names the actor's row, or ``EXISTS`` the
  parent row with the parent's own conditions;
- a constant backing: the question at the fixed object, the same for every row.

The statement costs what the rows it reads cost, whatever the actor holds. It
is the worse form where the caller's conditions select many rows and the actor
holds few: each row is asked, where the id set would have named the few.
``"named"`` therefore uses it only where the queryset's own conditions bind its
primary key or one of its foreign keys to literal values, so the rows asked are
the rows named; ``True`` uses it for every read the policy allows.

The policy must be one the walk reads whole (``rebac.walk``): no recursion, no
caveated relation, plain integer keys, enforced single foreign keys; and no
expiring relation, whose instant the compiled scope binds once per statement.
Anything else, an override, an anonymous actor or an actor that is a subject
set, keeps the compiled scope.

Only the statement a queryset runs itself is written this way (``executing``).
A queryset embedded in another one, a combined query and the count, exists and
aggregate forms keep the compiled scope, whose single uncorrelated condition
Django can move between query levels; a condition that refers to its row
cannot be added to a query whose aliases are already settled.

The conditions carry the same fence as a compiled statement: they admit
nothing while the published policy revision is not the one they were built for.

So does a permission that is too large written this way. The compiled scope
names each id set once and selects from it wherever the policy reaches it; a
condition on a row cannot be named, so a permission that several others refer
to is written out at each use, and a deep policy grows with every level. Each
subquery is planned at every execution (measured on PostgreSQL 16, registry
layout: 0.3 to 0.4 ms each). Past ``_SUBQUERIES`` the compiled scope is kept.

**PostgreSQL's JIT must be off for these statements.** A statement of
correlated subqueries is estimated expensive, the JIT compiles every one of
them, and a 40-subquery scope over three rows took 6 s with ``jit = on`` (the
PostgreSQL default) against 2 ms of execution with it off. The library sets no
database option: the application does (``OPTIONS["options"] = "-c jit=off"``).

The conditions depend on the policy, the model, the action and the actor, not
on any row, so they are built once and kept (``_built``).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

from django.core.exceptions import FieldError
from django.db import models
from django.db.models import Exists, F, OuterRef, Q, Value
from django.db.models.functions import Cast
from django.db.models.sql import Query

from . import walk as walking
from ._id import resource_id_attr
from .codec import identity_codec
from .compile.predicate import (
    _FALSE,
    _TRUE,
    _and,
    _not,
    _one_level_out,
    _or,
    _truth,
    is_false,
    is_true,
)
from .field_backing import resolve_const_backing
from .models import active_relationship_model
from .resources import model_resource_type
from .schema.ast import (
    ConstBinding,
    Definition,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermExpr,
    PermNil,
    PermRef,
    Relation,
)
from .types import SubjectRef

Key = tuple[str, str]

# Subqueries in one scope past which the compiled id sets are kept.
_SUBQUERIES = 24
# Built conditions, by policy, model, action, actor and alias; ``None`` for a
# permission that keeps the compiled scope.
_built: OrderedDict[tuple[Any, ...], Q | None] = OrderedDict()
_BUILT_LIMIT = 1024
# The query a queryset is about to run itself.
_executing: ContextVar[Any] = ContextVar("rebac_row_scope_executing", default=None)


@contextmanager
def executing(query: Any) -> Iterator[None]:
    """``query`` is scoped here to be run as its own statement, not embedded in another."""
    token = _executing.set(query)
    try:
        yield
    finally:
        _executing.reset(token)


class _TooLarge(Exception):
    """The permission written as row conditions passes ``_SUBQUERIES``."""


def mode() -> str:
    """``"always"``, ``"named"`` or empty for off."""
    from .conf import app_settings

    value = app_settings.REBAC_ROW_SCOPE
    if value is True or value == "always":
        return "always"
    return "named" if value == "named" else ""


def predicate(
    query: Any, model: type[models.Model], actor: SubjectRef, action: str, using: str
) -> Q | None:
    """The scope of ``query`` as row conditions, or ``None`` to keep the compiled scope."""
    chosen = mode()
    if not chosen or _executing.get() is not query or query.combined_queries:
        return None
    if chosen == "named" and not names_its_rows(query, model):
        return None
    return row_conditions(model, actor, action, using)


def names_its_rows(query: Any, model: type[models.Model]) -> bool:
    """Whether the query's own conditions bind its primary key or a foreign key to literals."""
    from .structural import bound_fields

    return any(
        field.primary_key or isinstance(field, models.ForeignKey)
        for field, _values in bound_fields(query, model)
    )


def explain(
    model: type[models.Model], actor: SubjectRef, action: str, using: str = "default"
) -> dict[str, Any]:
    """Whether ``action`` on ``model`` is written as row conditions for ``actor``, and if not, why.

    ``{"applies": bool, "subqueries": int | None, "blocked": [reasons]}``. For a
    test, a benchmark or a system check: it builds the conditions without keeping them.
    """
    from .backends import backend
    from .backends.local import LocalBackend

    local = backend()
    resource_type = model_resource_type(model)
    if not isinstance(local, LocalBackend) or not resource_type:
        return {"applies": False, "subqueries": None, "blocked": ["no local policy for the model"]}
    walk = walking._walk(local, actor, using, None)
    if walk is None:
        return {"applies": False, "subqueries": None, "blocked": ["the actor or an override"]}
    if walking._integer_identity(model) is None:
        return {
            "applies": False,
            "subqueries": None,
            "blocked": ["the model's key is not an integer"],
        }
    rows = _Rows(walk, limit=None)
    found = rows.holds((resource_type, action), _At(model, None), frozenset())
    size = None if found is None else rows._size(found)
    blocked = list(rows.blocked)
    if size is not None and size > _SUBQUERIES:
        blocked.append(f"{size} subqueries, past the limit of {_SUBQUERIES}")
    return {"applies": found is not None and not blocked, "subqueries": size, "blocked": blocked}


def row_conditions(
    model: type[models.Model], actor: SubjectRef, action: str, using: str
) -> Q | None:
    """The actor's ``action`` on ``model`` as a condition on each row; ``None`` where unreadable."""
    from .backends import backend
    from .backends.local import LocalBackend

    local = backend()
    resource_type = model_resource_type(model)
    if not isinstance(local, LocalBackend) or not resource_type:
        return None
    walk = walking._walk(local, actor, using, None)
    if walk is None or walking._integer_identity(model) is None:
        return None
    key = (walk.policy.key, active_relationship_model(), model, action, actor, using, _SUBQUERIES)
    if key in _built:
        _built.move_to_end(key)
        return _built[key]
    rows = _Rows(walk, limit=_SUBQUERIES)
    try:
        found = rows.holds((resource_type, action), _At(model, None), frozenset())
    except _TooLarge:
        found = None
    if found is not None:
        from .compile.read import _fence

        # Closed while the published revision is not this policy's, as a compiled statement is.
        fence = Q(Exists(_fence(walk.policy, backend=local, using=using, parametric=False)))
        found = Q(pk__in=[]) if is_false(found) else fence if is_true(found) else fence & found
    _built[key] = found
    while len(_built) > _BUILT_LIMIT:
        _built.popitem(last=False)
    return found


@dataclass(frozen=True)
class _At:
    """The object a condition is about, as the query being filtered reads it.

    ``model`` when the object is that query's own row. Otherwise ``ref`` is its
    wire id: a column of the query (a relationship row's subject) or a value.
    """

    model: type[models.Model] | None
    ref: Any


class _Rows:
    def __init__(self, walk: walking._Walk, *, limit: int | None = _SUBQUERIES) -> None:
        self.walk = walk
        self.limit = limit
        self.schema = walk.schema
        self.program = walk.program
        self.actor = walk.actor
        self.using = walk.using
        self._memo: dict[tuple[Any, ...], Q | None] = {}
        # Why the permission keeps the compiled scope, where it does (``explain``).
        self.blocked: list[str] = []
        # The subqueries each condition built here is written with, by identity.
        self._sizes: dict[int, tuple[Q, int]] = {}

    # ---------- Size: a condition is written out wherever it is used ----------

    def _no(self, reason: str) -> Q | None:
        """This part cannot be written as a row condition: remember why, and say so."""
        if reason not in self.blocked:
            self.blocked.append(reason)
        return None

    def _size(self, condition: Q) -> int:
        return self._sizes.get(id(condition), (condition, 0))[1]

    def _sized(self, condition: Q, size: int) -> Q:
        if self.limit is not None and size > self.limit:
            raise _TooLarge
        self._sizes[id(condition)] = (condition, size)
        return condition

    def _exists(self, rows: Any, *inside: Q) -> Q:
        """``EXISTS(rows)``, counted with the conditions its rows were filtered by."""
        return self._sized(Q(Exists(rows)), 1 + sum(self._size(part) for part in inside))

    def _any(self, *parts: Q) -> Q:
        distinct = list({id(part): part for part in parts}.values())
        found = _or(*distinct)
        if any(found is part for part in distinct) or is_true(found) or is_false(found):
            return found
        return self._sized(found, sum(self._size(part) for part in distinct))

    def _all(self, *parts: Q) -> Q:
        distinct = list({id(part): part for part in parts}.values())
        found = _and(*distinct)
        if any(found is part for part in distinct) or is_true(found) or is_false(found):
            return found
        return self._sized(found, sum(self._size(part) for part in distinct))

    # ---------- The permission ----------

    def holds(self, key: Key, at: _At, path: frozenset[Key]) -> Q | None:
        """The condition under which the actor holds ``key`` on the object at ``at``."""
        if key not in self.program.dependencies:
            return _FALSE
        if key in path or self.walk.handed_over(key):
            kind = (
                "a recursion" if key in self.program.recursive else "a caveated relation or a cycle"
            )
            return self._no(f"{key[0]}#{key[1]}: {kind}")
        memo = (key, at.model, _ref_key(at.ref))
        if memo in self._memo:
            return self._memo[memo]
        found = self._memo[memo] = self._node(key, at, path | {key})
        return found

    def _node(self, key: Key, at: _At, path: frozenset[Key]) -> Q | None:
        definition = self.schema.get_definition(key[0])
        if definition is None:
            return _FALSE
        relation = next((r for r in definition.relations if r.name == key[1]), None)
        if relation is not None:
            return self._through(definition, relation, None, at, path)
        permission = self.schema.get_permission(*key)
        if permission is None:
            return _FALSE
        return self._expression(definition, permission.expression, at, path)

    def _expression(
        self, definition: Definition, expr: PermExpr, at: _At, path: frozenset[Key]
    ) -> Q | None:
        if isinstance(expr, PermNil):
            return _FALSE
        if isinstance(expr, PermRef):
            # The walk's actor is a plain subject with an id: authenticated, not anonymous.
            if expr.name == "authenticated":
                return _TRUE
            if expr.name == "anonymous":
                return _FALSE
            return self.holds((definition.resource_type, expr.name), at, path)
        if isinstance(expr, PermArrow):
            relation = next((r for r in definition.relations if r.name == expr.via), None)
            if relation is None:
                return _FALSE
            return self._through(definition, relation, expr.target, at, path)
        if not isinstance(expr, PermBinOp) or expr.op not in ("+", "&", "-"):
            return None
        left = self._expression(definition, expr.left, at, path)
        right = self._expression(definition, expr.right, at, path)
        if left is None or right is None:
            return None
        if expr.op == "+":
            return self._any(left, right)
        if expr.op == "&":
            return self._all(left, right)
        return self._all(left, self._excluded(right, at))

    def _excluded(self, condition: Q, at: _At) -> Q:
        """``NOT condition``, two-valued: a condition that is unknown on a row excludes nothing.

        A column condition on the row can be NULL, so it is asked of the row
        again through ``NOT EXISTS``. Away from a row every condition is an
        ``EXISTS`` or a constant, and ``NOT`` is exact.
        """
        if at.model is None or is_true(condition) or is_false(condition):
            negated = _not(condition)
            if not (is_true(negated) or is_false(negated)):
                self._sized(negated, self._size(condition))
            return negated
        identity = walking._integer_identity(at.model)
        assert identity is not None
        again = (
            at.model._base_manager.using(self.using)
            .filter(**{identity.attname: OuterRef(identity.attname)})
            .filter(condition)
        )
        return self._sized(~Q(Exists(again)), 1 + self._size(condition))

    def _through(
        self,
        definition: Definition,
        relation: Relation,
        target: str | None,
        at: _At,
        path: frozenset[Key],
    ) -> Q | None:
        if isinstance(relation.backing, FieldBinding):
            return self._field(definition, relation, target, at, path)
        if isinstance(relation.backing, ConstBinding):
            return self._constant(definition, relation, target, at, path)
        if relation.backing is not None:
            return self._no(f"{definition.resource_type}#{relation.name}: an attribute backing")
        return self._stored(definition, relation, target, at, path)

    def _below(self, at: _At) -> Any:
        """The object's wire id, as a subquery of the query being filtered reads it."""
        if at.model is None:
            return _one_level_out(at.ref)
        identity = walking._integer_identity(at.model)
        assert identity is not None
        # An integer key written as text is its canonical wire id: no guard is needed.
        return Cast(OuterRef(identity.attname), models.TextField())

    def _stored(
        self,
        definition: Definition,
        relation: Relation,
        target: str | None,
        at: _At,
        path: frozenset[Key],
    ) -> Q | None:
        if relation.with_expiration:
            # The compiled scope binds one instant per statement.
            return self._no(f"{definition.resource_type}#{relation.name}: an expiring relation")
        named: set[str] = set()
        through: set[Key] = set()
        for allowed in relation.allowed_subjects:
            if allowed.with_caveat:
                return None
            if allowed.relation and not self.walk._is_relation(allowed.type, allowed.relation):
                continue
            if target is not None:
                if allowed.relation or allowed.wildcard or allowed.id:
                    return None
                through.add((allowed.type, ""))
            elif allowed.relation:
                if allowed.wildcard or allowed.id:
                    return None
                through.add((allowed.type, allowed.relation))
            elif allowed.type == self.actor.subject_type:
                if allowed.wildcard:
                    named.add("*")
                elif not allowed.id or allowed.id == self.actor.subject_id:
                    named.add(self.actor.subject_id)
        parts: list[Q] = []
        if named:
            parts.append(
                Q(subject_type=self.actor.subject_type, subject_relation="")
                & Q(subject_id__in=sorted(named))
            )
        for subject_type, subject_relation in sorted(through):
            name = target if target is not None else subject_relation
            inner = self.holds((subject_type, name), _At(None, F("subject_id")), path)
            if inner is None:
                return None
            if is_false(inner):
                continue
            shape = Q(subject_type=subject_type, subject_relation=subject_relation) & ~Q(
                subject_id="*"
            )
            if is_true(inner):
                parts.append(shape)
            else:
                parts.append(self._sized(shape & inner, self._size(inner)))
        if not parts:
            return _FALSE
        live = Q(expires_at__isnull=True)
        tuples: Any = active_relationship_model().objects.using(self.using)
        rows = (
            tuples.wire_projection()
            .filter(
                resource_type=definition.resource_type,
                relation=relation.name,
                caveat_name="",
            )
            .filter(live)
            .filter(resource_id=self._below(at))
            .filter(_or(*parts))
        )
        return self._exists(rows, *parts)

    def _field(
        self,
        definition: Definition,
        relation: Relation,
        target: str | None,
        at: _At,
        path: frozenset[Key],
    ) -> Q | None:
        backed = self.walk._backed(definition, relation)
        if backed is None:
            cause = (
                walking.backing_obstacle(definition, relation) or "a backing the walk does not read"
            )
            return self._no(f"{definition.resource_type}#{relation.name}: {cause}")
        resolved, source = backed
        [allowed] = relation.allowed_subjects
        if allowed.wildcard or allowed.id or allowed.with_caveat:
            return self._no(f"{definition.resource_type}#{relation.name}: its subject shape")
        if allowed.relation and not self.walk._is_relation(allowed.type, allowed.relation):
            return _FALSE
        column = cast(models.ForeignKey[Any, Any], resolved.field).attname
        parent = walking._integer_identity(resolved.target_model)
        assert parent is not None
        name = target if target is not None else (allowed.relation or None)
        present = Q(**{f"{column}__isnull": False})
        if name is None:
            codec = identity_codec(resolved.target_model, resolved.target_id_attr)
            if allowed.type != self.actor.subject_type or not codec.is_canonical(
                self.actor.subject_id, using=self.using
            ):
                return _FALSE
            own = present & Q(**{column: parent.to_python(self.actor.subject_id)})
        else:
            inner = self.holds((allowed.type, name), _At(resolved.target_model, None), path)
            if inner is None:
                return None
            if is_false(inner):
                return _FALSE
            if is_true(inner):
                own = present
            else:
                parents = (
                    resolved.target_model._base_manager.using(self.using)
                    .filter(**{parent.attname: OuterRef(column)})
                    .filter(inner)
                )
                own = self._exists(parents, inner)
        # Only the rows the backing's source-column filters select carry the edge.
        if resolved.filters:
            selected = self._selected(resolved.source_model, resolved.filters)
            own = self._sized(selected & own, self._size(selected) + self._size(own))
        if at.model is not None:
            return own if at.model is resolved.source_model else None
        # The object is named by its wire id: the condition holds on its row, if it has one.
        codec = identity_codec(resolved.source_model, resource_id_attr(resolved.source_model))
        rows = (
            resolved.source_model._base_manager.using(self.using)
            .filter(**{source.attname: codec.to_column(self._below(at))})
            .filter(own)
        )
        return self._exists(rows, own)

    def _selected(self, model: type[models.Model], filters: dict[str, Any]) -> Q:
        """A backing's source filters as a condition on the row of ``model``.

        A scope is added to a query that admits no join. Filters on the row's
        own columns are written in place; filters that follow a relation ask
        the row again in a subquery, where they may join.
        """
        selected = Q(**filters)
        try:
            Query(model).build_where(selected)
        except FieldError:
            identity = walking._integer_identity(model)
            assert identity is not None
            again = (
                model._base_manager.using(self.using)
                .filter(**{identity.attname: OuterRef(identity.attname)})
                .filter(selected)
            )
            return self._exists(again)
        return selected

    def _constant(
        self,
        definition: Definition,
        relation: Relation,
        target: str | None,
        at: _At,
        path: frozenset[Key],
    ) -> Q | None:
        """A fixed object: the same answer for every row its filters select."""
        resolved = resolve_const_backing(definition, relation)
        if resolved is None or len(relation.allowed_subjects) != 1:
            return None
        [allowed] = relation.allowed_subjects
        if allowed.wildcard or allowed.id or allowed.with_caveat:
            return None
        if allowed.relation and not self.walk._is_relation(allowed.type, allowed.relation):
            return _FALSE
        name = target if target is not None else (allowed.relation or None)
        if name is None:
            reached: Q | None = _truth(
                allowed.type == self.actor.subject_type
                and resolved.target_id == self.actor.subject_id
            )
        else:
            fixed = Value(resolved.target_id, output_field=models.TextField())
            reached = self.holds((allowed.type, name), _At(None, fixed), path)
        filters = resolved.filters
        if reached is None or is_false(reached) or not filters:
            return reached
        selected = self._selected(resolved.source_model, filters)
        if at.model is not None:
            return self._all(selected, reached) if at.model is resolved.source_model else None
        source = walking._integer_identity(resolved.source_model)
        if source is None:
            return None
        codec = identity_codec(resolved.source_model, resource_id_attr(resolved.source_model))
        rows = (
            resolved.source_model._base_manager.using(self.using)
            .filter(**{source.attname: codec.to_column(self._below(at))})
            .filter(selected)
        )
        return self._all(self._exists(rows), reached)


def _ref_key(ref: Any) -> Any:
    if ref is None:
        return None
    if isinstance(ref, Value):
        return ("value", ref.value)
    return ("column", getattr(ref, "name", repr(ref)))
