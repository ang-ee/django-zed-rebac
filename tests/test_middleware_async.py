"""Async-mode tests for :class:`rebac.middleware.ActorMiddleware`.

The middleware is dual-mode (``sync_capable`` + ``async_capable``).
These tests pin the async path:

  - With an ``async def`` ``get_response``, the middleware reports as
    a coroutine function to Django (collapsing the sync↔async sandwich
    described in the v0.4.0 stack-trace investigation).
  - ContextVar set/reset still runs in LIFO even on exceptions.
  - Superuser bypass routes through ``sudo()`` and emits an audit row.
  - Header + session Zookie transport work, with session transport
    using the async ``aget``/``aset`` session API.
  - Evaluator + Zookie scopes are opened per request.

No pytest-asyncio dependency — coroutines are driven with
``asyncio.run`` (same idiom used by the strawberry tests).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from asgiref.sync import iscoroutinefunction
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

from rebac import Zookie, current_evaluator, current_zookie, record_zookie
from rebac.actors import current_actor, is_sudo
from rebac.middleware import ActorMiddleware
from rebac.models import PermissionAuditEvent


class _FakeRequest:
    def __init__(self, user):
        self.user = user


def _bare_request():
    """Anonymous-user request — default resolver handles it without DB IO."""
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    return request


# ---------- async-mode detection ----------


def test_async_get_response_marks_middleware_as_coroutine():
    async def get_response(request):  # pragma: no cover — never invoked here
        return "ok"

    mw = ActorMiddleware(get_response)

    assert mw._async_mode is True
    # markcoroutinefunction was applied — Django will skip async_to_sync.
    assert iscoroutinefunction(mw)


def test_sync_get_response_stays_sync():
    def get_response(request):
        return "ok"

    mw = ActorMiddleware(get_response)
    assert mw._async_mode is False
    assert iscoroutinefunction(mw) is False


# ---------- async __acall__ semantics ----------


def test_async_call_sets_and_resets_actor_contextvar():
    """No ``@pytest.mark.django_db`` — proves the anonymous-actor path
    doesn't touch the DB even under async transport.
    """
    captured: dict[str, object] = {}

    async def get_response(request):
        captured["actor"] = current_actor()
        captured["evaluator"] = current_evaluator()
        return "ok"

    mw = ActorMiddleware(get_response)
    response = asyncio.run(mw(_bare_request()))

    assert response == "ok"
    # Inside the bracket the actor + evaluator were live.
    assert captured["actor"] is not None
    assert captured["evaluator"] is not None
    # Teardown ran.
    assert current_actor() is None
    assert current_evaluator() is None


@pytest.mark.django_db(transaction=True)
@override_settings(REBAC_SUPERUSER_BYPASS=True)
def test_async_superuser_request_runs_inside_sudo_bracket(db):
    # ``transaction=True`` because asudo() awaits ``acreate`` which
    # runs on asgiref's shared thread (its own DB connection).
    # pytest-django's rollback-wrapped transaction would block that
    # connection's INSERT on sqlite — a test-harness artefact, not a
    # production issue. Postgres handles the separate-connection
    # write without locking.
    User = get_user_model()
    root = User.objects.create_superuser(username="aroot", email="a@x.com", password="x")
    PermissionAuditEvent.objects.all().delete()

    captured: dict[str, object] = {}

    async def get_response(request):
        captured["is_sudo"] = is_sudo()
        return "ok"

    mw = ActorMiddleware(get_response)
    asyncio.run(mw(_FakeRequest(root)))

    assert captured["is_sudo"] is True
    # Bracket teardown — no leak.
    assert is_sudo() is False
    assert current_actor() is None
    # Audit row written through the public sudo() helper.
    rows = list(PermissionAuditEvent.objects.filter(kind="sudo.bypass", reason="superuser-bypass"))
    assert len(rows) == 1


@pytest.mark.django_db(transaction=True)
def test_async_middleware_resolves_django_lazy_user_off_event_loop():
    from django.utils.functional import SimpleLazyObject

    from rebac import SubjectRef

    user = get_user_model().objects.create_user(username="lazy-async")
    request = RequestFactory().get("/")
    # AuthenticationMiddleware installs this same lazy shape. Resolving the
    # user performs a database lookup on the first attribute access.
    request.user = SimpleLazyObject(lambda: get_user_model().objects.get(pk=user.pk))
    seen = []

    async def get_response(request):
        seen.append(current_actor())
        return "ok"

    assert asyncio.run(ActorMiddleware(get_response)(request)) == "ok"
    assert seen == [SubjectRef.of("auth/user", str(user.pk))]


@pytest.mark.django_db(transaction=True)
def test_async_custom_resolver_leaves_lazy_user_safe_for_superuser_check(monkeypatch, settings):
    from django.utils.functional import SimpleLazyObject

    from rebac import SubjectRef

    settings.REBAC_SUPERUSER_BYPASS = True
    user = get_user_model().objects.create_user(username="lazy-custom-async")
    request = RequestFactory().get("/")
    request.user = SimpleLazyObject(lambda: get_user_model().objects.get(pk=user.pk))

    async def resolver(request):
        return SubjectRef.of("auth/user", str(user.pk))

    monkeypatch.setattr("rebac.middleware.get_actor_resolver", lambda: resolver)

    async def get_response(request):
        assert not is_sudo()
        return "ok"

    assert asyncio.run(ActorMiddleware(get_response)(request)) == "ok"


@pytest.mark.django_db(transaction=True)
@override_settings(REBAC_SUPERUSER_BYPASS=True)
def test_async_exception_in_view_still_tears_down(db):
    User = get_user_model()
    root = User.objects.create_superuser(username="aboom", email="b@x.com", password="x")

    async def boom(request):
        assert is_sudo() is True
        raise RuntimeError("view exploded")

    mw = ActorMiddleware(boom)
    with pytest.raises(RuntimeError, match="view exploded"):
        asyncio.run(mw(_FakeRequest(root)))

    assert is_sudo() is False
    assert current_actor() is None


# ---------- Zookie header transport (async) ----------


@pytest.mark.django_db
def test_async_header_transport_rehydrates_and_persists():
    seen: dict[str, object] = {}

    class _Resp(dict[str, object]):
        pass

    async def get_response(request):
        seen["initial"] = current_zookie()
        record_zookie(Zookie("local", "501"))
        return _Resp()

    with override_settings(REBAC_ZOOKIE_TRANSPORT="header"):
        request = RequestFactory().get("/", HTTP_X_REBAC_ZOOKIE="local.42")
        request.user = AnonymousUser()
        mw = ActorMiddleware(get_response)
        response = asyncio.run(mw(request))

    assert seen["initial"] == Zookie("local", "42")
    assert response["X-Rebac-Zookie"] == "local.501"


@pytest.mark.django_db
def test_async_header_transport_malformed_is_safe():
    seen: dict[str, object] = {}

    async def get_response(request):
        seen["initial"] = current_zookie()
        return type("R", (), {"__setitem__": lambda *_: None})()

    with override_settings(REBAC_ZOOKIE_TRANSPORT="header"):
        request = RequestFactory().get("/", HTTP_X_REBAC_ZOOKIE="garbage")
        request.user = AnonymousUser()
        mw = ActorMiddleware(get_response)
        asyncio.run(mw(request))

    assert seen["initial"] is None


# ---------- Zookie session transport (async) ----------


class _AsyncSessionDict(dict[str, object]):
    """Stand-in for a Django ``SessionBase`` exposing the async
    ``aget`` / ``aset`` API the middleware uses on the async path.
    """

    modified = False

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.aget_calls: list[str] = []
        self.aset_calls: list[tuple[str, object]] = []

    async def aget(self, key, default=None):
        self.aget_calls.append(key)
        return self.get(key, default)

    async def aset(self, key, value):
        self.aset_calls.append((key, value))
        self[key] = value


@pytest.mark.django_db
def test_async_session_transport_uses_aget_aset():
    seen: dict[str, object] = {}

    async def get_response(request):
        seen["initial"] = current_zookie()
        record_zookie(Zookie("local", "999"))
        return type("R", (), {"__setitem__": lambda *_: None})()

    with override_settings(REBAC_ZOOKIE_TRANSPORT="session"):
        request = _bare_request()
        request.session = _AsyncSessionDict({"_rebac_zookie": "local.42"})
        mw = ActorMiddleware(get_response)
        asyncio.run(mw(request))

    assert seen["initial"] == Zookie("local", "42")
    assert request.session["_rebac_zookie"] == "local.999"
    # The async session API was used — no sync DB call on the loop.
    assert request.session.aget_calls == ["_rebac_zookie"]
    assert request.session.aset_calls == [("_rebac_zookie", "local.999")]


@pytest.mark.django_db
def test_async_session_transport_no_session_skips():
    """Request without ``session`` doesn't crash the async path."""

    async def get_response(request):
        record_zookie(Zookie("local", "1"))
        return type("R", (), {"__setitem__": lambda *_: None})()

    with override_settings(REBAC_ZOOKIE_TRANSPORT="session"):
        mw = ActorMiddleware(get_response)
        # Must not raise.
        asyncio.run(mw(_bare_request()))


# ---------- async resolver support ----------


@pytest.mark.django_db
def test_async_resolver_is_awaited(settings):
    """A resolver declared ``async def`` is awaited on the async path."""

    seen: dict[str, object] = {}

    async def my_resolver(request):
        seen["called"] = True
        # Return the canonical anonymous SubjectRef so the rest of the
        # middleware doesn't care.
        from rebac.actors import anonymous_actor

        return anonymous_actor()

    # Inject the resolver via dotted path. Import the module the
    # middleware will look up.
    import sys
    import types

    fake_mod: Any = types.ModuleType("tests._async_resolver_fixture")
    fake_mod.my_resolver = my_resolver
    sys.modules["tests._async_resolver_fixture"] = fake_mod

    async def get_response(request):
        return "ok"

    with override_settings(REBAC_ACTOR_RESOLVER="tests._async_resolver_fixture.my_resolver"):
        mw = ActorMiddleware(get_response)
        asyncio.run(mw(_bare_request()))

    assert seen.get("called") is True
