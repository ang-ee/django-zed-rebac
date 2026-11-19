"""``read_snapshot`` (proposal 0020): one transaction for a unit of reading, in which decisions are kept."""

from __future__ import annotations

import pytest
from django.db import connection, transaction

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope, snapshot
from rebac.snapshot import SnapshotUnavailable, read_snapshot
from tests.test_constant_arms import ADMIN, _read, console  # noqa: F401

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pg_delta]
GRANT = RelationshipTuple(ObjectRef("blog/post", "1"), "viewer", SubjectRef.of("auth/user", "x"))


def witnesses(statements: list[str]) -> int:
    """The statements that decide a constant arm: neither the profile nor the row read."""
    return sum("rebac_profile" not in sql and "testapp_post" not in sql for sql in statements)


def reads(count: int = 2) -> list[list[str]]:
    return [_read(ADMIN)[1] for _ in range(count)]


def test_outside_a_transaction_a_decision_is_kept_for_the_scope(console):  # noqa: F811
    with evaluator_scope():
        first, second = reads()
    assert witnesses(first) == 1 and witnesses(second) == 0


def test_inside_a_plain_transaction_no_decision_is_kept(console):  # noqa: F811
    """A rollback would leave a kept decision stale, so each read decides again."""
    with evaluator_scope(), transaction.atomic():
        first, second = reads()
    assert witnesses(first) == 1 and witnesses(second) == 1


def test_inside_a_read_snapshot_a_decision_is_kept(console):  # noqa: F811
    with evaluator_scope(), read_snapshot():
        assert snapshot.covers("default") and connection.in_atomic_block
        first, second = reads()
    assert not snapshot.covers("default") and not connection.in_atomic_block
    assert witnesses(first) == 1 and witnesses(second) == 0


def test_a_snapshot_joins_the_transaction_already_open(console):  # noqa: F811
    with evaluator_scope(), transaction.atomic(), read_snapshot(), read_snapshot():
        first, second = reads()
    assert witnesses(first) == 1 and witnesses(second) == 0


def test_a_library_write_inside_ends_the_snapshots_standing(console):  # noqa: F811
    local, _folders, _posts = console
    with evaluator_scope(), read_snapshot():
        local.write_relationships([GRANT])
        assert not snapshot.covers("default")
        first, second = reads()
    assert witnesses(first) == 1 and witnesses(second) == 1


def test_a_transaction_that_has_written_relationships_cannot_become_a_snapshot(console):  # noqa: F811
    """Those rows may still roll back, and with them whatever would be decided from them."""
    local, _folders, _posts = console
    with transaction.atomic():
        local.write_relationships([GRANT])
        with pytest.raises(SnapshotUnavailable), read_snapshot():
            pass


def test_isolation_is_reported_from_the_connections_configuration():
    """SQLite's transactions each read one snapshot. PostgreSQL's do when the connection is
    configured for repeatable read or serializable; the test database uses the default."""
    assert snapshot.isolated("default") == (connection.vendor == "sqlite")
