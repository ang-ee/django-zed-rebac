"""Tests for the .zed schema parser."""

from __future__ import annotations

import pytest

from rebac.schema import (
    AllowedSubject,
    PermArrow,
    PermBinOp,
    PermNil,
    PermRef,
    parse_zed,
    validate_schema,
)
from rebac.schema.parser import ParseError

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
    viewer = next(r for r in schema.get_definition("storage/file").relations if r.name == "viewer")
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
    viewer = next(r for r in schema.get_definition("storage/file").relations if r.name == "viewer")
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
    viewer = next(r for r in schema.get_definition("storage/file").relations if r.name == "viewer")
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
