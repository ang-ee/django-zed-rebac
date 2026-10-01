"""Persistent caveat identities used by compiled permission bounds."""

from importlib import import_module

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.models.signals import post_save, pre_save

from rebac.caveats import instance_key
from rebac.compile import formulas as conditions
from rebac.models.relationship import WIRE_VALUE_FIELDS, Relationship, RelationshipRegistry
from rebac.models.resource import RebacResource
from rebac.testing import install_schema
from rebac.types import ObjectRef, RelationshipTuple, SubjectRef

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]


@pytest.fixture(params=["denormalized", "registry"])
def store(request, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    backend = install_schema("""
        caveat allowed(flag bool) { flag }
        caveat other(flag bool) { flag }
        definition auth/user {}
        definition test/item {
            relation viewer: auth/user | auth/user with allowed | auth/user with other
            permission read = viewer
        }
    """)
    model = Relationship if request.param == "denormalized" else RelationshipRegistry
    return backend, model


def candidate(model, *, resource="one", name="allowed", context=None):
    kwargs = {
        "relation": "viewer",
        "caveat_name": name,
        "caveat_context": context,
        "caveat_key": "untrusted input",
    }
    if model is Relationship:
        kwargs.update(
            resource_type="test/item",
            resource_id=resource,
            subject_type="auth/user",
            subject_id="alice",
        )
    else:
        kwargs.update(
            resource_fk=RebacResource.upsert_ref("test/item", resource),
            subject_fk=RebacResource.upsert_ref("auth/user", "alice"),
        )
    return model(**kwargs)


def assert_key(row):
    row.refresh_from_db()
    assert row.caveat_key == conditions.key(conditions.leaf(row.caveat_name, row.caveat_context))


def test_backend_upsert_refreshes_key(store):
    backend, model = store
    for flag in (True, False):
        backend.write_relationships(
            [
                RelationshipTuple(
                    ObjectRef("test/item", "one"),
                    "viewer",
                    SubjectRef.of("auth/user", "alice"),
                    caveat_name="allowed",
                    caveat_context={"flag": flag},
                )
            ]
        )
        row = model.objects.get()
        assert_key(row)
        assert row.caveat_key == instance_key("allowed", {"flag": flag})


def test_partial_save_uses_stored_excluded_caveat_fields(store):
    _, model = store
    row = candidate(model, context={"flag": True})
    row.save()
    row.caveat_name = "other"  # Deliberately excluded from this save.
    row.caveat_context = {"flag": False}
    row.save(update_fields=["caveat_context"])
    assert_key(row)
    assert row.caveat_name == "allowed"
    assert row.caveat_context == {"flag": False}
    row.caveat_name = "other"
    row.caveat_context = {"flag": True}  # Now exclude the context instead.
    row.save(update_fields=["caveat_name"])
    assert_key(row)
    assert row.caveat_context == {"flag": False}


def test_fixture_save_base_computes_key(store):
    _, model = store
    row = candidate(model, context={"flag": True})
    row.save_base(raw=True)
    assert_key(row)


def test_partial_save_on_reconstructed_instance_uses_stored_name(store):
    _, model = store
    row = candidate(model, context={"flag": True})
    row.save()
    replacement = model(pk=row.pk, caveat_context={"flag": False})
    replacement.save_base(update_fields=["caveat_context"])
    assert_key(row)
    assert row.caveat_name == "allowed"
    assert row.caveat_context == {"flag": False}


@pytest.mark.parametrize("raw", [False, True])
def test_receiver_normalization_is_included_in_key(store, raw):
    from rebac.compile.read import check

    backend, model = store
    allowed = candidate(model, resource="allowed", context={"flag": True})
    allowed.save()

    def normalize(sender, instance, raw, **kwargs):
        instance.caveat_context = {"flag": False}

    observed = []

    def observe(sender, instance, **kwargs):
        observed.append(instance.caveat_key)

    pre_save.connect(normalize, sender=model)
    post_save.connect(observe, sender=model)
    try:
        row = candidate(model, context={"flag": True})
        row.save_base(raw=True) if raw else row.save()
    finally:
        pre_save.disconnect(normalize, sender=model)
        post_save.disconnect(observe, sender=model)
    assert_key(row)
    assert row.caveat_context == {"flag": False}
    assert row.caveat_key != allowed.caveat_key
    assert observed == [row.caveat_key]
    assert not check(
        backend=backend,
        resource=ObjectRef("test/item", "one"),
        action="read",
        actor=SubjectRef.of("auth/user", "alice"),
        context=None,
        using="default",
    ).allowed


def test_json_database_representation_keeps_its_write_owned_verdict_key(store):
    from rebac.compile.conditions import CaveatVerdicts

    backend, model = store
    context = {"flag": True, "nested": [1e20, 1e23, {"float": 1.0, "int": 1}]}
    row = candidate(model, context=context)
    row.save()
    row.refresh_from_db()
    assert row.caveat_key == instance_key("allowed", context)
    # PostgreSQL expands scientific notation in JSONB. The label still
    # witnesses this payload; it need not equal a digest of the decoded JSON.
    verdicts = CaveatVerdicts.prepare(
        backend.schema(), ("test/item", "read"), context=None, using="default"
    )
    assert row.caveat_key in verdicts.true_keys
    assert instance_key("allowed", {"value": 1}) != instance_key("allowed", {"value": 1.0})


def test_bulk_create_upsert_and_ignored_conflict(store):
    _, model = store
    row = candidate(model, context={"flag": True})
    model.objects.bulk_create([row])
    assert_key(row)
    unique = (
        list(WIRE_VALUE_FIELDS)
        if model is Relationship
        else ["resource_fk", "relation", "subject_fk", "optional_subject_relation", "caveat_name"]
    )
    model.objects.bulk_create(
        [candidate(model, context={"flag": False})],
        update_conflicts=True,
        unique_fields=unique,
        update_fields=["caveat_context"],
    )
    assert_key(row)
    assert row.caveat_context == {"flag": False}
    model.objects.bulk_create([candidate(model, context={"flag": True})], ignore_conflicts=True)
    assert_key(row)
    assert row.caveat_context == {"flag": False}
    model.objects.bulk_create(
        [candidate(model, context={"flag": True})],
        update_conflicts=True,
        unique_fields=unique,
        update_fields=["written_at_xid"],
    )
    assert_key(row)
    assert row.caveat_context == {"flag": False}


def test_backfill_covers_both_storage_shapes_and_unconditional_rows():
    # Historical managers intentionally bypass the current model's write hooks.
    state = MigrationLoader(connection).project_state([("rebac", "0008_relationship_caveat_key")])
    apps = state.apps
    resource = apps.get_model("rebac", "RebacResource")
    item = resource.objects.create(resource_type="test/item", resource_id="one")
    actor = resource.objects.create(resource_type="auth/user", resource_id="alice")
    for model_name in ("Relationship", "RelationshipRegistry"):
        model = apps.get_model("rebac", model_name)
        kwargs = (
            {
                "resource_type": "test/item",
                "resource_id": "one",
                "subject_type": "auth/user",
                "subject_id": "alice",
            }
            if model_name == "Relationship"
            else {"resource_fk_id": item.pk, "subject_fk_id": actor.pk}
        )
        model.objects.create(
            **kwargs, pk=-1, relation="viewer", caveat_name="allowed", caveat_context={"flag": True}
        )
        model.objects.create(
            **kwargs, relation="viewer", caveat_name="", caveat_context={"flag": True}
        )
    migration = import_module("rebac.migrations.0008_relationship_caveat_key")
    # No DDL: call the data migration against its historical model state.
    migration.backfill_keys(apps, connection.schema_editor())
    for model_name in ("Relationship", "RelationshipRegistry"):
        rows = apps.get_model("rebac", model_name).objects.order_by("caveat_name")
        assert list(rows.values_list("caveat_key", flat=True)) == [
            "",
            instance_key("allowed", {"flag": True}),
        ]
