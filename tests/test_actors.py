"""Tests for actor resolution + sudo / system_context."""

from __future__ import annotations

import pytest
from django.test import override_settings

from rebac import (
    ANONYMOUS_ACTOR,
    NoActorResolvedError,
    SubjectRef,
    actor_context,
    anonymous_actor,
    current_actor,
    is_anonymous_actor,
    rebac_subject,
    sudo,
    system_context,
    to_subject_ref,
)
from rebac.actors import current_sudo_reason, default_resolver, is_sudo
from rebac.errors import SudoNotAllowedError, SudoReasonRequiredError


def test_subject_ref_passes_through():
    s = SubjectRef.of("auth/user", "42")
    assert to_subject_ref(s) is s


@pytest.mark.django_db
def test_django_user_to_subject_ref():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    u = User.objects.create(username="alice", is_active=True)
    ref = to_subject_ref(u)
    assert ref.subject_type == "auth/user"
    assert ref.subject_id == str(u.pk)


@pytest.mark.django_db
def test_django_group_to_subject_ref():
    from django.contrib.auth.models import Group

    g = Group.objects.create(name="eng")
    ref = to_subject_ref(g)
    assert ref.subject_type == "auth/group"
    assert ref.optional_relation == "member"


def test_unknown_actor_raises():
    with pytest.raises(NoActorResolvedError):
        to_subject_ref(object())


def test_none_actor_raises():
    with pytest.raises(NoActorResolvedError):
        to_subject_ref(None)


def test_unauthenticated_user_instance_raises():
    """A user-model instance with ``is_authenticated == False`` must raise.

    Strict-by-default (CLAUDE.md § 3). The previous "defensive" downgrade to
    the anonymous actor masked the most common cause of this state — a
    programming bug like passing an unsaved User or a leftover fixture
    instance. ``AnonymousUser`` remains the explicit anonymous path
    (covered by ``test_anonymous_user_resolves_to_anonymous_subject``);
    arbitrary user instances with ``is_authenticated=False`` must fail loudly.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()

    # Build a minimally-faked user with is_authenticated=False; the field is
    # property-derived on the default User model so constructing an unsaved
    # instance with no pk gives is_authenticated=True. Subclass + override.
    class _UnauthUser(User):  # type: ignore[misc, valid-type]
        class Meta:
            proxy = True
            app_label = "auth"

        @property
        def is_authenticated(self) -> bool:  # type: ignore[override]
            return False

    with pytest.raises(NoActorResolvedError, match="is_authenticated=False"):
        to_subject_ref(_UnauthUser(username="ghost"))


# ---------- Anonymous actor ----------


def test_anonymous_actor_constant_uses_default_type():
    assert ANONYMOUS_ACTOR.subject_type == "auth/anonymous"
    assert ANONYMOUS_ACTOR.subject_id == "*"
    assert ANONYMOUS_ACTOR.optional_relation == ""


def test_anonymous_actor_function_reads_setting():
    assert anonymous_actor() == ANONYMOUS_ACTOR
    with override_settings(REBAC_ANONYMOUS_TYPE="anon/principal"):
        ref = anonymous_actor()
        assert ref.subject_type == "anon/principal"
        assert ref.subject_id == "*"


def test_is_anonymous_actor_recognises_canonical():
    assert is_anonymous_actor(ANONYMOUS_ACTOR)
    assert is_anonymous_actor(SubjectRef.of("auth/anonymous", "*"))


def test_is_anonymous_actor_rejects_users_and_none():
    assert not is_anonymous_actor(None)
    assert not is_anonymous_actor(SubjectRef.of("auth/user", "42"))
    assert not is_anonymous_actor(SubjectRef.of("auth/anonymous", "specific"))
    assert not is_anonymous_actor(SubjectRef.of("auth/anonymous", "*", "member"))


def test_is_anonymous_actor_honours_setting_override():
    custom = SubjectRef.of("anon/principal", "*")
    with override_settings(REBAC_ANONYMOUS_TYPE="anon/principal"):
        assert is_anonymous_actor(custom)
        # The default-typed singleton no longer matches when the
        # setting was changed.
        assert not is_anonymous_actor(ANONYMOUS_ACTOR)


def test_django_anonymous_user_resolves_to_anonymous_actor():
    from django.contrib.auth.models import AnonymousUser

    ref = to_subject_ref(AnonymousUser())
    assert is_anonymous_actor(ref)


class _FakeRequest:
    def __init__(self, user):
        self.user = user


def test_default_resolver_returns_anonymous_for_missing_user():
    assert default_resolver(_FakeRequest(user=None)) == ANONYMOUS_ACTOR


def test_default_resolver_returns_anonymous_for_unauth_user():
    from django.contrib.auth.models import AnonymousUser

    assert default_resolver(_FakeRequest(user=AnonymousUser())) == ANONYMOUS_ACTOR


@pytest.mark.django_db
def test_default_resolver_returns_user_ref_for_authenticated():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    alice = User.objects.create(username="alice", is_active=True)
    ref = default_resolver(_FakeRequest(user=alice))
    assert ref is not None
    assert ref.subject_type == "auth/user"
    assert ref.subject_id == str(alice.pk)


def test_rebac_subject_decorator_registers():
    @rebac_subject(type="auth/apikey", id_attr="public_id")
    class ApiKey:
        def __init__(self, public_id: str) -> None:
            self.public_id = public_id

    ref = to_subject_ref(ApiKey("abc"))
    assert ref.subject_type == "auth/apikey"
    assert ref.subject_id == "abc"


def test_actor_context_block_scope():
    assert current_actor() is None
    s = SubjectRef.of("auth/user", "1")
    with actor_context(s):
        assert current_actor() == s
    assert current_actor() is None


def test_sudo_requires_reason_when_strict():
    with pytest.raises(SudoReasonRequiredError):
        with sudo():
            pass


@pytest.mark.django_db
def test_sudo_with_reason_flips_flag():
    # `sudo()` now writes a PermissionAuditEvent on enter, so this test
    # needs DB access. Behaviour under test (the flag flip) is unchanged.
    assert not is_sudo()
    with sudo(reason="cron.test"):
        assert is_sudo()
        assert current_sudo_reason() == "cron.test"
    assert not is_sudo()


def test_sudo_denied_when_disabled():
    with override_settings(REBAC_ALLOW_SUDO=False):
        with pytest.raises(SudoNotAllowedError):
            with sudo(reason="request.path"):
                pass


@pytest.mark.django_db
def test_system_context_allowed_when_sudo_disabled():
    with override_settings(REBAC_ALLOW_SUDO=False):
        assert not is_sudo()
        with system_context(reason="asset.load"):
            assert is_sudo()
            assert current_sudo_reason() == "asset.load"
        assert not is_sudo()
