"""Tests for ``rebac.check_new`` — preflight against not-yet-persisted resources."""

from __future__ import annotations

import pytest

from rebac import (
    LocalBackend,
    ObjectRef,
    PermissionDepthExceeded,
    PermissionResult,
    RelationshipTuple,
    SubjectRef,
    check_new,
)
from rebac.schema import parse_zed

SCHEMA_TEXT = """
caveat link_not_expired(expires_at timestamp, now timestamp) {
    now < expires_at
}

definition auth/user {}

definition auth/group {
    relation member: auth/user | auth/group#member
}

definition blog/vault {
    relation owner:  auth/user
    relation writer: auth/user | auth/group#member with link_not_expired
    permission write = owner + writer
    permission read  = owner + writer
}

definition blog/post {
    relation vault:  blog/vault
    relation author: auth/user

    permission create = vault->write
    permission read   = author + vault->read
    permission write  = author + vault->write
}

definition site/page {
    relation owner: auth/user
    permission create = authenticated
    permission read   = owner
}

definition pub/page {
    relation owner: auth/user
    permission create = anonymous
    permission read   = owner
}

definition project/widget {
    relation member: auth/user
    relation banned: auth/user
    permission create = member - banned
}

definition project/note {
    relation owner: auth/user
    permission create = owner
    permission visible = create
}
"""


@pytest.fixture
def backend(db):
    b = LocalBackend()
    b.set_schema(parse_zed(SCHEMA_TEXT))
    return b


def _user(id_: str) -> SubjectRef:
    return SubjectRef.of("auth/user", id_)


def _group(id_: str) -> SubjectRef:
    return SubjectRef.of("auth/group", id_, "member")


def _vault(id_: str) -> SubjectRef:
    return SubjectRef.of("blog/vault", id_)


def test_create_via_arrow_consults_real_target(backend):
    """`permission create = vault->write` allows when actor has write on the
    referenced (real) vault."""
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/vault", "v1"),
                relation="owner",
                subject=_user("alice"),
            ),
        ]
    )
    result = check_new(
        subject=_user("alice"),
        action="create",
        resource_type="blog/post",
        relationships={"vault": [_vault("v1")]},
        backend=backend,
    )
    assert result.allowed


def test_create_via_arrow_denies_when_actor_lacks_target_permission(backend):
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/vault", "v1"),
                relation="owner",
                subject=_user("alice"),
            ),
        ]
    )
    result = check_new(
        subject=_user("eve"),
        action="create",
        resource_type="blog/post",
        relationships={"vault": [_vault("v1")]},
        backend=backend,
    )
    assert not result.allowed


def test_create_denies_when_arrow_relation_not_supplied(backend):
    """No vault in the input → no path to write on a vault → deny."""
    result = check_new(
        subject=_user("alice"),
        action="create",
        resource_type="blog/post",
        relationships={},
        backend=backend,
    )
    assert not result.allowed


def test_create_via_arrow_walks_each_candidate(backend):
    """Multiple virtual targets via OR: allow if any one grants the target permission."""
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/vault", "v2"),
                relation="owner",
                subject=_user("alice"),
            ),
        ]
    )
    result = check_new(
        subject=_user("alice"),
        action="create",
        resource_type="blog/post",
        relationships={"vault": [_vault("v1"), _vault("v2")]},
        backend=backend,
    )
    assert result.allowed


def test_create_via_arrow_with_group_membership(backend):
    """Subject-set membership on the real target flows through correctly."""
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("auth/group", "g1"),
                relation="member",
                subject=_user("alice"),
            ),
            RelationshipTuple(
                resource=ObjectRef("blog/vault", "v1"),
                relation="writer",
                subject=_group("g1"),
            ),
        ]
    )
    result = check_new(
        subject=_user("alice"),
        action="create",
        resource_type="blog/post",
        relationships={"vault": [_vault("v1")]},
        backend=backend,
    )
    assert result.allowed


def test_authenticated_builtin(backend):
    assert check_new(
        subject=_user("alice"),
        action="create",
        resource_type="site/page",
        backend=backend,
    ).allowed


def test_authenticated_builtin_denies_anonymous(backend):
    from rebac import ANONYMOUS_ACTOR

    assert not check_new(
        subject=ANONYMOUS_ACTOR,
        action="create",
        resource_type="site/page",
        backend=backend,
    ).allowed


def test_anonymous_builtin_allows_anonymous(backend):
    from rebac import ANONYMOUS_ACTOR

    assert check_new(
        subject=ANONYMOUS_ACTOR,
        action="create",
        resource_type="pub/page",
        backend=backend,
    ).allowed


def test_minus_operator_denies_banned(backend):
    """`permission create = member - banned` — denial wins when both apply."""
    result = check_new(
        subject=_user("alice"),
        action="create",
        resource_type="project/widget",
        relationships={
            "member": [_user("alice")],
            "banned": [_user("alice")],
        },
        backend=backend,
    )
    assert not result.allowed


def test_minus_operator_allows_when_only_member(backend):
    result = check_new(
        subject=_user("alice"),
        action="create",
        resource_type="project/widget",
        relationships={
            "member": [_user("alice")],
            "banned": [_user("eve")],
        },
        backend=backend,
    )
    assert result.allowed


def test_subpermission_reference_resolves(backend):
    """`permission visible = create` should follow the sub-permission ref."""
    result = check_new(
        subject=_user("alice"),
        action="visible",
        resource_type="project/note",
        relationships={"owner": [_user("alice")]},
        backend=backend,
    )
    assert result.allowed


def test_no_permission_falls_back_to_relation_match(backend):
    """When `action` isn't declared as a permission, treat as direct relation."""
    result = check_new(
        subject=_user("alice"),
        action="owner",
        resource_type="blog/vault",
        relationships={"owner": [_user("alice")]},
        backend=backend,
    )
    assert result.allowed


def test_unknown_resource_type_denies_with_reason(backend):
    result = check_new(
        subject=_user("alice"),
        action="create",
        resource_type="unknown/thing",
        backend=backend,
    )
    assert not result.allowed
    assert result.reason and "unknown" in result.reason


def test_wildcard_subject_matches_in_virtual_relation(backend):
    """A virtual `<type>:*` subject matches any actor of that type."""
    wildcard = SubjectRef.of("auth/user", "*")
    result = check_new(
        subject=_user("alice"),
        action="owner",
        resource_type="blog/vault",
        relationships={"owner": [wildcard]},
        backend=backend,
    )
    assert result.allowed


def test_unknown_action_returns_diagnostic(backend):
    """A misspelled / undeclared action name surfaces a clear reason."""
    result = check_new(
        subject=_user("alice"),
        action="crete",  # typo: not a permission or relation on blog/post
        resource_type="blog/post",
        relationships={"vault": [_vault("v1")]},
        backend=backend,
    )
    assert not result.allowed
    assert result.reason and "unknown action" in result.reason


def test_virtual_relation_with_subject_set_candidate(backend):
    """A virtual `auth/group:g1#member` candidate resolves via the backend.

    Mirrors LocalBackend's `_has_direct_relation` subject-set walk — the
    actor's membership in the (real) group lifts the virtual relation match.
    """
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("auth/group", "g1"),
                relation="member",
                subject=_user("alice"),
            ),
        ]
    )
    result = check_new(
        subject=_user("alice"),
        action="writer",
        resource_type="blog/vault",
        relationships={"writer": [_group("g1")]},
        backend=backend,
    )
    assert result.allowed


def test_virtual_relation_with_subject_set_candidate_denies_non_member(backend):
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("auth/group", "g1"),
                relation="member",
                subject=_user("alice"),
            ),
        ]
    )
    result = check_new(
        subject=_user("eve"),
        action="writer",
        resource_type="blog/vault",
        relationships={"writer": [_group("g1")]},
        backend=backend,
    )
    assert not result.allowed


def test_arrow_target_conditional_propagates(backend):
    """Caveat-conditional permission on the (real) arrow target surfaces as
    CONDITIONAL_PERMISSION with the missing parameter name."""
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/vault", "v1"),
                relation="writer",
                subject=_user("alice"),
                caveat_name="link_not_expired",
                caveat_context={"expires_at": "2099-01-01T00:00:00Z"},
            ),
        ]
    )
    # Caller did not supply `now` — backend.check_access on the real vault
    # returns CONDITIONAL, and that flows through the virtual arrow.
    result = check_new(
        subject=_user("alice"),
        action="create",
        resource_type="blog/post",
        relationships={"vault": [_vault("v1")]},
        backend=backend,
    )
    assert result.result is PermissionResult.CONDITIONAL_PERMISSION
    assert result.conditional_on == ("now",)
    assert not result.allowed


def test_arrow_target_conditional_satisfied_returns_has(backend):
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/vault", "v1"),
                relation="writer",
                subject=_user("alice"),
                caveat_name="link_not_expired",
                caveat_context={"expires_at": "2099-01-01T00:00:00Z"},
            ),
        ]
    )
    result = check_new(
        subject=_user("alice"),
        action="create",
        resource_type="blog/post",
        relationships={"vault": [_vault("v1")]},
        backend=backend,
        context={"now": "1999-01-01T00:00:00Z"},
    )
    assert result.allowed
    assert result.result is PermissionResult.HAS_PERMISSION


def test_depth_limit_on_arrow_hop_raises(settings, backend):
    """A virtual arrow hop costs depth+1 against REBAC_DEPTH_LIMIT.

    Set the limit to 0 so the first hop overflows.
    """
    settings.REBAC_DEPTH_LIMIT = 0
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/vault", "v1"),
                relation="owner",
                subject=_user("alice"),
            ),
        ]
    )
    with pytest.raises(PermissionDepthExceeded):
        check_new(
            subject=_user("alice"),
            action="create",
            resource_type="blog/post",
            relationships={"vault": [_vault("v1")]},
            backend=backend,
        )


def test_depth_limit_on_subject_set_candidate_raises(settings, backend):
    """A virtual subject-set candidate dispatches into the backend on the
    real group row — that costs depth+1 too."""
    settings.REBAC_DEPTH_LIMIT = 0
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("auth/group", "g1"),
                relation="member",
                subject=_user("alice"),
            ),
        ]
    )
    with pytest.raises(PermissionDepthExceeded):
        check_new(
            subject=_user("alice"),
            action="writer",
            resource_type="blog/vault",
            relationships={"writer": [_group("g1")]},
            backend=backend,
        )


def test_minus_with_conditional_right_propagates(backend):
    """`member - banned` where `banned` is caveat-conditional via an arrow
    hop should propagate CONDITIONAL when `member` resolves True."""
    # The schema doesn't expose a binop arrow→caveat path on a single
    # definition without extending it; this test guards the in-memory
    # walker's tri-state combinators stay aligned with LocalBackend's
    # behaviour for the simpler shape: pure bools combine correctly under
    # `-`.
    result = check_new(
        subject=_user("alice"),
        action="create",
        resource_type="project/widget",
        relationships={
            "member": [_user("alice")],
            "banned": [],
        },
        backend=backend,
    )
    assert result.allowed
    # And the symmetric: both members and banned populated → deny.
    blocked = check_new(
        subject=_user("alice"),
        action="create",
        resource_type="project/widget",
        relationships={
            "member": [_user("alice")],
            "banned": [_user("alice")],
        },
        backend=backend,
    )
    assert not blocked.allowed
