"""check_bulk_permissions: the answers of check_access, from statements the items share."""

from itertools import pairwise

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import (
    CheckItem,
    CheckResult,
    ObjectRef,
    PermissionDepthExceeded,
    PermissionResult,
    RelationshipTuple,
    SubjectRef,
)
from rebac.backends.base import Backend
from rebac.compile import read
from rebac.compile.predicate import Bound
from rebac.evaluator import evaluator_scope
from rebac.testing import install_schema

pytestmark = pytest.mark.django_db

SCHEMA = """
caveat office(inside bool) { inside }
definition auth/user {}
definition auth/group {
    relation member: auth/user | auth/user:* | auth/group#member | auth/user with office
}
definition docs/folder {
    relation parent: docs/folder
    relation viewer: auth/user | auth/group#member
    permission read = viewer + parent->read
}
definition docs/doc {
    relation folder: docs/folder
    relation viewer: auth/user | auth/group#member
    relation banned: auth/group#member
    permission read = (viewer + folder->read) - banned
    permission view = viewer
}
"""
MEMBERS = ("auth/group", "member")
DOC = ObjectRef("docs/doc", "d")


def user(name: str) -> SubjectRef:
    return SubjectRef.of("auth/user", name)


def group(name: str) -> ObjectRef:
    return ObjectRef("auth/group", name)


def members(name: str) -> SubjectRef:
    return SubjectRef.of("auth/group", name, "member")


def member(name: str, subject: SubjectRef, **extra) -> RelationshipTuple:
    return RelationshipTuple(group(name), "member", subject, **extra)


def one_by_one(local, items):
    return [
        local.check_access(
            subject=item.subject, action=item.action, resource=item.resource, context=item.context
        )
        for item in items
    ]


@pytest.fixture
def office():
    """Nested groups, a wildcard, a caveat, a ban and a folder chain."""
    local = install_schema(SCHEMA)
    folder = ObjectRef("docs/folder", "f")
    local.write_relationships(
        [
            member("team", user("ann")),
            member("team", user("bob")),
            member("staff", members("team")),
            member("everyone", SubjectRef.of("auth/user", "*")),
            member("visitors", user("eve"), caveat_name="office"),
            member("blocked", user("bob")),
            RelationshipTuple(DOC, "viewer", members("staff")),
            RelationshipTuple(DOC, "viewer", members("visitors")),
            RelationshipTuple(DOC, "viewer", user("dan")),
            RelationshipTuple(DOC, "banned", members("blocked")),
            RelationshipTuple(DOC, "folder", SubjectRef.of("docs/folder", "f")),
            RelationshipTuple(ObjectRef("docs/folder", "top"), "viewer", user("fay")),
            RelationshipTuple(folder, "parent", SubjectRef.of("docs/folder", "top")),
            RelationshipTuple(ObjectRef("docs/doc", "open"), "viewer", members("everyone")),
        ]
    )
    return local


PEOPLE = ("ann", "bob", "cat", "dan", "eve", "fay")


def test_the_answers_are_those_of_check_access(office):
    items = [
        CheckItem(user(name), action, resource, context)
        for name in PEOPLE
        for action in ("read", "view")
        for resource in (DOC, ObjectRef("docs/doc", "open"), ObjectRef("docs/doc", "missing"))
        for context in (None, {"inside": True}, {"inside": False})
    ]
    answers = office.check_bulk_permissions(items)
    assert answers == one_by_one(office, items)
    by_item = {
        (
            item.subject.subject_id,
            item.action,
            item.resource.resource_id,
            item.context is None,
        ): answer
        for item, answer in zip(items, answers, strict=True)
    }
    assert by_item["ann", "read", "d", True].result is PermissionResult.HAS_PERMISSION
    assert by_item["bob", "read", "d", True].result is PermissionResult.NO_PERMISSION
    assert by_item["cat", "view", "open", True].result is PermissionResult.HAS_PERMISSION
    assert {r.result for r in answers} == set(PermissionResult)
    conditional = next(r for r in answers if r.result is PermissionResult.CONDITIONAL_PERMISSION)
    assert conditional.conditional_on == ("inside",)


def test_items_that_are_not_points_are_answered_as_check_access_answers_them(office):
    items = [
        CheckItem(user("ann"), "read", ObjectRef("docs/doc", "")),
        CheckItem(user("cat"), "view", ObjectRef("docs/doc", "")),
        CheckItem(user("ann"), "read", ObjectRef("docs/unknown", "x")),
        CheckItem(user("ann"), "fly", DOC),
        CheckItem(members("team"), "read", DOC),
        CheckItem(user("ann"), "viewer", DOC),
    ]
    answers = office.check_bulk_permissions(items)
    assert answers == one_by_one(office, items)
    assert [bool(answer) for answer in answers[:4]] == [True, True, False, False]
    assert "unknown resource type" in answers[2].reason


def test_no_items_cost_nothing(office):
    with CaptureQueriesContext(connection) as queries:
        assert office.check_bulk_permissions([]) == []
    assert len(queries) == 0


def crowd(local, count):
    """``count`` users, each in a group of their own nested under ``staff``."""
    names = [f"u{n:03}" for n in range(count)]
    local.write_relationships(
        [
            *(member(f"own-{name}", user(name)) for name in names),
            *(member("staff", members(f"own-{name}")) for name in names[::2]),
        ]
    )
    return names


def test_statements_do_not_grow_with_the_number_of_actors(office):
    names = crowd(office, 50)
    counts = []
    for size in (5, 50):
        items = [CheckItem(user(name), "view", DOC) for name in names[:size]]
        with CaptureQueriesContext(connection) as queries:
            answers = office.check_bulk_permissions(items)
        counts.append(len(queries))
        # Every other user reaches ``staff`` through a group of their own.
        assert [bool(answer) for answer in answers] == [n % 2 == 0 for n in range(size)]
        assert answers == one_by_one(office, items)
    # The caveat verdicts; the sets, three statements a bound; the two bounds.
    assert counts == [9, 9]


def test_a_statement_of_bounds_is_cut_by_its_size(office, monkeypatch):
    names = crowd(office, 20)
    # ``read`` recurses over folders: its statement at one object is long.
    items = [CheckItem(user(name), "read", DOC) for name in names]
    with CaptureQueriesContext(connection) as whole:
        answers = office.check_bulk_permissions(items)
    assert [bool(answer) for answer in answers] == [n % 2 == 0 for n in range(20)]
    longest = max(len(query["sql"]) for query in whole)
    monkeypatch.setattr(read, "_BULK_SQL", longest // 4)
    with CaptureQueriesContext(connection) as cut:
        assert office.check_bulk_permissions(items) == answers
    assert max(len(query["sql"]) for query in cut) < longest
    with CaptureQueriesContext(connection) as alone:
        assert one_by_one(office, items) == answers
    assert len(whole) < len(cut) < len(alone) / 2


def test_a_chunk_is_fifty_items(office):
    names = crowd(office, 120)
    items = [CheckItem(user(name), "view", DOC) for name in names]
    with CaptureQueriesContext(connection) as small:
        office.check_bulk_permissions(items[:50])
    with CaptureQueriesContext(connection) as large:
        answers = office.check_bulk_permissions(items)
    assert [bool(answer) for answer in answers] == [n % 2 == 0 for n in range(120)]
    assert len(large) <= 3 * len(small)


def test_sets_decided_together_are_the_sets_each_actor_decides_alone(office):
    names = [*PEOPLE, *crowd(office, 6)]
    key = ("docs/doc", "read")
    for context in (None, {"inside": True}, {"inside": False}):
        operations = [read._Operation.begin(office, user(n), "default", context) for n in names]
        verdicts = operations[0].verdicts(key)
        together = read._decide_sets_together(operations, key, verdicts)
        assert together is not None
        for operation in operations:
            alone = operation._decide_sets(key, operation.verdicts(key))
            found = together[operation.actor]
            assert (found.keys, found.lower, found.upper) == (alone.keys, alone.lower, alone.upper)
            # Each support is a tuple from the actor or from a set found before it.
            reached = set()
            for edge in found.support:
                via = (edge.subject_type, edge.subject_relation, edge.subject_id)
                actor = operation.actor
                assert (
                    (
                        edge.own
                        and via == (actor.subject_type, actor.optional_relation, actor.subject_id)
                    )
                    or via in reached
                    or edge.subject_id == "*"
                )
                reached.add((*edge.key, edge.resource_id))
            assert {(key, id_) for key, ids in found.lower.items() for id_ in ids} == {
                (edge.key, edge.resource_id) for edge in found.support
            }


def test_nested_sets_and_cycles_are_followed_for_every_actor():
    local = install_schema(SCHEMA)
    names = [f"g{n}" for n in range(12)]
    local.write_relationships(
        [
            member(names[0], user("ann")),
            member(names[6], user("bob")),
            *(member(outer, members(inner)) for inner, outer in pairwise(names)),
            member(names[0], members(names[-1])),
            member("other", user("cat")),
            RelationshipTuple(DOC, "viewer", members(names[3])),
            RelationshipTuple(DOC, "banned", members("other")),
            RelationshipTuple(DOC, "viewer", user("cat")),
        ]
    )
    items = [CheckItem(user(name), "read", DOC) for name in ("ann", "bob", "cat", "dan")]
    answers = local.check_bulk_permissions(items)
    # The groups form a cycle: every one of them holds ann and bob.
    assert [bool(answer) for answer in answers] == [True, True, False, False]
    assert answers == one_by_one(local, items)
    operations = [read._Operation.begin(local, item.subject, "default") for item in items]
    key = ("docs/doc", "read")
    together = read._decide_sets_together(operations, key, operations[0].verdicts(key))
    assert together[user("ann")].ids(MEMBERS, Bound.LOWER) == frozenset(names)
    assert together[user("bob")].ids(MEMBERS, Bound.LOWER) == frozenset(names)
    assert together[user("cat")].ids(MEMBERS, Bound.LOWER) == {"other"}
    assert together[user("dan")].ids(MEMBERS, Bound.LOWER) == frozenset()


def elsewhere(monkeypatch, write):
    """Write tuples as another process would: this one does not see them change."""
    from rebac.backends import local as local_module

    generation = local_module._relationship_generation
    write()
    monkeypatch.setattr(local_module, "_relationship_generation", generation)


def test_a_membership_revoked_after_the_sets_were_decided_is_not_read_through(office, monkeypatch):
    names = crowd(office, 4)
    decide = read._decide_sets_together
    done = []

    def decide_then_revoke(operations, key, verdicts):
        found = decide(operations, key, verdicts)
        if not done:
            done.append(True)
            gone = member("staff", members(f"own-{names[0]}"))
            elsewhere(monkeypatch, lambda: office.delete_relationship(gone))
        return found

    monkeypatch.setattr(read, "_decide_sets_together", decide_then_revoke)
    items = [CheckItem(user(name), "view", DOC) for name in names]
    with evaluator_scope():
        answers = office.check_bulk_permissions(items)
    assert done
    # u000 was decided into ``staff``; the statement re-reads the tuple that put it there.
    assert [bool(answer) for answer in answers] == [False, False, True, False]


def test_a_membership_gained_after_the_sets_were_decided_does_not_escape_a_ban(office, monkeypatch):
    decide = read._decide_sets_together
    done = []

    def decide_then_ban(operations, key, verdicts):
        found = decide(operations, key, verdicts)
        if not done:
            done.append(True)
            banned = member("blocked", user("ann"))
            elsewhere(monkeypatch, lambda: office.write_relationships([banned]))
        return found

    monkeypatch.setattr(read, "_decide_sets_together", decide_then_ban)
    items = [CheckItem(user(name), "read", DOC) for name in ("ann", "dan")]
    with evaluator_scope():
        answers = office.check_bulk_permissions(items)
    assert done
    assert [bool(answer) for answer in answers] == [False, True]


def test_sets_changed_by_another_process_are_answered_afresh_not_as_a_depth(office, monkeypatch):
    ann = dict(subject=user("ann"), action="read", resource=DOC)
    with evaluator_scope():
        assert office.check_access(**ann).allowed
        # Another process bans ann, so the scope keeps the sets it decided.
        banned = member("blocked", user("ann"))
        elsewhere(monkeypatch, lambda: office.write_relationships([banned]))
        assert office.check_access(**ann) == CheckResult.no()
        assert office.check_bulk_permissions([CheckItem(user("ann"), "read", DOC)]) == [
            CheckResult.no()
        ]


def test_an_item_beyond_the_depth_limit_raises_as_check_access_does(settings):
    settings.REBAC_DEPTH_LIMIT = 2
    local = install_schema(SCHEMA)
    names = [f"f{n}" for n in range(6)]
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("docs/folder", names[-1]), "viewer", user("ann")),
            *(
                RelationshipTuple(
                    ObjectRef("docs/folder", child), "parent", SubjectRef.of("docs/folder", parent)
                )
                for child, parent in pairwise(names)
            ),
        ]
    )
    deep = CheckItem(user("ann"), "read", ObjectRef("docs/folder", names[0]))
    near = CheckItem(user("ann"), "read", ObjectRef("docs/folder", names[-2]))
    with pytest.raises(PermissionDepthExceeded):
        local.check_access(subject=deep.subject, action=deep.action, resource=deep.resource)
    assert local.check_bulk_permissions([near]) == [CheckResult.has()]
    with pytest.raises(PermissionDepthExceeded):
        local.check_bulk_permissions([near, deep])


def test_the_base_backend_asks_item_by_item():
    asked = []

    class Recording(Backend):
        kind = "recording"

        def check_access(self, *, subject, action, resource, context=None, **options):
            asked.append((subject, action, resource, context, options))
            return CheckResult.has() if action == "read" else CheckResult.no()

        accessible = lookup_subjects = write_relationships = None
        delete_relationships = delete_relationship = schema = None

    Recording.__abstractmethods__ = frozenset()
    items = [
        CheckItem(user("ann"), "read", DOC, {"inside": True}),
        CheckItem(user("bob"), "write", DOC),
    ]
    assert Recording().check_bulk_permissions(iter(items)) == [CheckResult.has(), CheckResult.no()]
    assert [entry[:4] for entry in asked] == [
        (user("ann"), "read", DOC, {"inside": True}),
        (user("bob"), "write", DOC, None),
    ]
