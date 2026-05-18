"""Public helpers `write_relationships` / `delete_relationships`."""

from __future__ import annotations

from collections.abc import Iterable

from .types import RelationshipFilter, RelationshipTuple, Zookie


def _format_target(tup: RelationshipTuple) -> str:
    """Render a `RelationshipTuple` as the canonical wire string used in audit rows.

    Format:  `<rt>:<id>#<rel> @ <st>:<sid>[#<sr>]`
    """
    res = f"{tup.resource.resource_type}:{tup.resource.resource_id}#{tup.relation}"
    sub = f"{tup.subject.subject_type}:{tup.subject.subject_id}"
    if tup.subject.optional_relation:
        sub = f"{sub}#{tup.subject.optional_relation}"
    return f"{res} @ {sub}"


def write_relationships(writes: Iterable[RelationshipTuple]) -> Zookie:
    """Atomically commit relationship rows. Returns a consistency token."""
    from . import backend
    from .actors import current_actor
    from .audit import emit as emit_audit
    from .models import PermissionAuditEvent

    # Materialise so we can both pass to the backend and audit.
    rows = list(writes)
    zookie = backend().write_relationships(rows)

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
    from . import backend
    from .actors import current_actor
    from .audit import emit as emit_audit
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
    qs = RelationshipModel.objects.all()
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
            }
            for row in qs.values(
                "resource_fk__resource_type",
                "resource_fk__resource_id",
                "relation",
                "subject_fk__resource_type",
                "subject_fk__resource_id",
                "optional_subject_relation",
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
            )
        )

    zookie = backend().delete_relationships(filter_)

    actor = current_actor()
    for row in snapshot:
        sub = f"{row['subject_type']}:{row['subject_id']}"
        if row["optional_subject_relation"]:
            sub = f"{sub}#{row['optional_subject_relation']}"
        target = f"{row['resource_type']}:{row['resource_id']}#{row['relation']} @ {sub}"
        emit_audit(
            PermissionAuditEvent.KIND_RELATIONSHIP_REVOKE,
            actor=actor,
            origin=actor,
            target_repr=target,
            defer_to_commit=True,
        )
    return zookie
