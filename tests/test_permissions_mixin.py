"""Tests for ``rebac.permissions_mixin.RebacPermissionsMixin``.

The mixin must:

- Expose ``has_perm`` / ``has_perms`` / ``has_module_perms``.
- Walk :setting:`AUTHENTICATION_BACKENDS`, returning True on first
  backend that grants.
- Honour :class:`PermissionDenied` raised by a backend (short-circuit
  to False).
- Skip backends that don't define a given method (NOT every backend
  has ``has_module_perms``).
- Delegate active-superuser policy to the configured backends.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar, cast

import pytest
from django.core.exceptions import PermissionDenied
from django.test import override_settings

from rebac import RebacPermissionsMixin


class _FakeUser:
    """Plain Python user — exercises the methods without a Django row.

    The mixin methods only read ``is_active`` / ``is_superuser`` and walk
    the configured backends, so a duck-typed object stands in fine. We
    delegate through ``RebacPermissionsMixin`` rather than subclassing it
    (it's an abstract Django model that can't be instantiated plainly);
    the ``self`` cast is the cost of that deliberate duck-typing.
    """

    def __init__(self, *, is_active: bool = True, is_superuser: bool = False) -> None:
        self.is_active = is_active
        self.is_superuser = is_superuser

    def has_perm(self, perm: str, obj: Any = None) -> bool:
        return RebacPermissionsMixin.has_perm(cast(RebacPermissionsMixin, self), perm, obj)

    def has_perms(self, perm_list: Iterable[str], obj: Any = None) -> bool:
        return RebacPermissionsMixin.has_perms(cast(RebacPermissionsMixin, self), perm_list, obj)

    def has_module_perms(self, app_label: str) -> bool:
        return RebacPermissionsMixin.has_module_perms(cast(RebacPermissionsMixin, self), app_label)


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


@override_settings(AUTHENTICATION_BACKENDS=["tests.test_permissions_mixin._AlwaysFalseBackend"])
def test_has_perm_active_superuser_is_answered_by_backends():
    """The mixin adds no superuser shortcut: the backend chain owns that policy."""
    user = _FakeUser(is_active=True, is_superuser=True)
    assert user.has_perm("any.perm") is False


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


@override_settings(AUTHENTICATION_BACKENDS=["tests.test_permissions_mixin._AlwaysFalseBackend"])
def test_has_module_perms_active_superuser_is_answered_by_backends():
    user = _FakeUser(is_active=True, is_superuser=True)
    assert user.has_module_perms("any_app") is False


# ---------- get_*_permissions ----------


class _CodenameBackend:
    """Contributes codenames; records the object each lookup received."""

    seen_objects: ClassVar[list[Any]] = []

    def get_user_permissions(self, user, obj=None):
        self.seen_objects.append(obj)
        return {"blog.view_post"}

    def get_group_permissions(self, user, obj=None):
        self.seen_objects.append(obj)
        return {"blog.change_post"}

    def get_all_permissions(self, user, obj=None):
        self.seen_objects.append(obj)
        return {"blog.view_post", "blog.change_post"}

    async def aget_user_permissions(self, user, obj=None):
        return self.get_user_permissions(user, obj)

    async def aget_group_permissions(self, user, obj=None):
        return self.get_group_permissions(user, obj)

    async def aget_all_permissions(self, user, obj=None):
        return self.get_all_permissions(user, obj)


class _OtherCodenameBackend:
    def get_user_permissions(self, user, obj=None):
        return {"drive.view_file"}

    def get_group_permissions(self, user, obj=None):
        return set()

    def get_all_permissions(self, user, obj=None):
        return {"drive.view_file"}

    async def aget_user_permissions(self, user, obj=None):
        return {"drive.view_file"}

    async def aget_group_permissions(self, user, obj=None):
        return set()

    async def aget_all_permissions(self, user, obj=None):
        return {"drive.view_file"}


_CODENAME_BACKENDS = [
    "tests.test_permissions_mixin._CodenameBackend",
    "tests.test_permissions_mixin._NoMethodsBackend",
    "tests.test_permissions_mixin._OtherCodenameBackend",
]


def _mixin(user: Any) -> RebacPermissionsMixin:
    return cast(RebacPermissionsMixin, user)


@override_settings(AUTHENTICATION_BACKENDS=_CODENAME_BACKENDS)
def test_get_permissions_union_every_backend_and_skip_the_rest():
    user = _mixin(_FakeUser())
    assert RebacPermissionsMixin.get_user_permissions(user) == {
        "blog.view_post",
        "drive.view_file",
    }
    assert RebacPermissionsMixin.get_group_permissions(user) == {"blog.change_post"}
    assert RebacPermissionsMixin.get_all_permissions(user) == {
        "blog.view_post",
        "blog.change_post",
        "drive.view_file",
    }


@override_settings(AUTHENTICATION_BACKENDS=["tests.test_permissions_mixin._CodenameBackend"])
def test_get_permissions_pass_the_object_through():
    _CodenameBackend.seen_objects = []
    marker = object()
    user = _mixin(_FakeUser())
    RebacPermissionsMixin.get_user_permissions(user, marker)
    RebacPermissionsMixin.get_group_permissions(user, marker)
    RebacPermissionsMixin.get_all_permissions(user, marker)
    assert _CodenameBackend.seen_objects == [marker, marker, marker]


@override_settings(AUTHENTICATION_BACKENDS=_CODENAME_BACKENDS)
def test_async_get_permissions_match_sync():
    from asgiref.sync import async_to_sync

    user = _mixin(_FakeUser())
    for scope in ("user", "group", "all"):
        sync = getattr(RebacPermissionsMixin, f"get_{scope}_permissions")(user)
        coroutine = getattr(RebacPermissionsMixin, f"aget_{scope}_permissions")
        assert async_to_sync(coroutine)(user) == sync


# ---------- async siblings ----------


class _AsyncGrantBackend:
    async def ahas_perm(self, user, perm, obj=None):
        return perm != "blog.delete_post"

    async def ahas_module_perms(self, user, app_label):
        return app_label == "blog"


class _AsyncRaisingBackend:
    async def ahas_perm(self, user, perm, obj=None):
        raise PermissionDenied("explicit deny")

    async def ahas_module_perms(self, user, app_label):
        raise PermissionDenied("explicit deny")


class _AsyncFakeUser(_FakeUser):
    async def ahas_perm(self, perm: str, obj: Any = None) -> bool:
        return await RebacPermissionsMixin.ahas_perm(_mixin(self), perm, obj)

    async def ahas_perms(self, perm_list: Iterable[str], obj: Any = None) -> bool:
        return await RebacPermissionsMixin.ahas_perms(_mixin(self), perm_list, obj)

    async def ahas_module_perms(self, app_label: str) -> bool:
        return await RebacPermissionsMixin.ahas_module_perms(_mixin(self), app_label)


def _run(coroutine_function, *args):
    from asgiref.sync import async_to_sync

    return async_to_sync(coroutine_function)(*args)


@override_settings(
    AUTHENTICATION_BACKENDS=[
        "tests.test_permissions_mixin._NoMethodsBackend",
        "tests.test_permissions_mixin._AsyncGrantBackend",
    ]
)
def test_async_has_perm_walks_async_backends():
    user = _AsyncFakeUser()
    assert _run(user.ahas_perm, "blog.view_post") is True
    assert _run(user.ahas_perm, "blog.delete_post") is False
    assert _run(user.ahas_perms, ["blog.view_post", "blog.change_post"]) is True
    assert _run(user.ahas_perms, ["blog.view_post", "blog.delete_post"]) is False
    assert _run(user.ahas_module_perms, "blog") is True
    assert _run(user.ahas_module_perms, "drive") is False


@override_settings(
    AUTHENTICATION_BACKENDS=[
        "tests.test_permissions_mixin._AsyncRaisingBackend",
        "tests.test_permissions_mixin._AsyncGrantBackend",
    ]
)
def test_async_has_perm_short_circuits_on_permission_denied():
    user = _AsyncFakeUser()
    assert _run(user.ahas_perm, "blog.view_post") is False
    assert _run(user.ahas_module_perms, "blog") is False


def test_async_has_perms_rejects_string_argument():
    user = _AsyncFakeUser()
    with pytest.raises(ValueError, match="iterable"):
        _run(user.ahas_perms, "auth.view_user")


@override_settings(AUTHENTICATION_BACKENDS=["tests.test_permissions_mixin._AsyncGrantBackend"])
def test_async_active_superuser_is_answered_by_backends():
    user = _AsyncFakeUser(is_active=True, is_superuser=True)
    assert _run(user.ahas_perm, "blog.delete_post") is False


_REBAC_BACKEND_ASYNC_REASON = (
    "RebacBackend (src/rebac/backends/auth.py:48) defines no ahas_perm/ahas_module_perms, "
    "so the mixin's async walk skips it and async checks never reach REBAC."
)


@pytest.fixture
def owned_post(db):
    from django.contrib.auth import get_user_model

    from rebac import ObjectRef, RelationshipTuple, SubjectRef, backend, sudo
    from rebac.schema import parse_zed
    from tests.backend_setup import atomic_source_write, install_schema
    from tests.testapp.models import Post

    install_schema(
        backend(),
        parse_zed(
            """
            definition auth/user {}
            definition blog/post {
                relation owner: auth/user
                permission read = owner
                permission write = owner
            }
            """
        ),
    )
    user = atomic_source_write(get_user_model().objects.create_user, username="mixin-owner")
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="Owned")
    backend().write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(post.pk)),
                relation="owner",
                subject=SubjectRef.of("auth/user", str(user.pk)),
            )
        ]
    )
    return user, post


@pytest.mark.xfail(strict=True, reason=_REBAC_BACKEND_ASYNC_REASON)
@override_settings(AUTHENTICATION_BACKENDS=["rebac.backends.auth.RebacBackend"])
@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("has_perm", ("testapp.change_post", "post")),
        ("has_perms", (["testapp.view_post", "testapp.change_post"], "post")),
        ("has_module_perms", ("testapp",)),
    ],
)
def test_async_mixin_methods_reach_rebac_backend(owned_post, method, args):
    user, post = owned_post
    args = tuple(post if arg == "post" else arg for arg in args)
    sync_answer = getattr(RebacPermissionsMixin, method)(user, *args)
    assert sync_answer is True
    assert _run(getattr(RebacPermissionsMixin, f"a{method}"), user, *args) is sync_answer
