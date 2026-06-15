"""End-to-end LocalBackend permission checks through public surfaces.

This suite intentionally uses a small fake authorization world:

- Django test users are the fake actors.
- ``tests.testapp`` posts/folders are the fake resources.
- LocalBackend is the real evaluator behind public relationship helpers,
  model/queryset read scoping, and per-field read/write gates.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from django.test import override_settings

from rebac import (
    LocalBackend,
    ObjectRef,
    PermissionDenied,
    PermissionResult,
    RelationshipTuple,
    SubjectRef,
    backend,
    delete_relationship,
    delete_relationships,
    rebac_subject,
    sudo,
    write_relationships,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from rebac.types import RelationshipFilter

SCHEMA_TEXT = """
caveat link_not_expired(expires_at timestamp, now timestamp) {
    now < expires_at
}

definition auth/user {}

definition auth/group {
    relation member: auth/user
}

definition agents/agent {}
definition agents/grant {}

definition blog/folder {
    relation owner: auth/user
    relation parent: blog/folder
    permission read = owner + parent->read
    permission write = owner + parent->write
}

definition blog/post {
    relation owner: auth/user | agents/agent | agents/grant#valid
    relation editor: auth/user
    relation viewer: auth/user | auth/group#member | auth/user:*
    relation temporary_viewer: auth/user with link_not_expired
    relation title_reader: auth/user with link_not_expired
    relation folder: blog/folder

    permission read = owner + editor + viewer + temporary_viewer + folder->read
    permission global_read = authenticated
    permission write = owner + editor
    permission write__title = owner
    permission read__title = owner + title_reader
    permission delete = owner
}
"""

PAST = "1999-01-01T00:00:00Z"
MIDDLE = "2050-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"
EXPIRED = "2000-01-01T00:00:00Z"


@dataclass(frozen=True)
class FakeUsers:
    alice: Any
    bob: Any
    cara: Any


@rebac_subject(type="agents/agent", id_attr="slug")
class FakeAgent:
    def __init__(self, slug: str) -> None:
        self.slug = slug


@pytest.fixture
def local_backend(db) -> Iterator[LocalBackend]:
    reset_backend()
    active_backend = backend()
    assert isinstance(active_backend, LocalBackend)
    active_backend.set_schema(parse_zed(SCHEMA_TEXT))
    yield active_backend
    reset_backend()


@pytest.fixture
def fake_users(db) -> FakeUsers:
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return FakeUsers(
        alice=User.objects.create(username="alice", is_active=True),
        bob=User.objects.create(username="bob", is_active=True),
        cara=User.objects.create(username="cara", is_active=True),
    )


def _user(user: Any) -> SubjectRef:
    return SubjectRef.of("auth/user", str(user.pk))


def _agent(slug: str) -> SubjectRef:
    return SubjectRef.of("agents/agent", slug)


def _grant(user_or_subject: Any, resource: ObjectRef, relation: str) -> RelationshipTuple:
    subject = user_or_subject if isinstance(user_or_subject, SubjectRef) else _user(user_or_subject)
    return RelationshipTuple(resource=resource, relation=relation, subject=subject)


def _post_ref(resource_id: str) -> ObjectRef:
    return ObjectRef("blog/post", resource_id)


def _folder_ref(resource_id: str) -> ObjectRef:
    return ObjectRef("blog/folder", resource_id)


def _create_post(*, title: str, body: str = ""):
    from tests.testapp.models import Post

    with sudo(reason="local-backend-e2e.fixture"):
        return Post.objects.create(title=title, body=body)


def test_public_backend_methods_cover_graph_checks_and_relationship_lifecycle(
    local_backend: LocalBackend,
    fake_users: FakeUsers,
) -> None:
    owned = _post_ref("owned")
    group_post = _post_ref("group-post")
    folder_post = _post_ref("folder-post")
    public_post = _post_ref("public-post")
    folder = _folder_ref("root")

    zookie = write_relationships(
        [
            _grant(fake_users.alice, owned, "owner"),
            _grant(fake_users.bob, owned, "editor"),
            RelationshipTuple(
                resource=ObjectRef("auth/group", "eng"),
                relation="member",
                subject=_user(fake_users.cara),
            ),
            RelationshipTuple(
                resource=group_post,
                relation="viewer",
                subject=SubjectRef.of("auth/group", "eng", "member"),
            ),
            _grant(fake_users.alice, folder, "owner"),
            RelationshipTuple(
                resource=folder_post,
                relation="folder",
                subject=SubjectRef.of("blog/folder", "root"),
            ),
            RelationshipTuple(
                resource=public_post,
                relation="viewer",
                subject=SubjectRef.of("auth/user", "*"),
            ),
            _grant(_agent("indexer"), owned, "owner"),
        ]
    )

    assert zookie.backend == "local"
    assert local_backend.check_access(
        subject=_user(fake_users.alice), action="delete", resource=owned
    ).allowed
    assert local_backend.has_access(subject=_user(fake_users.bob), action="write", resource=owned)
    assert not local_backend.has_access(
        subject=_user(fake_users.bob), action="delete", resource=owned
    )
    assert local_backend.has_access(
        subject=_user(fake_users.cara), action="read", resource=group_post
    )
    assert local_backend.has_access(
        subject=_user(fake_users.alice), action="read", resource=folder_post
    )
    assert local_backend.has_access(
        subject=_user(fake_users.bob), action="read", resource=public_post
    )
    assert local_backend.has_access(subject=_agent("indexer"), action="write", resource=owned)
    assert local_backend.grants_all(
        subject=_user(fake_users.alice),
        action="global_read",
        resource_type="blog/post",
    )

    assert set(
        local_backend.accessible(
            subject=_user(fake_users.alice),
            action="read",
            resource_type="blog/post",
        )
    ) == {"owned", "folder-post", "public-post"}
    assert set(
        local_backend.lookup_subjects(
            resource=owned,
            action="write",
            subject_type="auth/user",
        )
    ) == {_user(fake_users.alice), _user(fake_users.bob)}

    delete_relationship(_grant(fake_users.bob, owned, "editor"))
    assert not local_backend.has_access(
        subject=_user(fake_users.bob), action="write", resource=owned
    )
    assert local_backend.has_access(subject=_user(fake_users.alice), action="write", resource=owned)

    delete_relationships(
        RelationshipFilter(
            resource_type="blog/post",
            resource_id="owned",
            relation="owner",
            subject_type="auth/user",
            subject_id=str(fake_users.alice.pk),
        )
    )
    assert not local_backend.has_access(
        subject=_user(fake_users.alice), action="delete", resource=owned
    )


def test_local_backend_consistency_tokens_scope_direct_checks_and_accessible(
    local_backend: LocalBackend,
    fake_users: FakeUsers,
) -> None:
    resource = _post_ref("freshness")

    before_bob = write_relationships([_grant(fake_users.alice, resource, "owner")])
    after_bob = write_relationships([_grant(fake_users.bob, resource, "viewer")])

    assert local_backend.has_access(subject=_user(fake_users.bob), action="read", resource=resource)
    assert not local_backend.has_access(
        subject=_user(fake_users.bob),
        action="read",
        resource=resource,
        at_zookie=before_bob,
    )
    assert local_backend.has_access(
        subject=_user(fake_users.bob),
        action="read",
        resource=resource,
        at_zookie=after_bob,
    )
    assert (
        list(
            local_backend.accessible(
                subject=_user(fake_users.bob),
                action="read",
                resource_type="blog/post",
                at_zookie=before_bob,
            )
        )
        == []
    )
    assert set(
        local_backend.accessible(
            subject=_user(fake_users.bob),
            action="read",
            resource_type="blog/post",
            at_zookie=after_bob,
        )
    ) == {"freshness"}


def test_local_backend_caveats_are_conditional_for_checks_and_conservative_for_reads(
    local_backend: LocalBackend,
    fake_users: FakeUsers,
) -> None:
    ok = _post_ref("temporary-ok")
    expired = _post_ref("temporary-expired")
    write_relationships(
        [
            RelationshipTuple(
                resource=ok,
                relation="temporary_viewer",
                subject=_user(fake_users.alice),
                caveat_name="link_not_expired",
                caveat_context={"expires_at": FUTURE},
            ),
            RelationshipTuple(
                resource=expired,
                relation="temporary_viewer",
                subject=_user(fake_users.alice),
                caveat_name="link_not_expired",
                caveat_context={"expires_at": EXPIRED},
            ),
        ]
    )

    conditional = local_backend.check_access(
        subject=_user(fake_users.alice),
        action="read",
        resource=ok,
    )
    assert conditional.result == PermissionResult.CONDITIONAL_PERMISSION
    assert conditional.conditional_on == ("now",)
    assert local_backend.check_access(
        subject=_user(fake_users.alice),
        action="read",
        resource=ok,
        context={"now": PAST},
    ).allowed
    assert not local_backend.check_access(
        subject=_user(fake_users.alice),
        action="read",
        resource=expired,
        context={"now": FUTURE},
    ).allowed
    assert (
        list(
            local_backend.accessible(
                subject=_user(fake_users.alice),
                action="read",
                resource_type="blog/post",
            )
        )
        == []
    )
    assert set(
        local_backend.accessible(
            subject=_user(fake_users.alice),
            action="read",
            resource_type="blog/post",
            context={"now": MIDDLE},
        )
    ) == {"temporary-ok"}


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_queryset_read_methods_and_field_read_gates_use_local_backend_end_to_end(
    local_backend: LocalBackend,
    fake_users: FakeUsers,
) -> None:
    from tests.testapp.models import Post

    owned = _create_post(title="visible to owner")
    viewer_visible_row = _create_post(title="secret title", body="viewer can see body")
    hidden = _create_post(title="hidden")
    write_relationships(
        [
            _grant(fake_users.alice, ObjectRef("blog/post", str(owned.pk)), "owner"),
            _grant(fake_users.bob, ObjectRef("blog/post", str(viewer_visible_row.pk)), "owner"),
            _grant(fake_users.alice, ObjectRef("blog/post", str(viewer_visible_row.pk)), "viewer"),
            _grant(fake_users.alice, ObjectRef("blog/post", str(viewer_visible_row.pk)), "editor"),
            _grant(fake_users.bob, ObjectRef("blog/post", str(hidden.pk)), "owner"),
        ]
    )

    scoped = Post.objects.as_user(fake_users.alice).order_by("pk")
    assert scoped.count() == 2
    assert list(scoped.values_list("pk", flat=True)) == [owned.pk, viewer_visible_row.pk]
    assert Post.objects.as_user(fake_users.alice).filter(pk=owned.pk).exists()
    assert not Post.objects.as_user(fake_users.alice).filter(pk=hidden.pk).exists()

    owner_row = Post.objects.as_user(fake_users.alice).get(pk=owned.pk)
    assert owner_row.title == "visible to owner"
    viewer_row = Post.objects.as_user(fake_users.alice).get(pk=viewer_visible_row.pk)
    assert viewer_row.title is None
    assert viewer_row.body == "viewer can see body"
    assert viewer_row._rebac_redacted_fields == frozenset({"title"})
    iterated = list(
        Post.objects.as_user(fake_users.alice).filter(pk=viewer_visible_row.pk).iterator()
    )
    assert iterated[0].title is None

    with pytest.raises(PermissionDenied):
        list(Post.objects.as_user(fake_users.alice).values("title"))

    viewer_row.title = "attempted overwrite"
    with pytest.raises(PermissionDenied, match="redacted"):
        viewer_row.save(update_fields=["title"])


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_caveated_field_read_permission_fails_closed_and_allows_with_context(
    local_backend: LocalBackend,
    fake_users: FakeUsers,
) -> None:
    from tests.testapp.models import Post

    post = _create_post(title="caveated title", body="visible body")
    resource = ObjectRef("blog/post", str(post.pk))
    write_relationships(
        [
            _grant(fake_users.alice, resource, "viewer"),
            RelationshipTuple(
                resource=resource,
                relation="title_reader",
                subject=_user(fake_users.alice),
                caveat_name="link_not_expired",
                caveat_context={"expires_at": FUTURE},
            ),
        ]
    )

    row = Post.objects.as_user(fake_users.alice).get(pk=post.pk)
    assert row.body == "visible body"
    assert row.title is None
    assert row._rebac_redacted_fields == frozenset({"title"})

    with sudo(reason="local-backend-e2e.load"):
        instance = Post.objects.get(pk=post.pk)
    instance.with_actor(fake_users.alice)
    assert instance.denied_read_fields(context={"now": MIDDLE}) == frozenset()
    assert instance.denied_read_fields(context={"now": FUTURE}) == frozenset({"title"})


def test_model_write_delete_and_field_write_gates_use_local_backend_end_to_end(
    local_backend: LocalBackend,
    fake_users: FakeUsers,
) -> None:
    from tests.testapp.models import Post

    post = _create_post(title="original title", body="original body")
    resource = ObjectRef("blog/post", str(post.pk))
    write_relationships(
        [
            _grant(fake_users.alice, resource, "owner"),
            _grant(fake_users.bob, resource, "editor"),
        ]
    )

    editor_copy = Post.objects.as_user(fake_users.bob).get(pk=post.pk)
    editor_copy.body = "editor body"
    editor_copy.save()
    with sudo(reason="local-backend-e2e.verify"):
        assert Post.objects.get(pk=post.pk).body == "editor body"

    editor_copy = Post.objects.as_user(fake_users.bob).get(pk=post.pk)
    editor_copy.title = "editor title"
    with pytest.raises(PermissionDenied, match="write__title"):
        editor_copy.save()
    with sudo(reason="local-backend-e2e.verify"):
        assert Post.objects.get(pk=post.pk).title == "original title"

    delete_relationship(_grant(fake_users.bob, resource, "editor"))
    assert not local_backend.has_access(
        subject=_user(fake_users.bob),
        action="write",
        resource=resource,
    )
    with pytest.raises(PermissionDenied):
        Post.objects.as_user(fake_users.bob).filter(pk=post.pk).update(body="after revoke")

    write_relationships([_grant(fake_users.bob, resource, "editor")])
    with pytest.raises(PermissionDenied, match="write__title"):
        Post.objects.as_user(fake_users.bob).filter(pk=post.pk).update(title="bulk editor title")
    with sudo(reason="local-backend-e2e.verify"):
        assert Post.objects.get(pk=post.pk).title == "original title"
    assert Post.objects.as_user(fake_users.bob).filter(pk=post.pk).update(body="bulk body") == 1
    assert (
        Post.objects.as_user(fake_users.alice).filter(pk=post.pk).update(title="owner title") == 1
    )

    with pytest.raises(PermissionDenied):
        Post.objects.as_user(fake_users.bob).filter(pk=post.pk).delete()
    assert Post.objects.as_user(fake_users.alice).filter(pk=post.pk).delete()[0] == 1
    with sudo(reason="local-backend-e2e.verify"):
        assert not Post.objects.filter(pk=post.pk).exists()


def test_as_agent_shorthands_scope_querysets_and_instances_through_local_backend(
    local_backend: LocalBackend,
    fake_users: FakeUsers,
) -> None:
    from tests.testapp.models import Post

    post = _create_post(title="agent readable")
    agent = FakeAgent("assistant")
    grant_subject = SubjectRef.of("agents/grant", f"{fake_users.alice.pk}.assistant", "valid")
    write_relationships([_grant(grant_subject, ObjectRef("blog/post", str(post.pk)), "owner")])

    assert list(
        Post.objects.as_agent(agent, on_behalf_of=fake_users.alice).values_list("pk", flat=True)
    ) == [post.pk]
    assert list(Post.objects.as_agent(agent, on_behalf_of=fake_users.bob)) == []

    with sudo(reason="local-backend-e2e.load"):
        instance = Post.objects.get(pk=post.pk)
    instance.as_agent(agent, on_behalf_of=fake_users.alice)
    assert instance.actor() == grant_subject
    assert instance.has_access("read")
    instance.as_agent(agent, on_behalf_of=fake_users.bob)
    assert not instance.has_access("read")
