from __future__ import annotations

from rebac.roles import roles_reaching
from rebac.schema import parse_zed
from rebac.schema.introspection import (
    permission_sources,
    permissions_reaching_relation,
    relation_dependencies,
)
from rebac.types import ObjectRef

SCHEMA_TEXT = """
definition auth/user {}

definition storage/role {
    relation member: auth/user
}

definition blog/folder {
    relation owner: auth/user
    permission read = owner
}

definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user | storage/role:object_viewer#member
    relation folder: blog/folder
    relation admin: storage/role // rebac:const=admin

    permission base = owner + viewer
    permission read = base + folder->read + authenticated
    permission manage = admin->member
}
"""


def test_permission_sources_collects_refs_arrows_builtins_and_subpermissions() -> None:
    schema = parse_zed(SCHEMA_TEXT)

    sources = permission_sources(schema, "blog/post", "read")

    assert sources.direct_relations == frozenset({"owner", "viewer"})
    assert sources.arrows == frozenset({("folder", "read")})
    assert sources.builtins == frozenset({"authenticated"})
    assert sources.subpermissions == frozenset({"base"})


def test_relation_dependencies_include_direct_refs_and_arrow_via_relations() -> None:
    schema = parse_zed(SCHEMA_TEXT)

    assert relation_dependencies(schema, "blog/post", "read") == frozenset(
        {"owner", "viewer", "folder"}
    )


def test_permissions_reaching_relation_uses_subpermissions() -> None:
    schema = parse_zed(SCHEMA_TEXT)

    assert permissions_reaching_relation(schema, "blog/post", "owner") == frozenset(
        {"base", "read"}
    )


def test_roles_reaching_is_parameterized_by_role_resource_type() -> None:
    schema = parse_zed(SCHEMA_TEXT)

    assert roles_reaching(
        "blog/post",
        "read",
        role_resource_type="storage/role",
        schema=schema,
    ) == frozenset({ObjectRef("storage/role", "object_viewer")})
    assert roles_reaching(
        "blog/post",
        "manage",
        role_resource_type="storage/role",
        schema=schema,
    ) == frozenset({ObjectRef("storage/role", "admin")})
