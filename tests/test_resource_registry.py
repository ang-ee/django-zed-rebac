"""Tests for ``RebacResource`` + ``RelationshipRegistry``.

Covers:
  - ``RebacResource.upsert_ref`` idempotency + lazy backing pointer fill.
  - ``RebacResource.upsert_refs_bulk`` (batched form used by migrate-storage).
  - ``RelationshipRegistry.objects`` string-kwarg translation (create / filter
    / exclude / get_or_create / update_or_create).
  - Property accessors on materialised rows (``row.resource_type`` etc.).
  - FK CASCADE on RebacResource deletion sweeps tuples.
  - Read-side never creates registry rows.
  - ``active_relationship_model()`` honors the setting.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from rebac import SubjectRef, sudo
from rebac.models import (
    RebacResource,
    Relationship,
    RelationshipRegistry,
    active_relationship_model,
)

# ---------- active_relationship_model ----------


def test_active_model_default_is_denormalized():
    assert active_relationship_model() is Relationship


def test_active_model_with_registry_setting():
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE="registry"):
        assert active_relationship_model() is RelationshipRegistry


# ---------- RebacResource.upsert_ref ----------


@pytest.mark.django_db
def test_upsert_ref_creates_then_returns_same_row():
    a = RebacResource.upsert_ref("storage/file", "abc")
    b = RebacResource.upsert_ref("storage/file", "abc")
    assert a.pk == b.pk
    assert RebacResource.objects.filter(resource_type="storage/file").count() == 1


@pytest.mark.django_db
def test_upsert_ref_fills_backing_pointer_lazily():
    """First writer with content_type=None creates the row; later writer fills it."""
    from django.contrib.contenttypes.models import ContentType

    # First call: no backing pointer.
    a = RebacResource.upsert_ref("auth/user", "42")
    assert a.content_type_id is None
    assert a.object_pk == ""

    # Second call with backing pointer fills it.
    ct = ContentType.objects.get_for_model(RebacResource)
    b = RebacResource.upsert_ref("auth/user", "42", content_type=ct, object_pk="42")
    assert a.pk == b.pk
    b.refresh_from_db()
    assert b.content_type_id == ct.id
    assert b.object_pk == "42"


@pytest.mark.django_db
def test_upsert_ref_does_not_overwrite_existing_backing():
    """First writer with backing wins; later writers don't clobber."""
    from django.contrib.contenttypes.models import ContentType

    ct1 = ContentType.objects.get_for_model(RebacResource)
    ct2 = ContentType.objects.get_for_model(RebacResource)  # same; for shape only
    RebacResource.upsert_ref("auth/user", "1", content_type=ct1, object_pk="1")
    row = RebacResource.upsert_ref("auth/user", "1", content_type=ct2, object_pk="999")
    row.refresh_from_db()
    assert row.object_pk == "1"  # NOT 999


@pytest.mark.django_db
def test_upsert_refs_bulk_returns_complete_pk_map():
    pairs = [("auth/user", "1"), ("auth/user", "2"), ("auth/user", "1")]  # dup intentional
    pk_map = RebacResource.upsert_refs_bulk(pairs)
    assert set(pk_map.keys()) == {("auth/user", "1"), ("auth/user", "2")}
    assert all(isinstance(v, int) for v in pk_map.values())


@pytest.mark.django_db
def test_upsert_refs_bulk_empty_short_circuits():
    """Empty input returns empty dict without issuing any query."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        result = RebacResource.upsert_refs_bulk([])
    assert result == {}
    # No new SELECT/INSERT against the registry table.
    assert len(ctx.captured_queries) == 0


# ---------- RelationshipRegistry manager — write paths ----------


@pytest.mark.django_db
def test_create_upserts_resource_and_subject_fks():
    row = RelationshipRegistry.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="42",
    )
    assert row.resource_fk.resource_type == "storage/file"
    assert row.resource_fk.resource_id == "abc"
    assert row.subject_fk.resource_type == "auth/user"
    assert row.subject_fk.resource_id == "42"
    # Two registry rows now exist — one per side.
    assert RebacResource.objects.count() == 2


@pytest.mark.django_db
def test_create_reuses_existing_registry_row():
    RebacResource.upsert_ref("auth/user", "42")
    before = RebacResource.objects.count()
    RelationshipRegistry.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="42",
    )
    # +1 (storage/file:abc) — auth/user:42 already existed.
    assert RebacResource.objects.count() == before + 1


@pytest.mark.django_db
def test_create_with_partial_resource_kwargs_raises():
    with pytest.raises(ValueError, match="resource_type and resource_id"):
        RelationshipRegistry.objects.create(
            resource_type="storage/file",
            # resource_id missing
            relation="viewer",
            subject_type="auth/user",
            subject_id="42",
        )


@pytest.mark.django_db
def test_get_or_create_upserts_and_dedups():
    a, created_a = RelationshipRegistry.objects.get_or_create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="42",
    )
    assert created_a is True
    b, created_b = RelationshipRegistry.objects.get_or_create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="42",
    )
    assert created_b is False
    assert a.pk == b.pk


# ---------- RelationshipRegistry manager — read paths ----------


@pytest.mark.django_db
def test_filter_translates_string_kwargs_to_fk_lookups():
    RelationshipRegistry.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="42",
    )
    RelationshipRegistry.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="owner",
        subject_type="auth/user",
        subject_id="42",
    )

    viewers = list(
        RelationshipRegistry.objects.filter(
            resource_type="storage/file",
            resource_id="abc",
            relation="viewer",
        )
    )
    assert len(viewers) == 1
    assert viewers[0].relation == "viewer"


@pytest.mark.django_db
def test_filter_translates_in_lookups():
    RelationshipRegistry.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="1",
    )
    RelationshipRegistry.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="2",
    )
    rows = RelationshipRegistry.objects.filter(
        resource_type="storage/file",
        subject_id__in=["1", "2"],
    )
    assert rows.count() == 2


@pytest.mark.django_db
def test_chained_filter_exclude_translate_at_every_step():
    """Chained calls re-translate kwargs — the QuerySet override is in scope."""
    RelationshipRegistry.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="1",
    )
    RelationshipRegistry.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="owner",
        subject_type="auth/user",
        subject_id="1",
    )
    rows = (
        RelationshipRegistry.objects.filter(resource_type="storage/file")
        .exclude(relation="owner")
        .filter(subject_id="1")
    )
    assert rows.count() == 1
    assert rows.first().relation == "viewer"


@pytest.mark.django_db
def test_property_accessors_avoid_n_plus_1():
    """Manager's default select_related means iterating rows hits FKs once."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    for i in range(5):
        RelationshipRegistry.objects.create(
            resource_type="storage/file",
            resource_id=f"id{i}",
            relation="viewer",
            subject_type="auth/user",
            subject_id=str(i),
        )

    # CaptureQueriesContext forces query logging regardless of DEBUG,
    # so the assertion is meaningful on default-DEBUG test runs.
    with CaptureQueriesContext(connection) as ctx:
        rows = list(RelationshipRegistry.objects.all())
        _ = [(r.resource_type, r.resource_id, r.subject_type, r.subject_id) for r in rows]
    # One SELECT with the 2-way JOIN; no per-row FK fetches.
    assert len(ctx.captured_queries) == 1, ctx.captured_queries


@pytest.mark.django_db
def test_filter_for_missing_resource_returns_empty():
    """Reads don't create registry rows. Look-up of a nonexistent (type,id) → no match."""
    before = RebacResource.objects.count()
    rows = RelationshipRegistry.objects.filter(
        resource_type="storage/nonexistent",
        resource_id="never_seen",
    )
    assert rows.count() == 0
    assert RebacResource.objects.count() == before


# ---------- FK CASCADE ----------


@pytest.mark.django_db
def test_resource_delete_cascades_to_tuples():
    row = RelationshipRegistry.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="42",
    )
    resource_pk = row.resource_fk_id
    assert RelationshipRegistry.objects.filter(pk=row.pk).exists()

    # Delete the resource's registry row directly.
    RebacResource.objects.filter(pk=resource_pk).delete()

    # The tuple is swept by the FK CASCADE — no orphan.
    assert not RelationshipRegistry.objects.filter(pk=row.pk).exists()


@pytest.mark.django_db
def test_subject_delete_also_cascades():
    """Cascade fires from the subject side too (both FKs are CASCADE)."""
    row = RelationshipRegistry.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="42",
    )
    subject_pk = row.subject_fk_id
    RebacResource.objects.filter(pk=subject_pk).delete()
    assert not RelationshipRegistry.objects.filter(pk=row.pk).exists()


# ---------- mode-agnostic relationship query helpers ----------


@pytest.mark.django_db
@pytest.mark.parametrize("model_cls", [Relationship, RelationshipRegistry])
def test_relationship_helpers_filter_and_order_wire_rows(model_cls):
    model_cls.objects.create(
        resource_type="storage/file",
        resource_id="b",
        relation="viewer",
        subject_type="auth/user",
        subject_id="2",
    )
    model_cls.objects.create(
        resource_type="storage/file",
        resource_id="a",
        relation="owner",
        subject_type="auth/user",
        subject_id="1",
    )

    rows = list(
        model_cls.objects.for_resource("storage/file", "a").order_by_subject().wire_values()
    )

    assert rows == [
        {
            "resource_type": "storage/file",
            "resource_id": "a",
            "relation": "owner",
            "subject_type": "auth/user",
            "subject_id": "1",
            "optional_subject_relation": "",
            "caveat_name": "",
        }
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("model_cls", [Relationship, RelationshipRegistry])
def test_for_subject_optional_relation_none_means_any_relation(model_cls):
    model_cls.objects.create(
        resource_type="storage/file",
        resource_id="a",
        relation="viewer",
        subject_type="auth/group",
        subject_id="eng",
        optional_subject_relation="member",
    )
    model_cls.objects.create(
        resource_type="storage/file",
        resource_id="b",
        relation="viewer",
        subject_type="auth/group",
        subject_id="eng",
    )

    any_rows = list(
        model_cls.objects.for_subject(
            "auth/group", "eng", optional_relation=None
        ).order_by_resource().wire_values()
    )
    direct_rows = list(
        model_cls.objects.for_subject("auth/group", "eng", optional_relation="").wire_values()
    )

    assert [row["resource_id"] for row in any_rows] == ["a", "b"]
    assert [row["resource_id"] for row in direct_rows] == ["b"]


@pytest.mark.django_db
def test_resolve_subjects_maps_registered_models_to_rows():
    from rebac.relationships import resolve_subjects
    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="subject")
    post_ref = SubjectRef.of("blog/post", str(post.pk))
    refs = [post_ref, SubjectRef.of("missing/type", "1")]

    assert resolve_subjects(refs) == {post_ref: post}
