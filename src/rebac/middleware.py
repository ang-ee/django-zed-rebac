"""ActorMiddleware — populates `current_actor()` from `request.user`.

Per proposal 0002 also brackets each request with an evaluator scope
(per-request permission check cache) and a Zookie scope (write-then-read
freshness propagation, with optional cross-request transport via header
or session).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .actors import _current_actor, get_actor_resolver, sudo
from .conf import app_settings
from .consistency import current_zookie, zookie_scope
from .evaluator import evaluator_scope
from .types import Zookie


class ActorMiddleware:
    """Reads `request.user` (via the configured resolver) and sets the
    `current_actor()` ContextVar for the duration of the request. Also
    opens per-request evaluator + Zookie scopes, handles the superuser
    bypass, and (opt-in) rehydrates / persists a Zookie via header or
    session transport.

    Add to MIDDLEWARE *after* the middleware named by
    ``REBAC_AUTHENTICATION_MIDDLEWARE``.

    Resolver
    --------

    The middleware calls ``REBAC_ACTOR_RESOLVER`` (default
    ``rebac.actors.default_resolver``) to translate the request into a
    :class:`SubjectRef`. The default resolver returns the canonical
    anonymous SubjectRef (``REBAC_ANONYMOUS_TYPE:*``) for any request
    whose ``user.is_authenticated`` is False, so downstream checks
    against ``permission read = ... + anonymous`` evaluate correctly
    without callers having to construct the subject.

    The resolver is looked up per request via ``get_actor_resolver()``.
    The cost is one ``sys.modules`` lookup + ``getattr`` per request —
    cheap enough that adding signal-based cache invalidation is more
    complexity than the saving justifies. ``app_settings`` already
    invalidates its own cache on ``setting_changed``, so ``override_settings``
    in tests works without any extra plumbing here.

    Per-request evaluator + Zookie
    -------------------------------

    Each request is bracketed in :func:`rebac.evaluator.evaluator_scope`
    (caches ``check_access`` + ``accessible`` results per
    ``(subject, action, resource_or_type, context)`` key) and
    :func:`rebac.consistency.zookie_scope` (records post-write Zookies
    so subsequent reads upgrade to ``at_least_as_fresh``). Both ride on
    ContextVars; async tasks and Celery workers each see their own
    slot.

    Cross-request Zookie transport
    ------------------------------

    Controlled by ``REBAC_ZOOKIE_TRANSPORT``:

    - ``"none"`` (default) — single-request scope only.
    - ``"header"`` — request reads ``REBAC_ZOOKIE_HEADER_NAME`` (default
      ``X-Rebac-Zookie``) and seeds the scope; response writes the
      latest Zookie back under the same header. The natural SPA / JWT
      fit; both client and server are stateless.
    - ``"session"`` — write persists into ``request.session`` under
      ``REBAC_ZOOKIE_SESSION_KEY`` (default ``_rebac_zookie``);
      subsequent requests in the same session rehydrate. Requires
      ``django.contrib.sessions`` (system check ``rebac.W006``).

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

    def __call__(self, request: Any) -> Any:
        resolver = get_actor_resolver()
        actor_ref = resolver(request)
        actor_token = _current_actor.set(actor_ref)
        initial_zookie = self._rehydrate_zookie(request)
        user = getattr(request, "user", None)
        use_sudo = (
            app_settings.REBAC_SUPERUSER_BYPASS
            and app_settings.REBAC_ALLOW_SUDO
            and user is not None
            and getattr(user, "is_active", False)
            and getattr(user, "is_superuser", False)
        )
        try:
            with evaluator_scope():
                with zookie_scope(initial=initial_zookie):
                    if use_sudo:
                        with sudo(reason="superuser-bypass"):
                            response = self.get_response(request)
                    else:
                        response = self.get_response(request)
                    # Persist Zookie BEFORE the scope exits so
                    # ``current_zookie()`` still sees the post-write
                    # token. Header transport only mutates response
                    # headers; session transport mutates request.session.
                    self._persist_zookie(request, response)
                    return response
        finally:
            _current_actor.reset(actor_token)

    # ---------- Zookie transport plumbing (opt-in) ----------

    def _rehydrate_zookie(self, request: Any) -> Zookie | None:
        """Pull a previously-emitted Zookie out of the request, if any.

        ``"none"`` transport returns ``None`` (single-request scope).
        Header transport reads the configured header; malformed values
        are treated as absent (don't crash request handling on a
        client-supplied bad token). Session transport reads the
        configured session key.
        """
        transport = app_settings.REBAC_ZOOKIE_TRANSPORT
        if transport == "none":
            return None
        if transport == "header":
            header_name = self._header_meta_key()
            raw = request.META.get(header_name)
            if not raw:
                return None
            return _safe_parse_zookie(raw)
        if transport == "session":
            session = getattr(request, "session", None)
            if session is None:
                return None
            raw = session.get(app_settings.REBAC_ZOOKIE_SESSION_KEY)
            if not raw:
                return None
            return _safe_parse_zookie(raw)
        return None

    def _persist_zookie(self, request: Any, response: Any) -> None:
        """Write the in-scope Zookie back to the configured transport.

        No-op when ``"none"``, or when no Zookie was recorded during the
        request (read-only requests stay invisible to the transport).
        """
        transport = app_settings.REBAC_ZOOKIE_TRANSPORT
        if transport == "none":
            return
        zookie = current_zookie()
        if zookie is None:
            return
        if transport == "header":
            response[app_settings.REBAC_ZOOKIE_HEADER_NAME] = str(zookie)
        elif transport == "session":
            session = getattr(request, "session", None)
            if session is not None:
                session[app_settings.REBAC_ZOOKIE_SESSION_KEY] = str(zookie)

    @staticmethod
    def _header_meta_key() -> str:
        """Translate the user-visible header name to Django's META key.

        ``X-Rebac-Zookie`` → ``HTTP_X_REBAC_ZOOKIE`` per Django's WSGI
        convention.
        """
        name = app_settings.REBAC_ZOOKIE_HEADER_NAME.upper().replace("-", "_")
        return f"HTTP_{name}"


def _safe_parse_zookie(raw: str) -> Zookie | None:
    """Parse a Zookie wire string, swallowing malformed input.

    Returns ``None`` on parse failure so a broken / spoofed client
    header doesn't crash request handling. The lost freshness is the
    correct fail-safe: the read falls back to the backend's default
    consistency, which is no worse than no-Zookie behaviour.
    """
    try:
        return Zookie.parse(raw)
    except ValueError:
        return None
    except TypeError:
        return None
