"""LocalBackend's ``at_least_as_fresh`` filter.

The LocalBackend uses ``Relationship.written_at_xid`` as its freshness
witness — ``Zookie.token`` carries the xid; reads with
``Consistency.AT_LEAST_AS_FRESH(zookie)`` filter
``written_at_xid <= cutoff``.
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


def test_at_least_as_fresh_excludes_later_writes(backend):
    """A read pinned to an early xid does not see writes that came after."""
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
    # Read pinned to z1 sees ONLY p1 — p2's xid is strictly greater
    # than z1's xid and is excluded by ``written_at_xid <= cutoff``.
    pinned = set(
        backend.accessible(
            subject=_user("u1"),
            action="read",
            resource_type="blog/post",
            at_zookie=z1,
        )
    )
    assert pinned == {"p1"}


def test_at_least_as_fresh_applies_to_check_access(backend):
    """``check_access`` honours the cutoff via the same internal walk."""
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
    # p2 created AFTER z1 — pinned read says NO.
    assert backend.has_access(
        subject=_user("u1"), action="read", resource=_post("p1"), at_zookie=z1
    )
    assert not backend.has_access(
        subject=_user("u1"), action="read", resource=_post("p2"), at_zookie=z1
    )
    # Without the cutoff both pass.
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
    see every row produced by the write and exclude any strictly-later
    rows.

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
