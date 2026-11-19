"""The bounded walk (proposal 0020): ``held`` settled by small lookups, the compiled statement for the rest."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, walk
from rebac.testing import install_schema
from tests.test_actor_profiles import seeded, subtracting
from tests.test_inherited_witness_reference import build
from tests.testapp.models import ScopeMessage, ScopePart, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
MEMBER = SubjectRef.of("auth/user", "member")
ADMIN = SubjectRef.of("auth/user", "admin")

SCHEMA = """
definition auth/user {}
definition auth/group { relation member: auth/user }
definition scope/thread {
    relation reader: auth/user | auth/group#member | auth/user:*
    relation blocked: auth/user
    permission read = reader
    permission guarded = reader - blocked
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission read = thread->read + direct
    permission guarded = thread->guarded
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
}
"""


def grant(
    type_: str, id_: object, relation: str, subject: SubjectRef = MEMBER, **kwargs
) -> RelationshipTuple:
    return RelationshipTuple(ObjectRef(type_, str(id_)), relation, subject, **kwargs)


@pytest.fixture
def local():
    """Two threads; three messages, two of them in the first thread; a part of each message."""
    backend = install_schema(SCHEMA)
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
    return backend


def held(local, resource_type: str, ids, action: str = "read", actor: SubjectRef = MEMBER):
    with CaptureQueriesContext(connection) as captured:
        found = local.held(
            subject=actor, action=action, resource_type=resource_type, ids=[str(i) for i in ids]
        )
    return found, [query["sql"] for query in captured.captured_queries]


def proved(
    local, resource_type: str, ids, action: str = "read", actor: SubjectRef = MEMBER
) -> set[str]:
    return walk.proved(
        backend=local, resource_type=resource_type, action=action, actor=actor,
        ids=[str(i) for i in ids], using="default",
    )  # fmt: skip


def test_a_grant_on_the_row_is_one_small_lookup(local, settings):
    local.write_relationships([grant("scope/thread", 1, "reader")])
    expected, compiled = held(local, "scope/thread", [1, 2])
    settings.REBAC_BOUNDED_WALK = True
    found, statements = held(local, "scope/thread", [1])
    assert found == {"1"} and expected == {"1"}
    # The proved id asks nothing of the compiled statement.
    assert len(statements) == 1 and "rebac_ids" not in statements[0]
    assert any("rebac_ids" in sql or "WITH" in sql for sql in compiled)


def test_the_ids_the_walk_does_not_settle_go_to_the_compiled_statement(local, settings):
    local.write_relationships([grant("scope/thread", 1, "reader")])
    settings.REBAC_BOUNDED_WALK = True
    found, _statements = held(local, "scope/thread", [1, 2, 9])
    assert found == {"1"}


def test_a_child_is_proved_through_its_parents(local, settings):
    local.write_relationships(
        [grant("scope/thread", 1, "reader"), grant("scope/message", 3, "direct")]
    )
    assert proved(local, "scope/part", [1, 2, 3]) == {"1", "2", "3"}
    settings.REBAC_BOUNDED_WALK = True
    found, statements = held(local, "scope/part", [1, 2, 3])
    assert found == {"1", "2", "3"}
    # Part to message, message to thread, the thread's grant, the message's own grant: no id set.
    assert all("rebac_ids" not in sql for sql in statements)
    assert len(statements) <= 4


def test_a_wildcard_grant_is_read_where_the_relation_declares_one(local):
    everyone = SubjectRef.of("auth/user", "*")
    local.write_relationships([grant("scope/thread", 2, "reader", everyone)])
    assert proved(local, "scope/thread", [1, 2]) == {"2"}
    assert proved(local, "scope/thread", [1, 2], actor=ADMIN) == {"2"}


def test_what_the_walk_cannot_read_exactly_it_leaves_alone(local):
    team = SubjectRef.of("auth/group", "team", "member")
    local.write_relationships(
        [
            grant("auth/group", "team", "member"),
            grant("scope/thread", 1, "reader", team),  # through a subject set
        ]
    )
    assert proved(local, "scope/thread", [1, 2]) == set()
    # An exclusion above the arm: the walk proves nothing, whatever the rows say.
    local.write_relationships([grant("scope/thread", 1, "reader")])
    assert proved(local, "scope/thread", [1]) == {"1"}
    assert proved(local, "scope/thread", [1], action="guarded") == set()
    assert proved(local, "scope/message", [1], action="guarded") == set()


def test_an_id_that_is_not_the_rows_own_spelling_is_not_credited(local):
    local.write_relationships([grant("scope/thread", 1, "reader")])
    assert proved(local, "scope/message", ["1", "01", "x"]) == {"1"}


def test_a_subject_set_an_anonymous_actor_and_a_caveat_context_are_not_walked(local):
    local.write_relationships([grant("scope/thread", 1, "reader")])
    team = SubjectRef.of("auth/group", "team", "member")
    assert proved(local, "scope/thread", [1], actor=team) == set()
    assert walk.proved(
        backend=local, resource_type="scope/thread", action="read", actor=MEMBER,
        ids=["1"], using="default", context={"ip": "10.0.0.1"},
    ) == set()  # fmt: skip


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("seed", range(40))
def test_held_with_the_walk_is_held_without_it(seed, storage, settings):
    """Random policies with unions, intersections, exclusions, caveats, expiration, wildcards and
    subject sets: for every model, action and actor, ``held`` returns the same ids with the
    walk on, and the walk alone proves nothing the compiled statement does not."""
    generate = subtracting if seed % 2 else build
    local = seeded(*generate(seed), storage, settings)
    candidates = {
        "scope/thread": ["1", "2", "3", "x"],
        "scope/message": ["1", "2", "3", "4", "01"],
        "scope/part": ["1", "2", "3", "9"],
    }
    for actor in (ADMIN, MEMBER):
        for resource_type, ids in candidates.items():
            settings.REBAC_BOUNDED_WALK = False
            expected = local.held(
                subject=actor, action="read", resource_type=resource_type, ids=ids
            )
            settings.REBAC_BOUNDED_WALK = True
            found = local.held(subject=actor, action="read", resource_type=resource_type, ids=ids)
            assert found == expected, (actor, resource_type, seed)
            alone = walk.proved(
                backend=local,
                resource_type=resource_type,
                action="read",
                actor=actor,
                ids=ids,
                using="default",
            )
            assert alone <= expected, (actor, resource_type, seed, sorted(alone - expected))
