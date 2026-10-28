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
@pytest.mark.parametrize("force", [False, True])
def test_sync_only_prunes_stale_children_when_forced(force: bool) -> None:
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

    flags = ["--force-overwrite", "--yes"] if force else []
    call_command("rebac", "sync", *flags, stdout=io.StringIO())

    assert SchemaRelation.objects.filter(
        definition=post_def,
        name="stale_relation",
    ).exists() is (not force)
    assert SchemaPermission.objects.filter(
        definition=post_def,
        name="stale_permission",
    ).exists() is (not force)


@pytest.mark.django_db
@pytest.mark.parametrize("force", [False, True])
def test_sync_only_prunes_stale_managed_rows_when_forced(force: bool) -> None:
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

    flags = ["--force-overwrite", "--yes"] if force else []
    call_command("rebac", "sync", *flags, stdout=io.StringIO())

    assert SchemaDefinition.objects.filter(resource_type="legacy/type").exists() is (not force)
    assert SchemaRelation.objects.filter(pk=stale_relation.pk).exists() is (not force)
    assert SchemaCaveat.objects.filter(name="legacy_caveat").exists() is (not force)
    assert PackageManagedRecord.objects.filter(
        external_id__in=[
            "definition:legacy/type",
            "relation:legacy/type#owner",
            "caveat:legacy_caveat",
        ]
    ).exists() is (not force)


@pytest.mark.django_db
def test_sync_force_requires_confirmation_before_writing(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO())
    with pytest.raises(CommandError, match="--yes"):
        call_command("rebac", "sync", "--force-overwrite", stdout=io.StringIO())
    assert not SchemaDefinition.objects.exists()


@pytest.mark.django_db
def test_sync_force_check_does_not_require_confirmation() -> None:
    call_command("rebac", "sync", stdout=io.StringIO())
    call_command("rebac", "sync", "--force-overwrite", "--check", stdout=io.StringIO())


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


@pytest.mark.django_db
def test_sync_rejects_cross_package_permission_subject_before_writing(
    monkeypatch, tmp_path
) -> None:
    group_app = tmp_path / "group_app"
    docs_app = tmp_path / "docs_app"
    group_app.mkdir()
    docs_app.mkdir()
    (group_app / "permissions.zed").write_text(
        "definition auth/user {}\n"
        "definition auth/group {\n"
        " relation member: auth/user\n"
        " permission effective_member = member\n"
        "}\n",
        encoding="utf-8",
    )
    (docs_app / "permissions.zed").write_text(
        "definition docs/document {\n relation viewer: auth/group#effective_member\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "rebac.management.commands.rebac.apps.get_app_configs",
        lambda: [
            SimpleNamespace(name="app.groups", path=str(group_app)),
            SimpleNamespace(name="app.docs", path=str(docs_app)),
        ],
    )

    with pytest.raises(CommandError, match="Effective schema validation failed"):
        call_command("rebac", "sync", stdout=io.StringIO())

    assert not SchemaDefinition.objects.exists()


def _managed_record(external_id: str, target: Any, *, package: str = "tests.testapp") -> None:
    PackageManagedRecord.objects.create(
        package=package,
        external_id=external_id,
        schema_revision=1,
        target_ct=ContentType.objects.get_for_model(type(target)),
        target_pk=target.pk,
        content_hash="stale",
        no_update=True,
        last_synced_at=timezone.now(),
    )


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("removed_source", ["package", "file"])
def test_prune_stale_retires_owned_policy_and_tuples_without_overwriting_live_edits(
    storage, removed_source, settings, monkeypatch, tmp_path
) -> None:
    from rebac.models import active_relationship_model

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    current = tmp_path / "current"
    retired = tmp_path / "retired"
    current.mkdir()
    retired.mkdir()
    current_source = current / "permissions.zed"
    retired_source = retired / "permissions.zed"
    current_source.write_text(
        "definition auth/user {}\n"
        "definition current/item {\n"
        " relation owner: auth/user\n"
        " relation legacy: auth/user\n"
        " permission read = owner\n}\n",
        encoding="utf-8",
    )
    retired_source.write_text(
        "definition retired/item { relation member: auth/user }\n", encoding="utf-8"
    )
    configs = [
        SimpleNamespace(name="current", path=str(current)),
        SimpleNamespace(name="retired", path=str(retired)),
    ]
    monkeypatch.setattr("rebac.management.commands.rebac.apps.get_app_configs", lambda: configs)
    call_command("rebac", "sync", stdout=io.StringIO())
    definition = SchemaDefinition.objects.get(resource_type="current/item")
    edited = SchemaPermission.objects.get(definition=definition, name="read")
    edited.expression = "nil"
    edited.save()
    manual = SchemaPermission.objects.create(
        definition=definition, name="manual", expression="owner"
    )
    rows = active_relationship_model().objects
    tuples = [
        ("retired/item", "member", "auth/user", ""),
        ("current/item", "owner", "retired/item", "member"),
        ("current/item", "legacy", "auth/user", ""),
        ("current/item", "owner", "current/item", "legacy"),
        ("current/item", "owner", "auth/user", ""),
    ]
    for index, (resource_type, relation, subject_type, subject_relation) in enumerate(tuples):
        rows.create(
            resource_type=resource_type,
            resource_id=str(index),
            relation=relation,
            subject_type=subject_type,
            subject_id="1",
            optional_subject_relation=subject_relation,
        )
    current_source.write_text(
        "definition auth/user {}\n"
        "definition current/item { relation owner: auth/user permission read = owner }\n",
        encoding="utf-8",
    )
    if removed_source == "package":
        configs.pop()
    else:
        retired_source.unlink()

    call_command("rebac", "sync", "--prune-stale", "--yes", stdout=io.StringIO())

    edited.refresh_from_db()
    assert edited.expression == "nil"
    assert SchemaPermission.objects.filter(pk=manual.pk).exists()
    assert not SchemaDefinition.objects.filter(resource_type="retired/item").exists()
    assert not SchemaRelation.objects.filter(definition=definition, name="legacy").exists()
    assert not PackageManagedRecord.objects.filter(package="retired").exists()
    assert rows.count() == 1
    assert rows.get().resource_id == "4"


@pytest.mark.django_db
def test_prune_stale_check_and_confirmation_preserve_policy_and_revision(monkeypatch) -> None:
    from rebac.models.generation import SchemaGeneration

    call_command("rebac", "sync", stdout=io.StringIO())
    stale = SchemaDefinition.objects.create(resource_type="removed/type")
    _managed_record("definition:removed/type", stale, package="removed")
    revision = SchemaGeneration.objects.get(pk=1).revision
    monkeypatch.setattr("sys.stdin", io.StringIO())
    with pytest.raises(CommandError, match="--prune-stale requires --yes"):
        call_command("rebac", "sync", "--prune-stale", stdout=io.StringIO())
    with pytest.raises(CommandError, match="Schema drift detected"):
        call_command("rebac", "sync", "--prune-stale", "--check", stdout=io.StringIO())
    assert SchemaDefinition.objects.filter(pk=stale.pk).exists()
    assert PackageManagedRecord.objects.filter(package="removed").exists()
    assert SchemaGeneration.objects.get(pk=1).revision == revision


@pytest.mark.django_db
def test_prune_stale_package_scope_includes_removed_package_only() -> None:
    call_command("rebac", "sync", stdout=io.StringIO())
    for package in ("removed.first", "removed.second"):
        target = SchemaDefinition.objects.create(resource_type=package.replace(".", "/"))
        _managed_record(f"definition:{target.resource_type}", target, package=package)
    call_command(
        "rebac",
        "sync",
        "--prune-stale",
        "--package",
        "removed.first",
        "--yes",
        stdout=io.StringIO(),
    )
    assert not SchemaDefinition.objects.filter(resource_type="removed/first").exists()
    assert SchemaDefinition.objects.filter(resource_type="removed/second").exists()
    assert PackageManagedRecord.objects.filter(package="removed.second").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("scoped", [False, True])
def test_prune_stale_transfers_live_ownership_without_overwriting_policy_or_tuples(
    scoped, monkeypatch, tmp_path
) -> None:
    from rebac.models import active_relationship_model

    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    source = (
        "definition auth/user {}\n"
        "definition shared/item { relation owner: auth/user permission read = owner }\n"
    )
    (old / "permissions.zed").write_text(source, encoding="utf-8")
    configs = [SimpleNamespace(name="old", path=str(old))]
    monkeypatch.setattr("rebac.management.commands.rebac.apps.get_app_configs", lambda: configs)
    call_command("rebac", "sync", stdout=io.StringIO())
    definition = SchemaDefinition.objects.get(resource_type="shared/item")
    permission = SchemaPermission.objects.get(definition=definition, name="read")
    permission.expression = "nil"
    permission.save()
    relationship = active_relationship_model().objects.create(
        resource_type="shared/item",
        resource_id="1",
        relation="owner",
        subject_type="auth/user",
        subject_id="1",
    )
    # The old source is visited first, but its transferred rows remain protected.
    (old / "permissions.zed").write_text("definition old/remaining {}\n", encoding="utf-8")
    (new / "permissions.zed").write_text(source, encoding="utf-8")
    configs.append(SimpleNamespace(name="new", path=str(new)))

    flags = ["--package", "old"] if scoped else []
    call_command("rebac", "sync", "--prune-stale", "--yes", *flags, stdout=io.StringIO())
    if scoped:
        # The next ordinary sync must retain the transferred no_update metadata.
        call_command("rebac", "sync", stdout=io.StringIO())

    permission.refresh_from_db()
    assert permission.expression == "nil"
    assert SchemaDefinition.objects.get(resource_type="shared/item").pk == definition.pk
    assert active_relationship_model().objects.filter(pk=relationship.pk).exists()
    assert not PackageManagedRecord.objects.filter(
        package="old", external_id__contains="shared/"
    ).exists()
    assert PackageManagedRecord.objects.filter(
        package="new",
        external_id="permission:shared/item#read",
        no_update=True,
    ).exists()


@pytest.mark.django_db
def test_prune_stale_validates_unselected_sources_before_deleting(monkeypatch, tmp_path) -> None:
    call_command("rebac", "sync", stdout=io.StringIO())
    stale = SchemaDefinition.objects.create(resource_type="removed/type")
    _managed_record("definition:removed/type", stale, package="removed")
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "permissions.zed").write_text(
        "definition invalid/type { permission read = missing }\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "rebac.management.commands.rebac.apps.get_app_configs",
        lambda: [SimpleNamespace(name="invalid", path=str(invalid))],
    )
    with pytest.raises(CommandError, match="Schema validation failed"):
        call_command(
            "rebac",
            "sync",
            "--prune-stale",
            "--package",
            "removed",
            "--yes",
            stdout=io.StringIO(),
        )
    assert SchemaDefinition.objects.filter(pk=stale.pk).exists()
    assert PackageManagedRecord.objects.filter(package="removed").exists()


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_prune_stale_rolls_policy_tuple_and_revision_back_together(
    storage, settings, monkeypatch
) -> None:
    from rebac.management.commands.rebac import Command
    from rebac.models import active_relationship_model
    from rebac.models.generation import SchemaGeneration

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    call_command("rebac", "sync", stdout=io.StringIO())
    stale = SchemaDefinition.objects.create(resource_type="removed/type")
    _managed_record("definition:removed/type", stale, package="removed")
    rows = active_relationship_model().objects
    relationship = rows.create(
        resource_type="removed/type",
        resource_id="1",
        relation="member",
        subject_type="auth/user",
        subject_id="1",
    )
    revision = SchemaGeneration.objects.get(pk=1).revision
    prune = Command._prune_package_records

    def fail_after_retirement(self, **kwargs):
        drift = prune(self, **kwargs)
        if kwargs["package"] == "removed":
            assert not SchemaDefinition.objects.filter(pk=stale.pk).exists()
            assert not rows.filter(pk=relationship.pk).exists()
            raise RuntimeError("retirement aborted")
        return drift

    monkeypatch.setattr(Command, "_prune_package_records", fail_after_retirement)
    with pytest.raises(RuntimeError, match="retirement aborted"):
        call_command("rebac", "sync", "--prune-stale", "--yes", stdout=io.StringIO())
    assert SchemaDefinition.objects.filter(pk=stale.pk).exists()
    assert PackageManagedRecord.objects.filter(package="removed").exists()
    assert rows.filter(pk=relationship.pk).exists()
    assert SchemaGeneration.objects.get(pk=1).revision == revision


def _two_packages(tmp_path, monkeypatch, *, retired_schema):
    current = tmp_path / "current"
    retired = tmp_path / "retired"
    current.mkdir()
    retired.mkdir()
    (current / "permissions.zed").write_text(
        "definition auth/user {}\n"
        "definition current/item { relation owner: auth/user permission read = owner }\n",
        encoding="utf-8",
    )
    (retired / "permissions.zed").write_text(retired_schema, encoding="utf-8")
    configs = [
        SimpleNamespace(name="current", path=str(current)),
        SimpleNamespace(name="retired", path=str(retired)),
    ]
    monkeypatch.setattr("rebac.management.commands.rebac.apps.get_app_configs", lambda: configs)
    call_command("rebac", "sync", stdout=io.StringIO())
    return configs


@pytest.mark.django_db
def test_prune_stale_refuses_a_policy_alias_that_is_not_the_relationship_write_alias(
    monkeypatch, tmp_path
) -> None:
    from django.db import router

    from rebac.models import active_relationship_model

    configs = _two_packages(
        tmp_path,
        monkeypatch,
        retired_schema="definition retired/item { relation member: auth/user }\n",
    )
    active_relationship_model().objects.create(
        resource_type="retired/item",
        resource_id="1",
        relation="member",
        subject_type="auth/user",
        subject_id="1",
    )
    configs.pop()
    original = router.db_for_write

    def routed(model, **hints):
        if model is active_relationship_model():
            return "other"
        return original(model, **hints)

    monkeypatch.setattr(router, "db_for_write", routed)
    with pytest.raises(CommandError, match=r"relationships live on 'other'"):
        call_command("rebac", "sync", "--prune-stale", "--yes", stdout=io.StringIO())
    assert SchemaDefinition.objects.filter(resource_type="retired/item").exists()
    assert active_relationship_model().objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("storage", ("denormalized", "registry"))
def test_prune_stale_retires_caveat_tuples_and_registry_rows_and_reports_counts(
    storage, settings, monkeypatch, tmp_path
) -> None:
    from rebac.models import RebacResource, active_relationship_model

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    configs = _two_packages(
        tmp_path,
        monkeypatch,
        retired_schema=(
            "caveat retired_check(level int) { level > 0 }\n"
            "definition retired/item { relation member: auth/user }\n"
        ),
    )
    rows = active_relationship_model().objects
    rows.create(
        resource_type="retired/item",
        resource_id="1",
        relation="member",
        subject_type="auth/user",
        subject_id="1",
    )
    rows.create(
        resource_type="current/item",
        resource_id="2",
        relation="owner",
        subject_type="auth/user",
        subject_id="1",
        caveat_name="retired_check",
    )
    rows.create(
        resource_type="current/item",
        resource_id="3",
        relation="owner",
        subject_type="auth/user",
        subject_id="1",
    )
    if storage == "registry":
        assert RebacResource.objects.filter(resource_type="retired/item").exists()
    configs.pop()

    check = io.StringIO()
    with pytest.raises(CommandError, match="drift"):
        call_command("rebac", "sync", "--prune-stale", "--check", stdout=check)
    assert "1 relationship(s) naming retired definition retired/item" in check.getvalue()
    assert "1 relationship(s) naming retired caveat retired_check" in check.getvalue()
    assert rows.count() == 3

    out = io.StringIO()
    call_command("rebac", "sync", "--prune-stale", "--yes", stdout=out)
    assert "1 relationship(s) naming retired definition retired/item" in out.getvalue()
    assert "1 relationship(s) naming retired caveat retired_check" in out.getvalue()
    assert [row.resource_id for row in rows.all()] == ["3"]
    assert not RebacResource.objects.filter(resource_type="retired/item").exists()
