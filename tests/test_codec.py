"""Identity conversion between model columns and stored tuple identities.

Vendor cases opt in with REBAC_TEST_SCHEMA_VENDORS=1.
"""

from __future__ import annotations

import os
from datetime import timedelta
from uuid import UUID

import pytest
from django.db import connection, models
from django.db.models import F, Value
from django.test import override_settings
from django.test.utils import CaptureQueriesContext, isolate_apps

from rebac.clock import application_now
from rebac.codec import identity_codec
from rebac.errors import SchemaError
from rebac.models.relationship import WIRE_PROJECTION_FIELDS, active_relationship_model
from rebac.models.resource import RebacResource

# Reuse the existing disposable vendor fixture and its opt-in convention.
from .test_schema_generation_vendors import vendor_connection, vendor_database  # noqa: F401

INTEGER_FIELDS = [
    models.SmallIntegerField,
    models.IntegerField,
    models.BigIntegerField,
    models.PositiveSmallIntegerField,
    models.PositiveIntegerField,
    models.PositiveBigIntegerField,
    models.SmallAutoField,
    models.AutoField,
    models.BigAutoField,
]


def _identity_model(field):
    return type(
        "IdentityProbe",
        (models.Model,),
        {
            "__module__": __name__,
            "identity": field,
            "Meta": type("Meta", (), {"app_label": "codec", "db_table": "rebac_codec_probe"}),
        },
    )


def test_codec_refuses_custom_encoded_identity():
    from tests.testapp.models import EncodedPost

    with pytest.raises(SchemaError, match=r"rebac\.E014.*custom field conversions"):
        identity_codec(EncodedPost)


@pytest.mark.parametrize("field_class", INTEGER_FIELDS)
@isolate_apps()
def test_integer_codec_canonical_and_bounds(field_class):
    field = (
        field_class(primary_key=True)
        if issubclass(field_class, models.AutoField)
        else field_class()
    )
    codec = identity_codec(_identity_model(field), "identity")
    low, high = connection.ops.integer_field_range(field.get_internal_type())
    assert low is not None and high is not None
    for value in (low, 0, 1, high):
        assert codec.wire(value) == str(value)
        assert codec.is_canonical(str(value))
    for value in (
        "01",
        "-0",
        "+1",
        "1.0",
        "1e0",
        " 1",
        "1 ",
        "1\n",
        "\u0661",  # Non-ASCII digits must never coerce to integers.
        "",
        "*",
        str(low - 1),
        str(high + 1),
        "9" * 65,
    ):
        assert not codec.is_canonical(value)
        with pytest.raises(SchemaError):
            codec.wire(value)
    with pytest.raises(SchemaError):
        codec.wire(True)


@pytest.mark.parametrize(
    "field", [models.CharField(max_length=64), models.TextField(), models.SlugField(max_length=64)]
)
@isolate_apps()
def test_text_codec(field):
    codec = identity_codec(_identity_model(field), "identity")
    for text in ("abc", "01", "ü", "x" * 64):
        assert codec.wire(text) == text
        assert codec.is_canonical(text)
    for text in ("", "*", "x" * 65):
        assert not codec.is_canonical(text)
    with pytest.raises(SchemaError):
        codec.wire(None)


@isolate_apps()
def test_uuid_codec_wire_form():
    codec = identity_codec(_identity_model(models.UUIDField()), "identity")
    value = UUID("aabbccdd-1234-5678-90ab-1234567890ab")
    assert codec.wire(value) == str(value)
    for bad in (
        value.hex,
        str(value).upper(),
        "{" + str(value) + "}",
        str(value) + "\n",
        "*",
        "not-a-uuid",
    ):
        assert not codec.is_canonical(bad)
    assert codec.is_canonical(str(value))


@pytest.mark.parametrize(
    "field",
    [
        models.BooleanField(),
        models.DateField(),
        models.FloatField(),
        models.JSONField(),
        models.BinaryField(),
    ],
)
@isolate_apps()
def test_unsupported_codecs_report_e014(field):
    with pytest.raises(SchemaError, match=r"rebac\.E014.*IdentityProbe\.identity"):
        identity_codec(_identity_model(field), "identity")


def _exercise_codec_sql(db):
    using = db.alias
    for factory in (
        *INTEGER_FIELDS,
        models.CharField,
        models.TextField,
        models.SlugField,
        models.UUIDField,
    ):
        with isolate_apps():
            if issubclass(factory, models.AutoField):
                field = factory(primary_key=True)
            elif issubclass(factory, models.CharField):
                field = factory(max_length=64, null=True)
            else:
                field = factory(null=True)
            model = _identity_model(field)
            codec = identity_codec(model, "identity")
            is_uuid = isinstance(field, models.UUIDField)
            is_integer = isinstance(field, models.IntegerField)
            value = (
                UUID("aabbccdd-1234-5678-90ab-1234567890ab")
                if is_uuid
                else 1
                if is_integer
                else "001"
            )
            with db.schema_editor() as editor:
                editor.create_model(model)
            try:
                model.objects.using(using).create(identity=value)
                rows = model.objects.using(using).annotate(wire=codec.to_wire("identity"))
                assert rows.get().wire == str(value)
                candidates = [str(value), "*", "", "x" * 65]
                if is_integer:
                    candidates += ["01", "-0", "+1", "1\n", "no", "9" * 64, "-" + "9" * 63]
                if is_uuid:
                    candidates += [value.hex, str(value).upper(), str(value) + "\n", "invalid"]
                for candidate in candidates:
                    converted = codec.to_column(Value(candidate))
                    matches = model.objects.using(using).filter(identity=converted).count()
                    assert matches == int(candidate == str(value)), (factory, candidate)
                    if not codec.is_canonical(candidate, using=using):
                        assert (
                            model.objects.using(using)
                            .annotate(converted=converted)
                            .values_list("converted", flat=True)
                            .get()
                            is None
                        )
                if not field.primary_key:
                    empty = model.objects.using(using).create(identity=None)
                    assert rows.get(pk=empty.pk).wire is None
                # A subquery of stored identities also holds the wildcard id.
                stored_type = "codec/" + field.get_internal_type()
                RebacResource.objects.using(using).bulk_create(
                    [
                        RebacResource(resource_type=stored_type, resource_id=str(value)),
                        RebacResource(resource_type=stored_type, resource_id="*"),
                    ]
                )
                ids = (
                    RebacResource.objects.using(using)
                    .filter(resource_type=stored_type)
                    .annotate(column=codec.to_column("resource_id"))
                    .values("column")
                )
                assert model.objects.using(using).filter(identity__in=ids).count() == 1
            finally:
                with db.schema_editor() as editor:
                    editor.delete_model(model)


@pytest.mark.django_db(transaction=True)
def test_codec_sql():
    _exercise_codec_sql(connection)


def _exercise_projection(db, storage):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        model = active_relationship_model()
        now = application_now() + timedelta(hours=1)
        common = {
            "relation": "viewer",
            "optional_subject_relation": "member",
            "caveat_name": "window",
            "caveat_context": {"enabled": True},
            "expires_at": now,
        }
        if storage == "registry":
            resource = RebacResource.objects.using(db.alias).create(
                resource_type="storage/projection", resource_id="one"
            )
            subject = RebacResource.objects.using(db.alias).create(
                resource_type="auth/group", resource_id="team"
            )
            model.objects.using(db.alias).create(resource_fk=resource, subject_fk=subject, **common)
        else:
            model.objects.using(db.alias).create(
                resource_type="storage/projection",
                resource_id="one",
                subject_type="auth/group",
                subject_id="team",
                **common,
            )
        with CaptureQueriesContext(db) as queries:
            projection = (
                model.objects.using(db.alias)
                .filter(resource_type="storage/projection")
                .wire_projection()
            )
        assert len(queries) == 0
        assert projection.db == db.alias
        assert tuple(projection.query.selected) == WIRE_PROJECTION_FIELDS
        assert list(
            projection.annotate(copied=F("resource_id")).values_list("copied", flat=True)
        ) == ["one"]
        assert list(projection) == [
            {
                "resource_type": "storage/projection",
                "resource_id": "one",
                "relation": "viewer",
                "subject_type": "auth/group",
                "subject_id": "team",
                "subject_relation": "member",
                "caveat_name": "window",
                "caveat_context": {"enabled": True},
                "expires_at": now,
            }
        ]


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.django_db
def test_relationship_index_projection(storage):
    _exercise_projection(connection, storage)


@pytest.mark.schema_vendors
@pytest.mark.skipif(
    os.environ.get("REBAC_TEST_SCHEMA_VENDORS") != "1", reason="opt-in vendor suite"
)
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_codec_vendor_contract(vendor_connection, storage):  # noqa: F811 — pytest fixture
    from django.core.management import call_command

    db = vendor_connection
    call_command("migrate", database=db.alias, verbosity=0)
    _exercise_codec_sql(db)
    _exercise_projection(db, storage)
