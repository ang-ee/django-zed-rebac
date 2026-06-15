# Proposal 0004 - MCP tool integration

**Target version:** 0.11.
**Status:** Shipped.
**Scope:** MCP/FastMCP adapter only. No schema grammar change.

> **Shipped in `rebac.mcp`.** `rebac_mcp_tool`, `default_actor_resolver`, and
> `get_mcp_actor_resolver` are exported from `rebac.mcp` (and lazily from the
> top-level `rebac` package). Actor resolution is pluggable via the
> `REBAC_MCP_ACTOR_RESOLVER` setting (default
> `"rebac.mcp.default_actor_resolver"`). The adapter targets FastMCP's
> `ctx.request_context.meta` shape but reads it by duck-typing — it never
> imports the `mcp` SDK — so the module stays SDK-neutral and import-light.
> `hide_id_arg` is accepted but is a documented no-op against FastMCP 1.27,
> which exposes no public schema-filtering hook (see § Design).

## Why

The schema language already models MCP tools and capabilities naturally: a tool
or capability is just another resource type, and `invoke` / `use` is just
another permission. What is missing is the transport adapter that turns an MCP
request into a `SubjectRef`, checks the target permission, and only then calls
the tool body.

Earlier docs described `rebac.mcp.rebac_mcp_tool` as shipped before the module
existed; this proposal was the home for the work that landed it.

## Proposed API

```python
from rebac.mcp import rebac_mcp_tool

@mcp.tool
@rebac_mcp_tool(resource_type="blog/post", action="write", id_arg="post_id")
async def edit_post(post_id: str, body: str, ctx: Context = CurrentContext()) -> dict:
    ...
```

Capability-style tools use a fixed resource type plus a hidden resource id:

```python
@mcp.tool
@rebac_mcp_tool(
    resource_type="mcp/capability",
    action="use",
    id_arg="_capability",
    hide_id_arg=True,
)
async def search_documents(
    q: str,
    ctx: Context = CurrentContext(),
    *,
    _capability: str = "docs.search",
) -> list[dict]:
    ...
```

## Design

- The decorator resolves the actor from the MCP request context. Initial
  support should read `ctx.request_context.meta["actor_subject"]` as a canonical
  string such as `auth/user:42` or `agents/grant:42.assistant#valid`.
- The decorator constructs `ObjectRef(resource_type, resource_id)` where
  `resource_id` comes from `id_arg`, `resource_id`, or `"*"` for singleton tools.
- The permission check runs before the tool body. Deny raises
  `rebac.PermissionDenied`.
- The decorator must support sync and async tool functions.
- Create-shaped actions (`action="create"`) route through `rebac.check_new`,
  which authorises a not-yet-persisted row against a *relationship overlay* — the
  relations the new row would carry. The decorator builds that overlay from the
  `create_relations` mapping (relation name → the call argument holding the
  subject the new row would point at, as a canonical ref string). Without
  `create_relations`, only built-in-actor permissions (`authenticated` /
  `anonymous`) can pass; any relation/arrow-based create permission fails closed.
- The decorator must not mint or validate identity. Authentication remains the
  MCP server or transport layer's responsibility.
- The adapter should not depend on one MCP SDK if a narrow protocol shim can
  support FastMCP and the official SDK.

**As built.** Actor resolution runs the per-call `REBAC_MCP_ACTOR_RESOLVER`
callable (against the request context) first, then falls back to the ambient
`current_actor()`; no actor resolved → fail closed. The explicit per-request
ctx identity outranks the ambient ContextVar (CLAUDE.md § 5), so a leaked
ambient actor cannot override the authenticated caller. A malformed
`actor_subject` is a clean deny, not a 500 — `default_actor_resolver` returns
`None` on an unparseable ref. The body runs inside `actor_context(actor)` so any
queryset it builds scopes to the same actor; sync functions, coroutine
functions, and async generators (streaming tools) are all supported. The context
is read through a single `_context_meta(ctx)` accessor by duck-typing
(`ctx.request_context.meta`), so the module loads with or without the SDK
installed and the one SDK-shaped assumption lives in one function. The decorator
locates the MCP `Context` among the bound call arguments by the conventional
`ctx` / `context` parameter name, then by an argument whose type name is
`Context` *and* that carries a `request_context` attribute. The wrapped
function's `__signature__` is preserved unchanged so FastMCP's input-schema
introspection is unaffected. A `CONDITIONAL_PERMISSION` result fail-closes but
surfaces the missing caveat parameters in the `PermissionDenied` message.
`hide_id_arg` is a documented no-op against FastMCP 1.27 (no public
schema-filtering hook) and warns when set; keep the hidden id keyword-only so it
stays off the model-facing surface.

## Tests

- Actor resolution from `ctx.request_context.meta`.
- Missing actor fails closed.
- Denied permission prevents the tool body from running.
- Allowed permission calls the tool body exactly once.
- `id_arg` resource ids and singleton `"*"` tools both work.
- Capability-style hidden args do not appear in the exported MCP schema when
  the selected SDK exposes a hook for schema filtering.
- Sync and async tools are both supported.
- Grant-backed actors (`agents/grant:<id>#valid`) are accepted as ordinary
  `SubjectRef`s.

## Acceptance

- `rebac.mcp.rebac_mcp_tool` exists and is exported.
- README, ARCHITECTURE, and ZED docs move MCP from "planned" to "shipped".
- The test suite covers both FastMCP-compatible and SDK-neutral context shapes,
  or documents why only one SDK is supported.
- No changes to `.zed` grammar, parser, AST, or backend methods.
