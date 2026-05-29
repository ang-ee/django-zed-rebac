# Proposal 0004 - MCP tool integration

**Target version:** future minor release.
**Status:** Draft.
**Scope:** MCP/FastMCP adapter only. No schema grammar change.

## Why

The schema language already models MCP tools and capabilities naturally: a tool
or capability is just another resource type, and `invoke` / `use` is just
another permission. What is missing is the transport adapter that turns an MCP
request into a `SubjectRef`, checks the target permission, and only then calls
the tool body.

Earlier docs described `rebac.mcp.rebac_mcp_tool` as shipped. That module does
not exist in the package today, so this proposal is the new home for the work.

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
- The decorator must not mint or validate identity. Authentication remains the
  MCP server or transport layer's responsibility.
- The adapter should not depend on one MCP SDK if a narrow protocol shim can
  support FastMCP and the official SDK.

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
