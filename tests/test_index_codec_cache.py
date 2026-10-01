"""The identity conversion's compiled wrapper is cached; its SQL must not change."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.db import connection, models
from django.db.models import F, OuterRef, Subquery, Value
from django.db.models.functions import Concat
from django.test.utils import isolate_apps

from rebac.index import codec as codec_module
from rebac.index.codec import _Conversion, _IdentityCodec, identity_codec
from tests.testapp.models import Folder, Post, TextIdentityFolder

pytestmark = pytest.mark.pg_delta  # the compiled SQL is vendor-specific

TEXT = models.TextField()


def _compiled(queryset):
    return queryset.query.get_compiler(connection.alias).as_sql()


def _fresh(queryset):
    with patch.object(_Conversion, "as_sql", _Conversion._compile_fresh):
        return _compiled(queryset)


def _inners(attr):
    return {
        "column": attr,
        "literal": Value("42", output_field=TEXT),
        "percent literal": Value("100%s", output_field=TEXT),
        "expression with parameters": Concat(
            Value("a", output_field=TEXT),
            F(attr),
            Value("%", output_field=TEXT),
            output_field=TEXT,
        ),
    }


@pytest.fixture(autouse=True)
def empty_cache():
    codec_module._fragments.clear()
    yield
    codec_module._fragments.clear()


@pytest.mark.parametrize(
    "inner", ["column", "literal", "percent literal", "expression with parameters"]
)
@pytest.mark.parametrize("to_wire", [True, False])
@pytest.mark.parametrize(
    ("model", "attr"), [(Folder, "pk"), (TextIdentityFolder, "public_id")], ids=["integer", "text"]
)
def test_cached_conversion_compiles_to_the_same_sql(model, attr, to_wire, inner):
    codec = identity_codec(model, attr)
    convert = codec.to_wire if to_wire else codec.to_column
    name = "id" if attr == "pk" else attr
    queryset = model._base_manager.annotate(converted=convert(_inners(name)[inner])).values(
        "converted"
    )

    fresh = _fresh(queryset)
    first = _compiled(queryset)
    again = _compiled(queryset)

    assert first == fresh
    assert again == fresh
    assert len(codec_module._fragments) == 1


@pytest.mark.parametrize("to_wire", [True, False])
@pytest.mark.parametrize("native", [True, False])
def test_cached_uuid_conversion_compiles_to_the_same_sql(to_wire, native):
    with isolate_apps("tests.testapp"):

        class Token(models.Model):
            uid = models.UUIDField(unique=True)

            class Meta:
                app_label = "testapp"

        codec = _IdentityCodec(Token._meta.get_field("uid"), "uuid")
        convert = codec.to_wire if to_wire else codec.to_column
        queryset = Token._base_manager.annotate(converted=convert("uid")).values("converted")
        with patch.object(connection.features, "has_native_uuid_field", native):
            fresh = _fresh(queryset)
            assert _compiled(queryset) == fresh
            assert _compiled(queryset) == fresh


def test_conversion_inside_a_subquery_compiles_to_the_same_sql():
    codec = identity_codec(Folder, "pk")
    posts = Post._base_manager.annotate(wire=codec.to_wire(OuterRef("pk"))).values("wire")[:1]
    queryset = Folder._base_manager.annotate(wire=Subquery(posts)).values("wire")

    fresh = _fresh(queryset)

    assert _compiled(queryset) == fresh
    assert _compiled(queryset) == fresh


def test_cache_is_keyed_by_field_direction_and_connection():
    integer = identity_codec(Folder, "pk")
    text = identity_codec(TextIdentityFolder, "public_id")
    for queryset in (
        Folder._base_manager.annotate(x=integer.to_wire("id")).values("x"),
        Folder._base_manager.annotate(x=integer.to_column(Value("1", output_field=TEXT))).values(
            "x"
        ),
        TextIdentityFolder._base_manager.annotate(x=text.to_wire("public_id")).values("x"),
    ):
        _compiled(queryset)
        _compiled(queryset)

    assert len(codec_module._fragments) == 3


@pytest.mark.django_db
def test_cached_conversion_returns_the_same_rows():
    Folder._base_manager.bulk_create([Folder(name=str(n)) for n in range(3)])
    codec = identity_codec(Folder, "pk")
    queryset = Folder._base_manager.annotate(
        wire=codec.to_wire("id"), back=codec.to_column(codec.to_wire("id"))
    ).order_by("pk")

    with patch.object(_Conversion, "as_sql", _Conversion._compile_fresh):
        fresh = list(queryset.values_list("pk", "wire", "back"))
    cached = list(queryset.values_list("pk", "wire", "back"))

    assert cached == fresh
    assert all(wire == str(pk) and back == pk for pk, wire, back in cached)
