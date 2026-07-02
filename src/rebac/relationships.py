"""Public helpers `write_relationships` / `delete_relationships`."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db import models
from django.db.models import QuerySet

from ._id import resource_id_attr
from .resources import model_for_resource_type, model_resource_id
from .types import RelationshipFilter, RelationshipTuple, SubjectRef, Zookie


def _format_target(tup: RelationshipTuple) -> str:
    """Render a `RelationshipTuple` as the canonical wire string used in audit rows.

    Format: ``<rt>:<id>#<rel> @ <st>:<sid>[#<sr>][ with <caveat>]``. The
    optional ``with <caveat>`` suffix is appended when ``caveat_name`` is
    non-empty so caveated grants/revokes are distinguishable in the audit
    log from their uncaveated counterparts.
    """
    res = f"{tup.resource.resource_type}:{tup.resource.resource_id}#{tup.relation}"
    sub = f"{tup.subject.subject_type}:{tup.subject.subject_id}"
    if tup.subject.optional_relation:
        sub = f"{sub}#{tup.subject.optional_relation}"
    target = f"{res} @ {sub}"
    if tup.caveat_name:
        target = f"{target} with {tup.caveat_name}"
    return target


def write_relationships(writes: Iterable[RelationshipTuple]) -> Zookie:
    """Atomically commit relationship rows. Returns a consistency token."""
    from .actors import current_actor
    from .audit import emit as emit_audit
    from .backends import backend
    from .consistency import record_zookie
    from .models import PermissionAuditEvent

    # Materialise so we can both pass to the backend and audit.
    rows = list(writes)
    zookie = backend().write_relationships(rows)
    # Stash the post-write Zookie in the ambient ContextVar so subsequent
    # reads in this scope auto-upgrade to ``at_least_as_fresh`` — closes
    # the SpiceDB write-then-read staleness window. No-op outside a
    # zookie_scope (e.g. management commands, Celery without the actor
    # propagator hook).
    record_zookie(zookie)

    actor = current_actor()
    for tup in rows:
        emit_audit(
            PermissionAuditEvent.KIND_RELATIONSHIP_GRANT,
            actor=actor,
            origin=actor,
            target_repr=_format_target(tup),
            defer_to_commit=True,
        )
    return zookie


def delete_relationships(filter_: RelationshipFilter) -> Zookie:
    """Atomically delete matching relationship rows."""
    from .actors import current_actor
    from .audit import emit as emit_audit
    from .backends import backend
    from .consistency import record_zookie
    from .models import PermissionAuditEvent, active_relationship_model

    # Snapshot the matched rows BEFORE the delete so we can audit each row's
    # canonical wire string. Keep the matcher in lockstep with
    # LocalBackend.delete_relationships — if a future filter field is added
    # there, mirror it here. The audit projection always uses the
    # denormalized field names (``resource_type``, ``subject_id``, etc.) —
    # the registry manager translates filters internally and the property
    # accessors expose the same names on instances, but for ``.values()``
    # in registry mode we have to project through the FK rows explicitly.
    RelationshipModel = active_relationship_model()
    # ``active_relationship_model`` returns a union of the two storage
    # models; the FK-side lookups below only apply in registry mode
    # (guarded by ``is_registry``). Type as ``QuerySet[Any]`` so the
    # runtime-dispatched field names don't trip static field validation.
    qs: QuerySet[Any] = RelationshipModel.objects.all()
    if filter_.resource_type:
        qs = qs.filter(resource_type=filter_.resource_type)
    if filter_.resource_id:
        qs = qs.filter(resource_id=filter_.resource_id)
    if filter_.relation:
        qs = qs.filter(relation=filter_.relation)
    if filter_.subject_type:
        qs = qs.filter(subject_type=filter_.subject_type)
    if filter_.subject_id:
        qs = qs.filter(subject_id=filter_.subject_id)
    if filter_.optional_subject_relation:
        qs = qs.filter(optional_subject_relation=filter_.optional_subject_relation)
    if filter_.caveat_name:
        qs = qs.filter(caveat_name=filter_.caveat_name)
    is_registry = RelationshipModel.__name__ == "RelationshipRegistry"
    if is_registry:
        snapshot = [
            {
                "resource_type": row["resource_fk__resource_type"],
                "resource_id": row["resource_fk__resource_id"],
                "relation": row["relation"],
                "subject_type": row["subject_fk__resource_type"],
                "subject_id": row["subject_fk__resource_id"],
                "optional_subject_relation": row["optional_subject_relation"],
                "caveat_name": row["caveat_name"],
            }
            for row in qs.values(
                "resource_fk__resource_type",
                "resource_fk__resource_id",
                "relation",
                "subject_fk__resource_type",
                "subject_fk__resource_id",
                "optional_subject_relation",
                "caveat_name",
            )
        ]
    else:
        snapshot = list(
            qs.values(
                "resource_type",
                "resource_id",
                "relation",
                "subject_type",
                "subject_id",
                "optional_subject_relation",
                "caveat_name",
            )
        )

    zookie = backend().delete_relationships(filter_)
    # Delete is a write — the new state matters for freshness, same as
    # write_relationships above. Record so subsequent reads in scope
    # honour the post-delete state.
    record_zookie(zookie)

    actor = current_actor()
    for row in snapshot:
        sub = f"{row['subject_type']}:{row['subject_id']}"
        if row["optional_subject_relation"]:
            sub = f"{sub}#{row['optional_subject_relation']}"
        target = f"{row['resource_type']}:{row['resource_id']}#{row['relation']} @ {sub}"
        if row["caveat_name"]:
            target = f"{target} with {row['caveat_name']}"
        emit_audit(
            PermissionAuditEvent.KIND_RELATIONSHIP_REVOKE,
            actor=actor,
            origin=actor,
            target_repr=target,
            defer_to_commit=True,
        )
    return zookie


def delete_relationship(tuple_: RelationshipTuple) -> Zookie:
    """Atomically delete exactly one relationship tuple shape.

    Unlike ``delete_relationships(RelationshipFilter(...))``, empty optional
    subject relations and caveat names are exact values here, not wildcards.

    No direct SpiceDB equivalent — SpiceDB expresses the same intent via
    ``WriteRelationships`` with an ``OPERATION_DELETE`` update. This helper
    is therefore local-only today; the plan for 0.4 is to lower it through
    that path once the backend ABC accepts operation-shaped updates. See
    ``docs/ARCHITECTURE.md``.
    """
    from .actors import current_actor
    from .audit import emit as emit_audit
    from .backends import backend
    from .consistency import record_zookie
    from .models import PermissionAuditEvent, active_relationship_model

    RelationshipModel = active_relationship_model()
    snapshot = list(
        RelationshipModel.objects.filter(
            resource_type=tuple_.resource.resource_type,
            resource_id=tuple_.resource.resource_id,
            relation=tuple_.relation,
            subject_type=tuple_.subject.subject_type,
            subject_id=tuple_.subject.subject_id,
            optional_subject_relation=tuple_.subject.optional_relation,
            caveat_name=tuple_.caveat_name,
        )
    )
    zookie = backend().delete_relationship(tuple_)
    record_zookie(zookie)

    actor = current_actor()
    for _row in snapshot:
        emit_audit(
            PermissionAuditEvent.KIND_RELATIONSHIP_REVOKE,
            actor=actor,
            origin=actor,
            target_repr=_format_target(tuple_),
            defer_to_commit=True,
        )
    return zookie


def resolve_subjects(refs: Iterable[SubjectRef | str]) -> dict[SubjectRef, models.Model]:
    """Resolve subject refs whose object type maps to a registered Django model.

    This is the inverse of the public ``SubjectRef`` creation path for resource
    types the library can map back to a model. Unknown resource types and
    missing rows are omitted. ``optional_relation`` is ignored for lookup
    purposes: ``auth/group:eng#member`` and ``auth/group:eng`` both point at
    the same underlying object id.
    """
    refs_by_type: dict[str, list[SubjectRef]] = {}
    for ref in refs:
        if not isinstance(ref, SubjectRef):
            ref = SubjectRef.parse(str(ref))
        refs_by_type.setdefault(ref.subject_type, []).append(ref)

    resolved: dict[SubjectRef, models.Model] = {}
    for subject_type, refs_for_type in refs_by_type.items():
        model = model_for_resource_type(subject_type)
        if model is None:
            continue
        ids = {ref.subject_id for ref in refs_for_type}
        rows = model._base_manager.filter(**{f"{resource_id_attr(model)}__in": list(ids)})
        by_id = {model_resource_id(row): row for row in rows}
        for ref in refs_for_type:
            row = by_id.get(ref.subject_id)
            if row is not None:
                resolved[ref] = row
    return resolved
