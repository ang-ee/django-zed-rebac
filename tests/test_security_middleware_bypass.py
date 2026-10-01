"""The superuser bypass follows the resolved actor, not the session user."""

from __future__ import annotations

import asyncio

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from rebac import SubjectRef, backend, sudo, to_subject_ref
from rebac.actors import current_actor, is_sudo
from rebac.backends import reset_backend
from rebac.middleware import ActorMiddleware
from rebac.models import PermissionAuditEvent
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.testapp.models import Post

SCHEMA_TEXT = """
definition auth/user {}
definition agents/agent {}
definition blog/post {
    relation owner: auth/user | agents/agent
    permission read = owner
    permission write = owner
    permission delete = owner
    permission create = owner
}
"""

AGENT = SubjectRef.of("agents/agent", "helper")


class _Request:
    def __init__(self, user):
        self.user = user


def agent_resolver(request):
    return AGENT


def session_user_resolver(request):
    return to_subject_ref(request.user)


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


@pytest.fixture
def root(db):
    return get_user_model().objects.create_superuser(
        username="root", email="root@example.com", password="x"
    )


@pytest.fixture
def post(db):
    with sudo(reason="test.fixture"):
        return Post.objects.create(title="not the agent's")


def _sync_view(captured):
    def view(request):
        captured["sudo"] = is_sudo()
        captured["actor"] = current_actor()
        captured["rows"] = list(Post.objects.values_list("pk", flat=True))
        return "ok"

    return view


@override_settings(
    REBAC_SUPERUSER_BYPASS=True,
    REBAC_ACTOR_RESOLVER=f"{__name__}.agent_resolver",
)
def test_sync_resolved_agent_on_superuser_session_is_not_sudo(root, post):
    PermissionAuditEvent.objects.all().delete()
    captured = {}

    ActorMiddleware(_sync_view(captured))(_Request(root))

    assert captured["actor"] == AGENT
    assert captured["sudo"] is False
    assert captured["rows"] == []
    assert not PermissionAuditEvent.objects.filter(reason="superuser-bypass").exists()


@pytest.mark.django_db(transaction=True)
@override_settings(
    REBAC_SUPERUSER_BYPASS=True,
    REBAC_ACTOR_RESOLVER=f"{__name__}.agent_resolver",
)
def test_async_resolved_agent_on_superuser_session_is_not_sudo(root, post):
    PermissionAuditEvent.objects.all().delete()
    captured = {}

    async def view(request):
        captured["sudo"] = is_sudo()
        captured["actor"] = current_actor()
        captured["rows"] = [row.pk async for row in Post.objects.all()]
        return "ok"

    asyncio.run(ActorMiddleware(view)(_Request(root)))

    assert captured["actor"] == AGENT
    assert captured["sudo"] is False
    assert captured["rows"] == []
    assert not PermissionAuditEvent.objects.filter(reason="superuser-bypass").exists()


@override_settings(
    REBAC_SUPERUSER_BYPASS=True,
    REBAC_ACTOR_RESOLVER=f"{__name__}.session_user_resolver",
)
def test_resolved_superuser_keeps_documented_bypass(root, post):
    captured = {}

    ActorMiddleware(_sync_view(captured))(_Request(root))

    assert captured["actor"] == to_subject_ref(root)
    assert captured["sudo"] is True
    assert captured["rows"] == [post.pk]


@override_settings(
    REBAC_SUPERUSER_BYPASS=True,
    REBAC_ACTOR_RESOLVER=f"{__name__}.session_user_resolver",
)
def test_superuser_resolution_error_suppresses_bypass(root, post, monkeypatch):
    from rebac import middleware
    from rebac.errors import NoActorResolvedError

    def fail(user):
        raise NoActorResolvedError("identity unavailable")

    monkeypatch.setattr(middleware, "to_subject_ref", fail)
    captured = {}
    ActorMiddleware(_sync_view(captured))(_Request(root))
    assert captured["sudo"] is False
    assert captured["rows"] == []
