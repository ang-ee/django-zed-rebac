from __future__ import annotations

import pytest

from rebac import LocalBackend, ObjectRef, RelationshipTuple, SubjectRef
from rebac.models import Relationship
from rebac.schema import parse_zed

SCHEMA_TEXT = """
definition auth/user {}

definition auth/group {
    relation member: auth/user
}

definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user
    relation group_viewer: auth/group#member
    relation public_viewer: auth/user:*

    permission read = owner + viewer + group_viewer + public_viewer
    permission write = owner
}
"""


@pytest.fixture
def backend(db):
    backend = LocalBackend()
    backend.set_schema(parse_zed(SCHEMA_TEXT))
    return backend


def _user(id_: str) -> SubjectRef:
    return SubjectRef.of("auth/user", id_)


def _group_member(id_: str) -> SubjectRef:
    return SubjectRef.of("auth/group", id_, "member")


def _post(id_: str) -> ObjectRef:
    return ObjectRef("blog/post", id_)


def test_stale_undeclared_action_row_does_not_authorize_check_or_accessible(backend) -> None:
    Relationship.objects.create(
        resource_type="blog/post",
        resource_id="p-undeclared",
        relation="delete",
        subject_type="auth/user",
        subject_id="alice",
    )

    assert not backend.has_access(
        subject=_user("alice"),
        action="delete",
        resource=_post("p-undeclared"),
    )
    assert (
        set(
            backend.accessible(
                subject=_user("alice"),
                action="delete",
                resource_type="blog/post",
            )
        )
        == set()
    )


def test_stale_subject_set_row_not_allowed_by_relation_does_not_authorize(backend) -> None:
    Relationship.objects.create(
        resource_type="auth/group",
        resource_id="eng",
        relation="member",
        subject_type="auth/user",
        subject_id="alice",
    )
    Relationship.objects.create(
        resource_type="blog/post",
        resource_id="p-invalid-subject-set",
        relation="viewer",
        subject_type="auth/group",
        subject_id="eng",
        optional_subject_relation="member",
    )

    assert not backend.has_access(
        subject=_user("alice"),
        action="read",
        resource=_post("p-invalid-subject-set"),
    )
    assert "p-invalid-subject-set" not in set(
        backend.accessible(
            subject=_user("alice"),
            action="read",
            resource_type="blog/post",
        )
    )


def test_stale_wildcard_row_not_allowed_by_relation_does_not_authorize(backend) -> None:
    Relationship.objects.create(
        resource_type="blog/post",
        resource_id="p-invalid-wildcard",
        relation="viewer",
        subject_type="auth/user",
        subject_id="*",
    )

    assert not backend.has_access(
        subject=_user("anyone"),
        action="read",
        resource=_post("p-invalid-wildcard"),
    )
    assert "p-invalid-wildcard" not in set(
        backend.accessible(
            subject=_user("anyone"),
            action="read",
            resource_type="blog/post",
        )
    )


def test_public_writes_reject_unknown_relation(backend) -> None:
    with pytest.raises(ValueError, match="unknown relation"):
        backend.write_relationships(
            [
                RelationshipTuple(
                    resource=_post("p-write-unknown"),
                    relation="delete",
                    subject=_user("alice"),
                )
            ]
        )


def test_public_writes_reject_subject_shape_not_in_relation_type_union(backend) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        backend.write_relationships(
            [
                RelationshipTuple(
                    resource=_post("p-write-invalid"),
                    relation="viewer",
                    subject=_group_member("eng"),
                )
            ]
        )


def test_valid_subject_set_and_wildcard_rows_still_authorize(backend) -> None:
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("auth/group", "eng"),
                relation="member",
                subject=_user("alice"),
            ),
            RelationshipTuple(
                resource=_post("p-group"),
                relation="group_viewer",
                subject=_group_member("eng"),
            ),
            RelationshipTuple(
                resource=_post("p-public"),
                relation="public_viewer",
                subject=_user("*"),
            ),
        ]
    )

    assert backend.has_access(subject=_user("alice"), action="read", resource=_post("p-group"))
    assert backend.has_access(subject=_user("bob"), action="read", resource=_post("p-public"))
