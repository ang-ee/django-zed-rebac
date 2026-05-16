"""Integration tests for LocalBackend evaluation against the synthetic schema."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import (
    LocalBackend,
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
)
from rebac.models import SchemaDefinition, SchemaPermission, SchemaRelation
from rebac.schema import parse_zed

SCHEMA_TEXT = """
definition auth/user {}

definition auth/group {
    relation member: auth/user | auth/group#member
}

definition blog/post {
    relation owner:  auth/user
    relation viewer: auth/user | auth/group#member | auth/user:*
    relation folder: blog/folder

    permission read   = owner + viewer + folder->read
    permission write  = owner
    permission delete = owner
}

definition blog/folder {
    relation owner: auth/user
    relation viewer: auth/user | auth/group#member
    relation parent: blog/folder
    permission read = owner + viewer + parent->read
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


def _post(id_: str) -> ObjectRef:
    return ObjectRef("blog/post", id_)


def _folder(id_: str) -> ObjectRef:
    return ObjectRef("blog/folder", id_)


def test_owner_has_read_and_write(backend):
    backend.write_relationships(
        [
            RelationshipTuple(resource=_post("p1"), relation="owner", subject=_user("u1")),
        ]
    )
    assert backend.has_access(subject=_user("u1"), action="read", resource=_post("p1"))
    assert backend.has_access(subject=_user("u1"), action="write", resource=_post("p1"))
    assert not backend.has_access(subject=_user("u2"), action="read", resource=_post("p1"))


def test_viewer_can_read_but_not_write(backend):
    backend.write_relationships(
        [
            RelationshipTuple(resource=_post("p2"), relation="viewer", subject=_user("u3")),
        ]
    )
    assert backend.has_access(subject=_user("u3"), action="read", resource=_post("p2"))
    assert not backend.has_access(subject=_user("u3"), action="write", resource=_post("p2"))


def test_group_membership_inherits_read(backend):
    # User u4 is a member of group g1; g1#member is a viewer of post p3.
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("auth/group", "g1"),
                relation="member",
                subject=_user("u4"),
            ),
            RelationshipTuple(
                resource=_post("p3"),
                relation="viewer",
                subject=_group("g1"),
            ),
        ]
    )
    assert backend.has_access(subject=_user("u4"), action="read", resource=_post("p3"))
    assert not backend.has_access(subject=_user("u5"), action="read", resource=_post("p3"))


def test_wildcard_grants_read_to_anyone(backend):
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=_post("p4"),
                relation="viewer",
                subject=SubjectRef.of("auth/user", "*"),
            ),
        ]
    )
    assert backend.has_access(subject=_user("u_anything"), action="read", resource=_post("p4"))


def test_schema_level_builtin_actor_grants(db):
    backend = LocalBackend()
    backend.set_schema(
        parse_zed(
            """
            definition auth/user {}

            definition auth_oidc/provider {
                permission list = anonymous + authenticated
                permission preauth = anonymous
                permission signed_in = authenticated
            }
            """
        )
    )
    provider = ObjectRef("auth_oidc/provider", "google")
    anonymous = SubjectRef.of("anonymous", "*")
    alice = SubjectRef.of("auth/user", "alice")
    service = SubjectRef.of("auth/service", "worker")

    assert backend.has_access(subject=anonymous, action="list", resource=provider)
    assert backend.has_access(subject=alice, action="list", resource=provider)
    assert backend.has_access(subject=service, action="list", resource=provider)
    assert backend.has_access(subject=anonymous, action="preauth", resource=provider)
    assert not backend.has_access(subject=alice, action="preauth", resource=provider)
    assert backend.has_access(subject=alice, action="signed_in", resource=provider)
    assert not backend.has_access(subject=anonymous, action="signed_in", resource=provider)
    assert backend.grants_all(
        subject=anonymous,
        action="list",
        resource_type="auth_oidc/provider",
    )
    assert backend.grants_all(
        subject=alice,
        action="signed_in",
        resource_type="auth_oidc/provider",
    )
    assert not backend.grants_all(
        subject=anonymous,
        action="signed_in",
        resource_type="auth_oidc/provider",
    )


@pytest.mark.django_db
def test_db_loaded_schema_refreshes_when_schema_rows_change() -> None:
    sd = SchemaDefinition.objects.create(resource_type="blog/post")
    SchemaRelation.objects.create(
        definition=sd,
        name="owner",
        allowed_subjects=[{"type": "auth/user"}],
    )
    SchemaRelation.objects.create(
        definition=sd,
        name="viewer",
        allowed_subjects=[{"type": "auth/user"}],
    )
    permission = SchemaPermission.objects.create(
        definition=sd,
        name="read",
        expression="owner",
    )
    SchemaDefinition.objects.create(resource_type="auth/user")

    backend = LocalBackend()
    post = _post("p-db-refresh")
    alice = _user("alice")
    bob = _user("bob")
    backend.write_relationships(
        [
            RelationshipTuple(resource=post, relation="owner", subject=alice),
            RelationshipTuple(resource=post, relation="viewer", subject=bob),
        ]
    )

    assert backend.has_access(subject=alice, action="read", resource=post)
    assert not backend.has_access(subject=bob, action="read", resource=post)

    permission.expression = "owner + viewer"
    permission.save(update_fields=["expression"])

    assert backend.has_access(subject=bob, action="read", resource=post)


@pytest.mark.django_db
def test_cached_db_schema_does_not_query_schema_tables_on_hot_path() -> None:
    sd = SchemaDefinition.objects.create(resource_type="blog/post")
    SchemaRelation.objects.create(
        definition=sd,
        name="owner",
        allowed_subjects=[{"type": "auth/user"}],
    )
    SchemaPermission.objects.create(
        definition=sd,
        name="read",
        expression="owner",
    )
    SchemaDefinition.objects.create(resource_type="auth/user")

    backend = LocalBackend()
    post = _post("p-db-hot-path")
    alice = _user("alice")
    backend.write_relationships(
        [RelationshipTuple(resource=post, relation="owner", subject=alice)]
    )

    assert backend.has_access(subject=alice, action="read", resource=post)

    with CaptureQueriesContext(connection) as queries:
        assert backend.has_access(subject=alice, action="read", resource=post)

    schema_queries = [
        query["sql"] for query in queries if '"rebac_schema' in query["sql"].lower()
    ]
    assert schema_queries == []


def test_arrow_propagates_via_folder(backend):
    # u6 is folder owner; post p5 is in that folder; p5#read should resolve via folder->read.
    backend.write_relationships(
        [
            RelationshipTuple(resource=_folder("f1"), relation="owner", subject=_user("u6")),
            RelationshipTuple(
                resource=_post("p5"),
                relation="folder",
                subject=SubjectRef(object=ObjectRef("blog/folder", "f1")),
            ),
        ]
    )
    assert backend.has_access(subject=_user("u6"), action="read", resource=_post("p5"))


def test_accessible_returns_owned_resources(backend):
    backend.write_relationships(
        [
            RelationshipTuple(resource=_post("a"), relation="owner", subject=_user("u7")),
            RelationshipTuple(resource=_post("b"), relation="owner", subject=_user("u7")),
            RelationshipTuple(resource=_post("c"), relation="owner", subject=_user("u8")),
        ]
    )
    ids = set(backend.accessible(subject=_user("u7"), action="read", resource_type="blog/post"))
    assert ids == {"a", "b"}


def test_lookup_subjects(backend):
    backend.write_relationships(
        [
            RelationshipTuple(resource=_post("p9"), relation="owner", subject=_user("u9")),
            RelationshipTuple(resource=_post("p9"), relation="viewer", subject=_user("u10")),
        ]
    )
    subs = list(
        backend.lookup_subjects(resource=_post("p9"), action="read", subject_type="auth/user")
    )
    ids = {s.subject_id for s in subs}
    assert ids == {"u9", "u10"}


def test_unknown_resource_type_returns_no(backend):
    assert not backend.has_access(
        subject=_user("u1"),
        action="read",
        resource=ObjectRef("unknown/thing", "x"),
    )
