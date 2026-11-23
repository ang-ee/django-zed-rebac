"""Structural delivery (proposal 0020): a prefetch's children read without their permission statement."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.models import Prefetch
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope, structural
from rebac.compile import read
from rebac.testing import install_schema
from tests.test_actor_profiles import seeded, subtracting
from tests.test_inherited_witness_reference import build
from tests.testapp.models import ScopeMessage, ScopePart, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
MEMBER = SubjectRef.of("auth/user", "member")
OTHER = SubjectRef.of("auth/user", "other")

SCHEMA = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation auditor: auth/user
    relation muted: auth/user
    permission read = reader
    permission audit = auditor
    permission inbox = reader - muted
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
def rows():
    """Two threads; three messages, two of them in the first thread; a part of each message."""
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


@pytest.fixture
def structural_on(settings):
    settings.REBAC_STRUCTURAL_PREFETCH = True


def policy(schema: str = SCHEMA):
    return read._policy(install_schema(schema))


def proves(
    schema: str,
    child=ScopeMessage,
    fk="thread",
    parent=ScopeThread,
    action="read",
    parent_action="read",
):
    return structural.parent_implies_children(
        policy(schema), child_model=child, fk_name=fk, parent_model=parent,
        child_action=action, parent_action=parent_action,
    )  # fmt: skip


def threads_with_messages(actor: SubjectRef = MEMBER, action: str = "read"):
    """Each thread the actor holds ``action`` on, its readable message ids, and the statements."""
    queryset = (
        ScopeThread.objects.with_actor(actor)
        .with_action(action)
        .rebac_prefetch_related("scopemessage_set")
    )
    with CaptureQueriesContext(connection) as captured:
        found = {
            thread.pk: sorted(m.pk for m in thread.scopemessage_set.all()) for thread in queryset
        }
    return found, [query["sql"] for query in captured.captured_queries]


def test_a_union_arm_through_the_foreign_key_proves_the_children():
    assert proves(SCHEMA)
    assert proves(SCHEMA, child=ScopePart, fk="message", parent=ScopeMessage)
    # The arm names another action of the parent: holding ``audit`` does not grant ``read``.
    assert not proves(SCHEMA, parent_action="audit")
    assert not proves(SCHEMA, action="write")
    # Holding ``inbox`` (``reader - muted``) does (proposal 0022).
    assert proves(SCHEMA, parent_action="inbox")


def test_why_an_edge_is_refused_is_explained():
    policy_ = policy(SCHEMA)

    def why(**kwargs):
        edge = {"child_model": ScopeMessage, "fk_name": "thread", "parent_model": ScopeThread}
        return structural.explain(
            policy_, **{**edge, "child_action": "read", "parent_action": "read", **kwargs}
        )

    assert why() is None and why(parent_action="inbox") is None
    assert why(parent_action="audit") == (
        "thread->read: audit is not proven to grant read on scope/thread"
    )
    assert why(parent_action="share") == "scope/thread has no permission share"
    assert why(fk_name="other") == "no arm of read's union goes through other"


@pytest.mark.parametrize(
    "read_rule",
    [
        "(thread->read + direct) - blocked",  # an exclusion can take the grant away
        "thread->read & direct",  # so can an intersection
        "direct",  # no arm through the parent
        "thread->audit",  # another action of the parent
        "blocked->read",  # a relation that is not this foreign key
    ],
)
def test_what_can_take_the_grant_away_proves_nothing(read_rule):
    schema = SCHEMA.replace(
        "relation direct: auth/user\n    permission read = thread->read + direct",
        "relation direct: auth/user\n    relation blocked: auth/user\n    permission read = "
        + read_rule,
    )
    assert schema != SCHEMA
    assert not proves(schema)


def test_a_named_permission_in_the_union_contributes_its_arms():
    schema = SCHEMA.replace(
        "permission read = thread->read + direct",
        "permission inherited = thread->read\n    permission read = inherited + direct",
    )
    assert proves(schema)


def test_children_of_admitted_parents_are_read_without_their_permission_statement(
    rows, structural_on
):
    local = install_schema(SCHEMA)
    local.write_relationships([grant("scope/thread", 1, "reader")])
    found, statements = threads_with_messages()
    assert found == {1: [1, 2]}
    child = next(sql for sql in statements if '"testapp_scopemessage"' in sql and "FROM" in sql)
    # A plain read by foreign key: no relationship row, no id set.
    assert "rebac_" not in child
    assert '"thread_id" IN' in child


def test_children_of_parents_admitted_under_an_action_that_grants_theirs_are_delivered(
    rows, structural_on
):
    """Threads admitted under ``inbox``; their messages read under ``read``, whose arm is
    ``thread->read``: holding ``inbox`` on a thread grants ``read`` on it (proposal 0022)."""
    local = install_schema(SCHEMA)
    local.write_relationships(
        [
            grant("scope/thread", 1, "reader"),
            grant("scope/thread", 2, "reader"),
            grant("scope/thread", 2, "muted"),
        ]
    )
    found, statements = threads_with_messages(action="inbox")
    # The muted thread is not in the inbox; its message is not read through it.
    assert found == {1: [1, 2]}
    child = next(sql for sql in statements if 'FROM "testapp_scopemessage"' in sql)
    assert "rebac_" not in child
    assert '"thread_id" IN' in child


def test_children_of_parents_admitted_under_an_action_that_does_not_grant_theirs_are_scoped(
    rows, structural_on
):
    local = install_schema(SCHEMA)
    local.write_relationships(
        [
            grant("scope/thread", 1, "auditor"),
            grant("scope/thread", 2, "auditor"),
            grant("scope/message", 1, "direct"),
        ]
    )
    found, statements = threads_with_messages(action="audit")
    # An auditor reads a message only by its own grant.
    assert found == {1: [1], 2: []}
    assert "rebac_" in next(sql for sql in statements if 'FROM "testapp_scopemessage"' in sql)


def test_each_edge_is_kept_with_its_parent_action():
    policy_ = policy(SCHEMA)

    def proven(parent_action: str) -> bool:
        return structural.parent_implies_children(
            policy_, child_model=ScopeMessage, fk_name="thread", parent_model=ScopeThread,
            child_action="read", parent_action=parent_action,
        )  # fmt: skip

    assert proven("inbox")
    assert not proven("audit")
    assert proven("inbox")


def test_a_child_query_must_name_only_admitted_parents(rows):
    install_schema(SCHEMA)
    [first] = ScopeThread._base_manager.filter(pk=1)
    with structural.delivering(MEMBER, ScopeThread, "read", [first]):

        def delivered(*threads: int) -> bool:
            query = ScopeMessage.objects.filter(thread_id__in=threads).query
            return structural.delivers(query, ScopeMessage, MEMBER, "read", using="default")

        assert delivered(1)
        # Thread 2 was not admitted: a query that may read its messages is not delivered.
        assert not delivered(1, 2)


def test_a_filtered_backing_is_refused():
    schema = SCHEMA.replace(
        "relation message: scope/message // rebac:field=message\n    permission read = message->read",
        'relation message: scope/message // rebac:field={"path":"message","filters":{"fragment_id":1}}'
        "\n    permission read = message->read",
    )
    assert schema != SCHEMA
    assert not proves(schema, child=ScopePart, fk="message", parent=ScopeMessage)
    assert structural.explain(
        policy(schema), child_model=ScopePart, fk_name="message", parent_model=ScopeMessage,
        child_action="read", parent_action="read",
    ) == "message->read: the backing is filtered"  # fmt: skip


def refusal(schema: str, child, fk: str, parent) -> str | None:
    """Why the edge ``child.fk`` is refused below parents held under ``read``, if it is."""
    edge = {"child_model": child, "fk_name": fk, "parent_model": parent}
    return structural.explain(policy(schema), **edge, child_action="read", parent_action="read")


@pytest.mark.parametrize("shape", ["tree", "mutual"])
def test_an_edge_inside_a_recursion_is_refused_with_its_reason(shape):
    from tests.test_structural_depth_frontier import MUTUAL, TREE
    from tests.testapp.models import Folder, Post

    schema, child, fk = (TREE, Folder, "parent") if shape == "tree" else (MUTUAL, Post, "folder")
    assert refusal(schema, child, fk, Folder) == (
        f"{fk}->read: read on blog/folder is recursive with read: the child reads it one hop "
        "deeper than its parent was admitted at"
    )
    # An arrow into the recursion from outside it is read with the whole limit.
    assert refusal(TREE, Post, "folder", Folder) is None


def test_off_by_default_every_prefetch_is_scoped(rows):
    local = install_schema(SCHEMA)
    local.write_relationships([grant("scope/thread", 1, "reader")])
    found, statements = threads_with_messages()
    assert found == {1: [1, 2]}
    child = next(sql for sql in statements if 'FROM "testapp_scopemessage"' in sql)
    assert "rebac_" in child


@pytest.mark.parametrize("value", ["True", "False", 1, "on"])
def test_only_true_turns_it_on(rows, settings, value):
    """A value read from the environment is a string: ``"False"`` was truthy and turned the
    mechanism on, reading children without their statement. Anything but ``True`` is off."""
    settings.REBAC_STRUCTURAL_PREFETCH = value
    local = install_schema(SCHEMA)
    local.write_relationships([grant("scope/thread", 1, "reader")])
    assert not structural.enabled()
    found, statements = threads_with_messages()
    assert found == {1: [1, 2]}
    child = next(sql for sql in statements if 'FROM "testapp_scopemessage"' in sql)
    assert "rebac_" in child


def test_a_rule_that_proves_nothing_keeps_the_scoped_read(rows, structural_on):
    schema = SCHEMA.replace(
        "relation direct: auth/user\n    permission read = thread->read + direct",
        "relation direct: auth/user\n    relation blocked: auth/user\n"
        "    permission read = (thread->read + direct) - blocked",
    )
    local = install_schema(schema)
    local.write_relationships(
        [grant("scope/thread", 1, "reader"), grant("scope/message", 2, "blocked")]
    )
    found, statements = threads_with_messages()
    # The blocked message is withheld: the exclusion is still evaluated.
    assert found == {1: [1]}
    assert "rebac_" in next(sql for sql in statements if 'FROM "testapp_scopemessage"' in sql)


def test_a_prefetch_for_another_actor_is_scoped_for_that_actor(rows, structural_on):
    local = install_schema(SCHEMA)
    local.write_relationships(
        [grant("scope/thread", 1, "reader"), grant("scope/message", 1, "direct", OTHER)]
    )
    queryset = ScopeThread.objects.with_actor(MEMBER).prefetch_related(
        Prefetch("scopemessage_set", queryset=ScopeMessage.objects.with_actor(OTHER))
    )
    with CaptureQueriesContext(connection) as captured:
        found = {
            thread.pk: sorted(m.pk for m in thread.scopemessage_set.all()) for thread in queryset
        }
    # The other actor reads the first message by its own grant, not through the member's thread.
    assert found == {1: [1]}
    assert any(
        "rebac_" in q["sql"]
        for q in captured.captured_queries
        if 'FROM "testapp_scopemessage"' in q["sql"]
    )


def test_parents_read_in_a_system_context_admit_nothing(rows, structural_on):
    local = install_schema(SCHEMA)
    local.write_relationships([grant("scope/message", 3, "direct")])
    queryset = ScopeThread.objects.sudo(reason="test.structural").prefetch_related(
        Prefetch("scopemessage_set", queryset=ScopeMessage.objects.with_actor(MEMBER))
    )
    found = {thread.pk: sorted(m.pk for m in thread.scopemessage_set.all()) for thread in queryset}
    # Every thread is read, and of their messages only the one the member holds.
    assert found == {1: [], 2: [3]}


def test_a_nested_prefetch_delivers_each_level(rows, structural_on):
    local = install_schema(SCHEMA)
    local.write_relationships([grant("scope/thread", 1, "reader")])
    queryset = ScopeThread.objects.with_actor(MEMBER).rebac_prefetch_related(
        "scopemessage_set__scopepart_set"
    )
    with CaptureQueriesContext(connection) as captured:
        found = {
            thread.pk: sorted(
                part.pk for m in thread.scopemessage_set.all() for part in m.scopepart_set.all()
            )
            for thread in queryset
        }
    assert found == {1: [1, 2]}
    for table in ("testapp_scopemessage", "testapp_scopepart"):
        child = next(q["sql"] for q in captured.captured_queries if f'FROM "{table}"' in q["sql"])
        assert "rebac_" not in child


def test_children_read_after_the_prefetch_are_scoped(rows, structural_on):
    """Only the prefetch that read the parents delivers; a later read asks as ever."""
    local = install_schema(SCHEMA)
    local.write_relationships([grant("scope/thread", 1, "reader")])
    [thread] = list(ScopeThread.objects.with_actor(MEMBER))
    with CaptureQueriesContext(connection) as captured:
        assert sorted(m.pk for m in thread.scopemessage_set.all()) == [1, 2]
    assert any("rebac_" in q["sql"] for q in captured.captured_queries)


def prefetched(actor, action="read"):
    threads = (
        ScopeThread.objects.with_actor(actor)
        .with_action(action)
        .rebac_prefetch_related("scopemessage_set__scopepart_set")
    )
    return {
        thread.pk: {
            m.pk: sorted(p.pk for p in m.scopepart_set.all()) for m in thread.scopemessage_set.all()
        }
        for thread in threads
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("seed", range(40))
def test_structural_delivery_reads_what_scoped_prefetches_read(seed, storage, settings):
    """Random policies with unions, intersections, exclusions, caveats, wildcards and subject sets:
    with the setting on, a nested prefetch returns exactly what it returns with the setting off,
    inside an evaluator scope and outside one, with the threads admitted under each of their
    actions (the inherited-witness policies give threads ``view`` beside ``read``)."""
    generate = subtracting if seed % 2 else build
    seeded(*generate(seed), storage, settings)
    actions = ("read", "view") if generate is build else ("read",)
    for actor in (SubjectRef.of("auth/user", "admin"), MEMBER):
        for action in actions:
            settings.REBAC_STRUCTURAL_PREFETCH = False
            expected = prefetched(actor, action)
            settings.REBAC_STRUCTURAL_PREFETCH = True
            assert prefetched(actor, action) == expected, (actor, action, seed)
            with evaluator_scope():
                assert prefetched(actor, action) == expected, (actor, action, seed)


def test_the_mechanisms_of_one_read_are_compared_side_by_side(rows):
    """``compare_mechanisms`` runs a read under each combination of settings and reports
    its cost and whether the result moved."""
    from rebac.testing import compare_mechanisms, format_mechanisms

    local = install_schema(SCHEMA)
    local.write_relationships([grant("scope/thread", 1, "reader")])
    report = compare_mechanisms(
        lambda: threads_with_messages()[0],
        {"REBAC_STRUCTURAL_PREFETCH": (False, True)},
        repeats=1,
    )
    assert [row["settings"]["REBAC_STRUCTURAL_PREFETCH"] for row in report] == [False, True]
    assert [row["result"] for row in report] == ["baseline", "same"]
    assert report[0]["statements"] == report[1]["statements"]
    # The same statements, the child's one without its permission SQL.
    assert report[1]["sql_kb"] < report[0]["sql_kb"]
    table = format_mechanisms(report)
    assert table.splitlines()[0].split() == [
        "structural_prefetch",
        "statements",
        "sql_kb",
        "ms",
        "best_ms",
        "result",
    ]


def test_parents_admitted_under_an_implying_action_compare_equal(rows):
    from rebac.testing import compare_mechanisms

    local = install_schema(SCHEMA)
    local.write_relationships(
        [
            grant("scope/thread", 1, "reader"),
            grant("scope/thread", 2, "reader"),
            grant("scope/thread", 2, "muted"),
        ]
    )
    report = compare_mechanisms(
        lambda: threads_with_messages(action="inbox")[0],
        {"REBAC_STRUCTURAL_PREFETCH": (False, True)},
        repeats=1,
    )
    assert [row["result"] for row in report] == ["baseline", "same"]
    assert report[1]["sql_kb"] < report[0]["sql_kb"]
