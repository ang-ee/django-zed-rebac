"""The bounded walk (proposal 0020): ``held`` settled by small lookups, the compiled statement for the rest."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, walk
from rebac.testing import install_schema
from tests.mechanism_policies import application_shaped, label_threads, seeds
from tests.test_actor_profiles import seeded, subtracting
from tests.test_inherited_witness_reference import build
from tests.test_schema_generation import ACTOR as PUBLISHED_ACTOR
from tests.test_schema_generation import _unpublish, synced  # noqa: F401
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
@pytest.mark.parametrize("seed", seeds(40))
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


# ---------- "decide": the whole permission, read from the rows it names ----------

DECIDING = """
definition auth/user {}
definition auth/group { relation member: auth/user | auth/group#member }
definition scope/role { relation member: auth/user | auth/group#member }
definition scope/thread {
    relation admin: scope/role // rebac:const=admin
    relation reader: auth/user | auth/group#member | auth/user:*
    relation team: scope/role#member
    relation blocked: auth/user | auth/group#member
    permission read = reader + team + admin->member
    permission guarded = read - blocked
    permission both = reader & team
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
TEAM = SubjectRef.of("scope/role", "team", "member")


@pytest.fixture
def deciding(local, settings):
    backend = install_schema(DECIDING)
    settings.REBAC_BOUNDED_WALK = "decide"
    return backend


def compiled(statements: list[str]) -> list[str]:
    """The statements that read an id set or a recursion: the compiler's, not the walk's."""
    return [sql for sql in statements if "rebac_ids" in sql or "WITH " in sql]


def test_deciding_answers_no_without_the_compiled_statement(deciding):
    deciding.write_relationships([grant("scope/thread", 1, "reader")])
    found, statements = held(deciding, "scope/part", [1, 2, 3])
    assert found == {"1", "2"}
    # Part 3 is refused by the same lookups that grant the others.
    assert not compiled(statements)


def test_deciding_reads_a_subject_set_and_a_constant_arm(deciding):
    deciding.write_relationships(
        [
            grant("scope/role", "team", "member"),
            grant("scope/thread", 2, "team", TEAM),
            grant("scope/role", "admin", "member", ADMIN),
        ]
    )
    found, statements = held(deciding, "scope/thread", [1, 2])
    assert found == {"2"} and not compiled(statements)
    found, statements = held(deciding, "scope/part", [1, 2, 3], actor=ADMIN)
    assert found == {"1", "2", "3"} and not compiled(statements)


def test_deciding_reads_an_intersection_and_an_exclusion(deciding):
    deciding.write_relationships(
        [
            grant("scope/thread", 1, "reader"),
            grant("scope/thread", 2, "reader"),
            grant("scope/role", "team", "member"),
            grant("scope/thread", 2, "team", TEAM),
            grant("scope/thread", 1, "blocked"),
        ]
    )
    found, statements = held(deciding, "scope/thread", [1, 2], action="both")
    assert found == {"2"} and not compiled(statements)
    found, statements = held(deciding, "scope/thread", [1, 2], action="guarded")
    assert found == {"2"} and not compiled(statements)
    found, statements = held(deciding, "scope/message", [1, 2, 3], action="guarded")
    assert found == {"3"} and not compiled(statements)


def test_deciding_asks_the_compiler_for_a_recursion_and_for_that_part_alone(deciding):
    """Group membership nests: the walk asks the compiled statement which of the groups named on
    the candidates the actor is in, and reads the rest itself."""
    inner = SubjectRef.of("auth/group", "inner", "member")
    outer = SubjectRef.of("auth/group", "outer", "member")
    deciding.write_relationships(
        [
            grant("auth/group", "inner", "member"),
            grant("auth/group", "outer", "member", inner),
            grant("scope/thread", 2, "reader", outer),
        ]
    )
    found, statements = held(deciding, "scope/part", [1, 2, 3])
    assert found == {"3"}
    asked = compiled(statements)
    assert asked and all("scope_part" not in sql and "scope_message" not in sql for sql in asked)


def test_deciding_leaves_what_is_below_an_exclusion_and_unreadable_to_the_compiler(deciding):
    """``blocked`` admits a nested group below the exclusion: the permission above it is compiled."""
    inner = SubjectRef.of("auth/group", "inner", "member")
    deciding.write_relationships(
        [
            grant("scope/thread", 1, "reader"),
            grant("scope/thread", 2, "reader"),
            grant("auth/group", "inner", "member"),
            grant("scope/thread", 1, "blocked", inner),
        ]
    )
    found, statements = held(deciding, "scope/thread", [1, 2], action="guarded")
    assert found == {"2"} and compiled(statements)


def test_deciding_leaves_an_id_that_is_no_row_to_the_compiled_statement(deciding):
    deciding.write_relationships(
        [grant("scope/thread", 1, "reader"), grant("scope/thread", 77, "reader")]
    )
    found, _statements = held(deciding, "scope/thread", ["1", "77", "01", "x"])
    walked, answered = walk.decided(
        backend=deciding, resource_type="scope/thread", action="read", actor=MEMBER,
        ids=["1", "77", "01", "x"], using="default",
    )  # fmt: skip
    assert (walked, answered) == ({"1"}, {"1"})
    settings_off = deciding.held(
        subject=MEMBER, action="read", resource_type="scope/thread", ids=["1", "77", "01", "x"]
    )
    assert found == settings_off


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("seed", seeds(60))
def test_held_decided_by_the_walk_is_held_without_it(seed, storage, settings):
    """The same random policies: for every model, action and actor, ``held`` returns the same ids
    when the walk decides, and what the walk answers alone is the compiled answer for those ids."""
    generate = subtracting if seed % 2 else build
    local = seeded(*generate(seed), storage, settings)
    candidates = {
        "scope/thread": ["1", "2", "3", "x"],
        "scope/message": ["1", "2", "3", "4", "01"],
        "scope/part": ["1", "2", "3", "9"],
    }
    for actor in (ADMIN, MEMBER):
        for resource_type, ids in candidates.items():
            actions = ["read"]
            if local._schema_snapshot().schema.get_permission(resource_type, "view") is not None:
                actions.append("view")
            for action in actions:
                settings.REBAC_BOUNDED_WALK = False
                expected = local.held(
                    subject=actor, action=action, resource_type=resource_type, ids=ids
                )
                settings.REBAC_BOUNDED_WALK = "decide"
                found = local.held(
                    subject=actor, action=action, resource_type=resource_type, ids=ids
                )
                assert found == expected, (actor, resource_type, action, seed)
                walked, answered = walk.decided(
                    backend=local,
                    resource_type=resource_type,
                    action=action,
                    actor=actor,
                    ids=ids,
                    using="default",
                )
                assert walked == expected & answered, (actor, resource_type, action, seed)
            for setting in (True, "decide"):
                settings.REBAC_BOUNDED_WALK = False
                expected_many = local.held_many(
                    subject=actor, actions=actions, resource_type=resource_type, ids=ids
                )
                settings.REBAC_BOUNDED_WALK = setting
                found_many = local.held_many(
                    subject=actor, actions=actions, resource_type=resource_type, ids=ids
                )
                assert found_many == expected_many, (actor, resource_type, setting, seed)


def test_several_actions_share_what_they_read(deciding):
    deciding.write_relationships([grant("scope/thread", 1, "reader")])
    with CaptureQueriesContext(connection) as captured:
        found = deciding.held_many(
            subject=MEMBER,
            actions=["read", "guarded", "both"],
            resource_type="scope/thread",
            ids=["1", "2"],
        )
    assert found == {"read": {"1"}, "guarded": {"1"}, "both": set()}
    statements = [query["sql"] for query in captured.captured_queries]
    assert not compiled(statements)
    # The rows, then one lookup per relation and open id set: reader, team, the admin role,
    # blocked, and team again for the row the union did not need it on.
    assert len(statements) <= 6, "\n".join(statements)


@pytest.mark.parametrize("state", ["absent", "empty"])
@pytest.mark.parametrize("setting", [True, "decide"])
def test_the_walk_answers_nothing_while_no_policy_is_published(
    synced,  # noqa: F811
    settings,
    setting,
    state,
):
    """A compiled statement is fenced to its policy's revision; so is every lookup of the walk."""
    local, resource = synced
    settings.REBAC_BOUNDED_WALK = setting
    question = dict(
        backend=local, resource_type="blog/post", action="write", actor=PUBLISHED_ACTOR,
        ids=[resource.resource_id], using="default",
    )  # fmt: skip
    assert walk.decided(**question) == ({resource.resource_id}, {resource.resource_id})
    _unpublish(local, state)
    assert walk.decided(**question) == (set(), set())
    assert walk.proved(**question) == set()
    assert local.held(
        subject=PUBLISHED_ACTOR, action="write", resource_type="blog/post",
        ids=[resource.resource_id],
    ) == set()  # fmt: skip


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("seed", seeds(60))
def test_an_application_shaped_policy_is_decided_without_the_compiled_statement(
    seed, storage, settings, monkeypatch
):
    """Flat groups, roles, a constant administrator, parents and filtered constants under
    exclusions: every part is one the walk reads, so it never asks the compiler, and ``held``
    is the compiled answer."""
    schema, tuples, labels = application_shaped(seed)
    local = seeded(schema, tuples, storage, settings)
    label_threads(labels)
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(walk._Walk, "_compiled", lambda self, key, ids: asked.append(key))
    candidates = {
        "scope/thread": (["1", "2", "3", "x"], ["read", "write"]),
        "scope/message": (["1", "2", "3", "4", "01"], ["read"]),
        "scope/part": (["1", "2", "3", "9"], ["read"]),
    }
    for actor in (ADMIN, MEMBER):
        for resource_type, (ids, actions) in candidates.items():
            settings.REBAC_BOUNDED_WALK = False
            expected = local.held_many(
                subject=actor, actions=actions, resource_type=resource_type, ids=ids
            )
            settings.REBAC_BOUNDED_WALK = "decide"
            found = local.held_many(
                subject=actor, actions=actions, resource_type=resource_type, ids=ids
            )
            assert found == expected, (actor, resource_type, seed)
            walked = walk.decided_many(
                backend=local, resource_type=resource_type, actions=actions, actor=actor,
                ids=ids, using="default",
            )  # fmt: skip
            for action in actions:
                held_ids, answered = walked[action]
                assert held_ids == expected[action] & answered, (actor, resource_type, action, seed)
                # Every candidate that is a row is answered by the walk alone.
                assert answered >= {i for i in ids if i in ("1", "2", "3")} - {
                    i for i in ("3",) if resource_type == "scope/thread"
                }, (actor, resource_type, action, seed)
    assert asked == []
