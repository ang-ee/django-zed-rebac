"""ActorMiddleware integration with evaluator + Zookie transport.

Covers:
  - HTTP request opens evaluator_scope + zookie_scope.
  - Header transport rehydrates / persists ``X-Rebac-Zookie``.
  - Session transport rehydrates / persists ``_rebac_zookie`` key.
  - "none" transport is single-request scope only.
  - Malformed header values fail safe (treated as absent).
  - System checks E007 / W006.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core import checks
from django.test import RequestFactory, override_settings
from django.test.client import RequestFactory as _RF

from rebac import Zookie, current_evaluator, current_zookie, record_zookie
from rebac.middleware import ActorMiddleware


def _bare_request():
    """RequestFactory request with AnonymousUser pre-attached.

    ActorMiddleware reads ``request.user`` and the default resolver
    handles AnonymousUser without DB access — keeps tests pure.
    """
    rf: _RF = RequestFactory()
    request = rf.get("/")
    request.user = AnonymousUser()
    return request


# ---------- Per-request evaluator + zookie scopes ----------


@pytest.mark.django_db
def test_middleware_opens_evaluator_scope_for_request():
    """During get_response the evaluator is available; afterward it is None."""
    seen: dict[str, object] = {}

    def get_response(request):
        seen["evaluator"] = current_evaluator()
        return type("R", (), {"__setitem__": lambda *_: None})()

    mw = ActorMiddleware(get_response)
    mw(_bare_request())
    # Outside the bracket: no evaluator.
    assert current_evaluator() is None
    # Inside the bracket: evaluator instance was live.
    assert seen["evaluator"] is not None


@pytest.mark.django_db
def test_middleware_opens_zookie_scope_for_request():
    seen: dict[str, object] = {}

    def get_response(request):
        seen["zookie_before_write"] = current_zookie()
        record_zookie(Zookie("local", "100"))
        seen["zookie_after_write"] = current_zookie()
        return type("R", (), {"__setitem__": lambda *_: None})()

    mw = ActorMiddleware(get_response)
    mw(_bare_request())
    # Outside the bracket: no zookie.
    assert current_zookie() is None
    assert seen["zookie_before_write"] is None
    assert seen["zookie_after_write"] == Zookie("local", "100")


# ---------- Header transport ----------


@pytest.mark.django_db
def test_header_transport_rehydrates_initial_zookie():
    seen: dict[str, object] = {}

    def get_response(request):
        seen["initial"] = current_zookie()
        return type("R", (), {"__setitem__": lambda *_: None})()

    with override_settings(REBAC_ZOOKIE_TRANSPORT="header"):
        rf = RequestFactory()
        request = rf.get("/", HTTP_X_REBAC_ZOOKIE="local.42")
        request.user = AnonymousUser()
        mw = ActorMiddleware(get_response)
        mw(request)
    assert seen["initial"] == Zookie("local", "42")


@pytest.mark.django_db
def test_header_transport_persists_response_header():
    class _Resp(dict[str, object]):
        pass

    def get_response(request):
        record_zookie(Zookie("local", "500"))
        return _Resp()

    with override_settings(REBAC_ZOOKIE_TRANSPORT="header"):
        mw = ActorMiddleware(get_response)
        response = mw(_bare_request())
    assert response["X-Rebac-Zookie"] == "local.500"


@pytest.mark.django_db
def test_header_transport_malformed_value_is_safe():
    """A garbage header doesn't crash; rehydrated as None."""
    seen: dict[str, object] = {}

    def get_response(request):
        seen["initial"] = current_zookie()
        return type("R", (), {"__setitem__": lambda *_: None})()

    with override_settings(REBAC_ZOOKIE_TRANSPORT="header"):
        rf = RequestFactory()
        request = rf.get("/", HTTP_X_REBAC_ZOOKIE="no-dots-here")
        request.user = AnonymousUser()
        mw = ActorMiddleware(get_response)
        mw(request)
    assert seen["initial"] is None


@pytest.mark.django_db
def test_header_transport_read_only_request_skips_response_header():
    """A request that records no Zookie produces no response header."""

    class _Resp(dict[str, object]):
        pass

    def get_response(request):
        return _Resp()

    with override_settings(REBAC_ZOOKIE_TRANSPORT="header"):
        mw = ActorMiddleware(get_response)
        response = mw(_bare_request())
    assert "X-Rebac-Zookie" not in response


# ---------- "none" transport ----------


@pytest.mark.django_db
def test_none_transport_ignores_header():
    seen: dict[str, object] = {}

    def get_response(request):
        seen["initial"] = current_zookie()
        return type("R", (), {"__setitem__": lambda *_: None})()

    # Default is "none" — header should be ignored.
    rf = RequestFactory()
    request = rf.get("/", HTTP_X_REBAC_ZOOKIE="local.42")
    request.user = AnonymousUser()
    mw = ActorMiddleware(get_response)
    mw(request)
    assert seen["initial"] is None


# ---------- Session transport ----------


@pytest.mark.django_db
def test_session_transport_rehydrates_and_persists():
    seen: dict[str, object] = {}

    class _SessionDict(dict[str, object]):
        modified = False

    def get_response(request):
        seen["initial"] = current_zookie()
        record_zookie(Zookie("local", "999"))
        return type("R", (), {"__setitem__": lambda *_: None})()

    with override_settings(REBAC_ZOOKIE_TRANSPORT="session"):
        request = _bare_request()
        request.session = _SessionDict({"_rebac_zookie": "local.42"})
        mw = ActorMiddleware(get_response)
        mw(request)
    assert seen["initial"] == Zookie("local", "42")
    assert request.session["_rebac_zookie"] == "local.999"


@pytest.mark.django_db
def test_session_transport_no_session_attribute_skips():
    """Request without a session attribute degrades silently — no crash."""

    def get_response(request):
        record_zookie(Zookie("local", "1"))
        return type("R", (), {"__setitem__": lambda *_: None})()

    with override_settings(REBAC_ZOOKIE_TRANSPORT="session"):
        request = _bare_request()
        # Deliberately no request.session
        mw = ActorMiddleware(get_response)
        # Must NOT raise.
        mw(request)


# ---------- System checks ----------


def test_invalid_transport_triggers_e007():
    with override_settings(REBAC_ZOOKIE_TRANSPORT="bogus"):
        errors = checks.run_checks(tags=["rebac"])
        assert any(e.id == "rebac.E007" for e in errors)


def test_session_without_contrib_sessions_triggers_w006():
    """W006 fires when transport=session but django.contrib.sessions is absent."""
    # `tests.settings` includes contrib.sessions; mock its absence via
    # override_settings with a filtered INSTALLED_APPS.
    from django.conf import settings as dj_settings

    installed = [a for a in dj_settings.INSTALLED_APPS if a != "django.contrib.sessions"]
    with override_settings(
        REBAC_ZOOKIE_TRANSPORT="session",
        INSTALLED_APPS=installed,
    ):
        warnings = checks.run_checks(tags=["rebac"])
        assert any(w.id == "rebac.W006" for w in warnings)
