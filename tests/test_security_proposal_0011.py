"""Executable security gaps deferred to proposal 0011."""

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction

from rebac import (
    MissingActorError,
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    actor_context,
    backend,
    sudo,
    to_subject_ref,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.testapp.models import (
    BackingEntry,
    BackingProject,
    BackingQueue,
    BackingRound,
    BackingTask,
    Folder,
    Post,
)

A = SubjectRef.of("auth/user", "alice")
B = SubjectRef.of("auth/user", "bob")


@pytest.fixture(autouse=True)
def isolated_backend(db):
    reset_backend()
    yield
    reset_backend()


def grant(type_, id_, relation, subject):
    backend().write_relationships(
        [RelationshipTuple(ObjectRef(type_, str(id_)), relation, subject)]
    )


POST_DELETE_SCHEMA = """
definition auth/user {}
definition blog/post {
    relation owner: auth/user
    relation locker: auth/user:*
    permission locked = locker
    permission read = owner
    permission write = owner
    permission delete = owner
}
definition blog/folder {
    relation owner: auth/user
    relation viewer: auth/user
    relation items: blog/post // rebac:field=posts
    permission read = viewer - items->locked
    permission write = owner
    permission delete = owner
}
"""


@pytest.mark.xfail(
    strict=True, reason="proposal 0011: deleting a resource lifts another type's backed exclusion"
)
@pytest.mark.parametrize("method", ["instance", "queryset"])
def test_post_delete_must_check_folder_write(method):
    install_schema(backend(), parse_zed(POST_DELETE_SCHEMA))
    with sudo(reason="fixture"):
        folder = Folder.objects.create(name="locked")
        post = Post.objects.create(title="p", folder=folder)
    grant("blog/post", post.pk, "owner", A)
    grant("blog/post", post.pk, "locker", SubjectRef.of("auth/user", "*"))
    grant("blog/folder", folder.pk, "viewer", A)
    resource = ObjectRef("blog/folder", str(folder.pk))
    assert not backend().has_access(subject=A, action="read", resource=resource)
    with actor_context(A), pytest.raises(PermissionDenied), transaction.atomic():
        if method == "instance":
            Post.objects.with_actor(A).get(pk=post.pk).delete()
        else:
            Post.objects.with_actor(A).filter(pk=post.pk).delete()
    assert Post._base_manager.filter(pk=post.pk).exists()
    assert not backend().has_access(subject=A, action="read", resource=resource)


REVERSE_FK_SCHEMA = """
definition auth/user {}
definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user
    permission read = owner + viewer
    permission write = owner
    permission delete = owner
}
definition blog/folder {
    relation owner: auth/user
    relation items: blog/post // rebac:field=posts
    permission read = owner + items->read
    permission write = owner
    permission delete = owner
}
"""


@pytest.mark.xfail(
    strict=True, reason="proposal 0011: reverse FK bulk add skips the moved row's write"
)
def test_reverse_fk_bulk_add_must_check_moved_post():
    install_schema(backend(), parse_zed(REVERSE_FK_SCHEMA))
    with sudo(reason="fixture"):
        post = Post.objects.create(title="theirs")
        folder = Folder.objects.create(name="mine")
    grant("blog/post", post.pk, "viewer", A)
    grant("blog/folder", folder.pk, "owner", A)
    mine = Folder.objects.with_actor(A).get(pk=folder.pk)
    theirs = Post.objects.with_actor(A).get(pk=post.pk)
    with actor_context(A), pytest.raises(PermissionDenied), transaction.atomic():
        mine.posts.add(theirs, bulk=True)
    assert Post._base_manager.get(pk=post.pk).folder_id is None


@pytest.mark.xfail(
    strict=True, reason="proposal 0011: reverse FK bulk add uses ambient actor over pinned actor"
)
def test_reverse_fk_bulk_add_must_use_pinned_actor():
    install_schema(backend(), parse_zed(REVERSE_FK_SCHEMA))
    with sudo(reason="fixture"):
        post = Post.objects.create(title="p")
        folder = Folder.objects.create(name="f")
    grant("blog/post", post.pk, "owner", A)
    grant("blog/post", post.pk, "owner", B)
    grant("blog/folder", folder.pk, "owner", A)
    pinned = Folder.objects.with_actor(A).get(pk=folder.pk).with_actor(B)
    row = Post.objects.with_actor(A).get(pk=post.pk)
    with actor_context(A), pytest.raises(PermissionDenied), transaction.atomic():
        pinned.posts.add(row, bulk=True)


@pytest.mark.xfail(
    strict=True, reason="proposal 0011: ambient sudo bypasses pinned reverse FK actor"
)
def test_reverse_fk_bulk_add_pinned_actor_must_beat_block_sudo():
    install_schema(backend(), parse_zed(REVERSE_FK_SCHEMA))
    with sudo(reason="fixture"):
        post = Post.objects.create(title="p")
        folder = Folder.objects.create(name="f")
    grant("blog/post", post.pk, "owner", B)
    grant("blog/folder", folder.pk, "owner", A)
    pinned = Folder.objects.with_actor(A).get(pk=folder.pk).with_actor(B)
    row = Post.objects.with_actor(B).get(pk=post.pk)
    with sudo(reason="unrelated block"), pytest.raises(PermissionDenied), transaction.atomic():
        pinned.posts.add(row, bulk=True)


CASCADE_SCHEMA = """
definition auth/user {}
definition test/backingtask {
    relation owner: auth/user
    permission read = owner
    permission write = owner
    permission delete = owner
}
definition test/backinground {
    relation owner: auth/user
    relation viewer: auth/user
    relation banned: auth/user // rebac:field=project__task__asker
    permission read = viewer - banned
    permission write = owner
}
"""


@pytest.mark.xfail(
    strict=True,
    reason="proposal 0011: CASCADE checks an ambient actor instead of the pinned delete actor",
)
def test_task_cascade_must_check_round_under_pinned_actor():
    install_schema(backend(), parse_zed(CASCADE_SCHEMA))
    with sudo(reason="fixture"), transaction.atomic():
        user = get_user_model().objects.create_user(username="cascade-user")
        queue = BackingQueue.objects.create()
        task = BackingTask.objects.create(queue=queue, asker=user)
        project = BackingProject.objects.create(task=task)
        round_ = BackingRound.objects.create(project=project)
    grant("test/backingtask", task.pk, "owner", A)
    grant("test/backinground", round_.pk, "viewer", to_subject_ref(user))
    grant("test/backinground", round_.pk, "owner", B)
    resource = ObjectRef("test/backinground", str(round_.pk))
    assert not backend().has_access(subject=to_subject_ref(user), action="read", resource=resource)
    with actor_context(B), pytest.raises(PermissionDenied), transaction.atomic():
        BackingTask.objects.with_actor(A).get(pk=task.pk).delete()
    assert not backend().has_access(subject=to_subject_ref(user), action="read", resource=resource)


SETNULL_SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation owner: auth/user
    relation locked: auth/user:*
    permission read = owner
    permission write = owner
    permission delete = owner
}
definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user
    relation folder: blog/folder // rebac:field=folder
    permission read = viewer - folder->locked
    permission write = owner
    permission delete = owner
}
"""


@pytest.mark.xfail(
    strict=True,
    reason="proposal 0011: collector SET_NULL checks ambient actor over pinned delete actor",
)
def test_set_null_must_check_post_under_pinned_actor():
    install_schema(backend(), parse_zed(SETNULL_SCHEMA))
    with sudo(reason="fixture"):
        folder = Folder.objects.create(name="f")
        post = Post.objects.create(title="p", folder=folder)
    grant("blog/folder", folder.pk, "owner", A)
    grant("blog/folder", folder.pk, "locked", SubjectRef.of("auth/user", "*"))
    grant("blog/post", post.pk, "viewer", A)
    grant("blog/post", post.pk, "owner", B)
    resource = ObjectRef("blog/post", str(post.pk))
    assert not backend().has_access(subject=A, action="read", resource=resource)
    with actor_context(B), pytest.raises(PermissionDenied), transaction.atomic():
        Folder.objects.with_actor(A).get(pk=folder.pk).delete()
    assert not backend().has_access(subject=A, action="read", resource=resource)


NO_WRITE_SCHEMA = """
definition auth/user {}
definition test/backinground {
    relation owner: auth/user
    relation responders: auth/user // rebac:field=entries__responder
    permission read = owner + responders
    permission edit = owner
}
"""


@pytest.mark.xfail(
    strict=True, reason="proposal 0011: a resource type using edit has no actor gate"
)
def test_actorless_tracked_create_must_not_grant_read_on_edit_only_resource():
    install_schema(backend(), parse_zed(NO_WRITE_SCHEMA))
    with sudo(reason="fixture"):
        round_ = BackingRound.objects.create()
    user = get_user_model().objects.create_user(username="actorless-responder")
    resource = ObjectRef("test/backinground", str(round_.pk))
    assert not backend().has_access(subject=to_subject_ref(user), action="read", resource=resource)
    with pytest.raises(MissingActorError), transaction.atomic():
        BackingEntry.objects.create(round=round_, responder=user)
    assert not backend().has_access(subject=to_subject_ref(user), action="read", resource=resource)
