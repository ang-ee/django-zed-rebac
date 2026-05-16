"""Tests for actor resolution + sudo / system_context."""

from __future__ import annotations

import pytest
from django.test import override_settings

from rebac import (
    NoActorResolvedError,
    SubjectRef,
    actor_context,
    current_actor,
    rebac_subject,
    sudo,
    system_context,
    to_subject_ref,
)
from rebac.actors import current_sudo_reason, is_sudo
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


def test_anonymous_or_unknown_raises():
    with pytest.raises(NoActorResolvedError):
        to_subject_ref(object())


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
