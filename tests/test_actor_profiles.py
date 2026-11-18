"""Actor grant profiles (proposal 0019): what is left out, when it is read again, and that a stale one never admits."""

from __future__ import annotations

import random

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope
from rebac.compile import read
from rebac.models import Relationship
from rebac.testing import install_schema
from rebac.types import Consistency
from tests.test_inherited_witness_reference import build
from tests.testapp.models import ScopeMessage, ScopePart, ScopeThread

MEMBER = SubjectRef.of("auth/user", "member")
ADMIN = SubjectRef.of("auth/user", "admin")
KEPT = pytest.mark.django_db(transaction=True)

SCHEMA = """
definition auth/user {}
definition auth/group { relation member: auth/user | auth/group#member }
definition scope/thread {
    relation reader: auth/user | auth/group#member
    relation blocked: auth/user
    relation pub: auth/user:*
    permission read = (reader + pub) - blocked
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission read = thread->read + direct
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
}
"""


def grant(
    type_: str, id_: object, relation: str, subject: SubjectRef = MEMBER
) -> RelationshipTuple:
    return RelationshipTuple(ObjectRef(type_, str(id_)), relation, subject)


@pytest.fixture
def threads():
    """Two threads, three messages (two in the first thread) and a part of each message."""
    local = install_schema(SCHEMA)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1), ScopeThread(id=2)])
    ScopeMessage._base_manager.bulk_create(
        [
            ScopeMessage(id=1, thread_id=1),
            ScopeMessage(id=2, thread_id=1),
            ScopeMessage(id=3, thread_id=2),
        ]
    )
    ScopePart._base_manager.bulk_create(
        [ScopePart(id=n, message_id=n, fragment_id=n) for n in (1, 2, 3)]
    )
    return local


def messages(actor: SubjectRef = MEMBER) -> tuple[list[int], list[str]]:
    with CaptureQueriesContext(connection) as captured:
        found = sorted(ScopeMessage.objects.with_actor(actor).values_list("pk", flat=True))
    return found, [query["sql"] for query in captured.captured_queries]


def behind_the_library(type_: str, id_: object, relation: str, subject_id: str = "member") -> None:
    """Write a relationship row as another process would: no library hook runs."""
    table = connection.ops.quote_name(Relationship._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table} (resource_type, resource_id, relation, subject_type, subject_id, "
            "optional_subject_relation, caveat_name, caveat_context, caveat_key, written_at_xid) "
            "VALUES (%s, %s, %s, 'auth/user', %s, '', '', NULL, '', 0)",
            [type_, str(id_), relation, subject_id],
        )


def profile_statements(statements: list[str]) -> int:
    return sum("rebac_profile" in sql for sql in statements)


@KEPT
def test_the_profile_is_read_once_in_a_scope(threads):
    threads.write_relationships([grant("scope/thread", 1, "reader")])
    with evaluator_scope():
        first, statements = messages()
        again, repeated = messages()
    assert first == again == [1, 2]
    assert profile_statements(statements) == 1
    assert profile_statements(repeated) == 0


@KEPT
def test_a_relation_the_actor_holds_no_row_of_is_not_compiled(threads):
    threads.write_relationships([grant("scope/thread", 1, "reader")])
    outside, plain = messages()
    with evaluator_scope():
        inside, narrowed = messages()
    assert inside == outside == [1, 2]
    # The member holds no direct grant on a message: that arm reads no relationship row.
    assert "'direct'" in plain[-1]
    assert "'direct'" not in narrowed[-1]
    assert "'reader'" in narrowed[-1]


@KEPT
def test_outside_a_scope_the_statement_is_the_same_with_and_without_profiles(threads, settings):
    threads.write_relationships([grant("scope/thread", 1, "reader")])
    _ids, with_profiles = messages()
    settings.REBAC_ACTOR_PROFILES = False
    read.reset()
    _ids, without = messages()
    assert with_profiles == without


@KEPT
def test_profiles_can_be_turned_off(threads, settings):
    settings.REBAC_ACTOR_PROFILES = False
    read.reset()
    with evaluator_scope():
        found, statements = messages()
    assert found == []
    assert profile_statements(statements) == 0
    assert "'direct'" in statements[-1]


@KEPT
def test_a_grant_written_through_the_library_is_seen_by_the_next_read(threads):
    with evaluator_scope():
        assert messages()[0] == []
        threads.write_relationships([grant("scope/message", 3, "direct")])
        found, statements = messages()
    assert found == [3]
    assert profile_statements(statements) == 1


@KEPT
def test_a_grant_written_behind_the_library_is_seen_by_a_check_and_then_by_reads(threads):
    with evaluator_scope():
        assert messages()[0] == []
        behind_the_library("scope/message", 3, "direct")
        # A read may stay narrower until the scope ends; it is never wider.
        assert messages()[0] == []
        # A check is exact: it reads the profile again before it answers no.
        assert threads.check_access(
            subject=MEMBER, action="read", resource=ObjectRef("scope/message", "3")
        ).allowed
        assert messages()[0] == [3]
    with evaluator_scope():
        assert messages()[0] == [3]


@KEPT
def test_an_explicit_consistency_requirement_reads_the_profile_now(threads):
    with evaluator_scope():
        assert messages()[0] == []
        behind_the_library("scope/message", 3, "direct")
        assert "3" in threads.accessible(
            subject=MEMBER,
            action="read",
            resource_type="scope/message",
            consistency=Consistency.FULLY_CONSISTENT,
        )


@KEPT
def test_a_subtracted_relation_is_compiled_whatever_the_profile(threads):
    threads.write_relationships([grant("scope/thread", 1, "reader")])
    with evaluator_scope():
        assert messages()[0] == [1, 2]
        # The member held no ``blocked`` row when the profile was read. Leaving
        # that arm out would keep admitting them.
        behind_the_library("scope/thread", 1, "blocked")
        assert messages()[0] == []
        assert not threads.check_access(
            subject=MEMBER, action="read", resource=ObjectRef("scope/message", "1")
        ).allowed


@KEPT
def test_a_revocation_is_seen_at_once(threads):
    reader = grant("scope/thread", 1, "reader")
    threads.write_relationships([reader])
    with evaluator_scope():
        assert messages()[0] == [1, 2]
        table = connection.ops.quote_name(Relationship._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table} WHERE relation = 'reader'")
        assert messages()[0] == []


@KEPT
def test_a_rolled_back_revocation_leaves_the_grant_readable(threads):
    reader = grant("scope/thread", 1, "reader")
    threads.write_relationships([reader])
    with evaluator_scope():
        assert messages()[0] == [1, 2]
        with pytest.raises(RuntimeError), transaction.atomic():
            threads.delete_relationship(reader)
            assert messages()[0] == []
            raise RuntimeError("roll back")
        assert messages()[0] == [1, 2]


@KEPT
def test_a_rolled_back_grant_is_not_readable(threads):
    with evaluator_scope():
        assert messages()[0] == []
        with pytest.raises(RuntimeError), transaction.atomic():
            threads.write_relationships([grant("scope/message", 3, "direct")])
            assert messages()[0] == [3]
            raise RuntimeError("roll back")
        assert messages()[0] == []


@KEPT
def test_a_transaction_after_a_committed_grant_keeps_its_profile(threads):
    """Each request a transaction with a scope of its own, as under ``ATOMIC_REQUESTS``."""
    with transaction.atomic(), evaluator_scope():
        threads.write_relationships([grant("scope/message", 3, "direct")])
    # The grant is committed: a transaction that writes nothing keeps its profile.
    with transaction.atomic(), evaluator_scope():
        found, statements = messages()
        again, repeated = messages()
    assert found == again == [3]
    assert profile_statements(statements) == 1
    assert profile_statements(repeated) == 0


@KEPT
def test_a_transaction_after_a_rolled_back_grant_keeps_its_profile(threads):
    with pytest.raises(RuntimeError), transaction.atomic(), evaluator_scope():
        threads.write_relationships([grant("scope/message", 3, "direct")])
        raise RuntimeError("roll back")
    # The rolled-back transaction is over: the next one keeps its profile.
    with transaction.atomic(), evaluator_scope():
        found, statements = messages()
        again, repeated = messages()
    assert found == again == []
    assert profile_statements(statements) == 1
    assert profile_statements(repeated) == 0


@KEPT
def test_while_a_written_grant_is_uncommitted_the_profile_is_read_at_each_compile(threads):
    with evaluator_scope():
        with transaction.atomic():
            threads.write_relationships([grant("scope/message", 3, "direct")])
            found, statements = messages()
            again, repeated = messages()
        # Committed: the scope keeps the profile again.
        after, committed = messages()
        last, kept = messages()
    assert found == again == after == last == [3]
    # A read compiles twice: its scope when the queryset is built, which pins
    # the policy and decides the constant arms, and its statement when the SQL
    # is compiled. Neither keeps the profile while the grant may roll back.
    assert profile_statements(statements) == profile_statements(repeated) == 2
    assert profile_statements(committed) == 1
    assert profile_statements(kept) == 0


@KEPT
def test_a_check_that_stays_denied_reads_a_moved_profile_once(threads):
    with evaluator_scope():
        assert messages()[0] == []
        # A grant on another message: the profile moves, the check's answer does not.
        behind_the_library("scope/message", 3, "direct")
        with CaptureQueriesContext(connection) as checked:
            assert not threads.check_access(
                subject=MEMBER, action="read", resource=ObjectRef("scope/message", "1")
            ).allowed
        # The scope keeps the profile the check read.
        found, statements = messages()
    # The lower bound for the profile the scope kept; the profile, read again
    # before the check answers no; then the decision made again for the profile
    # that moved, which reads none: its lower bound, and its upper bound, as a
    # recursion (nested groups) is in reach.
    assert len(checked.captured_queries) == 4
    assert profile_statements([query["sql"] for query in checked.captured_queries]) == 1
    assert found == [3]
    assert profile_statements(statements) == 0


@KEPT
@pytest.mark.postgresql
@pytest.mark.parametrize("revoked", ["before the block", "inside the block"])
def test_a_revocation_rolled_back_by_the_application_leaves_the_grant_readable(threads, revoked):
    """With autocommit off no ``atomic`` block owns the commit: a profile read from the
    uncommitted revocation is not kept, inside a block or after it.

    PostgreSQL only: with autocommit off SQLite commits before a savepoint, so
    Django supports no ``atomic`` block there.
    """
    if connection.vendor != "postgresql":
        pytest.skip("Django supports no atomic block on SQLite with autocommit off.")
    reader = grant("scope/thread", 1, "reader")
    threads.write_relationships([reader])
    transaction.set_autocommit(False)
    try:
        with evaluator_scope():
            assert messages()[0] == [1, 2]
            if revoked == "before the block":
                threads.delete_relationship(reader)
                with transaction.atomic():
                    assert messages()[0] == []
            else:
                with transaction.atomic():
                    threads.delete_relationship(reader)
                assert messages()[0] == []
            transaction.rollback()
            assert messages()[0] == [1, 2]
    finally:
        transaction.rollback()
        transaction.set_autocommit(True)


@KEPT
def test_nested_subject_sets_and_wildcards_reach_the_actor(threads):
    inner = SubjectRef.of("auth/group", "inner", "member")
    outer = SubjectRef.of("auth/group", "outer", "member")
    threads.write_relationships(
        [
            grant("auth/group", "inner", "member"),
            grant("auth/group", "outer", "member", inner),
            grant("scope/thread", 1, "reader", outer),
            grant("scope/thread", 2, "pub", SubjectRef.of("auth/user", "*")),
        ]
    )
    with evaluator_scope():
        assert messages()[0] == [1, 2, 3]
        assert messages(ADMIN)[0] == [3]


def subtracting(seed: int) -> tuple[str, list[RelationshipTuple]]:
    """A policy in which every permission subtracts, and rows that make the subtraction bite.

    The inherited-witness generator subtracts rarely; a missing arm admits only
    under a subtraction, so this one puts each stored relation on either side of
    one, nested one level, with each row present half the time.
    """
    rng = random.Random(seed)

    def union(atoms: list[str]) -> str:
        return " + ".join(rng.sample(atoms, rng.randint(1, len(atoms))))

    def subtraction(grants: list[str], denials: list[str]) -> str:
        shape = rng.choice(["flat", "nested", "kept"])
        if shape == "flat":
            return f"({union(grants)}) - ({union(denials)})"
        if shape == "nested":
            # The innermost operand is negated twice: a granting position again.
            return f"({union(grants)}) - (({union(denials)}) - ({union(grants)}))"
        return f"(({union(grants)}) - ({union(denials)})) + ({union(grants)})"

    schema = f"""
definition auth/user {{}}
definition auth/group {{ relation member: auth/user }}
definition scope/thread {{
    relation direct: auth/user
    relation grp: auth/group#member
    relation banned: auth/user
    relation muted: auth/user | auth/group#member
    permission read = {subtraction(["direct", "grp"], ["banned", "muted"])}
}}
definition scope/message {{
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    relation banned: auth/user
    permission read = {subtraction(["direct", "thread->read"], ["banned"])}
}}
definition scope/part {{
    relation message: scope/message // rebac:field=message
    relation direct: auth/user
    relation banned: auth/user
    permission read = {subtraction(["direct", "message->read"], ["banned"])}
}}
"""
    group = SubjectRef.of("auth/group", "g", "member")
    tuples = []
    for actor in (ADMIN, MEMBER):
        if rng.random() < 0.5:
            tuples.append(RelationshipTuple(ObjectRef("auth/group", "g"), "member", actor))
    for type_, count, relations in (
        ("scope/thread", 2, ("direct", "banned", "muted")),
        ("scope/message", 3, ("direct", "banned")),
        ("scope/part", 3, ("direct", "banned")),
    ):
        for oid in range(1, count + 1):
            for relation in relations:
                for actor in (ADMIN, MEMBER):
                    if rng.random() < 0.5:
                        tuples.append(
                            RelationshipTuple(ObjectRef(type_, str(oid)), relation, actor)
                        )
            if type_ == "scope/thread":
                for relation in ("grp", "muted"):
                    if rng.random() < 0.4:
                        tuples.append(
                            RelationshipTuple(ObjectRef(type_, str(oid)), relation, group)
                        )
    return schema, tuples


def assert_missing_keys_only_lose_rows(schema, tuples, seed, storage, settings, monkeypatch):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(schema)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1), ScopeThread(id=2)])
    ScopeMessage._base_manager.bulk_create(
        [
            ScopeMessage(id=1, thread_id=1),
            ScopeMessage(id=2, thread_id=1),
            ScopeMessage(id=3, thread_id=2),
        ]
    )
    ScopePart._base_manager.bulk_create(
        [ScopePart(id=n, message_id=n, fragment_id=n) for n in (1, 2, 3)]
    )
    if tuples:
        local.write_relationships(tuples)
    rng = random.Random(seed)
    models = ((ScopePart, "scope/part"), (ScopeMessage, "scope/message"))
    for actor in (ADMIN, MEMBER):
        expected = {
            model: set(model.objects.with_actor(actor).values_list("pk", flat=True))
            for model, _type in models
        }
        with evaluator_scope():
            held = read._actor_profile(local, actor, "default")
        assert held is not None
        keys = sorted(held)
        stale = {frozenset(), *(frozenset(keys) - {key} for key in keys)}
        stale |= {frozenset(rng.sample(keys, size)) for size in range(1, len(keys))}
        for missing in stale:
            with monkeypatch.context() as patch:
                patch.setattr(
                    read, "_actor_profile", lambda *args, _stale=missing, **kwargs: _stale
                )
                with evaluator_scope():
                    for model, resource_type in models:
                        found = set(model.objects.with_actor(actor).values_list("pk", flat=True))
                        assert found <= expected[model], (
                            actor,
                            model.__name__,
                            sorted(missing),
                            schema,
                        )
                        for pk in model._base_manager.values_list("pk", flat=True):
                            allowed = local.check_access(
                                subject=actor,
                                action="read",
                                resource=ObjectRef(resource_type, str(pk)),
                            ).allowed
                            assert not allowed or pk in expected[model], (
                                actor,
                                resource_type,
                                pk,
                                sorted(missing),
                                schema,
                            )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.pg_delta
@pytest.mark.parametrize("seed", range(24))
def test_a_profile_with_keys_removed_never_admits_a_row(seed, storage, settings, monkeypatch):
    """The contract's direction: whatever a profile misses, a read or a check only loses rows.

    The random policies carry unions, intersections, exclusions, caveats,
    expiration, wildcards and subject sets. A real profile can be stale only by
    missing keys: the empty one, the true one less each key, and a random subset of
    each size are tried.
    """
    assert_missing_keys_only_lose_rows(*build(seed), seed, storage, settings, monkeypatch)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.pg_delta
@pytest.mark.parametrize("seed", range(40))
def test_a_profile_with_keys_removed_never_admits_a_row_under_a_subtraction(
    seed, storage, settings, monkeypatch
):
    assert_missing_keys_only_lose_rows(*subtracting(seed), seed, storage, settings, monkeypatch)
