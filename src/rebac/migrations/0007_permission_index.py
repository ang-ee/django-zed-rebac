"""Internal, rebuildable permission index. Hand-authored to match model state."""

import django.db.models.deletion
from django.db import migrations, models

import rebac.index.time


def create_global_state(apps, schema_editor):
    apps.get_model("rebac", "IndexState").objects.using(
        schema_editor.connection.alias
    ).get_or_create(key="global")


def remove_global_state(apps, schema_editor):
    apps.get_model("rebac", "IndexState").objects.using(schema_editor.connection.alias).filter(
        key="global"
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("rebac", "0006_schema_write_owners")]

    operations = [
        migrations.AddField(
            model_name="schemageneration",
            name="index_revision",
            field=models.CharField(editable=False, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="schemageneration",
            name="index_program",
            field=models.CharField(default="", editable=False, max_length=32),
        ),
        migrations.CreateModel(
            name="IndexState",
            fields=[("key", models.CharField(max_length=64, primary_key=True, serialize=False))],
            options={"db_table": "rebac_index_state", "default_permissions": ()},
        ),
        migrations.CreateModel(
            name="IndexTerm",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("type", models.CharField(max_length=64)),
                ("object_id", models.CharField(max_length=64)),
                ("relation", models.CharField(default="", max_length=64)),
            ],
            options={
                "db_table": "rebac_term",
                "default_permissions": (),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("type", "object_id", "relation"), name="rebac_term_uniq"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="IndexCover",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("expires_at", models.DateTimeField(default=rebac.index.time.time_max)),
                ("condition", models.JSONField(null=True)),
                ("condition_key", models.CharField(default="", max_length=64)),
                ("resource_type", models.CharField(max_length=64)),
                ("node", models.CharField(max_length=64)),
                ("site", models.CharField(default="", max_length=64)),
                ("pass_id", models.BigIntegerField(null=True)),
                ("round", models.PositiveIntegerField(null=True)),
                (
                    "holder",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="held_covers",
                        to="rebac.indexterm",
                    ),
                ),
                (
                    "scope",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="covers",
                        to="rebac.indexterm",
                    ),
                ),
            ],
            options={
                "db_table": "rebac_grant",
                "default_permissions": (),
                "indexes": [
                    models.Index(
                        fields=["resource_type", "node", "site", "holder"],
                        name="rebac_grant_node_idx",
                    ),
                    models.Index(fields=["scope", "node"], name="rebac_cover_scope_idx"),
                    models.Index(fields=["holder", "site", "node"], name="rebac_grant_holder_idx"),
                    models.Index(fields=["pass_id", "round"], name="rebac_grant_pass_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=(
                            "scope",
                            "node",
                            "holder",
                            "site",
                            "condition_key",
                        ),
                        name="rebac_cover_uniq",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="IndexEdge",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("expires_at", models.DateTimeField(default=rebac.index.time.time_max)),
                ("condition", models.JSONField(null=True)),
                ("condition_key", models.CharField(default="", max_length=64)),
                ("resource_type", models.CharField(max_length=64)),
                ("relation", models.CharField(max_length=64)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("tuple", "tuple"),
                            ("field", "field"),
                            ("attribute", "attribute"),
                            ("const", "const"),
                        ],
                        max_length=9,
                    ),
                ),
                (
                    "resource",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="edges_out",
                        to="rebac.indexterm",
                    ),
                ),
                (
                    "subject",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="edges_as_subject",
                        to="rebac.indexterm",
                    ),
                ),
                (
                    "target",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="edges_in",
                        to="rebac.indexterm",
                    ),
                ),
            ],
            options={
                "db_table": "rebac_edge",
                "default_permissions": (),
                "indexes": [
                    models.Index(fields=["resource_type", "relation"], name="rebac_edge_node_idx"),
                    models.Index(fields=["target", "relation"], name="rebac_edge_target_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=(
                            "resource",
                            "relation",
                            "subject",
                            "source",
                            "condition_key",
                        ),
                        name="rebac_edge_uniq",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="IndexMember",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("expires_at", models.DateTimeField(default=rebac.index.time.time_max)),
                ("condition", models.JSONField(null=True)),
                ("condition_key", models.CharField(default="", max_length=64)),
                ("member_type", models.CharField(max_length=64)),
                ("pass_id", models.BigIntegerField(null=True)),
                ("round", models.PositiveIntegerField(null=True)),
                (
                    "member",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="memberships",
                        to="rebac.indexterm",
                    ),
                ),
                (
                    "set",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="members",
                        to="rebac.indexterm",
                    ),
                ),
            ],
            options={
                "db_table": "rebac_membership",
                "default_permissions": (),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("member", "set", "condition_key"),
                        name="rebac_member_uniq",
                    )
                ],
                "indexes": [
                    models.Index(fields=["set", "member"], name="rebac_member_set_idx"),
                    models.Index(fields=["pass_id", "round"], name="rebac_member_pass_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="IndexWork",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("pass_id", models.BigIntegerField()),
                ("kind", models.CharField(max_length=64)),
                ("node", models.CharField(default="", max_length=64)),
                (
                    "phase",
                    models.CharField(
                        choices=[("old", "old"), ("new", "new"), ("region", "region")], max_length=6
                    ),
                ),
                (
                    "term",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to="rebac.indexterm",
                    ),
                ),
            ],
            options={
                "db_table": "rebac_index_work",
                "default_permissions": (),
                "indexes": [
                    models.Index(
                        fields=["pass_id", "phase", "kind"], name="rebac_work_pass_phase_idx"
                    ),
                    models.Index(
                        fields=["pass_id", "phase", "term"], name="rebac_work_pass_term_idx"
                    ),
                ],
            },
        ),
        migrations.RunPython(create_global_state, remove_global_state),
    ]
