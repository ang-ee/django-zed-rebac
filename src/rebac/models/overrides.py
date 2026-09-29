"""SchemaOverride — Tier 2 admin-editable tweaks."""

from __future__ import annotations

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from .schema_write import SchemaRow


class SchemaOverride(SchemaRow):
    KIND_TIGHTEN = "tighten"
    KIND_LOOSEN = "loosen"
    KIND_DISABLE = "disable"
    KIND_EXTEND = "extend"
    KIND_RECAVEAT = "recaveat"

    KIND_CHOICES = [
        (KIND_TIGHTEN, "Tighten"),
        (KIND_LOOSEN, "Loosen"),
        (KIND_DISABLE, "Disable"),
        (KIND_EXTEND, "Extend"),
        (KIND_RECAVEAT, "Recaveat"),
    ]

    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    target_ct = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    target_pk = models.PositiveIntegerField()
    target = GenericForeignKey("target_ct", "target_pk")
    expression = models.TextField()
    reason = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta(SchemaRow.Meta):
        app_label = "rebac"
        indexes = [
            models.Index(fields=["target_ct", "target_pk"], name="rebac_ovr_target_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.target_ct}/{self.target_pk}"
