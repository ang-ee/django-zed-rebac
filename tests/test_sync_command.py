from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from rebac.models import (
    PackageManagedRecord,
    SchemaCaveat,
    SchemaDefinition,
    SchemaPermission,
    SchemaRelation,
)


@pytest.mark.django_db
def test_sync_check_detects_relation_payload_drift() -> None:
    call_command("rebac", "sync", stdout=io.StringIO())
    post_def = SchemaDefinition.objects.get(resource_type="blog/post")
    SchemaRelation.objects.filter(definition=post_def, name="viewer").update(
        allowed_subjects=[{"type": "auth/user"}]
    )

    with pytest.raises(CommandError, match="Schema drift detected"):
        call_command("rebac", "sync", "--check", stdout=io.StringIO())


@pytest.mark.django_db
def test_sync_check_detects_stale_permission_rows() -> None:
    call_command("rebac", "sync", stdout=io.StringIO())
    post_def = SchemaDefinition.objects.get(resource_type="blog/post")
    SchemaPermission.objects.create(
        definition=post_def,
        name="stale_admin",
        expression="owner",
    )

    with pytest.raises(CommandError, match="Schema drift detected"):
        call_command("rebac", "sync", "--check", stdout=io.StringIO())


@pytest.mark.django_db
def test_sync_prunes_stale_relation_and_permission_rows() -> None:
    call_command("rebac", "sync", stdout=io.StringIO())
    post_def = SchemaDefinition.objects.get(resource_type="blog/post")
    SchemaRelation.objects.create(
        definition=post_def,
        name="stale_relation",
        allowed_subjects=[{"type": "auth/user"}],
    )
    SchemaPermission.objects.create(
        definition=post_def,
        name="stale_permission",
        expression="owner",
    )

    call_command("rebac", "sync", stdout=io.StringIO())

    assert not SchemaRelation.objects.filter(
        definition=post_def,
        name="stale_relation",
    ).exists()
    assert not SchemaPermission.objects.filter(
        definition=post_def,
        name="stale_permission",
    ).exists()


@pytest.mark.django_db
def test_sync_prunes_stale_package_managed_definition_and_caveat_rows() -> None:
    call_command("rebac", "sync", stdout=io.StringIO())
    stale_definition = SchemaDefinition.objects.create(resource_type="legacy/type")
    stale_relation = SchemaRelation.objects.create(
        definition=stale_definition,
        name="owner",
        allowed_subjects=[{"type": "auth/user"}],
    )
    stale_caveat = SchemaCaveat.objects.create(
        name="legacy_caveat",
        params=[],
        expression="true",
    )
    _managed_record("definition:legacy/type", stale_definition)
    _managed_record("relation:legacy/type#owner", stale_relation)
    _managed_record("caveat:legacy_caveat", stale_caveat)

    with pytest.raises(CommandError, match="Schema drift detected"):
        call_command("rebac", "sync", "--check", stdout=io.StringIO())

    call_command("rebac", "sync", stdout=io.StringIO())

    assert not SchemaDefinition.objects.filter(resource_type="legacy/type").exists()
    assert not SchemaRelation.objects.filter(pk=stale_relation.pk).exists()
    assert not SchemaCaveat.objects.filter(name="legacy_caveat").exists()
    assert not PackageManagedRecord.objects.filter(
        external_id__in=[
            "definition:legacy/type",
            "relation:legacy/type#owner",
            "caveat:legacy_caveat",
        ]
    ).exists()


@pytest.mark.django_db
def test_sync_rejects_duplicate_definitions_before_writing(monkeypatch, tmp_path) -> None:
    app_one = tmp_path / "app_one"
    app_two = tmp_path / "app_two"
    app_one.mkdir()
    app_two.mkdir()
    (app_one / "permissions.zed").write_text(
        "definition auth/user {}\ndefinition duplicate/type {}\n",
        encoding="utf-8",
    )
    (app_two / "permissions.zed").write_text(
        "definition duplicate/type {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "rebac.management.commands.rebac.apps.get_app_configs",
        lambda: [
            SimpleNamespace(name="app.one", path=str(app_one)),
            SimpleNamespace(name="app.two", path=str(app_two)),
        ],
    )

    with pytest.raises(CommandError, match="Duplicate definition"):
        call_command("rebac", "sync", stdout=io.StringIO())

    assert not SchemaDefinition.objects.filter(resource_type="duplicate/type").exists()


@pytest.mark.django_db
def test_sync_package_rejects_duplicate_definitions_before_writing(monkeypatch, tmp_path) -> None:
    app_one = tmp_path / "app_one"
    app_two = tmp_path / "app_two"
    app_one.mkdir()
    app_two.mkdir()
    (app_one / "permissions.zed").write_text(
        "definition duplicate/type {}\n",
        encoding="utf-8",
    )
    (app_two / "permissions.zed").write_text(
        "definition duplicate/type {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "rebac.management.commands.rebac.apps.get_app_configs",
        lambda: [
            SimpleNamespace(name="app.one", path=str(app_one)),
            SimpleNamespace(name="app.two", path=str(app_two)),
        ],
    )

    with pytest.raises(CommandError, match="Duplicate definition"):
        call_command("rebac", "sync", "--package", "app.two", stdout=io.StringIO())

    assert not SchemaDefinition.objects.filter(resource_type="duplicate/type").exists()


@pytest.mark.django_db
def test_sync_rejects_duplicate_caveats_before_writing(monkeypatch, tmp_path) -> None:
    app_one = tmp_path / "app_one"
    app_two = tmp_path / "app_two"
    app_one.mkdir()
    app_two.mkdir()
    caveat_text = "caveat duplicate_caveat() { true }\n"
    (app_one / "permissions.zed").write_text(caveat_text, encoding="utf-8")
    (app_two / "permissions.zed").write_text(caveat_text, encoding="utf-8")
    monkeypatch.setattr(
        "rebac.management.commands.rebac.apps.get_app_configs",
        lambda: [
            SimpleNamespace(name="app.one", path=str(app_one)),
            SimpleNamespace(name="app.two", path=str(app_two)),
        ],
    )

    with pytest.raises(CommandError, match="Duplicate caveat"):
        call_command("rebac", "sync", stdout=io.StringIO())

    assert not SchemaCaveat.objects.filter(name="duplicate_caveat").exists()


@pytest.mark.django_db
def test_sync_package_rejects_duplicate_caveats_before_writing(monkeypatch, tmp_path) -> None:
    app_one = tmp_path / "app_one"
    app_two = tmp_path / "app_two"
    app_one.mkdir()
    app_two.mkdir()
    caveat_text = "caveat duplicate_caveat() { true }\n"
    (app_one / "permissions.zed").write_text(caveat_text, encoding="utf-8")
    (app_two / "permissions.zed").write_text(caveat_text, encoding="utf-8")
    monkeypatch.setattr(
        "rebac.management.commands.rebac.apps.get_app_configs",
        lambda: [
            SimpleNamespace(name="app.one", path=str(app_one)),
            SimpleNamespace(name="app.two", path=str(app_two)),
        ],
    )

    with pytest.raises(CommandError, match="Duplicate caveat"):
        call_command("rebac", "sync", "--package", "app.two", stdout=io.StringIO())

    assert not SchemaCaveat.objects.filter(name="duplicate_caveat").exists()


def _managed_record(external_id: str, target: Any) -> None:
    PackageManagedRecord.objects.create(
        package="tests.testapp",
        external_id=external_id,
        schema_revision=1,
        target_ct=ContentType.objects.get_for_model(type(target)),
        target_pk=target.pk,
        content_hash="stale",
        no_update=True,
        last_synced_at=timezone.now(),
    )
