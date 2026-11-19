"""``read_snapshot`` (proposal 0020): one consistent transaction in which decisions are kept."""

from __future__ import annotations

import pytest
from django.db import connection, transaction

from rebac import SnapshotUnavailable, evaluator_scope, read_snapshot, snapshot
from tests.test_constant_arms import ADMIN, _read, console  # noqa: F401
from tests.testapp.models import Folder

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pg_delta]


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
        assert snapshot.covers("default")
        first, second = reads()
    assert not snapshot.covers("default")
    assert witnesses(first) == 1 and witnesses(second) == 0


def test_a_snapshot_entered_twice_is_one_snapshot(console):  # noqa: F811
    with evaluator_scope(), read_snapshot(), read_snapshot():
        first, second = reads()
    assert witnesses(first) == 1 and witnesses(second) == 0


def test_a_library_write_inside_ends_the_snapshots_standing(console):  # noqa: F811
    """SQLite cannot refuse the write. PostgreSQL refuses it: the transaction is read-only."""
    local, _folders, _posts = console
    from rebac import ObjectRef, RelationshipTuple, SubjectRef

    grant = RelationshipTuple(
        ObjectRef("blog/post", "1"), "viewer", SubjectRef.of("auth/user", "x")
    )
    if connection.vendor == "postgresql":
        from django.db import InternalError

        with pytest.raises(InternalError), read_snapshot():
            local.write_relationships([grant])
        return
    with evaluator_scope(), read_snapshot():
        local.write_relationships([grant])
        assert not snapshot.covers("default")
        first, second = reads()
    assert witnesses(first) == 1 and witnesses(second) == 1


@pytest.mark.postgresql
def test_on_postgresql_the_snapshot_is_repeatable_read_and_read_only(console):  # noqa: F811
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL isolation levels")
    from django.db import InternalError

    with read_snapshot(), connection.cursor() as cursor:
        cursor.execute("SHOW transaction_isolation")
        assert cursor.fetchone()[0] == "repeatable read"
        cursor.execute("SHOW transaction_read_only")
        assert cursor.fetchone()[0] == "on"
    table = connection.ops.quote_name(Folder._meta.db_table)
    with pytest.raises(InternalError), read_snapshot(), connection.cursor() as cursor:
        cursor.execute(f"UPDATE {table} SET name = name")


@pytest.mark.postgresql
def test_on_postgresql_a_plain_open_transaction_cannot_become_a_snapshot(console):  # noqa: F811
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL isolation levels")
    with transaction.atomic():
        with pytest.raises(SnapshotUnavailable), read_snapshot():
            pass
