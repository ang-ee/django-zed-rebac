"""RebacResource — registry-mode resource table.

A row per ``(resource_type, resource_id)`` pair seen by the engine;
``RelationshipRegistry`` integer FKs point at it. The two correctness
wins are:

  - **Cascade delete**: ``post_delete`` on a ``RebacMixin``-bearing Django
    row removes its ``RebacResource``; CASCADE on ``RelationshipRegistry``
    FKs then sweeps every tuple it appeared in.
  - **Referential integrity**: writes to ``RelationshipRegistry`` can only
    reference a registered ``(type, id)`` pair, surfacing typos as
    constraint violations instead of orphan tuples.

The model is wire-compatible with the public ``RelationshipTuple`` and the
denormalized ``Relationship`` row via the registry's translating manager —
callers writing string kwargs see no shape change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, cast

from django.db import models, transaction
from django.db.models import Q

if TYPE_CHECKING:
    from django.contrib.contenttypes.models import ContentType


class RebacResource(models.Model):
    """A typed ``(resource_type, resource_id)`` pair the engine has seen.

    ``content_type`` / ``object_pk`` are optional reverse pointers to the
    Django row that originated this resource. They are NULL for synthetic
    resources without a Django backing — role objects
    (``storage/role:object_viewer``), wildcards (``auth/user:*``),
    subject-set sources (``auth/group:eng#member``), and the canonical
    anonymous singleton (``auth/anonymous:*``). The fields are populated
    lazily: first writer leaves them NULL; a later writer that does know
    the backing row can fill them in. We don't gate writes on having a
    backing — the engine accepts tuples for resources that have no Django
    row (the public-API contract).
    """

    resource_type = models.CharField(max_length=64)
    resource_id = models.CharField(max_length=64)
    content_type = models.ForeignKey(
        "contenttypes.ContentType",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="+",
    )
    object_pk = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        app_label = "rebac"
        constraints = [
            models.UniqueConstraint(
                fields=["resource_type", "resource_id"],
                name="rebac_resource_uniq",
            ),
        ]
        indexes = [
            # Cascade lookup: "what RebacResource rows back this Django row?"
            models.Index(
                fields=["content_type", "object_pk"],
                name="rebac_resource_ct_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.resource_type}:{self.resource_id}"

    @classmethod
    def upsert_ref(
        cls,
        resource_type: str,
        resource_id: str,
        *,
        content_type: ContentType | None = None,
        object_pk: str = "",
    ) -> RebacResource:
        """Get-or-create the registry row for ``(resource_type, resource_id)``.

        On conflict returns the existing row unchanged. If the caller supplies
        ``content_type``/``object_pk`` and the existing row carries them NULL,
        the backing pointers are filled in lazily (first writer wins for the
        registry row itself; first writer with a Django row wins for the
        backing pointer). Backs the registry manager's create / get_or_create
        translation path — one DB round-trip in the common case.

        Concurrent inserts are safe: ``get_or_create`` issues an INSERT
        wrapped in the appropriate dialect's ON CONFLICT path; a racing
        insert collapses to a single row courtesy of the unique constraint.
        """
        with transaction.atomic():
            obj, created = cls.objects.get_or_create(
                resource_type=resource_type,
                resource_id=resource_id,
                defaults={
                    "content_type": content_type,
                    "object_pk": object_pk or "",
                },
            )
            if not created and content_type is not None and obj.content_type_id is None:
                # Fill backing pointer lazily; never overwrite an existing one.
                obj.content_type = content_type
                obj.object_pk = object_pk or ""
                obj.save(update_fields=["content_type", "object_pk"])
            return obj

    @classmethod
    def upsert_refs_bulk(
        cls,
        pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], int]:
        """Batched variant of :meth:`upsert_ref` returning a ``(type, id) → pk`` map.

        ``bulk_create`` with ``ignore_conflicts=True`` so re-runs are
        idempotent and a single round-trip suffices for the insert; a
        follow-up SELECT resolves all primary keys (including any that
        existed before the bulk insert). Returns an empty dict on empty
        input — the caller can branch on emptiness without a query.
        """
        if not pairs:
            return {}
        unique_pairs = list({pair for pair in pairs})
        # ``cls(...)`` is typed as the concrete model rather than ``Self``
        # by django-stubs, so cast for the ``bulk_create(Iterable[Self])``
        # signature. ``RebacResource`` is a concrete (non-abstract) model
        # that is never subclassed, so the cast is sound.
        objs = cast(
            "list[Self]", [cls(resource_type=rt, resource_id=rid) for rt, rid in unique_pairs]
        )
        cls.objects.bulk_create(objs, ignore_conflicts=True)
        # Build the OR-of-AND lookup directly — a naive
        # ``resource_type__in / resource_id__in`` would scan the Cartesian
        # product of the two sets and then post-filter in Python, which
        # blows up to N² for N input pairs. The Q chain is exact and the
        # SQL planner usually executes it as an index range scan per pair.
        lookup = Q()
        for rt, rid in unique_pairs:
            lookup |= Q(resource_type=rt, resource_id=rid)
        existing = cls.objects.filter(lookup).values_list("resource_type", "resource_id", "id")
        return {(rt, rid): pk for rt, rid, pk in existing}
