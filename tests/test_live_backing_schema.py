"""Live backing declarations keep one validated schema/DB representation."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from rebac.field_backing import ResolvedAttributeBacking
from rebac.schema import (
    AttributeBinding,
    FieldBinding,
    ParseError,
    parse_zed,
    relation_is_writable,
    render_zed,
)
from rebac.schema.ast import backing_from_dict, backing_to_dict
from rebac.schema.introspection import named_object_refs
from rebac.types import ObjectRef


@pytest.mark.parametrize(
    "directive",
    [
        "field=members",
        'field={"path":"roster__user","filters":{"roster__active":true,"roster__role":"editor"}}',
        'attribute={"field":"kind"}',
        'attribute={"field":"is_active","resource":"inactive","value":false}',
        'attribute={"field":"kind","resource":"unknown","value":null}',
    ],
)
def test_backing_round_trip_preserves_schema_and_persistence(directive: str) -> None:
    schema = parse_zed(
        "definition auth/user {}\n"
        "definition sample/container {\n"
        f" relation member: auth/user // rebac:{directive}\n"
        " permission read = member\n"
        "}\n"
    )
    definition = schema.get_definition("sample/container")
    assert definition is not None
    backing = definition.relations[0].backing
    assert backing_from_dict(backing_to_dict(backing)) == backing
    reparsed = parse_zed(render_zed(schema)).get_definition("sample/container")
    assert reparsed is not None
    assert reparsed.relations[0].backing == backing
    assert "rebac:" not in render_zed(schema, include_backing=False)


@pytest.mark.parametrize(
    "directive",
    [
        "field=members.invalid",
        'field={"path":"members","unexpected":true}',
        'field={"path":"members","filters":{"active":[true]}}',
        'attribute={"field":"kind","resource":"person"}',
        'attribute={"field":"kind","value":"person"}',
        'attribute={"field":"kind","resource":"*","value":"person"}',
        'attribute={"field":"kind","resource":"person","value":[]}',
        'attribute={"field":"kind","filters":{"active":NaN}}',
        'attribute={"field":"kind","filters":[]}',
        'attribute={"field":"kind","kind":"fk"}',
        'attribute={"field":"kind"',
    ],
)
def test_invalid_live_backing_fails_at_parse_boundary(directive: str) -> None:
    with pytest.raises(ParseError):
        parse_zed(
            "definition sample/container {\n"
            f" relation member: auth/user // rebac:{directive}\n"
            "}\n"
        )


def test_similarly_prefixed_comment_is_not_a_backing_directive() -> None:
    schema = parse_zed(
        "definition auth/user {}\n"
        "definition sample/container {\n"
        " relation member: auth/user // rebac:fieldwork=ordinary-comment\n"
        "}\n"
    )

    definition = schema.get_definition("sample/container")
    assert definition is not None
    assert definition.relations[0].backing is None


def test_fixed_attribute_names_its_container_without_tuple_evidence() -> None:
    schema = parse_zed(
        "definition auth/user {}\n"
        "definition sample/role {\n"
        ' relation member: auth/user // rebac:attribute={"field":"is_staff","resource":"admin","value":true}\n'
        "}\n"
    )
    assert named_object_refs(schema) == frozenset({ObjectRef("sample/role", "admin")})


def test_filters_have_canonical_order_without_mutable_schema_state() -> None:
    backing = backing_from_dict(
        {"kind": "fk", "path": "roster__user", "filters": {"z": False, "a": 1}}
    )
    assert backing == FieldBinding(path="roster__user", filters=(("a", 1), ("z", False)))
    assert isinstance(backing_from_dict({"kind": "attribute", "field": "kind"}), AttributeBinding)


def test_filter_lookup_paths_are_preserved_for_same_join_resolution() -> None:
    backing = backing_from_dict(
        {
            "kind": "fk",
            "path": "memberships__user",
            "filters": {
                "memberships__role": "editor",
                "memberships__is_confirmed": True,
            },
        }
    )

    assert backing == FieldBinding(
        path="memberships__user",
        filters=(
            ("memberships__is_confirmed", True),
            ("memberships__role", "editor"),
        ),
    )


def test_filter_keys_reject_empty_lookup_segments() -> None:
    with pytest.raises(ValueError, match="ORM lookup names"):
        backing_from_dict(
            {
                "kind": "fk",
                "path": "memberships__user",
                "filters": {"memberships____role": "editor"},
            }
        )


def test_fixed_attribute_writeability_is_scoped_to_its_named_resource() -> None:
    schema = parse_zed(
        "definition auth/user {}\n"
        "definition sample/role {\n"
        ' relation member: auth/user // rebac:attribute={"field":"is_staff","resource":"admin","value":true}\n'
        "}\n"
    )
    definition = schema.get_definition("sample/role")
    assert definition is not None
    member = definition.relations[0]

    assert member.has_backing()
    assert member.has_backing("admin")
    assert not member.has_backing("editor")
    assert not relation_is_writable(
        schema, resource=ObjectRef("sample/role", "admin"), relation="member"
    )
    assert relation_is_writable(
        schema, resource=ObjectRef("sample/role", "editor"), relation="member"
    )
    assert not relation_is_writable(
        schema, resource=ObjectRef("sample/role", "editor"), relation="missing"
    )


def test_dynamic_boolean_container_rejects_noncanonical_numeric_spelling() -> None:
    user_model = get_user_model()
    backing = ResolvedAttributeBacking(
        target_model=user_model,
        target_resource_type="auth/user",
        target_id_attr="pk",
        field=user_model._meta.get_field("is_staff"),
        resource=None,
        value=None,
        filters={},
    )

    assert backing.subjects_filter("1").children == [("pk__in", [])]
    assert backing.subjects_filter("True").children == [("is_staff", True)]


def test_dynamic_integer_container_rejects_leading_zero_spelling() -> None:
    user_model = get_user_model()
    backing = ResolvedAttributeBacking(
        target_model=user_model,
        target_resource_type="auth/user",
        target_id_attr="pk",
        field=user_model._meta.pk,
        resource=None,
        value=None,
        filters={},
    )

    assert backing.subjects_filter("01").children == [("pk__in", [])]
    assert backing.subjects_filter("1").children == [(user_model._meta.pk.name, 1)]


def test_backing_render_is_deterministic_for_unicode_and_reordered_filters() -> None:
    first = parse_zed(
        "definition auth/user {}\n"
        "definition sample/container {\n"
        ' relation member: auth/user // rebac:field={"path":"roster__user","filters":{"roster__team":"équipe","roster__active":true}}\n'
        ' relation kind: auth/user // rebac:attribute={"field":"kind","filters":{"région":"Île-de-France"}}\n'
        "}\n"
    )
    second = parse_zed(
        "definition auth/user {}\n"
        "definition sample/container {\n"
        ' relation kind: auth/user // rebac:attribute={"field":"kind","filters":{"r\\u00e9gion":"\\u00cele-de-France"}}\n'
        ' relation member: auth/user // rebac:field={"path":"roster__user","filters":{"roster__active":true,"roster__team":"\\u00e9quipe"}}\n'
        "}\n"
    )

    rendered = render_zed(first)
    assert rendered == render_zed(second)
    assert "équipe" in rendered and "\\u00e9" not in rendered
    # Canonical form is a fixed point: relations sort by name on every render.
    assert render_zed(parse_zed(rendered)) == rendered
