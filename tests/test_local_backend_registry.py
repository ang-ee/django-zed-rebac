"""LocalBackend parity under ``REBAC_LOCAL_BACKEND_STORAGE='registry'``.

Mirrors the read/write scenarios from ``test_local_backend.py`` with the
registry storage shape active. The original test module pins the
denormalized contract; this one pins the registry contract. They share
the SAME schema (imported below) so any divergence in evaluation between
the two backends surfaces as a test mismatch.

Intentional duplication: pulling the original tests through pytest
parametrize would invalidate existing test-id selectors used in CI
filters. Two compact test modules is the cleaner trade.
"""

from __future__ import annotations

import pytest
from django.test.utils import override_settings

from rebac import LocalBackend, ObjectRef, RelationshipTuple, SubjectRef
from rebac.models import RebacResource, Relationship, RelationshipRegistry
from rebac.schema import parse_zed
from tests.test_local_backend import SCHEMA_TEXT


def _user(id_: str) -> SubjectRef:
    return SubjectRef.of("auth/user", id_)


def _group(id_: str) -> SubjectRef:
    return SubjectRef.of("auth/group", id_, "member")


def _post(id_: str) -> ObjectRef:
    return ObjectRef("blog/post", id_)


def _folder(id_: str) -> ObjectRef:
    return ObjectRef("blog/folder", id_)


@pytest.fixture
def backend(db):
    """LocalBackend bound to the registry-mode active table."""
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE="registry"):
        b = LocalBackend()
        b.set_schema(parse_zed(SCHEMA_TEXT))
        yield b


# ---------- Parity with the denormalized suite ----------


def test_owner_has_read_and_write(backend):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE="registry"):
        backend.write_relationships(
            [
                RelationshipTuple(resource=_post("p1"), relation="owner", subject=_user("u1")),
            ]
        )
        assert backend.has_access(subject=_user("u1"), action="read", resource=_post("p1"))
        assert backend.has_access(subject=_user("u1"), action="write", resource=_post("p1"))
        assert not backend.has_access(subject=_user("u2"), action="read", resource=_post("p1"))


def test_viewer_can_read_but_not_write(backend):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE="registry"):
        backend.write_relationships(
            [
                RelationshipTuple(resource=_post("p2"), relation="viewer", subject=_user("u3")),
            ]
        )
        assert backend.has_access(subject=_user("u3"), action="read", resource=_post("p2"))
        assert not backend.has_access(subject=_user("u3"), action="write", resource=_post("p2"))


def test_group_membership_inherits_read(backend):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE="registry"):
        backend.write_relationships(
            [
                # auth/group:g1 has u4 as a member.
                RelationshipTuple(
                    resource=ObjectRef("auth/group", "g1"),
                    relation="member",
                    subject=_user("u4"),
                ),
                # blog/post:p3 grants viewer to anyone in g1's member set.
                RelationshipTuple(resource=_post("p3"), relation="viewer", subject=_group("g1")),
            ]
        )
        assert backend.has_access(subject=_user("u4"), action="read", resource=_post("p3"))


def test_wildcard_grants_read_to_anyone(backend):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE="registry"):
        backend.write_relationships(
            [
                RelationshipTuple(
                    resource=_post("public"),
                    relation="viewer",
                    subject=SubjectRef.of("auth/user", "*"),
                ),
            ]
        )
        assert backend.has_access(subject=_user("anyone"), action="read", resource=_post("public"))


def test_arrow_propagates_via_folder(backend):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE="registry"):
        backend.write_relationships(
            [
                RelationshipTuple(resource=_folder("f1"), relation="owner", subject=_user("u9")),
                RelationshipTuple(
                    resource=_post("p9"),
                    relation="folder",
                    subject=SubjectRef(object=_folder("f1")),
                ),
            ]
        )
        assert backend.has_access(subject=_user("u9"), action="read", resource=_post("p9"))


def test_accessible_returns_owned_resources(backend):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE="registry"):
        backend.write_relationships(
            [
                RelationshipTuple(resource=_post("a"), relation="owner", subject=_user("u")),
                RelationshipTuple(resource=_post("b"), relation="owner", subject=_user("u")),
            ]
        )
        ids = set(backend.accessible(subject=_user("u"), action="read", resource_type="blog/post"))
        assert ids == {"a", "b"}


# ---------- Registry-specific assertions ----------


def test_writes_land_in_registry_not_denormalized(backend):
    """Sanity: tuples written via the engine in registry mode go into
    RelationshipRegistry, not Relationship."""
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE="registry"):
        backend.write_relationships(
            [
                RelationshipTuple(
                    resource=_post("p_reg"), relation="owner", subject=_user("u_reg")
                ),
            ]
        )

    assert RelationshipRegistry.objects.filter(
        resource_type="blog/post", resource_id="p_reg", relation="owner"
    ).exists()
    assert not Relationship.objects.filter(resource_id="p_reg").exists()
    assert RebacResource.objects.filter(resource_type="blog/post", resource_id="p_reg").exists()
    assert RebacResource.objects.filter(resource_type="auth/user", resource_id="u_reg").exists()
