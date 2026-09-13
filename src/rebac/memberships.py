"""Generic direct membership operations over canonical ``member`` tuples."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction

from .actors import ActorLike, to_subject_ref
from .resources import to_object_ref
from .types import ObjectRef, RelationshipTuple, SubjectRef

if TYPE_CHECKING:
    from .models import Relationship, RelationshipRegistry

    RelationshipRow = Relationship | RelationshipRegistry
else:  # pragma: no cover
    RelationshipRow = Any

MEMBER_RELATION = "member"


def _container_ref(container: Any) -> ObjectRef:
    if isinstance(container, ObjectRef):
        return container
    if isinstance(container, str):
        return ObjectRef.parse(container)
    return to_object_ref(container)


def grant(
    *,
    subject: ActorLike,
    container: Any,
    caveat_name: str = "",
    caveat_context: Mapping[str, Any] | None = None,
) -> RelationshipRow:
    """Idempotently grant ``subject`` direct membership in ``container``."""
    from .models import active_relationship_model
    from .relationships import write_relationships

    relationship_model = active_relationship_model()
    subject_ref = to_subject_ref(subject)
    container_ref = _container_ref(container)
    tuple_ = RelationshipTuple(
        resource=container_ref,
        relation=MEMBER_RELATION,
        subject=subject_ref,
        caveat_name=caveat_name,
        caveat_context=dict(caveat_context or {}),
    )
    with transaction.atomic():
        write_relationships([tuple_])
        row = relationship_model.objects.get(
            resource_type=container_ref.resource_type,
            resource_id=container_ref.resource_id,
            relation=MEMBER_RELATION,
            subject_type=subject_ref.subject_type,
            subject_id=subject_ref.subject_id,
            optional_subject_relation=subject_ref.optional_relation,
            caveat_name=caveat_name,
        )
    return cast("RelationshipRow", row)


def revoke(
    *,
    subject: ActorLike,
    container: Any,
    caveat_name: str = "",
) -> int:
    """Revoke exactly one direct membership tuple, including its caveat name."""
    from .models import active_relationship_model
    from .relationships import delete_relationship

    relationship_model = active_relationship_model()
    subject_ref = to_subject_ref(subject)
    container_ref = _container_ref(container)
    tuple_ = RelationshipTuple(
        resource=container_ref,
        relation=MEMBER_RELATION,
        subject=subject_ref,
        caveat_name=caveat_name,
    )
    with transaction.atomic():
        exists = relationship_model.objects.filter(
            resource_type=container_ref.resource_type,
            resource_id=container_ref.resource_id,
            relation=MEMBER_RELATION,
            subject_type=subject_ref.subject_type,
            subject_id=subject_ref.subject_id,
            optional_subject_relation=subject_ref.optional_relation,
            caveat_name=caveat_name,
        ).exists()
        delete_relationship(tuple_)
    return int(exists)


def members_of(container: Any) -> Iterator[SubjectRef]:
    """Yield subjects with direct membership in ``container``."""
    from .models import active_relationship_model

    container_ref = _container_ref(container)
    rows = active_relationship_model().objects.filter(
        resource_type=container_ref.resource_type,
        resource_id=container_ref.resource_id,
        relation=MEMBER_RELATION,
    )
    for row in rows:
        yield SubjectRef.of(row.subject_type, row.subject_id, row.optional_subject_relation)


def containers_of(subject: ActorLike) -> Iterator[ObjectRef]:
    """Yield containers in which ``subject`` has direct membership."""
    from .models import active_relationship_model

    subject_ref = to_subject_ref(subject)
    rows = active_relationship_model().objects.filter(
        relation=MEMBER_RELATION,
        subject_type=subject_ref.subject_type,
        subject_id=subject_ref.subject_id,
        optional_subject_relation=subject_ref.optional_relation,
    )
    for row in rows:
        yield ObjectRef(row.resource_type, row.resource_id)


__all__ = ["MEMBER_RELATION", "containers_of", "grant", "members_of", "revoke"]
