"""Registry storage shape.

Adds ``RebacResource`` (one row per ``(resource_type, resource_id)`` pair)
and ``RelationshipRegistry`` (FK-shaped relationship table). Both tables
ship on disk; the active one is selected by ``REBAC_LOCAL_BACKEND_STORAGE``.

This migration is additive — the existing ``Relationship`` table is
unchanged. Operators flip ``REBAC_LOCAL_BACKEND_STORAGE = 'registry'``
after running ``python manage.py rebac migrate-storage --to registry`` to
copy existing rows. The default remains denormalized in 0.7.0; any default
flip or denormalized-table removal is deferred to a future release.
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("rebac", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="RebacResource",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("resource_type", models.CharField(max_length=64)),
                ("resource_id", models.CharField(max_length=64)),
                ("object_pk", models.CharField(blank=True, default="", max_length=64)),
                (
                    "content_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="rebacresource",
            constraint=models.UniqueConstraint(
                fields=("resource_type", "resource_id"),
                name="rebac_resource_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="rebacresource",
            index=models.Index(
                fields=["content_type", "object_pk"],
                name="rebac_resource_ct_idx",
            ),
        ),
        migrations.CreateModel(
            name="RelationshipRegistry",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("relation", models.CharField(max_length=64)),
                (
                    "optional_subject_relation",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("caveat_name", models.CharField(blank=True, default="", max_length=64)),
                ("caveat_context", models.JSONField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("written_at_xid", models.BigIntegerField(db_index=True, default=0)),
                (
                    "resource_fk",
                    models.ForeignKey(
                        db_column="resource_fk_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="rebac.rebacresource",
                    ),
                ),
                (
                    "subject_fk",
                    models.ForeignKey(
                        db_column="subject_fk_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="rebac.rebacresource",
                    ),
                ),
            ],
            options={
                "verbose_name": "Relationship (registry)",
                "verbose_name_plural": "Relationships (registry)",
            },
        ),
        migrations.AddIndex(
            model_name="relationshipregistry",
            index=models.Index(
                fields=["resource_fk", "relation"],
                name="rebac_reg_rel_fwd_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="relationshipregistry",
            index=models.Index(
                fields=["subject_fk", "relation"],
                name="rebac_reg_rel_rev_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="relationshipregistry",
            index=models.Index(
                fields=["subject_fk", "optional_subject_relation"],
                name="rebac_reg_rel_subset_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="relationshipregistry",
            constraint=models.UniqueConstraint(
                fields=(
                    "resource_fk",
                    "relation",
                    "subject_fk",
                    "optional_subject_relation",
                    "caveat_name",
                ),
                name="rebac_relationship_reg_uniq",
            ),
        ),
    ]
