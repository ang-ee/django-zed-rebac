"""Policy graph validation before any live predicate is built."""

from __future__ import annotations

import pytest

from rebac.compile.program import CompileProgram
from rebac.errors import SchemaError
from rebac.schema import parse_zed


def test_linear_positive_recursion_is_accepted():
    schema = parse_zed(
        """
        definition auth/user {}
        definition test/folder {
            relation owner: auth/user
            relation parent: test/folder
            permission read = owner + parent->read
        }
        """
    )
    program = CompileProgram.build(schema)
    assert ("test/folder", "read") in program.recursive
    assert ("test/folder", "parent") in program.reachable(("test/folder", "read"))


def test_negative_internal_recursive_edge_is_rejected():
    schema = parse_zed(
        """
        definition test/folder {
            relation parent: test/folder
            permission read = authenticated - parent->read
        }
        """
    )
    with pytest.raises(SchemaError, match="exclusion dependency"):
        CompileProgram.build(schema)


def test_negative_external_recursive_edge_is_allowed():
    schema = parse_zed(
        """
        definition auth/user {}
        definition test/folder {
            relation owner: auth/user
            relation parent: test/folder
            permission read = owner + parent->read
            permission no_read = authenticated - read
        }
        """
    )
    program = CompileProgram.build(schema)
    assert ("test/folder", "no_read") not in program.recursive


def test_nonlinear_cycle_is_rejected():
    schema = parse_zed(
        """
        definition test/folder {
            relation parent: test/folder
            permission read = parent->read + parent->read
        }
        """
    )
    with pytest.raises(SchemaError, match="nonlinear"):
        CompileProgram.build(schema)


def test_duplicate_allowed_subject_dispatch_is_one_recursive_edge():
    schema = parse_zed(
        """
        caveat conditional(ok bool) { ok }
        definition auth/user {}
        definition test/folder {
            relation owner: auth/user
            relation parent: test/folder | test/folder with conditional
            permission read = owner + parent->read
        }
        definition test/group {
            relation member: auth/user | test/group#member | test/group#member with conditional
        }
        """
    )
    program = CompileProgram.build(schema)
    assert [dep.target for dep in program.dependencies[("test/folder", "read")]].count(
        ("test/folder", "read")
    ) == 1
    assert [dep.target for dep in program.dependencies[("test/group", "member")]].count(
        ("test/group", "member")
    ) == 1
    assert {("test/folder", "read"), ("test/group", "member")} <= program.recursive


def test_alias_cycle_can_have_an_external_arrow_without_structural_recursion():
    program = CompileProgram.build(
        parse_zed("""
        definition auth/user {}
        definition test/group { relation member: auth/user }
        definition test/folder {
            relation owner: test/group
            permission read = alias
            permission alias = read + owner->member
        }
    """)
    )
    assert program.alias_cycles == {("test/folder", "read"), ("test/folder", "alias")}
    assert not program.recursive


def test_alias_in_a_component_with_a_traversal_is_structural():
    program = CompileProgram.build(
        parse_zed("""
        definition test/folder {
            relation parent: test/folder
            permission read = alias
            permission alias = parent->read
        }
    """)
    )
    assert program.recursive == {("test/folder", "read"), ("test/folder", "alias")}
    assert not program.alias_cycles
