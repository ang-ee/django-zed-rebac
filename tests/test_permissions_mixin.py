"""Tests for ``rebac.permissions_mixin.RebacPermissionsMixin``.

The mixin must:

- Expose ``has_perm`` / ``has_perms`` / ``has_module_perms``.
- Walk :setting:`AUTHENTICATION_BACKENDS`, returning True on first
  backend that grants.
- Honour :class:`PermissionDenied` raised by a backend (short-circuit
  to False).
- Skip backends that don't define a given method (NOT every backend
  has ``has_module_perms``).
- Bypass for active superusers.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied
from django.test import override_settings

from rebac import RebacPermissionsMixin


class _FakeUser:
    """Plain Python user — exercises the methods without a Django row."""

    def __init__(self, *, is_active: bool = True, is_superuser: bool = False) -> None:
        self.is_active = is_active
        self.is_superuser = is_superuser

    # Bound methods picked up by the mixin via descriptor lookup.
    has_perm = RebacPermissionsMixin.has_perm
    has_perms = RebacPermissionsMixin.has_perms
    has_module_perms = RebacPermissionsMixin.has_module_perms


# ---------- Backend stubs ----------


class _AlwaysFalseBackend:
    def has_perm(self, user, perm, obj=None):
        return False

    def has_module_perms(self, user, app_label):
        return False


class _GrantBackend:
    def has_perm(self, user, perm, obj=None):
        return True

    def has_module_perms(self, user, app_label):
        return True


class _RaisingBackend:
    def has_perm(self, user, perm, obj=None):
        raise PermissionDenied("explicit deny")

    def has_module_perms(self, user, app_label):
        raise PermissionDenied("explicit deny")


class _NoMethodsBackend:
    """Models a backend that only does authenticate; should be skipped."""

    def authenticate(self, request, **credentials):
        return None


# ---------- has_perm ----------


@override_settings(AUTHENTICATION_BACKENDS=["tests.test_permissions_mixin._AlwaysFalseBackend"])
def test_has_perm_returns_false_when_no_backend_grants():
    user = _FakeUser()
    assert user.has_perm("any.perm") is False


@override_settings(
    AUTHENTICATION_BACKENDS=[
        "tests.test_permissions_mixin._AlwaysFalseBackend",
        "tests.test_permissions_mixin._GrantBackend",
    ]
)
def test_has_perm_returns_true_when_any_backend_grants():
    user = _FakeUser()
    assert user.has_perm("any.perm") is True


@override_settings(
    AUTHENTICATION_BACKENDS=[
        "tests.test_permissions_mixin._RaisingBackend",
        "tests.test_permissions_mixin._GrantBackend",
    ]
)
def test_has_perm_short_circuits_on_permission_denied():
    """A backend raising PermissionDenied wins — the chain stops there
    even if a later backend would grant. Matches contrib.auth's
    documented contract."""
    user = _FakeUser()
    assert user.has_perm("any.perm") is False


class _RebacDenyingBackend:
    """Raises ``rebac.errors.PermissionDenied`` (subclass of Django's).

    Verifies that a REBAC-flavored denial still short-circuits the
    chain — important because real REBAC backends raise the
    rebac-namespaced exception, not the django.core one directly.
    """

    def has_perm(self, user, perm, obj=None):
        from rebac.errors import PermissionDenied as RebacPermissionDenied

        raise RebacPermissionDenied("rebac says no")


@override_settings(
    AUTHENTICATION_BACKENDS=[
        "tests.test_permissions_mixin._RebacDenyingBackend",
        "tests.test_permissions_mixin._GrantBackend",
    ]
)
def test_has_perm_short_circuits_on_rebac_permission_denied():
    user = _FakeUser()
    assert user.has_perm("any.perm") is False


@override_settings(
    AUTHENTICATION_BACKENDS=[
        "tests.test_permissions_mixin._NoMethodsBackend",
        "tests.test_permissions_mixin._GrantBackend",
    ]
)
def test_has_perm_skips_backends_without_method():
    user = _FakeUser()
    assert user.has_perm("any.perm") is True


def test_has_perm_active_superuser_bypasses():
    """Active superusers always get True without consulting backends.
    Matches contrib.auth.models.PermissionsMixin.has_perm."""
    user = _FakeUser(is_active=True, is_superuser=True)
    # Even without any backends configured, superuser wins.
    with override_settings(AUTHENTICATION_BACKENDS=[]):
        assert user.has_perm("any.perm") is True


@override_settings(AUTHENTICATION_BACKENDS=["tests.test_permissions_mixin._GrantBackend"])
def test_has_perm_inactive_superuser_still_walks_backends():
    """Inactive users skip the superuser fast-path and let the backend
    chain answer — the backend is responsible for re-checking
    ``is_active`` (RebacBackend does)."""
    user = _FakeUser(is_active=False, is_superuser=True)
    # _GrantBackend doesn't check is_active so it returns True; what
    # we're verifying is that the mixin DIDN'T bypass on an inactive
    # superuser.
    assert user.has_perm("any.perm") is True


# ---------- has_perms ----------


@override_settings(AUTHENTICATION_BACKENDS=["tests.test_permissions_mixin._GrantBackend"])
def test_has_perms_true_when_all_granted():
    user = _FakeUser()
    assert user.has_perms(["a", "b", "c"]) is True


@override_settings(AUTHENTICATION_BACKENDS=["tests.test_permissions_mixin._AlwaysFalseBackend"])
def test_has_perms_false_when_any_denied():
    user = _FakeUser()
    assert user.has_perms(["a", "b"]) is False


def test_has_perms_rejects_string_argument():
    """contrib.auth's guard rail — a bare string is almost always a
    typo for has_perm."""
    user = _FakeUser()
    with pytest.raises(ValueError, match="iterable"):
        user.has_perms("auth.view_user")


# ---------- has_module_perms ----------


@override_settings(AUTHENTICATION_BACKENDS=["tests.test_permissions_mixin._GrantBackend"])
def test_has_module_perms_true_when_backend_grants():
    user = _FakeUser()
    assert user.has_module_perms("any_app") is True


@override_settings(AUTHENTICATION_BACKENDS=["tests.test_permissions_mixin._AlwaysFalseBackend"])
def test_has_module_perms_false_when_no_backend_grants():
    user = _FakeUser()
    assert user.has_module_perms("any_app") is False


@override_settings(
    AUTHENTICATION_BACKENDS=[
        "tests.test_permissions_mixin._RaisingBackend",
        "tests.test_permissions_mixin._GrantBackend",
    ]
)
def test_has_module_perms_short_circuits_on_permission_denied():
    user = _FakeUser()
    assert user.has_module_perms("any_app") is False


def test_has_module_perms_active_superuser_bypasses():
    user = _FakeUser(is_active=True, is_superuser=True)
    with override_settings(AUTHENTICATION_BACKENDS=[]):
        assert user.has_module_perms("any_app") is True
