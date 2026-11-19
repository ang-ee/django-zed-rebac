"""Decide a bounded question from the rows it names (proposal 0020).

``held`` asks which of some ids an actor holds an action on. The compiled answer
is one statement over the whole reachable policy, however few the ids. With
``REBAC_BOUNDED_WALK`` set, the question is read from the rows the ids name, by
small indexed lookups, and the compiled statement is asked only what they leave.

``"prove"`` (or ``True``) is **sound and incomplete**. It reads the arms of a
permission's top-level union, where one arm that holds grants the permission
whatever the others say, and of those only three kinds: a stored relation that
names the actor or its type's declared wildcard, a foreign key that names the
actor's own row, and an arrow through one foreign key to parents it proves by
the same rules. It never answers no: every id it does not prove goes to the
compiled statement.

``"decide"`` reads the whole permission, and answers no as well. It evaluates
the expression as the compiler does (unions, intersections, exclusions, stored
relations with their subject sets and arrows, foreign-key and constant
backings), one lookup per relation on the ids still open. A part it cannot read
is asked of the compiled statement for that part alone:

- a recursive component and a relation that admits a conditional (caveated)
  grant: what holds and what may hold differ there, and only the compiler reads
  both;
- an attribute, generic, filtered or multi-valued backing, a non-integer
  identity, a foreign key the database does not enforce, a subject-set wildcard,
  and a relation with more subject rows on the candidates than ``_ROWS``.

Below an exclusion nothing can be asked of the compiler (the excluded side is
read at its upper bound, a compiled answer is the lower one), so the walk hands
the nearest permission above the exclusion over whole. Candidates that name no
row of their type keep their compiled answer.

Both modes leave a caveat context, an override, an anonymous actor and an actor
that is a subject set to the compiled statement, so ``held`` returns the same
set whatever the setting.

A foreign key is read from its column: its target is taken to be a row, which
the database guarantees for an enforced key once the transaction commits.

**Consistency.** The compiled answer is one statement, which reads one state of
the data. The walk is several: each lookup reads the state committed when it
runs. Under READ COMMITTED a grant, a foreign key or a blocking row that
changes between two lookups can give an answer that held in neither state (a
row read under a parent it has since left; an exclusion read after it was
lifted and the grant withdrawn). Inside one snapshot the walk is exact: a
transaction on a connection configured for repeatable read
(``snapshot.isolated``), or any transaction on SQLite. Whether a bounded
decision may be assembled from adjacent states is the application's decision,
as it is for a prefetch; that is why the setting is off by default. A policy
revision published during a walk is handled: after an exclusion the fence is
read again, and a walk it closed under answers nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import TYPE_CHECKING, Any, cast

from django.db import models
from django.db.models import Exists, Q

from ._id import _NATIVE_INTEGER_FIELDS, model_identity_fields, resource_id_attr
from .actors import is_anonymous_actor
from .clock import application_now
from .codec import identity_codec
from .field_backing import ResolvedFieldBacking, resolve_const_backing, resolve_field_backing
from .resources import model_for_resource_type, stores_rows
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
from .structural import _union_arms
from .types import SubjectRef

if TYPE_CHECKING:
    from .backends.local import LocalBackend


def _relationships() -> Any:
    """The relationship model, imported when a walk reads it.

    ``rebac.managers`` imports this module, and applications import the managers
    before the app registry is populated; a model cannot be imported then.
    """
    from .models import active_relationship_model

    return active_relationship_model()


# Arrows followed from the question's own type before the proving walk gives up.
_DEPTH = 4
# Subject rows of one relation on the candidates past which the compiler is asked instead.
_ROWS = 2000
# Candidates decided together.
_CHUNK = 500

Key = tuple[str, str]


def mode() -> str:
    """``"prove"``, ``"decide"`` or empty for off."""
    from .conf import app_settings

    value = app_settings.REBAC_BOUNDED_WALK
    if value is True or value == "prove":
        return "prove"
    return "decide" if value == "decide" else ""


def enabled() -> bool:
    return bool(mode())


def _walk(
    backend: LocalBackend, actor: SubjectRef, using: str, context: Mapping[str, Any] | None
) -> _Walk | None:
    """The walk for one question, or ``None`` where the compiled statement answers alone."""
    if context is not None:
        return None
    if actor.optional_relation or actor.subject_id in ("", "*") or is_anonymous_actor(actor):
        return None
    from .compile.read import _policy

    policy = _policy(backend)
    # An override retags arms the walk would read as plain expressions.
    if policy.tagged.arms or policy.tagged.sites:
        return None
    return _Walk(policy, backend, actor, using)


def proved(
    *,
    backend: LocalBackend,
    resource_type: str,
    action: str,
    actor: SubjectRef,
    ids: Iterable[str],
    using: str,
    context: Mapping[str, Any] | None = None,
) -> set[str]:
    """The ids among ``ids`` the proving walk shows the actor holds ``action`` on."""
    walk = _walk(backend, actor, using, context)
    if walk is None:
        return set()
    return walk.holds(resource_type, action, walk.canonical(resource_type, set(ids)), frozenset())


def decided(
    *,
    backend: LocalBackend,
    resource_type: str,
    action: str,
    actor: SubjectRef,
    ids: Iterable[str],
    using: str,
    context: Mapping[str, Any] | None = None,
) -> tuple[set[str], set[str]]:
    """``(held, answered)``: the ids the walk holds, and those whose answer is final.

    In ``"decide"`` mode every candidate that is a row of its type is answered,
    held or not. In ``"prove"`` mode, and where the walk cannot read the
    question, only what it proved is.
    """
    return decided_many(
        backend=backend,
        resource_type=resource_type,
        actions=(action,),
        actor=actor,
        ids=ids,
        using=using,
        context=context,
    )[action]


def decided_many(
    *,
    backend: LocalBackend,
    resource_type: str,
    actions: Iterable[str],
    actor: SubjectRef,
    ids: Iterable[str],
    using: str,
    context: Mapping[str, Any] | None = None,
) -> dict[str, tuple[set[str], set[str]]]:
    """``decided`` for several actions on the same candidates, their shared parts read once."""
    names = tuple(dict.fromkeys(actions))
    result: dict[str, tuple[set[str], set[str]]] = {name: (set(), set()) for name in names}
    candidates = sorted(set(ids))
    walk = _walk(backend, actor, using, context)
    if walk is None or not candidates:
        return result
    deciding = [
        name for name in names if mode() == "decide" and not walk.handed_over((resource_type, name))
    ]
    for start in range(0, len(candidates), _CHUNK):
        chunk = set(candidates[start : start + _CHUNK])
        rows = walk.existing(resource_type, chunk) if deciding else None
        for name in names:
            held, answered = result[name]
            found = (
                walk.exact(resource_type, name, rows, frozenset(), negated=False)
                if rows is not None and name in deciding
                else None
            )
            if found is None:
                found = walk.holds(
                    resource_type, name, walk.canonical(resource_type, chunk), frozenset()
                )
                answered |= found
            else:
                assert rows is not None
                answered |= rows
            held |= found
    if walk.excluded and not walk.open():
        # A policy published while the walk read: a fenced lookup of an excluded
        # side read nothing and excluded nobody. Nothing is answered; the compiled
        # statement, fenced as a whole, decides.
        return {name: (set(), set()) for name in names}
    return result


class _Walk:
    def __init__(self, policy: Any, backend: LocalBackend, actor: SubjectRef, using: str) -> None:
        self.policy = policy
        self.schema = policy.schema
        self.program = policy.program
        self.backend = backend
        self.actor = actor
        self.using = using
        # What exact() has settled: key -> id -> held.
        self._known: dict[Key, dict[str, bool]] = {}
        # What the proving walk has read of a relation: key -> id -> proved.
        self._proved: dict[Key, dict[str, bool]] = {}
        self._costly: dict[Key, bool] = {}
        # Whether an exclusion was read: its lookups are fenced, and a fence that
        # closed under them would have excluded nobody (``decided_many``).
        self.excluded = False
        from .compile.read import _fence

        # Every lookup reads nothing while the published policy revision is not
        # this policy's: the walk then answers nothing, and the compiled
        # statement, fenced the same way, denies.
        self.fence = Q(Exists(_fence(policy, backend=backend, using=using, parametric=False)))

    def open(self) -> bool:
        """Whether the fence admits reads now, for a question that makes no lookup of its own."""
        from .models.generation import SchemaGeneration

        return SchemaGeneration.objects.using(self.using).filter(pk=1).filter(self.fence).exists()

    # ---------- Deciding: the whole of a permission ----------

    def existing(self, resource_type: str, ids: set[str]) -> set[str] | None:
        """The candidates the walk can answer: the rows of the type among them.

        Every candidate of a type that stores no rows. ``None`` where the
        type's identity is not a plain integer key.
        """
        model = model_for_resource_type(resource_type)
        if model is None or not stores_rows(model):
            return set(ids) if self.open() else set()
        identity = _integer_identity(model)
        if identity is None:
            return None
        keys = _keys(model, identity, ids, self.using)
        if not keys:
            return set()
        found = (
            model._base_manager.using(self.using)
            .filter(**{f"{identity.attname}__in": keys})
            .filter(self.fence)
            .order_by()
            .values_list(identity.attname, flat=True)
        )
        return {str(key) for key in found} & ids

    def canonical(self, resource_type: str, ids: set[str]) -> set[str]:
        """The candidates that can name a row of the type: the compiled check drops the others."""
        model = model_for_resource_type(resource_type)
        if model is None or not stores_rows(model):
            return ids
        codec = identity_codec(model, resource_id_attr(model))
        return {value for value in ids if codec.is_canonical(value, using=self.using)}

    def handed_over(self, key: Key) -> bool:
        """Whether ``key`` is the compiler's to read whole.

        In a recursive component and on a relation that admits a caveated
        subject, what holds and what may hold differ, and the walk reads one
        answer for both.
        """
        if key in self.program.recursive or key in self.program.alias_cycles:
            return True
        definition = self.schema.get_definition(key[0])
        relation = (
            next((r for r in definition.relations if r.name == key[1]), None)
            if definition
            else None
        )
        return relation is not None and any(
            allowed.with_caveat for allowed in relation.allowed_subjects
        )

    def _reaches_compiler(self, key: Key) -> bool:
        """Whether reading ``key`` may ask the compiler: such arms are read last."""
        found = self._costly.get(key)
        if found is None:
            reach = {key}
            if key in self.program.dependencies:
                reach |= self.program.reachable(key)
            found = self._costly[key] = any(self.handed_over(node) for node in reach)
        return found

    def _arm_reaches_compiler(self, definition: Definition, arm: PermExpr) -> bool:
        if isinstance(arm, PermRef):
            return self._reaches_compiler((definition.resource_type, arm.name))
        if isinstance(arm, PermArrow):
            relation = next((r for r in definition.relations if r.name == arm.via), None)
            return self._reaches_compiler((definition.resource_type, arm.via)) or any(
                self._reaches_compiler((allowed.type, arm.target))
                for allowed in (relation.allowed_subjects if relation is not None else ())
            )
        if isinstance(arm, PermBinOp):
            return self._arm_reaches_compiler(definition, arm.left) or self._arm_reaches_compiler(
                definition, arm.right
            )
        return False

    def exact(
        self,
        resource_type: str,
        name: str,
        ids: set[str],
        path: frozenset[Key],
        *,
        negated: bool,
    ) -> set[str] | None:
        """Exactly the ids on which the actor holds ``name``; ``None`` where that cannot be said.

        ``negated`` is set below an exclusion, where no part may be asked of the
        compiled statement.
        """
        key = (resource_type, name)
        if not ids or key not in self.program.dependencies:
            return set()
        known = self._known.setdefault(key, {})
        unknown = {value for value in ids if value not in known}
        if unknown:
            found = self._evaluate(key, unknown, path, negated)
            if found is None:
                return None
            for value in unknown:
                known[value] = value in found
        return {value for value in ids if known[value]}

    def _evaluate(
        self, key: Key, ids: set[str], path: frozenset[Key], negated: bool
    ) -> set[str] | None:
        if key in path or self.handed_over(key):
            return None if negated else self._compiled(key, ids)
        definition = self.schema.get_definition(key[0])
        if definition is None:
            return set()
        path = path | {key}
        relation = next((r for r in definition.relations if r.name == key[1]), None)
        if relation is not None:
            found = self._through(definition, relation, None, ids, path, negated)
            return self._compiled(key, ids) if found is None and not negated else found
        permission = self.schema.get_permission(*key)
        if permission is None:
            return set()
        # The arms of the permission's own union: one that cannot be read leaves
        # only the ids the others did not grant to the compiled statement.
        found = set()
        unread = False
        for arm in self._ordered(definition, permission.expression):
            left = ids - found
            if not left:
                break
            part = self._expression(definition, arm, left, path, negated)
            if part is not None:
                found |= part
            elif negated:
                return None
            else:
                unread = True
        if unread and ids - found:
            rest = self._compiled(key, ids - found)
            if rest is None:
                return None
            found |= rest
        return found

    def _ordered(self, definition: Definition, expr: PermExpr) -> list[PermExpr]:
        """The arms of a union, those that may ask the compiler last."""
        arms = list(_flat_union(expr))
        return sorted(arms, key=lambda arm: self._arm_reaches_compiler(definition, arm))

    def _compiled(self, key: Key, ids: set[str]) -> set[str] | None:
        """The compiled answer for one part of the question, on the ids that reach it.

        ``None`` for an id that is not a canonical identity of a type with rows:
        the bounded check drops it, where a statement reading the part in place
        would still match it against stored rows.
        """
        from .compile.read import held

        model = model_for_resource_type(key[0])
        if model is not None and stores_rows(model):
            codec = identity_codec(model, resource_id_attr(model))
            if not all(codec.is_canonical(value, using=self.using) for value in ids):
                return None
        return held(
            backend=self.backend,
            resource_type=key[0],
            action=key[1],
            actor=self.actor,
            ids=sorted(ids),
            using=self.using,
        )

    def _expression(
        self,
        definition: Definition,
        expr: PermExpr,
        ids: set[str],
        path: frozenset[Key],
        negated: bool,
    ) -> set[str] | None:
        if not ids or isinstance(expr, PermNil):
            return set()
        if isinstance(expr, PermRef):
            # The walk's actor is a plain subject with an id: authenticated, not anonymous.
            if expr.name == "authenticated":
                return set(ids)
            if expr.name == "anonymous":
                return set()
            return self.exact(definition.resource_type, expr.name, ids, path, negated=negated)
        if isinstance(expr, PermArrow):
            relation = next((r for r in definition.relations if r.name == expr.via), None)
            if relation is None:
                return set()
            return self._through(definition, relation, expr.target, ids, path, negated)
        if not isinstance(expr, PermBinOp):
            return None
        if expr.op == "+":
            found: set[str] = set()
            for arm in self._ordered(definition, expr):
                part = self._expression(definition, arm, ids - found, path, negated)
                if part is None:
                    return None
                found |= part
            return found
        left = self._expression(definition, expr.left, ids, path, negated)
        if left is None or expr.op not in ("&", "-"):
            return None
        if expr.op == "&":
            right = self._expression(definition, expr.right, left, path, negated)
            return None if right is None else left & right
        # What is excluded is read whole: nothing below may be asked of the compiler.
        self.excluded = True
        right = self._expression(definition, expr.right, left, path, True)
        return None if right is None else left - right

    def _through(
        self,
        definition: Definition,
        relation: Relation,
        target: str | None,
        ids: set[str],
        path: frozenset[Key],
        negated: bool,
    ) -> set[str] | None:
        """The ids with a subject of ``relation`` that is the actor, or holds ``target``."""
        if isinstance(relation.backing, FieldBinding):
            return self._through_field(definition, relation, target, ids, path, negated)
        if isinstance(relation.backing, ConstBinding):
            return self._through_constant(definition, relation, target, ids, path, negated)
        if relation.backing is not None:
            return None
        return self._through_stored(definition, relation, target, ids, path, negated)

    def _through_field(
        self,
        definition: Definition,
        relation: Relation,
        target: str | None,
        ids: set[str],
        path: frozenset[Key],
        negated: bool,
    ) -> set[str] | None:
        backed = self._backed(definition, relation)
        if backed is None:
            return None
        resolved, source = backed
        [allowed] = relation.allowed_subjects
        if allowed.wildcard or allowed.id or allowed.with_caveat:
            return None
        if allowed.relation and not self._is_relation(allowed.type, allowed.relation):
            return set()
        name = target if target is not None else (allowed.relation or None)
        pairs = list(self._targets(resolved, source, ids))
        if name is None:
            if allowed.type != self.actor.subject_type:
                return set()
            codec = identity_codec(resolved.target_model, resolved.target_id_attr)
            if not codec.is_canonical(self.actor.subject_id, using=self.using):
                return set()
            return {key for key, subject in pairs if subject == self.actor.subject_id}
        members = self.exact(
            allowed.type, name, {subject for _key, subject in pairs}, path, negated=negated
        )
        if members is None:
            return None
        return {key for key, subject in pairs if subject in members}

    def _through_constant(
        self,
        definition: Definition,
        relation: Relation,
        target: str | None,
        ids: set[str],
        path: frozenset[Key],
        negated: bool,
    ) -> set[str] | None:
        """A fixed object: the relation holds on every id its filters select, or on none."""
        resolved = resolve_const_backing(definition, relation)
        if resolved is None or len(relation.allowed_subjects) != 1:
            return None
        [allowed] = relation.allowed_subjects
        if allowed.wildcard or allowed.id or allowed.with_caveat:
            return None
        if allowed.relation and not self._is_relation(allowed.type, allowed.relation):
            return set()
        filters = resolved.filters
        identity = _integer_identity(resolved.source_model) if filters else None
        if filters and identity is None:
            return None
        name = target if target is not None else (allowed.relation or None)
        if name is None:
            reached = (
                allowed.type == self.actor.subject_type
                and resolved.target_id == self.actor.subject_id
            )
        else:
            holds = self.exact(allowed.type, name, {resolved.target_id}, path, negated=negated)
            if holds is None:
                return None
            reached = resolved.target_id in holds
        if not reached:
            return set()
        if identity is None:
            return set(ids)
        # Only the rows its source-column filters select carry the edge.
        keys = _keys(resolved.source_model, identity, ids, self.using)
        if not keys:
            return set()
        rows = (
            resolved.source_model._base_manager.using(self.using)
            .filter(**{f"{identity.attname}__in": keys})
            .filter(**filters)
            .filter(self.fence)
            .order_by()
            .values_list(identity.attname, flat=True)
        )
        return {str(key) for key in rows} & ids

    def _through_stored(
        self,
        definition: Definition,
        relation: Relation,
        target: str | None,
        ids: set[str],
        path: frozenset[Key],
        negated: bool,
    ) -> set[str] | None:
        """Live, uncaveated relationship rows on the ids, by the shapes the relation declares."""
        read_as = self.stored_shapes(relation, target)
        if read_as is None:
            return None
        named, through = read_as
        live = Q(expires_at__isnull=True)
        if relation.with_expiration:
            live |= Q(expires_at__gt=application_now())
        rows = (
            _relationships()
            .objects.using(self.using)
            .filter(
                live,
                resource_type=definition.resource_type,
                relation=relation.name,
                caveat_name="",
            )
            .filter(self.fence)
            .order_by()
        )
        if not named and not through:
            return set()
        # One lookup: the rows that name the actor, and the rows whose subject is asked.
        shapes = Q()
        if named:
            shapes |= Q(
                subject_type=self.actor.subject_type,
                optional_subject_relation="",
                subject_id__in=sorted(named),
            )
        for subject_type, subject_relation in sorted(through):
            shapes |= Q(subject_type=subject_type, optional_subject_relation=subject_relation) & ~Q(
                subject_id="*"
            )
        found: set[str] = set()
        subjects: dict[Key, list[tuple[str, str]]] = {}
        read = 0
        for resource_id, subject_type, subject_relation, subject_id in rows.filter(
            shapes, resource_id__in=sorted(ids)
        ).values_list("resource_id", "subject_type", "optional_subject_relation", "subject_id")[
            : _ROWS + 1
        ]:
            read += 1
            shape = (str(subject_type), str(subject_relation))
            if shape in through and subject_id != "*":
                subjects.setdefault(shape, []).append((str(resource_id), str(subject_id)))
            else:
                found.add(str(resource_id))
        if read > _ROWS:
            return None
        found &= ids
        for (subject_type, subject_relation), pairs in sorted(subjects.items()):
            pairs = [
                (resource_id, subject)
                for resource_id, subject in pairs
                if resource_id in ids and resource_id not in found
            ]
            name = target if target is not None else subject_relation
            members = self.exact(
                subject_type, name, {subject for _id, subject in pairs}, path, negated=negated
            )
            if members is None:
                return None
            found |= {resource_id for resource_id, subject in pairs if subject in members}
        return found

    def stored_shapes(
        self, relation: Relation, target: str | None
    ) -> tuple[set[str], set[Key]] | None:
        """How a stored relation's rows are read for the actor, as the compiler groups them.

        ``named``: the subject ids that are the actor itself (its id, its type's
        wildcard). ``through``: the ``(subject type, subject relation)`` shapes
        whose subjects are then asked (the relation is empty for an arrow).
        ``None`` for a shape the walk does not read: a caveated subject, a
        wildcard or a fixed id on a subject set or an arrow. The walk and the row
        scope both read a stored relation through this, so they cannot come to
        read the same policy differently.
        """
        named: set[str] = set()
        through: set[Key] = set()
        for allowed in relation.allowed_subjects:
            if allowed.with_caveat:
                return None
            if allowed.relation and not self._is_relation(allowed.type, allowed.relation):
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
        return named, through

    def _is_relation(self, resource_type: str, name: str) -> bool:
        definition = self.schema.get_definition(resource_type)
        return definition is not None and any(r.name == name for r in definition.relations)

    # ---------- Proving: what a few lookups show, and no more ----------

    def holds(
        self, resource_type: str, name: str, ids: set[str], path: frozenset[tuple[str, str]]
    ) -> set[str]:
        """The ids proved to hold permission or relation ``name`` on ``resource_type``."""
        if not ids or (resource_type, name) in path or len(path) > _DEPTH:
            return set()
        definition = self.schema.get_definition(resource_type)
        if definition is None:
            return set()
        relations = {relation.name: relation for relation in definition.relations}
        permission = self.schema.get_permission(resource_type, name)
        if permission is not None:
            arms = list(_union_arms(definition, permission.expression, frozenset({name})))
        elif name in relations:
            arms = [PermRef(name)]
        else:
            return set()
        path = path | {(resource_type, name)}
        found: set[str] = set()
        for arm in arms:
            left = ids - found
            if not left:
                break
            if isinstance(arm, PermRef):
                relation = relations.get(arm.name)
                if relation is not None:
                    found |= self._relation(definition, relation, left)
            elif isinstance(arm, PermArrow):
                relation = relations.get(arm.via)
                if relation is not None:
                    found |= self._arrow(definition, relation, arm.target, left, path)
        return found

    def _relation(self, definition: Definition, relation: Relation, ids: set[str]) -> set[str]:
        """The ids proved through one relation; an id is read once however many actions name it."""
        known = self._proved.setdefault((definition.resource_type, relation.name), {})
        unread = {value for value in ids if value not in known}
        if unread:
            if relation.backing is None:
                found = self._stored(definition, relation, unread)
            elif isinstance(relation.backing, FieldBinding):
                found = self._column(definition, relation, unread)
            else:
                found = set()
            for value in unread:
                known[value] = value in found
        return {value for value in ids if known[value]}

    def _stored(self, definition: Definition, relation: Relation, ids: set[str]) -> set[str]:
        """Live, uncaveated rows of a stored relation that name the actor or its type's wildcard."""
        subjects: list[str] = []
        for allowed in relation.allowed_subjects:
            if allowed.type != self.actor.subject_type or allowed.relation or allowed.with_caveat:
                continue
            if allowed.wildcard:
                subjects.append("*")
            elif not allowed.id or allowed.id == self.actor.subject_id:
                subjects.append(self.actor.subject_id)
        if not subjects:
            return set()
        live = Q(expires_at__isnull=True)
        if relation.with_expiration:
            live |= Q(expires_at__gt=application_now())
        rows = (
            _relationships()
            .objects.using(self.using)
            .filter(
                live,
                resource_type=definition.resource_type,
                relation=relation.name,
                resource_id__in=sorted(ids),
                subject_type=self.actor.subject_type,
                subject_id__in=sorted(set(subjects)),
                optional_subject_relation="",
                caveat_name="",
            )
            .filter(self.fence)
            .order_by()
            .values_list("resource_id", flat=True)
        )
        return {str(resource_id) for resource_id in rows} & ids

    def _backed(
        self, definition: Definition, relation: Relation
    ) -> tuple[ResolvedFieldBacking, models.Field[Any, Any]] | None:
        """A backing the walk can read: one enforced foreign key between integer identities.

        ``backing_obstacle`` is the one list of what makes a backing unreadable.
        """
        if backing_obstacle(definition, relation) is not None:
            return None  # where it does not resolve, the compiler reports it
        resolved = resolve_field_backing(definition, relation)
        assert resolved is not None
        source = _integer_identity(resolved.source_model)
        assert source is not None
        return resolved, source

    def _targets(
        self, resolved: ResolvedFieldBacking, source: models.Field[Any, Any], ids: set[str]
    ) -> Iterator[tuple[str, str]]:
        """``(source id, target id)`` for the candidate rows that carry the edge.

        Their foreign key is set, and the backing's source-column filters select them.
        """
        keys = _keys(resolved.source_model, source, ids, self.using)
        if not keys:
            return
        rows = (
            resolved.source_model._base_manager.using(self.using)
            .filter(**{f"{source.attname}__in": keys})
            .filter(**resolved.filters)
            .filter(self.fence)
            .order_by()
            .values_list(source.attname, cast(models.ForeignKey[Any, Any], resolved.field).attname)
        )
        for key, target in rows:
            # An id is its row's only when it is that row's canonical spelling.
            if target is not None and str(key) in ids:
                yield str(key), str(target)

    def _column(self, definition: Definition, relation: Relation, ids: set[str]) -> set[str]:
        """Rows whose foreign key names the actor's own row."""
        backed = self._backed(definition, relation)
        if backed is None:
            return set()
        resolved, source = backed
        [allowed] = relation.allowed_subjects
        if (
            allowed.type != self.actor.subject_type
            or allowed.relation
            or allowed.wildcard
            or allowed.id
            or allowed.with_caveat
        ):
            return set()
        codec = identity_codec(resolved.target_model, resolved.target_id_attr)
        if not codec.is_canonical(self.actor.subject_id, using=self.using):
            return set()
        return {
            key
            for key, target in self._targets(resolved, source, ids)
            if target == self.actor.subject_id
        }

    def _arrow(
        self,
        definition: Definition,
        relation: Relation,
        target: str,
        ids: set[str],
        path: frozenset[tuple[str, str]],
    ) -> set[str]:
        """Rows whose foreign key names a parent the walk proves ``target`` on."""
        if not isinstance(relation.backing, FieldBinding):
            return set()
        backed = self._backed(definition, relation)
        if backed is None:
            return set()
        resolved, source = backed
        [allowed] = relation.allowed_subjects
        if allowed.relation or allowed.wildcard or allowed.id or allowed.with_caveat:
            return set()
        pairs = list(self._targets(resolved, source, ids))
        parents = self.holds(allowed.type, target, {parent for _key, parent in pairs}, path)
        return {key for key, parent in pairs if parent in parents}


def backing_obstacle(definition: Definition, relation: Relation) -> str | None:
    """Why the walk does not read a field backing; ``None`` when it does (``_Walk._backed``)."""
    try:
        resolved = resolve_field_backing(definition, relation)
    except ValueError:
        return "a backing that does not resolve"
    if resolved is None:
        return "a backing that does not resolve"
    if resolved.generic is not None:
        return "a generic foreign key"
    if "__" in resolved.path:
        return "a path through several relations"
    if not isinstance(resolved.field, (models.ForeignKey, models.OneToOneField)):
        return "a reverse or many-to-many relation"
    if not resolved.single_valued:
        return "filters that follow a to-many relation"
    if not getattr(resolved.field, "db_constraint", False):
        return "a foreign key the database does not enforce"
    if not resolved.targets_identity_directly():
        return "a foreign key to a column that is not the target's identity"
    if len(relation.allowed_subjects) != 1:
        return "several subject types"
    if _integer_identity(resolved.source_model) is None:
        return "a source key that is not an integer"
    if _integer_identity(resolved.target_model) is None:
        return "a target key that is not an integer"
    return None


def _keys(
    model: type[models.Model], identity: models.Field[Any, Any], ids: Iterable[str], using: str
) -> list[Any]:
    """The candidates that are canonical identities of ``model``, as column values."""
    codec = identity_codec(model, resource_id_attr(model))
    return [identity.to_python(value) for value in ids if codec.is_canonical(value, using=using)]


def _flat_union(expr: PermExpr) -> Iterator[PermExpr]:
    """The operands of a union, nested unions opened."""
    if isinstance(expr, PermBinOp) and expr.op == "+":
        yield from _flat_union(expr.left)
        yield from _flat_union(expr.right)
        return
    yield expr


def _integer_identity(model: type[models.Model]) -> models.Field[Any, Any] | None:
    """The model's identity column when a wire id is that column's integer, spelled plainly."""
    if model_for_resource_type_of(model) is None:
        return None
    identity = resource_id_attr(model)
    field, scalar = model_identity_fields(model, identity)
    if field is not scalar or type(scalar) not in _NATIVE_INTEGER_FIELDS or not scalar.primary_key:
        return None
    return scalar


def model_for_resource_type_of(model: type[models.Model]) -> Any:
    from .resources import model_resource_type

    resource_type = model_resource_type(model)
    return model_for_resource_type(resource_type) if resource_type else None
