"""Read-time evaluation of indexed monotone sets and named sites."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from itertools import count
from typing import TYPE_CHECKING, Any, cast

from django.db import models
from django.db.models import (
    Case,
    Exists,
    Expression,
    F,
    FilteredRelation,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Value,
    When,
)
from django.db.models.lookups import Contains, Exact, GreaterThanOrEqual, LessThan

from .._id import resource_id_attr
from ..actors import anonymous_actor
from ..errors import SchemaError
from ..resources import model_for_resource_type, model_resource_type, stores_rows
from ..schema.cache import SchemaSnapshot, schema_operation
from ..schema.walker import find_relation, tri_and, tri_minus
from ..types import CheckResult, ObjectRef, SubjectRef
from . import classes, conditions
from . import time as index_time
from .codec import identity_codec
from .program import IndexProgram, Key, program_for
from .terms import AUTHENTICATED, Triple, anonymous, wildcard
from .time import active_q

if TYPE_CHECKING:
    from ..backends.local import LocalBackend


@dataclass
class _ReadOperation:
    backend: LocalBackend
    snapshot: SchemaSnapshot | None = None


_operation: ContextVar[_ReadOperation | None] = ContextVar("rebac_index_read", default=None)


@contextmanager
def using_backend(backend: LocalBackend) -> Iterator[None]:
    current = _operation.get()
    if current is not None and current.backend is backend:
        yield
        return
    token = _operation.set(_ReadOperation(backend))
    try:
        yield
    finally:
        _operation.reset(token)


def pin_snapshot(snapshot: SchemaSnapshot) -> None:
    operation = _operation.get()
    if operation is not None:
        operation.snapshot = snapshot


def pinned_snapshot(using: str) -> SchemaSnapshot | None:
    operation = _operation.get()
    snapshot = operation.snapshot if operation is not None else None
    return snapshot if snapshot is not None and snapshot.using == using else None


def _backend() -> LocalBackend:
    operation = _operation.get()
    if operation is not None:
        return operation.backend
    from ..backends import backend
    from ..backends.local import LocalBackend

    active = backend()
    if not isinstance(active, LocalBackend):
        raise SchemaError("Permission-index reads require LocalBackend")
    return active


def ensure_ready(*, using: str) -> IndexProgram:
    """The program to read with, once the index is known to be derived by it."""
    from ..models.generation import SchemaGeneration

    active = _backend()
    operation = _operation.get()
    snapshot = operation.snapshot if operation is not None else None
    witness = (
        (snapshot.revision, snapshot.index_revision, snapshot.index_program)
        if snapshot is not None and snapshot.using == using and not active._schema_is_manual
        else SchemaGeneration.objects.witness(using)
    )
    expected = witness[0] if witness else None
    if active._schema_is_manual:
        expected = snapshot.revision if snapshot is not None else active._manual_schema_revision()
    if witness is None or not expected or expected != witness[1]:
        raise SchemaError(
            "rebac.E013: permission index does not match the schema revision; "
            "run rebac index rebuild."
        )
    program = program_for(active, using=using)
    if witness[2] != program.digest:
        raise SchemaError(
            "rebac.E013: permission index was derived by a different program; "
            "run rebac index rebuild."
        )
    return program


class _ExecutionTime(Expression):
    def __init__(self) -> None:
        super().__init__(output_field=models.DateTimeField())

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, Any]:
        return cast(
            tuple[str, Any],
            compiler.compile(Value(index_time.index_now(), output_field=models.DateTimeField())),
        )


class _ManualRevision(Expression):
    def __init__(self, backend: LocalBackend) -> None:
        super().__init__(output_field=models.CharField())
        self.backend = backend

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, Any]:
        return cast(
            tuple[str, Any], compiler.compile(Value(self.backend._manual_schema_revision()))
        )


def _ready_rows(*, using: str, program: IndexProgram, pinned: bool = False) -> QuerySet[Any]:
    """The fence every statement carries, evaluated when the statement runs.

    The index is published for the current policy and was derived by the
    program the statement was compiled with. A statement that carries
    verdicts prepared from a pinned schema also requires that schema.
    """
    from ..models.generation import SchemaGeneration

    rows = SchemaGeneration.objects.using(using).filter(pk=1, index_program=program.digest)
    active = _backend()
    if active._schema_is_manual:
        rows = rows.filter(index_revision=_ManualRevision(active))
    else:
        rows = rows.filter(SchemaGeneration.objects.ready_q())
    if pinned:
        operation = _operation.get()
        snapshot = operation.snapshot if operation is not None else None
        if snapshot is None or snapshot.revision is None:
            return rows.none()
        rows = rows.filter(index_revision=snapshot.revision)
        if snapshot.expires_at is not None:
            # Verdicts prepared from the pinned schema hold until its earliest
            # override deadline; the statement closes when it executes later.
            rows = rows.filter(LessThan(_ExecutionTime(), Value(snapshot.expires_at)))
    return rows


def _triple_q(triple: Triple, prefix: str = "") -> Q:
    return Q(
        **dict(
            zip((prefix + "type", prefix + "object_id", prefix + "relation"), triple, strict=True)
        )
    )


def _actor_q(actor: SubjectRef, prefix: str = "") -> Q:
    return _triple_q((actor.subject_type, actor.subject_id, actor.optional_relation), prefix)


def _direct_q(actor: SubjectRef, prefix: str = "") -> Q:
    result = _actor_q(actor, prefix)
    for triple in (AUTHENTICATED, anonymous(), wildcard(actor.subject_type)):
        cls = classes.class_of(triple)
        if cls is not None and classes.matches(cls, actor):
            result |= _triple_q(triple, prefix)
    return result


@dataclass(frozen=True)
class ActorSets:
    exact: QuerySet[Any]
    definite: QuerySet[Any]
    possible: QuerySet[Any]


class _Verdicts:
    """The caveats the context decides, prepared before a statement is built.

    A statement cannot evaluate a caveat. The formulas on the rows of a read
    plan are decided here and named to the statement: a row is definite when
    its formula holds, and possible unless its formula fails. A formula
    written after the preparation is unknown to the statement and counts as
    possible only. The names travel as one parameter, so the statement does
    not grow with the number of formulas.
    """

    def __init__(
        self,
        key: Key,
        *,
        actor: SubjectRef | None,
        program: IndexProgram,
        schema: Any,
        context: Mapping[str, Any] | None,
        using: str,
    ) -> None:
        from ..models.index import IndexCover, IndexMember

        # Only the rows the read can reach: those of its plan, and the
        # memberships of its actor when there is one.
        memberships = Q() if actor is None else _direct_q(actor, "member__")
        nodes = Q(pk__in=[])
        pending = [_plan(program, key)]
        while pending:
            lookup = pending.pop()
            nodes |= Q(resource_type=lookup.key[0], node=lookup.key[1])
            pending.extend(operand for pair in lookup.sites.values() for operand in pair)
        decided: dict[bool, set[str]] = {True: set(), False: set()}
        for rows in (
            IndexCover.objects.using(using).filter(nodes, condition__isnull=False),
            IndexMember.objects.using(using).filter(memberships, condition__isnull=False),
        ):
            formulas = rows.order_by().values_list("condition_key", "condition").distinct()
            for name, formula in formulas.iterator():
                verdict = conditions.evaluate(formula, schema, context)[0]
                if verdict is not None:
                    decided[verdict].add(name)
        self.holds, self.fails = (
            Value(",".join(sorted(decided[verdict])), output_field=models.TextField())
            for verdict in (True, False)
        )

    def condition(self, positive: bool) -> Q:
        unconditional = Q(condition__isnull=True)
        if positive:
            return unconditional | Q(Contains(self.holds, F("condition_key")))
        return unconditional | ~Q(Contains(self.fails, F("condition_key")))


def actor_sets(
    actor: SubjectRef,
    *,
    using: str,
    now: datetime | Expression,
    verdicts: _Verdicts | None = None,
) -> ActorSets:
    from ..models.index import IndexMember, IndexTerm

    terms = IndexTerm.objects.using(using).order_by()
    memberships = (
        IndexMember.objects.using(using)
        .filter(active_q(now), _direct_q(actor, "member__"))
        .order_by()
    )
    direct = _direct_q(actor)
    definite, possible = (
        (Q(condition__isnull=True), Q())
        if verdicts is None
        else (verdicts.condition(True), verdicts.condition(False))
    )
    return ActorSets(
        terms.filter(_actor_q(actor)).values_list("pk", flat=True),
        terms.filter(direct | Q(pk__in=memberships.filter(definite).values("set_id"))).values_list(
            "pk", flat=True
        ),
        terms.filter(direct | Q(pk__in=memberships.filter(possible).values("set_id"))).values_list(
            "pk", flat=True
        ),
    )


def _up(name: str, depth: int) -> OuterRef:
    result: OuterRef = OuterRef(name)
    for _ in range(depth - 1):
        result = OuterRef(result)
    return result


class _Object:
    """The object a membership test is evaluated at.

    ``member`` compiles to nested ``EXISTS`` queries. Number them from the
    outside in: level 0 is the query the caller filters, level 1 holds the
    grant rows of the outermost node, and so on. An object is a column of one
    of those queries. It records that query's level and renders itself for any
    deeper level, so no caller counts relative distances.
    """

    def at(self, level: int) -> Any:
        """This object as referenced from the query at ``level``."""
        raise NotImplementedError


@dataclass(frozen=True)
class _Term(_Object):
    pk: int

    def at(self, level: int) -> Any:
        return self.pk


@dataclass(frozen=True)
class _Column(_Object):
    name: str
    level: int

    def at(self, level: int) -> Any:
        return _up(self.name, level - self.level)


@dataclass(frozen=True)
class _SiteObject(_Object):
    """Where the site held by a grant row is evaluated.

    The row at ``level`` was matched at ``tested``. Its site is evaluated
    there when the row's holder is its own scope (a site used as an arm, and
    type-level rows), and at the holder otherwise (an arrow or constant
    target).
    """

    level: int
    tested: _Object

    def at(self, level: int) -> Any:
        holder = _up("holder_id", level - self.level)
        return Case(
            When(Exact(holder, _up("scope_id", level - self.level)), then=self.tested.at(level)),
            default=holder,
            output_field=models.BigIntegerField(),
        )


class _Compiler:
    """Compile ``member``, or with ``naming`` the subjects a stored row names.

    Naming answers which subjects a tuple or a membership puts on a path to
    the node at an object. It follows the same rows as ``member`` but matches
    a holder only by its own term and its members, never as a class, and
    takes every active row whatever its condition. The result of ``A - B``
    is within ``A`` and that of ``A & B`` within both operands, so a site
    names the subjects of its left operand, and of its right one too when it
    is an intersection.
    """

    def __init__(
        self,
        program: IndexProgram,
        *,
        using: str,
        actor: SubjectRef | None,
        now: Expression,
        verdicts: _Verdicts | None = None,
        naming: bool = False,
    ) -> None:
        self.program = program
        self.using = using
        self.actor = actor
        self.now = now
        self.verdicts = verdicts
        self.naming = naming
        self._constant_sets = (
            actor_sets(actor, using=using, now=now, verdicts=verdicts)
            if actor is not None and not naming
            else None
        )

    def _condition(self, positive: bool) -> Q:
        """A definite result takes the rows that hold; a possible one, those that may."""
        if self.naming:
            return Q()
        if self.verdicts is None:
            return Q(condition__isnull=True) if positive else Q()
        return self.verdicts.condition(positive)

    def _direct(self, prefix: str, level: int) -> Q:
        """The terms that match the actor itself, in the query at ``level``."""
        if self.actor is not None:
            return _actor_q(self.actor, prefix) if self.naming else _direct_q(self.actor, prefix)
        # ``lookup_subjects``: the actor is the candidate term row at level 0.
        actor_type = _up("type", level)
        actor_id = _up("object_id", level)
        actor_relation = _up("relation", level)
        exact = Q(
            **{
                prefix + "type": actor_type,
                prefix + "object_id": actor_id,
                prefix + "relation": actor_relation,
            }
        )
        is_anon = Q(
            Exact(actor_type, Value(anonymous_actor().subject_type)),
            Exact(actor_id, Value("*")),
            Exact(actor_relation, Value("")),
        )
        wildcard_match = Q(
            Exact(actor_relation, Value("")),
            **{
                prefix + "type": actor_type,
                prefix + "object_id": "*",
                prefix + "relation": "",
            },
        )
        if self.naming:
            return exact
        authenticated_match = (
            ~is_anon & ~Q(Exact(actor_id, Value(""))) & _triple_q(AUTHENTICATED, prefix)
        )
        return (
            exact
            | wildcard_match
            | authenticated_match
            | (is_anon & _triple_q(anonymous(), prefix))
        )

    def _holder(self, positive: bool, level: int) -> Q:
        """``holder IN H(actor, polarity)`` for the grant rows at ``level``."""
        from ..models.index import IndexMember

        if self._constant_sets is not None:
            return Q(
                holder_id__in=(
                    self._constant_sets.definite if positive else self._constant_sets.possible
                )
            )
        members = IndexMember.objects.using(self.using).filter(
            active_q(self.now),
            self._direct("member__", level + 1),
            self._condition(positive),
            set_id=OuterRef("holder_id"),
        )
        return self._direct("holder__", level) | Q(Exists(members))

    def _grants(self, key: Key, positive: bool) -> QuerySet[Any]:
        from ..models.index import IndexCover

        return cast(
            QuerySet[Any],
            IndexCover.objects.using(self.using).filter(
                active_q(self.now),
                self._condition(positive),
                resource_type=key[0],
                node=key[1],
            ),
        )

    def _site(self, key: Key, x: _Object, positive: bool, level: int) -> Q:
        """``sat(site, x)`` for the query at ``level``."""
        node = self.program.nodes[key]
        assert node.operands is not None
        left = self.member((key[0], node.operands[0]), x, positive, level)
        if self.naming and node.kind == "minus":
            return left
        right = self.member(
            (key[0], node.operands[1]),
            x,
            positive if node.kind == "and" else not positive,
            level,
        )
        if self.naming:
            return left | right
        applies = right if node.kind == "and" else ~right
        if node.deadline is not None:
            # A timed site (disable/tighten override) is the identity from its
            # deadline on. The clock is read when the statement executes, so a
            # queryset built before the deadline and run after it is correct.
            applies |= Q(GreaterThanOrEqual(self.now, Value(node.deadline)))
        return left & applies

    def _match(self, key: Key, x: _Object, positive: bool, level: int) -> Q:
        """The row predicate of ``member``, for the grant rows at ``level``.

        A plain row matches when its holder is one of the actor's terms. A
        site row matches when the actor satisfies the site at the object the
        row points at. Each site is compiled once.
        """
        matched = Q(site="") & self._holder(positive, level)
        for site in sorted(self.program.held_sites(key)):
            # Site names are local to their resource type; the holder's type
            # tells which type's site a propagated row carries.
            matched |= Q(site=site[1], holder__type=site[0]) & self._site(
                site, _SiteObject(level, x), positive, level
            )
        return matched

    def member(self, key: Key, x: _Object, positive: bool, level: int = 0) -> Q:
        """The actor satisfies ``key`` at ``x``; a predicate for the query at ``level``."""
        from ..models.index import IndexMember, IndexTerm

        if key not in self.program.nodes:
            return Q(pk__in=[])
        terms = IndexTerm.objects.using(self.using)
        if key in self.program.userset_only_relations:
            # No node references this relation, so it has no grant rows; its
            # holders are the members of the set ``x#relation``.
            identity = Subquery(terms.filter(pk=x.at(level + 2)).values("object_id")[:1])
            members = IndexMember.objects.using(self.using).filter(
                active_q(self.now),
                self._condition(positive),
                self._direct("member__", level + 1),
                set__type=key[0],
                set__object_id=identity,
                set__relation=key[1],
            )
            return Q(Exists(members))
        type_scope = Subquery(
            terms.filter(type=key[0], object_id="*", relation="$type").values("pk")[:1]
        )
        rows = self._grants(key, positive).filter(
            Q(scope_id=x.at(level + 1)) | Q(scope_id=type_scope),
            self._match(key, x, positive, level + 1),
        )
        return Q(Exists(rows))


def member(
    node: Key,
    x: str | int,
    actor: SubjectRef | None,
    polarity: bool,
    *,
    program: IndexProgram,
    using: str,
    now: Expression,
    verdicts: _Verdicts | None = None,
) -> Q:
    tested: _Object = _Term(x) if isinstance(x, int) else _Column(x, 0)
    return _Compiler(program, using=using, actor=actor, now=now, verdicts=verdicts).member(
        node, tested, polarity
    )


def named(node: Key, x: int, *, program: IndexProgram, using: str, now: Expression) -> Q:
    """The candidate term row is named on a path to ``node`` at the term ``x``."""
    return _Compiler(program, using=using, actor=None, now=now, naming=True).member(
        node, _Term(x), True
    )


@schema_operation
def scope_q(model: type[models.Model], *, action: str, actor: SubjectRef, using: str) -> Q:
    active = _backend()
    with using_backend(active):
        active.schema()
        program = ensure_ready(using=using)
        resource_type = model_resource_type(model)
        if resource_type is None:
            return Q(pk__in=[])
        attr = resource_id_attr(model)
        codec = identity_codec(model, attr)
        from ..models.index import IndexTerm

        key = resource_type, action
        terms = IndexTerm.objects.using(using)

        def identity(row: OuterRef) -> Q:
            """The term of a model row; ``row`` refers to it from the query that asks."""
            return Q(
                type=resource_type, relation="", object_id=codec.to_wire(cast(Expression, row))
            )

        tested = identity(OuterRef(attr))
        if key in program.type_level_nodes:
            # A row without a term of its own is read at the type-level scope.
            interned = terms.filter(identity(OuterRef(OuterRef(attr))))
            tested |= Q(type=resource_type, object_id="*", relation="$type") & ~Q(Exists(interned))
        return Q(
            Exists(
                terms.filter(
                    tested,
                    Exists(_ready_rows(using=using, program=program)),
                    member(
                        key, "pk", actor, True, program=program, using=using, now=_ExecutionTime()
                    ),
                )
            )
        )


def _resource_term(resource: ObjectRef, *, using: str) -> int | None:
    from ..models.index import IndexTerm

    concrete = (
        IndexTerm.objects.using(using)
        .filter(type=resource.resource_type, object_id=resource.resource_id, relation="")
        .values_list("pk", flat=True)
        .first()
    )
    if concrete is not None:
        return cast(int, concrete)
    return cast(
        int | None,
        IndexTerm.objects.using(using)
        .filter(type=resource.resource_type, object_id="*", relation="$type")
        .values_list("pk", flat=True)
        .first(),
    )


@dataclass(frozen=True)
class _Lookup:
    """One node of a read plan, and the operands of the sites its rows can hold."""

    tag: int
    key: Key
    sites: Mapping[Key, tuple[_Lookup, _Lookup]]


def _plan(program: IndexProgram, key: Key) -> _Lookup:
    tags = count()

    def lookup(key: Key) -> _Lookup:
        tag = next(tags)
        sites = {}
        for site in sorted(program.held_sites(key)):
            operands = program.nodes[site].operands
            assert operands is not None
            sites[site] = lookup((site[0], operands[0])), lookup((site[0], operands[1]))
        return _Lookup(tag, key, sites)

    return lookup(key)


def _plan_rows(
    plan: _Lookup,
    tested: QuerySet[Any],
    *,
    actor: SubjectRef,
    program: IndexProgram,
    using: str,
    now: datetime,
) -> QuerySet[Any]:
    """Every row the plan can read for the actor at the tested term, as one statement.

    A site is evaluated at the tested term or at the holder of the row that
    holds it, so the rows of its operands are read at both. A row that holds
    no site is read only when the actor is its holder or a member of it. Each
    row carries the tested term, which the statement resolves.
    """
    from ..models.index import IndexCover, IndexTerm

    terms = IndexTerm.objects.using(using)
    own = terms.filter(_direct_q(actor)).values("pk")
    x = tested.values("pk")
    ready = Exists(_ready_rows(using=using, program=program, pinned=True))
    # A join condition takes a subquery expression, not a queryset.
    member = Q(holder__members__expires_at__gt=now, holder__members__member_id__in=Subquery(own))
    queries = []

    def add(lookup: _Lookup, objects: Q) -> None:
        type_, node = lookup.key
        type_scope = terms.filter(type=type_, object_id="*", relation="$type").values("pk")
        grants = IndexCover.objects.using(using).filter(
            ready,
            objects | Q(scope_id__in=type_scope),
            resource_type=type_,
            node=node,
            expires_at__gt=now,
        )
        queries.append(
            grants.alias(_member=FilteredRelation("holder__members", condition=member))
            .filter(~Q(site="") | Q(holder_id__in=own) | Q(_member__isnull=False))
            .order_by()
            .values_list(
                Value(lookup.tag),
                Subquery(x[:1]),
                "scope_id",
                "scope__relation",
                "holder_id",
                "holder__type",
                "holder__object_id",
                "holder__relation",
                "site",
                "condition",
                "_member__pk",
                "_member__condition",
            )
        )
        for site, operands in lookup.sites.items():
            holders = grants.filter(site=site[1], holder__type=site[0]).values("holder_id")
            for operand in operands:
                add(operand, objects | Q(scope_id__in=holders))

    add(plan, Q(scope_id__in=x))
    first, *rest = queries
    return cast(QuerySet[Any], first.union(*rest, all=True) if rest else first)


class _Evaluation(conditions.Evaluation):
    """Decide a read plan at an object from its fetched rows.

    The rows at a node are alternative paths. A path is the row's condition,
    then the membership that makes the actor a holder, or the site the row
    holds: an atom whose verdict is that of its operands.
    """

    def __init__(
        self,
        rows: Iterable[tuple[Any, ...]],
        *,
        program: IndexProgram,
        actor: SubjectRef,
        schema: Any,
        context: Mapping[str, Any] | None,
        now: datetime,
    ) -> None:
        super().__init__(schema, context)
        self.program = program
        self.actor = actor
        self.now = now
        self.tested: int | None = None
        self.rows: dict[int, list[tuple[Any, ...]]] = defaultdict(list)
        for tag, self.tested, *row in rows:
            self.rows[tag].append(tuple(row))

    def _holds(self, holder: Triple) -> bool:
        cls = classes.class_of(holder)
        if cls is not None and classes.matches(cls, self.actor):
            return True
        return holder == (
            self.actor.subject_type,
            self.actor.subject_id,
            self.actor.optional_relation,
        )

    def _site(self, lookup: _Lookup, site: Key, y: int) -> str:
        atom = f"{site[0]}#{site[1]}@{y}"
        if atom not in self.atoms:
            spec = self.program.nodes[site]
            left, right = (self.node(operand, y) for operand in lookup.sites[site])
            if spec.deadline is not None and self.now >= spec.deadline:
                self.atoms[atom] = left
            else:
                combine = tri_and if spec.kind == "and" else tri_minus
                value = combine(left[0], right[0])
                # A conditional site needs what its conditional operands need.
                needed = [names for verdict, names in (left, right) if verdict is None]
                self.atoms[atom] = (
                    value,
                    frozenset().union(*needed) if value is None else frozenset(),
                )
        return atom

    def node(self, lookup: _Lookup, x: int) -> conditions.Verdict:
        paths: list[conditions.Path] = []
        for scope, level, holder_id, *holder, site, condition, member, membership in self.rows[
            lookup.tag
        ]:
            if scope != x and level != "$type":
                continue
            if site:
                if (holder[0], site) not in lookup.sites:
                    continue
                # The same rule as ``_SiteObject``.
                at = x if holder_id == scope else holder_id
                matches = [frozenset({self._site(lookup, (holder[0], site), at)})]
            elif self._holds(cast(Triple, tuple(holder))):
                matches = [frozenset()]
            elif member is not None:
                matches = self.paths(membership)
            else:
                continue
            paths.extend(path | match for path in self.paths(condition) for match in matches)
        return self.decide(paths)


def _evaluate(
    key: Key,
    resource: ObjectRef,
    *,
    actor: SubjectRef,
    program: IndexProgram,
    schema: Any,
    context: Mapping[str, Any] | None,
    using: str,
) -> conditions.Verdict:
    """The three-valued result at a resource, from one statement of rows."""
    from ..models.index import IndexMember, IndexTerm

    now = index_time.index_now()
    terms = IndexTerm.objects.using(using)
    ready = Exists(_ready_rows(using=using, program=program, pinned=True))
    if key in program.userset_only_relations:
        # No node references this relation: its holders are the set's members.
        formulas = IndexMember.objects.using(using).filter(
            ready,
            expires_at__gt=now,
            member_id__in=terms.filter(_direct_q(actor)).values("pk"),
            set__type=key[0],
            set__object_id=resource.resource_id,
            set__relation=key[1],
        )
        members = conditions.Evaluation(schema, context)
        return members.decide(
            path
            for formula in formulas.values_list("condition", flat=True).iterator()
            for path in members.paths(formula)
        )
    concrete = Q(type=key[0], object_id=resource.resource_id, relation="")
    # A resource without a term of its own is read at the type-level scope.
    tested = terms.filter(
        ready,
        concrete
        | (Q(type=key[0], object_id="*", relation="$type") & ~Q(Exists(terms.filter(concrete)))),
    )
    plan = _plan(program, key)
    rows = _plan_rows(plan, tested, actor=actor, program=program, using=using, now=now)
    evaluation = _Evaluation(
        rows.iterator(), program=program, actor=actor, schema=schema, context=context, now=now
    )
    if evaluation.tested is None:
        return False, frozenset()
    return evaluation.node(plan, evaluation.tested)


@schema_operation
def check(
    *,
    resource: ObjectRef,
    action: str,
    actor: SubjectRef,
    context: Mapping[str, Any] | None,
    using: str,
) -> CheckResult:
    active = _backend()
    with using_backend(active):
        schema = active.schema()
        program = ensure_ready(using=using)
        definition = schema.get_definition(resource.resource_type)
        if definition is None:
            return CheckResult.no(reason=f"unknown resource type: {resource.resource_type}")
        if (
            schema.get_permission(resource.resource_type, action) is None
            and find_relation(definition, action) is None
        ):
            return CheckResult.no(reason=f"unknown action: {resource.resource_type}#{action}")
        key = resource.resource_type, action
        from ..models.index import IndexTerm

        terms = IndexTerm.objects.using(using).filter(
            Exists(_ready_rows(using=using, program=program, pinned=True))
        )
        conditional = key in program.conditional_nodes
        if resource.resource_id and conditional:
            # A caveat is in reach: read the rows once and decide in Python.
            verdict, missing = _evaluate(
                key,
                resource,
                actor=actor,
                program=program,
                schema=schema,
                context=context,
                using=using,
            )
            if verdict is None:
                return CheckResult.conditional(tuple(sorted(missing)))
            return CheckResult.has() if verdict else CheckResult.no()
        verdicts = (
            _Verdicts(
                key, actor=actor, program=program, schema=schema, context=context, using=using
            )
            if conditional and context is not None
            else None
        )
        holds = member(
            key,
            "pk",
            actor,
            True,
            program=program,
            using=using,
            now=_ExecutionTime(),
            verdicts=verdicts,
        )
        if not resource.resource_id:
            # A model-level check: the actor holds the node at the type-level
            # scope, or on any row.
            tested = terms.filter(
                Q(object_id="*", relation="$type") | (Q(relation="") & ~Q(object_id__in=("*", ""))),
                type=key[0],
            )
        else:
            concrete = Q(type=key[0], object_id=resource.resource_id, relation="")
            # A resource without a term of its own is read at the type-level scope.
            tested = terms.filter(
                concrete
                | (
                    Q(type=key[0], object_id="*", relation="$type")
                    & ~Q(Exists(IndexTerm.objects.using(using).filter(concrete)))
                )
            )
        return CheckResult.has() if tested.filter(holds).exists() else CheckResult.no()


def _term_resource_ids(*, resource_type: str, using: str) -> QuerySet[Any]:
    from ..models.index import IndexTerm

    return cast(
        QuerySet[Any],
        IndexTerm.objects.using(using)
        .filter(type=resource_type, relation="")
        .exclude(object_id__in=("*", ""))
        .order_by()
        .values_list("object_id", flat=True),
    )


def resource_ids(
    *,
    resource_type: str,
    using: str,
    allowed: Iterable[str] | None = None,
) -> QuerySet[Any]:
    concrete = _term_resource_ids(resource_type=resource_type, using=using)
    if allowed is not None:
        concrete = concrete.filter(object_id__in=allowed)
    model = model_for_resource_type(resource_type)
    if model is not None and stores_rows(model):
        codec = identity_codec(model)
        rows = (
            model._base_manager.using(using)
            .order_by()
            .annotate(_wire_id=codec.to_wire(resource_id_attr(model)))
            .exclude(_wire_id__isnull=True)
            .values_list("_wire_id", flat=True)
        )
        if allowed is not None:
            rows = rows.filter(_wire_id__in=allowed)
        return cast(QuerySet[Any], rows.union(concrete))
    return concrete


def _grants_all(*, resource_type: str, action: str, actor: SubjectRef, using: str) -> bool:
    """The actor holds the node on every row: no site can take a row away."""
    from ..models.index import IndexTerm

    _backend().schema()
    program = ensure_ready(using=using)
    key = resource_type, action
    if program.held_sites(key):
        return False
    type_scope = IndexTerm.objects.using(using).filter(
        type=resource_type, object_id="*", relation="$type"
    )
    return cast(
        bool,
        type_scope.filter(
            Exists(_ready_rows(using=using, program=program, pinned=True)),
            member(key, "pk", actor, True, program=program, using=using, now=_ExecutionTime()),
        ).exists(),
    )


@schema_operation
def accessible_ids(
    *,
    resource_type: str,
    action: str,
    actor: SubjectRef,
    using: str,
    context: Mapping[str, Any] | None = None,
) -> QuerySet[Any]:
    """The resources the actor definitely holds the node on, as a lazy queryset."""
    from ..models.index import IndexTerm

    active = _backend()
    with using_backend(active):
        schema = active.schema()
        program = ensure_ready(using=using)
        key = resource_type, action
        verdicts = (
            _Verdicts(
                key, actor=actor, program=program, schema=schema, context=context, using=using
            )
            if context is not None and key in program.conditional_nodes
            else None
        )
        return cast(
            QuerySet[Any],
            IndexTerm.objects.using(using)
            .filter(
                Exists(_ready_rows(using=using, program=program, pinned=verdicts is not None)),
                member(
                    key,
                    "pk",
                    actor,
                    True,
                    program=program,
                    using=using,
                    now=_ExecutionTime(),
                    verdicts=verdicts,
                ),
                type=resource_type,
                relation="",
            )
            .exclude(object_id__in=("*", ""))
            .order_by()
            .values_list("object_id", flat=True),
        )


def subject_candidates(
    *, subject_type: str, key: Key, program: IndexProgram, using: str
) -> QuerySet[Any]:
    """Terms of the type that some row of the node or its operands holds.

    This narrows the terms to test and ignores where a row applies; ``named``
    and ``member`` decide. Class holders name no subject and contribute none.
    """
    from ..models.index import IndexCover, IndexMember, IndexTerm

    keys = {key}
    pending = [key]
    while pending:
        for site in program.held_sites(pending.pop()):
            spec = program.nodes[site]
            assert spec.operands is not None
            operands = spec.operands if spec.kind == "and" else spec.operands[:1]
            for operand in operands:
                if (site[0], operand) not in keys:
                    keys.add((site[0], operand))
                    pending.append((site[0], operand))
    selected = Q(pk__in=[])
    usersets = Q(pk__in=[])
    for type_, node in sorted(keys):
        if (type_, node) in program.userset_only_relations:
            usersets |= Q(type=type_, relation=node)
        else:
            selected |= Q(resource_type=type_, node=node)
    holders = (
        IndexCover.objects.using(using)
        .filter(selected, site="")
        .order_by()
        .values_list("holder_id", flat=True)
    )
    # A relation no node references has no grant rows: its sets hold the members.
    sets = IndexTerm.objects.using(using).filter(usersets).order_by().values_list("pk", flat=True)
    members = (
        IndexMember.objects.using(using)
        .filter(set_id__in=holders.union(sets), member_type=subject_type)
        .order_by()
        .values_list("member_id", flat=True)
    )
    return cast(
        QuerySet[Any],
        IndexTerm.objects.using(using)
        .filter(type=subject_type, pk__in=holders.union(members))
        .exclude(relation__startswith="$")
        .exclude(object_id="")
        .order_by(),
    )


@schema_operation
def lookup_subjects(
    *,
    resource: ObjectRef,
    action: str,
    subject_type: str,
    using: str,
    context: Mapping[str, Any] | None = None,
) -> list[SubjectRef]:
    active = _backend()
    with using_backend(active):
        schema = active.schema()
        program = ensure_ready(using=using)
        x = _resource_term(resource, using=using)
        if x is None:
            return []
        key = resource.resource_type, action
        now = _ExecutionTime()
        candidates = subject_candidates(
            subject_type=subject_type, key=key, program=program, using=using
        ).filter(
            Exists(_ready_rows(using=using, program=program, pinned=True)),
            named(key, x, program=program, using=using, now=now),
        )
        verdicts = (
            _Verdicts(key, actor=None, program=program, schema=schema, context=context, using=using)
            if context is not None and key in program.conditional_nodes
            else None
        )
        listed = candidates.filter(
            member(key, x, None, True, program=program, using=using, now=now, verdicts=verdicts)
        )
        return sorted(
            (
                SubjectRef.of(subject_type, object_id, relation)
                for object_id, relation in listed.values_list("object_id", "relation").iterator()
            ),
            key=str,
        )
