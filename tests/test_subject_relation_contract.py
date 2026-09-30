"""Relationship subjects name relations, matching SpiceDB's wire contract."""

from unittest.mock import patch

import pytest

from rebac import LocalBackend, ObjectRef, RelationshipTuple, SchemaError, SubjectRef
from rebac.models import active_relationship_model
from rebac.preflight import check_new
from rebac.schema import parse_zed, validate_schema
from tests.backend_setup import install_schema
from tests.testapp.models import Post


def test_subject_set_rejects_permission_name_when_target_is_present() -> None:
    schema = parse_zed(
        """
        definition auth/user {}
        definition auth/group {
            relation member: auth/user
            permission effective_member = member
        }
        definition docs/document {
            relation viewer: auth/group#effective_member
        }
        """
    )

    errors = validate_schema(schema)
    assert errors == [
        "docs/document#viewer: subject relation auth/group#effective_member names a permission; "
        "relationship subjects may reference relations only. Store a direct object in the "
        "relation, use relation->permission in the resource permission, and migrate existing "
        "tuples together with the schema"
    ]
    with pytest.raises(SchemaError, match="migrate existing tuples together with the schema"):
        install_schema(LocalBackend(), schema)


def test_subject_set_accepts_declared_relation() -> None:
    schema = parse_zed(
        """
        definition auth/user {}
        definition auth/group {
            relation member: auth/user
            permission effective_member = member
        }
        definition docs/document {
            relation viewer: auth/group#member
        }
        """
    )

    assert validate_schema(schema) == []


def test_subject_set_rejects_unknown_relation_when_target_is_present() -> None:
    schema = parse_zed(
        """
        definition auth/user {}
        definition auth/group {
            relation member: auth/user
        }
        definition docs/document {
            relation viewer: auth/group#missing
        }
        """
    )

    errors = validate_schema(schema)
    assert errors == [
        "docs/document#viewer: subject relation auth/group#missing does not name a declared "
        "relation; relationship subjects may reference relations only. Store a direct object "
        "in the relation, use relation->permission in the resource permission, and migrate "
        "existing tuples together with the schema"
    ]


def test_unresolved_cross_package_subject_type_is_deferred() -> None:
    schema = parse_zed(
        """
        definition docs/document {
            relation viewer: accounts/group#member
        }
        """
    )

    assert validate_schema(schema) == []


@pytest.mark.django_db
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_invalid_ast_cannot_write_or_authorize_permission_subject_tuple(
    settings, monkeypatch, storage: str
) -> None:
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    monkeypatch.setattr(Post._meta, "rebac_resource_type", "docs/document")
    schema = parse_zed(
        """
        definition auth/user {
            relation member: auth/user
            permission effective_member = member
        }
        definition docs/document {
            relation viewer: auth/user#effective_member
            permission read = viewer
        }
        """
    )
    local = LocalBackend()
    # Exercise the defensive write/read boundary independently of the schema
    # validator, as if malformed persisted AST data reached the backend.
    with patch("rebac.backends.local._enforced_schema_errors", return_value=[]):
        install_schema(local, schema)
    from rebac import sudo

    with sudo(reason="invalid subject-set fixture"):
        post = Post.objects.create(title="protected")
    user = SubjectRef.of("auth/user", "alice")
    local.write_relationships([RelationshipTuple(ObjectRef("auth/user", "set"), "member", user)])
    invalid = RelationshipTuple(
        ObjectRef("docs/document", str(post.pk)),
        "viewer",
        SubjectRef.of("auth/user", "set", "effective_member"),
    )

    with pytest.raises(ValueError, match="cannot reference permissions"):
        local.write_relationships([invalid])

    from rebac.index.read import scope_q, using_backend

    with using_backend(local):
        assert not Post._base_manager.filter(
            scope_q(Post, action="read", actor=user, using="default")
        ).exists()

    preflight = check_new(
        subject=user,
        action="read",
        resource_type="docs/document",
        relationships={"viewer": [invalid.subject]},
        backend=local,
    )
    assert not preflight.allowed

    active_relationship_model().objects.create(
        resource_type="docs/document",
        resource_id=str(post.pk),
        relation="viewer",
        subject_type="auth/user",
        subject_id="set",
        optional_subject_relation="effective_member",
    )
    from tests.backend_setup import rebuild_backend

    rebuild_backend(local)
    assert not local.has_access(
        subject=user,
        action="read",
        resource=ObjectRef("docs/document", str(post.pk)),
    )
