"""Tests for ``manage.py rebac migrate-storage`` (proposal 0001)."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command

from rebac.models import RebacResource, Relationship, RelationshipRegistry


def _seed_denormalized(rows: int = 3) -> None:
    """Three rows exercising direct user, wildcard, and subject-set shapes."""
    Relationship.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="42",
    )
    Relationship.objects.create(
        resource_type="storage/file",
        resource_id="xyz",
        relation="owner",
        subject_type="auth/user",
        subject_id="42",
    )
    Relationship.objects.create(
        resource_type="storage/file",
        resource_id="xyz",
        relation="viewer",
        subject_type="auth/group",
        subject_id="eng",
        optional_subject_relation="member",
    )


def _seed_registry() -> None:
    RelationshipRegistry.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="42",
    )
    RelationshipRegistry.objects.create(
        resource_type="storage/file",
        resource_id="xyz",
        relation="owner",
        subject_type="auth/user",
        subject_id="42",
    )
    RelationshipRegistry.objects.create(
        resource_type="storage/file",
        resource_id="xyz",
        relation="viewer",
        subject_type="auth/group",
        subject_id="eng",
        optional_subject_relation="member",
    )


# ---------- denormalized → registry ----------


@pytest.mark.django_db(transaction=True)
def test_dry_run_does_not_write():
    _seed_denormalized()
    out = io.StringIO()
    call_command("rebac", "migrate-storage", "--to", "registry", "--dry-run", stdout=out)
    assert "--dry-run: no writes performed" in out.getvalue()
    assert RebacResource.objects.count() == 0
    assert RelationshipRegistry.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_migrate_to_registry_copies_all_rows():
    _seed_denormalized()
    out = io.StringIO()
    call_command("rebac", "migrate-storage", "--to", "registry", stdout=out)
    assert RelationshipRegistry.objects.count() == 3
    # Four unique (type, id) pairs: storage/file:abc, storage/file:xyz,
    # auth/user:42, auth/group:eng.
    assert RebacResource.objects.count() == 4


@pytest.mark.django_db(transaction=True)
def test_migrate_to_registry_is_idempotent():
    _seed_denormalized()
    call_command("rebac", "migrate-storage", "--to", "registry", stdout=io.StringIO())
    call_command("rebac", "migrate-storage", "--to", "registry", stdout=io.StringIO())
    # No growth on re-run — bulk_create(ignore_conflicts) absorbed dupes.
    assert RelationshipRegistry.objects.count() == 3
    assert RebacResource.objects.count() == 4


@pytest.mark.django_db(transaction=True)
def test_migrate_to_registry_preserves_optional_subject_relation():
    _seed_denormalized()
    call_command("rebac", "migrate-storage", "--to", "registry", stdout=io.StringIO())
    subj_set_row = RelationshipRegistry.objects.get(
        resource_type="storage/file",
        resource_id="xyz",
        subject_type="auth/group",
    )
    assert subj_set_row.optional_subject_relation == "member"


@pytest.mark.django_db(transaction=True)
def test_migrate_to_registry_preserves_caveat_metadata():
    Relationship.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="42",
        caveat_name="ip_in_cidr",
        caveat_context={"cidr": "10.0.0.0/8"},
        written_at_xid=99,
    )
    call_command("rebac", "migrate-storage", "--to", "registry", stdout=io.StringIO())
    row = RelationshipRegistry.objects.get(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
    )
    assert row.caveat_name == "ip_in_cidr"
    assert row.caveat_context == {"cidr": "10.0.0.0/8"}
    assert row.written_at_xid == 99


# ---------- registry → denormalized (reverse direction) ----------


@pytest.mark.django_db(transaction=True)
def test_migrate_to_denormalized_copies_all_rows():
    _seed_registry()
    call_command(
        "rebac",
        "migrate-storage",
        "--from",
        "registry",
        "--to",
        "denormalized",
        stdout=io.StringIO(),
    )
    assert Relationship.objects.count() == 3
    direct = Relationship.objects.get(
        resource_type="storage/file",
        resource_id="abc",
        subject_id="42",
    )
    assert direct.relation == "viewer"


@pytest.mark.django_db(transaction=True)
def test_round_trip_denormalized_registry_denormalized():
    """denormalized → registry → denormalized → identical row set."""
    _seed_denormalized()
    snapshot = sorted(
        Relationship.objects.values_list(
            "resource_type",
            "resource_id",
            "relation",
            "subject_type",
            "subject_id",
            "optional_subject_relation",
        )
    )
    call_command("rebac", "migrate-storage", "--to", "registry", stdout=io.StringIO())
    Relationship.objects.all().delete()
    call_command(
        "rebac",
        "migrate-storage",
        "--from",
        "registry",
        "--to",
        "denormalized",
        stdout=io.StringIO(),
    )
    roundtripped = sorted(
        Relationship.objects.values_list(
            "resource_type",
            "resource_id",
            "relation",
            "subject_type",
            "subject_id",
            "optional_subject_relation",
        )
    )
    assert roundtripped == snapshot


# ---------- argument validation ----------


@pytest.mark.django_db
def test_from_equals_to_is_rejected():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="--from and --to must differ"):
        call_command(
            "rebac",
            "migrate-storage",
            "--from",
            "registry",
            "--to",
            "registry",
            stdout=io.StringIO(),
        )


@pytest.mark.django_db
def test_empty_source_short_circuits():
    out = io.StringIO()
    call_command("rebac", "migrate-storage", "--to", "registry", stdout=out)
    assert "nothing to copy" in out.getvalue()
    assert RelationshipRegistry.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_batch_argument_controls_pagination():
    """``--batch`` slices the source query; behavior is identical for any batch."""
    for i in range(5):
        Relationship.objects.create(
            resource_type="storage/file",
            resource_id=f"id{i}",
            relation="viewer",
            subject_type="auth/user",
            subject_id="42",
        )
    call_command(
        "rebac",
        "migrate-storage",
        "--to",
        "registry",
        "--batch",
        "2",
        stdout=io.StringIO(),
    )
    assert RelationshipRegistry.objects.count() == 5
