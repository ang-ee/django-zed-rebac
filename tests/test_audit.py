"""Tests for PermissionAuditEvent emission.

Covers:
  - sudo() writes a KIND_SUDO_BYPASS row immediately (with the active actor).
  - write_relationships() emits one KIND_RELATIONSHIP_GRANT per tuple after
    the surrounding transaction commits (on_commit).
  - delete_relationships() emits one KIND_RELATIONSHIP_REVOKE per matched row.
  - rolled-back transaction → no GRANT row (proves deferral works).
  - REBAC_AUDIT_DENIALS gates whether denied saves write a denial row.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from django.db import transaction

from rebac import (
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    actor_context,
    backend,
    delete_relationships,
    sudo,
    write_relationships,
)
from rebac.backends import reset_backend
from rebac.models import PermissionAuditEvent
from rebac.schema import parse_zed
from rebac.types import RelationshipFilter

SCHEMA_TEXT = """
definition auth/user {}
definition auth/group {
    relation member: auth/user
}
definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user | auth/group#member
    permission read = owner + viewer
    permission write = owner
    permission delete = owner
    permission create = owner
}
"""


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


# ---------- sudo emission ----------


@pytest.mark.django_db
def test_sudo_emits_bypass_row_with_reason():
    PermissionAuditEvent.objects.all().delete()
    with sudo(reason="cron.gc"):
        # Inside the block — write must already exist (defer_to_commit=False).
        rows = list(PermissionAuditEvent.objects.filter(kind="sudo.bypass"))
        assert len(rows) == 1
        assert rows[0].reason == "cron.gc"


@pytest.mark.django_db
def test_sudo_records_ambient_actor_when_set():
    PermissionAuditEvent.objects.all().delete()
    actor = SubjectRef.of("auth/user", "42")
    with actor_context(actor):
        with sudo(reason="impersonation.support"):
            pass
    row = PermissionAuditEvent.objects.get(kind="sudo.bypass")
    assert row.actor_subject_type == "auth/user"
    assert row.actor_subject_id == "42"
    assert row.reason == "impersonation.support"


@pytest.mark.django_db
def test_sudo_with_no_ambient_actor_records_empty_actor():
    PermissionAuditEvent.objects.all().delete()
    with sudo(reason="bootstrap.init"):
        pass
    row = PermissionAuditEvent.objects.get(kind="sudo.bypass")
    assert row.actor_subject_type == ""
    assert row.actor_subject_id == ""


# ---------- write_relationships emission ----------


@pytest.mark.django_db(transaction=True)
def test_write_relationships_emits_grant_after_commit():
    PermissionAuditEvent.objects.all().delete()
    tup1 = RelationshipTuple(
        resource=ObjectRef("blog/post", "1"),
        relation="owner",
        subject=SubjectRef.of("auth/user", "alice"),
    )
    tup2 = RelationshipTuple(
        resource=ObjectRef("blog/post", "2"),
        relation="viewer",
        subject=SubjectRef.of("auth/group", "eng", "member"),
    )
    with transaction.atomic():
        write_relationships([tup1, tup2])
        # Pre-commit — no audit rows yet (deferred).
        assert PermissionAuditEvent.objects.filter(kind="rel.grant").count() == 0

    rows = list(PermissionAuditEvent.objects.filter(kind="rel.grant").order_by("id"))
    assert len(rows) == 2
    assert rows[0].target_repr == "blog/post:1#owner @ auth/user:alice"
    assert rows[1].target_repr == "blog/post:2#viewer @ auth/group:eng#member"


@pytest.mark.django_db(transaction=True)
def test_write_relationships_inside_rolled_back_txn_writes_no_audit():
    PermissionAuditEvent.objects.all().delete()
    tup = RelationshipTuple(
        resource=ObjectRef("blog/post", "rollback"),
        relation="owner",
        subject=SubjectRef.of("auth/user", "alice"),
    )

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with transaction.atomic():
            write_relationships([tup])
            raise _Boom("force rollback")

    # Neither relationship row nor audit row landed.
    assert PermissionAuditEvent.objects.filter(kind="rel.grant").count() == 0


# ---------- delete_relationships emission ----------


@pytest.mark.django_db(transaction=True)
def test_delete_relationships_emits_revoke_per_matched_row():
    # Seed two rows under sudo (so the audit-write-without-actor path is
    # exercised; the GRANTs from this seed go into the audit table too).
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", "p1"),
                relation="owner",
                subject=SubjectRef.of("auth/user", "alice"),
            ),
            RelationshipTuple(
                resource=ObjectRef("blog/post", "p2"),
                relation="owner",
                subject=SubjectRef.of("auth/user", "alice"),
            ),
        ]
    )
    PermissionAuditEvent.objects.all().delete()  # narrow the assertion to the revoke

    with transaction.atomic():
        delete_relationships(
            RelationshipFilter(
                resource_type="blog/post",
                relation="owner",
                subject_type="auth/user",
                subject_id="alice",
            )
        )
        # Pre-commit — no revoke rows yet.
        assert PermissionAuditEvent.objects.filter(kind="rel.revoke").count() == 0

    rows = list(PermissionAuditEvent.objects.filter(kind="rel.revoke").order_by("id"))
    assert len(rows) == 2
    targets = sorted(r.target_repr for r in rows)
    assert targets == [
        "blog/post:p1#owner @ auth/user:alice",
        "blog/post:p2#owner @ auth/user:alice",
    ]


@pytest.mark.django_db(transaction=True)
def test_audit_target_repr_includes_caveat_name():
    """Grants/revokes with a non-empty caveat must render `with <caveat>`."""
    PermissionAuditEvent.objects.all().delete()
    caveated = RelationshipTuple(
        resource=ObjectRef("blog/post", "p-caveat"),
        relation="viewer",
        subject=SubjectRef.of("auth/user", "alice"),
        caveat_name="during_business_hours",
    )

    with transaction.atomic():
        write_relationships([caveated])
    with transaction.atomic():
        delete_relationships(
            RelationshipFilter(
                resource_type="blog/post",
                resource_id="p-caveat",
                relation="viewer",
                subject_type="auth/user",
                subject_id="alice",
                caveat_name="during_business_hours",
            )
        )

    grant_row = PermissionAuditEvent.objects.get(kind="rel.grant")
    revoke_row = PermissionAuditEvent.objects.get(kind="rel.revoke")
    assert grant_row.target_repr == (
        "blog/post:p-caveat#viewer @ auth/user:alice with during_business_hours"
    )
    assert revoke_row.target_repr == (
        "blog/post:p-caveat#viewer @ auth/user:alice with during_business_hours"
    )


# ---------- denial emission gating ----------


@pytest.mark.django_db
def test_denial_not_audited_by_default():
    """Default REBAC_AUDIT_DENIALS=False — denied saves do NOT emit audit rows."""
    from tests.testapp.models import Post

    # Create the post under sudo so we have an existing row.
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="hi")

    bob = SubjectRef.of("auth/user", "bob")
    PermissionAuditEvent.objects.all().delete()

    # Bob has no rights — save should be denied.
    post._rebac_actor = bob
    post.title = "rename"
    with pytest.raises(PermissionDenied):
        post.save()

    # No audit row written (denial gate is OFF by default).
    assert PermissionAuditEvent.objects.count() == 0


@pytest.mark.django_db
def test_denial_audited_when_setting_enabled(settings):
    """REBAC_AUDIT_DENIALS=True — denied saves DO emit a denial row."""
    from rebac.conf import app_settings as _app_settings
    from tests.testapp.models import Post

    settings.REBAC_AUDIT_DENIALS = True
    _app_settings.reset()  # bust the cached default

    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="hi")

    bob = SubjectRef.of("auth/user", "bob")
    PermissionAuditEvent.objects.all().delete()

    post._rebac_actor = bob
    post.title = "rename"
    with pytest.raises(PermissionDenied):
        post.save()

    rows = list(PermissionAuditEvent.objects.all())
    assert len(rows) == 1
    assert rows[0].actor_subject_type == "auth/user"
    assert rows[0].actor_subject_id == "bob"
    assert rows[0].reason.startswith("denied:")


# ---------- aemit (async) ----------


@pytest.mark.django_db(transaction=True)
def test_aemit_immediate_writes_via_acreate():
    """``aemit(defer_to_commit=False)`` awaits acreate on the loop."""
    import asyncio

    from rebac.audit import aemit

    PermissionAuditEvent.objects.filter(reason="aemit.immediate").delete()

    async def _run() -> None:
        await aemit(
            PermissionAuditEvent.KIND_SUDO_BYPASS,
            actor=SubjectRef.of("auth/user", "alice"),
            reason="aemit.immediate",
            defer_to_commit=False,
        )

    asyncio.run(_run())
    rows = list(PermissionAuditEvent.objects.filter(reason="aemit.immediate"))
    assert len(rows) == 1
    assert rows[0].actor_subject_id == "alice"
    assert rows[0].kind == PermissionAuditEvent.KIND_SUDO_BYPASS


@pytest.mark.django_db(transaction=True)
def test_aemit_defer_to_commit_registers_on_commit_hook():
    """``aemit(defer_to_commit=True)`` routes through ``transaction.on_commit``.

    Patching ``transaction.on_commit`` is the only way to *prove* the
    deferred path was taken — asserting the row exists wouldn't
    distinguish ``defer_to_commit=True`` from ``defer_to_commit=False``
    in autocommit mode (both produce a row eventually). Here we
    confirm the registration call shape and that the registered
    callback, when invoked, writes the expected row.
    """
    import asyncio
    from unittest.mock import patch

    from rebac.audit import aemit

    PermissionAuditEvent.objects.filter(reason="aemit.deferred").delete()

    captured: list[Callable[[], None]] = []

    def _fake_on_commit(func, using=None, robust=False):
        captured.append(func)

    async def _run() -> None:
        with patch("rebac.audit.transaction.on_commit", side_effect=_fake_on_commit):
            await aemit(
                PermissionAuditEvent.KIND_RELATIONSHIP_GRANT,
                actor=SubjectRef.of("auth/user", "bob"),
                reason="aemit.deferred",
                defer_to_commit=True,
            )

    asyncio.run(_run())

    # The deferred path called ``transaction.on_commit`` exactly once
    # with a 0-arity sync callable; nothing landed in the DB while the
    # patch was active.
    assert len(captured) == 1
    assert PermissionAuditEvent.objects.filter(reason="aemit.deferred").count() == 0

    # Invoking the registered callback writes the expected row,
    # proving the callback closure carries the correct kwargs.
    captured[0]()
    rows = list(PermissionAuditEvent.objects.filter(reason="aemit.deferred"))
    assert len(rows) == 1
    assert rows[0].actor_subject_id == "bob"
    assert rows[0].kind == PermissionAuditEvent.KIND_RELATIONSHIP_GRANT
