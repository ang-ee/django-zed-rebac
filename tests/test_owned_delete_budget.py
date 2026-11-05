"""Owned deletes share one schema pin and one invalidation per model lineage."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, backend, sudo
from rebac.backends import reset_backend
from rebac.models import SchemaDefinition, SchemaPermission
from rebac.watch import row_generations

from .testapp.models import Folder

ACTOR = SubjectRef.of("auth/user", "1")


@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
def test_stored_policy_delete_budget_is_per_collected_model():
    call_command("rebac", "sync", stdout=StringIO())
    SchemaDefinition.objects.get_or_create(resource_type="auth/user")
    definition = SchemaDefinition.objects.get(resource_type="blog/folder")
    SchemaPermission.objects.create(
        definition=definition, name="delete", expression="owner + parent->write"
    )
    reset_backend()
    local = backend()
    local.schema()  # A warm policy gives each operation one revision read.

    counts = []
    for size in (1, 2, 10):
        with sudo(reason="delete budget fixture"):
            root = Folder.objects.create(name="root")
            children = Folder.objects.bulk_create(
                [Folder(name=f"child {index}", parent=root) for index in range(size - 1)]
            )
        local.write_relationships(
            [RelationshipTuple(ObjectRef("blog/folder", str(root.pk)), "owner", ACTOR)]
            + [
                RelationshipTuple(
                    ObjectRef("blog/folder", str(child.pk)),
                    "parent",
                    SubjectRef.of("blog/folder", str(root.pk)),
                )
                for child in children
            ]
        )
        before = dict(row_generations((Folder._meta.label_lower,), using="default"))[
            Folder._meta.label_lower
        ]
        with CaptureQueriesContext(connection) as queries:
            root.with_actor(ACTOR).delete()
        after = dict(row_generations((Folder._meta.label_lower,), using="default"))[
            Folder._meta.label_lower
        ]
        revision_reads = sum(
            query["sql"].startswith('SELECT "rebac_schemageneration".') for query in queries
        )
        assert revision_reads == 1
        assert after == before + 1
        assert not Folder._base_manager.filter(pk__in=[root.pk, *(c.pk for c in children)]).exists()
        counts.append(len(queries))
    assert counts[1] == counts[2]


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_key_chunk_binds_postgresql_array_or_sqlite_in():
    from rebac.watch import by_key

    [part] = by_key(Folder._base_manager.all(), list(range(1, 5001)))
    sql, params = part.query.sql_with_params()
    if connection.vendor == "postgresql":
        assert '"testapp_folder"."id" = ANY(%s)' in sql
        assert sum(isinstance(param, list) for param in params) == 1
        assert len(next(param for param in params if isinstance(param, list))) == 5000
    else:
        assert '"testapp_folder"."id" IN (' in sql
        assert "ANY(" not in sql
