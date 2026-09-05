"""Per-field write enforcement (Part C of the C+J work plan).

When the schema declares ``permission write__<field> = ...``, the engine
must run that check in addition to the resource-level ``write`` for any
save / bulk update that touches that field. Fields without a per-field
permission inherit the resource-level ``write``.
"""

from __future__ import annotations

import pytest

from rebac import (
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    backend,
    sudo,
    write_relationships,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed

# ``write`` opens up to owners + editors; ``write__title`` narrows it back
# to owners only. ``body`` has no per-field gate, so it inherits ``write``.
SCHEMA_TEXT = """
definition auth/user {}
definition auth/group {
    relation member: auth/user
}
definition blog/folder {
    relation owner: auth/user
    relation parent: blog/folder
    permission read = owner + parent->read
    permission write = owner + parent->write
}
definition blog/post {
    relation owner: auth/user
    relation editor: auth/user
    relation viewer: auth/user | auth/group#member | auth/user:*
    relation folder: blog/folder
    permission read = owner + viewer + editor + folder->read
    permission write = owner + editor
    permission write__title = owner
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


@pytest.fixture
def alice(db):
    """Owner of the post."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username="alice", is_active=True)


@pytest.fixture
def bob(db):
    """Editor of the post (not owner)."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username="bob", is_active=True)


@pytest.fixture
def post(db):
    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        p = Post.objects.create(title="original title", body="original body")
    return p


def _grant(post_pk, user, relation):
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(post_pk)),
                relation=relation,
                subject=SubjectRef.of("auth/user", str(user.pk)),
            ),
        ]
    )


# ---------- single-instance saves ----------


def test_editor_can_update_ungated_field(alice, bob, post):
    """``body`` has no ``write__body`` declared → editor can change it."""
    from tests.testapp.models import Post

    _grant(post.pk, alice, "owner")
    _grant(post.pk, bob, "editor")

    instance = Post.objects.as_user(bob).get(pk=post.pk)
    instance.body = "edited by bob"
    instance.save()

    with sudo(reason="test.verify"):
        assert Post.objects.get(pk=post.pk).body == "edited by bob"


def test_editor_denied_on_gated_field(alice, bob, post):
    """``write__title = owner`` → editor cannot change title even though
    they pass the resource-level ``write`` check."""
    from tests.testapp.models import Post

    _grant(post.pk, alice, "owner")
    _grant(post.pk, bob, "editor")

    instance = Post.objects.as_user(bob).get(pk=post.pk)
    instance.title = "hijacked title"
    with pytest.raises(PermissionDenied) as excinfo:
        instance.save()
    # Error message must name the field so debugging is obvious.
    assert "title" in str(excinfo.value)
    assert "write__title" in str(excinfo.value)


def test_owner_can_update_gated_field(alice, post):
    """Owner satisfies both ``write`` and ``write__title``."""
    from tests.testapp.models import Post

    _grant(post.pk, alice, "owner")

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    instance.title = "owner-renamed"
    instance.save()

    with sudo(reason="test.verify"):
        assert Post.objects.get(pk=post.pk).title == "owner-renamed"


def test_editor_can_save_unchanged_title(alice, bob, post):
    """Editor saves the row without dirtying the gated field — allowed.

    Demonstrates the dirty-set heuristic: only fields that actually
    changed go through per-field gating.
    """
    from tests.testapp.models import Post

    _grant(post.pk, alice, "owner")
    _grant(post.pk, bob, "editor")

    instance = Post.objects.as_user(bob).get(pk=post.pk)
    # Touching only body — title untouched.
    instance.body = "body-only update"
    instance.save()

    with sudo(reason="test.verify"):
        assert Post.objects.get(pk=post.pk).title == "original title"


def test_editor_save_with_update_fields_only_body(alice, bob, post):
    """``save(update_fields=["body"])`` trusts the caller — title is not
    written, so its gate is not consulted even if the in-memory value
    differs.
    """
    from tests.testapp.models import Post

    _grant(post.pk, alice, "owner")
    _grant(post.pk, bob, "editor")

    instance = Post.objects.as_user(bob).get(pk=post.pk)
    instance.title = "stale in-memory only"  # not in update_fields, won't write
    instance.body = "edited via update_fields"
    instance.save(update_fields=["body"])

    with sudo(reason="test.verify"):
        row = Post.objects.get(pk=post.pk)
        assert row.title == "original title"
        assert row.body == "edited via update_fields"


def test_editor_save_with_update_fields_includes_gated(alice, bob, post):
    """``save(update_fields=["title"])`` triggers ``write__title`` —
    editor denied.
    """
    from tests.testapp.models import Post

    _grant(post.pk, alice, "owner")
    _grant(post.pk, bob, "editor")

    instance = Post.objects.as_user(bob).get(pk=post.pk)
    instance.title = "hijack via update_fields"
    with pytest.raises(PermissionDenied):
        instance.save(update_fields=["title"])


def test_handbuilt_instance_with_pinned_actor(alice, post):
    """A hand-built instance with ``_rebac_actor`` pinned saves through
    the create path (no per-field gate fires on INSERT).
    """
    from tests.testapp.models import Post

    instance = Post(title="from-thin-air", body="b")
    instance._rebac_actor = SubjectRef.of("auth/user", str(alice.pk))
    # This schema's create gate requires ownership of an existing resource.
    # Grant it explicitly so the pinned actor can authorize the insert.
    _grant(post.pk, alice, "owner")
    instance.save()
    assert instance.pk is not None


# ---------- bulk updates ----------


def test_bulk_update_ungated_field_allowed(alice, bob, post):
    """``qs.update(body=...)`` only needs the resource-level write."""
    from tests.testapp.models import Post

    _grant(post.pk, alice, "owner")
    _grant(post.pk, bob, "editor")

    n = Post.objects.as_user(bob).filter(pk=post.pk).update(body="bulk-body")
    assert n == 1
    with sudo(reason="test.verify"):
        assert Post.objects.get(pk=post.pk).body == "bulk-body"


def test_bulk_update_gated_field_denied(alice, bob, post):
    """``qs.update(title=...)`` triggers ``write__title`` — editor
    denied (all-or-nothing semantics)."""
    from tests.testapp.models import Post

    _grant(post.pk, alice, "owner")
    _grant(post.pk, bob, "editor")

    qs = Post.objects.as_user(bob).filter(pk=post.pk)
    with pytest.raises(PermissionDenied) as excinfo:
        qs.update(title="bulk-hijack")
    assert "write__title" in str(excinfo.value)
    # Row left untouched.
    with sudo(reason="test.verify"):
        assert Post.objects.get(pk=post.pk).title == "original title"


def test_bulk_update_gated_field_owner_allowed(alice, post):
    """Owner passes both the resource-level write and ``write__title``."""
    from tests.testapp.models import Post

    _grant(post.pk, alice, "owner")

    n = Post.objects.as_user(alice).filter(pk=post.pk).update(title="owner-bulk")
    assert n == 1
    with sudo(reason="test.verify"):
        assert Post.objects.get(pk=post.pk).title == "owner-bulk"


def test_field_gated_actions_matches_existing_write_gate_discovery():
    """Shared schema accessor must preserve the old ``write__`` discovery."""
    from rebac.schema.walker import field_gated_actions

    schema = parse_zed(SCHEMA_TEXT)
    definition = schema.get_definition("blog/post")
    assert definition is not None
    assert field_gated_actions(definition, "write") == frozenset({"write__title"})
