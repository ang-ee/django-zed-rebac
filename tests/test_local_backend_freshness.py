"""LocalBackend's ``at_least_as_fresh`` read contract.

The LocalBackend uses ``Relationship.written_at_xid`` as its freshness
witness — ``Zookie.token`` carries the xid; reads with
``Consistency.AT_LEAST_AS_FRESH(zookie)`` retain newer writes.
"""

from __future__ import annotations

import pytest

from rebac import (
    LocalBackend,
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    Zookie,
)
from rebac.schema import parse_zed

SCHEMA = """
definition auth/user {}
definition blog/post {
    relation owner: auth/user
    permission read = owner
}
"""


@pytest.fixture
def backend(db):
    b = LocalBackend()
    b.set_schema(parse_zed(SCHEMA))
    return b


def _user(id_: str) -> SubjectRef:
    return SubjectRef.of("auth/user", id_)


def _post(id_: str) -> ObjectRef:
    return ObjectRef("blog/post", id_)


def test_zookie_kind_mismatch_raises(backend):
    """A SpiceDB-emitted zookie handed to LocalBackend must fail loudly."""
    bad = Zookie("spicedb", "ZTk3Y2VkZjQ=")
    with pytest.raises(ValueError, match="cannot consume a Zookie from backend"):
        backend.has_access(
            subject=_user("u1"),
            action="read",
            resource=_post("p1"),
            at_zookie=bad,
        )


def test_zookie_with_non_numeric_token_raises(backend):
    bad = Zookie("local", "not-a-number")
    with pytest.raises(ValueError, match="numeric xid"):
        backend.has_access(
            subject=_user("u1"),
            action="read",
            resource=_post("p1"),
            at_zookie=bad,
        )


def test_at_least_as_fresh_includes_later_writes(backend):
    """A token is a freshness floor, not a historical snapshot."""
    # First write — capture the resulting Zookie.
    z1 = backend.write_relationships(
        [
            RelationshipTuple(
                resource=_post("p1"),
                relation="owner",
                subject=_user("u1"),
            ),
        ]
    )
    # Second write — strictly later xid.
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=_post("p2"),
                relation="owner",
                subject=_user("u1"),
            ),
        ]
    )
    # Read without Zookie sees both posts.
    all_ids = set(backend.accessible(subject=_user("u1"), action="read", resource_type="blog/post"))
    assert all_ids == {"p1", "p2"}
    # A read carrying z1 sees newer writes as well.
    pinned = set(
        backend.accessible(
            subject=_user("u1"),
            action="read",
            resource_type="blog/post",
            at_zookie=z1,
        )
    )
    assert pinned == {"p1", "p2"}


def test_at_least_as_fresh_applies_to_check_access(backend):
    """``check_access`` retains newer writes through the same internal walk."""
    z1 = backend.write_relationships(
        [
            RelationshipTuple(
                resource=_post("p1"),
                relation="owner",
                subject=_user("u1"),
            ),
        ]
    )
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=_post("p2"),
                relation="owner",
                subject=_user("u1"),
            ),
        ]
    )
    # p2 was created after z1 and remains visible to a read carrying z1.
    assert backend.has_access(
        subject=_user("u1"), action="read", resource=_post("p1"), at_zookie=z1
    )
    assert backend.has_access(
        subject=_user("u1"), action="read", resource=_post("p2"), at_zookie=z1
    )
    # Without a token both pass too.
    assert backend.has_access(subject=_user("u1"), action="read", resource=_post("p2"))


def test_write_zookie_kind_is_local(backend):
    z = backend.write_relationships(
        [
            RelationshipTuple(
                resource=_post("p1"),
                relation="owner",
                subject=_user("u1"),
            ),
        ]
    )
    assert z.backend == "local"
    assert z.token.isdigit()


def test_delete_returns_zookie(backend):
    from rebac.types import RelationshipFilter

    backend.write_relationships(
        [
            RelationshipTuple(
                resource=_post("p1"),
                relation="owner",
                subject=_user("u1"),
            ),
        ]
    )
    z = backend.delete_relationships(RelationshipFilter(resource_type="blog/post"))
    assert z.backend == "local"
    assert z.token.isdigit()


def test_write_zookie_is_batch_high_watermark(backend, django_assert_num_queries=None):
    """The returned Zookie's token equals the max ``written_at_xid`` of the
    batch — NOT a phantom xid past it. Reads pinned to this Zookie must
    see every row produced by the write and retain newer rows.

    Regression for the freshness contract: an earlier implementation
    consumed an extra xid in ``_zookie()`` after the loop, making the
    returned token strictly greater than every row. Tests happened to
    pass because the next batch's xids were also strictly greater, but
    the contract wasn't actually being held.
    """
    from rebac.models import active_relationship_model

    z = backend.write_relationships(
        [
            RelationshipTuple(
                resource=_post(f"p{i}"),
                relation="owner",
                subject=_user("u1"),
            )
            for i in range(3)
        ]
    )
    Rel = active_relationship_model()
    max_row_xid = max(r.written_at_xid for r in Rel.objects.filter(resource_type="blog/post"))
    # Token equals the batch's max xid — not strictly greater.
    assert int(z.token) == max_row_xid
