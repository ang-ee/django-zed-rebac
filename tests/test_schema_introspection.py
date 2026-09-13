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


# ---------------------------------------------------------------------------
# live_backed_resource_types — conservative reachability to live ORM backing
# ---------------------------------------------------------------------------

LIVE_SCHEMA_TEXT = """
definition auth/user {}

definition auth/group {
    relation member: auth/user // rebac:attribute={"field":"is_staff","resource":"staff","value":true}
}

definition blog/folder {
    relation owner: auth/user
    permission read = owner
}

definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}

definition blog/comment {
    relation viewer: auth/group#member
    permission read = viewer
}

definition blog/digest {
    relation post: blog/post
    permission read = post->read
}

definition platform/role {
    relation member: auth/user
}

definition blog/banner {
    relation admin: platform/role // rebac:const=admin
    permission read = admin->member
}

definition blog/tag {
    relation owner: auth/user
    permission read = owner
}
"""


def test_live_backed_resource_types_seeds_field_and_attribute_backings():
    from rebac.schema.introspection import live_backed_resource_types

    live = live_backed_resource_types(parse_zed(LIVE_SCHEMA_TEXT))

    assert "blog/post" in live  # field backing on its own relation
    assert "auth/group" in live  # attribute backing on its own relation


def test_live_backed_resource_types_propagates_through_allowed_subject_types():
    from rebac.schema.introspection import live_backed_resource_types

    live = live_backed_resource_types(parse_zed(LIVE_SCHEMA_TEXT))

    assert "blog/comment" in live  # subject set ``auth/group#member``
    assert "blog/digest" in live  # arrow ``post->read`` walks a live type


def test_live_backed_resource_types_excludes_static_and_unreachable_types():
    from rebac.schema.introspection import live_backed_resource_types

    live = live_backed_resource_types(parse_zed(LIVE_SCHEMA_TEXT))

    # Const backing is static; ``platform/role`` and its subjects are tuples.
    assert "blog/banner" not in live
    assert "platform/role" not in live
    # Subjects of a live container are not themselves live.
    assert "auth/user" not in live
    assert "blog/folder" not in live
    assert "blog/tag" not in live


def test_live_backed_resource_types_follows_const_targets_into_live_types():
    from rebac.schema.introspection import live_backed_resource_types

    schema = parse_zed(
        LIVE_SCHEMA_TEXT.replace(
            "definition platform/role {\n    relation member: auth/user\n}",
            'definition platform/role {\n    relation member: auth/user // rebac:attribute={"field":"kind"}\n}',
        )
    )

    assert "blog/banner" in live_backed_resource_types(schema)


def test_live_backed_resource_types_is_empty_without_live_backings():
    from rebac.schema.introspection import live_backed_resource_types

    assert live_backed_resource_types(parse_zed(SCHEMA_TEXT)) == frozenset()


def test_accessible_is_exact_requires_no_caveats_and_no_builtin_actor_terms():
    from rebac.schema.introspection import accessible_is_exact

    assert accessible_is_exact(parse_zed(LIVE_SCHEMA_TEXT))
    assert not accessible_is_exact(
        parse_zed(
            LIVE_SCHEMA_TEXT.replace(
                "permission read = owner\n}", "permission read = owner + authenticated\n}", 1
            )
        )
    )
    assert not accessible_is_exact(
        parse_zed(
            'caveat present(token string) {\n token == "x"\n}\n'
            + LIVE_SCHEMA_TEXT.replace(
                "relation owner: auth/user\n    permission read = owner",
                "relation owner: auth/user | auth/user with present\n    permission read = owner",
                1,
            )
        )
    )
