"""Tier 1 baseline — schema rows loaded from each app's permissions.zed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from django.db.models.fields.related_descriptors import RelatedManager


class SchemaDefinition(models.Model):
    resource_type = models.CharField(max_length=64, unique=True)

    if TYPE_CHECKING:
        # Reverse accessors for the FKs below. django-stubs synthesises these
        # for mypy via its plugin; pyright (no plugin) needs them spelled out.
        relations: RelatedManager[SchemaRelation]
        permissions: RelatedManager[SchemaPermission]

    class Meta:
        app_label = "rebac"

    def __str__(self) -> str:
        return self.resource_type


class SchemaRelation(models.Model):
    definition = models.ForeignKey(
        SchemaDefinition, on_delete=models.CASCADE, related_name="relations"
    )
    name = models.CharField(max_length=64)
    # Array of `{"type": "...", "relation": "...", "wildcard": bool}`.
    allowed_subjects = models.JSONField(default=list)
    # Optional relation backing in the codec shape owned by
    # ``rebac.schema.ast.backing_to_dict`` — ``{"kind": "fk", "path": ...}``,
    # ``{"kind": "const", "target_id": ...}`` or ``{"kind": "attribute", ...}``
    # — for relations resolved live instead of from stored tuple rows.
    backing = models.JSONField(null=True, blank=True, default=None)
    caveat = models.CharField(max_length=64, blank=True, default="")
    with_expiration = models.BooleanField(default=False)

    class Meta:
        app_label = "rebac"
        unique_together = [("definition", "name")]

    def __str__(self) -> str:
        return f"{self.definition.resource_type}#{self.name}"


class SchemaPermission(models.Model):
    definition = models.ForeignKey(
        SchemaDefinition, on_delete=models.CASCADE, related_name="permissions"
    )
    name = models.CharField(max_length=64)
    expression = models.TextField()

    class Meta:
        app_label = "rebac"
        unique_together = [("definition", "name")]

    def __str__(self) -> str:
        return f"{self.definition.resource_type}#{self.name}"


class SchemaCaveat(models.Model):
    name = models.CharField(max_length=64, unique=True)
    # `[{"name": "ip", "type": "ipaddress"}, ...]`.
    params = models.JSONField(default=list)
    expression = models.TextField()

    class Meta:
        app_label = "rebac"

    def __str__(self) -> str:
        return self.name
