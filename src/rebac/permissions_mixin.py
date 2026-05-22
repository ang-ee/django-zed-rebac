"""``RebacPermissionsMixin`` — a slim drop-in for contrib.auth's
:class:`PermissionsMixin` that doesn't ship Permission / Group M2Ms.

REBAC replaces Django's permissions table wholesale: per-row grants
live in the ``rebac.Relationship`` table, not in
``auth.User.user_permissions`` or ``auth.Group.permissions``. Domain
projects that use REBAC as the only source of truth still need
``has_perm`` / ``has_perms`` / ``has_module_perms`` on their User
model — Django admin, DRF's permission classes, and template tags
all call those methods.

This mixin provides them. The methods walk
:setting:`AUTHENTICATION_BACKENDS` exactly the way contrib.auth's
``PermissionsMixin`` does, but it carries no M2M fields, so it can
ride on a custom user that subclasses :class:`AbstractBaseUser`
directly (instead of the heavier :class:`AbstractUser`) without
forcing ``django.contrib.auth`` into ``INSTALLED_APPS``.

Usage::

    from rebac.permissions_mixin import RebacPermissionsMixin
    from django.contrib.auth.base_user import AbstractBaseUser

    class User(AbstractBaseUser, RebacPermissionsMixin):
        ...

Pair with :class:`rebac.backends.auth.RebacBackend` (front-of-line in
``AUTHENTICATION_BACKENDS``) so ``user.has_perm(...)`` calls flow
through the engine without hitting the now-empty
``Permission`` / ``Group`` M2Ms.

Surface beyond the canonical contrib.auth methods:

- :meth:`get_user_permissions` / :meth:`get_group_permissions` /
  :meth:`get_all_permissions` aggregate codenames across every
  configured backend — same shape as contrib.auth.
- ``a*`` async siblings (``ahas_perm`` / ``aget_all_permissions`` /
  …) for Django 4.1+ async views and DRF async pipelines. Backends
  that don't ship the async sibling are skipped silently.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.contrib.auth import get_backends
from django.core.exceptions import PermissionDenied
from django.db import models


def _walk_backends(method_name: str, *args: Any, **kwargs: Any) -> bool:
    """Iterate ``AUTHENTICATION_BACKENDS`` and stop on first ``True``.

    A backend may raise :class:`PermissionDenied` to short-circuit the
    chain — same contract as ``django.contrib.auth.models._user_has_*``.
    Backends that don't define the method are skipped silently.
    Note that ``rebac.errors.PermissionDenied`` subclasses Django's,
    so a REBAC-raised denial short-circuits the chain too.

    Other exceptions (``OperationalError``, custom backend bugs)
    propagate **by design** — also matches contrib.auth. Don't add a
    broad ``except`` here without changing
    ``django.contrib.auth.models._user_has_perm`` first; the chain
    semantics need to stay consistent.
    """
    for backend in get_backends():
        method = getattr(backend, method_name, None)
        if method is None:
            continue
        try:
            if method(*args, **kwargs):
                return True
        except PermissionDenied:
            return False
    return False


async def _awalk_backends(method_name: str, *args: Any, **kwargs: Any) -> bool:
    """Async sibling of :func:`_walk_backends`.

    Backends that ship the ``a``-prefixed sibling get awaited; ones
    that don't are skipped. Same ``PermissionDenied`` short-circuit.
    """
    for backend in get_backends():
        method = getattr(backend, method_name, None)
        if method is None:
            continue
        try:
            if await method(*args, **kwargs):
                return True
        except PermissionDenied:
            return False
    return False


def _walk_get_permissions(user: Any, obj: Any, scope: str) -> set[str]:
    """Aggregate permission codenames from every backend.

    Mirrors ``django.contrib.auth.models._user_get_permissions``: do
    NOT catch ``PermissionDenied`` here. ``has_perm`` short-circuits
    on it (as the chain semantics require), but the *aggregator* must
    return the union from every backend that contributed before the
    raise — otherwise a denial from the first backend silently zeros
    out everything later backends would have offered.
    """
    seen: set[str] = set()
    method_name = f"get_{scope}_permissions"
    for backend in get_backends():
        fn = getattr(backend, method_name, None)
        if fn is None:
            continue
        seen.update(fn(user, obj))
    return seen


async def _awalk_get_permissions(user: Any, obj: Any, scope: str) -> set[str]:
    """Async sibling of :func:`_walk_get_permissions`."""
    seen: set[str] = set()
    method_name = f"aget_{scope}_permissions"
    for backend in get_backends():
        fn = getattr(backend, method_name, None)
        if fn is None:
            continue
        seen.update(await fn(user, obj))
    return seen


class RebacPermissionsMixin(models.Model):
    """Permission methods + ``is_superuser`` field, no M2Ms.

    Mirrors the public surface of
    :class:`django.contrib.auth.models.PermissionsMixin` minus the
    ``groups`` and ``user_permissions`` many-to-many relations. Pairs
    with :class:`rebac.backends.auth.RebacBackend` to route
    permission checks through the REBAC engine.

    Active superusers always pass; the ``REBAC_SUPERUSER_BYPASS``
    setting is honoured by the auth backend itself, so a project that
    flips the setting off can still keep this mixin — the engine
    simply receives the call and answers per relationship rows.
    """

    is_superuser = models.BooleanField(
        "superuser status",
        default=False,
        help_text=(
            "Designates that this user has all permissions without explicitly assigning them."
        ),
    )

    class Meta:
        abstract = True

    # ---------- Permission lookups ----------

    def get_user_permissions(self, obj: Any = None) -> set[str]:
        """Codenames granted to the user directly across all backends."""
        return _walk_get_permissions(self, obj, "user")

    def get_group_permissions(self, obj: Any = None) -> set[str]:
        """Codenames granted via the user's groups across all backends."""
        return _walk_get_permissions(self, obj, "group")

    def get_all_permissions(self, obj: Any = None) -> set[str]:
        """Union of user + group codenames across all backends."""
        return _walk_get_permissions(self, obj, "all")

    # ---------- has_perm / has_perms ----------

    def has_perm(self, perm: str, obj: Any = None) -> bool:
        """Return True if any backend grants ``perm`` (optionally on ``obj``).

        Active superusers short-circuit to True — matches contrib.auth
        so admin behaves identically for superuser sessions. Anyone
        else walks :setting:`AUTHENTICATION_BACKENDS`.
        """
        if self.is_active and self.is_superuser:
            return True
        return _walk_backends("has_perm", self, perm, obj)

    def has_perms(self, perm_list: Iterable[str], obj: Any = None) -> bool:
        """Return True iff every perm in ``perm_list`` is granted.

        ``perm_list`` must be iterable but not a bare string (matches
        contrib.auth's guard rail — a stringly-typed argument here is
        almost always a typo for ``has_perm``).
        """
        if not isinstance(perm_list, Iterable) or isinstance(perm_list, str):
            raise ValueError("perm_list must be an iterable of permissions.")
        return all(self.has_perm(perm, obj) for perm in perm_list)

    # ---------- has_module_perms ----------

    def has_module_perms(self, app_label: str) -> bool:
        """Return True if any backend grants any permission on ``app_label``.

        Used by the admin index to decide whether to render an app's
        section. Superusers bypass.
        """
        if self.is_active and self.is_superuser:
            return True
        return _walk_backends("has_module_perms", self, app_label)

    # ---------- Async siblings (Django 4.1+) ----------

    async def aget_user_permissions(self, obj: Any = None) -> set[str]:
        return await _awalk_get_permissions(self, obj, "user")

    async def aget_group_permissions(self, obj: Any = None) -> set[str]:
        return await _awalk_get_permissions(self, obj, "group")

    async def aget_all_permissions(self, obj: Any = None) -> set[str]:
        return await _awalk_get_permissions(self, obj, "all")

    async def ahas_perm(self, perm: str, obj: Any = None) -> bool:
        if self.is_active and self.is_superuser:
            return True
        return await _awalk_backends("ahas_perm", self, perm, obj)

    async def ahas_perms(self, perm_list: Iterable[str], obj: Any = None) -> bool:
        if not isinstance(perm_list, Iterable) or isinstance(perm_list, str):
            raise ValueError("perm_list must be an iterable of permissions.")
        for perm in perm_list:
            if not await self.ahas_perm(perm, obj):
                return False
        return True

    async def ahas_module_perms(self, app_label: str) -> bool:
        if self.is_active and self.is_superuser:
            return True
        return await _awalk_backends("ahas_module_perms", self, app_label)


__all__ = ["RebacPermissionsMixin"]
