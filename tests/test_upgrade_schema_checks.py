"""Pre-migration checks must not prevent the persisted backing upgrade."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from rebac import SchemaError, backend
from rebac.backends import reset_backend
from rebac.checks import check_field_backed_relations, check_universal_admin_in_roles
from rebac.models import SchemaDefinition, SchemaRelation

pytestmark = pytest.mark.django_db(transaction=True)


def _old_backing() -> SchemaRelation:
    definition = SchemaDefinition.objects.create(resource_type="blog/post")
    return SchemaRelation.objects.create(
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
        row = _old_backing()
        reset_backend()
        assert check_field_backed_relations() == []
        assert check_universal_admin_in_roles() == []
        # Missing revision metadata degrades to uncached loading; malformed
        # backing still fails closed before and after the upgrade.
        with pytest.raises(SchemaError, match="backing"):
            backend().schema()

        call_command("migrate", "rebac", "0005", skip_checks=False, verbosity=0, stdout=output)

        row.refresh_from_db()
        assert row.backing == {"kind": "fk", "path": "folder"}
        reset_backend()
        assert backend().schema().get_definition("blog/post") is not None
        row.backing = {"kind": "fk", "attname": "folder"}
        row.save(update_fields=["backing"])
        with pytest.raises(SchemaError, match="backing"):
            backend().schema()
    finally:
        call_command("migrate", "rebac", "0005", skip_checks=True, verbosity=0, stdout=output)
        reset_backend()


@pytest.mark.parametrize("check", [check_field_backed_relations, check_universal_admin_in_roles])
def test_invalid_backing_is_not_suppressed_after_migrations(check, settings):
    settings.REBAC_UNIVERSAL_ADMIN_ROLE = "platform/role:admin"
    _old_backing()
    reset_backend()
    try:
        with pytest.raises(SchemaError, match="backing"):
            check()
    finally:
        reset_backend()
