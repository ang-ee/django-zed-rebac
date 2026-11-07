"""An actor that holds a constant arm reads a short statement.

A constant arm of a permission (``admin->member`` over a ``rebac:const``
relation) names one fixed object, so it holds on every object of the type or on
none. One statement serves every actor of a shape, so the policy's statement
carries the whole policy for an administrator too. Inside an evaluator scope a
witness decides the permission's constant arms once per request: an actor who
holds one reads a statement of those arms alone, which still evaluates them when
it runs; for every other actor they leave the statement, and with them the union
branch that lists every row of the model.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    evaluator_scope,
    permission_expression,
    sudo,
    to_object_ref,
)
from rebac.compile import read
from rebac.models import Relationship
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

# Emits SQL and branches on the vendor (the plan test): tier 2 runs it too.
pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]

ADMIN = SubjectRef.of("auth/user", "admin")
MEMBER = SubjectRef.of("auth/user", "member")
# A console-like policy: a chain of types, each with subject-set arms and an
# arrow to the next type, under two model types that carry the administrator's
# constant arm, as an application console declares one per model type. (A
# constant backing needs the declaring type's model, so the chain has none.)
CHAIN = 6
ARMS = 4


def _console() -> str:
    lines = [
        "definition auth/user {}",
        "definition auth/group {",
        "    relation member: auth/user | auth/group#member",
        "}",
        "definition org/role {",
        "    relation member: auth/user | auth/group#member",
        "}",
    ]
    for level in range(CHAIN):
        lines.append(f"definition console/t{level} {{")
        lines.append("    relation viewer: auth/user | auth/group#member")
        arms = ["viewer"]
        for arm in range(ARMS):
            lines.append(f"    relation side{arm}: auth/group")
            arms.append(f"side{arm}->member")
        if level + 1 < CHAIN:
            lines.append(f"    relation up: console/t{level + 1}")
            arms.append("up->view")
        lines.append(f"    permission view = {' + '.join(arms)}")
        lines.append("}")
    lines += [
        "definition blog/folder {",
        "    relation project: console/t0",
        "    relation viewer: auth/user | auth/group#member",
        "    relation admin: org/role // rebac:const=admin",
        "    permission read = viewer + project->view + admin->member",
        "}",
        "definition blog/post {",
        "    relation folder: blog/folder // rebac:field=folder",
        "    relation viewer: auth/user | auth/group#member",
        "    relation editor: auth/user | auth/group#member",
        "    relation admin: org/role // rebac:const=admin",
        "    permission read = folder->read + viewer + editor + admin->member",
        "}",
    ]
    return "\n".join(lines)


@pytest.fixture
def console():
    """The console policy: two folders of three posts, an administrator and a member.

    The member reads the first folder through the console chain and one post of
    the second folder directly.
    """
    local = install_schema(_console())
    with sudo(reason="constant arms fixture"):
        folders = [Folder.objects.create(name=f"folder-{index}") for index in range(2)]
        posts = [
            Post.objects.create(title=f"post-{index}", folder=folders[index // 3])
            for index in range(6)
        ]
    tuples = [
        RelationshipTuple(ObjectRef("org/role", "admin"), "member", ADMIN),
        RelationshipTuple(to_object_ref(folders[0]), "project", SubjectRef.of("console/t0", "0")),
        RelationshipTuple(ObjectRef(f"console/t{CHAIN - 1}", "0"), "viewer", MEMBER),
        RelationshipTuple(to_object_ref(posts[4]), "viewer", MEMBER),
    ]
    tuples += [
        RelationshipTuple(
            ObjectRef(f"console/t{level}", "0"), "up", SubjectRef.of(f"console/t{level + 1}", "0")
        )
        for level in range(CHAIN - 1)
    ]
    local.write_relationships(tuples)
    read.reset()
    return local, folders, posts


def _read(actor: SubjectRef, **filters: object) -> tuple[list[int], list[str]]:
    with CaptureQueriesContext(connection) as captured:
        ids = list(
            Post.objects.with_actor(actor)
            .filter(**filters)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
    return ids, [query["sql"] for query in captured.captured_queries]


def test_an_actor_holding_a_constant_arm_reads_without_the_policy(console):
    _local, _folders, posts = console
    with evaluator_scope():
        admin_ids, admin_sql = _read(ADMIN)
    with evaluator_scope():
        member_ids, member_sql = _read(MEMBER)
    assert admin_ids == [post.pk for post in posts]
    assert member_ids == [posts[0].pk, posts[1].pk, posts[2].pk, posts[4].pk]
    # One witness, then the read.
    assert len(admin_sql) == len(member_sql) == 2
    admin_read, member_read = admin_sql[-1], member_sql[-1]
    # Measured on SQLite: 2,224 bytes, after a witness of 2,263 (most of either
    # is the actor's nested groups), against 47,179 for the member's read; on
    # 0.31.3 both actors read the policy's 47,920 bytes in one statement.
    assert len(admin_read) < 2_560
    assert len(admin_sql[0]) < 2_560
    assert len(member_read) > 16 * len(admin_read)
    # The administrator's statement reads no console type and no post row.
    assert "console/t" not in admin_read
    assert admin_read.count('FROM "testapp_post"') == 1


def test_the_witness_runs_once_per_request(console):
    _local, folders, _posts = console
    for actor in (ADMIN, MEMBER):
        with evaluator_scope():
            _ids, first = _read(actor)
            # Another read of the same permission, and one of another type
            # whose constant arm is the same fact, decide nothing again.
            _ids, again = _read(actor, folder=folders[1])
            with CaptureQueriesContext(connection) as captured:
                list(Folder.objects.with_actor(actor).values_list("pk", flat=True))
        assert (len(first), len(again), len(captured.captured_queries)) == (2, 1, 1)
        with evaluator_scope():
            _ids, next_request = _read(actor)
        assert len(next_request) == 2


def test_outside_an_evaluator_scope_the_statement_keeps_every_arm(console):
    _local, _folders, posts = console
    admin_ids, admin_sql = _read(ADMIN)
    member_ids, member_sql = _read(MEMBER)
    assert admin_ids == [post.pk for post in posts]
    assert len(member_ids) == 4
    # One statement each: the policy's, the same for both actors but for the
    # actor's id, which the captured SQL interpolates.
    assert len(admin_sql) == len(member_sql) == 1
    assert admin_sql[0].replace("('admin')", "('member')") == member_sql[0]


def test_the_member_statement_has_no_branch_for_the_constant_arm(console):
    with evaluator_scope():
        _ids, inside = _read(MEMBER)
    _ids, outside = _read(MEMBER)
    assert len(inside[-1]) < len(outside[-1])
    # The constant arm's union branch selected every post row; without it the
    # post table is read by key only.
    assert inside[-1].count('FROM "testapp_post"') < outside[-1].count('FROM "testapp_post"')


def test_a_stale_witness_narrows_a_read_and_never_widens_it(console):
    local, _folders, posts = console
    local.write_relationships([RelationshipTuple(to_object_ref(posts[5]), "editor", ADMIN)])
    admin_tuple = Relationship.objects.get(
        resource_type="org/role", resource_id="admin", relation="member"
    )
    table = connection.ops.quote_name(Relationship._meta.db_table)
    with evaluator_scope():
        assert _read(ADMIN)[0] == [post.pk for post in posts]
        # Revoked behind the library: the witness is not told.
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table} WHERE id = %s", [admin_tuple.pk])
        # The statement it chose evaluates the constant arm when it runs: it
        # admits nothing, not even the post the actor edits.
        assert _read(ADMIN)[0] == []
    with evaluator_scope():
        assert _read(ADMIN)[0] == [posts[5].pk]


def test_a_constant_arm_granted_within_a_request_waits_for_the_next(console):
    _local, _folders, posts = console
    table = connection.ops.quote_name(Relationship._meta.db_table)
    with sudo(reason="constant arms fixture"):
        # Only the post's own constant arm reaches a post without a folder; the
        # folders' constant arms are evaluated where the folders are read.
        alone = Post.objects.create(title="no folder")
    with evaluator_scope():
        assert alone.pk not in _read(MEMBER)[0]
        # Granted behind the library: the member's read keeps the arm out
        # until the next request.
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {table} (resource_type, resource_id, relation, subject_type, "
                "subject_id, optional_subject_relation, caveat_name, caveat_context, "
                "caveat_key, written_at_xid) VALUES ('org/role', 'admin', 'member', "
                "'auth/user', 'member', '', '', NULL, '', 0)"
            )
        assert alone.pk not in _read(MEMBER)[0]
    with evaluator_scope():
        assert _read(MEMBER)[0] == [*(post.pk for post in posts), alone.pk]


def test_a_revocation_through_the_library_is_seen_in_the_same_request(console):
    local, _folders, posts = console
    local.write_relationships([RelationshipTuple(to_object_ref(posts[5]), "editor", ADMIN)])
    with evaluator_scope():
        assert _read(ADMIN)[0] == [post.pk for post in posts]
        local.delete_relationship(
            RelationshipTuple(ObjectRef("org/role", "admin"), "member", ADMIN)
        )
        ids, statements = _read(ADMIN)
    # The write invalidated the witness: it is decided again, and the read keeps
    # the rows the actor holds through the other arms.
    assert ids == [posts[5].pk]
    assert len(statements) == 2


def test_a_point_check_of_an_administrator_is_the_witness(console):
    local, _folders, posts = console
    with evaluator_scope():
        with CaptureQueriesContext(connection) as captured:
            assert local.check_access(
                subject=ADMIN, action="read", resource=to_object_ref(posts[5])
            ).allowed
    assert len(captured.captured_queries) == 1
    assert len(captured.captured_queries[0]["sql"]) < 2_560
    with evaluator_scope():
        assert local.check_access(
            subject=MEMBER, action="read", resource=to_object_ref(posts[4])
        ).allowed
        assert not local.check_access(
            subject=MEMBER, action="read", resource=to_object_ref(posts[5])
        ).allowed


def test_a_permission_projection_of_an_administrator_is_the_constant_statement(console):
    _local, _folders, posts = console
    rows = Post._base_manager.annotate(
        readable=permission_expression(Post, "read", actor=ADMIN)
    ).order_by("pk")
    with evaluator_scope():
        with CaptureQueriesContext(connection) as captured:
            assert list(rows.values_list("readable", flat=True)) == [True] * len(posts)
    # The projection decides the witness when its SQL is compiled, then reads.
    assert len(captured.captured_queries) == 2
    assert len(captured.captured_queries[-1]["sql"]) < 2_560


@pytest.mark.parametrize(
    ("permission", "admin_reads"),
    [
        # Through a permission of the same type: still a constant arm.
        ("viewer + keeper", "every"),
        # Beside an exclusion that it is not under: it holds for every row.
        ("(viewer - banned) + admin->member", "every"),
        # Under an exclusion: the excluded row stays excluded.
        ("(viewer + admin->member) - banned", "unbanned"),
        # Under an intersection: it decides nothing alone.
        ("(admin->member & viewer) + editor", "viewed"),
    ],
)
def test_only_a_disjunct_of_the_permission_is_a_constant_arm(permission, admin_reads):
    local = install_schema(
        "\n".join(
            [
                "definition auth/user {}",
                "definition org/role { relation member: auth/user }",
                "definition blog/post {",
                "    relation viewer: auth/user",
                "    relation editor: auth/user",
                "    relation banned: auth/user",
                "    relation admin: org/role // rebac:const=admin",
                "    permission keeper = editor + admin->member",
                f"    permission read = {permission}",
                "}",
            ]
        )
    )
    with sudo(reason="constant arms shapes"):
        posts = [Post.objects.create(title=f"post-{index}") for index in range(3)]
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("org/role", "admin"), "member", ADMIN),
            *(
                RelationshipTuple(to_object_ref(posts[index]), relation, actor)
                for actor in (ADMIN, MEMBER)
                for index, relation in ((0, "viewer"), (1, "banned"), (2, "editor"))
            ),
        ]
    )
    expected = {
        "every": [post.pk for post in posts],
        "unbanned": [posts[0].pk, posts[2].pk],
        "viewed": [posts[0].pk, posts[2].pk],
    }[admin_reads]
    # Outside an evaluator scope the statement keeps every arm: the reference.
    reference = {ADMIN: _read(ADMIN)[0], MEMBER: _read(MEMBER)[0]}
    assert reference[ADMIN] == expected
    for actor in (ADMIN, MEMBER):
        with evaluator_scope():
            assert _read(actor)[0] == reference[actor]
            for post in posts:
                assert (
                    local.check_access(subject=actor, action="read", resource=to_object_ref(post))
                ).allowed is (post.pk in reference[actor])


@pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite's plans read no statistics")
@pytest.mark.parametrize("actor", [ADMIN, MEMBER], ids=["administrator", "member"])
def test_a_parent_to_child_read_does_not_scan_the_child_table(console, actor):
    _local, folders, posts = console
    with evaluator_scope():
        ids, statements = _read(actor, folder=folders[0])
    assert ids == [post.pk for post in posts[:3]]
    # SQLite's captured statement has its parameters interpolated.
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN QUERY PLAN {statements[-1]}")
        plan = [str(row[-1]) for row in cursor.fetchall()]
    table = Post._meta.db_table
    steps = [step for step in plan if step.split()[1:2] == [table]]
    assert steps, plan
    # The outer read seeks the folder's posts by key; no step scans the table.
    assert steps[0].startswith(f"SEARCH {table} USING ") and "(folder_id=?)" in steps[0], plan
    assert not any(step.startswith(f"SCAN {table}") for step in steps), plan
