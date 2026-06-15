"""Tests for ``rebac.mcp`` — the ``rebac_mcp_tool`` adapter (proposal 0004).

Covers actor resolution from ``ctx.request_context.meta``, the ambient
``current_actor()`` path, fail-closed on a missing actor, deny short-circuiting
the body, allow running the body exactly once, ``id_arg`` / singleton ``"*"``
resource ids, sync + async tools, and grant-backed actors.

A fake ``Context`` (a nested ``SimpleNamespace``) stands in for the FastMCP
``Context`` so these tests need no ``mcp`` SDK install — the adapter reads the
context by duck-typing, exactly as proposal 0004 specifies.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from rebac import (
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    actor_context,
    backend,
)
from rebac.backends import reset_backend
from rebac.mcp import default_actor_resolver, rebac_mcp_tool
from rebac.schema import parse_zed

SCHEMA_TEXT = """
definition auth/user {}

definition agents/grant {}

definition mcp/tool/edit_post {
    relation invoker: auth/user | agents/grant#valid
    permission invoke = invoker
}

definition blog/post {
    relation owner: auth/user | agents/grant#valid
    relation parent: blog/post
    permission write = owner
    permission create = parent->write
}
"""


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


def _ctx(actor_subject: str | None = None) -> SimpleNamespace:
    """A fake FastMCP context exposing ``request_context.meta``."""
    meta: dict[str, str] = {}
    if actor_subject is not None:
        meta["actor_subject"] = actor_subject
    return SimpleNamespace(request_context=SimpleNamespace(meta=meta))


def _grant_invoke(resource: ObjectRef, subject: SubjectRef) -> None:
    backend().write_relationships(
        [RelationshipTuple(resource=resource, relation="invoker", subject=subject)]
    )


def _grant_owner(resource: ObjectRef, subject: SubjectRef) -> None:
    backend().write_relationships(
        [RelationshipTuple(resource=resource, relation="owner", subject=subject)]
    )


# ---------- default_actor_resolver ----------


def test_default_resolver_reads_actor_subject() -> None:
    actor = default_actor_resolver(_ctx("auth/user:42"))
    assert actor == SubjectRef.of("auth/user", "42")


def test_default_resolver_parses_grant_backed_subject() -> None:
    actor = default_actor_resolver(_ctx("agents/grant:42.assistant#valid"))
    assert actor == SubjectRef.of("agents/grant", "42.assistant", "valid")
    assert actor.optional_relation == "valid"


def test_default_resolver_returns_none_without_actor_subject() -> None:
    assert default_actor_resolver(_ctx()) is None


# ---------- actor resolution paths ----------


@pytest.mark.django_db
def test_actor_from_request_context_meta_allows() -> None:
    _grant_invoke(ObjectRef("mcp/tool/edit_post", "*"), SubjectRef.of("auth/user", "7"))
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    def edit(body: str, ctx: object = None) -> str:
        calls.append(body)
        return "ok"

    assert edit("hello", ctx=_ctx("auth/user:7")) == "ok"
    assert calls == ["hello"]


@pytest.mark.django_db
def test_ctx_actor_takes_priority_over_ambient() -> None:
    # Ctx names user 7 (granted); ambient names user 9 (no grant). The per-call
    # ctx actor must win — the body runs because user 7 is authorised, and the
    # ambient ContextVar does NOT override the explicit per-request identity.
    _grant_invoke(ObjectRef("mcp/tool/edit_post", "*"), SubjectRef.of("auth/user", "7"))
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    def edit(body: str, ctx: object = None) -> str:
        calls.append(body)
        return "ok"

    with actor_context(SubjectRef.of("auth/user", "9")):
        assert edit("hi", ctx=_ctx("auth/user:7")) == "ok"
    assert calls == ["hi"]


@pytest.mark.django_db
def test_ambient_actor_used_as_fallback_when_ctx_has_no_actor() -> None:
    # Ctx carries no actor_subject; ambient names a granted user. With no per-call
    # identity stamped, the ambient actor is the fallback and the body runs.
    _grant_invoke(ObjectRef("mcp/tool/edit_post", "*"), SubjectRef.of("auth/user", "9"))
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    def edit(body: str, ctx: object = None) -> str:
        calls.append(body)
        return "ok"

    with actor_context(SubjectRef.of("auth/user", "9")):
        assert edit("hi", ctx=_ctx()) == "ok"
    assert calls == ["hi"]


@pytest.mark.django_db
def test_missing_actor_fails_closed() -> None:
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    def edit(body: str, ctx: object = None) -> str:
        calls.append(body)
        return "ok"

    with pytest.raises(PermissionDenied):
        edit("hi", ctx=_ctx())  # no actor_subject, no ambient actor
    assert calls == []


# ---------- permission gating ----------


@pytest.mark.django_db
def test_denied_permission_does_not_run_body() -> None:
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    def edit(body: str, ctx: object = None) -> str:
        calls.append(body)
        return "ok"

    with pytest.raises(PermissionDenied):
        edit("hi", ctx=_ctx("auth/user:404"))  # never granted invoke
    assert calls == []


@pytest.mark.django_db
def test_allowed_runs_body_exactly_once() -> None:
    _grant_invoke(ObjectRef("mcp/tool/edit_post", "*"), SubjectRef.of("auth/user", "1"))
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    def edit(body: str, ctx: object = None) -> str:
        calls.append(body)
        return "ok"

    edit("once", ctx=_ctx("auth/user:1"))
    assert calls == ["once"]


# ---------- resource id resolution ----------


@pytest.mark.django_db
def test_id_arg_targets_the_named_row() -> None:
    _grant_owner(ObjectRef("blog/post", "p1"), SubjectRef.of("auth/user", "5"))
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="blog/post", action="write", id_arg="post_id")
    def edit_post(post_id: str, body: str, ctx: object = None) -> str:
        calls.append(post_id)
        return "ok"

    # Authorised on p1, denied on p2 — same actor, different id_arg value.
    assert edit_post("p1", "x", ctx=_ctx("auth/user:5")) == "ok"
    with pytest.raises(PermissionDenied):
        edit_post("p2", "x", ctx=_ctx("auth/user:5"))
    assert calls == ["p1"]


@pytest.mark.django_db
def test_resource_id_decorator_arg_used_when_no_id_arg() -> None:
    _grant_owner(ObjectRef("blog/post", "fixed"), SubjectRef.of("auth/user", "5"))

    @rebac_mcp_tool(resource_type="blog/post", action="write", resource_id="fixed")
    def edit_fixed(body: str, ctx: object = None) -> str:
        return "ok"

    assert edit_fixed("x", ctx=_ctx("auth/user:5")) == "ok"


@pytest.mark.django_db
def test_singleton_star_when_no_id_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    _grant_invoke(ObjectRef("mcp/tool/edit_post", "*"), SubjectRef.of("auth/user", "5"))
    seen: list[ObjectRef] = []

    real_check_access = backend().check_access

    def spy(*, subject, action, resource, **kw):  # type: ignore[no-untyped-def]
        seen.append(resource)
        return real_check_access(subject=subject, action=action, resource=resource, **kw)

    # monkeypatch auto-restores the bound method after the test.
    monkeypatch.setattr(backend(), "check_access", spy)

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    def tool(ctx: object = None) -> str:
        return "ok"

    assert tool(ctx=_ctx("auth/user:5")) == "ok"
    assert seen == [ObjectRef("mcp/tool/edit_post", "*")]


@pytest.mark.django_db
def test_id_arg_default_none_falls_back_to_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    # A tool whose id_arg parameter has a default the caller omits must not check
    # a bogus "<type>:None" row — it falls back to the singleton "*".
    _grant_invoke(ObjectRef("mcp/tool/edit_post", "*"), SubjectRef.of("auth/user", "5"))
    seen: list[ObjectRef] = []

    real_check_access = backend().check_access

    def spy(*, subject, action, resource, **kw):  # type: ignore[no-untyped-def]
        seen.append(resource)
        return real_check_access(subject=subject, action=action, resource=resource, **kw)

    monkeypatch.setattr(backend(), "check_access", spy)

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke", id_arg="thing")
    def tool(thing: str | None = None, ctx: object = None) -> str:
        return "ok"

    assert tool(ctx=_ctx("auth/user:5")) == "ok"
    assert seen == [ObjectRef("mcp/tool/edit_post", "*")]


# ---------- create-style action routes through check_new ----------


@pytest.mark.django_db
def test_create_action_uses_preflight() -> None:
    # blog/post#create = parent->write — there is no per-row tuple yet, so a
    # plain has_access would deny. check_new evaluates the permission for a
    # not-yet-persisted row. With no create_relations overlay it resolves NO, so
    # the body must not run.
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="blog/post", action="create")
    def create_post(body: str, ctx: object = None) -> str:
        calls.append(body)
        return "ok"

    with pytest.raises(PermissionDenied):
        create_post("x", ctx=_ctx("auth/user:5"))
    assert calls == []


@pytest.mark.django_db
def test_create_with_relations_overlay_allows() -> None:
    # create = parent->write. The new row would carry parent -> blog/post:p0,
    # and user 5 owns p0 (so has write on it). The overlay lets check_new walk
    # the arrow into the real parent and authorise the create.
    _grant_owner(ObjectRef("blog/post", "p0"), SubjectRef.of("auth/user", "5"))
    calls: list[str] = []

    @rebac_mcp_tool(
        resource_type="blog/post",
        action="create",
        create_relations={"parent": "parent_ref"},
    )
    def create_post(parent_ref: str, body: str, ctx: object = None) -> str:
        calls.append(body)
        return "ok"

    assert create_post("blog/post:p0", "x", ctx=_ctx("auth/user:5")) == "ok"
    assert calls == ["x"]


@pytest.mark.django_db
def test_create_with_relations_overlay_denies_without_parent_write() -> None:
    # Same overlay, but user 5 has no write on the named parent -> deny.
    calls: list[str] = []

    @rebac_mcp_tool(
        resource_type="blog/post",
        action="create",
        create_relations={"parent": "parent_ref"},
    )
    def create_post(parent_ref: str, body: str, ctx: object = None) -> str:
        calls.append(body)
        return "ok"

    with pytest.raises(PermissionDenied):
        create_post("blog/post:p0", "x", ctx=_ctx("auth/user:5"))
    assert calls == []


# ---------- async support ----------


# ``transaction=True`` — the async wrapper runs the sync permission check on
# asgiref's worker thread (``sync_to_async``); pytest-django's default
# rollback-wrapped transaction would deadlock that thread on sqlite.
@pytest.mark.django_db(transaction=True)
def test_async_tool_allowed_runs_body() -> None:
    _grant_invoke(ObjectRef("mcp/tool/edit_post", "*"), SubjectRef.of("auth/user", "1"))
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    async def edit(body: str, ctx: object = None) -> str:
        calls.append(body)
        return "ok"

    assert asyncio.run(edit("hi", ctx=_ctx("auth/user:1"))) == "ok"
    assert calls == ["hi"]


@pytest.mark.django_db(transaction=True)
def test_async_tool_denied_does_not_run_body() -> None:
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    async def edit(body: str, ctx: object = None) -> str:
        calls.append(body)
        return "ok"

    with pytest.raises(PermissionDenied):
        asyncio.run(edit("hi", ctx=_ctx("auth/user:404")))
    assert calls == []


# ---------- grant-backed actor accepted ----------


@pytest.mark.django_db
def test_grant_backed_actor_is_accepted() -> None:
    grant = SubjectRef.of("agents/grant", "42.assistant", "valid")
    _grant_invoke(ObjectRef("mcp/tool/edit_post", "*"), grant)
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    def edit(body: str, ctx: object = None) -> str:
        calls.append(body)
        return "ok"

    assert edit("hi", ctx=_ctx("agents/grant:42.assistant#valid")) == "ok"
    assert calls == ["hi"]


# ---------- signature preserved for SDK schema introspection ----------


def test_wrapped_signature_is_unchanged() -> None:
    import inspect

    @rebac_mcp_tool(resource_type="blog/post", action="write", id_arg="post_id")
    def edit_post(post_id: str, body: str, ctx: object = None) -> str:
        return "ok"

    params = list(inspect.signature(edit_post).parameters)
    assert params == ["post_id", "body", "ctx"]
    assert edit_post.__name__ == "edit_post"


# ---------- actor_context entered around the body ----------


@pytest.mark.django_db
def test_body_runs_inside_actor_context() -> None:
    from rebac import current_actor

    _grant_invoke(ObjectRef("mcp/tool/edit_post", "*"), SubjectRef.of("auth/user", "1"))
    seen: list[SubjectRef | None] = []

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    def edit(ctx: object = None) -> str:
        seen.append(current_actor())
        return "ok"

    edit(ctx=_ctx("auth/user:1"))
    assert seen == [SubjectRef.of("auth/user", "1")]


# ---------- async-generator (streaming) tools ----------


async def _drain(agen: object) -> list[str]:
    return [item async for item in agen]  # type: ignore[attr-defined, union-attr]


@pytest.mark.django_db(transaction=True)
def test_async_generator_tool_runs_inside_actor_context() -> None:
    from rebac import current_actor

    _grant_invoke(ObjectRef("mcp/tool/edit_post", "*"), SubjectRef.of("auth/user", "1"))
    seen: list[SubjectRef | None] = []

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    async def stream(ctx: object = None):  # type: ignore[no-untyped-def]
        seen.append(current_actor())
        yield "a"
        yield "b"

    assert asyncio.run(_drain(stream(ctx=_ctx("auth/user:1")))) == ["a", "b"]
    assert seen == [SubjectRef.of("auth/user", "1")]


@pytest.mark.django_db(transaction=True)
def test_async_generator_tool_denied_does_not_run_body() -> None:
    produced: list[str] = []

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    async def stream(ctx: object = None):  # type: ignore[no-untyped-def]
        produced.append("a")
        yield "a"

    with pytest.raises(PermissionDenied):
        asyncio.run(_drain(stream(ctx=_ctx("auth/user:404"))))
    assert produced == []


# ---------- malformed actor_subject fails closed (no 500) ----------


def test_default_resolver_returns_none_on_malformed_actor_subject() -> None:
    # No ':' separator -> SubjectRef.parse would raise; the resolver declines.
    assert default_actor_resolver(_ctx("garbage-no-colon")) is None


@pytest.mark.django_db
def test_malformed_actor_subject_raises_permission_denied_not_value_error() -> None:
    calls: list[str] = []

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    def edit(ctx: object = None) -> str:
        calls.append("ran")
        return "ok"

    with pytest.raises(PermissionDenied):
        edit(ctx=_ctx("garbage-no-colon"))
    assert calls == []


# ---------- CONDITIONAL collapses to a deny that names missing context ----------


@pytest.mark.django_db
def test_conditional_permission_surfaces_missing_caveat_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rebac import CheckResult

    def conditional(*, subject, action, resource, **kw):  # type: ignore[no-untyped-def]
        return CheckResult.conditional(missing=("now",))

    monkeypatch.setattr(backend(), "check_access", conditional)

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    def edit(ctx: object = None) -> str:
        return "ok"

    with pytest.raises(PermissionDenied) as excinfo:
        edit(ctx=_ctx("auth/user:5"))
    message = str(excinfo.value)
    assert "missing caveat context" in message
    assert "now" in message


# ---------- context-finding heuristic ----------


@pytest.mark.django_db
def test_find_context_accepts_context_named_arg_with_request_context() -> None:
    # A non-ctx/context parameter whose value's class is named 'Context' and
    # carries request_context is recognised as the MCP context.
    _grant_invoke(ObjectRef("mcp/tool/edit_post", "*"), SubjectRef.of("auth/user", "5"))

    class Context(SimpleNamespace):
        pass

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    def edit(c: object) -> str:
        return "ok"

    fake = Context(request_context=SimpleNamespace(meta={"actor_subject": "auth/user:5"}))
    assert edit(fake) == "ok"


@pytest.mark.django_db
def test_find_context_ignores_context_named_arg_without_request_context() -> None:
    # An unrelated arg of a class named 'Context' but lacking request_context
    # must NOT be mistaken for the MCP context -> no actor -> fail closed.
    class Context:  # a test double, not the MCP context shape
        pass

    @rebac_mcp_tool(resource_type="mcp/tool/edit_post", action="invoke")
    def edit(payload: object) -> str:
        return "ok"

    with pytest.raises(PermissionDenied):
        edit(Context())


# ---------- _context_meta SDK-shape fallbacks ----------


def test_default_resolver_reads_model_dump_meta() -> None:
    class Meta:
        def model_dump(self) -> dict[str, str]:
            return {"actor_subject": "auth/user:42"}

    ctx = SimpleNamespace(request_context=SimpleNamespace(meta=Meta()))
    assert default_actor_resolver(ctx) == SubjectRef.of("auth/user", "42")


def test_default_resolver_reads_namespace_dict_meta() -> None:
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(meta=SimpleNamespace(actor_subject="auth/user:42"))
    )
    assert default_actor_resolver(ctx) == SubjectRef.of("auth/user", "42")


# ---------- hide_id_arg warns instead of silently no-op'ing ----------


def test_hide_id_arg_emits_warning() -> None:
    with pytest.warns(UserWarning, match="hide_id_arg"):

        @rebac_mcp_tool(
            resource_type="mcp/capability",
            action="use",
            id_arg="_capability",
            hide_id_arg=True,
        )
        def search(ctx: object = None, *, _capability: str = "docs.search") -> str:
            return "ok"
