"""Relationship — Tier 3 core REBAC store.

Wire-shape mirrors `authzed.api.v1.Relationship` exactly. Renames are breaking.

Two LocalBackend storage shapes coexist:

  - ``Relationship`` (denormalized) — the historical shape with four wide
    CharField columns. Default in 0.4.
  - ``RelationshipRegistry`` (registry) — same wire-shape, but the four
    string columns become two integer FKs into ``RebacResource``. Opt-in
    via ``REBAC_LOCAL_BACKEND_STORAGE='registry'``.

Both tables ship on disk; ``rebac.models.active_relationship_model()``
returns the one selected by the setting. The wire shape (``RelationshipTuple``
+ string kwargs to the active manager) is invariant across modes.
"""

from __future__ import annotations

from typing import Any

from django.db import models

from ..conf import app_settings


class Relationship(models.Model):
    """Denormalized relationship row — historical default storage shape."""

    resource_type = models.CharField(max_length=64, db_index=True)
    resource_id = models.CharField(max_length=64, db_index=True)
    relation = models.CharField(max_length=64, db_index=True)
    subject_type = models.CharField(max_length=64, db_index=True)
    subject_id = models.CharField(max_length=64, db_index=True)
    optional_subject_relation = models.CharField(max_length=64, blank=True, default="")
    caveat_name = models.CharField(max_length=64, blank=True, default="")
    caveat_context = models.JSONField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    written_at_xid = models.BigIntegerField(default=0, db_index=True)

    class Meta:
        app_label = "rebac"
        verbose_name = "Relationship"
        verbose_name_plural = "Relationships"
        indexes = [
            # Forward: "what subjects have <relation> on <resource>?"
            models.Index(
                fields=["resource_type", "resource_id", "relation"],
                name="rebac_rel_fwd_idx",
            ),
            # Reverse: "what resources does <subject> have <relation> on?"
            models.Index(
                fields=["subject_type", "subject_id", "relation"],
                name="rebac_rel_rev_idx",
            ),
            # Subject-set traversal (group#member -> user)
            models.Index(
                fields=["subject_type", "subject_id", "optional_subject_relation"],
                name="rebac_rel_subset_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "resource_type",
                    "resource_id",
                    "relation",
                    "subject_type",
                    "subject_id",
                    "optional_subject_relation",
                    "caveat_name",
                ],
                name="rebac_relationship_uniq",
            ),
        ]

    def __str__(self) -> str:
        rel = f"#{self.optional_subject_relation}" if self.optional_subject_relation else ""
        return (
            f"{self.resource_type}:{self.resource_id}#{self.relation} "
            f"@ {self.subject_type}:{self.subject_id}{rel}"
        )


def _translate_read_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Rewrite denormalized-style string lookups into FK-side lookups.

    The registry model stores ``(type, id)`` pairs in ``RebacResource`` and
    references them via ``resource_fk`` / ``subject_fk``. Engine code is
    written against the denormalized shape (where the strings are inline
    columns), so we translate at the manager boundary::

        resource_type="x" / resource_id="y" → resource_fk__resource_type / __resource_id
        subject_type__in=[...]              → subject_fk__resource_type__in=[...]

    Only the four denormalized field names ``resource_type`` /
    ``resource_id`` / ``subject_type`` / ``subject_id`` are rewritten; all
    other lookup kwargs pass through unchanged so the manager is a
    drop-in for the denormalized one.
    """
    out: dict[str, Any] = {}
    for key, value in kwargs.items():
        head, _, tail = key.partition("__")
        suffix = ("__" + tail) if tail else ""
        if head == "resource_type":
            out[f"resource_fk__resource_type{suffix}"] = value
        elif head == "resource_id":
            out[f"resource_fk__resource_id{suffix}"] = value
        elif head == "subject_type":
            out[f"subject_fk__resource_type{suffix}"] = value
        elif head == "subject_id":
            out[f"subject_fk__resource_id{suffix}"] = value
        else:
            out[key] = value
    return out


class RelationshipRegistryQuerySet(models.QuerySet["RelationshipRegistry"]):
    """Translating QuerySet for :class:`RelationshipRegistry`.

    Rewriting the kwargs at the QuerySet level (rather than only on the
    manager) means chained calls like
    ``RelationshipRegistry.objects.filter(...).exclude(...)`` translate at
    every step — not just the first. The manager's ``get_queryset()``
    returns this class so the rewrite is in scope for the whole chain.
    """

    def filter(self, *args: Any, **kwargs: Any) -> RelationshipRegistryQuerySet:
        return super().filter(*args, **_translate_read_kwargs(kwargs))

    def exclude(self, *args: Any, **kwargs: Any) -> RelationshipRegistryQuerySet:
        return super().exclude(*args, **_translate_read_kwargs(kwargs))

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return super().get(*args, **_translate_read_kwargs(kwargs))


class RelationshipRegistryManager(models.Manager.from_queryset(RelationshipRegistryQuerySet)):  # type: ignore[misc]
    """Translating manager for :class:`RelationshipRegistry`.

    Accepts the same ``(resource_type, resource_id, subject_type, subject_id)``
    string kwargs as :class:`Relationship` and upserts the corresponding
    :class:`RebacResource` rows transparently. The translation lives on
    the manager — engine code can keep building filter querysets the way
    it already does.

    Translation policy:

    - ``create()`` upserts both ``resource_fk`` and ``subject_fk`` and
      writes one row. Two extra SELECTs + one INSERT in the common path,
      all in one transaction.
    - ``get_or_create()`` / ``update_or_create()`` perform the same upsert
      then defer to the parent.
    - ``filter()`` / ``get()`` / ``exclude()`` translate string kwargs
      into FK-side lookups via :func:`_translate_read_kwargs`. Lookups
      for resources not in the registry collapse to "no match" — reads
      never create ``RebacResource`` rows.
    """

    def _translate_write_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Upsert ``RebacResource`` rows for any (type, id) pair in kwargs."""
        from .resource import RebacResource

        rt = kwargs.pop("resource_type", None)
        rid = kwargs.pop("resource_id", None)
        if rt is not None and rid is not None:
            kwargs["resource_fk"] = RebacResource.upsert_ref(rt, rid)
        elif rt is not None or rid is not None:
            raise ValueError(
                "RelationshipRegistry write needs both resource_type and resource_id "
                f"(got resource_type={rt!r}, resource_id={rid!r})"
            )
        st = kwargs.pop("subject_type", None)
        sid = kwargs.pop("subject_id", None)
        if st is not None and sid is not None:
            kwargs["subject_fk"] = RebacResource.upsert_ref(st, sid)
        elif st is not None or sid is not None:
            raise ValueError(
                "RelationshipRegistry write needs both subject_type and subject_id "
                f"(got subject_type={st!r}, subject_id={sid!r})"
            )
        return kwargs

    def get_queryset(self) -> RelationshipRegistryQuerySet:
        # Eager-join the FK rows. Engine code reads ``row.resource_type``
        # / ``row.subject_id`` per-row, so without select_related the
        # per-row property accessors would issue N queries.
        qs: RelationshipRegistryQuerySet = super().get_queryset()
        return qs.select_related("resource_fk", "subject_fk")

    def create(self, **kwargs: Any) -> RelationshipRegistry:
        obj: RelationshipRegistry = super().create(**self._translate_write_kwargs(kwargs))
        return obj

    def get_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[RelationshipRegistry, bool]:
        kwargs = self._translate_write_kwargs(kwargs)
        result: tuple[RelationshipRegistry, bool] = super().get_or_create(
            defaults=defaults, **kwargs
        )
        return result

    def update_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        create_defaults: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[RelationshipRegistry, bool]:
        kwargs = self._translate_write_kwargs(kwargs)
        result: tuple[RelationshipRegistry, bool] = super().update_or_create(
            defaults=defaults, create_defaults=create_defaults, **kwargs
        )
        return result


class RelationshipRegistry(models.Model):
    """Registry-mode relationship row.

    Same wire-shape as :class:`Relationship` but ``resource_*`` and
    ``subject_*`` collapse into two integer FKs pointing at
    :class:`rebac.models.RebacResource`. Reads of the denormalized string
    columns are proxied through ``resource_fk`` / ``subject_fk`` via the
    property accessors below.

    Writes via the public manager (``RelationshipRegistry.objects.create(
    resource_type='...', resource_id='...', ...)``) upsert the
    ``RebacResource`` rows transparently — callers see no shape change.
    """

    resource_fk = models.ForeignKey(
        "rebac.RebacResource",
        on_delete=models.CASCADE,
        related_name="+",
        db_column="resource_fk_id",
    )
    relation = models.CharField(max_length=64)
    subject_fk = models.ForeignKey(
        "rebac.RebacResource",
        on_delete=models.CASCADE,
        related_name="+",
        db_column="subject_fk_id",
    )
    optional_subject_relation = models.CharField(max_length=64, blank=True, default="")
    caveat_name = models.CharField(max_length=64, blank=True, default="")
    caveat_context = models.JSONField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    written_at_xid = models.BigIntegerField(default=0, db_index=True)

    objects = RelationshipRegistryManager()

    class Meta:
        app_label = "rebac"
        verbose_name = "Relationship (registry)"
        verbose_name_plural = "Relationships (registry)"
        indexes = [
            # Forward / reverse / subject-set parallels of the denormalized
            # indexes; integer-FK leading columns shrink the leaf-page
            # footprint ~5-10x for the hot lookup paths.
            models.Index(
                fields=["resource_fk", "relation"],
                name="rebac_reg_rel_fwd_idx",
            ),
            models.Index(
                fields=["subject_fk", "relation"],
                name="rebac_reg_rel_rev_idx",
            ),
            models.Index(
                fields=["subject_fk", "optional_subject_relation"],
                name="rebac_reg_rel_subset_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "resource_fk",
                    "relation",
                    "subject_fk",
                    "optional_subject_relation",
                    "caveat_name",
                ],
                name="rebac_relationship_reg_uniq",
            ),
        ]

    # Wire-shape accessors. Mirror :class:`Relationship`'s public attribute
    # surface so that engine code reading ``row.resource_type`` /
    # ``row.subject_id`` works in both modes without branching. The
    # accessors hit the joined FK row, so the manager's default queryset
    # eagerly ``select_related``s both to avoid N+1.
    @property
    def resource_type(self) -> str:
        return self.resource_fk.resource_type

    @property
    def resource_id(self) -> str:
        return self.resource_fk.resource_id

    @property
    def subject_type(self) -> str:
        return self.subject_fk.resource_type

    @property
    def subject_id(self) -> str:
        return self.subject_fk.resource_id

    def __str__(self) -> str:
        rel = f"#{self.optional_subject_relation}" if self.optional_subject_relation else ""
        return (
            f"{self.resource_type}:{self.resource_id}#{self.relation} "
            f"@ {self.subject_type}:{self.subject_id}{rel}"
        )


def active_relationship_model() -> type[Relationship] | type[RelationshipRegistry]:
    """Return the relationship model selected by ``REBAC_LOCAL_BACKEND_STORAGE``.

    Engine code (``LocalBackend``, ``rebac.relationships``,
    ``rebac.roles``-via-Relationship.objects) routes every read/write
    through this helper so the storage mode flip is a settings change, not
    a code change. External consumers that import ``Relationship`` or
    ``RelationshipRegistry`` by name keep working unchanged in their
    respective modes.

    The return type is a union of the two concrete model classes (not
    ``type[Model]``) so call sites can reach ``.objects`` without losing
    the manager type — both classes expose the same Django-default
    ``Manager`` plus, in the registry case, the extra translation
    helpers defined on ``RelationshipRegistryManager``.
    """
    if app_settings.REBAC_LOCAL_BACKEND_STORAGE == "registry":
        return RelationshipRegistry
    return Relationship
