"""``REBAC_ROW_SCOPE`` (proposal 0020): a queryset's scope as conditions on each of its rows."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import SubjectRef, rowscope, sudo
from rebac.testing import install_schema
from tests.mechanism_policies import (
    ADMIN,
    EVERYONE,
    FIXED,
    MEMBER,
    application_shaped,
    grant,
    group,
    label_threads,
    role,
)
from tests.test_actor_profiles import seeded, subtracting
from tests.test_bounded_walk import DECIDING
from tests.test_bounded_walk import local as local
from tests.test_inherited_witness_reference import build
from tests.test_schema_generation import ACTOR as PUBLISHED_ACTOR
from tests.test_schema_generation import _unpublish, synced  # noqa: F401
from tests.testapp.models import ScopeMessage, ScopePart, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
MODELS = (ScopeThread, ScopeMessage, ScopePart)


@pytest.fixture(autouse=True)
def jit_off(db):
    """The setting's own requirement on PostgreSQL (``rebac.rowscope``): a statement of
    correlated subqueries is estimated expensive, and the JIT then compiles every one."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SET jit = off")


def read(model, actor=MEMBER, action="read", **filters):
    with CaptureQueriesContext(connection) as captured:
        found = sorted(
            model.objects.with_actor(actor)
            .with_action(action)
            .filter(**filters)
            .values_list("pk", flat=True)
        )
    return found, [query["sql"] for query in captured.captured_queries]


def id_sets(statements: list[str]) -> list[str]:
    return [sql for sql in statements if "rebac_ids" in sql]


@pytest.fixture
def rows(local, settings):
    backend = install_schema(FIXED)
    settings.REBAC_ROW_SCOPE = True
    return backend


def test_the_scope_is_a_condition_on_the_row(rows):
    rows.write_relationships([grant("scope/thread", 1, "reader")])
    found, statements = read(ScopeThread)
    assert found == [1]
    assert not id_sets(statements) and "EXISTS" in statements[-1]
    found, statements = read(ScopePart)
    assert found == [1, 2] and not id_sets(statements)


def test_a_subject_set_a_constant_arm_and_a_parent_are_row_conditions(rows):
    rows.write_relationships(
        [
            grant("auth/group", "g", "member"),
            grant("scope/role", "team", "member", group("g")),
            grant("scope/thread", 2, "team", role("team")),
            grant("scope/role", "admin", "member", ADMIN),
            grant("scope/message", 1, "direct"),
        ]
    )
    assert read(ScopeThread)[0] == [2]
    assert read(ScopeMessage)[0] == [1, 3]
    assert read(ScopePart)[0] == [1, 3]
    found, statements = read(ScopePart, ADMIN)
    assert found == [1, 2, 3] and not id_sets(statements)


def test_an_intersection_an_exclusion_and_filtered_backings_are_row_conditions(rows):
    rows.write_relationships(
        [
            grant("scope/thread", 1, "reader"),
            grant("scope/thread", 2, "reader", EVERYONE),
            grant("scope/role", "team", "member"),
            grant("scope/thread", 2, "team", role("team")),
        ]
    )
    assert read(ScopeThread, action="both")[0] == [2]
    assert read(ScopeThread)[0] == [1, 2]
    # The filtered constant withholds the thread labelled hidden; a grant row blocks the other.
    label_threads({2: "hidden"})
    rows.write_relationships([grant("scope/thread", 1, "blocked")])
    found, statements = read(ScopeThread)
    assert found == [] and not id_sets(statements)
    assert read(ScopeThread, ADMIN)[0] == []
    label_threads({2: "thread"})
    assert read(ScopeThread)[0] == [2]
    # ``first`` is the message of the parts whose fragment is 1.
    found, statements = read(ScopePart, action="opening")
    assert found == [] and not id_sets(statements)
    assert read(ScopePart, action="opening", actor=ADMIN)[0] == []
    rows.write_relationships([grant("scope/message", 1, "direct")])
    assert read(ScopePart, action="opening")[0] == [1]


def test_named_uses_row_conditions_only_where_the_query_names_its_rows(rows, settings):
    rows.write_relationships([grant("scope/thread", 1, "reader")])
    settings.REBAC_ROW_SCOPE = "named"
    found, statements = read(ScopeMessage)
    assert found == [1, 2] and id_sets(statements)
    found, statements = read(ScopeMessage, pk__in=[1, 3])
    assert found == [1] and not id_sets(statements)
    found, statements = read(ScopeMessage, thread_id=1)
    assert found == [1, 2] and not id_sets(statements)
    found, statements = read(ScopeMessage, pk__lte=2)
    assert found == [1, 2] and id_sets(statements)


def test_what_cannot_be_read_as_row_conditions_keeps_the_compiled_scope(local, settings):
    backend = install_schema(DECIDING)
    settings.REBAC_ROW_SCOPE = True
    inner = SubjectRef.of("auth/group", "inner", "member")
    backend.write_relationships(
        [grant("auth/group", "inner", "member"), grant("scope/thread", 2, "reader", inner)]
    )
    # ``reader`` admits a group whose membership nests: a recursion.
    assert rowscope.row_conditions(ScopeThread, MEMBER, "read", "default") is None
    found, statements = read(ScopeThread)
    assert found == [2] and id_sets(statements)
    # An actor that is a subject set.
    assert rowscope.row_conditions(ScopePart, inner, "read", "default") is None


NULLABLE = """
definition auth/user {}
definition test/backingqueue {
    relation reader: auth/user
    permission read = reader
}
definition test/backingtask {
    relation queue: test/backingqueue // rebac:field=queue
    relation rounded: test/backingtask // rebac:const={"target_id":"rounded","filters":{"shared_round_id":1}}
    permission conceal = authenticated
    permission withheld = rounded->conceal
    permission read = queue->read - withheld
}
"""


def test_an_exclusion_whose_condition_is_unknown_on_a_row_excludes_nothing(settings):
    """``shared_round_id = 1`` is NULL on a task without a round: it is not withheld."""
    from tests.testapp.models import BackingQueue, BackingRound, BackingTask

    backend = install_schema(NULLABLE)
    with sudo(reason="row scope fixture"):
        queue = BackingQueue.objects.create()
        first, second = (BackingRound.objects.create(id=n) for n in (1, 2))
        without = BackingTask.objects.create(queue=queue)
        withheld = BackingTask.objects.create(queue=queue, shared_round=first)
        other = BackingTask.objects.create(queue=queue, shared_round=second)
    backend.write_relationships([grant("test/backingqueue", queue.pk, "reader")])
    expected, compiled_statements = read(BackingTask)
    assert expected == sorted([without.pk, other.pk]) and withheld.pk not in expected
    settings.REBAC_ROW_SCOPE = True
    found, statements = read(BackingTask)
    assert found == expected
    assert id_sets(compiled_statements) and not id_sets(statements)
    settings.REBAC_BOUNDED_WALK = "decide"
    held = backend.held(
        subject=MEMBER,
        action="read",
        resource_type="test/backingtask",
        ids=[str(without.pk), str(withheld.pk), str(other.pk)],
    )
    assert held == {str(pk) for pk in expected}


DEEP = """
definition auth/user {}
definition scope/thread {
    relation a: auth/user
    relation b: auth/user
    relation c: auth/user
    permission p1 = (a - b) + (c - a)
    permission p2 = (p1 - b) + (p1 & c) + (c - p1)
    permission p3 = (p2 - p1) + (p2 & p1) + (a - p2)
    permission p4 = (p3 - p2) + (p3 & p2) + (b - p3)
    permission read = (p4 - p3) + (p4 & p3) + (c - p4)
}
"""


def test_a_permission_too_large_as_row_conditions_keeps_the_compiled_scope(local, settings):
    """Each level refers to the one below three times: written out, the fourth level alone is
    hundreds of subqueries. The compiled scope names each id set once."""
    backend = install_schema(DEEP)
    backend.write_relationships(
        [
            grant("scope/thread", 1, "a"),
            grant("scope/thread", 2, "c"),
            grant("scope/thread", 2, "b"),
        ]
    )
    expected, _ = read(ScopeThread)
    settings.REBAC_ROW_SCOPE = True
    assert rowscope.row_conditions(ScopeThread, MEMBER, "p1", "default") is not None
    assert rowscope.row_conditions(ScopeThread, MEMBER, "read", "default") is None
    found, statements = read(ScopeThread)
    assert found == expected and id_sets(statements)
    assert read(ScopeThread, action="p1")[0] == read(ScopeThread, action="p1")[0]


def assert_same_rows(seed, settings, actions=("read",), *, whole: bool, monkeypatch=None):
    for actor in (ADMIN, MEMBER):
        for model in MODELS:
            for action in actions if model is ScopeThread else ("read",):
                if whole:
                    # Every part is readable: only its size can keep the compiled scope.
                    with monkeypatch.context() as patch:
                        patch.setattr(rowscope, "_SUBQUERIES", 10**9)
                        conditions = rowscope.row_conditions(model, actor, action, "default")
                    rowscope._built.clear()
                    assert conditions is not None, (actor, model.__name__, action, seed)
                for filters in ({}, {"pk__in": [1, 3]}, {"pk__gte": 2}):
                    settings.REBAC_ROW_SCOPE = False
                    expected, _ = read(model, actor, action, **filters)
                    for setting in (True, "named"):
                        settings.REBAC_ROW_SCOPE = setting
                        found, _ = read(model, actor, action, **filters)
                        assert found == expected, (actor, model.__name__, filters, setting, seed)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("seed", range(60))
def test_rows_read_with_row_conditions_are_the_rows_read_without(
    seed, storage, settings, monkeypatch
):
    """Policies shaped like an application's, every part one the row scope reads: each model,
    action and actor reads the same rows either way, with and without a condition of the caller's."""
    schema, tuples, labels = application_shaped(seed)
    seeded(schema, tuples, storage, settings)
    label_threads(labels)
    assert_same_rows(seed, settings, ("read", "write"), whole=True, monkeypatch=monkeypatch)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("seed", range(40))
def test_the_setting_changes_no_rows_on_policies_it_reads_in_part(seed, storage, settings):
    """The library's own random policies, with caveats, expiration and recursion-free unions:
    where the row scope applies and where it falls back, the rows are the same."""
    generate = subtracting if seed % 2 else build
    seeded(*generate(seed), storage, settings)
    assert_same_rows(seed, settings, whole=False)


def test_built_conditions_are_kept_and_follow_the_policy(rows):
    rows.write_relationships([grant("scope/thread", 1, "reader")])
    rowscope._built.clear()
    first = rowscope.row_conditions(ScopeThread, MEMBER, "read", "default")
    assert (
        first is not None
        and rowscope.row_conditions(ScopeThread, MEMBER, "read", "default") is first
    )
    assert rowscope.row_conditions(ScopeThread, ADMIN, "read", "default") is not first
    assert read(ScopeThread)[0] == [1]
    # Another policy is another key: the kept conditions are not reused for it.
    install_schema(
        FIXED.replace("permission read = (granted - withheld) - blocked", "permission read = nil")
    )
    assert read(ScopeThread)[0] == []


@pytest.mark.parametrize("state", ["absent", "empty"])
def test_row_conditions_admit_nothing_while_no_policy_is_published(synced, settings, state):  # noqa: F811
    """They carry the fence a compiled statement carries."""
    from tests.testapp.models import Post

    local, resource = synced
    settings.REBAC_ROW_SCOPE = True

    def written():
        with CaptureQueriesContext(connection) as captured:
            found = list(
                Post.objects.with_actor(PUBLISHED_ACTOR)
                .with_action("write")
                .values_list("pk", flat=True)
            )
        return found, [query["sql"] for query in captured.captured_queries]

    found, statements = written()
    assert found == [int(resource.resource_id)] and not id_sets(statements)
    kept = rowscope.row_conditions(Post, PUBLISHED_ACTOR, "write", "default")
    _unpublish(local, state)
    assert written()[0] == []
    # Conditions built for the published policy admit nothing once it is not the published one.
    assert kept is not None and not Post._base_manager.filter(kept).exists()


def test_only_a_statement_the_queryset_runs_itself_is_written_as_row_conditions(rows):
    """Embedded in another query, counted or combined, the scope stays one uncorrelated condition."""
    rows.write_relationships([grant("scope/thread", 1, "reader")])
    readable = ScopeThread.objects.with_actor(MEMBER)
    with CaptureQueriesContext(connection) as captured:
        assert readable.count() == 1
        assert readable.exists()
        inside = ScopeMessage.objects.with_actor(MEMBER).filter(thread__in=readable)
        assert sorted(inside.values_list("pk", flat=True)) == [1, 2]
        both = readable.filter(pk=1) | readable.filter(pk=2)
        assert list(both.values_list("pk", flat=True)) == [1]
    statements = [query["sql"] for query in captured.captured_queries]
    count, exists, embedded, _combined = statements[-4:]
    assert "rebac_ids" in count and "rebac_ids" in exists
    # The message statement is the queryset's own; the thread queryset inside it is embedded.
    assert "rebac_ids" in embedded


def test_explain_says_why_a_permission_keeps_the_compiled_scope(local, settings):
    install_schema(FIXED)
    told = rowscope.explain(ScopePart, MEMBER, "read")
    assert told["applies"] and told["subqueries"] and told["blocked"] == []
    install_schema(DECIDING)
    told = rowscope.explain(ScopeThread, MEMBER, "read")
    assert not told["applies"] and told["blocked"] == ["auth/group#member: a recursion"]
    install_schema(DEEP)
    told = rowscope.explain(ScopeThread, MEMBER, "read")
    assert not told["applies"] and "past the limit" in told["blocked"][0]
