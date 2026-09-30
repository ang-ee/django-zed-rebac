"""Storage contracts; vendor cases opt in with REBAC_TEST_SCHEMA_VENDORS=1."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import UUID

import pytest
from django.db import IntegrityError, connection, connections, models, transaction
from django.db.models import Exists, F, OuterRef, Value
from django.db.utils import ConnectionHandler
from django.test import override_settings
from django.test.utils import CaptureQueriesContext, isolate_apps

from rebac.errors import SchemaError
from rebac.index import terms
from rebac.index import time as index_time
from rebac.index.codec import identity_codec
from rebac.index.project import select
from rebac.index.write import stream_create
from rebac.models.index import (
    IndexCover,
    IndexEdge,
    IndexMember,
    IndexState,
    IndexTerm,
    IndexWork,
)
from rebac.models.relationship import INDEX_PROJECTION_FIELDS, active_relationship_model
from rebac.models.resource import RebacResource

# Reuse the existing disposable vendor fixture and its opt-in convention.
from .test_schema_generation_vendors import vendor_connection, vendor_database  # noqa: F401

FIELDS = ("type", "object_id", "relation")


def _source(*, using="default", type_="storage/object"):
    return IndexTerm.objects.using(using).filter(type=type_).values(*FIELDS)


def _exercise_insert(db):
    using = db.alias
    triples = [("storage/object", "one", ""), ("storage/object", "two", "")]
    original_ids = terms.intern(triples, using=using)
    source = _source(using=using).order_by("-pk")
    assert stream_create(source, IndexTerm, FIELDS, using=using, ignore_conflicts=True) == 2
    assert terms.term_ids(triples, using=using) == original_ids
    assert IndexTerm.objects.using(using).filter(type="storage/object").count() == 2
    with pytest.raises(IntegrityError), transaction.atomic(using=using):
        stream_create(source, IndexTerm, FIELDS, using=using)
    with CaptureQueriesContext(db) as queries:
        assert stream_create(source.none(), IndexTerm, FIELDS, using=using) == 0
    assert len(queries) == 0

    remapped = select(
        source, type=Value("storage/copy"), object_id=F("object_id"), relation=F("relation")
    )
    assert stream_create(remapped, IndexTerm, FIELDS, using=using) == 2
    assert set(
        IndexTerm.objects.using(using)
        .filter(type="storage/copy")
        .values_list("object_id", flat=True)
    ) == {"one", "two"}
    assert source.query.order_by == ("-pk",)
    distinct = select(
        source, type=Value("storage/distinct"), object_id=Value("only"), relation=F("relation")
    ).distinct()
    assert stream_create(distinct, IndexTerm, FIELDS, using=using) == 1

    auto = select(source, type=Value("storage/auto"), object_id=F("object_id"))
    assert stream_create(auto, IndexTerm, ("type", "object_id"), using=using) == 2
    copied = IndexTerm.objects.using(using).filter(type="storage/auto")
    assert set(copied.values_list("relation", flat=True)) == {""}
    assert set(copied.values_list("pk", flat=True)).isdisjoint(original_ids.values())

    existing = IndexTerm.objects.using(using).filter(
        type="storage/new", object_id=OuterRef("object_id"), relation=OuterRef("relation")
    )
    delta = select(
        source.filter(~Exists(existing)),
        type=Value("storage/new"),
        object_id=F("object_id"),
        relation=F("relation"),
    )
    assert stream_create(delta, IndexTerm, FIELDS, using=using, batch_size=1) == 2
    assert stream_create(delta, IndexTerm, FIELDS, using=using, batch_size=1) == 0
    assert IndexTerm.objects.using(using).filter(type="storage/new").count() == 2


@pytest.mark.django_db
def test_stream_create_contract():
    _exercise_insert(connection)


@pytest.mark.django_db(transaction=True)
def test_stream_create_late_batch_failure_rolls_back_earlier_batches():
    with pytest.raises(IntegrityError):
        stream_create(
            [(71, "scope", "new"), (None, "scope", "new")],
            IndexWork,
            ("pass_id", "kind", "phase"),
            using="default",
            batch_size=1,
        )
    assert not IndexWork.objects.filter(pass_id=71).exists()


@pytest.mark.django_db
def test_grant_site_rows_are_separate():
    ids = terms.intern([("storage/load", "one", ""), ("auth/user", "one", "")], using="default")
    scope, holder = ids["storage/load", "one", ""], ids["auth/user", "one", ""]
    for index in range(12):
        IndexCover.objects.create(
            scope_id=scope,
            holder_id=holder,
            resource_type="storage/load",
            node=f"read{index}",
            site="read.1",
        )
    rows = IndexCover.objects.filter(resource_type="storage/load", site__gt="")
    with CaptureQueriesContext(connection) as captured:
        values = list(rows.values_list("node", "site", "holder__type", "holder__object_id"))
    assert len(captured) == 1
    assert len(values) == 12
    assert all(value[1:] == ("read.1", "auth/user", "one") for value in values)


@pytest.mark.django_db
def test_stream_create_required_fields_and_sql_null_defaults():
    ids = terms.intern([("storage/object", "one", ""), ("auth/user", "1", "")], using="default")
    resource = ids["storage/object", "one", ""]
    holder = ids["auth/user", "1", ""]
    source = select(
        _source(),
        resource_id=Value(resource),
        resource_type=F("type"),
        relation=F("relation"),
        subject_id=Value(holder),
        target_id=Value(holder),
        source=Value("tuple"),
    )
    assert (
        stream_create(
            source,
            IndexEdge,
            (
                "resource_id",
                "resource_type",
                "relation",
                "subject_id",
                "target_id",
                "source",
            ),
            using="default",
        )
        == 1
    )
    edge = IndexEdge.objects.get()
    assert edge.condition is None
    assert IndexEdge.objects.unconditional().count() == 1
    assert edge.expires_at == index_time.TIME_MAX
    missing = select(_source(), kind=Value("scope"), node=Value(""), phase=Value("new"))
    with pytest.raises(IntegrityError), transaction.atomic():
        stream_create(missing, IndexWork, ("kind", "node", "phase"), using="default")
    assert not IndexWork.objects.exists()
    with pytest.raises(ValueError, match="zip"):
        stream_create([("too", "many")], IndexTerm, ("type",), using="default")


@pytest.mark.django_db(transaction=True)
def test_stream_create_alias_routing(tmp_path):
    handler = ConnectionHandler(
        {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": str(tmp_path / "index.sqlite3"),
            }
        }
    )
    alias = "index_storage_alias"
    db = handler["default"]
    db.alias = alias
    connections[alias] = db
    try:
        with db.schema_editor() as editor:
            editor.create_model(IndexTerm)
        terms.intern([("storage/object", "default-only", "")], using="default")
        terms.intern([("storage/object", "alias-only", "")], using=alias)
        source = select(
            _source(),
            type=Value("storage/copied"),
            object_id=F("object_id"),
            relation=F("relation"),
        )
        with (
            CaptureQueriesContext(connection) as default_queries,
            CaptureQueriesContext(db) as alias_queries,
        ):
            assert stream_create(source, IndexTerm, FIELDS, using=alias) == 1
        assert len(default_queries) == 0
        assert any(item["sql"].lstrip().upper().startswith("SELECT") for item in alias_queries)
        assert any(item["sql"].lstrip().upper().startswith("INSERT") for item in alias_queries)
        assert IndexTerm.objects.using(alias).get(type="storage/copied").object_id == "alias-only"
        assert not IndexTerm.objects.filter(type="storage/copied").exists()
    finally:
        db.close()
        del connections[alias]


def _exercise_intern(using):
    before = IndexTerm.objects.using(using).create(type="storage/intern", object_id="existing")
    triples = [("storage/intern", "existing", ""), ("storage/intern", "new", "member")]
    result = terms.intern(reversed(triples + triples), using=using)
    assert result[triples[0]] == before.pk
    assert terms.term_ids(triples, using=using) == result
    assert terms.intern(triples, using=using) == result
    assert terms.term_ids([("storage/missing", "1", "")], using=using) == {}
    assert terms.intern([], using=using) == {}
    projected = (
        RebacResource.objects.using(using)
        .filter(resource_type="storage/intern-projection")
        .annotate(type=F("resource_type"), object_id=F("resource_id"), relation=Value(""))
        .values(*FIELDS)
    )
    RebacResource.objects.using(using).create(
        resource_type="storage/intern-projection", resource_id="one"
    )
    assert terms.intern_from(projected, using=using) == 1
    first = terms.term_ids([("storage/intern-projection", "one", "")], using=using)
    assert len(first) == 1
    assert terms.intern_from(projected, using=using) == 1
    assert terms.term_ids([("storage/intern-projection", "one", "")], using=using) == first
    assert IndexTerm.objects.using(using).filter(type="storage/intern-projection").count() == 1


@pytest.mark.django_db
def test_intern_handles_preexisting_racing_inserts():
    _exercise_intern("default")


@pytest.mark.django_db
def test_registry_interning_batches_and_recovers_every_identity(monkeypatch):
    original = models.QuerySet.bulk_create
    sizes = []

    def record(queryset, objects, **kwargs):
        if queryset.model is RebacResource:
            sizes.append(len(objects))
            assert kwargs["update_conflicts"] is True
            assert kwargs["batch_size"] <= 200
        return original(queryset, objects, **kwargs)

    monkeypatch.setattr(models.QuerySet, "bulk_create", record)
    pairs = [("storage/registry", str(i)) for i in range(450)]
    first = RebacResource.upsert_refs_bulk(pairs + pairs)
    assert len(first) == 450
    assert sizes == [200, 200, 50]
    assert RebacResource.upsert_refs_bulk(list(reversed(pairs))) == first
    assert RebacResource.objects.filter(resource_type="storage/registry").count() == 450


@pytest.mark.django_db(transaction=True)
def test_registry_interning_missing_result_rolls_back(monkeypatch):
    original = models.QuerySet.values_list
    original_create = models.QuerySet.bulk_create

    def missing(queryset, *fields, **kwargs):
        if queryset.model is RebacResource:
            queryset = queryset.none()
        return original(queryset, *fields, **kwargs)

    monkeypatch.setattr(models.QuerySet, "values_list", missing)

    def without_returned_keys(queryset, objects, **kwargs):
        result = original_create(queryset, objects, **kwargs)
        if queryset.model is RebacResource:
            for row in objects:
                row.pk = None
        return result

    monkeypatch.setattr(models.QuerySet, "bulk_create", without_returned_keys)
    with pytest.raises(SchemaError, match="failed to retrieve"):
        RebacResource.upsert_refs_bulk([("storage/missing-registry", "one")])
    assert not RebacResource.objects.filter(resource_type="storage/missing-registry").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("model", [IndexEdge, IndexMember, IndexCover])
def test_expiry_upsert_keeps_max_across_batches_and_preserves_delta_tags(model, monkeypatch):
    from django.db.models import Case, When

    from rebac.index import project

    monkeypatch.setattr(project, "BATCH_SIZE", 1)
    ids = terms.intern([("storage/growth", str(i), "") for i in range(3)], using="default")
    scope, holder = ids["storage/growth", "0", ""], ids["storage/growth", "1", ""]
    now = index_time.index_now()
    long = now + timedelta(hours=2)
    short = now + timedelta(hours=1)
    fields = dict(
        expires_at=Case(When(object_id="0", then=Value(long)), default=Value(short)),
        condition=project.formula_value(None),
        condition_key=Value(""),
    )
    if model is IndexEdge:
        fields.update(
            resource_id=Value(scope),
            resource_type=Value("storage/growth"),
            relation=Value("viewer"),
            subject_id=Value(holder),
            target_id=Value(holder),
            source=Value("tuple"),
        )
    elif model is IndexMember:
        fields.update(
            set_id=Value(scope),
            member_id=Value(holder),
            member_type=Value("storage/growth"),
            pass_id=Value(1),
            round=Value(0),
        )
    else:
        fields.update(
            scope_id=Value(scope),
            resource_type=Value("storage/growth"),
            node=Value("read"),
            holder_id=Value(holder),
            site=Value(""),
            pass_id=Value(1),
            round=Value(0),
        )
    key = tuple(model._meta.get_field(name).attname for name in model._meta.constraints[0].fields)
    source = select(IndexTerm.objects.filter(type="storage/growth"), **fields)
    stats = project.Stats()
    assert (
        project.upsert(source, model, key, using="default", stats=stats, pass_id=1, round_=0) >= 1
    )
    row = model.objects.get()
    assert row.expires_at == long
    assert stats.inserted == 1
    assert stats.python_rows >= 3
    assert (
        project.upsert(source, model, key, using="default", stats=stats, pass_id=2, round_=1) == 0
    )
    row.refresh_from_db()
    assert row.expires_at == long
    if model is not IndexEdge:
        assert (row.pass_id, row.round) == (1, 0)
    fields["expires_at"] = Value(long + timedelta(hours=1))
    grown = select(IndexTerm.objects.filter(pk=scope), **fields)
    assert project.upsert(grown, model, key, using="default", stats=stats, pass_id=3, round_=2) == 1
    row.refresh_from_db()
    assert row.expires_at == long + timedelta(hours=1)
    if model is not IndexEdge:
        assert (row.pass_id, row.round) == (3, 2)


@pytest.mark.django_db
def test_work_and_exception_constraint_policy_matches_migration():

    # Constraint policy is intentionally unchanged in both models and migration.
    migration = import_module("rebac.migrations.0007_permission_index")
    for model in (IndexWork,):
        operation = next(
            op
            for op in migration.Migration.operations
            if getattr(op, "name", None) == model.__name__
        )
        assert model._meta.constraints == []
        assert operation.options.get("constraints", []) == []


@pytest.mark.django_db
def test_intern_batches_in_sorted_order(monkeypatch):
    from rebac.models.index import IndexQuerySet

    original = IndexQuerySet.bulk_create
    seen = []

    def record(queryset, objects, **kwargs):
        seen.extend((obj.type, obj.object_id, obj.relation) for obj in objects)
        assert kwargs["batch_size"] == 200
        return original(queryset, objects, **kwargs)

    monkeypatch.setattr(IndexQuerySet, "bulk_create", record)
    triples = [("storage/batch", str(i), "") for i in range(450)]
    assert len(terms.intern(reversed(triples + triples), using="default")) == 450
    assert seen == sorted(triples)


@pytest.mark.django_db(transaction=True)
def test_intern_missing_rows_fail_and_roll_back(monkeypatch):
    from rebac.models.index import IndexQuerySet

    original = IndexQuerySet.values_list
    original_create = IndexQuerySet.bulk_create

    def missing(queryset, *fields, **kwargs):
        return original(queryset.none(), *fields, **kwargs)

    monkeypatch.setattr(IndexQuerySet, "values_list", missing)

    def without_returned_keys(queryset, objects, **kwargs):
        result = original_create(queryset, objects, **kwargs)
        for row in objects:
            row.pk = None
        return result

    monkeypatch.setattr(IndexQuerySet, "bulk_create", without_returned_keys)
    with pytest.raises(SchemaError, match="failed to retrieve"):
        terms.intern([("storage/missing", "one", "")], using="default")
    assert not IndexTerm.objects.filter(type="storage/missing").exists()


@pytest.mark.django_db
def test_intern_from_missing_rows_fail(monkeypatch):
    RebacResource.objects.create(resource_type="storage/absent", resource_id="one")
    source = RebacResource.objects.annotate(
        type=F("resource_type"), object_id=F("resource_id"), relation=Value("")
    ).values(*FIELDS)
    from rebac.models.index import IndexQuerySet

    monkeypatch.setattr(IndexQuerySet, "bulk_create", lambda *args, **kwargs: [])
    with pytest.raises(SchemaError, match="failed to retrieve"):
        terms.intern_from(source, using="default")


@pytest.mark.django_db(transaction=True)
def test_intern_from_rejects_overlong_source_without_truncation():
    RebacResource.objects.create(resource_type="storage/source", resource_id="one")
    source = RebacResource.objects.annotate(
        type=F("resource_type"), object_id=Value("x" * 65), relation=Value("")
    ).values(*FIELDS)
    with pytest.raises(SchemaError, match="64 characters"):
        terms.intern_from(source, using="default")
    assert not IndexTerm.objects.filter(type="storage/source").exists()


@pytest.mark.parametrize("bad", [("", "1", ""), ("t", "x" * 65, "")])
def test_intern_rejects_malformed_terms_before_insert(bad):
    with pytest.raises(SchemaError):
        terms.intern([bad], using="default")


def _validate_tuple(*, resource_id="ordinary", expires_at=None):
    from rebac.backends.local import LocalBackend
    from rebac.schema import parse_zed
    from rebac.types import ObjectRef, RelationshipTuple, SubjectRef

    schema = parse_zed("""
        use expiration
        definition auth/user {}
        definition storage/object { relation viewer: auth/user with expiration }
    """)
    LocalBackend()._validate_relationship_tuple(
        RelationshipTuple(
            ObjectRef("storage/object", resource_id),
            "viewer",
            SubjectRef.of("auth/user", "1"),
            expires_at=expires_at,
        ),
        schema=schema,
    )


@pytest.mark.parametrize("use_tz", [True, False])
def test_sentinels_and_validation(settings, use_tz):
    settings.USE_TZ = use_tz
    zone = UTC if use_tz else None
    assert index_time.TIME_MIN == datetime(1000, 1, 2, tzinfo=zone)
    assert index_time.TIME_MAX == datetime(9999, 12, 30, tzinfo=zone)
    assert (index_time.index_now().tzinfo is not None) is use_tz
    _validate_tuple(expires_at=None)
    _validate_tuple(expires_at=index_time.TIME_MIN + timedelta(microseconds=1))
    _validate_tuple(expires_at=index_time.TIME_MAX - timedelta(microseconds=1))
    for value in (
        index_time.TIME_MIN,
        index_time.TIME_MAX,
        index_time.TIME_MIN - timedelta(days=1),
        index_time.TIME_MAX + timedelta(days=1),
    ):
        with pytest.raises(ValueError):
            _validate_tuple(expires_at=value)
    with pytest.raises(ValueError, match="USE_TZ"):
        _validate_tuple(expires_at=datetime(2026, 1, 1, tzinfo=None if use_tz else UTC))
    assert IndexMember().expires_at == index_time.TIME_MAX


def test_reserved_terms(settings):
    settings.REBAC_ANONYMOUS_TYPE = "guest/anonymous"
    settings.REBAC_TYPE_PREFIX = "tenant/"
    assert terms.anonymous() == ("tenant/guest/anonymous", "*", "$anonymous")
    assert terms.anonymous() != terms.wildcard("tenant/guest/anonymous")
    assert terms.AUTHENTICATED == ("$authenticated", "*", "")
    assert terms.wildcard("t") == ("t", "*", "")
    assert terms.type_level("t") == ("t", "*", "$type")
    assert terms.type_level("t") != terms.wildcard("t")
    _validate_tuple(resource_id="ordinary")
    with pytest.raises(ValueError, match="resource IDs"):
        _validate_tuple(resource_id="*")


@pytest.mark.django_db
def test_internal_empty_actor_term_does_not_allow_empty_source_identity():
    triple = ("auth/user", "", "")
    ids = terms.intern([triple], using="default")
    assert IndexTerm.objects.get(pk=ids[triple]).object_id == ""
    with pytest.raises(ValueError, match="cannot be empty"):
        _validate_tuple(resource_id="")
    RebacResource.objects.create(resource_type="storage/source", resource_id="one")
    source = RebacResource.objects.annotate(
        type=F("resource_type"), object_id=Value(""), relation=Value("")
    ).values(*FIELDS)
    with pytest.raises(SchemaError, match="nonempty"):
        terms.intern_from(source, using="default")


def test_codec_refuses_custom_encoded_identity():
    from tests.testapp.models import EncodedPost

    with pytest.raises(SchemaError, match=r"rebac\.E014.*custom field conversions"):
        identity_codec(EncodedPost)


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
        "StorageIdentity",
        (models.Model,),
        {
            "__module__": __name__,
            "identity": field,
            "Meta": type(
                "Meta", (), {"app_label": "index_storage", "db_table": "rebac_storage_codec_probe"}
            ),
        },
    )


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
    with pytest.raises(SchemaError, match=r"rebac\.E014.*StorageIdentity\.identity"):
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
                # The actual term-subquery shape includes the synthetic scope.
                term_type = "storage/codec/" + field.get_internal_type()
                IndexTerm.objects.using(using).bulk_create(
                    [
                        IndexTerm(type=term_type, object_id=str(value)),
                        IndexTerm(type=term_type, object_id="*", relation="$type"),
                    ]
                )
                ids = (
                    IndexTerm.objects.using(using)
                    .filter(type=term_type)
                    .annotate(column=codec.to_column("object_id"))
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
        now = index_time.index_now() + timedelta(hours=1)
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
                .index_projection()
            )
        assert len(queries) == 0
        assert projection.db == db.alias
        assert tuple(projection.query.selected) == INDEX_PROJECTION_FIELDS
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


@pytest.mark.django_db
def test_queryset_predicates_and_constraints():
    ids = terms.intern([("storage/object", "one", ""), ("auth/user", "1", "")], using="default")
    scope = ids["storage/object", "one", ""]
    holder = ids["auth/user", "1", ""]
    now = index_time.index_now()
    cover = IndexCover.objects.create(
        scope_id=scope,
        holder_id=holder,
        resource_type="storage/object",
        node="read",
        site="",
    )
    assert (
        IndexCover.objects.using("default")
        .active(now)
        .unconditional()
        .filter(scope_id__in=[scope], resource_type="storage/object", node="read")
        .get()
        == cover
    )
    assert IndexCover.objects.active(now - timedelta(microseconds=1)).exists()
    IndexCover.objects.filter(pk=cover.pk).update(expires_at=now + timedelta(hours=1))
    assert not IndexCover.objects.active(now + timedelta(hours=1)).exists()
    with pytest.raises(IntegrityError), transaction.atomic():
        IndexCover.objects.create(
            scope_id=scope,
            holder_id=holder,
            resource_type="storage/object",
            node="read",
            site="",
        )
    IndexCover.objects.create(
        scope_id=scope,
        holder_id=holder,
        resource_type="storage/object",
        node="read",
        site="read.1",
    )
    for model in (
        IndexTerm,
        IndexEdge,
        IndexMember,
        IndexCover,
        IndexWork,
        IndexState,
    ):
        assert model._meta.default_permissions == ()


@pytest.mark.django_db
def test_migration_global_row_forward_and_reverse():
    from django.apps import apps

    migration = import_module("rebac.migrations.0007_permission_index")
    editor = connection.schema_editor()
    migration.create_global_state(apps, editor)
    migration.create_global_state(apps, editor)
    assert IndexState.objects.filter(pk="global").count() == 1
    migration.remove_global_state(apps, editor)
    assert not IndexState.objects.filter(pk="global").exists()


@pytest.mark.django_db
def test_work_lookup_index_matches_initial_migration():
    from rebac import backend
    from rebac.index.rebuild import rebuild
    from rebac.schema import parse_zed
    from tests.index_harness import assert_no_drift

    migration = import_module("rebac.migrations.0007_permission_index")
    operation = next(
        op for op in migration.Migration.operations if getattr(op, "name", None) == "IndexWork"
    )
    expected = ["pass_id", "phase", "kind"]
    assert any(index.fields == expected for index in IndexWork._meta.indexes)
    assert any(index.fields == expected for index in operation.options["indexes"])
    with connection.cursor() as cursor:
        indexes = connection.introspection.get_constraints(cursor, IndexWork._meta.db_table)
    assert indexes["rebac_work_pass_phase_idx"]["columns"] == expected
    backend().set_schema(parse_zed("definition auth/user {}"))
    rebuild(using="default")
    assert_no_drift()


@pytest.mark.schema_vendors
@pytest.mark.skipif(
    os.environ.get("REBAC_TEST_SCHEMA_VENDORS") != "1", reason="opt-in vendor suite"
)
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_storage_vendor_contract(vendor_connection, storage):  # noqa: F811 — pytest fixture
    from django.core.management import call_command

    db = vendor_connection
    call_command("migrate", database=db.alias, verbosity=0)
    assert IndexState.objects.using(db.alias).filter(pk="global").exists()
    _exercise_insert(db)
    _exercise_intern(db.alias)
    _exercise_codec_sql(db)
    _exercise_projection(db, storage)
    for use_tz in (True, False):
        with override_settings(USE_TZ=use_tz):
            term = IndexTerm.objects.using(db.alias).first()
            row = IndexMember.objects.using(db.alias).create(
                member=term, set=term, member_type=term.type
            )
            row.refresh_from_db(using=db.alias)
            assert row.expires_at == index_time.TIME_MAX
            row.delete(using=db.alias)
