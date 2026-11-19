"""Decide a bounded question from the rows it names: a positive walk (proposal 0020).

``held`` asks which of some ids an actor holds an action on. The compiled answer
is one statement over the whole reachable policy, however few the ids. Most
grants need far less: a row of the relationship table that names the actor, a
column that names it, or a parent it holds the action on. With
``REBAC_BOUNDED_WALK`` on, those are read first, with small indexed lookups, and
only the ids they do not settle go to the compiled statement.

The walk is **sound and incomplete by design**. It reads the arms of a
permission's top-level union, where one arm that holds grants the permission
whatever the others say, and of those only the kinds it can read exactly as the
compiler does at its lower bound:

- a stored relation that admits the actor's type as a plain, uncaveated subject
  (and that type's wildcard, where the relation declares it): one lookup of
  live, uncaveated rows naming the actor on the candidate ids;
- a relation backed by one foreign key to the actor's own model: the column
  names the actor;
- an arrow through a relation backed by one foreign key: the parents the walk
  proves, by the same rules.

It never answers no. An intersection, an exclusion, a subject set, a constant
or attribute backing, a caveat context, an override, a filtered or generic
backing, a non-integer identity and a second visit to a permission all leave
the id to the compiled statement, so the result of ``held`` is the same set.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import TYPE_CHECKING, Any, cast

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from ._id import _NATIVE_INTEGER_FIELDS, model_identity_fields, resource_id_attr
from .actors import is_anonymous_actor
from .clock import application_now
from .codec import identity_codec
from .field_backing import ResolvedFieldBacking, resolve_field_backing
from .models import active_relationship_model
from .resources import model_for_resource_type
from .schema.ast import (
    Definition,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermExpr,
    PermRef,
    Relation,
)
from .types import SubjectRef

if TYPE_CHECKING:
    from .backends.local import LocalBackend

# Arrows followed from the question's own type before the walk gives up.
_DEPTH = 4


def enabled() -> bool:
    from .conf import app_settings

    return bool(app_settings.REBAC_BOUNDED_WALK)


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
    """The ids among ``ids`` the walk proves the actor holds ``action`` on."""
    if context is not None:
        return set()
    if actor.optional_relation or not actor.subject_id or is_anonymous_actor(actor):
        return set()
    from .compile.read import _policy

    policy = _policy(backend)
    # An override retags arms the walk would read as plain unions.
    if policy.tagged.arms or policy.tagged.sites:
        return set()
    return _Walk(policy.schema, actor, using).holds(resource_type, action, set(ids), frozenset())


class _Walk:
    def __init__(self, schema: Any, actor: SubjectRef, using: str) -> None:
        self.schema = schema
        self.actor = actor
        self.using = using

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
        if relation.backing is None:
            return self._stored(definition, relation, ids)
        if isinstance(relation.backing, FieldBinding):
            return self._column(definition, relation, ids)
        return set()

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
            active_relationship_model()
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
            .order_by()
            .values_list("resource_id", flat=True)
        )
        return {str(resource_id) for resource_id in rows} & ids

    def _backed(
        self, definition: Definition, relation: Relation
    ) -> tuple[ResolvedFieldBacking, models.Field[Any, Any]] | None:
        """A backing the walk can read: one unfiltered foreign key between integer identities."""
        resolved = resolve_field_backing(definition, relation)
        if (
            resolved is None
            or resolved.generic is not None
            or resolved.filters
            or "__" in resolved.path
            or not resolved.single_valued
            or not isinstance(resolved.field, (models.ForeignKey, models.OneToOneField))
            or not resolved.targets_identity_directly()
            or len(relation.allowed_subjects) != 1
        ):
            return None
        source = _integer_identity(resolved.source_model)
        if source is None or _integer_identity(resolved.target_model) is None:
            return None
        return resolved, source

    def _targets(
        self, resolved: ResolvedFieldBacking, source: models.Field[Any, Any], ids: set[str]
    ) -> Iterator[tuple[str, str]]:
        """``(source id, target id)`` for the candidate rows whose foreign key is set."""
        keys = []
        for value in ids:
            try:
                keys.append(source.to_python(value))
            except ValueError, TypeError, ValidationError:
                continue  # not an id of this model: the compiled statement answers for it
        if not keys:
            return
        rows = (
            resolved.source_model._base_manager.using(self.using)
            .filter(**{f"{source.attname}__in": keys})
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


def _union_arms(definition: Definition, expr: PermExpr, seen: frozenset[str]) -> Iterator[PermExpr]:
    """The arms of a union, a named permission of the same definition opened into its own."""
    if isinstance(expr, PermBinOp) and expr.op == "+":
        yield from _union_arms(definition, expr.left, seen)
        yield from _union_arms(definition, expr.right, seen)
        return
    if isinstance(expr, PermRef) and expr.name not in seen:
        named = next((item for item in definition.permissions if item.name == expr.name), None)
        if named is not None:
            yield from _union_arms(definition, named.expression, seen | {expr.name})
            return
    yield expr
