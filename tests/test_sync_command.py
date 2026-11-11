from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
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
@pytest.mark.parametrize("synced", [True, False], ids=["declaration-changed", "admin-edited"])
def test_sync_check_names_why_a_managed_row_drifted(synced: bool) -> None:
    from rebac.management.commands.rebac import Command

    call_command("rebac", "sync", stdout=io.StringIO())
    post_def = SchemaDefinition.objects.get(resource_type="blog/post")
    SchemaPermission.objects.filter(definition=post_def, name="read").update(expression="owner")
    if synced:
        # The row holds what an earlier sync wrote: the declaration changed since.
        PackageManagedRecord.objects.filter(external_id="permission:blog/post#read").update(
            content_hash=Command._hash_payload(
                {"definition": post_def, "name": "read", "expression": "owner"}
            )
        )
    out = io.StringIO()
    with pytest.raises(CommandError, match="Schema drift detected"):
        call_command("rebac", "sync", "--check", stdout=out)
    cause = (
        "declaration changed; sync applies"
        if synced
        else "admin-edited; --force-overwrite to apply"
    )
    assert f"! drift: permission:blog/post#read ({cause})" in out.getvalue()


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
def test_sync_only_prunes_unmanaged_stale_children_when_forced(force: bool) -> None:
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


def _source(tmp_path, monkeypatch, name: str, text: str) -> None:
    """Install ``text`` as the one package ``name`` declares, replacing an earlier one."""
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "permissions.zed").write_text(text, encoding="utf-8")
    monkeypatch.setattr(
        "rebac.management.commands.rebac.apps.get_app_configs",
        lambda: [SimpleNamespace(name=name, path=str(root))],
    )


def _sync(*flags: str) -> str:
    out = io.StringIO()
    call_command("rebac", "sync", *flags, stdout=out)
    return out.getvalue()


def _docs(
    *, level: int = 0, viewer: str = "auth/user", read: str = "owner + viewer", extra: str = ""
) -> str:
    """The ``docs`` package: a caveat, a relation and a permission that each change alone."""
    return (
        f"caveat level_check(level int) {{ level > {level} }}\n"
        "definition auth/user {}\n"
        "definition doc/item {\n"
        " relation owner: auth/user\n"
        f" relation viewer: {viewer}\n"
        f"{extra}"
        f" permission read = {read}\n"
        "}\n"
    )


def _doc_children(model_cls: Any) -> Any:
    return model_cls.objects.filter(definition__resource_type="doc/item")


# Each fragment's managed id, the ``_docs`` change to its declaration and its stored row.
_FRAGMENTS = {
    "caveat": (
        "caveat:level_check",
        {"level": 1},
        lambda: SchemaCaveat.objects.get(name="level_check"),
    ),
    "relation": (
        "relation:doc/item#viewer",
        {"viewer": "auth/user with level_check"},
        lambda: _doc_children(SchemaRelation).get(name="viewer"),
    ),
    "permission": (
        "permission:doc/item#read",
        {"read": "owner"},
        lambda: _doc_children(SchemaPermission).get(name="read"),
    ),
}


def _records() -> dict[str, tuple[str, Any]]:
    return {
        record.external_id: (record.content_hash, record.last_synced_at)
        for record in PackageManagedRecord.objects.filter(package="docs")
    }


def _store_tuples(resource_type: str, relation: str, count: int = 1) -> Any:
    """Store ``count`` tuples of ``relation`` on ``resource_type:1``; the tuples of that relation."""
    from rebac.models import active_relationship_model

    rows = active_relationship_model().objects
    for subject_id in range(count):
        rows.create(
            resource_type=resource_type,
            resource_id="1",
            relation=relation,
            subject_type="auth/user",
            subject_id=str(subject_id),
        )
    return rows.filter(resource_type=resource_type, relation=relation)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", sorted(_FRAGMENTS))
def test_sync_applies_a_changed_declaration_to_an_unedited_row(kind, monkeypatch, tmp_path) -> None:
    from rebac.management.commands.rebac import Command

    external_id, change, stored = _FRAGMENTS[kind]
    _source(tmp_path, monkeypatch, "docs", _docs())
    _sync()
    before = stored()
    _source(tmp_path, monkeypatch, "docs", _docs(**change))

    out = _sync()

    row = stored()
    assert f"~ updated: {external_id} (declaration changed)" in out
    assert "drift" not in out
    assert row.pk == before.pk
    assert Command._row_hash(row) != Command._row_hash(before)
    assert _records()[external_id][0] == Command._row_hash(row)
    assert "OK — no drift." in _sync("--check")


@pytest.mark.django_db
@pytest.mark.parametrize("force", [False, True], ids=["kept", "forced"])
@pytest.mark.parametrize("changed", [False, True], ids=["declared-same", "declaration-changed"])
def test_sync_keeps_an_admin_edit_unless_forced(changed, force, monkeypatch, tmp_path) -> None:
    _source(tmp_path, monkeypatch, "docs", _docs())
    _sync()
    read = _doc_children(SchemaPermission).filter(name="read")
    read.update(expression="viewer")  # The admin's edit.
    synced = _records()
    declared = "owner" if changed else "owner + viewer"
    _source(tmp_path, monkeypatch, "docs", _docs(read=declared))

    out = _sync(*(["--force-overwrite", "--yes"] if force else []))

    if force:
        assert "~ updated: permission:doc/item#read (admin-edited)" in out
        assert read.get().expression == declared
        assert "OK — no drift." in _sync("--check")
    else:
        assert "! drift: permission:doc/item#read (admin-edited; --force-overwrite to apply)" in out
        assert "~ updated" not in out
        assert read.get().expression == "viewer"
        assert _records() == synced


@pytest.mark.django_db
def test_sync_check_reports_changed_declarations_and_admin_edits(monkeypatch, tmp_path) -> None:
    _source(tmp_path, monkeypatch, "docs", _docs())
    _sync()
    SchemaCaveat.objects.filter(name="level_check").update(expression="level > 5")
    synced = _records()
    _source(tmp_path, monkeypatch, "docs", _docs(read="owner"))

    out = io.StringIO()
    with pytest.raises(CommandError, match="Schema drift detected"):
        call_command("rebac", "sync", "--check", stdout=out)

    assert "! drift: permission:doc/item#read (declaration changed; sync applies)" in (
        out.getvalue()
    )
    assert "! drift: caveat:level_check (admin-edited; --force-overwrite to apply)" in (
        out.getvalue()
    )
    assert _doc_children(SchemaPermission).get(name="read").expression == "owner + viewer"
    assert SchemaCaveat.objects.get(name="level_check").expression == "level > 5"
    assert _records() == synced


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_sync_writes_changed_edited_unchanged_new_and_stale_rows_of_one_package(
    monkeypatch, tmp_path
) -> None:
    from rebac.management.commands.rebac import Command

    def declared(changed: str, edited: str, other: str) -> str:
        return _docs(
            read="owner",
            extra=f"{other} permission changed = {changed}\n"
            f" permission edited = {edited}\n permission same = owner\n",
        )

    stale = " relation held: auth/user\n permission dropped = owner\n"
    _source(tmp_path, monkeypatch, "docs", declared("owner", "owner", stale))
    _sync()
    _store_tuples("doc/item", "held")
    permissions = _doc_children(SchemaPermission)
    permissions.filter(name="edited").update(expression="viewer")  # The admin's edit.
    synced = _records()
    added = " relation reviewer: auth/user\n permission added = reviewer\n"
    _source(tmp_path, monkeypatch, "docs", declared("viewer", "owner + viewer", added))

    out = _sync()

    assert dict(permissions.values_list("name", "expression")) == {
        "read": "owner",
        "changed": "viewer",
        "edited": "viewer",
        "same": "owner",
        "added": "reviewer",
    }
    assert "~ updated: permission:doc/item#changed (declaration changed)" in out
    assert "! drift: permission:doc/item#edited (admin-edited; --force-overwrite to apply)" in out
    assert "- pruned: stale SchemaPermission doc/item#dropped" in out
    assert (
        "! drift: stale SchemaRelation doc/item#held "
        "(1 relationship(s) remain; delete them or --prune-stale)"
    ) in out
    assert _doc_children(SchemaRelation).filter(name="held").exists()
    records = _records()
    assert records["permission:doc/item#changed"][0] == Command._row_hash(
        permissions.get(name="changed")
    )
    for kept in (
        "permission:doc/item#edited",
        "permission:doc/item#same",
        "relation:doc/item#held",
    ):
        assert records[kept] == synced[kept]
    assert records.keys() - synced.keys() == {
        "relation:doc/item#reviewer",
        "permission:doc/item#added",
    }
    assert synced.keys() - records.keys() == {"permission:doc/item#dropped"}


@pytest.mark.django_db
def test_sync_prunes_an_unedited_stale_child_and_keeps_an_edited_one(monkeypatch, tmp_path) -> None:
    stale = " relation legacy: auth/user\n permission old = owner\n permission edited = owner\n"
    _source(tmp_path, monkeypatch, "docs", _docs(extra=stale))
    _sync()
    _doc_children(SchemaPermission).filter(name="edited").update(expression="read")
    _source(tmp_path, monkeypatch, "docs", _docs())
    edited = (
        "! drift: stale SchemaPermission doc/item#edited "
        "(admin-edited; --prune-stale or --force-overwrite to delete)"
    )

    check = io.StringIO()
    with pytest.raises(CommandError, match="Schema drift detected"):
        call_command("rebac", "sync", "--check", stdout=check)
    for name in ("SchemaRelation doc/item#legacy", "SchemaPermission doc/item#old"):
        assert f"! drift: stale {name} (no longer declared; sync prunes)" in check.getvalue()
    assert edited in check.getvalue()
    assert _doc_children(SchemaRelation).filter(name="legacy").exists()

    out = _sync()

    assert "- pruned: stale SchemaRelation doc/item#legacy" in out
    assert "- pruned: stale SchemaPermission doc/item#old" in out
    assert edited in out
    assert set(_doc_children(SchemaRelation).values_list("name", flat=True)) == {
        "owner",
        "viewer",
    }
    assert set(_doc_children(SchemaPermission).values_list("name", flat=True)) == {
        "read",
        "edited",
    }
    assert not {"relation:doc/item#legacy", "permission:doc/item#old"} & _records().keys()
    assert "permission:doc/item#edited" in _records()


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_sync_keeps_a_stale_stored_relation_while_tuples_hold_it(
    storage, settings, monkeypatch, tmp_path
) -> None:
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    _source(tmp_path, monkeypatch, "docs", _docs(extra=" relation legacy: auth/user\n"))
    _sync()
    held = _store_tuples("doc/item", "legacy", count=2)
    _store_tuples("doc/item", "owner")  # Another relation's tuple holds nothing stale.
    _source(tmp_path, monkeypatch, "docs", _docs())
    kept = (
        "! drift: stale SchemaRelation doc/item#legacy "
        "(2 relationship(s) remain; delete them or --prune-stale)"
    )

    check = io.StringIO()
    with pytest.raises(CommandError, match="Schema drift detected"):
        call_command("rebac", "sync", "--check", stdout=check)
    assert kept in check.getvalue()
    assert kept in _sync()
    assert _doc_children(SchemaRelation).filter(name="legacy").exists()
    assert held.count() == 2  # Sync retires no tuple.

    held.delete()
    assert "- pruned: stale SchemaRelation doc/item#legacy" in _sync()
    assert not _doc_children(SchemaRelation).filter(name="legacy").exists()
    assert "relation:doc/item#legacy" not in _records()


@pytest.mark.django_db
@pytest.mark.parametrize("backing", ["rebac:field=folder", "rebac:const=admins"])
def test_sync_prunes_a_stale_backed_relation_which_stores_no_tuples(
    backing, monkeypatch, tmp_path
) -> None:
    folder = "definition doc/folder { relation owner: auth/user }\n"
    _source(
        tmp_path,
        monkeypatch,
        "docs",
        folder + _docs(extra=f" relation folder: doc/folder // {backing}\n"),
    )
    _sync()
    _source(tmp_path, monkeypatch, "docs", folder + _docs())

    assert "- pruned: stale SchemaRelation doc/item#folder" in _sync()
    assert not _doc_children(SchemaRelation).filter(name="folder").exists()


@pytest.mark.django_db
def test_sync_keeps_a_stale_child_a_kept_permission_names(monkeypatch, tmp_path) -> None:
    stale = (
        " relation legacy: auth/user\n relation other: auth/user\n relation unused: auth/user\n"
        " permission old = other\n"
    )
    _source(tmp_path, monkeypatch, "docs", _docs(extra=stale))
    _sync()
    # The admin's edit names a relation and a permission the package then drops.
    _doc_children(SchemaPermission).filter(name="read").update(expression="owner + legacy + old")
    _source(tmp_path, monkeypatch, "docs", _docs())
    hint = "--prune-stale or --force-overwrite to delete"

    out = _sync()

    assert "! drift: permission:doc/item#read (admin-edited; --force-overwrite to apply)" in out
    for row, permission in (
        ("SchemaRelation doc/item#legacy", "doc/item#read"),
        ("SchemaPermission doc/item#old", "doc/item#read"),
        ("SchemaRelation doc/item#other", "doc/item#old"),  # Named by a permission kept for it.
    ):
        assert f"! drift: stale {row} (named by kept permission {permission}; {hint})" in out
    assert "- pruned: stale SchemaRelation doc/item#unused" in out
    assert set(_doc_children(SchemaRelation).values_list("name", flat=True)) == {
        "owner",
        "viewer",
        "legacy",
        "other",
    }
    assert _doc_children(SchemaPermission).filter(name="old").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("backing", ["", " // rebac:const=3"], ids=["stored", "const-backed"])
def test_sync_prunes_a_stale_relation_only_another_definitions_kept_arrow_names(
    backing, monkeypatch, tmp_path
) -> None:
    from rebac import ObjectRef, SubjectRef, sudo
    from rebac.backends import backend, reset_backend

    from .testapp.models import Folder, Post

    legacy = f" relation legacy: auth/user{backing}\n"
    declared = (
        "definition auth/user {}\n"
        "definition blog/folder {\n relation owner: auth/user\n" + legacy + "}\n"
        "definition blog/post {\n relation folder: blog/folder // rebac:field=folder\n"
        " relation owner: auth/user\n permission read = owner + folder->owner\n}\n"
    )
    _source(tmp_path, monkeypatch, "docs", declared)
    _sync()
    with sudo(reason="arrow to a pruned relation"):
        post = Post.objects.create(title="post", folder=Folder.objects.create(name="folder"))
    read = SchemaPermission.objects.filter(definition__resource_type="blog/post", name="read")
    read.update(expression="owner + folder->legacy")  # The admin's edit walks to the folder.

    def reads() -> bool:
        reset_backend()
        subject, resource = SubjectRef.of("auth/user", "3"), ObjectRef("blog/post", str(post.pk))
        return backend().check_access(subject=subject, action="read", resource=resource).allowed

    assert reads() is bool(backing)  # Only the constant grants user 3 through the arm.
    _source(tmp_path, monkeypatch, "docs", declared.replace(legacy, ""))

    out = _sync()

    # Names on the kept permission's own definition are kept, not its arrows'
    # targets: the relation is pruned, the policy still validates, and the arm
    # grants nothing.
    assert "- pruned: stale SchemaRelation blog/folder#legacy" in out
    assert "Sync complete." in out
    assert read.get().expression == "owner + folder->legacy"
    assert reads() is False


# A source of every stored shape, and the content hashes a 0.34.0 sync recorded for it.
# Frozen: a change to how sync hashes a row would turn each deployed row into an admin edit.
_EARLIER = (
    "use expiration\n"
    "caveat office_hours(hour int, zones list<string>) {{ hour < {hour} }}\n"
    "definition auth/user {{}}\n"
    "definition auth/group {{ relation member: auth/user | auth/group#member{group} }}\n"
    "definition doc/folder {{ relation owner: auth/user{group} }}\n"
    "definition doc/item {{\n"
    " relation owner: auth/user | auth/group#member{caveat}\n"
    " relation viewer: auth/user | auth/user:*{expiring}\n"
    " relation folder: doc/folder // rebac:field={folder}\n"
    " relation admin: auth/group // rebac:const={admin}\n"
    " permission read = owner + viewer{arrow}\n"
    " permission write = owner{exclusion}\n"
    "}}\n"
)
_EARLIER_034 = {
    "hour": 18,
    "group": "",
    "caveat": " with office_hours",
    "expiring": " | auth/user with expiration",
    "folder": "folder",
    "admin": "admins",
    "arrow": " + folder->owner",
    "exclusion": " - viewer",
}
_EARLIER_HASHES = {
    "caveat:office_hours": "14555e20ea4f7f6d524e5b80a7757198a52e6ce6a5eb2057c4d37355f01b3de1",
    "definition:auth/group": "1254b13139555473bd2ca42a12bf4fa901ca491a08ddc989b36e5d3167b969c0",
    "definition:auth/user": "faee4b066aa2cec2b8fdb89985a8b863f7be7a5448211ccdebc8f06cb4035847",
    "definition:doc/folder": "0028a2139db6001482ea54396d5291066f7b1ca55399ab1e315f64d0bb5b3d07",
    "definition:doc/item": "bc81522d589b2b0375fdd47b30fdc1080200bba38f98222da4292338fdb7f162",
    "permission:doc/item#read": "e62d96f6752d3bbc838163b3232fd1c600ab06970c76bb92ee08a5ca0a3a3e37",
    "permission:doc/item#write": "d2b097c14f93b125172a3629b06f1505457b3037418a0bbd9dde3f9d96ddfd15",
    "relation:auth/group#member": "2a0b747480388ac833d5004f2aaea2c58704b371cf3cd20a9402fae627a768a8",
    "relation:doc/folder#owner": "a03c87cb013ee8579046011ef5029e301ba8e8e1b384a9f900d7deadc866f804",
    "relation:doc/item#admin": "cae044514d9b0677ef6b0d7a4c98e144a4d428d9f71e2bcc9fcd11c68a058bc3",
    "relation:doc/item#folder": "c4b084c06d25ce06cfa20db14ed8ea33ff83fef788983bd2ebbd22ca770f4ce6",
    "relation:doc/item#owner": "7ae818ffed8277b0da70e5fe8666765f1fde1b0d5f64f3ad0d420e05b49ab6f8",
    "relation:doc/item#viewer": "9c19b1b5f0ba2cfd8fe87841ffb78353dd968f816d94db2fbda57dd908b0591e",
}


@pytest.mark.django_db
def test_sync_takes_rows_an_earlier_release_recorded_for_unedited(monkeypatch, tmp_path) -> None:
    from rebac.management.commands.rebac import Command

    _source(tmp_path, monkeypatch, "docs", _EARLIER.format(**_EARLIER_034))
    _sync()
    records = PackageManagedRecord.objects.filter(package="docs")
    # Sync records what 0.34.0 recorded, and each row it wrote hashes the same.
    assert {record.external_id: record.content_hash for record in records} == _EARLIER_HASHES
    assert {record.external_id: Command._row_hash(record.target) for record in records} == (
        _EARLIER_HASHES
    )
    # So every declaration a package then changes is applied, none taken for an admin edit.
    changed = {
        "hour": 17,
        "group": " | auth/group#member",
        "caveat": "",
        "expiring": "",
        "folder": "parent",
        "admin": "staff",
        "arrow": "",
        "exclusion": "",
    }
    _source(tmp_path, monkeypatch, "docs", _EARLIER.format(**changed))

    out = _sync()

    assert "drift" not in out
    updated = {line.split()[2] for line in out.splitlines() if "~ updated: " in line}
    assert updated == {eid for eid in _EARLIER_HASHES if not eid.startswith("definition:")}
    assert "(declaration changed)" in out and "(admin-edited)" not in out
    assert "OK — no drift." in _sync("--check")


@pytest.mark.django_db
@pytest.mark.parametrize("returned", [True, False], ids=["keys-returned", "keys-read-back"])
def test_sync_statements_do_not_grow_with_the_rows_it_writes(returned, monkeypatch, tmp_path):
    # Without returned keys (MySQL), sync reads the created rows' keys back once per table.
    monkeypatch.setattr(connection.features, "can_return_rows_from_bulk_insert", returned)

    def statements(size: int) -> tuple[int, int]:
        def text(read: str, stale: str) -> str:
            return f"caveat c{size}(on bool) {{ on }}\n" + "".join(
                f"definition s{size}/n{n} {{ relation member: s{size}/n{n} with c{size} "
                f"relation viewer: s{size}/n{n} {stale.format(f's{size}/n{n}')}"
                f"permission read = {read} }}\n"
                for n in range(size)
            )

        stale = "relation held: {0} relation spare: {0} permission old = member "
        _source(tmp_path, monkeypatch, f"app{size}", text("member", stale))
        with CaptureQueriesContext(connection) as written:
            call_command("rebac", "sync", stdout=io.StringIO())
        for n in range(size):
            _store_tuples(f"s{size}/n{n}", "held")
        # Every definition's permission changes, its stale permission and its
        # tuple-free stale relation are pruned, and its held relation is kept.
        _source(tmp_path, monkeypatch, f"app{size}", text("viewer", ""))
        with CaptureQueriesContext(connection) as applied:
            out = _sync()
        assert out.count("~ updated: ") == out.count(" relationship(s) remain") == size
        assert out.count("- pruned: ") == 2 * size
        return len(written), len(applied)

    call_command("rebac", "sync", stdout=io.StringIO())  # A published policy beside the sources.
    ContentType.objects.get_for_model(SchemaCaveat)  # Cached on first use, outside the measure.
    assert statements(1) == statements(20)
    records = PackageManagedRecord.objects.filter(package="app20")
    assert len(records) == 101
    assert all(record.external_id.endswith(f":{record.target}") for record in records)


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
