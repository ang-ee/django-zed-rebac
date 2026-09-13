"""Tests for the .zed schema parser."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from rebac.schema import (
    AllowedSubject,
    ConstBinding,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermNil,
    PermRef,
    Relation,
    Schema,
    parse_zed,
    validate_schema,
)
from rebac.schema.parser import ParseError


def _relations(schema: Schema, resource_type: str) -> Sequence[Relation]:
    """Fetch a definition's relations, asserting the definition exists."""
    definition = schema.get_definition(resource_type)
    assert definition is not None
    return definition.relations


SAMPLE = """
// @rebac_package: blog
// @rebac_package_version: 0.1.0

use typechecking

definition auth/user {}
definition auth/group {
    relation member: auth/user | auth/group#member
}

caveat ip_in_cidr(ip ipaddress, cidr string) {
    ip.in_cidr(cidr)
}

definition blog/post {
    relation owner:  auth/user
    relation viewer: auth/user | auth/group#member | auth/user:*
    relation folder: blog/folder
    relation gated:  auth/user with ip_in_cidr

    permission read   = owner + viewer + folder->read
    permission write  = owner
    permission delete = owner & gated
    permission audit  = owner - viewer
}

definition blog/folder {
    relation owner: auth/user
    relation parent: blog/folder
    permission read = owner + parent->read
}
"""


def test_parses_full_schema():
    schema = parse_zed(SAMPLE)
    assert schema.headers["rebac_package"] == "blog"
    assert schema.headers["rebac_package_version"] == "0.1.0"
    assert "use typechecking" in schema.directives[0]
    assert {d.resource_type for d in schema.definitions} == {
        "auth/user",
        "auth/group",
        "blog/post",
        "blog/folder",
    }
    cav = schema.get_caveat("ip_in_cidr")
    assert cav is not None
    assert [(p.name, p.type) for p in cav.params] == [
        ("ip", "ipaddress"),
        ("cidr", "string"),
    ]

    post = schema.get_definition("blog/post")
    assert post is not None
    viewer = next(r for r in post.relations if r.name == "viewer")
    assert AllowedSubject("auth/user") in viewer.allowed_subjects
    assert AllowedSubject("auth/group", "member") in viewer.allowed_subjects
    assert any(s.wildcard for s in viewer.allowed_subjects)

    gated = next(r for r in post.relations if r.name == "gated")
    assert gated.allowed_subjects[0].with_caveat == "ip_in_cidr"


def test_relation_comment_directive_lifts_field_binding():
    schema = parse_zed(
        """
        definition blog/folder {}
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->read
        }
        """
    )

    folder = next(r for r in _relations(schema, "blog/post") if r.name == "folder")
    assert folder.backing == FieldBinding(attname="folder")


@pytest.mark.parametrize(
    "relation_line, expected",
    [
        ("relation folder: blog/folder | auth/user // rebac:field=folder", "exactly one"),
        ("relation folder: blog/folder#member // rebac:field=folder", "concrete type"),
        ("relation folder: blog/folder:* // rebac:field=folder", "concrete type"),
        ("relation folder: blog/folder:root // rebac:field=folder", "concrete type"),
        ("relation folder: blog/folder with expiration // rebac:field=folder", "expiration"),
        ("relation folder: blog/folder with ip_in_cidr // rebac:field=folder", "caveat"),
    ],
)
def test_validate_schema_rejects_non_concrete_field_backed_relations(
    relation_line: str,
    expected: str,
) -> None:
    schema = parse_zed(
        f"""
        caveat ip_in_cidr(ip ipaddress, cidr string) {{
            ip.in_cidr(cidr)
        }}
        definition auth/user {{}}
        definition blog/folder {{
            relation member: auth/user
        }}
        definition blog/post {{
            {relation_line}
        }}
        """
    )

    errors = validate_schema(schema)
    assert any("blog/post#folder" in error and expected in error for error in errors)


@pytest.mark.parametrize(
    "relation_line, expected",
    [
        ("relation folder: blog/folder | auth/user // rebac:const=root", "exactly one"),
        ("relation folder: blog/folder#member // rebac:const=root", "concrete type"),
        ("relation folder: blog/folder:* // rebac:const=root", "concrete type"),
        ("relation folder: blog/folder:other // rebac:const=root", "concrete type"),
        ("relation folder: blog/folder with expiration // rebac:const=root", "expiration"),
        ("relation folder: blog/folder with ip_in_cidr // rebac:const=root", "caveat"),
    ],
)
def test_validate_schema_rejects_non_concrete_const_backed_relations(
    relation_line: str,
    expected: str,
) -> None:
    # Const-backing carries the same single-concrete-type constraints as
    # field-backing — the shared "backed relation" validation enforces both.
    schema = parse_zed(
        f"""
        caveat ip_in_cidr(ip ipaddress, cidr string) {{
            ip.in_cidr(cidr)
        }}
        definition auth/user {{}}
        definition blog/folder {{
            relation member: auth/user
        }}
        definition blog/post {{
            {relation_line}
        }}
        """
    )

    errors = validate_schema(schema)
    assert any("blog/post#folder" in error and expected in error for error in errors)


def test_relation_declaring_both_field_and_const_backing_is_rejected() -> None:
    # A relation has exactly one backing slot; both directives in the relation's
    # line span is a ParseError (they cannot share one line — each anchors to
    # end-of-comment — so this spreads them across a two-line subject union).
    with pytest.raises(ParseError, match=r"Multiple backing directives"):
        parse_zed(
            """
            definition auth/user {}
            definition blog/folder {}
            definition blog/post {
                relation folder: blog/folder |  // rebac:field=folder
                    blog/other  // rebac:const=root
            }
            """
        )


@pytest.mark.parametrize(
    "const_id",
    ["role-admin", "01HZX9K7Q2P3R4S5", "a|b", "ns/admin", "a=b", "a+b"],
)
def test_const_directive_accepts_spicedb_object_ids(const_id: str) -> None:
    # The const target is a SpiceDB object id, not a Python identifier: hyphens,
    # ULIDs/sqids (leading digit), and `| / = +` must round-trip rather than be
    # silently dropped as an un-backed relation.
    schema = parse_zed(
        f"""
        definition auth/user {{}}
        definition org/role {{ relation member: auth/user }}
        definition blog/post {{
            relation admin: org/role // rebac:const={const_id}
        }}
        """
    )

    admin = next(r for r in _relations(schema, "blog/post") if r.name == "admin")
    assert admin.backing == ConstBinding(target_id=const_id)


def test_permission_expression_precedence():
    # Per SpiceDB: + binds tightest, & next, - loosest.
    # So  "a + b & c"  parses as  (a + b) & c.
    schema = parse_zed(
        """
        definition x/y {
            relation a: auth/user
            relation b: auth/user
            relation c: auth/user
            permission p = a + b & c
        }
        """
    )
    perm = schema.get_permission("x/y", "p")
    assert perm is not None
    expr = perm.expression
    assert isinstance(expr, PermBinOp)
    assert expr.op == "&"
    assert isinstance(expr.left, PermBinOp)
    assert expr.left.op == "+"
    assert isinstance(expr.right, PermRef)
    assert expr.right.name == "c"


def test_permission_with_arrow():
    schema = parse_zed(
        """
        definition x/y {
            relation parent: x/y
            permission read = parent->read
        }
        """
    )
    perm = schema.get_permission("x/y", "read")
    assert perm is not None
    assert isinstance(perm.expression, PermArrow)
    assert perm.expression.via == "parent"
    assert perm.expression.target == "read"


def test_permission_names_may_match_top_level_keywords():
    schema = parse_zed(
        """
        definition auth/service {
            relation owner: auth/user
            permission use = owner
        }
        definition auth/apikey {
            relation service: auth/service
            permission authenticate = service->use
        }
        """
    )

    assert schema.get_permission("auth/service", "use") is not None
    perm = schema.get_permission("auth/apikey", "authenticate")
    assert perm is not None
    assert isinstance(perm.expression, PermArrow)
    assert perm.expression.target == "use"


def test_permission_with_parentheses_overrides_precedence():
    schema = parse_zed(
        """
        definition x/y {
            relation a: auth/user
            relation b: auth/user
            relation c: auth/user
            permission p = a + (b & c)
        }
        """
    )
    perm = schema.get_permission("x/y", "p")
    assert perm is not None
    assert isinstance(perm.expression, PermBinOp)
    assert perm.expression.op == "+"
    assert isinstance(perm.expression.right, PermBinOp)
    assert perm.expression.right.op == "&"


def test_nil_permission():
    schema = parse_zed(
        """
        definition x/y {
            permission unreachable = nil
        }
        """
    )
    perm = schema.get_permission("x/y", "unreachable")
    assert perm is not None
    assert isinstance(perm.expression, PermNil)


def test_builtin_actor_terms_are_valid_permission_refs():
    schema = parse_zed(
        """
        definition auth/user {
            permission credential_lookup = anonymous + authenticated
        }
        """
    )

    assert validate_schema(schema) == []
    perm = schema.get_permission("auth/user", "credential_lookup")
    assert perm is not None
    assert isinstance(perm.expression, PermBinOp)
    assert perm.expression.left == PermRef("anonymous")
    assert perm.expression.right == PermRef("authenticated")


def test_builtin_actor_names_are_reserved_outside_permission_rhs():
    schema = parse_zed(
        """
        definition anonymous {}

        definition auth/user {
            relation anonymous: auth/user
            relation public: authenticated:*
            permission read = anonymous
        }
        """
    )

    errors = validate_schema(schema)
    assert any("cannot be declared as a definition" in error for error in errors)
    assert any("cannot be declared as relations" in error for error in errors)
    assert any("valid only inside permission expressions" in error for error in errors)


def test_validate_schema_detects_undefined_reference():
    schema = parse_zed(
        """
        definition x/y {
            permission p = nonexistent
        }
        """
    )
    errors = validate_schema(schema)
    assert any("undefined reference" in e for e in errors)


def test_unterminated_block_raises():
    with pytest.raises(ParseError):
        parse_zed("definition x/y { relation a: auth/user")


# ---------- Specific-id subject terms (universal-admin pattern) ----------


def test_specific_id_subject_without_relation():
    """`type:id` in a type union — specific resource id, no subject-set."""
    schema = parse_zed(
        """
        definition angee/role {}
        definition storage/file {
            relation viewer: angee/role:admin
        }
        """
    )
    viewer = next(r for r in _relations(schema, "storage/file") if r.name == "viewer")
    assert AllowedSubject(type="angee/role", id="admin") in viewer.allowed_subjects


def test_specific_id_subject_with_relation():
    """`type:id#relation` — the canonical universal-admin pattern."""
    schema = parse_zed(
        """
        definition angee/role {
            relation member: auth/user
        }
        definition storage/file {
            relation viewer: angee/role:admin#member
        }
        """
    )
    viewer = next(r for r in _relations(schema, "storage/file") if r.name == "viewer")
    assert (
        AllowedSubject(type="angee/role", id="admin", relation="member") in viewer.allowed_subjects
    )


def test_specific_id_in_union_with_other_shapes():
    """Specific-id mixed with wildcard, type-only, and subject-set shapes."""
    schema = parse_zed(
        """
        definition auth/user {}
        definition auth/group { relation member: auth/user }
        definition angee/role { relation member: auth/user }
        definition storage/file {
            relation viewer:
                auth/user
                | auth/user:*
                | auth/group#member
                | angee/role:admin#member
        }
        """
    )
    viewer = next(r for r in _relations(schema, "storage/file") if r.name == "viewer")
    assert AllowedSubject(type="auth/user") in viewer.allowed_subjects
    assert AllowedSubject(type="auth/user", wildcard=True) in viewer.allowed_subjects
    assert AllowedSubject(type="auth/group", relation="member") in viewer.allowed_subjects
    assert (
        AllowedSubject(type="angee/role", id="admin", relation="member") in viewer.allowed_subjects
    )


def test_specific_id_rejects_digit_start():
    """Numeric ids (e.g. `role:42`) are not legal at schema-parse time."""
    with pytest.raises(ParseError):
        parse_zed(
            """
            definition angee/role {}
            definition storage/file {
                relation viewer: angee/role:42
            }
            """
        )


def test_specific_id_rejects_hyphen():
    """Hyphenated ids (e.g. `role:obj-admin`) are not legal at schema-parse time."""
    with pytest.raises(ParseError):
        parse_zed(
            """
            definition angee/role {}
            definition storage/file {
                relation viewer: angee/role:obj-admin
            }
            """
        )


def test_specific_id_rejects_empty_after_colon():
    """`role:` with no id (next token is `}` or `|`) must fail-fast."""
    with pytest.raises(ParseError):
        parse_zed(
            """
            definition angee/role {}
            definition storage/file {
                relation viewer: angee/role:
            }
            """
        )


def test_specific_id_with_hash_requires_relation_name():
    """`type:id#` must be followed by an identifier."""
    with pytest.raises(ParseError):
        parse_zed(
            """
            definition angee/role {}
            definition storage/file {
                relation viewer: angee/role:admin#
            }
            """
        )


def test_specific_id_rejects_namespace_separator():
    """`role:a/b` — namespace separators are forbidden inside specific ids.

    The post-colon token tokenizes as kind="type" (contains `/`); the parser
    must reject it rather than silently splicing a sub-namespace into the
    role id.
    """
    with pytest.raises(ParseError):
        parse_zed(
            """
            definition angee/role {}
            definition storage/file {
                relation viewer: angee/role:sub/admin
            }
            """
        )
