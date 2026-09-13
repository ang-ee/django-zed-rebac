"""Tests for ``rebac.middleware.ActorMiddleware`` — superuser bypass.

The middleware opens a ``sudo(reason="superuser-bypass")`` bracket for
the request lifetime when the user is an active superuser AND both
``REBAC_SUPERUSER_BYPASS`` and ``REBAC_ALLOW_SUDO`` are True.

These tests pin three things the bypass must guarantee:

  - It activates only for active superusers under both feature flags.
  - It routes through the public ``sudo()`` API so every elevated
    request emits a ``KIND_SUDO_BYPASS`` audit row (CLAUDE.md § 3).
  - Both the actor ContextVar and the sudo bracket are torn down in
    LIFO order, including when ``get_response`` raises.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model, login
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils.functional import SimpleLazyObject

from rebac import ANONYMOUS_ACTOR, SubjectRef
from rebac.actors import current_actor, is_sudo
from rebac.middleware import ActorMiddleware
from rebac.models import PermissionAuditEvent


class _FakeRequest:
    def __init__(self, user):
        self.user = user


@pytest.fixture
def superuser(db):
    User = get_user_model()
    return User.objects.create_superuser(username="root", email="root@example.com", password="x")


@pytest.fixture
def regular_user(db):
    User = get_user_model()
    return User.objects.create_user(username="alice", email="alice@example.com", password="x")


def _capture(captured):
    """Build a get_response that records sudo state at request time."""

    def get_response(request):
        captured["is_sudo"] = is_sudo()
        captured["actor"] = current_actor()
        return "ok"

    return get_response


@pytest.mark.django_db
@override_settings(REBAC_SUPERUSER_BYPASS=True)
def test_superuser_request_runs_inside_sudo_bracket(superuser):
    PermissionAuditEvent.objects.all().delete()
    captured: dict[str, object] = {}
    mw = ActorMiddleware(_capture(captured))

    response = mw(_FakeRequest(superuser))

    assert response == "ok"
    assert captured["is_sudo"] is True
    # Actor ContextVar was set from the resolver too.
    assert captured["actor"] is not None
    # Bracket teardown ran — no leak past the request.
    assert is_sudo() is False
    assert current_actor() is None
    # Audit row was emitted with the bypass reason.
    rows = list(PermissionAuditEvent.objects.filter(kind="sudo.bypass", reason="superuser-bypass"))
    assert len(rows) == 1


@pytest.mark.django_db
@override_settings(REBAC_SUPERUSER_BYPASS=True)
def test_regular_user_request_does_not_open_sudo(regular_user):
    PermissionAuditEvent.objects.all().delete()
    captured: dict[str, object] = {}
    mw = ActorMiddleware(_capture(captured))

    mw(_FakeRequest(regular_user))

    assert captured["is_sudo"] is False
    assert PermissionAuditEvent.objects.filter(reason="superuser-bypass").count() == 0


@pytest.mark.django_db
@override_settings(REBAC_SUPERUSER_BYPASS=True)
def test_inactive_superuser_does_not_open_sudo(db):
    User = get_user_model()
    inactive = User.objects.create_superuser(
        username="ghost", email="ghost@example.com", password="x"
    )
    inactive.is_active = False
    inactive.save(update_fields=["is_active"])

    captured: dict[str, object] = {}
    mw = ActorMiddleware(_capture(captured))
    mw(_FakeRequest(inactive))

    assert captured["is_sudo"] is False


@pytest.mark.django_db
@override_settings(REBAC_SUPERUSER_BYPASS=False)
def test_bypass_disabled_setting_suppresses_sudo(superuser):
    captured: dict[str, object] = {}
    mw = ActorMiddleware(_capture(captured))
    mw(_FakeRequest(superuser))
    assert captured["is_sudo"] is False


@pytest.mark.django_db
@override_settings(REBAC_SUPERUSER_BYPASS=True, REBAC_ALLOW_SUDO=False)
def test_allow_sudo_false_suppresses_bypass(superuser):
    """Tenants that globally disable sudo must not get an implicit
    superuser bypass — the safe fail-closed answer."""
    captured: dict[str, object] = {}
    mw = ActorMiddleware(_capture(captured))
    mw(_FakeRequest(superuser))
    assert captured["is_sudo"] is False


@pytest.mark.django_db
@override_settings(REBAC_SUPERUSER_BYPASS=True)
def test_exception_in_view_still_resets_actor_and_sudo(superuser):
    def boom(request):
        assert is_sudo() is True
        raise RuntimeError("view exploded")

    mw = ActorMiddleware(boom)
    with pytest.raises(RuntimeError, match="view exploded"):
        mw(_FakeRequest(superuser))

    # Both ContextVars must be torn down even on exception.
    assert is_sudo() is False
    assert current_actor() is None


@pytest.mark.django_db
@override_settings(SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies")
@pytest.mark.parametrize("id_attr", ["pk", "username"])
def test_authentication_middleware_lazy_model_user_resolves_and_tears_down(monkeypatch, id_attr):
    User = get_user_model()
    monkeypatch.setattr(User._meta, "rebac_resource_type", "accounts/member", raising=False)
    monkeypatch.setattr(User._meta, "rebac_id_attr", id_attr, raising=False)
    monkeypatch.setattr(User._meta, "rebac_subject_relation", "participant", raising=False)
    user = User.objects.create_user(username="session-alice", password="secret")
    request = RequestFactory().get("/")
    SessionMiddleware(lambda _: None).process_request(request)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    AuthenticationMiddleware(lambda _: None).process_request(request)
    assert isinstance(request.user, SimpleLazyObject)
    captured = {}

    def view(_request):
        captured["actor"] = current_actor()
        return HttpResponse("ok")

    with CaptureQueriesContext(connection) as queries:
        response = ActorMiddleware(view)(request)

    # The lazy user is materialised exactly once, by the resolver; identity
    # resolution itself reads only instance metadata and issues no query.
    assert len(queries.captured_queries) == 1, [q["sql"] for q in queries.captured_queries]
    assert response.status_code == 200
    assert captured["actor"] == SubjectRef.of(
        "accounts/member",
        str(user.pk) if id_attr == "pk" else "session-alice",
        "participant",
    )
    assert current_actor() is None


@pytest.mark.django_db
@override_settings(SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies")
def test_authentication_middleware_lazy_anonymous_user_is_unchanged():
    request = RequestFactory().get("/")
    SessionMiddleware(lambda _: None).process_request(request)
    AuthenticationMiddleware(lambda _: None).process_request(request)
    assert isinstance(request.user, SimpleLazyObject)
    captured = {}

    def view(_request):
        captured["actor"] = current_actor()
        return HttpResponse("ok")

    response = ActorMiddleware(view)(request)

    assert response.status_code == 200
    assert captured["actor"] == ANONYMOUS_ACTOR
    assert current_actor() is None
