"""What reads report after each kind of write, and what a rolled-back write leaves.

The PostgreSQL cases use the configured test database and deterministic event
barriers; they never create a database server or invoke Docker.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import timedelta
from threading import Event
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import close_old_connections, connection, connections, models, router, transaction
from django.test.utils import CaptureQueriesContext, isolate_apps
from django.utils import timezone

from rebac import schema_changes, sudo, to_object_ref
from rebac.backends import backend, reset_backend
from rebac.backends.local import LocalBackend
from rebac.errors import PermissionDenied
from rebac.models import (
    SchemaDefinition,
    SchemaOverride,
    SchemaPermission,
    SchemaRelation,
    active_relationship_model,
)
from rebac.models.generation import SchemaGeneration
from rebac.schema import parse_zed
from rebac.testing import install_schema
from rebac.types import (
    ObjectRef,
    PermissionResult,
    RelationshipFilter,
    RelationshipTuple,
    SubjectRef,
)
from tests.backend_setup import STORAGE_TIERS, sqlite_alias
from tests.reference_harness import assert_reads_match, seed
from tests.testapp.models import (
    BackingProject,
    BackingQueue,
    BackingRound,
    BackingStage,
    BackingTask,
    Folder,
    Post,
)

SCHEMA = """
caveat eligible(enabled bool) { enabled }
definition auth/user {}
definition auth/group {
    relation member: auth/user | auth/user with eligible | auth/group#member
}
definition test/role {
    relation member: auth/user | test/role#member
    relation includes: test/role
    permission effective_member = member + includes->effective_member
}
definition blog/folder {
    relation viewer: auth/user | auth/group#member
    relation parent: blog/folder // rebac:field=parent
    permission read = viewer + parent->read
    permission create = authenticated
    permission write = authenticated
    permission delete = authenticated
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation collections: blog/folder // rebac:field=collections
    permission read = folder->read + collections->read
    permission create = authenticated
    permission write = authenticated
    permission delete = authenticated
}
definition test/backingqueue {
    relation viewer: auth/user
    permission read = viewer
}
definition test/backingtask {
    relation queue: test/backingqueue // rebac:field={"path":"queue","filters":{"stage__hidden":false}}
    permission read = queue->read
}
definition test/backinground {
    relation queue: test/backingqueue // rebac:field=project__task__queue
    permission read = queue->read
}
definition test/document {
    relation blocked: auth/group#member
    relation viewer: auth/user
    permission read = authenticated - blocked
    permission direct = viewer
}
definition test/bucket {
    relation member: auth/user // rebac:attribute={"field":"last_name"}
    relation staff: auth/user // rebac:attribute={"field":"is_staff","resource":"staff","value":true}
    permission read = member
    permission admin = staff
}
"""

ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")


@pytest.fixture(params=["denormalized", "registry"])
def active(db, settings, request):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    local = install_schema(SCHEMA)
    with sudo(reason="write effect tests"):
        yield local


def allowed(subject, row, action="read", context=None):
    resource = row if isinstance(row, ObjectRef) else to_object_ref(row)
    return (
        backend()
        .check_access(subject=subject, action=action, resource=resource, context=context)
        .allowed
    )


def check_rows(
    *rows, actions=("read",), subjects=(ALICE, BOB), using="default", contexts=(None,), now=None
):
    """Point checks agree with the reference; a model row's queryset scope agrees with them."""
    resources = [row if isinstance(row, ObjectRef) else to_object_ref(row) for row in rows]
    assert_reads_match(
        subjects=subjects,
        resources=resources,
        actions=actions,
        contexts=contexts,
        using=using,
        now=now,
    )
    for row in rows:
        if isinstance(row, ObjectRef):
            continue
        for subject in subjects:
            for action in actions:
                scoped = type(row).objects.with_actor(subject).with_action(action)
                listed = scoped.filter(pk=row.pk).exists()
                assert listed is allowed(subject, row, action), (row, subject, action)


def grant_folder(folder, actor=ALICE):
    backend().write_relationships([RelationshipTuple(to_object_ref(folder), "viewer", actor)])


def test_explicit_backend_reads_its_own_tuple_writes(active):
    explicit = LocalBackend()
    explicit.set_schema(
        parse_zed("""
        definition auth/user {}
        definition isolated/doc {
            relation viewer: auth/user
            permission read = viewer
        }
    """)
    )
    resource = ObjectRef("isolated/doc", "one")
    tuple_ = RelationshipTuple(resource, "viewer", ALICE)
    explicit.write_relationships([tuple_])
    assert explicit.check_access(subject=ALICE, action="read", resource=resource).allowed
    explicit.delete_relationship(tuple_)
    assert not explicit.check_access(subject=ALICE, action="read", resource=resource).allowed
    explicit.write_relationships([tuple_])
    assert explicit.check_access(subject=ALICE, action="read", resource=resource).allowed
    explicit.delete_relationships(
        RelationshipFilter(resource_type="isolated/doc", resource_id="one")
    )
    assert not explicit.check_access(subject=ALICE, action="read", resource=resource).allowed


def test_tuple_to_a_constant_userset_grants_its_member(active):
    active.set_schema(
        parse_zed("""
        definition auth/user {}
        definition blog/post {
            relation member: auth/user // rebac:const=alice
        }
        definition test/document {
            relation viewer: blog/post#member
            permission read = viewer
        }
    """)
    )
    resource = ObjectRef("test/document", "new")
    assert not allowed(ALICE, resource)
    seed(["test/document:new#viewer@blog/post:new#member"])
    check_rows(resource)
    assert allowed(ALICE, resource)
    assert not allowed(BOB, resource)


@pytest.mark.parametrize("active", STORAGE_TIERS, indirect=True)
def test_mixin_reparent_moves_inherited_read(active):
    left, right = Folder.objects.create(name="left"), Folder.objects.create(name="right")
    child = Folder.objects.create(name="child", parent=left)
    post = Post.objects.create(title="post", folder=child)
    grant_folder(left)
    grant_folder(right, BOB)
    check_rows(left, right, child, post)
    assert allowed(ALICE, post)
    assert not allowed(BOB, post)
    child.parent = right
    child.save(update_fields=["parent"])
    check_rows(left, right, child, post)
    assert not allowed(ALICE, post)
    assert allowed(BOB, post)


@pytest.mark.pg_delta
@pytest.mark.parametrize("owner", ["instance", "queryset"])
@pytest.mark.parametrize("active", STORAGE_TIERS, indirect=True)
def test_subtree_delete_and_collector_set_null(active, owner):
    root = Folder.objects.create(name="root")
    child = Folder.objects.create(name="child", parent=root)
    post = Post.objects.create(title="survives", folder=child)
    grant_folder(root)
    check_rows(root, child, post)
    assert allowed(ALICE, post)
    old = (to_object_ref(root), to_object_ref(child))
    if owner == "instance":
        root.delete()
    else:
        Folder.objects.filter(pk=root.pk).delete()
    post.refresh_from_db()
    assert post.folder_id is None
    check_rows(*old, post)
    assert not allowed(ALICE, post)
    assert not any(allowed(ALICE, resource) for resource in old)


@pytest.mark.parametrize("active", STORAGE_TIERS, indirect=True)
def test_queryset_update_of_its_own_filter_column_moves_read(active):
    left, right = Folder.objects.create(name="left"), Folder.objects.create(name="right")
    posts = Post.objects.bulk_create([Post(title=str(i), folder=left) for i in range(4)])
    grant_folder(left)
    grant_folder(right, BOB)
    assert Post.objects.filter(folder=left).update(folder=right) == 4
    check_rows(*posts)
    assert all(allowed(BOB, post) for post in posts)
    assert not any(allowed(ALICE, post) for post in posts)


@pytest.mark.parametrize("active", STORAGE_TIERS, indirect=True)
def test_bulk_update_in_many_batches_moves_read(active):
    left, right = Folder.objects.create(name="left"), Folder.objects.create(name="right")
    posts = Post.objects.bulk_create([Post(title=str(i), folder=left) for i in range(3)])
    grant_folder(left)
    grant_folder(right, BOB)
    for post in posts:
        post.folder = right
    assert Post.objects.bulk_update(posts, ["folder"], batch_size=1) == 3
    check_rows(*posts)
    assert all(allowed(BOB, post) for post in posts)
    assert not any(allowed(ALICE, post) for post in posts)


@pytest.mark.parametrize("owner", ["queryset", "instance"])
def test_unwatched_update_leaves_read_as_it_was(active, owner):
    folder = Folder.objects.create(name="folder")
    post = Post.objects.create(title="old", folder=folder)
    grant_folder(folder)
    if owner == "queryset":
        assert Post.objects.filter(pk=post.pk).update(title="new") == 1
    else:
        post.title = "new"
        post.save(update_fields=["title"])
    post.refresh_from_db()
    assert post.title == "new"
    check_rows(post)
    assert allowed(ALICE, post)
    assert not allowed(BOB, post)


@pytest.mark.pg_delta
@pytest.mark.parametrize(
    ("operation", "alice", "bob"),
    [
        ("add", True, True),
        ("remove", False, False),
        ("clear", False, False),
        ("set", False, True),
        ("reverse_clear", False, False),
    ],
)
@pytest.mark.parametrize("active", STORAGE_TIERS, indirect=True)
def test_m2m_all_owner_actions(active, operation, alice, bob):
    first, second = Folder.objects.create(name="first"), Folder.objects.create(name="second")
    post = Post.objects.create(title="post")
    grant_folder(first)
    grant_folder(second, BOB)
    post.collections.add(first)
    check_rows(post)
    assert allowed(ALICE, post)
    assert not allowed(BOB, post)
    if operation == "add":
        post.collections.add(second)
    elif operation == "remove":
        post.collections.remove(first)
    elif operation == "clear":
        post.collections.clear()
    elif operation == "reverse_clear":
        first.collected_posts.clear()
    else:
        post.collections.set([second])
    check_rows(post)
    assert allowed(ALICE, post) is alice
    assert allowed(BOB, post) is bob


def test_plain_reparent_and_filter_transition(active):
    first, second = BackingQueue.objects.create(), BackingQueue.objects.create()
    stage = BackingStage.objects.create(hidden=False)
    a = BackingTask.objects.create(queue=first, stage=stage)
    b = BackingTask.objects.create(queue=second, stage=stage)
    project = BackingProject.objects.create(task=a)
    round_ = BackingRound.objects.create(project=project)
    active.write_relationships(
        [
            RelationshipTuple(to_object_ref(first), "viewer", ALICE),
            RelationshipTuple(to_object_ref(second), "viewer", BOB),
        ]
    )
    check_rows(a, b, round_)
    assert allowed(ALICE, a) and allowed(BOB, b)
    assert allowed(ALICE, round_) and not allowed(BOB, round_)
    with transaction.atomic():
        project.task = b
        project.save(update_fields=["task"])
    check_rows(a, b, round_)
    assert not allowed(ALICE, round_) and allowed(BOB, round_)
    with transaction.atomic():
        stage.hidden = True
        stage.save(update_fields=["hidden"])
    check_rows(a, b, round_)
    assert not allowed(ALICE, a) and not allowed(BOB, b)
    assert allowed(BOB, round_)
    stage.delete()  # Collector SET_NULL writes BackingTask.stage without save signals.
    check_rows(a, b, round_)
    assert not allowed(ALICE, a) and not allowed(BOB, b)
    project.delete()  # Collector SET_NULL writes BackingRound.project.
    check_rows(round_)
    assert not allowed(ALICE, round_) and not allowed(BOB, round_)


def test_plain_collector_cascade(active):
    queue = BackingQueue.objects.create()
    task = BackingTask.objects.create(queue=queue)
    project = BackingProject.objects.create(task=task)
    round_ = BackingRound.objects.create(project=project)
    active.write_relationships([RelationshipTuple(to_object_ref(queue), "viewer", ALICE)])
    assert allowed(ALICE, round_)
    task.delete()  # CASCADE through plain BackingProject; SET_NULL on round.
    round_.refresh_from_db()
    assert round_.project_id is None
    check_rows(round_)
    assert not allowed(ALICE, round_)


def test_plain_attribute_old_containers_and_fixed_boolean_anchor(active, django_user_model):
    from rebac import to_subject_ref

    actor = django_user_model.objects.create(
        username="container-member", last_name="old", is_staff=True
    )
    subject = to_subject_ref(actor)
    old, new, staff = (ObjectRef("test/bucket", name) for name in ("old", "new", "staff"))
    check_rows(old, new, staff, subjects=(subject,), actions=("read", "admin"))
    assert allowed(subject, old) and not allowed(subject, new)
    assert allowed(subject, staff, "admin")
    actor.last_name = "new"
    actor.is_staff = False
    actor.save(update_fields=["last_name", "is_staff"])
    check_rows(old, new, staff, subjects=(subject,), actions=("read", "admin"))
    assert not allowed(subject, old) and allowed(subject, new)
    assert not allowed(subject, staff, "admin")
    actor.delete()
    check_rows(old, new, staff, subjects=(subject,), actions=("read", "admin"))
    assert not allowed(subject, old) and not allowed(subject, new)


@pytest.mark.parametrize("surface", ["backend", "public", "membership", "role"])
def test_tuple_write_owners(active, surface):
    from rebac import memberships, relationships, roles

    resource = ObjectRef("test/role", "reviewer")
    tuple_ = RelationshipTuple(resource, "member", ALICE)
    if surface == "backend":
        active.write_relationships([tuple_])
    elif surface == "public":
        relationships.write_relationships([tuple_])
    elif surface == "membership":
        memberships.grant(subject=ALICE, container=resource)
    else:
        roles.grant(actor=ALICE, role=resource)
    check_rows(resource, actions=("member", "effective_member"))
    assert allowed(ALICE, resource, "member") and allowed(ALICE, resource, "effective_member")
    assert not allowed(BOB, resource, "effective_member")
    if surface == "backend":
        active.delete_relationships(RelationshipFilter(resource_type=resource.resource_type))
    elif surface == "public":
        relationships.delete_relationship(tuple_)
    elif surface == "membership":
        assert memberships.revoke(subject=ALICE, container=resource) == 1
    else:
        assert roles.revoke(actor=ALICE, role=resource) == 1
    check_rows(resource, actions=("member", "effective_member"))
    assert not allowed(ALICE, resource, "member")
    assert not allowed(ALICE, resource, "effective_member")


def test_role_hierarchy_owners(active):
    from rebac import roles

    parent = ObjectRef("test/role", "parent")
    roles.grant(actor=ALICE, role="test/role:child")
    assert not allowed(ALICE, parent, "effective_member")
    roles.imply(parent="test/role:parent", child="test/role:child")
    check_rows(parent, actions=("effective_member",))
    assert allowed(ALICE, parent, "effective_member")
    assert roles.unimply(parent="test/role:parent", child="test/role:child") == 1
    check_rows(parent, actions=("effective_member",))
    assert not allowed(ALICE, parent, "effective_member")


def test_conditional_deny_membership_revoke(active):
    resource = ObjectRef("test/document", "one")
    member = RelationshipTuple(
        ObjectRef("auth/group", "reviewers"), "member", ALICE, "eligible", {}
    )
    active.write_relationships(
        [
            member,
            RelationshipTuple(
                resource, "blocked", SubjectRef.of("auth/group", "reviewers", "member")
            ),
        ]
    )
    check_rows(resource, contexts=(None, {"enabled": True}, {"enabled": False}))
    assert not allowed(ALICE, resource, context={"enabled": True})
    assert allowed(ALICE, resource, context={"enabled": False})
    assert active.check_access(subject=ALICE, action="read", resource=resource).conditional_on
    active.delete_relationship(member)
    check_rows(resource, contexts=(None, {"enabled": True}, {"enabled": False}))
    assert allowed(ALICE, resource)
    assert allowed(ALICE, resource, context={"enabled": True})


def test_concrete_ban_denies_its_document_and_keeps_the_others(active):
    blocked = ObjectRef("test/document", "blocked")
    unaffected = ObjectRef("test/document", "unblocked")
    seed(["auth/group:g#member@auth/user:alice"])
    ban = RelationshipTuple(blocked, "blocked", SubjectRef.of("auth/group", "g", "member"))
    active.write_relationships([ban])
    check_rows(blocked, unaffected)
    assert not allowed(ALICE, blocked)
    assert allowed(ALICE, unaffected)
    assert allowed(BOB, blocked)
    active.delete_relationship(ban)
    check_rows(blocked, unaffected)
    assert allowed(ALICE, blocked)


@pytest.mark.parametrize("write", ["plain_update", "raw_save"])
@pytest.mark.parametrize("active", STORAGE_TIERS, indirect=True)
def test_writes_outside_the_owners_are_read_as_stored(active, write):
    folder = Folder.objects.create(name="folder")
    post = Post.objects.create(title="post", folder=folder)
    grant_folder(folder)
    if write == "raw_save":
        assert allowed(ALICE, post)
        post.folder = None
        post.save_base(raw=True)
    else:
        queue = BackingQueue.objects.create()
        stage = BackingStage.objects.create(hidden=False)
        task = BackingTask.objects.create(queue=queue, stage=stage)
        active.write_relationships([RelationshipTuple(to_object_ref(queue), "viewer", ALICE)])
        assert allowed(ALICE, task)
        BackingStage.objects.filter(pk=stage.pk).update(hidden=True)
        post = task
    check_rows(post)
    assert not allowed(ALICE, post)


@pytest.mark.pg_delta
@pytest.mark.parametrize("active", STORAGE_TIERS, indirect=True)
def test_tuple_queryset_delete_revokes_read(active):
    folder = Folder.objects.create(name="folder")
    post = Post.objects.create(title="post", folder=folder)
    grant_folder(folder)
    assert allowed(ALICE, post)

    active_relationship_model().objects.filter(
        resource_type="blog/folder", resource_id=str(folder.pk)
    ).delete()

    assert not allowed(ALICE, post)
    check_rows(folder, post)


@pytest.mark.parametrize("rollback", ["outer", "savepoint"])
@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("active", STORAGE_TIERS, indirect=True)
def test_source_write_and_its_read_roll_back_together(active, rollback):
    first, second = Folder.objects.create(name="a"), Folder.objects.create(name="b")
    post = Post.objects.create(title="post", folder=first)
    grant_folder(first)
    grant_folder(second, BOB)

    def change_and_abort():
        post.folder = second
        post.save(update_fields=["folder"])
        assert not allowed(ALICE, post)
        assert allowed(BOB, post)
        raise RuntimeError("abort " + rollback)

    if rollback == "outer":
        with pytest.raises(RuntimeError, match="abort"):
            with transaction.atomic():
                change_and_abort()
    else:
        with transaction.atomic():
            with pytest.raises(RuntimeError, match="abort"):
                with transaction.atomic():
                    change_and_abort()
    post.refresh_from_db()
    assert post.folder_id == first.pk
    check_rows(post)
    assert allowed(ALICE, post)
    assert not allowed(BOB, post)


@pytest.mark.pg_delta
@pytest.mark.parametrize("active", STORAGE_TIERS, indirect=True)
def test_conflict_bulk_create_preserves_refusal_and_sudo_writes_take_effect(active):
    left, right = Folder.objects.create(name="left"), Folder.objects.create(name="right")
    post = Post.objects.create(title="post", folder=left)
    grant_folder(left)
    grant_folder(right, BOB)
    with pytest.raises(PermissionDenied, match="conflicting"):
        Post.objects.with_actor(ALICE).bulk_create(
            [Post(pk=post.pk, title="new", folder=right)],
            update_conflicts=True,
            update_fields=["folder"],
            unique_fields=["pk"],
        )
    check_rows(post)
    assert allowed(ALICE, post) and not allowed(BOB, post)
    Post.objects.bulk_create(
        [Post(pk=post.pk, title="ignored", folder=right)], ignore_conflicts=True
    )
    check_rows(post)
    assert allowed(ALICE, post) and not allowed(BOB, post)
    Post.objects.bulk_create(
        [Post(pk=post.pk, title="updated", folder=right)],
        update_conflicts=True,
        update_fields=["folder"],
        unique_fields=["pk"],
    )
    check_rows(post)
    assert not allowed(ALICE, post) and allowed(BOB, post)


@pytest.mark.parametrize("active", STORAGE_TIERS, indirect=True)
def test_tuple_writes_and_reads_use_the_routed_alias(active, tmp_path, django_db_blocker):
    alias = "routed_write_target"
    target = sqlite_alias(alias, tmp_path / "routed.sqlite3")
    connections[alias] = target

    class AliasRouter:
        def db_for_read(self, model, **hints):
            return alias

        def db_for_write(self, model, **hints):
            return alias

    try:
        with django_db_blocker.unblock():
            call_command("migrate", database=alias, verbosity=0)
            resource = ObjectRef("test/document", "alias")
            routed = patch.object(router, "routers", [AliasRouter()])
            with routed:
                active.write_relationships([RelationshipTuple(resource, "viewer", ALICE)])
                check_rows(resource, actions=("direct",), using=alias)
                assert allowed(ALICE, resource, "direct")
            assert not active_relationship_model().objects.using("default").exists()
            assert not allowed(ALICE, resource, "direct")
            with routed:
                active.delete_relationships(RelationshipFilter(resource_type="test/document"))
                check_rows(resource, actions=("direct",), using=alias)
                assert not allowed(ALICE, resource, "direct")
    finally:
        target.close()
        del connections[alias]


@pytest.mark.parametrize("active", STORAGE_TIERS, indirect=True)
def test_queryset_update_leaves_unrelated_rows_as_they_were(active):
    left, right = Folder.objects.create(name="left"), Folder.objects.create(name="right")
    left_post = Post.objects.create(title="left", folder=left)
    right_post = Post.objects.create(title="right", folder=right)
    grant_folder(left)
    grant_folder(right, BOB)
    assert allowed(ALICE, left_post) and allowed(BOB, right_post)
    assert Post.objects.filter(pk=left_post.pk).update(folder=None) == 1
    check_rows(left_post, right_post)
    assert not allowed(ALICE, left_post)
    assert allowed(BOB, right_post)


@pytest.fixture
def persisted(db):
    reset_backend()
    with schema_changes():
        SchemaDefinition.objects.create(resource_type="auth/user")
        definition = SchemaDefinition.objects.create(resource_type="test/policy")
        SchemaRelation.objects.create(
            definition=definition, name="viewer", allowed_subjects=[{"type": "auth/user"}]
        )
        permission = SchemaPermission.objects.create(
            definition=definition, name="read", expression="viewer"
        )
        SchemaPermission.objects.create(definition=definition, name="dependent", expression="read")
    backend().write_relationships(
        [RelationshipTuple(ObjectRef("test/policy", "one"), "viewer", ALICE)]
    )
    return permission


def test_override_create_delete_and_deadline(persisted):
    resource = ObjectRef("test/policy", "one")
    assert allowed(ALICE, resource) and allowed(ALICE, resource, "dependent")
    deadline = timezone.now() + timedelta(hours=1)
    override = SchemaOverride.objects.create(
        kind="disable",
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=persisted.pk,
        expression="viewer",
        reason="write effect test",
        expires_at=deadline,
    )
    check_rows(resource, actions=("read", "dependent"))
    assert not allowed(ALICE, resource) and not allowed(ALICE, resource, "dependent")
    later = deadline + timedelta(seconds=1)
    check_rows(resource, actions=("read", "dependent"), now=later)
    with patch("django.utils.timezone.now", return_value=later):
        assert allowed(ALICE, resource) and allowed(ALICE, resource, "dependent")
    assert not allowed(ALICE, resource) and not allowed(ALICE, resource, "dependent")
    override.delete()
    check_rows(resource, actions=("read", "dependent"))
    assert allowed(ALICE, resource) and allowed(ALICE, resource, "dependent")


def test_schema_update_changes_dependent_permissions(persisted):
    resource = ObjectRef("test/policy", "one")
    assert allowed(ALICE, resource) and allowed(ALICE, resource, "dependent")
    SchemaPermission.objects.filter(pk=persisted.pk).update(expression="nil")
    check_rows(resource, actions=("read", "dependent"))
    assert not allowed(ALICE, resource) and not allowed(ALICE, resource, "dependent")


def test_schema_change_and_tuple_delete_in_one_transaction_both_take_effect(persisted):
    with schema_changes():
        other = SchemaDefinition.objects.create(resource_type="test/other")
        SchemaRelation.objects.create(
            definition=other, name="viewer", allowed_subjects=[{"type": "auth/user"}]
        )
        SchemaPermission.objects.create(definition=other, name="read", expression="viewer")
    resource = ObjectRef("test/other", "revoked")
    grant = RelationshipTuple(resource, "viewer", ALICE)
    backend().write_relationships([grant])
    assert allowed(ALICE, resource)
    with transaction.atomic():
        SchemaPermission.objects.filter(pk=persisted.pk).update(expression="nil")
        backend().delete_relationship(grant)
    assert not allowed(ALICE, resource)
    assert not allowed(ALICE, ObjectRef("test/policy", "one"))
    check_rows(resource, ObjectRef("test/policy", "one"))


def test_schema_change_and_model_revoke_in_one_transaction_both_take_effect(persisted):
    with schema_changes():
        folder_type = SchemaDefinition.objects.create(resource_type="blog/folder")
        SchemaRelation.objects.create(
            definition=folder_type, name="viewer", allowed_subjects=[{"type": "auth/user"}]
        )
        SchemaPermission.objects.create(definition=folder_type, name="read", expression="viewer")
        post_type = SchemaDefinition.objects.create(resource_type="blog/post")
        SchemaRelation.objects.create(
            definition=post_type,
            name="folder",
            allowed_subjects=[{"type": "blog/folder"}],
            backing={"kind": "fk", "path": "folder"},
        )
        SchemaPermission.objects.create(
            definition=post_type, name="read", expression="folder->read"
        )
    with sudo(reason="schema and model write in one transaction"):
        folder = Folder.objects.create(name="mixed")
        post = Post.objects.create(title="mixed", folder=folder)
        grant_folder(folder)
        assert allowed(ALICE, post)
        with transaction.atomic():
            SchemaPermission.objects.filter(pk=persisted.pk).update(expression="nil")
            post.folder = None
            post.save(update_fields=["folder"])
        assert not allowed(ALICE, post)
        assert allowed(ALICE, folder)
        check_rows(folder, post)


@pytest.mark.parametrize("owner", ["save", "update", "bulk_create"])
def test_watched_mixin_proxy_write(active, owner):
    with isolate_apps("tests.testapp"):

        class PostProxy(Post):
            class Meta:
                app_label = "testapp"
                proxy = True
                rebac_resource_type = "blog/post"

        folder = Folder.objects.create(name="proxy source")
        grant_folder(folder)
        if owner == "bulk_create":
            post = PostProxy.objects.bulk_create([PostProxy(title="proxy", folder=folder)])[0]
            assert allowed(ALICE, post)
        else:
            post = PostProxy.objects.create(title="proxy", folder=folder)
            assert allowed(ALICE, post)
            if owner == "save":
                post.folder = None
                post.save(update_fields=["folder"])
            else:
                PostProxy.objects.filter(pk=post.pk).update(folder=None)
            assert not allowed(ALICE, post)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("proxy", [True, False])
def test_plain_watched_proxy_and_mti_inherited_field_save(active, proxy):
    with isolate_apps("tests.testapp"):
        attrs = {
            "__module__": __name__,
            "Meta": type("Meta", (), {"app_label": "testapp", "proxy": proxy}),
        }
        if not proxy:
            # Exercise a child PK that differs from the watched parent's PK.
            attrs["child_key"] = models.CharField(max_length=20, primary_key=True)
        child = type("WatchedStageChild", (BackingStage,), attrs)
        if not proxy:
            with connection.schema_editor() as editor:
                editor.create_model(child)
        try:
            with transaction.atomic():
                stage = child.objects.create(
                    hidden=False, **({} if proxy else {"child_key": "child"})
                )
            queue = BackingQueue.objects.create()
            parent_pk = stage.pk if proxy else stage.backingstage_ptr_id
            task = BackingTask.objects.create(queue=queue, stage_id=parent_pk)
            active.write_relationships([RelationshipTuple(to_object_ref(queue), "viewer", ALICE)])
            assert allowed(ALICE, task)
            with transaction.atomic():
                stage.hidden = True
                stage.save(update_fields=["hidden"])
            assert not allowed(ALICE, task)
        finally:
            if not proxy:
                with connection.schema_editor() as editor:
                    editor.delete_model(child)


def test_setop_write_leaves_other_documents_as_they_were(active):
    active.set_schema(
        parse_zed("""
        definition auth/user {}
        definition auth/group { relation member: auth/user }
        definition test/document {
            relation viewer: auth/user
            relation blocked: auth/group#member
            permission read = viewer - blocked
            permission edit = viewer & blocked
        }
    """)
    )
    seed(
        [
            "auth/group:g#member@auth/user:alice",
            "auth/group:other#member@auth/user:alice",
            "test/document:one#blocked@auth/group:g#member",
            "test/document:one#viewer@auth/user:alice",
            "test/document:two#blocked@auth/group:other#member",
            "test/document:two#viewer@auth/user:alice",
            "test/document:two#viewer@auth/user:bob",
        ]
    )
    one, two = ObjectRef("test/document", "one"), ObjectRef("test/document", "two")
    check_rows(one, two, actions=("read", "edit"))
    assert not allowed(ALICE, one) and allowed(ALICE, one, "edit")
    active.delete_relationships(
        RelationshipFilter(resource_type="test/document", resource_id="one", relation="blocked")
    )
    check_rows(one, two, actions=("read", "edit"))
    assert allowed(ALICE, one) and not allowed(ALICE, one, "edit")
    assert not allowed(ALICE, two) and allowed(ALICE, two, "edit")
    assert allowed(BOB, two) and not allowed(BOB, two, "edit")


def test_universal_subtraction_replacement_preserves_other_bans(active):
    seed(
        [
            "auth/group:first#member@auth/user:alice",
            "auth/group:second#member@auth/user:bob",
            "test/document:one#blocked@auth/group:first#member",
            "test/document:two#blocked@auth/group:second#member",
        ]
    )
    active.delete_relationships(
        RelationshipFilter(resource_type="test/document", resource_id="one")
    )
    assert allowed(ALICE, ObjectRef("test/document", "one"))
    assert not allowed(BOB, ObjectRef("test/document", "two"))


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("atomic", [False, True])
def test_failed_save_leaves_read_as_it_was(active, atomic):
    with transaction.atomic():
        stage = BackingStage.objects.create(hidden=False)
    queue = BackingQueue.objects.create()
    task = BackingTask.objects.create(queue=queue, stage=stage)
    active.write_relationships([RelationshipTuple(to_object_ref(queue), "viewer", ALICE)])
    stage.hidden = True
    with transaction.atomic() if atomic else nullcontext():
        with patch.object(
            BackingStage, "_save_table", side_effect=RuntimeError("failed source save")
        ):
            with pytest.raises(RuntimeError, match="failed source save"):
                stage.save(update_fields=["hidden"])
    check_rows(task)
    assert allowed(ALICE, task)


@pytest.mark.postgresql
def test_caught_nested_schema_failure_leaves_the_policy_as_it_was(persisted):
    one, two = ObjectRef("test/policy", "one"), ObjectRef("test/policy", "two")
    with transaction.atomic():
        with pytest.raises(RuntimeError, match="rollback nested schema"):
            with transaction.atomic():
                SchemaPermission.objects.filter(pk=persisted.pk).update(expression="nil")
                assert not allowed(ALICE, one)
                raise RuntimeError("rollback nested schema")
        backend().write_relationships([RelationshipTuple(two, "viewer", BOB)])
    persisted.refresh_from_db()
    assert persisted.expression == "viewer"
    check_rows(one, two, actions=("read", "dependent"))
    assert allowed(ALICE, one) and allowed(ALICE, one, "dependent")
    assert allowed(BOB, two) and allowed(BOB, two, "dependent")


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
def test_concurrent_revoke_and_link_insertion_leave_no_read():
    if connection.vendor != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    active = install_schema(SCHEMA)
    with sudo(reason="concurrency fixture"):
        folder = Folder.objects.create(name="source")
        post = Post.objects.create(title="dependent")
    grant_folder(folder)
    revoked, attempting, release = Event(), Event(), Event()

    def revoke():
        close_old_connections()
        try:
            with transaction.atomic():
                active.delete_relationships(
                    RelationshipFilter(resource_type="blog/folder", resource_id=str(folder.pk))
                )
                revoked.set()
                assert release.wait(10)
        finally:
            connections["default"].close()

    def link():
        close_old_connections()
        try:
            assert revoked.wait(10)
            attempting.set()
            with sudo(reason="concurrent link"):
                Post.objects.filter(pk=post.pk).update(folder=folder)
        finally:
            connections["default"].close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        revoke_future, link_future = pool.submit(revoke), pool.submit(link)
        try:
            assert attempting.wait(10)
        finally:
            release.set()
        revoke_future.result(timeout=10)
        link_future.result(timeout=10)
    post.refresh_from_db()
    assert post.folder_id == folder.pk
    check_rows(post)
    assert not allowed(ALICE, post)


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
def test_concurrent_tuple_writes_both_take_effect(active):
    if connection.vendor != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    written, attempting, release = Event(), Event(), Event()

    def first():
        close_old_connections()
        try:
            with transaction.atomic():
                active.write_relationships(
                    [RelationshipTuple(ObjectRef("test/document", "first"), "viewer", ALICE)]
                )
                written.set()
                assert release.wait(10)
        finally:
            connections["default"].close()

    def second():
        close_old_connections()
        try:
            assert written.wait(10)
            attempting.set()
            active.write_relationships(
                [RelationshipTuple(ObjectRef("test/document", "second"), "viewer", BOB)]
            )
        finally:
            connections["default"].close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        try:
            assert attempting.wait(10)
        finally:
            release.set()
        a.result(timeout=10)
        b.result(timeout=10)
    assert allowed(ALICE, ObjectRef("test/document", "first"), "direct")
    assert allowed(BOB, ObjectRef("test/document", "second"), "direct")
    assert not allowed(BOB, ObjectRef("test/document", "first"), "direct")
    assert not allowed(ALICE, ObjectRef("test/document", "second"), "direct")


# Cases from the semantic review of membership and type-level grants.


def test_membership_write_reaches_arrow_and_userset_readers(active):
    active.set_schema(
        parse_zed("""
            definition auth/user {}
            definition test/group { relation member: auth/user | test/group#member }
            definition test/doc {
                relation parent: test/group
                relation viewer: test/group#member
                permission read = parent->member
                permission view = viewer
            }
        """)
    )
    group = ObjectRef("test/group", "g")
    child, holder = ObjectRef("test/doc", "child"), ObjectRef("test/doc", "holder")
    seed(["test/doc:child#parent@test/group:g", "test/doc:holder#viewer@test/group:g#member"])
    member = RelationshipTuple(group, "member", ALICE)

    def members():
        return list(
            active.lookup_subjects(resource=group, action="member", subject_type="auth/user")
        )

    for write, expected in ((active.write_relationships, True), (None, False)):
        if write is None:
            active.delete_relationship(member)
        else:
            write([member])
        assert members() == ([ALICE] if expected else [])
        check_rows(child, holder, actions=("read", "view"))
        check_rows(group, actions=("member",))
        for resource, action in ((child, "read"), (holder, "view")):
            assert allowed(ALICE, resource, action) is expected, action


@pytest.mark.parametrize("first", ["membership", "viewer"])
def test_revoking_a_type_level_grant_keeps_concrete_grants(active, first):
    active.set_schema(
        parse_zed("""
            definition auth/user {}
            definition test/org { relation member: auth/user }
            definition blog/post {
                relation org: test/org // rebac:const=default
                relation viewer: auth/user
                permission view = org->member + viewer
            }
        """)
    )
    post = ObjectRef("blog/post", "one")
    membership = RelationshipTuple(ObjectRef("test/org", "default"), "member", ALICE)
    viewer = RelationshipTuple(post, "viewer", ALICE)
    for write in (membership, viewer) if first == "membership" else (viewer, membership):
        active.write_relationships([write])
    assert allowed(ALICE, ObjectRef("blog/post", "two"), "view")
    active.delete_relationship(membership)
    check_rows(post, ObjectRef("blog/post", "two"), actions=("view",))
    assert allowed(ALICE, post, "view")
    assert not allowed(ALICE, ObjectRef("blog/post", "two"), "view")
    active.delete_relationship(viewer)
    check_rows(post, actions=("view",))
    assert not allowed(ALICE, post, "view")


# Before a policy is installed there are no writes to gate.


def _writes_of_every_owner(django_user_model):
    """An owned save, update, queryset update and delete, and a tracked save."""
    with sudo(reason="writes before the first sync"):
        folder = Folder.objects.create(name="first")
        folder.name = "renamed"
        folder.save()
        Folder.objects.filter(pk=folder.pk).update(name="again")
        post = Post.objects.create(title="kept", folder=folder)
        Post.objects.create(title="dropped", folder=folder).delete()
        Post.objects.filter(title="missing").delete()
    with transaction.atomic():
        user = django_user_model.objects.create(username="early")
        user.is_staff = True
        user.save()
    return folder, post, user


def test_writes_before_the_first_sync_proceed_and_reads_stay_closed(db, django_user_model):
    reset_backend()
    SchemaGeneration.objects.all().delete()
    folder, post, user = _writes_of_every_owner(django_user_model)
    assert user.is_staff
    assert Folder.objects.sudo(reason="test").get(pk=folder.pk).name == "again"
    assert Post.objects.sudo(reason="test").filter(folder=folder).count() == 1
    assert not SchemaGeneration.objects.exists()
    assert not Post.objects.with_actor(ALICE).exists()
    assert not Folder.objects.with_actor(ALICE).exists()
    check = backend().check_access(subject=ALICE, action="read", resource=to_object_ref(post))
    assert check.result is PermissionResult.NO_PERMISSION
    assert not backend().accessible(subject=ALICE, action="read", resource_type="blog/post")


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
def test_writes_proceed_while_the_library_tables_are_not_migrated(django_user_model):
    reset_backend()
    with connection.schema_editor() as editor:
        editor.delete_model(SchemaGeneration)
    try:
        folder, post, user = _writes_of_every_owner(django_user_model)
        assert Folder.objects.sudo(reason="test").filter(pk=folder.pk, name="again").exists()
        assert Post.objects.sudo(reason="test").filter(pk=post.pk, folder=folder).exists()
        assert django_user_model.objects.filter(pk=user.pk, is_staff=True).exists()
    finally:
        with connection.schema_editor() as editor:
            editor.create_model(SchemaGeneration)
    assert not SchemaGeneration.objects.exists()


@pytest.mark.parametrize("many", [5, pytest.param(30, marks=pytest.mark.slow)])
@pytest.mark.parametrize("active", STORAGE_TIERS, indirect=True)
def test_a_write_costs_the_same_whatever_shares_its_target(active, many):
    """The statements of a write do not grow with the rows that share its target."""
    cost = {}
    for siblings in (2, many):
        folder = Folder.objects.create(name=f"shared-{siblings}")
        grant_folder(folder)
        for number in range(siblings):
            Post.objects.create(title=f"sibling-{number}", folder=folder)
        with CaptureQueriesContext(connection) as queries:
            post = Post.objects.create(title="one more", folder=folder)
        cost[siblings] = len(queries)
        assert allowed(ALICE, post)
    assert cost[2] == cost[many]
    check_rows(post)
