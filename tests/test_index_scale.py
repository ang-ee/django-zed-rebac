"""A consumer-shaped rebuild with intersecting, multi-hop usersets."""

from __future__ import annotations

import re
from itertools import pairwise
from time import perf_counter
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, backend
from rebac.backends import reset_backend
from rebac.index import project, write
from rebac.index.rebuild import rebuild
from rebac.models.index import IndexCover, IndexEdge, IndexMember, IndexTerm
from rebac.models.relationship import Relationship
from rebac.schema import parse_zed
from tests.index_harness import assert_index_matches, assert_no_drift
from tests.testapp.models import BackingQueue, BackingTask

SCHEMA = """
definition auth/user {}
definition auth/group { relation member: auth/user }
definition test/role {
    relation member: auth/group#member
}
definition test/backingqueue {
    relation role: test/role
    permission write = role->member
}
definition test/person {
    relation active_member: auth/user // rebac:attribute={"field":"is_staff","resource":"person","value":true,"filters":{"is_active":true}}
}
definition test/admin { relation member: auth/user }
definition test/backingtask {
    relation facilitator: auth/user // rebac:field=asker
    relation team: test/backingqueue // rebac:field=queue
    relation person: test/person // rebac:const=person
    relation admin: test/admin // rebac:const=admin
    permission manage = ((facilitator + team->write) & person->active_member) + admin->member
}
"""

LEOPARD_SCALE_SCHEMA = """
definition auth/user {}
definition auth/group { relation member: auth/user | auth/group#member }
definition test/role { relation member: auth/user | auth/group#member }
definition test/kind { relation active_member: auth/group#member }
definition test/project {
    relation owner: auth/user
    relation reader: auth/user | auth/group#member | auth/user:*
    relation admin: test/role // rebac:const=admin
    permission read = owner + reader + admin->member
    permission write = owner + admin->member
}
definition test/task {
    relation project: test/project
    relation parent: test/task
    relation assignee: auth/user
    relation requester: auth/user
    relation reader: auth/user | auth/group#member
    relation banned: auth/user
    relation admin: test/role // rebac:const=admin
    relation person: test/kind // rebac:const=person
    permission withheld = nil
    permission granted = project->read + parent->granted + assignee + reader
    permission read = (granted - withheld) + admin->member
    permission hidden = granted - banned
    permission act = ((assignee - requester) + admin->member) & person->active_member
    permission gated = (reader + project->write) & person->active_member
}
definition test/item {
    relation task: test/task
    relation other: test/task
    relation admin: test/role // rebac:const=admin
    permission read = task->hidden + task->read + admin->member
    permission both = task->read & other->read
}
"""


def _scale_fixture(*, scale: int, user_factor: int = 1) -> None:
    reset_backend()
    backend().set_schema(parse_zed(LEOPARD_SCALE_SCHEMA))
    base_users, users, groups = 40 * scale, 40 * scale * user_factor, 4 * scale
    rows: list[Relationship] = []

    def edge(
        resource_type: str,
        resource_id: str | int,
        relation: str,
        subject_type: str,
        subject_id: str | int,
        subject_relation: str = "",
    ) -> None:
        rows.append(
            Relationship(
                resource_type=resource_type,
                resource_id=str(resource_id),
                relation=relation,
                subject_type=subject_type,
                subject_id=str(subject_id),
                optional_subject_relation=subject_relation,
            )
        )

    for group in range(groups):
        for offset in range(10):
            edge("auth/group", group, "member", "auth/user", (group * 10 + offset) % users)
    for actor in range(users):
        edge("auth/group", "everyone", "member", "auth/user", actor)
    edge("test/role", "admin", "member", "auth/user", 0)
    edge("test/role", "admin", "member", "auth/user", 1)
    edge("test/role", "admin", "member", "auth/group", "everyone", "member")
    edge("test/kind", "person", "active_member", "auth/group", "everyone", "member")
    for project_id in range(10 * scale):
        edge("test/project", project_id, "owner", "auth/user", project_id % base_users)
        for offset in (1, 2):
            edge(
                "test/project",
                project_id,
                "reader",
                "auth/user",
                (project_id + offset) % base_users,
            )
        edge("test/project", project_id, "reader", "auth/group", project_id % groups, "member")
        if project_id % 5 == 0:
            edge("test/project", project_id, "reader", "auth/group", "everyone", "member")
            edge("test/project", project_id, "reader", "auth/user", "*")
    for task in range(100 * scale):
        edge("test/task", task, "project", "test/project", task % (10 * scale))
        if task % 4:
            edge("test/task", task, "parent", "test/task", task - 1)
        edge("test/task", task, "assignee", "auth/user", task % base_users)
        edge("test/task", task, "requester", "auth/user", (task + 1) % base_users)
        edge("test/task", task, "reader", "auth/group", task % groups, "member")
        if task % 5 == 0:
            edge("test/task", task, "banned", "auth/user", (task + 2) % base_users)
    for item in range(200 * scale):
        edge("test/item", item, "task", "test/task", item % (100 * scale))
        edge("test/item", item, "other", "test/task", (item + 1) % (100 * scale))
    Relationship.objects.bulk_create(rows, batch_size=256)


def _fixture(
    *,
    users: int = 240,
    groups: int = 24,
    resources: int = 240,
    active_users: int | None = None,
) -> None:
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA))
    user_model = get_user_model()
    user_model._base_manager.bulk_create(
        [
            user_model(username=f"scale_{n}", is_staff=n % 3 != 0 and n < (active_users or users))
            for n in range(users)
        ],
        batch_size=256,
    )
    user_ids = list(user_model._base_manager.order_by("pk").values_list("pk", flat=True))
    BackingQueue._base_manager.bulk_create([BackingQueue() for _ in range(groups)])
    queue_ids = list(BackingQueue._base_manager.order_by("pk").values_list("pk", flat=True))
    BackingTask._base_manager.bulk_create(
        [
            BackingTask(queue_id=queue_ids[n % groups], asker_id=user_ids[(n * 7) % users])
            for n in range(resources)
        ],
        batch_size=256,
    )
    rows = []
    for group in range(groups):
        rows.append(
            Relationship(
                resource_type="test/role",
                resource_id=str(group),
                relation="member",
                subject_type="auth/group",
                subject_id=str(group),
                optional_subject_relation="member",
            )
        )
        rows.append(
            Relationship(
                resource_type="test/backingqueue",
                resource_id=str(queue_ids[group]),
                relation="role",
                subject_type="test/role",
                subject_id=str(group),
            )
        )
        for member in range(group % 4, users, groups):
            rows.append(
                Relationship(
                    resource_type="auth/group",
                    resource_id=str(group),
                    relation="member",
                    subject_type="auth/user",
                    subject_id=str(user_ids[member]),
                )
            )
    rows.append(
        Relationship(
            resource_type="test/admin",
            resource_id="admin",
            relation="member",
            subject_type="auth/user",
            subject_id=str(user_ids[0]),
        )
    )
    Relationship.objects.bulk_create(rows, batch_size=256)


def _measure(*, scale: int, user_factor: int = 1) -> dict[str, object]:
    with transaction.atomic():
        _scale_fixture(scale=scale, user_factor=user_factor)
        started = perf_counter()
        with CaptureQueriesContext(connection) as captured:
            stats = rebuild(using="default")
        elapsed = perf_counter() - started
        counts = {
            model._meta.db_table: model.objects.count()
            for model in (IndexTerm, IndexEdge, IndexMember, IndexCover)
        }
        measurements: dict[str, object] = {
            "seconds": elapsed,
            "statements": len(captured),
            "python_rows": stats.python_rows,
            "rows": counts,
            "savepoints": sum("SAVEPOINT" in row["sql"] for row in captured),
            "sql": [row["sql"] for row in captured],
        }
        transaction.set_rollback(True)
    return measurements


@pytest.mark.django_db(transaction=True)
def test_rebuild_scale_and_statement_budget():
    measurements = [_measure(scale=scale) for scale in (1, 2, 4)]
    for item in measurements:
        print("SCALE", {key: value for key, value in item.items() if key != "sql"})
    for before, after in pairwise(measurements):
        assert after["seconds"] <= 2.3 * before["seconds"]
        assert after["statements"] <= 2.3 * before["statements"]
        assert after["python_rows"] <= 2.3 * before["python_rows"]
        assert sum(after["rows"].values()) <= 2.3 * sum(before["rows"].values())
    base = measurements[0]
    assert base["savepoints"] <= 2 * 4
    assert base["statements"] / sum(base["rows"].values()) <= 0.03
    assert not any(re.search(r"\bOR\b[^()]*\bIN \(SELECT", sql) for sql in base["sql"])


@pytest.mark.django_db(transaction=True)
def test_fixed_resources_users_fourfold_and_membership_write_preserves_grants():
    baseline = _measure(scale=1)
    larger = _measure(scale=1, user_factor=4)
    print("USERS", {key: value for key, value in larger.items() if key != "sql"})
    assert larger["rows"]["rebac_grant"] == baseline["rows"]["rebac_grant"]
    assert larger["statements"] <= 1.5 * baseline["statements"]
    _scale_fixture(scale=1)
    rebuild(using="default")
    before = list(IndexCover.objects.order_by("pk").values())
    backend().write_relationships(
        [
            RelationshipTuple(
                ObjectRef("auth/group", "everyone"), "member", SubjectRef.of("auth/user", "new")
            )
        ]
    )
    assert list(IndexCover.objects.order_by("pk").values()) == before


@pytest.mark.django_db(transaction=True)
def test_consumer_shape_rebuild_is_bounded(caplog):
    _fixture()
    caplog.set_level("DEBUG", logger="rebac.index")
    query_rows: list[int] = []

    def measured(original):
        def rows(*args, **kwargs):
            count = 0
            for row in original(*args, **kwargs):
                count += 1
                yield row
            query_rows.append(count)

        return rows

    started = perf_counter()
    with (
        patch.object(write, "projected_rows", measured(write.projected_rows)),
        patch.object(project, "projected_rows", measured(project.projected_rows)),
    ):
        rebuild(using="default")
    elapsed = perf_counter() - started
    assert IndexMember.objects.exists()
    assert elapsed < 20, f"rebuild took {elapsed:.2f}s"
    assert query_rows
    assert max(query_rows) <= 20 * (240 + 24 + 240)
    rules = [
        record.message for record in caplog.records if "phase=nodes stratum=" in record.message
    ]
    assert rules
    assert max(
        int(match.group(1)) for message in rules if (match := re.search(r"rows_out=(\d+)", message))
    ) <= 20 * (240 + 24 + 240)
    task_ids = list(BackingTask._base_manager.order_by("pk").values_list("pk", flat=True))
    user_ids = list(get_user_model()._base_manager.order_by("pk").values_list("pk", flat=True))
    assert_index_matches(
        subjects=[SubjectRef.of("auth/user", str(pk)) for pk in user_ids[:4]],
        resources=[ObjectRef("test/backingtask", str(pk)) for pk in task_ids[:4]],
        actions=["manage"],
    )
    assert_no_drift()


@pytest.mark.postgresql
@pytest.mark.django_db
def test_read_lookup_plans_use_indexed_scope_and_member_keys():
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL only")
    # Source and derived rows remain uncommitted in pytest's outer transaction;
    # PostgreSQL cannot auto-analyze this fixture before the plain EXPLAIN.
    _fixture(users=120, groups=16, resources=120)
    rebuild(using="default")
    scope = (
        IndexCover.objects.filter(resource_type="test/backingtask")
        .values_list("scope_id", flat=True)
        .first()
    )
    assert scope is not None
    from rebac.index.program import program_for

    program = program_for(backend(), using="default")
    (site,) = program.held_sites(("test/backingtask", "manage"))
    operand = program.nodes[site].operands[0]
    covers = IndexCover.objects.filter(scope_id=scope, node=operand).select_related("holder")
    holder_ids = list(covers.values_list("holder_id", flat=True))
    assert holder_ids
    members = IndexMember.objects.filter(set_id__in=holder_ids).select_related("member")
    cover_plan, member_plan = covers.explain(), members.explain()
    assert "rebac_membership" not in cover_plan
    assert "rebac_grant" not in member_plan
    for plan in (cover_plan, member_plan):
        assert "Join Filter" not in plan
        assert "Nested Loop" not in plan or "Index Cond" in plan


def _statements_of_one_write(*, scale: int, extra_definitions: int = 0) -> dict[str, int]:
    """Statements of single writes, on an index of the given size and schema."""
    reset_backend()
    extra = "".join(
        f"""
definition extra/kind{n} {{
    relation viewer: auth/user | auth/group#member
    relation parent: extra/kind{n}
    permission read = viewer + parent->read
}}
"""
        for n in range(extra_definitions)
    )
    with transaction.atomic():
        _scale_fixture(scale=scale)
        backend().set_schema(parse_zed(LEOPARD_SCALE_SCHEMA + extra))
        rebuild(using="default")
        actor = SubjectRef.of("auth/user", "3")
        # Resources nothing depends on, so the region is the same at every
        # scale; a region with dependents grows with them, in batches.
        writes = {
            "task": RelationshipTuple(ObjectRef("test/task", "fresh"), "assignee", actor),
            "project": RelationshipTuple(ObjectRef("test/project", "fresh"), "owner", actor),
            "unread group": RelationshipTuple(
                ObjectRef("auth/group", "everyone"), "member", SubjectRef.of("auth/user", "new")
            ),
        }
        counts = {}
        for name, write in writes.items():
            with CaptureQueriesContext(connection) as captured:
                backend().write_relationships([write])
            counts[name] = len(captured)
            # No pass counts the relationships.
            for row in captured:
                sql = row["sql"].upper()
                assert not ("COUNT(" in sql and "RELATIONSHIP" in sql), row["sql"][:200]
        assert_no_drift()
        transaction.set_rollback(True)
    return counts


@pytest.mark.django_db(transaction=True)
def test_one_write_costs_the_same_whatever_the_size_of_the_index_and_of_the_schema():
    base = _statements_of_one_write(scale=1)
    print("WRITE", base)
    # Twice the resources, users and relationships.
    assert _statements_of_one_write(scale=2) == base
    # Forty definitions the write has nothing to do with.
    assert _statements_of_one_write(scale=1, extra_definitions=40) == base
    # The pass of a type costs a few statements for each of its relations and
    # nodes: `test/task` has eight relations and seven permissions.
    assert max(base.values()) <= 200
