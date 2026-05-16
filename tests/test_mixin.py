"""End-to-end tests of RebacMixin scoping."""

from __future__ import annotations

import pytest

from rebac import (
    MissingActorError,
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
    relation viewer: auth/user | auth/group#member | auth/user:*
    relation folder: blog/folder
    permission read = owner + viewer + folder->read
    permission credential_lookup = owner
    permission write = owner
    permission delete = owner
    permission create = owner
}
"""


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA_TEXT))  # type: ignore[attr-defined]
    yield
    reset_backend()


@pytest.fixture
def alice(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username="alice", is_active=True)


@pytest.fixture
def bob(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username="bob", is_active=True)


@pytest.fixture
def post(db):
    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        p = Post.objects.create(title="hello")
    return p


def _grant_owner(user, post):
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(post.pk)),
                relation="owner",
                subject=SubjectRef.of("auth/user", str(user.pk)),
            ),
        ]
    )


def test_strict_mode_raises_without_actor(post):
    from tests.testapp.models import Post

    with pytest.raises(MissingActorError):
        list(Post.objects.all())


def test_as_user_scopes_to_owner(alice, bob, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post

    assert list(Post.objects.as_user(alice).values_list("pk", flat=True)) == [post.pk]
    assert list(Post.objects.as_user(bob)) == []


def test_with_actor_accepts_subject_ref(alice, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post

    ref = SubjectRef.of("auth/user", str(alice.pk))
    assert list(Post.objects.with_actor(ref).values_list("pk", flat=True)) == [post.pk]


def test_save_blocked_for_non_owner(alice, bob, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post

    # Bob loads the post via sudo, then tries to save under his own actor.
    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    instance._rebac_actor = SubjectRef.of("auth/user", str(bob.pk))
    instance.title = "hijacked"
    with pytest.raises(PermissionDenied):
        instance.save()


def test_save_allowed_for_owner(alice, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance._rebac_actor is not None
    instance.title = "renamed"
    instance.save()


def test_sudo_bypass_allows_create(alice):
    from tests.testapp.models import Post

    with sudo(reason="test.sudo"):
        p = Post.objects.create(title="created in sudo")
        assert p.pk is not None


def test_count_respects_scope(alice, bob, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post

    assert Post.objects.as_user(alice).count() == 1
    assert Post.objects.as_user(bob).count() == 0


def test_sudo_queryset_returns_all(alice, bob, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post

    qs = Post.objects.sudo(reason="test.sudo")
    assert qs.count() == 1


def test_with_action_changes_queryset_read_scope(alice, bob, post):
    _grant_owner(alice, post)
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(post.pk)),
                relation="viewer",
                subject=SubjectRef.of("auth/user", str(bob.pk)),
            ),
        ]
    )
    from tests.testapp.models import Post

    assert list(Post.objects.as_user(bob).values_list("pk", flat=True)) == [post.pk]
    assert list(
        Post.objects.with_action("credential_lookup")
        .as_user(bob)
        .values_list("pk", flat=True)
    ) == []
    assert list(
        Post.objects.with_action("credential_lookup")
        .as_user(alice)
        .values_list("pk", flat=True)
    ) == [post.pk]


# ---------------------------------------------------------------------------
# Instance-level surface: with_actor / sudo / is_sudo / check_access / has_access
# ---------------------------------------------------------------------------


def test_instance_with_actor_save_allowed_for_owner(alice, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    instance.with_actor(alice)
    instance.title = "renamed via instance.with_actor"
    instance.save()


def test_instance_with_actor_save_blocked_for_non_owner(alice, bob, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    instance.with_actor(bob)
    instance.title = "hijacked"
    with pytest.raises(PermissionDenied):
        instance.save()


def test_instance_with_actor_returns_self(alice, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    assert instance.with_actor(alice) is instance


def test_handbuilt_instance_with_actor_routes_through_check(alice):
    """A hand-built instance's save() routes through REBAC once .with_actor() binds.

    The point: before this PR the only way to attach an actor to an
    in-memory instance was to load it through a scoped queryset. With
    instance-level ``with_actor``, ``Post(...).with_actor(u).save()``
    runs the same gate. Alice has no `owner` rows yet so the create
    check denies — the test confirms the gate fires (rather than the
    save going through unchecked OR raising MissingActorError).
    """
    from tests.testapp.models import Post

    p = Post(title="hand-built")
    p.with_actor(alice)
    with pytest.raises(PermissionDenied):
        p.save()


def test_instance_with_actor_outranks_ambient_actor(alice, bob, post):
    """Per-instance pinned actor must strictly outrank ambient current_actor().

    CLAUDE.md § 5: explicit local scope never gets overridden by ambient
    context. Bob has no grants; Alice owns the post. With Bob pinned on
    the instance and Alice in the ambient ContextVar, the save must use
    Bob and deny.
    """
    from rebac import actor_context
    from tests.testapp.models import Post

    _grant_owner(alice, post)
    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    instance.with_actor(bob)
    with actor_context(alice):
        instance.title = "hijacked"
        with pytest.raises(PermissionDenied):
            instance.save()


def test_instance_check_access_ambient_sudo_overrides_pinned_actor(bob, post):
    """Ambient `with sudo():` overrides a pinned actor — same precedence as queryset.

    Documents and locks the precedence rule on the new instance API:
    ambient sudo wins. Bob has no grants; without sudo `check_access`
    would deny. Inside `with sudo(...)` the same call returns HAS.
    """
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    instance.with_actor(bob)
    assert instance.check_access("read").allowed is False
    with sudo(reason="test.ambient"):
        assert instance.check_access("read").allowed is True


def test_instance_sudo_bypasses_save(alice, post):
    """`instance.sudo(reason=...).save()` skips the REBAC check."""
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    # Bind nobody as actor; alice has no grants — without sudo this would deny.
    instance.title = "via instance.sudo"
    instance.sudo(reason="test.instance_sudo")
    assert instance.is_sudo() is True
    instance.save()


def test_instance_sudo_bypasses_delete(alice, post):
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    instance.sudo(reason="test.instance_delete")
    instance.delete()
    with sudo(reason="test.assert"):
        assert not Post.objects.filter(pk=post.pk).exists()


def test_instance_sudo_requires_reason_when_required(post, settings):
    from rebac.errors import SudoReasonRequiredError
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    with pytest.raises(SudoReasonRequiredError):
        instance.sudo(reason="")


def test_instance_with_actor_clears_prior_sudo(alice, post):
    """Binding an actor wipes a prior per-instance sudo (mutually exclusive)."""
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    instance.sudo(reason="test.first")
    assert instance.is_sudo() is True
    instance.with_actor(alice)
    assert instance.is_sudo() is False


def test_instance_check_access_owner_has_read(alice, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    result = instance.check_access("read")
    assert result.allowed is True
    assert bool(result) is True


def test_instance_check_access_non_owner_denied(alice, bob, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    instance.with_actor(bob)
    result = instance.check_access("read")
    assert result.allowed is False


def test_instance_has_access_boolean_shape(alice, bob, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    assert instance.with_actor(alice).has_access("read") is True
    assert instance.with_actor(bob).has_access("read") is False


def test_instance_check_access_under_sudo_short_circuits(alice, post):
    """`instance.sudo(...)` makes `check_access` answer HAS without consulting the backend."""
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    instance.sudo(reason="test.check")
    result = instance.check_access("delete")  # alice owns nothing yet
    assert result.allowed is True


def test_instance_as_user_shorthand_equivalent_to_with_actor(alice, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    instance.as_user(alice)
    assert instance.actor() == SubjectRef.of("auth/user", str(alice.pk))
    assert instance.has_access("read") is True


def test_instance_actor_returns_pinned_subject(alice, post):
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    assert instance.actor() is None
    instance.with_actor(alice)
    assert instance.actor() == SubjectRef.of("auth/user", str(alice.pk))


def test_instance_check_access_strict_mode_raises_without_actor(post):
    from tests.testapp.models import Post

    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    # Loaded under sudo: _rebac_actor stays None. A subsequent check
    # outside any sudo / with_actor scope must raise under strict mode.
    instance._rebac_actor = None
    with pytest.raises(MissingActorError):
        instance.check_access("read")
