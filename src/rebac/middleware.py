"""ActorMiddleware — populates `current_actor()` from `request.user`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.test.signals import setting_changed

from .actors import (
    _current_actor,
    disable_accessible_cache,
    enable_accessible_cache,
    get_actor_resolver,
    sudo,
)
from .conf import app_settings


class ActorMiddleware:
    """Reads `request.user` (via the configured resolver) and sets the
    `current_actor()` ContextVar for the duration of the request. Also
    opens the per-request ``accessible()`` memoisation bracket and
    handles the superuser bypass.

    Add to MIDDLEWARE *after* `AuthenticationMiddleware`.

    Resolver
    --------

    The middleware calls ``REBAC_ACTOR_RESOLVER`` (default
    ``rebac.actors.default_resolver``) to translate the request into a
    :class:`SubjectRef`. The default resolver returns the canonical
    anonymous SubjectRef (``REBAC_ANONYMOUS_TYPE:*``) for any request
    whose ``user.is_authenticated`` is False, so downstream checks
    against ``permission read = ... + anonymous`` evaluate correctly
    without callers having to construct the subject.

    The resolver callable is cached on the middleware instance after
    first lookup and invalidated on Django's ``setting_changed`` signal
    (test ergonomics + runtime override safety).

    Per-request ``accessible()`` cache
    ----------------------------------

    The middleware brackets each request in
    ``enable_accessible_cache()`` / ``disable_accessible_cache()`` so a
    request that calls ``accessible(action, type)`` repeatedly only
    walks the relationship graph once per ``(subject, action, type)``
    triple. The cache rides on a ContextVar — same isolation
    guarantees as ``_current_actor``.

    Superuser bypass
    ----------------

    When ``REBAC_SUPERUSER_BYPASS`` and ``REBAC_ALLOW_SUDO`` are both
    True (the defaults) and the request user is an active superuser,
    the request runs inside a ``sudo(reason="superuser-bypass")``
    bracket. This mirrors the bypass that
    ``rebac.backends.auth.RebacBackend.has_perm`` already applies to
    ``user.has_perm(perm, obj)`` checks, but at the QuerySet layer:
    ``Model.objects.with_actor(superuser).filter(...)`` returns every
    row instead of ``accessible()``-scoped, matching the legacy
    contrib.auth contract that admin sees everything.

    Routing through the public ``sudo()`` context manager means each
    superuser request emits a ``KIND_SUDO_BYPASS`` audit row — that's
    the auditability cost of the elevated scope. Tenants that want to
    suppress the bypass (and therefore the audit volume) flip
    ``REBAC_SUPERUSER_BYPASS = False``; tenants that disable sudo
    globally (``REBAC_ALLOW_SUDO = False``) get neither bypass nor
    audit row, which is the right fail-closed behaviour.
    """

    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        self.get_response = get_response
        self._resolver: Callable[[Any], Any] | None = None
        # Drop the cached resolver when settings change so test
        # ``override_settings`` (and any runtime reconfiguration)
        # picks up the new ``REBAC_ACTOR_RESOLVER`` path on the next
        # request.
        setting_changed.connect(self._on_setting_changed)

    def _on_setting_changed(self, sender: Any, setting: str, **kwargs: Any) -> None:
        if setting == "REBAC_ACTOR_RESOLVER":
            self._resolver = None

    def _get_resolver(self) -> Callable[[Any], Any]:
        if self._resolver is None:
            self._resolver = get_actor_resolver()
        return self._resolver

    def __call__(self, request: Any) -> Any:
        resolver = self._get_resolver()
        actor_ref = resolver(request)
        actor_token = _current_actor.set(actor_ref)
        cache_token = enable_accessible_cache()
        user = getattr(request, "user", None)
        use_sudo = (
            app_settings.REBAC_SUPERUSER_BYPASS
            and app_settings.REBAC_ALLOW_SUDO
            and user is not None
            and getattr(user, "is_active", False)
            and getattr(user, "is_superuser", False)
        )
        try:
            if use_sudo:
                with sudo(reason="superuser-bypass"):
                    return self.get_response(request)
            return self.get_response(request)
        finally:
            # Teardown LIFO: cache first (innermost), then actor.
            disable_accessible_cache(cache_token)
            _current_actor.reset(actor_token)
