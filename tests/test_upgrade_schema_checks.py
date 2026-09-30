"""Pre-migration checks must not prevent the persisted backing upgrade."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from rebac import SchemaError, backend
from rebac.backends import reset_backend
from rebac.checks import check_field_backed_relations, check_universal_admin_in_roles
from rebac.models import SchemaDefinition, SchemaRelation

pytestmark = pytest.mark.django_db(transaction=True)


def _old_backing(*, historical=False) -> SchemaRelation:
    if historical:
        state = (
            MigrationExecutor(connection)
            .loader.project_state([("rebac", "0003_schema_relation_backing")])
            .apps
        )
        definition_model = state.get_model("rebac", "SchemaDefinition")
        relation_model = state.get_model("rebac", "SchemaRelation")
    else:
        definition_model, relation_model = SchemaDefinition, SchemaRelation
    definition = definition_model.objects.create(resource_type="blog/post")
    return relation_model.objects.create(
        definition=definition,
        name="folder",
        allowed_subjects=[{"type": "blog/folder"}],
        backing={"kind": "fk", "attname": "folder"},
    )


def test_migrate_can_upgrade_legacy_backing_with_system_checks_enabled(settings):
    settings.REBAC_UNIVERSAL_ADMIN_ROLE = "platform/role:admin"
    output = StringIO()
    call_command("migrate", "rebac", "0003", skip_checks=True, verbosity=0, stdout=output)
    try:
        row = _old_backing(historical=True)
        reset_backend()
        assert check_field_backed_relations() == []
        assert check_universal_admin_in_roles() == []
        # Runtime schema reads require the index witness columns after upgrade.
        from django.db import OperationalError, ProgrammingError

        with pytest.raises((OperationalError, ProgrammingError)):
            backend().schema()

        call_command("migrate", "rebac", "0007", skip_checks=False, verbosity=0, stdout=output)

        row = SchemaRelation.objects.get(pk=row.pk)
        assert row.backing == {"kind": "fk", "path": "folder"}
        reset_backend()
        assert backend().schema().get_definition("blog/post") is not None
        row.backing = {"kind": "fk", "attname": "folder"}
        with pytest.raises(SchemaError, match="backing"):
            row.save(update_fields=["backing"])
    finally:
        call_command("migrate", "rebac", "0007", skip_checks=True, verbosity=0, stdout=output)
        reset_backend()


@pytest.mark.parametrize("check", [check_field_backed_relations, check_universal_admin_in_roles])
def test_invalid_backing_is_not_suppressed_after_migrations(check, settings):
    settings.REBAC_UNIVERSAL_ADMIN_ROLE = "platform/role:admin"
    # Historical ORM rows deliberately bypass schema-write validation.
    state = MigrationExecutor(connection).loader.project_state().apps
    definition = state.get_model("rebac", "SchemaDefinition").objects.create(
        resource_type="blog/post"
    )
    state.get_model("rebac", "SchemaRelation").objects.create(
        definition=definition,
        name="folder",
        allowed_subjects=[{"type": "blog/folder"}],
        backing={"kind": "fk", "attname": "folder"},
    )
    reset_backend()
    try:
        with pytest.raises(SchemaError, match="backing"):
            check()
    finally:
        reset_backend()
