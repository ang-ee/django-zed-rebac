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

from typing import TYPE_CHECKING

from django.db import models, router, transaction

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
            # `content_type_id` is the FK's implicit `_id` column — django-stubs
            # generates it for mypy; pyright (no plugin) doesn't see it.
            if not created and content_type is not None and obj.content_type_id is None:  # pyright: ignore[reportAttributeAccessIssue]
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
        idempotent. Each batch contains at most 200 identities; a follow-up
        SELECT resolves and validates all primary keys, including existing rows. Returns an empty dict on empty
        input — the caller can branch on emptiness without a query.
        """
        from rebac.index.write import bulk_intern

        return bulk_intern(
            cls, ("resource_type", "resource_id"), pairs, using=router.db_for_write(cls)
        )
