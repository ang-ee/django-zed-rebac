"""Issue #11: a Zookie must witness the relationship writes nested inside its write.

Reported from a downstream project (2026-08-15, no reproduction attached): the
watermark a write returns or advances is wrong when that write nests further tuple
writes, from model signals or save-time side effects, and a read pinned to it misses
the nested tuples.

The contract these tests hold (docs/ARCHITECTURE.md § Zookie freshness and § The
unified check API; src/rebac/consistency.py):

* Every backend write returns a Zookie. On LocalBackend the token is an xid:
  ``write_relationships`` returns the high watermark of the batch's
  ``written_at_xid``, and the delete verbs return a fresh clock value.
* A read with ``Consistency.AT_LEAST_AS_FRESH`` and that token observes every effect
  of the write that returned it, and may observe newer ones: the token is a freshness
  floor, never a cutoff. LocalBackend reads the state visible on its connection, so
  the effects must already be in the permission index when the read runs.
* Writes nested inside a write are effects of it. The token is therefore not lower
  than the ``written_at_xid`` of any tuple written while the write ran, and the
  ambient token of a ``zookie_scope`` never moves backwards.

There is no ``awrite_relationships`` or ``acheck_access``. The async surface is
Django's ``asave()`` under the async ``ActorMiddleware``, covered at the end.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from django.db.models.signals import ModelSignal, post_save
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from rebac import (
    Consistency,
    LocalBackend,
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    Zookie,
    current_zookie,
    sudo,
    to_object_ref,
    write_relationships,
    zookie_scope,
)
from rebac.consistency import effective_consistency
from rebac.index.maintain import current_pass
from rebac.middleware import ActorMiddleware
from rebac.models import active_relationship_model
from rebac.schema import parse_zed
from rebac.types import RelationshipFilter
from tests.backend_setup import install_schema
from tests.testapp.models import Folder, Post

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation viewer: auth/user
    permission read = viewer
}
definition blog/post {
    relation owner: auth/user
    relation editor: auth/user
    relation folder: blog/folder // rebac:field=folder
    permission read = owner + editor + folder->read
}
"""

ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")
CAROL = SubjectRef.of("auth/user", "carol")

# (check_access, accessible, lookup_subjects) agree on every read below.
SEEN = (True, True, True)
UNSEEN = (False, False, False)


@pytest.fixture
def local(db: None) -> LocalBackend:
    backend = LocalBackend()
    # Also installs it as rebac.backend(), which the model owners and helpers use.
    install_schema(backend, parse_zed(SCHEMA))
    return backend


@contextmanager
def connected(signal: ModelSignal, sender: type, handler: Callable[..., None]) -> Iterator[None]:
    uid = f"{__name__}.{handler.__name__}"
    signal.connect(handler, sender=sender, weak=False, dispatch_uid=uid)
    try:
        yield
    finally:
        signal.disconnect(sender=sender, dispatch_uid=uid)


def seen(
    local: LocalBackend, token: Zookie, subject: SubjectRef, resource: ObjectRef
) -> tuple[bool, bool, bool]:
    """Whether ``subject`` reads ``resource`` on each read surface, at least as fresh as token."""
    fresh: dict[str, Any] = {"consistency": Consistency.AT_LEAST_AS_FRESH, "at_zookie": token}
    return (
        local.check_access(subject=subject, action="read", resource=resource, **fresh).allowed,
        resource.resource_id
        in set(
            local.accessible(
                subject=subject, action="read", resource_type=resource.resource_type, **fresh
            )
        ),
        subject.subject_id
        in {
            ref.subject_id
            for ref in local.lookup_subjects(
                resource=resource, action="read", subject_type=subject.subject_type, **fresh
            )
        },
    )


def high_watermark() -> int:
    """The newest ``written_at_xid`` among the stored rows (older rows have lower ones)."""
    return max(active_relationship_model().objects.values_list("written_at_xid", flat=True))


def create_post(**fields: Any) -> Post:
    with sudo(reason="zookie nested writes fixture"):
        return Post.objects.create(title="post", **fields)


# ---------- 1. backend.write_relationships in a post_save of a RebacMixin save ----------


def test_post_save_writes_are_read_at_their_zookies_after_the_save(local):
    tokens: list[Zookie] = []

    def persist_grants(sender, instance, created, **kwargs):
        # The row's own owner tuple (CLAUDE.md § 5b), then a second nested write.
        ref = to_object_ref(instance)
        tokens.append(local.write_relationships([RelationshipTuple(ref, "owner", ALICE)]))
        owner_xid = high_watermark()
        tokens.append(local.write_relationships([RelationshipTuple(ref, "editor", BOB)]))
        assert int(tokens[0].token) >= owner_xid

    with connected(post_save, Post, persist_grants):
        post = create_post()

    ref = to_object_ref(post)
    for token in tokens:
        assert seen(local, token, ALICE, ref) == SEEN
        assert seen(local, token, BOB, ref) == SEEN
    assert int(tokens[-1].token) >= high_watermark()


def test_post_save_write_is_read_at_its_zookie_inside_the_save(local):
    observed: dict[str, Any] = {}

    def persist_owner(sender, instance, created, **kwargs):
        ref = to_object_ref(instance)
        token = local.write_relationships([RelationshipTuple(ref, "owner", ALICE)])
        # The tuple is stored and visible on this connection; RebacMixin.save_base's
        # owner (src/rebac/mixins.py:654) is still open around this handler.
        assert current_pass("default") is not None
        assert active_relationship_model().objects.filter(relation="owner").exists()
        assert int(token.token) >= high_watermark()
        observed["alice"] = seen(local, token, ALICE, ref)

    with connected(post_save, Post, persist_owner):
        create_post()

    assert observed["alice"] == SEEN


def test_write_nested_in_write_relationships_is_read_at_the_outer_zookie(local):
    post = create_post()
    ref = to_object_ref(post)

    def grant_editor(sender, instance, created, **kwargs):
        if instance.relation == "owner":
            local.write_relationships([RelationshipTuple(ref, "editor", BOB)])

    with connected(post_save, active_relationship_model(), grant_editor):
        token = local.write_relationships([RelationshipTuple(ref, "owner", ALICE)])

    assert seen(local, token, ALICE, ref) == SEEN
    assert seen(local, token, BOB, ref) == SEEN


def test_outer_zookie_witnesses_writes_nested_in_write_relationships(local):
    post = create_post()
    ref = to_object_ref(post)

    def grant_editor(sender, instance, created, **kwargs):
        if instance.relation == "owner":
            local.write_relationships([RelationshipTuple(ref, "editor", BOB)])

    with connected(post_save, active_relationship_model(), grant_editor):
        token = local.write_relationships([RelationshipTuple(ref, "owner", ALICE)])

    assert active_relationship_model().objects.filter(relation="editor").exists()
    assert int(token.token) >= high_watermark()


# ---------- 2. a field-backed save plus a signal-triggered tuple in one transaction ----------


@pytest.mark.parametrize("change", ["create", "reparent"])
@pytest.mark.parametrize("where", ["in_handler", "after_save", "after_atomic"])
def test_field_backed_save_and_signal_tuple_are_read_at_the_zookie(local, change, where):
    with sudo(reason="zookie nested writes fixture"):
        granting = Folder.objects.create(name="granting")
        revoking = Folder.objects.create(name="revoking")
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(granting), "viewer", BOB),
            RelationshipTuple(to_object_ref(revoking), "viewer", CAROL),
        ]
    )
    post = create_post(folder=revoking) if change == "reparent" else None
    observed: dict[str, Any] = {}

    def read(ref: ObjectRef) -> None:
        token = observed["token"]
        observed["reads"] = {s.subject_id: seen(local, token, s, ref) for s in (ALICE, BOB, CAROL)}

    def persist_owner(sender, instance, **kwargs):
        ref = to_object_ref(instance)
        observed["token"] = local.write_relationships([RelationshipTuple(ref, "owner", ALICE)])
        if where == "in_handler":
            read(ref)

    with connected(post_save, Post, persist_owner), transaction.atomic():
        with sudo(reason="zookie nested writes save"):
            if post is None:
                post = Post.objects.create(title="post", folder=granting)
            else:
                post.folder = granting
                post.save()
        if where == "after_save":
            read(to_object_ref(post))
    if where == "after_atomic":
        read(to_object_ref(post))

    # alice: the signal's tuple; bob: the new folder; carol: the old folder, if any.
    assert observed["reads"] == {"alice": SEEN, "bob": SEEN, "carol": UNSEEN}
    assert int(observed["token"].token) >= high_watermark()


# ---------- 3. two write_relationships in one atomic block, read after commit ----------


@pytest.mark.django_db(transaction=True)
def test_sequential_writes_in_one_atomic_are_read_at_each_zookie(local):
    post = create_post()
    ref = to_object_ref(post)

    with transaction.atomic():
        first = local.write_relationships([RelationshipTuple(ref, "owner", ALICE)])
        assert int(first.token) >= high_watermark()
        assert seen(local, first, ALICE, ref) == SEEN
        assert seen(local, first, BOB, ref) == UNSEEN
        second = local.write_relationships([RelationshipTuple(ref, "editor", BOB)])
        assert int(second.token) >= high_watermark()
        assert int(second.token) >= int(first.token)
        assert seen(local, second, BOB, ref) == SEEN

    # Committed. The first token is a floor: it retains the later write.
    for token in (first, second):
        assert seen(local, token, ALICE, ref) == SEEN
        assert seen(local, token, BOB, ref) == SEEN


# ---------- 4. delete_relationships nested the same ways ----------


@pytest.mark.django_db(transaction=True)
def test_delete_in_one_atomic_is_read_at_its_zookie(local):
    post = create_post()
    ref = to_object_ref(post)

    with transaction.atomic():
        written = local.write_relationships(
            [RelationshipTuple(ref, "owner", ALICE), RelationshipTuple(ref, "editor", BOB)]
        )
        revoked_xid = high_watermark()
        deleted = local.delete_relationships(
            RelationshipFilter(
                resource_type=ref.resource_type, resource_id=ref.resource_id, relation="owner"
            )
        )
        assert int(deleted.token) > revoked_xid
        assert int(deleted.token) >= int(written.token)
        assert seen(local, deleted, ALICE, ref) == UNSEEN
        assert seen(local, deleted, BOB, ref) == SEEN

    for token in (written, deleted):
        assert seen(local, token, ALICE, ref) == UNSEEN
        assert seen(local, token, BOB, ref) == SEEN


@pytest.mark.parametrize("where", ["in_handler", "after_save"])
def test_post_save_delete_is_read_at_its_zookie(local, where):
    post = create_post()
    ref = to_object_ref(post)
    local.write_relationships([RelationshipTuple(ref, "editor", BOB)])
    observed: dict[str, Any] = {}

    def revoke_editors(sender, instance, created, **kwargs):
        observed["token"] = local.delete_relationships(
            RelationshipFilter(
                resource_type=ref.resource_type, resource_id=ref.resource_id, relation="editor"
            )
        )
        assert not active_relationship_model().objects.filter(relation="editor").exists()
        if where == "in_handler":
            observed["bob"] = seen(local, observed["token"], BOB, ref)

    with connected(post_save, Post, revoke_editors), sudo(reason="zookie nested writes save"):
        post.title = "retitled"
        post.save()
    if where == "after_save":
        observed["bob"] = seen(local, observed["token"], BOB, ref)

    assert observed["bob"] == UNSEEN


# ---------- ambient token: record_zookie / zookie_scope, as middleware transports it ----------


def test_ambient_zookie_after_a_save_with_nested_public_writes(local):
    def persist_grants(sender, instance, created, **kwargs):
        ref = to_object_ref(instance)
        write_relationships([RelationshipTuple(ref, "owner", ALICE)])
        write_relationships([RelationshipTuple(ref, "editor", BOB)])

    with zookie_scope(), connected(post_save, Post, persist_grants):
        post = create_post()
        consistency, token = effective_consistency()
        assert consistency is Consistency.AT_LEAST_AS_FRESH
        assert token is not None and token == current_zookie()
        assert int(token.token) >= high_watermark()
        ref = to_object_ref(post)
        assert seen(local, token, ALICE, ref) == SEEN
        assert seen(local, token, BOB, ref) == SEEN


def test_ambient_zookie_does_not_regress_below_a_nested_write(local):
    post = create_post()
    ref = to_object_ref(post)
    recorded: list[Zookie] = []

    def grant_editor(sender, instance, created, **kwargs):
        if instance.relation == "owner":
            write_relationships([RelationshipTuple(ref, "editor", BOB)])
            token = current_zookie()
            assert token is not None
            recorded.append(token)

    with zookie_scope(), connected(post_save, active_relationship_model(), grant_editor):
        write_relationships([RelationshipTuple(ref, "owner", ALICE)])
        token = current_zookie()
        assert token is not None
        assert seen(local, token, BOB, ref) == SEEN
        assert int(token.token) >= int(recorded[0].token)
        assert int(token.token) >= high_watermark()


@pytest.mark.parametrize("nesting", ["post_save", "relationship_post_save"])
@override_settings(REBAC_ZOOKIE_TRANSPORT="header")
def test_middleware_header_zookie_witnesses_nested_writes(local, nesting):
    post = create_post()
    ref = to_object_ref(post)

    def post_grants(sender, instance, created, **kwargs):
        write_relationships([RelationshipTuple(ref, "owner", ALICE)])
        write_relationships([RelationshipTuple(ref, "editor", BOB)])

    def relationship_grants(sender, instance, created, **kwargs):
        if instance.relation == "owner":
            write_relationships([RelationshipTuple(ref, "editor", BOB)])

    def view(request):
        if nesting == "post_save":
            with sudo(reason="zookie nested writes save"):
                post.save()
        else:
            write_relationships([RelationshipTuple(ref, "owner", ALICE)])
        return HttpResponse("ok")

    sender, handler = (
        (Post, post_grants)
        if nesting == "post_save"
        else (active_relationship_model(), relationship_grants)
    )
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    with connected(post_save, sender, handler):
        response = ActorMiddleware(view)(request)

    token = Zookie.parse(response["X-Rebac-Zookie"])
    assert seen(local, token, ALICE, ref) == SEEN
    assert seen(local, token, BOB, ref) == SEEN
    assert int(token.token) >= high_watermark()


# ---------- 5. async: no awrite_relationships / acheck_access; asave() under the middleware ----------


@pytest.mark.django_db(transaction=True)
@override_settings(REBAC_ZOOKIE_TRANSPORT="header")
def test_async_middleware_header_zookie_witnesses_writes_nested_in_asave(local):
    observed: dict[str, Any] = {}

    def persist_owner(sender, instance, created, **kwargs):
        write_relationships([RelationshipTuple(to_object_ref(instance), "owner", ALICE)])

    async def view(request):
        post = Post(title="post")
        # Async code enters sudo itself; asave() runs save() on asgiref's worker thread,
        # and the Zookie recorded there flows back to the request's scope.
        with sudo(reason="zookie nested writes asave"):
            await post.asave()
        observed["post"] = post
        observed["ambient"] = current_zookie()
        return HttpResponse("ok")

    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    with connected(post_save, Post, persist_owner):
        response = asyncio.run(ActorMiddleware(view)(request))

    token = Zookie.parse(response["X-Rebac-Zookie"])
    assert token == observed["ambient"]
    assert seen(local, token, ALICE, to_object_ref(observed["post"])) == SEEN
    assert int(token.token) >= high_watermark()
