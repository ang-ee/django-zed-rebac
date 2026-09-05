from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebac.schema import (
    PermBinOp,
    parse_zed,
    permission_object_sources,
    render_zed,
    resolve_schema_path,
)
from rebac.types import ObjectRef

SOURCE = """
// @rebac_package: library
// @rebac_package_version: 1.0
// @rebac_schema_revision: 3
// @rebac_extended_by: tools@2
use typechecking
use expiration
caveat permitted(value int) { value > 4 }
definition auth/user {}
definition access/role {
    relation member: auth/user
}
definition content/folder {
    relation editor: access/role:editor#member
    relation cycle: content/item#read
    permission read = editor + cycle
}
definition content/item {
    relation owner: auth/user // rebac:field=owner_id
    relation root: access/role // rebac:const=admin
    relation role: access/role:direct#member with permitted | auth/user:* with expiration
    relation parent: content/folder
    relation nested: content/folder#editor
    relation blocked: access/role:blocked#member
    permission base = role + parent->read
    permission read = (base + parent->editor + nested) & (root->member - blocked)
    permission missing = nil
}
"""


def test_render_round_trips_semantic_source_and_is_canonical():
    original = parse_zed(SOURCE)
    rendered = render_zed(original)
    parsed = parse_zed(rendered)
    assert parsed.headers == original.headers
    assert parsed.directives == original.directives
    assert parsed.caveats == original.caveats
    for definition in original.definitions:
        other = parsed.get_definition(definition.resource_type)
        assert other is not None
        assert set(other.relations) == set(definition.relations)
        assert {replace(p, raw_text="") for p in other.permissions} == {
            replace(p, raw_text="") for p in definition.permissions
        }
    assert render_zed(parsed) == rendered
    assert "rebac:field=owner_id" in rendered
    assert "rebac:const=admin" in rendered
    assert "rebac:field=" not in render_zed(original, include_backing=False)
    assert "rebac:const=" not in render_zed(original, include_backing=False)


def test_object_sources_follow_arrows_relations_subject_sets_and_cycles():
    sources = permission_object_sources(
        parse_zed(SOURCE), "content/item", "read", object_type="access/role"
    )
    assert sources == frozenset(
        ObjectRef("access/role", name) for name in ("admin", "direct", "editor")
    )


@pytest.mark.parametrize(
    ("resource", "permission"),
    [("missing/type", "read"), ("content/item", "unknown"), ("content/item", "missing")],
)
def test_object_sources_missing_terms_are_empty(resource, permission):
    assert not permission_object_sources(
        parse_zed(SOURCE), resource, permission, object_type="access/role"
    )


def test_object_sources_accept_relation_and_do_not_invent_generic_ids():
    schema = parse_zed(SOURCE)
    assert (
        permission_object_sources(schema, "content/item", "owner", object_type="auth/user")
        == frozenset()
    )
    assert permission_object_sources(
        schema, "content/item", "role", object_type="access/role"
    ) == frozenset({ObjectRef("access/role", "direct")})


@pytest.mark.parametrize("declaration", [None, "permissions.zed", Path("permissions.zed")])
def test_schema_source_default_and_relative_path(tmp_path, declaration):
    source = tmp_path / "permissions.zed"
    source.write_text(SOURCE)
    app = SimpleNamespace(path=str(tmp_path), rebac_schema=declaration)
    assert resolve_schema_path(app) == source


def test_schema_source_absolute_missing_and_invalid(tmp_path):
    source = tmp_path / "custom.zed"
    app = SimpleNamespace(path=str(tmp_path / "other"), rebac_schema=source)
    assert resolve_schema_path(app) is None
    source.write_text(SOURCE)
    assert resolve_schema_path(app) == source
    app.rebac_schema = tmp_path
    with pytest.raises(IsADirectoryError):
        resolve_schema_path(app)


def test_definition_extension_uses_native_ast_without_mutation():
    base = parse_zed(
        "definition file { relation owner: user permission read = owner }"
    ).definitions[0]
    addition = parse_zed(
        "definition file { relation viewer: user permission read = viewer }"
    ).definitions[0]
    extended = base.extend(relations=addition.relations, permission_arms=addition.permissions)
    assert len(base.relations) == 1
    assert len(extended.relations) == 2
    expression = extended.permissions[0].expression
    assert isinstance(expression, PermBinOp)
    assert expression.left == base.permissions[0].expression
    with pytest.raises(ValueError, match="already declared"):
        extended.extend(relations=addition.relations)
    with pytest.raises(ValueError, match="not declared"):
        base.extend(permission_arms=[replace(addition.permissions[0], name="write")])


@pytest.mark.parametrize("expression", ["root->absent", "unknown->member"])
def test_object_sources_missing_arrow_targets_contribute_nothing(expression):
    schema = parse_zed(
        "definition access/role { relation member: auth/user } "
        "definition content/item { "
        "relation root: access/role // rebac:const=admin\n"
        "relation unknown: missing/role // rebac:const=admin\n"
        f"permission read = {expression} }}"
    )
    assert not permission_object_sources(schema, "content/item", "read", object_type="access/role")
