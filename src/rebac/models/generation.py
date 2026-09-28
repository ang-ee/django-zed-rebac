"""Internal database witness for the effective-schema cache."""

from django.db import models


class SchemaGeneration(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    revision = models.CharField(max_length=32, editable=False)

    class Meta:
        app_label = "rebac"
        default_permissions = ()
