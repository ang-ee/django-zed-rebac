"""An actor that holds a constant arm reads a short statement.

A constant arm of a permission (``admin->member`` over a ``rebac:const``
relation) names one fixed object, so it holds on every object of the type or on
none. One statement serves every actor of a shape, so the policy's statement
carries the whole policy for an administrator too. Inside an evaluator scope a
witness statement decides the permission's constant arms: an actor who holds
one reads a statement of those arms alone, which evaluates them again when it
runs. Where the decision cache may keep a decision about the types the arms
name (no transaction, no model-backed fact in their reach, no expiring
relation) and the call states no consistency requirement, the evaluator keeps
the answer for the request, and for every other actor the arms leave the
statement, with the union branch that lists every row of the model. Elsewhere
the witness is decided at each read and the statement keeps every arm.

Tests that need the kept answer run outside a transaction (``KEPT``).
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    backend,
    evaluator_scope,
    permission_expression,
    sudo,
    to_object_ref,
)
from rebac.backends import reset_backend
from rebac.compile import read
from rebac.models import (
    Relationship,
    SchemaDefinition,
    SchemaOverride,
    SchemaPermission,
    SchemaRelation,
)
from rebac.testing import install_schema
from rebac.types import CheckItem, Consistency
from tests.backend_setup import atomic_source_write
from tests.reference_harness import assert_scope_matches
from tests.testapp.models import AuthoredPost, Folder, Post

# Emits SQL and branches on the vendor (the plan test): tier 2 runs it too.
pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
# The decision cache keeps nothing inside a transaction.
KEPT = pytest.mark.django_db(transaction=True)

ADMIN = SubjectRef.of("auth/user", "admin")
MEMBER = SubjectRef.of("auth/user", "member")
OUTSIDER = SubjectRef.of("auth/user", "outsider")
# The role object itself: the subject a constant relation used directly names.
ROLE = SubjectRef.of("org/role", "admin")
# A console-like policy: a chain of types, each with subject-set arms and an
# arrow to the next type, under two model types that carry the administrator's
# constant arm, as an application console declares one per model type. (A
# constant backing needs the declaring type's model, so the chain has none.)
CHAIN = 6
ARMS = 4
POST_ARMS = "folder->read + viewer + editor"


# The user type of an application often carries a model-backed relation (a
# person, a profile) that most of its policy never reads. Here an active user
# holds ``active`` on itself; only the desk's permission reads it.
LIVE_USER = "\n".join(
    [
        "definition auth/user {",
        '    relation active: auth/user // rebac:attribute={"field":"id","filters":{"is_active":true}}',
        "}",
        "definition console/desk {",
        "    relation owner: auth/user",
        "    permission view = owner->active",
        "}",
    ]
)


def _console(
    post_read: str = f"{POST_ARMS} + admin->member", user: str = "definition auth/user {}"
) -> str:
    lines = [
        user,
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
        f"    permission read = {post_read}",
        "}",
    ]
    return "\n".join(lines)


@pytest.fixture
def console():
    """The console policy: two folders of three posts, an administrator and a member.

    The member reads the first folder through the console chain and one post of
    the second folder directly.
    """
    return _install_console(_console())


@pytest.fixture
def live_user_console():
    """The console, its user type carrying a model-backed relation (``LIVE_USER``)."""
    return _install_console(_console(user=LIVE_USER))


def _install_console(policy: str):
    local = install_schema(policy)
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


def _check(local, actor: SubjectRef, post: Post, **options: object) -> bool:
    return local.check_access(
        subject=actor, action="read", resource=to_object_ref(post), **options
    ).allowed


def _table() -> str:
    return connection.ops.quote_name(Relationship._meta.db_table)


def _revoke_behind_the_library(subject_id: str) -> None:
    row = Relationship.objects.get(
        resource_type="org/role", resource_id="admin", relation="member", subject_id=subject_id
    )
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {_table()} WHERE id = %s", [row.pk])


def _grant_behind_the_library(subject_id: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {_table()} (resource_type, resource_id, relation, subject_type, "
            "subject_id, optional_subject_relation, caveat_name, caveat_context, "
            "caveat_key, written_at_xid) VALUES ('org/role', 'admin', 'member', "
            "'auth/user', %s, '', '', NULL, '', 0)",
            [subject_id],
        )


def _without_revision(sql: str) -> str:
    """A statement without the manual schema's revision, which names the policy text."""
    sql = re.sub(r"'[0-9a-f]{32}'", "'revision'", sql)
    return re.sub(r"rebac_fence_[0-9a-f]{16}", "rebac_fence_revision", sql)


@KEPT
def test_an_actor_holding_a_constant_arm_reads_without_the_policy(console):
    _local, _folders, posts = console
    with evaluator_scope():
        ids, statements = _read(ADMIN)
    assert ids == [post.pk for post in posts]
    # The witness, then the read.
    assert len(statements) == 2
    statement = statements[-1]
    # The read is the policy fence and the constant arm: no arm of the post,
    # no folder, no console type, and the post table only as the selecting
    # query's own.
    assert statement.count('FROM "testapp_post"') == 1
    assert "testapp_folder" not in statement
    assert "console/t" not in statement
    assert "'blog/post'" not in statement


@KEPT
def test_the_member_statement_is_the_policy_without_its_constant_arms(console):
    _local, _folders, posts = console
    with evaluator_scope():
        ids, statements = _read(MEMBER)
    assert ids == [posts[0].pk, posts[1].pk, posts[2].pk, posts[4].pk]
    # The same policy without the post's constant arm, read outside a scope.
    install_schema(_console(POST_ARMS))
    read.reset()
    reference_ids, reference = _read(MEMBER)
    assert reference_ids == ids
    assert _without_revision(statements[-1]) == _without_revision(reference[-1])


@KEPT
def test_the_witness_is_kept_for_the_request(console):
    _local, folders, _posts = console
    for actor in (ADMIN, MEMBER):
        with evaluator_scope() as evaluator:
            _ids, first = _read(actor)
            # Another read of the same permission, and one of another type
            # whose constant arm is the same fact, decide nothing again.
            _ids, again = _read(actor, folder=folders[1])
            with CaptureQueriesContext(connection) as captured:
                list(Folder.objects.with_actor(actor).values_list("pk", flat=True))
            assert evaluator.stats()["constant_witness_entries"] == 1
        assert (len(first), len(again), len(captured.captured_queries)) == (2, 1, 1)
        with evaluator_scope():
            _ids, next_request = _read(actor)
        assert len(next_request) == 2


@KEPT
def test_a_model_backed_relation_of_the_user_type_leaves_the_types_that_never_read_it_eligible(
    live_user_console,
):
    local, folders, _posts = live_user_console
    # Every type admits the user as a subject; only the types whose decisions
    # read a model column are live: the user's own, the post's (its folder is a
    # column) and the desk's, whose permission reads the user's relation.
    for type_ in ("org/role", "auth/group", "blog/folder", "console/t0", f"console/t{CHAIN - 1}"):
        assert local._cache_generation(type_) is not None, type_
    for type_ in ("auth/user", "blog/post", "console/desk"):
        assert local._cache_generation(type_) is None, type_
    # So the witness of ``org/role`` is kept for the request: one per request
    # for the administrator, as for the member.
    for actor in (ADMIN, MEMBER):
        with evaluator_scope() as evaluator:
            _ids, first = _read(actor)
            _ids, again = _read(actor, folder=folders[1])
            with CaptureQueriesContext(connection) as captured:
                list(Folder.objects.with_actor(actor).values_list("pk", flat=True))
            assert evaluator.stats()["constant_witness_entries"] == 1
        assert (len(first), len(again), len(captured.captured_queries)) == (2, 1, 1)
    for actor in (ADMIN, MEMBER, OUTSIDER):
        with evaluator_scope():
            # The kept witness first, so that the comparison reads the
            # statements it chooses.
            _read(actor)
            assert_scope_matches(Post, actor=actor, action="read")
            assert_scope_matches(Folder, actor=actor, action="read")


@KEPT
def test_a_type_whose_policy_reads_the_users_model_backed_relation_stays_live(
    live_user_console,
):
    local, _folders, _posts = live_user_console
    owner = atomic_source_write(get_user_model().objects.create_user, username="owner")
    actor = SubjectRef.of("auth/user", str(owner.pk))
    desk = ObjectRef("console/desk", "1")
    local.write_relationships([RelationshipTuple(desk, "owner", actor)])
    with evaluator_scope() as evaluator:
        assert evaluator.check(local, subject=actor, action="view", resource=desk).allowed
        # A model write: the next check reads the column.
        owner.is_active = False
        with sudo(reason="constant arms fixture"):
            atomic_source_write(owner.save, update_fields=["is_active"])
        assert not evaluator.check(local, subject=actor, action="view", resource=desk).allowed
        assert evaluator.stats()["check_entries"] == 0


def test_inside_a_transaction_a_held_arm_is_decided_at_each_read(console):
    local, _folders, posts = console
    _ids, policy = _read(MEMBER)
    with evaluator_scope() as evaluator:
        admin = [_read(ADMIN) for _read_again in range(2)]
        member = [_read(MEMBER) for _read_again in range(2)]
        with CaptureQueriesContext(connection) as checks:
            assert _check(local, ADMIN, posts[5])
            assert _check(local, MEMBER, posts[4])
        assert evaluator.stats()["constant_witness_entries"] == 2
    # The administrator: the witness, then the constant statement, every time.
    for ids, statements in admin:
        assert ids == [post.pk for post in posts]
        assert len(statements) == 2
    # The member: one witness for the request; its remembered answer selects
    # the policy's statement, every arm in it, and runs nothing.
    (first_ids, first), (again_ids, again) = member
    assert len(first_ids) == len(again_ids) == 4
    assert len(first) == 2 and len(again) == 1
    assert first[-1] == again[-1] == policy[-1]
    # A check: the witness for the administrator, the lower bound for the member.
    assert len(checks.captured_queries) == 2


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


@KEPT
def test_a_stale_witness_narrows_a_read_and_never_widens_it(console):
    local, _folders, posts = console
    local.write_relationships([RelationshipTuple(to_object_ref(posts[5]), "editor", ADMIN)])
    with evaluator_scope():
        assert _read(ADMIN)[0] == [post.pk for post in posts]
        _revoke_behind_the_library("admin")
        # The statement it chose evaluates the constant arm when it runs: it
        # admits nothing, not even the post the actor edits.
        assert _read(ADMIN)[0] == []
    with evaluator_scope():
        assert _read(ADMIN)[0] == [posts[5].pk]


@KEPT
def test_a_constant_arm_granted_within_a_request_waits_for_the_next_read_scope(console):
    _local, _folders, posts = console
    with sudo(reason="constant arms fixture"):
        # Only the post's own constant arm reaches a post without a folder; the
        # folders' constant arms are evaluated where the folders are read.
        alone = Post.objects.create(title="no folder")
    with evaluator_scope():
        assert alone.pk not in _read(MEMBER)[0]
        _grant_behind_the_library("member")
        # The scope keeps the arm out until the next request.
        assert alone.pk not in _read(MEMBER)[0]
    with evaluator_scope():
        assert _read(MEMBER)[0] == [*(post.pk for post in posts), alone.pk]


@KEPT
def test_after_a_write_behind_the_library_a_read_and_a_check_can_disagree_until_the_scope_ends(
    console,
):
    local, _folders, _posts = console
    with sudo(reason="constant arms fixture"):
        alone = Post.objects.create(title="no folder")
    with evaluator_scope():
        assert alone.pk not in _read(MEMBER)[0]
        _grant_behind_the_library("member")
        # The check evaluates every arm; the read keeps the arms out (a known
        # limit: the read is narrower, never wider).
        assert _check(local, MEMBER, alone)
        assert alone.pk not in _read(MEMBER)[0]
    with evaluator_scope():
        assert alone.pk in _read(MEMBER)[0]


@KEPT
def test_a_point_check_decides_the_constant_arms_again(console):
    local, _folders, posts = console
    with sudo(reason="constant arms fixture"):
        alone = Post.objects.create(title="no folder")
    with evaluator_scope():
        with CaptureQueriesContext(connection) as captured:
            assert _check(local, ADMIN, posts[5])
        # The witness is the check.
        assert len(captured.captured_queries) == 1
        _revoke_behind_the_library("admin")
        # A remembered answer never grants by itself.
        assert not _check(local, ADMIN, posts[5])
    with evaluator_scope():
        # The witness is kept as false for the member.
        assert not _check(local, MEMBER, alone)
        _grant_behind_the_library("member")
        # A point check never leaves the constant arms out: it sees the grant,
        # without a depth error.
        assert _check(local, MEMBER, alone)


@KEPT
def test_a_revocation_through_the_library_is_seen_in_the_same_request(console):
    local, _folders, posts = console
    local.write_relationships([RelationshipTuple(to_object_ref(posts[5]), "editor", ADMIN)])
    with evaluator_scope():
        assert _read(ADMIN)[0] == [post.pk for post in posts]
        local.delete_relationship(
            RelationshipTuple(ObjectRef("org/role", "admin"), "member", ADMIN)
        )
        ids, statements = _read(ADMIN)
        # The write changed the decision cache's generation: the witness is
        # decided again, and the read keeps the rows the other arms admit.
        assert ids == [posts[5].pk]
        assert len(statements) == 2
        assert _check(local, ADMIN, posts[5])
        assert not _check(local, ADMIN, posts[0])


@KEPT
def test_a_rolled_back_revocation_leaves_nothing_decided(console):
    local, _folders, posts = console
    with sudo(reason="constant arms fixture"):
        alone = Post.objects.create(title="no folder")
    with evaluator_scope():
        assert _read(ADMIN)[0] == [post.pk for post in posts] + [alone.pk]
        with pytest.raises(RuntimeError), transaction.atomic():
            local.delete_relationship(
                RelationshipTuple(ObjectRef("org/role", "admin"), "member", ADMIN)
            )
            assert not _check(local, ADMIN, alone)
            assert _read(ADMIN)[0] == []
            raise RuntimeError("roll back")
        assert _check(local, ADMIN, alone)
        assert _read(ADMIN)[0] == [post.pk for post in posts] + [alone.pk]


@KEPT
def test_a_model_backed_constant_arm_reads_the_policy_statement():
    users = get_user_model().objects
    owner = atomic_source_write(users.create_user, username="owner")
    member = atomic_source_write(users.create_user, username="member")
    with sudo(reason="constant arms fixture"):
        role = AuthoredPost.objects.create(title="role", author=owner)
        posts = [Post.objects.create(title=f"post-{index}") for index in range(3)]
    local = install_schema(
        "\n".join(
            [
                "definition auth/user {}",
                "definition blog/authoredpost {",
                "    relation member: auth/user // rebac:field=author",
                "}",
                "definition blog/post {",
                "    relation viewer: auth/user",
                f"    relation admin: blog/authoredpost // rebac:const={role.pk}",
                "    permission read = viewer + admin->member",
                "}",
            ]
        )
    )
    actor = SubjectRef.of("auth/user", str(member.pk))
    local.write_relationships([RelationshipTuple(to_object_ref(posts[0]), "viewer", actor)])
    read.reset()
    with evaluator_scope() as evaluator:
        assert _read(actor)[0] == [posts[0].pk]
        # A model write: the decision cache keeps nothing for a type whose
        # facts are model columns, so the remembered answer only selects the
        # policy's statement, which reads the column.
        with sudo(reason="constant arms fixture"):
            atomic_source_write(AuthoredPost._base_manager.filter(pk=role.pk).update, author=member)
        ids, statements = _read(actor)
        assert ids == [post.pk for post in posts]
        assert len(statements) == 1
        assert _check(local, actor, posts[2])
        assert evaluator.stats()["constant_witness_entries"] == 1


@KEPT
def test_a_consistency_requirement_keeps_nothing(console):
    local, _folders, _posts = console
    with sudo(reason="constant arms fixture"):
        alone = Post.objects.create(title="no folder")
    with evaluator_scope():
        assert not _check(local, MEMBER, alone)
        assert str(alone.pk) not in local.accessible(
            subject=MEMBER, action="read", resource_type="blog/post"
        )
        _grant_behind_the_library("member")
        assert _check(local, MEMBER, alone, consistency=Consistency.FULLY_CONSISTENT)
        assert str(alone.pk) in local.accessible(
            subject=MEMBER,
            action="read",
            resource_type="blog/post",
            consistency=Consistency.FULLY_CONSISTENT,
        )


@KEPT
@pytest.mark.parametrize("actor", [ADMIN, MEMBER, OUTSIDER], ids=["admin", "member", "outsider"])
def test_the_operations_agree_inside_a_scope(console, actor):
    local, _folders, posts = console
    ids = [str(post.pk) for post in posts]
    with evaluator_scope():
        # The scope decides and keeps the witness first.
        read_ids = {str(pk) for pk in _read(actor)[0]}
        checked = {
            pk
            for pk in ids
            if local.check_access(
                subject=actor, action="read", resource=ObjectRef("blog/post", pk)
            ).allowed
        }
        bulk = local.check_bulk_permissions(
            [CheckItem(actor, "read", ObjectRef("blog/post", pk)) for pk in ids]
        )
        held = local.held(subject=actor, action="read", resource_type="blog/post", ids=ids)
        held_many = local.held_many(
            subject=actor, actions=["read"], resource_type="blog/post", ids=ids
        )
        accessible = set(local.accessible(subject=actor, action="read", resource_type="blog/post"))
        looked_up = {
            pk
            for pk in ids
            if actor
            in local.lookup_subjects(
                resource=ObjectRef("blog/post", pk), action="read", subject_type="auth/user"
            )
        }
        every = local.grants_all(subject=actor, action="read", resource_type="blog/post")
    assert checked == read_ids
    assert {pk for pk, answer in zip(ids, bulk, strict=True) if answer.allowed} == read_ids
    assert held == held_many["read"] == read_ids
    assert accessible & set(ids) == read_ids
    assert looked_up == read_ids
    assert every is (actor == ADMIN)


@KEPT
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
    assert "console/t" not in captured.captured_queries[-1]["sql"]


def test_a_permission_that_holds_outright_runs_no_witness():
    install_schema(
        "\n".join(
            [
                "definition auth/user {}",
                "definition org/role { relation member: auth/user }",
                "definition blog/post {",
                "    relation viewer: auth/user",
                "    relation admin: org/role // rebac:const=admin",
                "    permission read = viewer + authenticated + admin->member",
                "}",
            ]
        )
    )
    with sudo(reason="constant arms shapes"):
        posts = [Post.objects.create(title=f"post-{index}") for index in range(2)]
    with evaluator_scope():
        ids, statements = _read(MEMBER)
    assert ids == [post.pk for post in posts]
    assert len(statements) == 1


# Policies over posts whose constant arms sit in each position the analysis
# distinguishes, with the tuples of three actors. ``MEMBER`` views post 0, is
# banned from post 1 and edits post 2.
SHAPES = {
    # Through a permission of the same type: still a constant arm.
    "through-a-permission": ("viewer + keeper", ""),
    # Beside an exclusion that it is not under: it holds for every row.
    "beside-an-exclusion": ("(viewer - banned) + admin->member", ""),
    # Under an exclusion: the excluded row stays excluded.
    "under-an-exclusion": ("(viewer + admin->member) - banned", ""),
    # Under an intersection: it decides nothing alone.
    "under-an-intersection": ("(admin->member & viewer) + editor", ""),
    # A constant relation used directly: it names the role object itself, and
    # holds or fails for an actor's shape, with nothing to witness.
    "constant-relation": ("viewer + admin", ""),
    # A caveated membership, decided from its pinned context.
    "caveated": ("viewer + admin->member", "caveated"),
    # An expiring membership: the decision cache keeps nothing.
    "expiring": ("viewer + admin->member", "expiring"),
}


def _shape(permission: str, membership: str) -> None:
    role = {
        "": "relation member: auth/user",
        "caveated": "relation member: auth/user with allowed",
        "expiring": "relation member: auth/user with expiration",
    }[membership]
    local = install_schema(
        "\n".join(
            [
                "caveat allowed(flag bool) { flag }",
                "definition auth/user {}",
                f"definition org/role {{ {role} }}",
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
    now = timezone.now()
    memberships = {
        "": [(ADMIN, "", {}, None)],
        "caveated": [
            (ADMIN, "allowed", {"flag": True}, None),
            (MEMBER, "allowed", {"flag": False}, None),
        ],
        "expiring": [
            (ADMIN, "", {}, now + timedelta(days=1)),
            (MEMBER, "", {}, now - timedelta(days=1)),
        ],
    }[membership]
    local.write_relationships(
        [
            *(
                RelationshipTuple(ObjectRef("org/role", "admin"), "member", actor, *rest)
                for actor, *rest in memberships
            ),
            *(
                RelationshipTuple(to_object_ref(posts[index]), relation, actor)
                for actor in (ADMIN, MEMBER)
                for index, relation in ((0, "viewer"), (1, "banned"), (2, "editor"))
            ),
        ]
    )
    read.reset()


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_only_a_disjunct_of_the_permission_is_a_constant_arm(shape):
    _shape(*SHAPES[shape])
    reference = {actor: _read(actor)[0] for actor in (ADMIN, MEMBER, OUTSIDER, ROLE)}
    for actor in reference:
        with evaluator_scope():
            assert _read(actor)[0] == reference[actor]
    if shape == "constant-relation":
        assert len(reference[ROLE]) == 3
        with evaluator_scope():
            # Decided from the actor's shape when the statement is compiled.
            assert len(_read(ROLE)[1]) == len(_read(ADMIN)[1]) == 1


@KEPT
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_reads_match_the_references_inside_and_outside_a_scope(shape):
    _shape(*SHAPES[shape])
    for actor in (ADMIN, MEMBER, OUTSIDER, ROLE):
        assert_scope_matches(Post, actor=actor, action="read")
        with evaluator_scope():
            # The scope's witness first, so that the comparison reads the
            # statements it chooses.
            _read(actor)
            assert_scope_matches(Post, actor=actor, action="read")


@KEPT
def test_the_witness_is_kept_per_database(tmp_path):
    from django.db import connections

    from rebac.models.generation import SchemaGeneration
    from tests.backend_setup import sqlite_alias

    alias = "witness_other"
    other = sqlite_alias(alias, tmp_path / "other.sqlite3")
    connections[alias] = other
    try:
        with other.schema_editor() as editor:
            for model in (Folder, Post, Relationship, SchemaGeneration):
                editor.create_model(model)
        _shape("viewer + admin->member", "")
        SchemaGeneration.objects.using(alias).create(pk=1, revision="")
        with sudo(reason="constant arms per database"):
            remote = [Post.objects.using(alias).create(title=f"remote-{n}") for n in range(2)]
        # On the other database the member holds the constant arm.
        with other.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {_table()} (resource_type, resource_id, relation, subject_type, "
                "subject_id, optional_subject_relation, caveat_name, caveat_context, "
                "caveat_key, written_at_xid) VALUES ('org/role', 'admin', 'member', "
                "'auth/user', 'member', '', '', NULL, '', 0)"
            )
        viewed = list(Post._base_manager.order_by("pk").values_list("pk", flat=True))[:1]
        with evaluator_scope() as evaluator:
            # It holds none on the default one: kept there, for that database.
            assert _read(MEMBER)[0] == viewed
            ids = Post.objects.using(alias).with_actor(MEMBER).order_by("pk")
            assert list(ids.values_list("pk", flat=True)) == [post.pk for post in remote]
            assert evaluator.stats()["constant_witness_entries"] == 2
    finally:
        other.close()
        del connections[alias]


@KEPT
def test_a_recursive_permission_with_a_constant_arm_matches_the_references():
    local = install_schema(
        "\n".join(
            [
                "definition auth/user {}",
                "definition org/role { relation member: auth/user }",
                "definition blog/folder {",
                "    relation parent: blog/folder // rebac:field=parent",
                "    relation viewer: auth/user",
                "    relation admin: org/role // rebac:const=admin",
                "    permission read = viewer + parent->read + admin->member",
                "}",
            ]
        )
    )
    with sudo(reason="constant arms recursion"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
        Folder.objects.create(name="grandchild", parent=child)
        Folder.objects.create(name="apart")
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("org/role", "admin"), "member", ADMIN),
            RelationshipTuple(to_object_ref(root), "viewer", MEMBER),
        ]
    )
    read.reset()
    for actor in (ADMIN, MEMBER, OUTSIDER):
        assert_scope_matches(Folder, actor=actor, action="read")
        with evaluator_scope():
            list(Folder.objects.with_actor(actor).values_list("pk", flat=True))
            assert_scope_matches(Folder, actor=actor, action="read")


@KEPT
def test_a_constant_arm_an_override_adds_ends_at_its_deadline(monkeypatch):
    now = timezone.now()
    deadline = now + timedelta(minutes=1)
    clock = {"now": now}
    monkeypatch.setattr(timezone, "now", lambda: clock["now"])
    SchemaDefinition.objects.create(resource_type="auth/user")
    role = SchemaDefinition.objects.create(resource_type="org/role")
    SchemaRelation.objects.create(
        definition=role, name="member", allowed_subjects=[{"type": "auth/user"}]
    )
    definition = SchemaDefinition.objects.create(resource_type="blog/post")
    SchemaRelation.objects.create(
        definition=definition, name="viewer", allowed_subjects=[{"type": "auth/user"}]
    )
    SchemaRelation.objects.create(
        definition=definition,
        name="admin",
        allowed_subjects=[{"type": "org/role"}],
        backing={"kind": "const", "target_id": "admin"},
    )
    permission = SchemaPermission.objects.create(
        definition=definition, name="read", expression="viewer"
    )
    SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_EXTEND,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=permission.pk,
        expression="admin->member",
        reason="temporary administration",
        expires_at=deadline,
    )
    reset_backend()
    local = backend()
    with sudo(reason="constant arms override"):
        posts = [Post.objects.create(title=f"post-{index}") for index in range(2)]
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("org/role", "admin"), "member", ADMIN),
            RelationshipTuple(to_object_ref(posts[0]), "viewer", ADMIN),
        ]
    )
    with evaluator_scope():
        ids, statements = _read(ADMIN)
        assert ids == [post.pk for post in posts]
        # The constant statement: no arm of the post.
        assert "'blog/post'" not in statements[-1]
        assert _check(local, ADMIN, posts[1])
        clock["now"] = deadline
        assert _read(ADMIN)[0] == [posts[0].pk]
        assert not _check(local, ADMIN, posts[1])
    assert _read(ADMIN)[0] == [posts[0].pk]


@KEPT
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
