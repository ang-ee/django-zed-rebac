"""Structural prefetch (proposals 0020, 0022) with display rows (0024) and without inheritance through links (0023).

Where they meet:

- Structural delivery reads the policy the backend evaluates. Under
  ``REBAC_INHERIT_THROUGH_LINKS = False`` that policy's arrows name generated
  permissions (``lent(P)``, ``excluded(P)``) and its recursions differ from the
  published policy's: the prover, the recursion analysis and ``explain`` read it
  as the compiler does, and a delivery returns what the scoped read under that
  policy returns. A generated name is not an action: rows admitted under one
  deliver nothing.
- Children a prefetch delivered structurally were admitted: the rows they refer to
  are delivered as display rows. A display row was never admitted: nothing is
  delivered structurally under it.
- A parent's admission delivers children structurally only under the policy its
  scope was built under: the children of an eager scope built under one value of
  the setting and prefetched under the other keep their statement (its rows still
  refer to display rows, as that policy admitted them:
  ``tests/test_display_rows_under_narrowing.py``).
- A policy that reads a name two ways is read by its statements by every mechanism,
  and refused by the narrowing.
"""

from __future__ import annotations

import random
from typing import Any

import pytest
from django.db import connection
from django.db.models import Prefetch
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, display, evaluator_scope, structural
from rebac.compile import read
from rebac.schema.ast import Permission, PermRef, is_builtin_reference, member
from rebac.testing import compare_mechanisms, install_schema
from tests.mechanism_policies import seeds
from tests.test_inherited_witness_reference import expr
from tests.test_structural_depth_frontier import SHAPES, children_of, posts_of, random_rows
from tests.testapp.models import (
    BackingQueue,
    BackingRound,
    BackingTask,
    ScopeMessage,
    ScopePart,
    ScopeProfile,
    ScopeThread,
)

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
MEMBER = SubjectRef.of("auth/user", "member")
OTHER = SubjectRef.of("auth/user", "other")


def grant(
    type_: str, id_: object, relation: str, subject: SubjectRef = MEMBER
) -> RelationshipTuple:
    return RelationshipTuple(ObjectRef(type_, str(id_)), relation, subject)


def on(type_: str, id_: object) -> SubjectRef:
    return SubjectRef.of(type_, str(id_))


def seed_scope() -> None:
    """Threads 1-2; messages 1 and 2 in thread 1, 3 in thread 2; part ``n`` of message ``n``."""
    ScopeThread._base_manager.bulk_create(
        [ScopeThread(id=1, label="first"), ScopeThread(id=2, label="second")]
    )
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


def statements_of(read_rows: Any) -> tuple[Any, list[str]]:
    """What a read returns, and the statements of a second run."""
    read_rows()
    with CaptureQueriesContext(connection) as captured:
        found = read_rows()
    return found, [query["sql"] for query in captured.captured_queries]


def selecting(sqls: list[str], model: Any) -> str:
    """The one statement that reads ``model``'s rows (a scoped one opens with its CTEs)."""
    table = model._meta.db_table
    columns = ", ".join(f'"{table}"."{field.column}"' for field in model._meta.concrete_fields)
    [found] = [sql for sql in sqls if f'SELECT {columns} FROM "{table}"' in sql]
    return found


def threads_messages_parts(action: str = "read", actor: SubjectRef = MEMBER) -> dict[int, Any]:
    rows = (
        ScopeThread.objects.with_actor(actor)
        .with_action(action)
        .order_by("pk")
        .rebac_prefetch_related("scopemessage_set__scopepart_set")
    )
    return {
        t.pk: {m.pk: sorted(p.pk for p in m.scopepart_set.all()) for m in t.scopemessage_set.all()}
        for t in rows
    }


# ---------- Structural delivery under the narrowed policy ----------

# Threads and messages are one recursion under the published policy: a thread lends what a
# message linked to it grants, and a message inherits its thread's. ``linked`` is a stored
# relation to records of another type, a link; the narrowed policy reads through it only what a
# message grants directly (``lent(read)``, its own ``direct``), and the recursion is gone.
RECURSIVE = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation linked: scope/message
    permission read = reader + linked->read
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission read = direct + thread->read
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
}
"""
EDGE = {
    "child_model": ScopeMessage,
    "fk_name": "thread",
    "parent_model": ScopeThread,
    "child_action": "read",
    "parent_action": "read",
}


@pytest.fixture(params=["denormalized", "registry"])
def recursive(request, settings):
    """The member reads thread 1; thread 2 is linked to message 1, which grants nothing directly."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    local = install_schema(RECURSIVE)
    seed_scope()
    local.write_relationships(
        [
            grant("scope/thread", 1, "reader"),
            grant("scope/thread", 2, "linked", on("scope/message", 1)),
        ]
    )
    return local


def test_a_recursion_the_narrowing_cuts_is_delivered_under_the_narrowed_policy(recursive, settings):
    """Published, a message reads its thread inside the recursion it entered: its statement is
    kept. Narrowed there is no recursion, so the edge is proven and delivered, and the read equals
    the scoped read under that policy. Thread 2 is read through a message only the published
    policy lends through."""
    expected = {
        True: {1: {1: [1], 2: [2]}, 2: {3: [3]}},
        False: {1: {1: [1], 2: [2]}},
    }
    for inherit, refused in ((True, True), (False, False)):
        settings.REBAC_INHERIT_THROUGH_LINKS = inherit
        policy = read._policy(recursive)
        reason = structural.explain(policy, **EDGE)
        assert (reason is not None and "recursive" in reason) is refused, (inherit, reason)
        settings.REBAC_STRUCTURAL_PREFETCH = False
        scoped = threads_messages_parts()
        settings.REBAC_STRUCTURAL_PREFETCH = True
        delivered, sqls = statements_of(threads_messages_parts)
        assert scoped == delivered == expected[inherit], inherit
        messages = selecting(sqls, ScopeMessage)
        assert ("rebac_" in messages) is refused, inherit
        assert "rebac_" not in selecting(sqls, ScopePart), inherit


def test_the_narrowed_policy_is_read_by_its_own_names(recursive, settings):
    """The narrowed policy's arrow names ``lent(read)``, a permission of the message's narrowed
    definition: ``member`` finds it, it is no built-in, and its component is the narrowed
    program's. The proofs read it there."""
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    policy = read._policy(recursive)
    message = policy.schema.get_definition("scope/message")
    assert message is not None
    found = member(message, "lent(read)")
    assert isinstance(found, Permission) and found.expression == PermRef("direct")
    assert not is_builtin_reference("lent(read)")
    assert not policy.program.recursive
    assert ("scope/message", "lent(read)") in policy.program.dependencies
    assert structural.implies(policy, "scope/message", "lent(read)", "read")
    settings.REBAC_INHERIT_THROUGH_LINKS = True
    published = read._policy(recursive)
    assert ("scope/thread", "read") in published.program.recursive
    assert published.schema.get_definition("scope/message").permissions == (
        recursive.schema().get_definition("scope/message").permissions
    )


def test_a_parent_admitted_under_a_generated_name_delivers_no_child(recursive, settings):
    """``lent(read)`` is a permission of the narrowed policy, and the prover shows it implies
    ``read``; it is still not an action. Rows recorded under it deliver no child, ``explain``
    says why, and a read scoped under it reads no row."""
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    settings.REBAC_STRUCTURAL_PREFETCH = True
    policy = read._policy(recursive)
    edge = {**EDGE, "child_model": ScopePart, "fk_name": "message", "parent_model": ScopeMessage}
    assert structural.implies(policy, "scope/message", "lent(read)", "read")
    assert structural.explain(policy, **edge) is None
    for name in ("lent(read)", "excluded(read)", "excluded(lent(read))"):
        reason = structural.explain(policy, **{**edge, "parent_action": name})
        assert reason is not None and "generated name" in reason, name
        assert not structural.parent_implies_children(policy, **{**edge, "parent_action": name})
        reason = structural.explain(policy, **{**edge, "child_action": name})
        assert reason is not None and "generated name" in reason, name
    messages = list(ScopeMessage._base_manager.filter(pk__in=[1, 2]))
    with structural.delivering(MEMBER, ScopeMessage, "lent(read)", messages):
        with CaptureQueriesContext(connection) as captured:
            parts = sorted(
                p.pk for p in ScopePart.objects.with_actor(MEMBER).filter(message_id__in=[1, 2])
            )
    assert parts == [1, 2]
    assert all("rebac_" in q["sql"] for q in captured.captured_queries)
    assert list(ScopeMessage.objects.with_actor(MEMBER).with_action("lent(read)")) == []


# ``authenticated`` inside an expression is the actor's state, whatever the type declares under
# that name; here it declares a permission the narrowing changes.
BUILTIN = """
definition auth/user {}
definition scope/profile {
    relation owner: scope/message // rebac:field=owner
    relation reader: auth/user
    permission read = reader + owner->own
}
definition scope/thread {
    relation reader: auth/user
    relation keeper: scope/profile
    permission read = reader + keeper->read
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation linked: scope/thread
    relation pub: auth/user:*
    relation direct: auth/user
    permission own = direct
    permission authenticated = linked->read
    permission read = (pub - authenticated) + thread->read
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
}
"""


def test_a_built_in_name_stays_the_actors_state_in_the_narrowed_policy(settings):
    """Published, ``pub - authenticated`` grants an authenticated member nothing. The narrowing
    read the name as the declared permission and named its version under the exclusion,
    ``excluded(authenticated)``: narrowed, the member read message 3, which the published policy
    denies. The name is the actor's state in both now, and structural delivery under the
    narrowed policy reads what the scoped read does."""
    local = install_schema(BUILTIN)
    seed_scope()
    local.write_relationships(
        [
            grant("scope/message", 3, "pub", SubjectRef.of("auth/user", "*")),
            grant("scope/message", 3, "linked", on("scope/thread", 2)),
            grant("scope/thread", 1, "reader"),
        ]
    )
    published = sorted(ScopeMessage.objects.with_actor(MEMBER).values_list("pk", flat=True))
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    narrowed = sorted(ScopeMessage.objects.with_actor(MEMBER).values_list("pk", flat=True))
    assert published == [1, 2] and set(narrowed) <= set(published)
    message = read._policy(local).schema.get_definition("scope/message")
    assert message is not None
    expression = member(message, "read")
    assert isinstance(expression, Permission)
    assert "authenticated" in repr(expression.expression)
    assert "excluded(authenticated)" not in repr(expression.expression)
    assert structural._opened(message, "authenticated") is None
    assert isinstance(member(message, "authenticated"), Permission)
    settings.REBAC_STRUCTURAL_PREFETCH = False
    scoped = threads_messages_parts()
    settings.REBAC_STRUCTURAL_PREFETCH = True
    assert threads_messages_parts() == scoped == {1: {1: [1], 2: [2]}}


# ---------- The depth frontier under the narrowed policy ----------


def test_the_frontier_shapes_reach_generated_names():
    """The recursions the depth-frontier differential reads: several are narrowed, so the run
    below reads recursions whose arrows name generated permissions, and recursions the
    narrowing cuts."""
    from rebac import links
    from rebac.schema.parser import parse_zed

    narrowed = 0
    for schema, _actions, _children in SHAPES.values():
        published = parse_zed("definition auth/user {}\n" + schema)
        names = {
            p.name
            for d in links.narrowed(published).definitions
            for p in d.permissions
            if links.is_generated(p.name)
        }
        narrowed += bool(names)
    assert narrowed >= 3


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize(
    "depth", [pytest.param(1, marks=pytest.mark.slow), 2, pytest.param(3, marks=pytest.mark.slow)]
)
@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("seed", seeds(5, fast=1))
def test_delivery_reads_what_the_narrowed_scoped_read_does_at_the_frontier(
    seed, shape, depth, storage, settings
):
    """The depth-frontier differential of structural prefetch, under the narrowed policy:
    every prefetch of posts and subfolders returns what it returns with the setting off."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    settings.REBAC_DEPTH_LIMIT = depth
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    schema, actions, child_actions = SHAPES[shape]
    rng = random.Random(seed * 1000 + depth)
    local = install_schema("definition auth/user {}\n" + schema)
    tuples = random_rows(rng, schema)
    if tuples:
        local.write_relationships(tuples)
    for actor in (SubjectRef.of("auth/user", "alice"), SubjectRef.of("auth/user", "bob")):
        for action in actions:
            settings.REBAC_STRUCTURAL_PREFETCH = False
            expected_children = children_of(actor, action)
            expected = {child: posts_of(actor, action, child) for child in child_actions}
            settings.REBAC_STRUCTURAL_PREFETCH = True
            assert children_of(actor, action) == expected_children, (actor, action)
            for child, posts in expected.items():
                assert posts_of(actor, action, child) == posts, (actor, action, child)
                with evaluator_scope():
                    assert posts_of(actor, action, child) == posts, (actor, action, child)


# ---------- Random policies that inherit through links ----------


def linked_policy(seed: int) -> tuple[str, list[RelationshipTuple]]:
    """``(schema, tuples)``: threads, messages and parts whose permissions reach both through
    foreign keys (``thread``, ``message``: parents, kept by the narrowing) and through stored
    relations to records of another type (``keeper``, ``linked``, ``pinned``: links, read
    through as what the linked record grants directly). A profile's ``read`` inherits from its
    message, so what it lends differs from it. Half the seeds inherit along the foreign keys,
    so that structural prefetch delivers."""
    rng = random.Random(seed)
    t_atoms = ["reader", "banned", "keeper->read", "keeper->view"]
    m_atoms = ["direct", "banned", "thread->read", "thread->view", "linked->read", "linked->view"]
    p_atoms = ["direct", "banned", "message->read", "message->view", "pinned->read"]
    t_view, t_read = expr(rng, t_atoms, 2), expr(rng, [*t_atoms, "view"], 2)
    m_view, m_read = expr(rng, m_atoms, 2), expr(rng, [*m_atoms, "view"], 2)
    p_read = expr(rng, p_atoms, 2)
    if seed % 2 == 0:
        t_read = f"reader + ({t_read})"
        m_read = f"thread->read + ({m_read})"
        p_read = f"message->read + ({p_read})"
    schema = f"""
definition auth/user {{}}
definition scope/profile {{
    relation owner: scope/message // rebac:field=owner
    relation reader: auth/user
    permission read = reader + owner->own
    permission view = reader
}}
definition scope/thread {{
    relation reader: auth/user
    relation banned: auth/user
    relation keeper: scope/profile
    permission view = {t_view}
    permission read = {t_read}
}}
definition scope/message {{
    relation thread: scope/thread // rebac:field=thread
    relation linked: scope/thread
    relation direct: auth/user
    relation banned: auth/user
    permission own = direct
    permission view = {m_view}
    permission read = {m_read}
}}
definition scope/part {{
    relation message: scope/message // rebac:field=message
    relation pinned: scope/message
    relation direct: auth/user
    relation banned: auth/user
    permission read = {p_read}
}}
"""
    tuples = [grant("scope/thread", 1, "reader")] if seed % 2 == 0 else []
    for actor in (MEMBER, OTHER):
        for type_, count, relations in (
            ("scope/thread", 2, ("reader", "banned")),
            ("scope/message", 3, ("direct", "banned")),
            ("scope/part", 3, ("direct", "banned")),
            ("scope/profile", 2, ("reader",)),
        ):
            for n in range(1, count + 1):
                for relation in relations:
                    if rng.random() < 0.3:
                        tuples.append(grant(type_, n, relation, actor))
    for n in (1, 2):
        if rng.random() < 0.6:
            tuples.append(
                grant("scope/thread", n, "keeper", on("scope/profile", rng.randint(1, 2)))
            )
    for n in (1, 2, 3):
        if rng.random() < 0.6:
            tuples.append(
                grant("scope/message", n, "linked", on("scope/thread", rng.randint(1, 2)))
            )
        if rng.random() < 0.6:
            tuples.append(grant("scope/part", n, "pinned", on("scope/message", rng.randint(1, 3))))
    return schema, sorted({repr(item): item for item in tuples}.values(), key=repr)


def seeded_linked(seed: int, storage: str, settings: Any) -> Any:
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    schema, tuples = linked_policy(seed)
    local = install_schema(schema)
    seed_scope()
    ScopeProfile._base_manager.bulk_create(
        [ScopeProfile(id=1, owner_id=1, nick="one"), ScopeProfile(id=2, owner_id=3, nick="three")]
    )
    if tuples:
        local.write_relationships(tuples)
    return local


def labelled_messages(action: str, actor: SubjectRef) -> dict[int, Any]:
    """Messages under ``action``, with their threads asked for as display rows and their parts."""
    rows = (
        ScopeMessage.objects.with_actor(actor)
        .with_action(action)
        .order_by("pk")
        .prefetch_related(
            Prefetch("thread", queryset=ScopeThread.objects.with_actor(actor).for_display()),
            Prefetch("scopepart_set", queryset=ScopePart.objects.with_actor(actor)),
        )
    )
    found = {}
    for m in rows:
        try:
            thread = m.thread
        except ScopeThread.DoesNotExist:
            thread = None
        label = None if thread is None else (thread.pk, display.is_display_row(thread))
        found[m.pk] = (label, sorted(p.pk for p in m.scopepart_set.all()))
    return found


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("seed", seeds(40))
def test_structural_delivery_reads_the_scoped_read_of_each_policy(
    seed, storage, settings, monkeypatch
):
    """Random policies that inherit through links: under each policy, published and narrowed, a
    nested prefetch with structural prefetch on returns exactly what it returns off, threads and
    messages read under each of their actions, and with display rows asked for on the messages'
    threads, inside an evaluator scope and outside one. On the seeds that inherit along the
    foreign keys, the narrowed policy delivers children."""
    seeded_linked(seed, storage, settings)
    settings.REBAC_DISPLAY_FIELDS = True
    display.declare(ScopeThread, "label")
    delivered: list[bool] = []
    decide = structural.delivers

    def counted(*args: Any, **kwargs: Any) -> bool:
        found = decide(*args, **kwargs)
        delivered.append(found)
        return found

    monkeypatch.setattr(structural, "delivers", counted)
    try:
        for inherit in (True, False):
            settings.REBAC_INHERIT_THROUGH_LINKS = inherit
            for actor in (MEMBER, OTHER):
                for action in ("read", "view"):
                    settings.REBAC_STRUCTURAL_PREFETCH = False
                    expected = (
                        threads_messages_parts(action, actor),
                        labelled_messages(action, actor),
                    )
                    settings.REBAC_STRUCTURAL_PREFETCH = True
                    delivered.clear()
                    found = (
                        threads_messages_parts(action, actor),
                        labelled_messages(action, actor),
                    )
                    assert found == expected, (inherit, actor, action, seed)
                    with evaluator_scope():
                        assert (
                            threads_messages_parts(action, actor),
                            labelled_messages(action, actor),
                        ) == expected, (inherit, actor, action, seed)
                    if not inherit and seed % 2 == 0 and (actor, action) == (MEMBER, "read"):
                        assert any(delivered), seed
    finally:
        display.declare(ScopeThread)


def test_compare_mechanisms_finds_the_narrowed_reads_equal(settings):
    """``compare_mechanisms`` on the narrowed policy: the same statements, the same result, the
    children's statements without their permission SQL."""
    seeded_linked(0, "registry", settings)
    report = compare_mechanisms(
        lambda: threads_messages_parts(),
        {"REBAC_INHERIT_THROUGH_LINKS": (False,), "REBAC_STRUCTURAL_PREFETCH": (False, True)},
        repeats=1,
    )
    assert [row["result"] for row in report] == ["baseline", "same"]
    assert report[0]["statements"] == report[1]["statements"]
    assert report[1]["sql_kb"] < report[0]["sql_kb"]


# ---------- Structural children and display rows ----------

QUEUES = """
definition auth/user {}
definition test/backingqueue {
    relation reader: auth/user
    relation muted: auth/user
    permission read = reader
    permission inbox = reader - muted
}
definition test/backingtask {
    relation queue: test/backingqueue // rebac:field=queue
    relation direct: auth/user
    permission read = queue->read + direct
}
definition test/backinground {
    relation reader: auth/user
    permission read = reader
}
"""


@pytest.fixture
def queues(settings):
    """Queue 1, which the member reads, holds tasks 1 and 2, in rounds 1 and 2, which it may not
    read; queue 2 holds task 3. A round shows its listing."""
    settings.REBAC_DISPLAY_FIELDS = True
    settings.REBAC_STRUCTURAL_PREFETCH = True
    local = install_schema(QUEUES)
    BackingQueue._base_manager.bulk_create([BackingQueue(id=1), BackingQueue(id=2)])
    BackingRound._base_manager.bulk_create(
        [BackingRound(id=1, listing="r1"), BackingRound(id=2, listing="r2")]
    )
    BackingTask._base_manager.bulk_create(
        [
            BackingTask(id=1, queue_id=1, shared_round_id=1),
            BackingTask(id=2, queue_id=1, shared_round_id=2),
            BackingTask(id=3, queue_id=2, shared_round_id=1),
        ]
    )
    local.write_relationships([grant("test/backingqueue", 1, "reader")])
    display.declare(BackingRound, "listing")
    yield local
    display.declare(BackingRound)


def tasks_with_rounds(action: str) -> dict[int, Any]:
    queues = (
        BackingQueue.objects.with_actor(MEMBER)
        .with_action(action)
        .order_by("pk")
        .prefetch_related(
            Prefetch("tasks", queryset=BackingTask.objects.with_actor(MEMBER).order_by("pk")),
            Prefetch(
                "tasks__shared_round",
                queryset=BackingRound.objects.with_actor(MEMBER).for_display(),
            ),
        )
    )
    return {
        q.pk: [
            (
                t.pk,
                t.shared_round.pk,
                t.shared_round.listing,
                display.is_display_row(t.shared_round),
            )
            for t in q.tasks.all()
        ]
        for q in queues
    }


@pytest.mark.parametrize("action", ["read", "inbox"])
def test_children_delivered_structurally_lend_their_labels(queues, settings, action):
    """Tasks of the queue the member reads (under ``read``, or ``inbox``, which implies it) are
    delivered without their permission statement, and they were admitted: the rounds they name
    are display rows, read by key. Read by their scope instead, the tasks lend the same labels."""
    expected = {1: [(1, 1, "r1", True), (2, 2, "r2", True)]}
    found, sqls = statements_of(lambda: tasks_with_rounds(action))
    assert found == expected
    [root, tasks, rounds] = sqls
    assert "rebac_" in root and "rebac_" not in tasks and "rebac_" not in rounds
    assert 'FROM "testapp_backinground"' in rounds
    settings.REBAC_STRUCTURAL_PREFETCH = False
    found, sqls = statements_of(lambda: tasks_with_rounds(action))
    assert found == expected and "rebac_" in sqls[1]


INBOX = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    relation muted: auth/user
    permission read = direct
    permission inbox = direct - muted
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    relation direct: auth/user
    permission read = message->read + direct
}
"""


def test_a_page_admitted_under_an_implying_action_gets_children_and_labels(settings):
    """Messages listed under ``inbox``, which implies ``read``: their parts are delivered
    structurally and their threads as display rows, neither with a permission statement."""
    settings.REBAC_DISPLAY_FIELDS = True
    settings.REBAC_STRUCTURAL_PREFETCH = True
    local = install_schema(INBOX)
    seed_scope()
    local.write_relationships(
        [grant("scope/message", n, "direct") for n in (1, 2, 3)]
        + [grant("scope/message", 3, "muted")]
    )
    display.declare(ScopeThread, "label")
    try:
        found, sqls = statements_of(lambda: labelled_messages("inbox", MEMBER))
    finally:
        display.declare(ScopeThread)
    assert found == {1: ((1, True), [1]), 2: ((1, True), [2])}
    [root, *rest] = sqls
    assert "rebac_" in root and len(rest) == 2 and all("rebac_" not in sql for sql in rest)


SHOWN = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission read = thread->read + direct
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    relation direct: auth/user
    permission read = message->read + direct
}
"""


@pytest.fixture
def shown(settings):
    """The member reads parts 1-3 and thread 1, and no message directly; a thread shows its
    label and a message its key."""
    settings.REBAC_DISPLAY_FIELDS = True
    settings.REBAC_STRUCTURAL_PREFETCH = True
    local = install_schema(SHOWN)
    seed_scope()
    local.write_relationships(
        [grant("scope/part", n, "direct") for n in (1, 2, 3)] + [grant("scope/thread", 1, "reader")]
    )
    display.declare(ScopeThread, "label")
    display.declare(ScopeMessage, "id")
    yield local
    display.declare(ScopeThread)
    display.declare(ScopeMessage)


def test_a_display_row_is_no_parent_for_structural_delivery(shown):
    """A part's message is a display row; its parts, which the policy grants with the message,
    are read by their permission statement: a display row was never admitted. The member reads
    message 3's part directly and its message not at all."""
    rows = (
        ScopePart.objects.with_actor(MEMBER)
        .order_by("pk")
        .prefetch_related(
            Prefetch("message", queryset=ScopeMessage.objects.with_actor(MEMBER).for_display()),
            Prefetch("message__scopepart_set", queryset=ScopePart.objects.with_actor(MEMBER)),
        )
    )
    with CaptureQueriesContext(connection) as captured:
        found = {
            p.pk: (
                display.is_display_row(p.message),
                sorted(q.pk for q in p.message.scopepart_set.all()),
            )
            for p in rows
        }
    assert found == {1: (True, [1]), 2: (True, [2]), 3: (True, [3])}
    [_root, messages, parts] = [q["sql"] for q in captured.captured_queries]
    assert "rebac_" not in messages and "rebac_" in parts


def test_a_display_rows_children_are_scoped_in_either_order_of_nesting(shown):
    """Messages under the threads the member reads are delivered structurally; a message's
    thread asked for as a display row lends nothing structurally to that thread's messages."""
    rows = (
        ScopeMessage.objects.with_actor(MEMBER)
        .order_by("pk")
        .prefetch_related(
            Prefetch("thread", queryset=ScopeThread.objects.with_actor(MEMBER).for_display()),
            Prefetch("thread__scopemessage_set", queryset=ScopeMessage.objects.with_actor(MEMBER)),
        )
    )
    with CaptureQueriesContext(connection) as captured:
        found = {
            m.pk: (
                display.is_display_row(m.thread),
                sorted(n.pk for n in m.thread.scopemessage_set.all()),
            )
            for m in rows
        }
    assert found == {1: (True, [1, 2]), 2: (True, [1, 2])}
    [_root, threads, messages] = [q["sql"] for q in captured.captured_queries]
    assert "rebac_" not in threads and "rebac_" in messages
    assert threads_messages_parts() == {1: {1: [1], 2: [2]}}


# ---------- One policy throughout ----------

# The member reads thread 1, and so its messages 1 and 2. (That a delivery serves the one
# evaluation that decided it is the scope rule's: tests/test_security_scope_decision.py, and
# under an implied action tests/test_structural_scope_decision.py.)
DIRECT = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission read = thread->read + direct
}
"""
READER = grant("scope/thread", 1, "reader")


# ---------- A name read two ways (a stored policy from before the rule was held) ----------

# The reviewer's policy: a thread's relation named ``authenticated`` beside ``read =
# authenticated``, which inside the expression is the actor's state. The member holds no tuple.
TWO_WAYS = """
definition auth/user {}
definition scope/thread {
    relation authenticated: auth/user
    permission read = authenticated
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    permission read = thread->authenticated
    permission browse = thread->read
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
}
definition lab/account {
    relation reader: auth/user
    permission read = reader
}
definition lab/note {
    relation place: scope/thread
    permission read = place->authenticated
    permission browse = place->read
}
"""


def two_ways_world(text: str) -> None:
    from tests.testapp.models import LabAccount, LabNote

    ScopeThread._base_manager.bulk_create([ScopeThread(id=1, label="one")])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=1, thread_id=1)])
    ScopePart._base_manager.bulk_create([ScopePart(id=1, message_id=1, fragment_id=1)])
    LabAccount._base_manager.bulk_create([LabAccount(id=1, code="a", name="Alice", secret="s")])
    LabNote._base_manager.bulk_create([LabNote(id=1, owner_id=1)])


def browsed_owners() -> list[Any]:
    from tests.testapp.models import LabAccount, LabNote

    notes = list(
        LabNote.objects.with_actor(MEMBER)
        .with_action("browse")
        .order_by("pk")
        .prefetch_related(
            Prefetch("owner", queryset=LabAccount.objects.with_actor(MEMBER).for_display())
        )
    )
    found = []
    for note in notes:
        try:
            owner = note.owner
        except LabAccount.DoesNotExist:
            owner = None
        found.append(None if owner is None else (owner.pk, display.is_display_row(owner)))
    return found


def threads_messages(on: bool, settings: Any) -> dict[int, list[int]]:
    settings.REBAC_STRUCTURAL_PREFETCH = on
    rows = (
        ScopeThread.objects.with_actor(MEMBER)
        .order_by("pk")
        .rebac_prefetch_related("scopemessage_set")
    )
    return {t.pk: sorted(m.pk for m in t.scopemessage_set.all()) for t in rows}


@pytest.mark.parametrize("name", ["authenticated", "anonymous"])
@pytest.mark.parametrize("inherit", [True, False], ids=["published", "narrowed"])
def test_a_relation_named_as_a_built_in_delivers_nothing(settings, monkeypatch, name, inherit):
    """Installed past the runtime check, as a stored policy from before the rule could be. The
    member reads thread 1 (``read`` is the actor's state) and no message (the relation is empty).
    Structural prefetch delivered message 1 under thread 1, and notes under ``browse`` lent
    their owner as a label, because the prover equated the relation with the built-in. The
    narrowing refuses the policy (kept), so with the setting ``False`` the read fails closed."""
    from rebac.backends import local as local_module
    from tests.testapp.models import LabAccount, LabNote

    monkeypatch.setattr(local_module, "_name_conflicts", lambda schema: [])
    local = install_schema(TWO_WAYS.replace("authenticated", name))
    two_ways_world(TWO_WAYS)
    local.write_relationships([grant("lab/note", 1, "place", on("scope/thread", 1))])
    settings.REBAC_INHERIT_THROUGH_LINKS = inherit
    settings.REBAC_DISPLAY_FIELDS = True
    display.declare(LabAccount, "name")
    try:
        if not inherit:
            from rebac import links

            with pytest.raises(links.NarrowingError, match="reserved built-in actors"):
                threads_messages(True, settings)
            return
        scoped = threads_messages(False, settings)
        assert threads_messages(True, settings) == scoped
        assert scoped == ({1: []} if name == "authenticated" else {})
        assert not display.admits_reading(LabNote, "browse")
        # Notes under ``browse`` are admitted where the member is authenticated, and lend
        # nothing: the owner is read by its scope.
        assert browsed_owners() == ([None] if name == "authenticated" else [])
    finally:
        display.declare(LabAccount)


@pytest.mark.django_db
def test_a_stored_policy_with_a_relation_named_as_a_built_in_delivers_nothing(settings):
    """The same policy stored, with the relation renamed outside the write owners after it was
    stored as ``authx``: the walk, the row scope, structural prefetch and display rows all read
    it by its statements."""
    from rebac import backend, rowscope
    from rebac.models import SchemaRelation
    from rebac.models.generation import SchemaGeneration
    from tests.test_inherit_through_links import store
    from tests.testapp.models import LabAccount

    store(TWO_WAYS.replace("relation authenticated: auth/user", "relation authx: auth/user"))
    two_ways_world(TWO_WAYS)
    backend().write_relationships([grant("lab/note", 1, "place", on("scope/thread", 1))])
    meta = SchemaRelation._meta
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {connection.ops.quote_name(meta.db_table)} SET name = %s WHERE name = %s",
            ["authenticated", "authx"],
        )
    SchemaGeneration.objects.filter(pk=1).update(revision="after-upgrade")
    backend().mark_schema_stale()
    settings.REBAC_DISPLAY_FIELDS = True
    settings.REBAC_ROW_SCOPE = True
    settings.REBAC_BOUNDED_WALK = "decide"
    display.declare(LabAccount, "name")
    try:
        assert threads_messages(True, settings) == threads_messages(False, settings) == {1: []}
        assert browsed_owners() == [None]
        told = rowscope.explain(ScopeMessage, MEMBER, "read")
        assert not told["applies"] and "reads two ways" in told["blocked"][0]
        assert backend().held(
            subject=MEMBER, action="read", resource_type="scope/thread", ids=["1"]
        ) == {"1"}
    finally:
        display.declare(LabAccount)


# ---------- The parents' admission proves under the policy their scope pinned ----------

# The reviewer's policy: ``a = linked->view`` implies ``t = linked->own`` only once narrowed
# (a profile's ``view`` lends nothing through its keeper), and a part's ``read`` is
# ``message->t``. The member reaches message 1 under ``a`` only through thread 1's keeper.
MIXED = """
definition auth/user {}
definition scope/profile {
    relation reader: auth/user
    permission view = reader
}
definition scope/thread {
    relation reader: auth/user
    relation keeper: scope/profile
    permission view = reader + keeper->view
    permission own = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation linked: scope/thread
    permission a = linked->view
    permission t = linked->own
    permission read = t
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->t
}
"""


@pytest.mark.parametrize(
    ("built", "read_under"),
    [(True, False), (False, True)],
    ids=["published then narrowed", "narrowed then published"],
)
def test_a_parents_admission_proves_under_the_policy_it_was_scoped_under(
    settings, monkeypatch, built, read_under
):
    """An eager scope built under one value of the setting, its children prefetched under the
    other. Built published, message 1 is admitted under ``a`` through the keeper; the narrowed
    policy proves ``a => t``; delivered, part 1 was read, which neither policy grants. The
    children are delivered only under the policy the parents' scope pinned, so here they are
    read by their statement."""
    from rebac.conf import app_settings

    settings.REBAC_STRUCTURAL_PREFETCH = True
    local = install_schema(MIXED)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=1, thread_id=1)])
    ScopePart._base_manager.bulk_create([ScopePart(id=1, message_id=1, fragment_id=1)])
    ScopeProfile._base_manager.bulk_create([ScopeProfile(id=1, owner_id=1)])
    local.write_relationships(
        [
            grant("scope/message", 1, "linked", on("scope/thread", 1)),
            grant("scope/thread", 1, "keeper", on("scope/profile", 1)),
            grant("scope/profile", 1, "reader"),
        ]
    )
    monkeypatch.setitem(app_settings._cache, "REBAC_INHERIT_THROUGH_LINKS", built)
    parents = (
        ScopeMessage.objects.with_actor(MEMBER)
        .with_action("a")
        .scoped()
        .prefetch_related(Prefetch("scopepart_set", queryset=ScopePart.objects.with_actor(MEMBER)))
    )
    monkeypatch.setitem(app_settings._cache, "REBAC_INHERIT_THROUGH_LINKS", read_under)
    with CaptureQueriesContext(connection) as captured:
        found = {m.pk: [p.pk for p in m.scopepart_set.all()] for m in parents}
    assert found == ({1: []} if built else {})
    parts = [
        q["sql"]
        for q in captured.captured_queries
        if '"testapp_scopepart"."fragment_id" FROM' in q["sql"]
    ]
    assert all("rebac_" in sql for sql in parts)


def test_children_are_delivered_under_the_policy_the_parents_were_scoped_under(settings):
    """Under one policy throughout, the pinned key is the policy's own: children are delivered."""
    settings.REBAC_STRUCTURAL_PREFETCH = True
    local = install_schema(DIRECT)
    seed_scope()
    local.write_relationships([READER])
    found, sqls = statements_of(
        lambda: {
            t.pk: sorted(m.pk for m in t.scopemessage_set.all())
            for t in ScopeThread.objects.with_actor(MEMBER).rebac_prefetch_related(
                "scopemessage_set"
            )
        }
    )
    assert found == {1: [1, 2]}
    assert "rebac_" not in selecting(sqls, ScopeMessage)
