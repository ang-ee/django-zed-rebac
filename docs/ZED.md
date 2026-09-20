# Defining Permissions in `django-zed-rebac`

> Last updated: 2026-05-30
> Status: **alpha implementation guide**.
> Audience: Django developers writing permission schemas. Read [ARCHITECTURE.md](./ARCHITECTURE.md) first for the system design.

---

## What you author

A **schema** is a `.zed` file shipped alongside your Django app. It declares the resource types, relations, permissions, and caveats your app uses. The plugin parses every app's `.zed` file at sync time and stores the result in `Schema*` tables. `LocalBackend` consumes that parsed output today; the planned `SpiceDBBackend` will consume the same schema contract.

You write three things and only three things:

1. **A `.zed` file** — `<app>/permissions.zed`, in SpiceDB's schema language.
2. **A pointer on the AppConfig** — `rebac_schema = "permissions.zed"`.
3. **A type label on each model** — `class Meta: rebac_resource_type = "<app>/<type>"`.

That's it. No Python schema-builder, no `Meta.permission_relations`, no decorators on the schema itself. The `.zed` file is the source of truth — same syntax SpiceDB users already know.

---

## The minimum viable schema

```zed
// blog/permissions.zed
// @rebac_package: blog
// @rebac_package_version: 0.1.0
// @rebac_schema_revision: 1

definition blog/post {
    relation owner: auth/user

    permission read  = owner
    permission write = owner
}
```

```python
# blog/apps.py
from django.apps import AppConfig

class BlogConfig(AppConfig):
    name = "blog"
    rebac_schema = "permissions.zed"   # relative to the app's package dir
```

```python
# blog/models.py
from django.db import models
from rebac import RebacMixin

class Post(RebacMixin, models.Model):
    title = models.CharField(max_length=200)

    class Meta:
        rebac_resource_type = "blog/post"
```

```bash
python manage.py migrate
python manage.py rebac sync       # parses permissions.zed → Schema* tables
```

`Post.objects.with_actor(request.user).all()` now returns only posts the user is `owner` of. Granting access:

```python
from rebac import ObjectRef, RelationshipTuple, SubjectRef, write_relationships

write_relationships([
    RelationshipTuple(
        resource=ObjectRef("blog/post", str(post.pk)),
        relation="owner",
        subject=SubjectRef.of("auth/user", str(user.pk)),
    ),
])
```

---

## Required headers

Every `.zed` file must declare three headers. They're parsed as comments by SpiceDB but consumed as metadata by `django-zed-rebac`:

| Header | Required | Purpose |
|---|---|---|
| `// @rebac_package: <name>` | yes | Stable package identity. Used as `package` in `PackageManagedRecord` rows. |
| `// @rebac_package_version: <semver>` | yes | Track upgrades; informational. |
| `// @rebac_schema_revision: <int>` | yes | Bump on any schema change. Drives `noupdate=True` upgrade logic — admin edits to overrides are preserved unless the revision increments. |

The build refuses to run with missing headers (`rebac.E010`).

---

## Schema language quick reference

`.zed` files use SpiceDB's schema language. Three top-level forms:

```zed
// Definition — a resource or subject type.
definition blog/post {
    relation owner:  auth/user
    relation viewer: auth/user | auth/group#member

    permission read  = owner + viewer
    permission write = owner
}

// Caveat — a CEL expression evaluated against runtime context.
caveat ip_in_cidr(ip ipaddress, cidr string) {
    ip.in_cidr(cidr)
}

// Directive — a build-time switch (typechecking is auto-emitted).
use expiration
```

**Type identifiers** — `<namespace>/<name>` (e.g. `blog/post`, `auth/user`, `mcp/tool/edit`). Slashes are part of the identifier; no spaces.

**Relations** — typed edges from the definition to subject types. The right-hand side is a `|`-separated union:

```zed
relation viewer: auth/user                              // single type
relation viewer: auth/user | auth/group#member          // union with subject set
relation viewer: auth/user | auth/user:*                // union with wildcard
relation viewer: auth/user with ip_in_cidr              // with caveat
```

Structural relations that already exist as Django fields can be
declared as field-backed:

```zed
definition blog/post {
    relation folder: blog/folder // rebac:field=folder

    permission read = folder->read
}
```

For `LocalBackend`, `post#folder` is read from `Post.folder` directly instead
of from a duplicate `Relationship` row. Tuple writes/deletes for that relation
raise `SchemaError`; update the Django field instead. Field-backed relations
must point at exactly one concrete resource type: no subject sets, wildcards,
specific ids, caveats, or expiration. The `rebac.E009` system check verifies
that the named field exists and points at the schema's declared type. The
`rebac build-zed` output omits the comment directive, so the emitted schema
remains valid SpiceDB `.zed`.

Forward, reverse, and many-to-many paths use the same declaration. Optional
filters are anchored on the **source model**, including the through row:

```zed
relation member: auth/user // rebac:field={"path":"roster__user","filters":{"roster__active":true,"roster__role":"editor"}}
```

The target predicate and filters share one Django join. An active editor's
roster row cannot accidentally authorize a different user's inactive row.
Filter values are JSON scalars; Django validates the complete lookup paths.
Both direct checks and lazy queryset scopes read the current rows, including
changes made through bulk updates or the M2M manager.

Before inserting a new model, the Django `create()`, `save()`, and
`bulk_create()` gates project direct forward `ForeignKey` and `OneToOneField`
backings from the constructed candidate into `check_new()`. Python defaults
are therefore visible to `permission create = parent->write`, and each bulk
row is checked before any insert. Reverse, many-to-many, filtered, and
database-default relations are not resolved on an unsaved candidate and fail
closed; authorize those shapes through an explicit checked command after their
relationship facts exist.
Adding REBAC model instances are insert-only, including candidates with an
explicit primary key. Load an existing row before updating it; a constructed
candidate cannot turn a successful create preflight into an update.
Actor-scoped multi-table child creation also inserts every parent table and
therefore fails if a parent row already exists. Existing-parent attachment is a
trusted bypass or application-command operation because it can update parent
fields and needs a separate parent write decision.

Source and target identities may be virtual scalar fields, such as a public ID
encoded from the existing primary key. The field must support SQL projection
and exact/`in` lookups; Django owns both lookup preparation and result
conversion. A separate identity column or tuple-ID migration is unnecessary.
Properties without an ORM field and composite identities are not supported.
Loaded instances must expose a nonempty identity; model reference resolution
rejects `None` and empty strings rather than constructing a shared invalid ID.
The ordinary `pk` identity also works on a multi-table child whose primary key
is a Django parent link. An explicit relation ID attribute such as
`parent_ptr_id` is scalar; the corresponding `parent_ptr` model-object accessor
is not a valid identity.

An **attribute backing** derives virtual container membership from a subject
column. The single allowed subject type selects its Django model:

```zed
definition accounts/kind {
    relation member: auth/user // rebac:attribute={"field":"kind"}
    relation active_member: auth/user // rebac:attribute={"field":"kind","filters":{"is_active":true}}
}

definition platform/role {
    relation member: auth/user // rebac:attribute={"field":"is_superuser","resource":"admin","value":true}
}
```

Without `resource`/`value`, the column value is the container ID. With both,
the declared comparison applies only to that fixed container; other IDs on
the same relation retain stored tuples. Tuple writes/deletes against the live
container are rejected. The subject column is the writer; no reconciliation
command is needed. Attribute filters apply to the subject model.

Two rules keep every read path in agreement. A dynamic container is named by
the column value's canonical Python spelling only (`"1"` is integer container
`1`; `"01"` is nothing). Text attribute columns need a deterministic,
case-sensitive collation, because the lazy queryset scope compares them in SQL
while direct checks compare exact strings; see ARCHITECTURE.md § Field-backed
structural relations.

**Anti-pattern:** do not derive ownership from an audit column such as
`created_by` (`relation owner: auth/user // rebac:attribute={"field":"created_by"}`).
Ownership must be transferable and revocable independently of who wrote the
row; keep it an explicit `owner` tuple (see ARCHITECTURE.md § No implicit
"owner from `create_uid`"). Attribute backing is for genuine membership
attributes such as a kind, plan or role flag.

Live ORM backing is implemented by `LocalBackend` in either storage mode.
Exporting valid Zed does not project the derived edges into remote SpiceDB;
until the roadmap projector ships, every backed relation holds no edges under
`REBAC_BACKEND = "spicedb"` (ARCHITECTURE.md lists the projection burden per
backing kind).

A relation can instead be declared **const-backed** — resolving to one fixed
object id for *every* row of the declaring type, with no stored tuple and no
model field:

```zed
definition blog/post {
    relation owner: auth/user             // rebac:field=author
    relation admin: platform/role            // rebac:const=admin

    permission read = owner + admin->member
}
```

Here `post#admin` resolves to `platform/role:admin` for every post, so
`admin->member` is "is the actor a member of `platform/role:admin`?" — answered
from the single role-membership tuples, never a per-post grant. This is the
schema-level "static relationship" SpiceDB never shipped (issues #346 / #1266);
it is the idiomatic way to express GCP-IAM's "admin at a scope covers every
resource under it" without a container model. The same single-concrete-type
constraints as field-backing apply, and `rebac.E009` verifies the declaring
type has a Django model (the target type need not — it is typically a virtual
role namespace such as `platform/role`). In reverse (`accessible`), a const arrow
returns *every* row of the source type when the constant target grants access —
the intended "covers any `<type>`" semantics. Like field-backing, this is a
`LocalBackend` synthesis with no SpiceDB equivalent; a SpiceDB backend would
need the edge materialised as tuples.

Create preflight (`rebac.check_new`) injects const-backed relations from the
schema into its virtual tuple overlay. For the `admin` relation above, a create
check behaves as if the not-yet-persisted post already carried
`#admin @ platform/role:admin`, then evaluates `admin->member` through the real
relationship store. Callers must not supply virtual tuples for const-backed
relation names; those relations are synthetic schema facts, and
`check_new()` raises `SchemaError` for non-empty caller entries on them.

**Permissions** — computed expressions over relations:

```zed
permission read = owner                          // direct
permission read = owner + viewer                 // union
permission read = owner & published              // intersection
permission read = owner - banned                 // exclusion
permission read = owner + parent->read           // arrow (recurse)
```

Field gates are ordinary permissions whose names follow
`<verb>__<field>`:

```zed
definition hr/employee {
    relation owner: auth/user
    relation manager: auth/user

    permission read = owner + manager
    permission write = owner + manager
    permission read__salary = owner
    permission write__salary = owner
}
```

`write__<field>` gates are enforced on instance saves and bulk updates. When
`REBAC_FIELD_READ_MODE` is set to `"redact"` or `"omit"`, `read__<field>` gates
are enforced after queryset materialisation: denied fields are set to `None`,
and `"omit"` additionally records `_rebac_omitted_fields` for serializers that
drop keys instead of emitting `null`. The default mode is `"allow"` for
backwards compatibility. Projection querysets that would return a gated field
directly (`.values("salary")`, `.values_list("salary", flat=True)`, or bare
`.values()`) fail closed in enforced modes; materialise model instances or
project only ungated fields.

Two built-in actor terms may appear directly in permission
expressions:

```zed
definition auth/user {
    permission credential_lookup = anonymous + authenticated
}
```

`anonymous` matches the canonical anonymous SubjectRef typed by
`REBAC_ANONYMOUS_TYPE` (default `auth/anonymous:*`).
`authenticated` matches any resolved non-anonymous subject
(`auth/user:<id>`, `auth/service:<id>`, `auth/apikey:<id>`, and
similar). They are schema-level grants: do not declare
`definition anonymous`, `definition authenticated`, `relation
anonymous: ...`, `relation authenticated: ...`, or relationship rows
whose subject type is either built-in actor. To write
relationship-shaped grants for unauthenticated readers, type the
subject as the configured anonymous type (e.g. `viewer: auth/anonymous:*`)
— see `REBAC_ANONYMOUS_TYPE` in the settings catalog.

**Operators**:

| Op | Meaning |
|---|---|
| `+` | union (binds tightest) |
| `&` | intersection |
| `-` | exclusion (binds loosest) |
| `->` | arrow — walk to the named relation, then check the named permission there |
| `:*` | wildcard — "any subject of this type" |
| `#<rel>` | subject set — "anyone with `<rel>` on this object" |

---

## Operator precedence — the one footgun

SpiceDB's expression precedence is **different from most languages**. `+` binds tightest, `&` next, `-` loosest. So `a + b & c` means `(a + b) & c`. **Always parenthesise** in compound expressions:

```zed
// ❌ Subtle. Means (owner + editor) & published.
permission read = owner + editor & published

// ✅ Explicit.
permission read = (owner + editor) & published
```

The build emits `use typechecking` automatically — that catches *type* errors (e.g. intersecting two mutually-exclusive subject types) but not *precedence* errors. Those are your responsibility.

---

## Patterns by scenario

### Users and groups

`auth/user` and `auth/group` are the default actor type labels for Django users
and groups. Automatic base-schema emission is planned but not implemented;
include these definitions once in an application schema when using them:

```zed
// Include once in your application schema
definition auth/user {}

definition auth/group {
    relation member: auth/user | auth/group#member
}
```

`auth/group#member` is a **subject set** — "anyone who is a `member` of this group". Use it to grant a relation to all group members at once:

```zed
// blog/permissions.zed
definition blog/post {
    relation owner:  auth/user
    relation viewer: auth/user | auth/group#member

    permission read  = owner + viewer
    permission write = owner
}
```

To grant `viewer` to every member of group `editors`:

```python
write_relationships([
    RelationshipTuple(
        resource=ObjectRef("blog/post", str(post.pk)),
        relation="viewer",
        subject=SubjectRef(
            object=ObjectRef("auth/group", str(editors.pk)),
            optional_relation="member",
        ),
    ),
])
```

#### Choosing the membership owner

Use `rebac.memberships` when relationship tuples are the membership store.
If membership already lives in a Django M2M, declare its path as field backing
on the container instead. These are alternative owners for the same relation;
do not mirror one into the other. The unused `REBAC_SYNC_DJANGO_GROUPS` setting
has been removed.

#### Public read access

```zed
definition blog/page {
    relation editor:        auth/user
    relation public_viewer: auth/user | auth/user:*

    permission read  = editor + public_viewer
    permission write = editor
}
```

```python
# Make a page world-readable
write_relationships([
    RelationshipTuple(
        resource=ObjectRef("blog/page", str(page.pk)),
        relation="public_viewer",
        subject=SubjectRef.of("auth/user", "*"),
    ),
])
```

**Wildcard rules:**

- Only on read-shaped permissions. The schema doctor (`rebac.W001`) emits a warning when a wildcard relation participates in a `write`/`delete`/`create` permission.
- Cannot be transitively included — `auth/user:*` cannot flow through a subject set like `auth/group#member`. SpiceDB rejects this at `WriteSchema` time.
- Cheap for `CheckPermission`; expensive for `LookupSubjects`. Prefer narrow shares when listing matters.

### Hierarchical resources (folders → files)

The classic recursive permission. `read` on a file = `read` on its parent folder.

```zed
// storage/permissions.zed
// @rebac_package: storage
// @rebac_package_version: 0.1.0
// @rebac_schema_revision: 1

definition storage/folder {
    relation owner:  auth/user
    relation viewer: auth/user | auth/group#member
    relation parent: storage/folder

    permission read  = owner + viewer + parent->read
    permission write = owner + parent->write
}

definition storage/file {
    relation owner:  auth/user
    relation folder: storage/folder

    permission read   = owner + folder->read
    permission write  = owner + folder->write
    permission delete = owner + folder->write
}
```

The arrow `parent->read` reads as "walk to the parent, then evaluate `read` over there". Recursion is bounded by `REBAC_DEPTH_LIMIT` (default 8). Deeper trees raise `PermissionDepthExceeded`; raise the limit carefully or move to the future `SpiceDBBackend` once it lands when you need deeper walks.

**Multi-hop arrows are not supported.** You cannot write `parent->parent->read`. The pattern above works because `read` itself recurses through `parent->read` — that's how multi-hop traversal is expressed in SpiceDB.

**Cycles in data.** SpiceDB doesn't reject `folder:A#parent @ folder:A`. The dispatcher hits the depth limit and silently returns `false`. Validate at the application layer:

```python
def assign_parent(self, new_parent):
    if self.is_descendant_of(new_parent):
        raise ValidationError("would create cycle")
    self.parent = new_parent
```

### Time-bound access

Modern SpiceDB schemas (v1.40+) support relationship expiration as a first-class feature. **Prefer this over the older "current_time as a caveat" pattern** — expiration garbage-collects automatically; caveats don't.

```zed
// blog/permissions.zed
use expiration

definition blog/post {
    relation owner:            auth/user
    relation temporary_viewer: auth/user with expiration

    permission read = owner + temporary_viewer
}
```

```python
from datetime import datetime, timedelta, timezone

write_relationships([
    RelationshipTuple(
        resource=ObjectRef("blog/post", str(post.pk)),
        relation="temporary_viewer",
        subject=SubjectRef.of("auth/user", str(user.pk)),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    ),
])
```

After 24 hours, `LocalBackend` excludes the row from query results; `rebac.gc.expire_relationships` deletes it within `REBAC_GC_INTERVAL_SECONDS` (default 300).

### Conditional access (caveats)

Caveats are CEL expressions evaluated against runtime context at check time. Use them for **dynamic** constraints — IP allow-lists, business hours, on-call membership. For static time bounds, prefer `use expiration`.

```zed
// docs/permissions.zed
caveat ip_in_cidr(ip ipaddress, cidr string) {
    ip.in_cidr(cidr)
}

caveat during_business_hours(hour int, start int, end int) {
    hour >= start && hour <= end
}

definition docs/sensitive {
    relation owner:                auth/user
    relation ip_restricted_viewer: auth/user with ip_in_cidr

    permission read = owner + ip_restricted_viewer
}
```

When writing the relationship, supply the static parameters (caveat *context*):

```python
write_relationships([
    RelationshipTuple(
        resource=ObjectRef("docs/sensitive", str(doc.pk)),
        relation="ip_restricted_viewer",
        subject=SubjectRef.of("auth/user", str(user.pk)),
        caveat_name="ip_in_cidr",
        caveat_context={"cidr": "10.0.0.0/8"},
    ),
])
```

Stored caveat context takes precedence over request context. A caller can supply
the missing `ip`, but cannot replace the pinned `cidr` during a permission check.
The `with ip_in_cidr` declaration requires that caveat on every viewer tuple;
allow both shapes explicitly with `auth/user | auth/user with ip_in_cidr` if
uncaveated grants are also intended.

When checking, supply the runtime parameter:

```python
result = backend().check_access(
    subject=to_subject_ref(user),
    action="read",
    resource=ObjectRef("docs/sensitive", str(doc.pk)),
    context={"ip": request.META["REMOTE_ADDR"]},
)
# result == HAS_PERMISSION if user.ip is in 10.0.0.0/8
```

If you check WITHOUT supplying `ip`, the result is `CONDITIONAL_PERMISSION(missing=["ip"])`. The application can re-check with the missing field — useful for two-pass evaluation (cheap relationship check + expensive context resolution).

**`LocalBackend` caveat support.** Backed by [`cel-python`](https://pypi.org/project/cel-python/). Most CEL types work out of the box (`int`, `string`, `bool`, `list`, `map`, `timestamp`, `duration`). The `ipaddress` type is **not** in `cel-python`'s built-ins — `LocalBackend` raises `CaveatUnsupportedError`. Rewrite the caveat to take strings and do CIDR matching server-side, or move to the future `SpiceDBBackend` once it lands.

### MCP tools as resources

MCP (Model Context Protocol) tools can be modeled as first-class resources.
Permissions on them gate which tools an actor can invoke. Gate a tool with the
shipped `rebac.mcp.rebac_mcp_tool` decorator (see
[proposal 0004](./proposals/0004-mcp-tool-integration.md)).

#### Pattern 1 — one resource type per tool

```zed
// mcp/permissions.zed
definition mcp/tool/query_posts {
    relation invoker: auth/user | agents/agent#operator
    permission invoke = invoker
}

definition mcp/tool/edit_post {
    relation invoker: auth/user        // narrower — no agent invocation
    permission invoke = invoker
}
```

Wire the tool with `rebac_mcp_tool` — it resolves the actor from the request
context, checks `invoke` on the singleton `mcp/tool/query_posts:*`, and runs the
body only on allow:

```python
from rebac.mcp import rebac_mcp_tool

@mcp.tool
@rebac_mcp_tool(resource_type="mcp/tool/query_posts", action="invoke")
async def query_posts(query: str, ctx: Context = CurrentContext()) -> list[dict]:
    ...   # runs only if the resolved actor may invoke the tool
```

Granting the right to invoke:

```python
write_relationships([
    RelationshipTuple(
        resource=ObjectRef("mcp/tool/query_posts", "*"),  # "*" = the tool itself
        relation="invoker",
        subject=SubjectRef.of("auth/user", str(user.pk)),
    ),
])
```

#### Pattern 2 — group tools into capability categories

For larger projects, one resource type per tool gets unwieldy. Group them:

```zed
definition mcp/capability {
    relation granted_to: auth/user | agents/agent#operator
    permission use = granted_to
}
```

Tag each tool with the capability it requires via `id_arg`. The keyword-only
`_capability` argument carries the id; `hide_id_arg=True` requests that the
adapter drop it from the published tool schema (a documented no-op under
FastMCP 1.27, which exposes no schema-filtering hook — keeping it keyword-only
already keeps it off the model-facing surface):

```python
from rebac.mcp import rebac_mcp_tool

@mcp.tool
@rebac_mcp_tool(
    resource_type="mcp/capability",
    action="use",
    id_arg="_capability",
    hide_id_arg=True,
)
async def query_posts(
    query: str,
    ctx: Context = CurrentContext(),
    *,
    _capability: str = "blog.read",
) -> list[dict]:
    ...   # checks `use` on mcp/capability:blog.read
```

Capabilities form a flat namespace (`blog.read`, `blog.write`, `admin.users`)
so admins can grant them in bulk.

### Agents acting on behalf of users (the Grant pattern)

Applications can represent delegation as grant objects and combine delegation
and capability conditions in their permission expressions. The graph must
declare those conditions explicitly: the engine does not impersonate an owner
or automatically inherit the owner's permissions.

#### Definitions live in YOUR `agents` app, not in the plugin

`agents/agent` and `agents/grant` are **not** auto-emitted. They live in an
application you ship. Declare any User/Group definitions your schema uses too;
automatic base-schema emission is not implemented.

A typical `agents/permissions.zed`:

```zed
// agents/permissions.zed — in YOUR agents app, not in rebac
// @rebac_package: agents
// @rebac_package_version: 0.1.0
// @rebac_schema_revision: 1

definition agents/agent {
    relation operator:       auth/user
    relation has_capability: agents/capability
}

definition agents/capability {}        // marker type

definition agents/grant {
    relation valid: auth/user
    relation agent: agents/agent

    permission active = valid
}
```

A `Grant` row records "user U has delegated to agent A". This illustrative
`active` permission follows the `valid` relation to authorize the checked user;
the `agent` relation records which agent the grant belongs to. Consumer schemas
that combine several arms in
`active` must make those arms accept the same checked subject type. For example,
`user & agent` cannot match because those relations accept different types.

#### Targeting resources from grants

Store the grant object in its own relation, then follow an arrow to `active`:

```zed
// blog/permissions.zed
definition blog/post {
    relation owner:  auth/user
    relation viewer: auth/user | auth/group#member
    relation viewer_grant: agents/grant

    permission read  = owner + viewer + viewer_grant->active
    permission write = owner                         // only owners can write
}
```

The resource tuple names `agents/grant:G123` directly. The arrow evaluates the
grant's `active` permission for the subject being checked. Relationship subjects
may use only relation suffixes; a permission such as `#active` belongs after an
arrow in the resource permission.

#### Bounding the agent further by capability

To require a capability as well as delegation for the same checked user,
extend the grant's permission with an explicit capability relation:

```zed
definition agents/capability {
    relation member: auth/user
    permission use = member
}

definition agents/grant {
    relation valid: auth/user
    relation capability: agents/capability

    permission active = valid & capability->use
}
```

The resource's `viewer_grant->active` arrow now requires both conditions.
Creating a grant alone does not establish capability membership. Applications
own which grants, capabilities and resource links a caller may create.

#### Per-grant exceptions via caveats

Sometimes a grant should be valid only in a window, an IP range, or for specific model kinds:

```zed
caveat grant_constraints(now timestamp, expires_at timestamp, model_kind string, allowed_kinds list<string>) {
    now < expires_at && model_kind in allowed_kinds
}
```

Declare `relation valid: auth/user with grant_constraints` on the grant before
writing a caveated membership:

```python
write_relationships([
    RelationshipTuple(
        resource=ObjectRef("agents/grant", str(grant.pk)),
        relation="valid",
        subject=SubjectRef.of("auth/user", str(user.pk)),
        caveat_name="grant_constraints",
        caveat_context={
            "expires_at": "2026-12-31T23:59:59Z",
            "allowed_kinds": ["claude_internal", "claude_external"],
        },
    ),
])
```

When the agent invokes a tool, the request passes `now` and `model_kind` as runtime context — `active` only resolves if both context-side checks pass.

#### Querying as an agent

`with_actor(actor)` is the generic verb; `as_agent(agent, on_behalf_of=user)`
constructs the conventional `agents/grant:<id>#valid` subject. In schemas using
that shorthand, `valid` must be a declared relation. The application must
authorize this subject shape explicitly; constructing it neither impersonates
the requester nor copies the requester's grants. The user-subject examples
above illustrate permission arrows, not an automatic mapping from this grant
subject back to a user.

```python
# Common case: HTTP request from a Django user
Post.objects.as_user(request.user)
# expands to: Post.objects.with_actor(to_subject_ref(request.user))
#                       → SubjectRef(auth/user:<id>)

# Agent acting on behalf of a user (canonical Grant pattern)
Post.objects.as_agent(agent, on_behalf_of=request.user)
# expands to: Post.objects.with_actor(grant_subject_ref(agent, request.user))
#                       → SubjectRef(agents/grant:<grant_id>#valid)

# Or pass any SubjectRef directly:
Post.objects.with_actor(SubjectRef.of("agents/grant", str(grant.pk)))
Post.objects.with_actor(SubjectRef.of("auth/apikey", apikey.public_id))

# Anything @rebac_subject-decorated also resolves automatically:
Post.objects.with_actor(my_apikey_instance)
```

Inside an MCP tool gated by `rebac_mcp_tool`, the actor comes from trusted
request context or the configured resolver. It may be a user, grant or another
canonical subject. The decorator opens `actor_context(actor)` around the body;
the ORM uses that actor's declared permissions:

```python
from rebac.mcp import rebac_mcp_tool

@mcp.tool
@rebac_mcp_tool(resource_type="blog/post", action="write", id_arg="post_id")
async def edit_post(post_id: str, body: str, ctx: Context = CurrentContext()) -> dict:
    post = await Post.objects.aget(pk=post_id)   # scoped to the resolved actor
    post.body = body
    await post.asave()                           # re-checks `write` against that actor
    return {"ok": True}
```

`with_actor` does NOT mutate `current_actor()` — the originating actor (typically the request user) is preserved for audit. See [ARCHITECTURE.md § `with_actor` vs `sudo`](./ARCHITECTURE.md#with_actor-vs-sudo--distinct-verbs).

### Celery tasks acting on behalf of users

Tasks must restore an actor explicitly with `actor_context()` or `.with_actor()`; automatic propagation is not shipped (see [ARCHITECTURE.md § Celery](./ARCHITECTURE.md#celery)). The schema authoring is the same as for HTTP — you don't declare separate "task" resource types unless tasks themselves are gated.

If they are (e.g., "only ops users can run reindex"), declare them as resources:

```zed
// ops/permissions.zed
definition celery/task/reindex_posts {
    relation runner: auth/user
    permission invoke = runner
}
```

```python
from rebac import require_permission

@shared_task
@require_permission(
    action="invoke",
    resource_type="celery/task/reindex_posts",
    resource_id="*",
)
def reindex_posts():
    ...
```

The example requires a task wrapper that opens `actor_context()` from a trusted
producer-supplied actor before invoking the decorated function. Without that
scope the permission decorator denies the call.

### DRF viewsets

DRF integration requires NO additional schema authoring. The model's `rebac_resource_type` is the source of truth; `RebacPermission` and `RebacFilterBackend` consult the schema:

```python
from rebac.drf import RebacPermission, RebacFilterBackend

class PostViewSet(viewsets.ModelViewSet):
    queryset           = Post.objects.all()
    serializer_class   = PostSerializer
    permission_classes = [RebacPermission]
    filter_backends    = [RebacFilterBackend]
```

Default action map: `list`/`retrieve` → `read`, `create` → `create`, `update`/`partial_update` → `write`, `destroy` → `delete`. Customise by subclassing:

```python
class PublishablePerm(RebacPermission):
    action_map = {**RebacPermission.action_map, "publish": "publish"}

class PostViewSet(viewsets.ModelViewSet):
    permission_classes = [PublishablePerm]

    @action(detail=True, methods=["post"])
    @require_permission("publish")
    def publish(self, request, pk):
        ...
```

### Plain Python entities (non-ORM)

Anything with a stable string ID can be a resource. Declare the type in any app's `permissions.zed`, then register the Python class with `@rebac_resource`:

```zed
// storage/permissions.zed (excerpt)
definition storage/s3_prefix {
    relation reader: auth/user
    permission read = reader
}
```

```python
from rebac import rebac_resource, backend, to_object_ref, to_subject_ref

@rebac_resource(type="storage/s3_prefix", id_attr="prefix")
class S3Prefix:
    def __init__(self, prefix: str):
        self.prefix = prefix


prefix = S3Prefix("uploads/2026/")
result = backend().check_access(
    subject=to_subject_ref(user),
    action="read",
    resource=to_object_ref(prefix),     # uses id_attr=prefix
)
```

This makes REBAC available for any resource boundary your project has, not just Django ORM rows.

### Multi-tenant scoping (soft tenants)

For projects where one Django DB serves multiple tenants, set `REBAC_TYPE_PREFIX` per request:

```python
REBAC_TYPE_PREFIX = "tenant_acme/"
```

Every generated identity carries the prefix: model resource types become
`tenant_acme/blog/post`, and the configured user, group and anonymous subject
types plus `@rebac_subject` types become `tenant_acme/auth/user`,
`tenant_acme/auth/group`, and so on. Declare the prefixed types in the tenant
schema. Relationships from one tenant cannot be referenced by another.

For hard-tenant isolation (separate databases or schemas), use `django-tenants` and let each tenant own its own `Relationship` table — no schema changes needed.

---

## Composing schemas across packages

The build walks every installed app, parses each app's `rebac_schema` file, and composes them into a single `effective.zed`. Sort is alphabetical by resource type — deterministic across runs.

**Cross-app references are first-class.** A `blog/post` definition in one app can name `storage/folder` from another:

```zed
// blog/permissions.zed
definition blog/post {
    relation folder: storage/folder         // declared in storage/permissions.zed
    relation owner:  auth/user
    permission read = owner + folder->read
}
```

The build raises a clear error if `storage/folder` isn't defined anywhere. **Cycles between apps are rejected** at build time — the dependency DAG enforces ordering (`blog` depends on `storage`; `storage` depending back on `blog` is a build error).

---

## Anti-patterns to avoid

### 1. Don't put `auth/user:*` in write-shaped permissions

```zed
// ❌ Anyone can edit anything.
definition blog/post {
    relation editor: auth/user | auth/user:*
    permission write = editor
}
```

The schema doctor (`rebac.W001`) warns when a wildcard relation participates in a `write`/`delete`/`create` permission. Wildcards are for read-shaped, public-share patterns only.

### 2. Don't smuggle wildcards transitively

```zed
// ❌ SpiceDB rejects this at WriteSchema time.
relation viewer: auth/user:* | auth/group#member
```

Error: "wildcard relations cannot be transitively included." Wildcards cannot flow through subject sets.

### 3. Don't write circular `parent` relations in your data

The schema is fine; the data corrupts the graph:

```
folder:A#parent @ folder:B
folder:B#parent @ folder:A   // ← cycle
```

SpiceDB hits the depth limit and silently returns `false`. Validate at the application layer (see [§ Hierarchical resources](#hierarchical-resources-folders--files)).

### 4. Don't intersect mutually-exclusive subject types

```zed
// ❌ user is type auth/user; admin is type billing/admin.
// This permission is never satisfiable.
permission edit = user & admin
```

The auto-emitted `use typechecking` directive catches this at `WriteSchema` time. The plugin runs the same typecheck locally and refuses to emit the schema.

### 5. Don't forget operator parens

```zed
// ❌ Means (a + b) & c. Probably not what you wanted.
permission read = a + b & c

// ✅
permission read = a + (b & c)
```

See [§ Operator precedence](#operator-precedence--the-one-footgun) above.

### 6. Don't model agents as principals — use the Grant pattern

```zed
// ❌ Bypasses the user's grants entirely.
definition blog/post {
    relation agent_viewer: agents/agent
    permission read = owner + agent_viewer
}
```

Store an `agents/grant` object directly on the resource and evaluate its
permission with `grant_relation->active`. The grant's permission must explicitly
encode the consumer's delegation policy for the subject being checked.

### 7. Don't omit the required headers

The build refuses (`rebac.E010`) if any of `// @rebac_package`, `// @rebac_package_version`, `// @rebac_schema_revision` is missing. Bump the revision number whenever you change the schema, even for cosmetic changes — the upgrade-safety machinery uses it to decide whether admin overrides survive.

---

## Patterns library — copyable starting points

### Pattern A — RBAC (role-based access control)

```zed
definition docs/document {
    relation admin:  auth/user
    relation writer: auth/user
    relation reader: auth/user | auth/group#member

    permission read   = admin + writer + reader
    permission write  = admin + writer
    permission delete = admin
}
```

### Pattern B — Hierarchical resources

```zed
definition docs/folder {
    relation owner:  auth/user
    relation parent: docs/folder

    permission read  = owner + parent->read
    permission write = owner + parent->write
}

definition docs/document {
    relation owner:  auth/user
    relation folder: docs/folder

    permission read  = owner + folder->read
    permission write = owner + folder->write
}
```

### Pattern C — Ownership + group sharing

```zed
definition blog/post {
    relation owner:        auth/user
    relation group_member: auth/group#member

    permission read  = owner + group_member
    permission write = owner
}
```

### Pattern D — Public-readable, group-writable

```zed
definition blog/page {
    relation editor:        auth/user
    relation public_viewer: auth/user | auth/user:*

    permission read  = editor + public_viewer
    permission write = editor
}
```

### Pattern E — Time-bound shared access (expiration)

```zed
use expiration

definition docs/document {
    relation owner:            auth/user
    relation temporary_viewer: auth/user with expiration

    permission read = owner + temporary_viewer
}
```

### Pattern F — Conditional access (caveat)

```zed
caveat tenant_match(user_tenant string, doc_tenant string) {
    user_tenant == doc_tenant
}

definition docs/document {
    relation viewer: auth/user with tenant_match
    permission read = viewer
}
```

### Pattern G — Agent acting on behalf of user (Grant pattern)

```zed
definition docs/document {
    relation owner:  auth/user
    relation viewer: auth/user | auth/group#member
    relation viewer_grant: agents/grant

    permission read  = owner + viewer + viewer_grant->active
    permission write = owner                       // grant alone does not permit writes
}
```

### Pattern H — MCP tool gated by capability

```zed
definition mcp/capability {
    relation granted_to: auth/user | agents/agent#operator
    permission use = granted_to
}
```

```python
from rebac.mcp import rebac_mcp_tool

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
):
    ...
```

### Pattern I — Celery task gated by role

Restore the trusted producer-supplied actor in `actor_context()` before calling
the decorated body; automatic task propagation is not shipped.

```zed
definition celery/task/reindex {
    relation runner: auth/user
    permission invoke = runner
}
```

```python
@shared_task
@require_permission(action="invoke", resource_type="celery/task/reindex", resource_id="*")
def reindex():
    ...
```

### Pattern J — Public Python entity (S3 prefix)

```zed
definition storage/s3_prefix {
    relation reader: auth/user | auth/user:*
    permission read = reader
}
```

```python
@rebac_resource(type="storage/s3_prefix", id_attr="prefix")
class S3Prefix:
    def __init__(self, prefix: str):
        self.prefix = prefix
```

---

## Reference — supported subset of the SpiceDB schema language

The plugin parses the SpiceDB-canonical subset relevant to Django projects:

- `definition` blocks (top-level)
- `relation` declarations with type unions, subject sets, wildcards, `with <caveat>`, `with expiration`
- `permission` expressions: `+`, `&`, `-`, arrows (`->`)
- `caveat` blocks with parameters and CEL expressions
- Directives: `use typechecking` (auto-emitted), `use expiration`

NOT yet supported by the parser (raw `.zed` import + `WriteSchema` only when running against SpiceDB):

- `use import` / composable schemas — multi-package composition is handled by the plugin's app-walking build instead
- `use self` shortcut — niche; raise an issue if needed
- `nil` type — almost never useful

For the full upstream language, see the [authzed schema reference](https://authzed.com/docs/spicedb/concepts/schema).

---

## Where to look next

- [ARCHITECTURE.md](./ARCHITECTURE.md) — system design: backends, public API, settings, surface integrations, determinism, testing, roadmap.
- [SpiceDB schema docs](https://authzed.com/docs/spicedb/concepts/schema) — upstream language reference.
- [Authzed "Secure AI Agents" tutorial](https://authzed.com/docs/spicedb/tutorials/ai-agent-authorization) — the canonical Grant-pattern walkthrough.
- [Zanzibar paper](https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/) — the conceptual origin.
