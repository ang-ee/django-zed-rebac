"""Adapter gates on identifiers that do not name the row they authorize."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.test import override_settings

from rebac import (
    ObjectRef,
    PermissionDenied,
    PermissionResult,
    RelationshipTuple,
    SubjectRef,
    backend,
    check_new,
    sudo,
)
from rebac.backends import reset_backend
from rebac.drf import RebacPermission
from rebac.mcp import rebac_mcp_tool
from rebac.schema import parse_zed
from tests.backend_setup import atomic_source_write, install_schema

SCHEMA_TEXT = """
definition auth/user {}

definition blog/post {
    relation owner: auth/user
    relation banned: auth/user
    relation parent: blog/post
    permission read = authenticated - banned
    permission write = owner
    permission create = parent->write
}

definition blog/sluggedpost {
    relation owner: auth/user
    relation editor: auth/user
    relation viewer: auth/user
    permission read = owner + editor + viewer
    permission write = owner + editor
    permission delete = owner
    permission read__slug = owner
}

definition site/page {
    permission create = authenticated
}
"""

ALICE = SubjectRef.of("auth/user", "alice")
NONCANONICAL = ("0{}", " {}", "+{}", "{}\n")


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


def _ctx(actor_subject: str) -> SimpleNamespace:
    return SimpleNamespace(request_context=SimpleNamespace(meta={"actor_subject": actor_subject}))


def _grant(resource: ObjectRef, relation: str, subject: SubjectRef) -> None:
    backend().write_relationships(
        [RelationshipTuple(resource=resource, relation=relation, subject=subject)]
    )


def _user(username: str):
    from django.contrib.auth import get_user_model

    return atomic_source_write(get_user_model().objects.create_user, username=username)


def _banned_post():
    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="banned for alice")
    _grant(ObjectRef("blog/post", str(post.pk)), "banned", ALICE)
    return post


# ---------- DRF ----------


def test_drf_object_permission_denies_unsaved_instance() -> None:
    from tests.testapp.models import Post

    request = SimpleNamespace(method="DELETE", user=_user("drf-unsaved"))
    view = SimpleNamespace(action="destroy", queryset=Post.objects.all())

    assert not RebacPermission().has_object_permission(request, view, Post(title="unsaved"))


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_drf_object_permission_denies_instance_with_redacted_id() -> None:
    from tests.testapp.models import SluggedPost

    user = _user("drf-redacted")
    subject = SubjectRef.of("auth/user", str(user.pk))
    with sudo(reason="test.fixture"):
        SluggedPost.objects.create(slug="hidden-id", title="t")
    _grant(ObjectRef("blog/sluggedpost", "hidden-id"), "viewer", subject)
    instance = SluggedPost.objects.as_user(user).get()
    assert instance.slug is None
    assert not backend().has_access(
        subject=subject, action="delete", resource=ObjectRef("blog/sluggedpost", "hidden-id")
    )
    request = SimpleNamespace(method="DELETE", user=user)
    view = SimpleNamespace(action="destroy", queryset=SluggedPost.objects.all())

    assert not RebacPermission().has_object_permission(request, view, instance)


# ---------- MCP ----------


def test_mcp_empty_id_arg_is_not_a_model_level_check() -> None:
    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
    post_id = str(post.pk)
    _grant(ObjectRef("blog/post", post_id), "owner", ALICE)
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="blog/post", action="write", id_arg="post_id")
    def edit_post(post_id: str, ctx: object = None) -> str:
        calls.append(post_id)
        return "ok"

    assert edit_post(post_id, ctx=_ctx("auth/user:alice")) == "ok"
    with pytest.raises(PermissionDenied):
        edit_post("", ctx=_ctx("auth/user:alice"))
    assert calls == [post_id]


def test_mcp_create_relations_with_empty_id_denies() -> None:
    _grant(ObjectRef("blog/post", "p0"), "owner", ALICE)
    calls: list[str] = []

    @rebac_mcp_tool(
        resource_type="blog/post", action="create", create_relations={"parent": "parent_ref"}
    )
    def create_post(parent_ref: str, ctx: object = None) -> str:
        calls.append(parent_ref)
        return "ok"

    assert create_post("blog/post:p0", ctx=_ctx("auth/user:alice")) == "ok"
    with pytest.raises(PermissionDenied):
        create_post("blog/post:", ctx=_ctx("auth/user:alice"))
    assert calls == ["blog/post:p0"]


@pytest.mark.parametrize("spelling", NONCANONICAL)
def test_mcp_noncanonical_id_arg_does_not_skip_concrete_ban(spelling) -> None:
    from tests.testapp.models import Post

    post = _banned_post()
    wire = spelling.format(post.pk)
    assert Post._base_manager.get(pk=wire) == post
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="blog/post", action="read", id_arg="post_id")
    def read_post(post_id: str, ctx: object = None) -> str:
        calls.append(post_id)
        return "ok"

    with pytest.raises(PermissionDenied):
        read_post(str(post.pk), ctx=_ctx("auth/user:alice"))
    with pytest.raises(PermissionDenied):
        read_post(wire, ctx=_ctx("auth/user:alice"))
    assert calls == []


# ---------- check_new ----------


def test_check_new_empty_id_overlay_subject_denies() -> None:
    _grant(ObjectRef("blog/post", "p0"), "owner", ALICE)

    assert check_new(
        subject=ALICE,
        action="create",
        resource_type="blog/post",
        relationships={"parent": [SubjectRef.of("blog/post", "p0")]},
    ).allowed
    assert not check_new(
        subject=ALICE,
        action="create",
        resource_type="blog/post",
        relationships={"parent": [SubjectRef.of("blog/post", "")]},
    ).allowed


def test_check_new_empty_id_actor_is_not_authenticated() -> None:
    assert check_new(subject=ALICE, action="create", resource_type="site/page").allowed
    result = check_new(
        subject=SubjectRef.of("auth/user", ""), action="create", resource_type="site/page"
    )
    assert result.result is PermissionResult.NO_PERMISSION


# ---------- backend.check_access identity ----------


@pytest.mark.parametrize("spelling", NONCANONICAL)
def test_noncanonical_id_without_a_term_reads_at_the_type_level(spelling) -> None:
    # Documented: a resource without a term of its own is read at the type-level
    # scope (ARCHITECTURE.md § Permission index, "T* also stands for a resource
    # that has no term yet"). A non-canonical spelling is a distinct wire id, so
    # the concrete ban on the canonical row does not apply to it.
    post = _banned_post()
    wire = spelling.format(post.pk)

    assert (
        not backend()
        .check_access(subject=ALICE, action="read", resource=ObjectRef("blog/post", str(post.pk)))
        .allowed
    )
    assert (
        backend()
        .check_access(subject=ALICE, action="read", resource=ObjectRef("blog/post", wire))
        .allowed
    )
