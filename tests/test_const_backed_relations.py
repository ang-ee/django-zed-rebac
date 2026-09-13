"""Const-backed relations resolve to one fixed object for every row.

A ``// rebac:const=<id>`` relation is the schema-level "static relationship"
SpiceDB never shipped (issue #346 / #1266): every object of the declaring type
behaves as if it pointed at ``<subject_type>:<id>``, with no stored tuple and
no model column. The canonical use is a universal-admin arrow —
``relation admin: angee/role // rebac:const=admin`` with
``permission read = owner + admin->member`` — so admin reach is one role
membership, not one grant per row.
"""

from __future__ import annotations

import pytest

from rebac import (
    LocalBackend,
    ObjectRef,
    RelationshipTuple,
    SchemaError,
    SubjectRef,
    sudo,
)
from rebac.models import Relationship, SchemaDefinition, SchemaPermission, SchemaRelation
from rebac.schema import ConstBinding, parse_zed
from rebac.types import RelationshipFilter

from .testapp.models import Post

SCHEMA_TEXT = """
definition auth/user {}

definition org/role {
    relation member: auth/user
}

definition blog/post {
    relation owner: auth/user
    relation admin: org/role // rebac:const=admin

    permission read = owner + admin->member
}
"""


@pytest.fixture
def backend(db):
    b = LocalBackend()
    b.set_schema(parse_zed(SCHEMA_TEXT))
    return b


def _user(id_: str) -> SubjectRef:
    return SubjectRef.of("auth/user", id_)


def _post_ref(post: Post) -> ObjectRef:
    return ObjectRef("blog/post", str(post.pk))


@pytest.fixture
def posts(db):
    with sudo(reason="const-backed-relations.fixture"):
        return [Post.objects.create(title=f"post-{i}") for i in range(3)]


def _grant_admin(backend: LocalBackend, user: str) -> None:
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("org/role", "admin"),
                relation="member",
                subject=_user(user),
            )
        ]
    )


def test_const_directive_parses_to_const_binding():
    definition = parse_zed(SCHEMA_TEXT).get_definition("blog/post")
    assert definition is not None
    admin = next(r for r in definition.relations if r.name == "admin")
    assert admin.backing == ConstBinding(target_id="admin")


def test_const_arrow_grants_every_row_from_one_membership_without_tuples(backend, posts):
    _grant_admin(backend, "alice")

    for post in posts:
        assert backend.has_access(subject=_user("alice"), action="read", resource=_post_ref(post))

    # The reach came from one org/role membership row — never a per-post tuple.
    assert Relationship.objects.filter(resource_type="org/role", relation="member").count() == 1
    assert not Relationship.objects.filter(resource_type="blog/post").exists()


def test_const_arrow_denies_non_member(backend, posts):
    _grant_admin(backend, "alice")
    assert not backend.has_access(subject=_user("bob"), action="read", resource=_post_ref(posts[0]))


def test_accessible_returns_all_rows_when_const_target_grants(backend, posts):
    _grant_admin(backend, "alice")
    # bob is not an admin but owns one post directly.
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=_post_ref(posts[0]),
                relation="owner",
                subject=_user("bob"),
            )
        ]
    )

    alice_ids = set(
        backend.accessible(subject=_user("alice"), action="read", resource_type="blog/post")
    )
    bob_ids = set(
        backend.accessible(subject=_user("bob"), action="read", resource_type="blog/post")
    )

    assert alice_ids == {str(p.pk) for p in posts}  # const arrow → every row
    assert bob_ids == {str(posts[0].pk)}  # owner grant only


def test_grants_all_short_circuits_const_arrow_without_enumeration(backend, posts):
    # A granting const arrow covers the whole type, so the queryset layer must
    # take the unrestricted path (no id__in filter) rather than enumerate every
    # row. A subject who is not the const target's member gets no blanket grant.
    _grant_admin(backend, "alice")
    assert (
        backend.grants_all(subject=_user("alice"), action="read", resource_type="blog/post") is True
    )
    assert (
        backend.grants_all(subject=_user("bob"), action="read", resource_type="blog/post") is False
    )


def test_tuple_writes_to_const_relation_are_rejected(backend, posts):
    with pytest.raises(SchemaError, match=r"const-backed.*synthetic"):
        backend.write_relationships(
            [
                RelationshipTuple(
                    resource=_post_ref(posts[0]),
                    relation="admin",
                    subject=SubjectRef.of("org/role", "admin"),
                )
            ]
        )

    with pytest.raises(SchemaError, match=r"const-backed.*synthetic"):
        backend.delete_relationships(
            RelationshipFilter(
                resource_type="blog/post",
                resource_id=str(posts[0].pk),
                relation="admin",
            )
        )


def test_db_loaded_schema_preserves_const_backing(db, posts):
    post_def = SchemaDefinition.objects.create(resource_type="blog/post")
    SchemaRelation.objects.create(
        definition=post_def,
        name="owner",
        allowed_subjects=[{"type": "auth/user"}],
    )
    SchemaRelation.objects.create(
        definition=post_def,
        name="admin",
        allowed_subjects=[{"type": "org/role"}],
        backing={"kind": "const", "target_id": "admin"},
    )
    SchemaPermission.objects.create(
        definition=post_def,
        name="read",
        expression="owner + admin->member",
    )
    role_def = SchemaDefinition.objects.create(resource_type="org/role")
    SchemaRelation.objects.create(
        definition=role_def,
        name="member",
        allowed_subjects=[{"type": "auth/user"}],
    )
    SchemaDefinition.objects.create(resource_type="auth/user")

    backend = LocalBackend()
    _grant_admin(backend, "alice")

    assert backend.has_access(subject=_user("alice"), action="read", resource=_post_ref(posts[0]))


def test_system_check_reports_const_relation_without_model(db):
    from rebac.backends import backend as active_backend
    from rebac.backends import reset_backend
    from rebac.checks import check_field_backed_relations

    reset_backend()
    active_backend().set_schema(
        parse_zed(
            """
            definition org/role { relation member: auth/user }
            definition ghost/thing {
                relation admin: org/role // rebac:const=admin
            }
            """
        )
    )

    issues = check_field_backed_relations()

    assert any(
        issue.id == "rebac.E009" and "const-backed relation requires a Django model" in issue.msg
        for issue in issues
    )


# A const relation referenced *directly* in a permission (no `->` arrow). The
# relation is held by the fixed target org/role:admin itself, so that subject —
# and only it — satisfies the permission; reaching role *members* still needs
# the arrow form. The direct form used to fall through to an always-empty
# stored-tuple query and silently deny even the const target.
DIRECT_SCHEMA_TEXT = """
definition auth/user {}

definition org/role {
    relation member: auth/user
}

definition blog/post {
    relation admin: org/role // rebac:const=admin

    permission manage = admin
}
"""


@pytest.fixture
def direct_backend(db):
    b = LocalBackend()
    b.set_schema(parse_zed(DIRECT_SCHEMA_TEXT))
    return b


def _role(id_: str) -> SubjectRef:
    return SubjectRef.of("org/role", id_)


def test_direct_const_reference_grants_only_the_const_target_subject(direct_backend, posts):
    for post in posts:
        assert direct_backend.has_access(
            subject=_role("admin"), action="manage", resource=_post_ref(post)
        )
    # A different role id is not the const target.
    assert not direct_backend.has_access(
        subject=_role("other"), action="manage", resource=_post_ref(posts[0])
    )
    # The direct form does not traverse membership — a user is not the target.
    assert not direct_backend.has_access(
        subject=_user("alice"), action="manage", resource=_post_ref(posts[0])
    )
    assert not Relationship.objects.filter(resource_type="blog/post").exists()


def test_direct_const_reference_accessible_returns_all_rows_for_const_target(direct_backend, posts):
    ids = set(
        direct_backend.accessible(
            subject=_role("admin"), action="manage", resource_type="blog/post"
        )
    )
    assert ids == {str(p.pk) for p in posts}
    # A non-target subject reaches nothing.
    assert not direct_backend.accessible(
        subject=_role("other"), action="manage", resource_type="blog/post"
    )


def test_direct_const_reference_lookup_subjects_returns_const_target(direct_backend, posts):
    subjects = list(
        direct_backend.lookup_subjects(
            resource=_post_ref(posts[0]), action="manage", subject_type="org/role"
        )
    )
    assert subjects == [_role("admin")]


def test_const_reverse_enumerates_via_base_manager(backend, posts):
    # The reverse (accessible) walk enumerates every row through the unscoped
    # _base_manager, independent of the scoped default `objects` manager.
    _grant_admin(backend, "alice")
    ids = set(backend.accessible(subject=_user("alice"), action="read", resource_type="blog/post"))
    assert ids == {str(pk) for pk in Post._base_manager.values_list("pk", flat=True)}
    assert len(ids) == Post._base_manager.count() == len(posts)


def test_system_check_reports_const_target_without_definition(db):
    # A const relation's target type must resolve to a schema definition;
    # a typo (here `org/role` is simply never defined) would otherwise make the
    # arrow walk silently deny. blog/post has a Django model, so the only issue
    # is the undefined target.
    from rebac.backends import backend as active_backend
    from rebac.backends import reset_backend
    from rebac.checks import check_field_backed_relations

    reset_backend()
    active_backend().set_schema(
        parse_zed(
            """
            definition blog/post {
                relation admin: org/role // rebac:const=admin
                permission read = admin->member
            }
            """
        )
    )

    issues = check_field_backed_relations()

    assert any(
        issue.id == "rebac.E009"
        and "has no schema definition" in issue.msg
        and "org/role" in issue.msg
        for issue in issues
    )


def test_system_check_reports_const_arrow_cycle(db):
    # Two types whose const arrows point at each other recurse to the depth
    # limit on every check; the E010 check catches it at load instead.
    from rebac.backends import backend as active_backend
    from rebac.backends import reset_backend
    from rebac.checks import check_field_backed_relations

    reset_backend()
    active_backend().set_schema(
        parse_zed(
            """
            definition blog/post {
                relation peer: blog/folder // rebac:const=x
                permission p = peer->p
            }
            definition blog/folder {
                relation peer: blog/post // rebac:const=x
                permission p = peer->p
            }
            """
        )
    )

    issues = check_field_backed_relations()

    assert any(issue.id == "rebac.E010" and "arrow cycle" in issue.msg for issue in issues)


def test_system_check_passes_for_acyclic_const_arrow(db):
    # Guard against false positives: the canonical admin->member schema has no
    # const-arrow cycle and an org/role definition, so neither E010 nor the
    # target-type E009 fires.
    from rebac.backends import backend as active_backend
    from rebac.backends import reset_backend
    from rebac.checks import check_field_backed_relations

    reset_backend()
    active_backend().set_schema(parse_zed(SCHEMA_TEXT))

    issues = check_field_backed_relations()

    assert not any(issue.id == "rebac.E010" for issue in issues)
    assert not any("has no schema definition" in issue.msg for issue in issues)
