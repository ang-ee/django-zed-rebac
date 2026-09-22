"""Smoke tests for core types."""

from __future__ import annotations

import pytest

from rebac import (
    CheckResult,
    ObjectRef,
    PermissionResult,
    RelationshipTuple,
    SubjectRef,
    Zookie,
)


def test_object_ref_str_and_parse():
    ref = ObjectRef("blog/post", "abc")
    assert str(ref) == "blog/post:abc"
    assert ObjectRef.parse("blog/post:abc") == ref


def test_object_ref_parse_rejects_missing_colon():
    with pytest.raises(ValueError):
        ObjectRef.parse("blog/post")


def test_subject_ref_with_relation():
    s = SubjectRef.of("auth/group", "eng", "member")
    assert str(s) == "auth/group:eng#member"
    assert s.subject_type == "auth/group"
    assert s.subject_id == "eng"
    assert s.optional_relation == "member"


def test_subject_ref_round_trip():
    raw = "agents/grant:abc#valid"
    s = SubjectRef.parse(raw)
    assert str(s) == raw


@pytest.mark.parametrize(
    ("subject", "caveat_name", "expected"),
    [
        ("auth/user:alice", "", "blog/post:1#viewer @ auth/user:alice"),
        ("auth/group:eng#member", "", "blog/post:1#viewer @ auth/group:eng#member"),
        (
            "auth/user:alice",
            "during_business_hours",
            "blog/post:1#viewer @ auth/user:alice with during_business_hours",
        ),
        (
            "auth/group:eng#member",
            "during_business_hours",
            "blog/post:1#viewer @ auth/group:eng#member with during_business_hours",
        ),
    ],
)
def test_relationship_tuple_str(subject, caveat_name, expected):
    tuple_ = RelationshipTuple(
        resource=ObjectRef("blog/post", "1"),
        relation="viewer",
        subject=SubjectRef.parse(subject),
        caveat_name=caveat_name,
    )

    assert str(tuple_) == expected


def test_check_result_helpers():
    assert CheckResult.has() == CheckResult(True, PermissionResult.HAS_PERMISSION, ())
    assert not CheckResult.no()
    cond = CheckResult.conditional(("ip", "cidr"))
    assert cond.result == PermissionResult.CONDITIONAL_PERMISSION
    assert cond.conditional_on == ("ip", "cidr")
    assert not cond.allowed


def test_zookie_round_trip():
    z = Zookie("local", "12345")
    assert str(z) == "local.12345"
    assert Zookie.parse("local.12345") == z
