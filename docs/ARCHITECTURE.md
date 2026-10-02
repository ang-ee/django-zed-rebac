# `django-zed-rebac` — Architecture

> Status: **alpha implementation guide** — specifies the release after 0.24.2 (permissions compiled to queries).
> Last updated: 2026-10-02
> Audience: Django integrators evaluating fit, contributors, framework authors building on top.
>
> Companion docs:
> - [ZED.md](./ZED.md) — schema authoring guide. How to write `permissions.zed` for users, groups, agents, Celery tasks, MCP tools, and arbitrary entities.

---

## TL;DR

`django-zed-rebac` is a **drop-in REBAC engine** for Django 6.0 projects. Add it to `INSTALLED_APPS`, declare your authorisation schema in a per-package `permissions.zed` file, and every queryset, save, and method call is gated against the effective user — without rewriting your viewsets.

Core capabilities:

- **The SpiceDB schema language**, hand-authored as `.zed` files shipped per package. Loaded into DB tables on install/upgrade with `noupdate=True` semantics that preserve admin edits.
- **A pluggable backend boundary:**
  - `LocalBackend` — pure Django. A permission is compiled to a query over the application's own tables and the relationship table, so the library stores no row per application row and a write is visible to the next read. Zero infrastructure; no build step.
  - `SpiceDBBackend` — roadmap adapter for the official [`authzed`](https://pypi.org/project/authzed/) Python client. The class is present as a clear stub, not a supported runtime backend yet.
- **A `RebacMixin` model mixin** that, by inclusion, replaces `Manager.objects` with a permission-aware variant. Every read scopes to the effective user; every write checks before SQL is issued.
- **Three storage tiers, three editors:**
  - **Tier 1 — Structural.** Per-package `permissions.zed`, code-shipped, DB-loaded.
  - **Tier 2 — Override.** Admin-editable tweaks on top of the package baseline.
  - **Tier 3 — Relationship.** The actual edges in `Relationship` rows.
- **One unified check API:** `check_access(op)` / `has_access(op)` / `accessible(op)` (borrowed from Odoo 18's PR #179148 unification). No model-level vs record-level split at the call site.
- **`Model.objects.with_actor(actor)` / `instance.sudo(reason=...)`** — distinct verbs for distinct intents. The actor is any `SubjectRef` — a Django `User`, a registered `Agent`, an `agents/grant` (agent-acting-on-behalf-of-user), an `auth/apikey`, or any `@rebac_subject`-registered object. `as_user(u)` and `as_agent(agent, on_behalf_of=u)` are typed shorthands. Mandatory `reason` on bypass, originating uid preserved through bypass for audit (Odoo `env.su` / `env.user` independence).

The plugin maps Django User/Group objects onto configured subject types, defaulting to `auth/user` and `auth/group`. Applications declare every subject definition their relations reference; automatic base-schema emission is not implemented. Agent, grant, API-key and service-account definitions likewise belong to consumer apps.
- **Strict-by-default**: a queryset that escapes its actor scope raises `MissingActorError` rather than silently returning all rows.
- **Consumer-defined agent delegation:** applications can express delegation and capability conditions through grant objects, relations and permission intersections. The engine evaluates the declared graph; resolving a grant subject neither creates its relationships nor inherits the user's permissions.

What `django-zed-rebac` deliberately does **not** ship: a `User` model, auth providers, login UI, session handling, GraphQL admin endpoints. Those are orthogonal — use `django.contrib.auth` (default) or any of `django-allauth` / `dj-rest-auth` / your own. Downstream frameworks may layer on top to provide polymorphic Subject types (`auth/apikey`, `agents/agent`, `agents/grant`, …), GraphQL admin surfaces, and Grant-pattern wiring; nothing here is coupled to any specific framework.

For schema authoring, see [ZED.md](./ZED.md).

---

## Quickstart

Install, declare the schema, attach the mixin, then migrate and sync.

### 1. Install and add to `INSTALLED_APPS`

```bash
pip install django-zed-rebac
```

```python
# settings.py
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    # ...
    "rebac",
]

AUTHENTICATION_BACKENDS = [
    "rebac.backends.RebacBackend",
    "django.contrib.auth.backends.ModelBackend",
]

REBAC_BACKEND = "local"   # "spicedb" is roadmap/stubbed today
```

### 2. Ship a `permissions.zed` next to your app

```zed
// blog/permissions.zed
// @rebac_package: blog
// @rebac_package_version: 0.1.0
// @rebac_schema_revision: 1

definition blog/post {
    relation owner:  auth/user
    relation viewer: auth/user | auth/group#member | auth/user:*

    permission read   = owner + viewer
    permission write  = owner
    permission delete = owner
}
```

Add to your `AppConfig`:

```python
# blog/apps.py
class BlogConfig(AppConfig):
    name           = "blog"
    rebac_schema     = "permissions.zed"   # relative to the app's package dir
```

### 3. Mix into your model

```python
# blog/models.py
from django.db import models
from rebac import RebacMixin

class Post(RebacMixin, models.Model):
    title  = models.CharField(max_length=200)
    body   = models.TextField()
    author = models.ForeignKey("auth.User", on_delete=models.CASCADE)

    class Meta:
        rebac_resource_type = "blog/post"
```

### 4. Sync and use

```bash
python manage.py migrate              # creates the library's tables
python manage.py rebac sync           # loads permissions.zed into the schema tables
```

```python
# blog/views.py
def post_detail(request, pk):
    post = get_object_or_404(
        Post.objects.with_actor(request.user),    # generic verb
        pk=pk,
    )
    return render(request, "post.html", {"post": post})

# Equivalent shorthands for the Django-User / agent cases:
# Post.objects.as_user(request.user)
# Post.objects.as_agent(agent, on_behalf_of=request.user)
```

The same flow works in DRF, Celery tasks, GraphQL resolvers, management commands, and MCP tools. See [§ Surface integrations](#surface-integrations).

---

## Conceptual model

`django-zed-rebac` is a faithful Django port of [Google's Zanzibar paper](https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/) as implemented by [SpiceDB](https://github.com/authzed/spicedb). Five core concepts:

| Concept | What it is | Example |
|---|---|---|
| **Subject** | Who is acting. A typed reference: `subject_type:subject_id`. | `auth/user:42`, `agents/agent:claude_v3` |
| **Resource** | What is being acted upon. A typed reference. | `blog/post:99` |
| **Relation** | A typed link from a subject to a resource. Usually rows in the `Relationship` table; field-backed relations are sourced from a Django FK and const-backed relations resolve to one fixed object id. | `blog/post:99 #owner @ auth/user:42` |
| **Permission** | A computed expression over relations. Defined by the schema, never written by applications; `LocalBackend` compiles it to a query when it is read (see [Compiled permissions](#compiled-permissions--the-localbackend-read-path)). | `permission read = owner + viewer` |
| **Caveat** | A CEL expression evaluated at check time against runtime context. | `permission read = viewer with ip_in_cidr` |

Two built-in actor terms, `anonymous` and `authenticated`, may appear
directly in permission expressions. They are schema-level grants, not
relationship rows and not user-declared definitions. `anonymous`
matches the canonical anonymous SubjectRef typed by `REBAC_ANONYMOUS_TYPE`
(default `auth/anonymous:*`); `authenticated` matches any non-anonymous
resolved subject. See "Anonymous subject — built-in" below for the
typing rationale.

The fundamental check operation: `check_access(subject, action, resource, context)` returns one of:

- `HAS_PERMISSION` — granted.
- `NO_PERMISSION` — denied.
- `CONDITIONAL_PERMISSION(missing=[...])` — the schema's caveats need context that wasn't supplied. The caller may retry with additional context.

This three-state result mirrors SpiceDB exactly and is critical for layered checks (e.g., a fast first-pass without context to confirm a relationship exists, then a second pass with context to evaluate caveats).

Relationship-pinned caveat context takes precedence over request context, as in
[SpiceDB](https://authzed.com/docs/spicedb/concepts/caveats). Request parameters
may fill missing values but cannot replace stored policy constraints. A stored
relationship must match both the subject shape and the caveat name of one
declared allowed-subject alternative. Required caveats cannot be omitted;
undeclared caveats and expirations are rejected on writes and stale invalid
rows do not authorize reads. Expired relationships are absent on every graph
hop. Enumeration APIs return only unconditional grants: a conditional exclusion
must not become an allow when its missing context is omitted.

### Three storage tiers

```
┌─ Tier 1: STRUCTURAL ───────────────────────────────────────────┐
│  Source: <app>/permissions.zed (code, in PR)                    │
│  Store:  SchemaDefinition / SchemaRelation /                    │
│          SchemaPermission / SchemaCaveat                        │
│  Loader: manage.py rebac sync                               │
│  Editor: engineers via PR (admins via Tier 2)                   │
├─ Tier 2: OVERRIDE ─────────────────────────────────────────────┤
│  Source: admin actions (your app's admin UI)                    │
│  Store:  SchemaOverride                                         │
│  Loader: applied lazily on top of Tier 1                      │
│  Editor: admins                                                 │
├─ Tier 3: RELATIONSHIPS ────────────────────────────────────────┤
│  Source: signals, sharing UIs, sharing APIs, Django FK fields    │
│  Store:  Relationship, plus field-backed structural relations    │
│  Loader: written transactionally                                │
│  Editor: application code + admins                              │
└────────────────────────────────────────────────────────────────┘
```

No tier is copied. `LocalBackend` reads Tier 3 and the backed application
columns when a permission statement runs, under the policy of Tiers 1 and 2.

**Critical invariant:** Tier 1 is the only place new relation types and permission expressions can introduce graph shape. Tier 2 may tighten / loosen / disable / additively extend, but a relation referenced by an override must already exist in some package's `permissions.zed`. This protects against the "admin invents `auditor` relation, no code writes `auditor` rows, every read returns nothing in production" failure mode.

### Field-backed structural relations

A relation may be annotated with `// rebac:field=<field_name>` when the
relationship is already represented by a forward Django FK or one-to-one field:

```zed
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
```

This is library-owned projection metadata, not a new SpiceDB semantic. The
parser stores it on `Relation.backing`; `rebac sync` persists it on
`SchemaRelation.backing`; `rebac build-zed` omits it from the generated SpiceDB
schema.

`LocalBackend` reads the relation's edges from the Django column, through the
model's `_base_manager`, in the statement that evaluates the permission, so
application default managers cannot move the authorization boundary (a base
manager the model declares itself must return every row; see
[What gets installed](#what-gets-installed)). The column is the only copy of
the fact: a write to it is visible to the next read, and it is gated as
described under [Writes](#writes). Tuple
writes/deletes targeting the backed relation raise `SchemaError` with the
actionable Django field to update instead.

The same field backing supports forward, reverse, and many-to-many lookup
paths with source-model filters. Target predicates and filters are composed
in one Django `.filter()` so they constrain the same through row. Attribute
backing exposes virtual containers derived from a scalar subject column;
fixed `resource`/`value` bindings own just that container, leaving other IDs
tuple-backed. The native AST codec owns parsing and schema persistence.

The schema validator rejects backed relations with multiple subject types,
subject sets, wildcards, specific ids, caveats, or expiration. `rebac.E009`
validates every Django path, lookup, and target model. The compiler and the
write gates share these resolved backings.

An edge change requires `write` on every affected resource row whose declared
backing watches the changed column or through table, provided that resource
type declares a `write` permission. This includes reverse FK and M2M accessors,
`RebacTrackedMixin` deletes, queryset writes through an auto-created through
model, scalar predicate columns, nested field paths whose source is another
model, and both mirror rows of a symmetrical self-M2M. `RebacMixin` deletes
are not gated; the collector's CASCADE and SET_NULL rows are gated under the
ambient actor, not the actor pinned on the deleted row; instance-level
through-model writes are not gated (all three: proposal 0011). Reads see
all of these writes when they commit; what is open is their authorization.
The gate finds source rows through the unchanged
prefix of each affected path, before mutation and under the carrying or ambient
actor. Queryset owners read the stored watched columns per model, resolve
changed FK targets and reverse sources once per watched field, and check the
union of affected IDs per declaring resource type.
A through-table gate takes the source FK values of the changed
rows, then queries only declaring rows at the path prefix before the M2M hop.
Using the full path would compare target PKs to source PKs and miss edges when
the two sequences differ.
Instance sudo does not carry through a related manager. A denied
declaring resource is audited after the failed owner transaction unwinds.
Tracked models can be backing sources even when their own saves have no
resource write gate. A backing type without a permission literally named
`write`, including resource types that name it `edit` or `update`, has no
actor gate on its backing columns; consumers protect those columns with
Django permissions (proposal 0011). Attribute-backed changes check both the old and proposed
virtual container IDs when the container key changes.

Dynamic attribute containers are named by the canonical Python spelling of the
column value (`ResolvedAttributeBacking.container_id_of`); a non-canonical id
such as `"01"` for integer `1` names no container. A statement compares
attribute columns in SQL, which follows the column's database collation, while
the write gates and the Python evaluators compare strings exactly. Give attribute
columns a deterministic, case-sensitive collation (MySQL's default `*_ci`
collations are not) so the two agree. Dynamic attribute fields need a canonical
integer, text or UUID codec (`rebac.E014`). Boolean, Date and Decimal dynamic
containers are refused; a fixed `resource`/`value` anchor does not
encode its attribute value as an identity and remains supported. `rebac.W009`
warns, best-effort, about case-insensitive attribute collations.

#### Backings are `LocalBackend`-only until the projector ships

Every backing kind below is read from its columns by `LocalBackend` and omitted from
the exported `.zed`, so with `REBAC_BACKEND = "spicedb"` these relations hold
no edges until the roadmap's library-owned projector materializes them as
ordinary tuples. This is the one place the "backend swap is a configuration
change" contract (`CLAUDE.md` invariant 1) is currently conditional; the
projection burden differs per kind:

| Backing | Edges the projector must maintain |
|---|---|
| Forward FK / one-to-one (`rebac:field=folder`) | one per source row holding the FK |
| Reverse, many-to-many and filtered paths (`rebac:field={"path":...}`) | one per distinct `(source, target)` pair whose through rows satisfy the filters |
| Dynamic attribute container (`rebac:attribute={"field":...}`) | one per qualifying subject row, into the container named by its column value |
| Fixed attribute container (`rebac:attribute={...,"resource":...,"value":...}`) | one per subject row whose column matches the declared value |
| Const (`rebac:const=<id>`) | one per source row, all pointing at the fixed target |

### Const-backed (synthetic) relations

A relation may instead be annotated with `// rebac:const=<id>` to resolve to one
fixed object id for *every* row of the declaring type, with no stored tuple and
no model column:

```zed
definition blog/post {
    relation admin: platform/role // rebac:const=admin
    permission read = owner + admin->member
}
```

Every `blog/post` behaves as if it held `#admin @ platform/role:admin`, so
`admin->member` is "is the actor a member of `platform/role:admin`?" — answered from
that one role's membership rows, never a per-post grant. This is the schema-level
"static relationship" SpiceDB never shipped (issues #346 / #1266); it is the
idiomatic way to express GCP-IAM's "a role bound at a scope covers every resource
under it" without a container object. It shares the field-backing constraints
(exactly one concrete subject type; no subject sets, wildcards, ids, caveats, or
expiration), parses to `ConstBinding` on `Relation.backing`, persists as
`{"kind": "const", "target_id": ...}`, and `rebac.E009` requires the declaring
type to have a Django model (the const *target* need not — it is typically a
virtual role namespace).

The additive object form restricts the edge to matching source rows:
`// rebac:const={"target_id":"public","filters":{"is_public":true}}`.
`ConstBinding(target_id: str, filters: tuple[tuple[str, Any], ...] = ())`
stores these predicates. Filters use the existing scalar backing grammar
(including false and null), and refer only to the declaring model’s local
concrete fields, with Django transforms/lookups. Inherited MTI columns requiring
a parent join and related-row traversals are rejected by `rebac.E009`. The
`pk` alias resolves through `_meta.pk`, including MTI parent-link primary keys;
FK attnames may address their stored scalar values. Empty filters
are equivalent to a bare ID and retain its existing serialized and rendered form.
Nonempty filters persist under `filters` beside `kind` and `target_id`. Field
and constant bindings sort native filter tuples at construction so equality
agrees with deterministic rendering. Malformed source IDs select no rows for
field and filtered-constant checks; conversion belongs to the shared identity
helper.

Only source rows matching the filters carry the edge, including under exclusion
and through arrows. Filter columns are watched fields: updating one changes
the edge with no tuple write, and the update is gated as a backed-edge write.
Native `.zed` rendering retains the JSON directive
deterministically; SpiceDB export continues to omit backing metadata.

For **unfiltered** constants, resolution is fixed-target rather than per-row,
which has two consequences in
`LocalBackend`:

- **One decision per statement, not one per resource.** Because the target
  object is the same for every row, a queryset scope decides the arrow over
  the constant once, before its statement is compiled, and witnesses the
  decision inside the statement (see
  [Facts about fixed objects](#facts-about-fixed-objects)). Nothing is stored
  per resource, so new rows need nothing written for this constant. A
  resource-specific ban (`- banned`) is evaluated per row in the same
  statement.
- **SpiceDB projection is one tuple per source row.** Unlike field-backing's
  one-tuple-per-FK, a `SpiceDBBackend` projector must materialize the synthetic
  edge for every row of the source type (or model it as a `parent`/`platform`
  hierarchy), since SpiceDB has no static-relationship primitive. The local
  synthesis stays free of that cost. See the per-kind table under
  [Backings are `LocalBackend`-only until the projector ships](#backings-are-localbackend-only-until-the-projector-ships).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         your Django project                      │
│                                                                   │
│  views/    drf/    celery/    graphql/    plain Python           │
│    │         │        │         │         │            │          │
│    └─────────┴────────┴─────────┴─────────┴────────────┘          │
│                          │                                        │
│              RebacMixin / RebacPermission / @rebac_resource         │
│                          │                                        │
│  ┌───────────────────────▼────────────────────────────────┐      │
│  │                 rebac.backends.Backend (ABC)           │     │
│  │   check_access  has_access  accessible  lookup_subjects  │     │
│  └───────────────────────┬────────────────────────────────┘      │
│                          │                                        │
│            ┌─────────────┴────────────┐                          │
│            │                          │                          │
│  ┌─────────▼──────────┐    ┌──────────▼──────────────┐          │
│  │  LocalBackend      │    │  SpiceDBBackend          │          │
│  │  ─────────────     │    │  ──────────────          │          │
│  │  permissions       │    │  planned authzed adapter │          │
│  │  compiled to SQL   │    │  roadmap implementation  │          │
│  │  + cel-python for  │    │                          │          │
│  │  caveats           │    │                          │          │
│  └────────────────────┘    └──────────────────────────┘          │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

**Three layers, one boundary.** The schema (`.zed` files → `Schema*` tables → in-memory expression tree) is the contract. Both backends honour it. Application code never imports a backend directly — it goes through the `Backend` ABC instance resolved lazily from `REBAC_BACKEND` on first use.

**Where each integration hooks:**

| Surface | Hook | What it does |
|---|---|---|
| Django ORM | `RebacMixin` metaclass | Replaces `objects` with `RebacManager`; installs save/delete owners and explicit-sender cascade receivers; stamps actors during queryset materialisation. |
| DRF | `RebacPermission` (BasePermission) + `RebacFilterBackend` (BaseFilterBackend) | Per-action permission check on viewsets; queryset filter on list endpoints. |
| Celery | Explicit `actor_context()` / `.with_actor()` in the task | Carry a trusted actor reference from the producer. Automatic signal propagation is planned. |
| MCP (FastMCP) | `rebac.mcp.rebac_mcp_tool` decorator | Resolves the actor from trusted request context through `REBAC_MCP_ACTOR_RESOLVER`, then falls back to ambient `current_actor()` when no identity field is present. Checks the target permission before running the body inside `actor_context`. See [proposal 0004](./proposals/0004-mcp-tool-integration.md). |
| GraphQL (strawberry) | `rebac.graphql.strawberry.RebacExtension` + `RebacChannelsConsumerMixin` | Opens evaluator/Zookie scopes per operation and per subscription emission. Use `require_permission` or actor-scoped querysets inside resolvers. |
| GraphQL (Strawberry-Django) | `rebac.graphql.strawberry_django.RebacDjangoOptimizerExtension` | Wraps Strawberry-Django's optimizer with REBAC-safe relation loading: guarded `select_related` for to-one paths and actor-scoped protected prefetches. |
| Plain Python | `@rebac_resource(type=..., id_attr=...)` | Registers the class as a known resource type for explicit `check_access()` calls. |

---

## Public API surface

```python
from rebac import (
    # Mixin and managers
    RebacMixin, RebacTrackedMixin, RebacManager, RebacQuerySet,
    TrackedManager, TrackedQuerySet,    # unscoped; own gated writes; the base for a declared base manager

    # Decorators
    require_permission, rebac_resource,

    # Backend interface
    Backend, LocalBackend, SpiceDBBackend,
    CheckResult, Consistency, Zookie,
    ObjectRef, SubjectRef, RelationshipTuple,
    CheckItem,                          # one item of check_bulk_permissions()

    # Errors
    PermissionDenied, MissingActorError, CaveatUnsupportedError,
    PermissionDepthExceeded, NoActorResolvedError, RelationshipReadError,

    # Actor types & resolution
    ActorLike,                          # SubjectRef | User | Group | AnonymousUser | <@rebac_subject-registered>
    current_actor, set_current_actor,
    actor_context,                      # context-manager form, mirrors sudo()
    sudo,                               # request-path bypass; gated by REBAC_ALLOW_SUDO
    system_context,                     # framework-job bypass; NOT gated by REBAC_ALLOW_SUDO

    # Anonymous subject
    ANONYMOUS_ACTOR,                    # SubjectRef.of("auth/anonymous", "*") — default
    anonymous_actor,                    # callable form, reads REBAC_ANONYMOUS_TYPE at call time
    is_anonymous_actor,                 # predicate

    # Convenience helpers
    write_relationships, delete_relationships, delete_relationship, backend,

    # One transaction and one validation for a block of stored-schema writes
    schema_changes,

    # Preflight against not-yet-persisted resources (0.4+)
    check_new,

    # Composable resolvers (0.3.1+)
    chain_resolvers, bearer_token,

    # Settings (advanced)
    app_settings,
)

from rebac.drf    import RebacPermission, RebacFilterBackend
from rebac.mcp    import rebac_mcp_tool, default_actor_resolver, get_mcp_actor_resolver
from rebac.schema import parse_zed, validate_schema   # for tooling
from rebac.memberships import grant, revoke, members_of, containers_of   # direct `member` tuples
from rebac.roles  import grant, revoke, roles_of, members_of   # role-as-namespace helpers
from rebac.testing import install_schema   # test helper: make a schema and backend current
```

`rebac.testing.install_schema(schema, *, backend=None, using=None)` is the
supported way for a dependent project's tests to run against a schema of
their own. It installs `schema` (a parsed `Schema` or `.zed` text) as a manual
schema on `backend` (a new `LocalBackend` by default), makes that instance the
one `rebac.backend()` returns in this process, makes sure the
`SchemaGeneration` row exists on `using` (the relationship write alias by
default), and returns the backend. It builds nothing: permissions on `using`
are decided from the rows already stored.
`rebac.backends.reset_backend()` undoes it; call it in teardown.

`rebac.schema_changes(using=None)` groups writes to stored schema rows
(`SchemaDefinition`, `SchemaRelation`, `SchemaPermission`, `SchemaCaveat`,
`SchemaOverride`). Each such write on its own locks the policy, publishes a
revision and validates the composed policy. Inside the block the writes share
one transaction on `using` (the relationship write alias by default) and the
policy lock is held until the block exits; the composed policy is validated
once, at the exit of the outermost block, and an exception rolls the writes
back. Blocks nest: an inner one joins the outer one. It applies to the stored
schema of `LocalBackend`.

`rebac.memberships` owns direct `member`-tuple creation, exact (caveat-aware)
revocation and enumeration for any container type. `rebac.roles` composes it
and adds role-spec parsing and role hierarchy; both are first-class, semver-stable
public APIs.

Role `grant` forwards optional `caveat_name` and `caveat_context` to memberships;
role `revoke` forwards `caveat_name` for exact revocation.
`rebac.roles.is_role_type(resource_type)` recognises the role convention using
`ROLE_TYPE_SUFFIX`; SQL convention filters use the same constant.

`str(RelationshipTuple(...))` renders the canonical wire string
`<type>:<id>#<relation> @ <subject_type>:<id>[#<subject_relation>][ with <caveat>]`.
Relationship model strings and relationship audit targets use this renderer.

Everything else (`rebac._internal.*`) is private and may change in any minor release.

### Anonymous subject — built-in

The plugin ships **three** built-in subject types alongside what consumer
apps register via `@rebac_subject`:

| Subject type | Source | Constructed by |
|---|---|---|
| `auth/user` | maps onto `django.contrib.auth.User` | `to_subject_ref(user)` |
| `auth/group` | maps onto `django.contrib.auth.Group` | `to_subject_ref(group)` → `auth/group:<pk>#member` |
| `auth/anonymous` | the unauthenticated request | `anonymous_actor()` / `ANONYMOUS_ACTOR` |

The subject type for anonymous is configurable via
`REBAC_ANONYMOUS_TYPE` (default `"auth/anonymous"`). The canonical
anonymous SubjectRef is `(REBAC_ANONYMOUS_TYPE, "*")`.

Schemas can grant the singleton explicitly through the `anonymous` builtin,
or grant every concrete actor of its type through a wildcard relation:

```zed
// Wildcard subject on a relation type union
definition knowledge/note {
    relation public: auth/anonymous:*
    permission read = public + viewer
}

// Bare schema keyword in a permission expression
definition knowledge/page {
    permission read = anonymous + authenticated
}
```

The bare keyword `anonymous` matches only the canonical anonymous SubjectRef;
`auth/anonymous:*` as a relationship wildcard also matches other concrete IDs
of that type. The bare keyword `authenticated` matches any nonempty resolved
actor other than the exact singleton, including subject-set actors.

The default resolver (`rebac.actors.default_resolver`) returns
`anonymous_actor()` for any request whose `user.is_authenticated` is
False, so callers don't have to construct the anonymous subject by
hand. Django's `AnonymousUser` also resolves to it via
`to_subject_ref()`.

### `rebac.roles` — predefined-role helpers

A convention layer on top of `Relationship` for the GCP-style
"role-as-resource" pattern. Roles live as objects in `<namespace>/role`
resource types; grants are `Relationship` rows on those objects with
relation `member`. No new storage type, no schema syntax addition —
this module packages the recipe into four helpers so every consumer
doesn't reinvent it.

```python
from rebac.roles import grant, revoke, roles_of, members_of

grant(actor=alice,      role="storage/role:object_viewer")
grant(actor=eng_group,  role="storage/role:object_admin")

revoke(actor=alice,     role="storage/role:object_viewer")

list(roles_of(alice))                          # [ObjectRef("storage/role", "object_viewer"), ...]
list(members_of("storage/role:object_admin"))  # [SubjectRef(auth/group:eng#member), ...]
```

Each consumer addon ships one `definition <addon>/role { relation
member: ... }` block per addon, plus references to specific role objects
from its resource definitions:

```zed
definition storage/role {
    relation member: auth/user | auth/group#member
}

definition storage/file {
    relation viewer: auth/user
                   | auth/group#member
                   | storage/role:object_viewer#member
                   | storage/role:object_admin#member   // admin includes viewer

    permission read = viewer
}
```

A pinned-id `#member` allowed subject is a *grantable* subject, not an
implicit grant. Granting Alice `storage/role:object_viewer` lights up `read`
only on files carrying a per-file `viewer @ storage/role:object_viewer#member`
tuple; once that linking tuple exists, role-membership changes reach every
linked file with no further per-file rows, but the role grant alone opens
nothing (the local backend never synthesises the linking tuple). For a role
that must cover *every* row of a type with no per-resource tuple, use a
const-backed relation (see [Const-backed (synthetic) relations](#const-backed-synthetic-relations)) —
the one tuple-free canon for role reach. The recipes below describe where a
*written* tuple takes effect; none makes an allowed subject reach implicitly.

**Role hierarchy** is stock SpiceDB — three recipes, none of which require
engine changes:

| Recipe | When to use |
|---|---|
| **Type-union inclusion** | Fixed compile-time hierarchy. Add the narrower role's `:<id>#member` to the wider role's type union: `relation member: auth/user \| storage/role:object_admin#member`. The narrower-role members flow through to every role declaring this union entry. Best for universal-admin (`platform/role:admin#member`). |
| **Per-resource permission composition** | Per-resource viewer/editor/admin tiers. Each resource declares `permission read = viewer + editor + admin` so granting `object_admin` lights up read/write/delete automatically. Most explicit; grep-able. Default choice for CRUD-shape roles. |
| **Runtime-editable `includes` + `effective_member`** | Hierarchy editable at runtime without a schema PR. Roles declare `relation includes: <namespace>/role` + `permission effective_member = member + includes->effective_member`; resources hold a direct role object and arrow to `effective_member`. `rebac.roles.imply(parent=..., child=...)` writes the direct child-role tuple. `LocalBackend` follows the inclusion chain when the permission is read, up to `REBAC_DEPTH_LIMIT` levels (see [Recursion](#recursion)). |

Relationship subjects may name only relations, as required by SpiceDB's
[subject relation contract](https://authzed.com/docs/spicedb/concepts/schema#subject-relations);
they cannot name computed permissions. Consumers upgrading an earlier
``includes: role#effective_member`` experiment must change it to
``includes: role``, change ``member + includes`` to
``member + includes->effective_member``, and rewrite each stored implication
tuple from ``@role:<child>#effective_member`` to ``@role:<child>``. Resource
grants follow the same shape: store the role object in a relation and arrow
from the resource permission to the role's ``effective_member`` permission.
No automatic tuple migration is provided because stripping a subject suffix
without its matching schema rewrite would change authorization semantics.

Use a consumer-owned data migration over historical relationship models to
rewrite only the affected grants, preserving caveats, context and expiration.
If the old persisted schema prevents system checks from loading, run that
migration with Django's `migrate --skip-checks`, then synchronize the corrected
schema with `rebac --skip-checks sync` under the normal provenance rules.
Do not resume serving until the migrations, schema synchronization and ordinary
checks complete. The runtime rejects the incompatible shape throughout;
`--skip-checks` only bypasses management-command startup checks.

**System / framework roles** (migrations, asset loaders) use
`rebac.actors.sudo` / `system_context` — they are not modelled as
roles. `rebac.roles` is exclusively for actor-grantable roles.

### Universal-admin convention

The "I'm in every role" tier is expressed as **a single role object plus
a type-union entry in every other `<namespace>/role` definition**:

```zed
// Ship once in your framework's meta-addon
definition platform/role {
    relation member: auth/user | auth/group#member
}

// Every other addon's role:
definition storage/role {
    relation member: auth/user
                   | auth/group#member
                   | platform/role:admin#member   // the universal-admin entry
}

definition knowledge/role {
    relation member: auth/user
                   | auth/group#member
                   | platform/role:admin#member
}
```

Granting `rebac.roles.grant(actor=alice, role="platform/role:admin")` gives
alice membership in that role. Other role objects must link their `member`
relation to `platform/role:admin#member`; the type-union entry only permits
those linking tuples and does not create them. The `:admin#member` subject reference in the type union uses
the canonical SpiceDB `<type>:<id>#<relation>` syntax (supported by the
parser since v0.3.x).

The convention is opt-in: `REBAC_UNIVERSAL_ADMIN_ROLE` defaults to `None`.
Set it to your application's role reference (for example,
`"platform/role:admin"`) to enable `rebac.W004`, which warns when a role
definition omits that subject-set entry. The package assumes no consumer's
role namespace and creates no linking tuples. Each opted-in role object still
needs an explicit relationship to the admin subject set before membership
flows through it.

---

## Models

The models hold relationships, the schema baseline, provenance and overrides.
Registry, audit and schema-generation models support those surfaces. The
library has no table that holds a row per application row; the full list is
under [Tables](#tables).

### `Relationship` — Tier 3, the core REBAC store

```python
# rebac/models/relationship.py (sketch)
class Relationship(models.Model):
    resource_type             = models.CharField(max_length=64, db_index=True)
    resource_id               = models.CharField(max_length=64, db_index=True)
    relation                  = models.CharField(max_length=64, db_index=True)
    subject_type              = models.CharField(max_length=64, db_index=True)
    subject_id                = models.CharField(max_length=64, db_index=True)
    optional_subject_relation = models.CharField(max_length=64, blank=True)
    caveat_name               = models.CharField(max_length=64, blank=True)
    caveat_context            = models.JSONField(null=True, blank=True)
    caveat_key                = models.CharField(max_length=64, blank=True, editable=False)
    expires_at                = models.DateTimeField(null=True, blank=True, db_index=True)
    written_at_xid            = models.BigIntegerField(db_index=True)

    class Meta:
        indexes = [
            # Forward: "what subjects have <relation> on <resource>?"
            models.Index(fields=["resource_type", "resource_id", "relation"]),
            # Reverse: "what resources does <subject> have <relation> on?"
            models.Index(fields=["subject_type", "subject_id", "relation"]),
            # Subject-set traversal (group#member -> user)
            models.Index(fields=["subject_type", "subject_id", "optional_subject_relation"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "resource_type", "resource_id", "relation",
                    "subject_type", "subject_id", "optional_subject_relation",
                    "caveat_name",
                ],
                name="rebac_relationship_uniq",
            ),
        ]
```

**Frozen contract.** The shape mirrors `authzed.api.v1.Relationship` exactly. Renames are breaking. Indexes are critical (every permission statement that reads tuples uses them) and ship in the initial migration — never as a documentation step.

**`caveat_key`.** A library-owned digest of the caveat name and the pinned context, empty for a tuple without a caveat. It is not part of the wire shape. Every supported write path writes it with the tuple, and statements select caveated tuples by it (see [Caveats](#caveats)).

**Swappability.** Projects that need to extend the model (audit FKs, multi-tenant prefix, etc.) declare a custom subclass and point `REBAC_RELATIONSHIP_MODEL = "myapp.MyRelationship"`. The plugin uses [`swapper`](https://pypi.org/project/swapper/) to keep migrations correct across this swap. Default behaviour: `swapper` returns the built-in `rebac.Relationship`.

**`written_at_xid` (Zookie equivalent).** Populated on save:
- PostgreSQL: `txid_current()` via a default expression.
- MySQL: monotonic timestamp (microsecond precision).
- SQLite: package-global `time.monotonic_ns()` counter (test-mode only — not for production).

`Zookie` consistency tokens encode `f"{backend_kind}.{xid}"`. Tokens are **not portable** across backends; if a project flips `REBAC_BACKEND` from `local` to `spicedb`, persisted Zookies in caches must be drained.

**`expires_at`.** Mirrors SpiceDB's [`use expiration`](https://authzed.com/docs/spicedb/concepts/schema#use-expiration) feature (GA in v1.40+). Expired rows are evaluated as absent at check time. Automatic garbage collection is planned; no `rebac.gc` task is shipped.

### Storage modes

`LocalBackend` ships two storage shapes for the relationship table; the
active one is selected by `REBAC_LOCAL_BACKEND_STORAGE`:

| Mode | Backing model | Hot index width per entry | When to use |
|---|---|---|---|
| `"denormalized"` *(current default)* | `Relationship` (the table above) | ~192 bytes (4 x CharField + relation) | Existing deployments; smallest change footprint. |
| `"registry"` *(opt-in)* | `RelationshipRegistry` + `RebacResource` | ~16 bytes (two integer FKs + relation) | Large relationship tables (>100k rows); deployments that want FK-CASCADE cleanup. |

Both tables ship in migration `0002_rebac_resource.py` so an operator can
flip the setting without further schema changes. `rebac.models.active_relationship_model()`
returns whichever is active; engine code (`LocalBackend`, `rebac.relationships`,
`rebac.roles`) routes every read/write through that helper.

The wire shape — `RelationshipTuple` and the string kwargs to the active
manager — is invariant across modes. `RelationshipRegistry.objects.create(
resource_type="…", resource_id="…", relation="…", subject_type="…",
subject_id="…")` upserts the two `RebacResource` rows transparently. Reads
translate the four denormalized wire field names (`resource_type`,
`resource_id`, `subject_type`, `subject_id`) into FK-side lookups
(`resource_fk__resource_type`, etc.) at the QuerySet layer. The read boundary
covers filter/exclude/get kwargs, nested `Q(...)` objects, positional
`values()` / `values_list()` projections, and `order_by()` (including `-`
prefixes), so chained filters and natural Django projection code work without
consumer storage-mode branches.

Expression surfaces that cannot be translated without changing Django's query
semantics, such as `annotate(subject=F("subject_id"))`, fail early with
`RelationshipReadError` instead of leaking a raw `FieldError`. Use
`for_resource()`, `for_subject()`, `wire_values()`, `order_by_resource()`,
`order_by_subject()`, or the explicit registry FK path when writing
storage-mode-specific expressions.

Both concrete relationship models expose the same mode-agnostic query helper
surface on their manager/queryset: `for_resource(type, id)`,
`for_subject(type, id, optional_relation=None)`, `order_by_resource()`,
`order_by_subject()`, and `wire_values()`. The `wire_values()` projection
returns denormalized wire-shaped dicts (`resource_type`, `resource_id`,
`relation`, `subject_type`, `subject_id`, `optional_subject_relation`,
`caveat_name`) in both storage modes; registry mode projects through
`resource_fk` / `subject_fk` internally. `rebac.relationships.resolve_subjects`
is the inverse model lookup for `SubjectRef`s whose object type maps to a
registered Django model; unknown types and missing rows are omitted.

**Why registry shape exists.**

- Index density: with integer FKs the hot `(resource_fk, relation)` index
  fits ~500+ entries per Postgres leaf page vs ~40 in denormalized form.
  Every permission statement that reads tuples uses these indexes.
- FK cascade: when a Django row backed by `RebacMixin` is deleted, the
  `post_delete` signal handler drops the matching `RebacResource` row,
  and the FK CASCADE on `RelationshipRegistry` sweeps every tuple that
  referenced it. Denormalized mode deletes matching resource-side and
  subject-side tuples directly. Both paths use the deleted instance's Django
  database alias and participate in that alias's deletion transaction.
- Referential integrity: writes to `RelationshipRegistry` reference
  registered `(type, id)` pairs only — typos surface as constraint
  violations instead of orphan tuples that never match a check.

**Migration command.**

```bash
python manage.py rebac migrate-storage --to registry [--from denormalized] \
    [--batch 5000] [--dry-run]
```

Both directions supported; `--dry-run` reports row counts without writes;
re-runs are idempotent (the destination's unique constraint absorbs
duplicates). Row-count parity is checked at the end. The source table is
not dropped — flip `REBAC_LOCAL_BACKEND_STORAGE` once the copy completes,
then drop manually. `rebac.W005` surfaces the recommendation at startup
when the setting is `"denormalized"`.

**SpiceDB unaffected.** This is purely a `LocalBackend` optimisation. The
future `SpiceDBBackend` will write through gRPC and will not touch the local
relationship table.

**Current status.** Both tables have shipped since 0.7.0. The default remains
`"denormalized"` and registry mode is opt-in. A future minor release may flip
the default or remove the denormalized path after migration experience is
boring enough to justify the churn.

### `SchemaDefinition` / `SchemaRelation` / `SchemaPermission` / `SchemaCaveat` — Tier 1 baseline

Loaded from each app's `permissions.zed` at sync time. Read lazily by `LocalBackend` into an in-memory expression tree.

```python
class SchemaDefinition(models.Model):
    resource_type = models.CharField(max_length=64, unique=True)   # "blog/post"

class SchemaRelation(models.Model):
    definition       = models.ForeignKey(SchemaDefinition, on_delete=models.CASCADE)
    name             = models.CharField(max_length=64)              # "owner"
    allowed_subjects = models.JSONField()                           # see below
    caveat           = models.CharField(max_length=64, blank=True)

    class Meta:
        unique_together = [("definition", "name")]

class SchemaPermission(models.Model):
    definition = models.ForeignKey(SchemaDefinition, on_delete=models.CASCADE)
    name       = models.CharField(max_length=64)                    # "read"
    expression = models.TextField()                                 # "owner + viewer + folder->read"

    class Meta:
        unique_together = [("definition", "name")]

class SchemaCaveat(models.Model):
    name       = models.CharField(max_length=64, unique=True)
    params     = models.JSONField()                                 # [{"name":"required","type":"int"}]
    expression = models.TextField()                                 # CEL source
```

`SchemaRelation.allowed_subjects` is a JSON array:

```json
[
  {"type": "auth/user"},
  {"type": "auth/group", "relation": "member"},
  {"type": "auth/user", "wildcard": true}
]
```

These rows are **read-only** to application code. They're populated by `manage.py rebac sync` and (for Tier 2 deltas) by `SchemaOverride` rows that mutate them indirectly.

### `PackageManagedRecord` — Tier 1 provenance, the `noupdate` mechanism

Borrowed from Odoo 18's `ir.model.data`. Tracks which package shipped which schema row, with `noupdate` semantics that preserve admin edits across upgrades.

```python
class PackageManagedRecord(models.Model):
    package         = models.CharField(max_length=128)             # "blog"
    external_id     = models.CharField(max_length=255)             # "blog.post.read"
    schema_revision = models.PositiveIntegerField()                # from .zed header
    target_ct       = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    target_pk       = models.PositiveIntegerField()
    content_hash    = models.CharField(max_length=64)              # of source fragment
    no_update       = models.BooleanField(default=True)
    last_synced_at  = models.DateTimeField()

    class Meta:
        unique_together = [("package", "external_id")]
        indexes = [models.Index(fields=["target_ct", "target_pk"])]
```

The schema rows themselves stay clean. Provenance, hash-checking, and noupdate are one decoupled layer above. This is the lesson from Odoo's two decades of `ir.model.data`: by keying noupdate on the external id (provenance), upgrades can cleanly distinguish "package shipped a new version of a row" from "admin edited the row and we need to preserve their edit".

### `SchemaOverride` — Tier 2, runtime tweaks

`rebac.admin` registers the override and read-only REBAC models through
Django admin autodiscovery without a runtime generic-class shim.

```python
class SchemaOverride(models.Model):
    KIND_TIGHTEN  = "tighten"
    KIND_LOOSEN   = "loosen"
    KIND_DISABLE  = "disable"
    KIND_EXTEND   = "extend"
    KIND_RECAVEAT = "recaveat"

    kind        = models.CharField(max_length=16, choices=...)
    target_ct   = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    target_pk   = models.PositiveIntegerField()
    expression  = models.TextField()                                # zed-syntax fragment
    reason      = models.TextField()                                # required
    created_by  = models.ForeignKey(settings.AUTH_USER_MODEL,
                                     on_delete=models.SET_NULL, null=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    expires_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["target_ct", "target_pk"])]
```

Composition rule (applied when the effective schema is loaded):

```
effective_expr = (baseline_expr + extends) AND tightens
                                 minus disables
                                 with caveats merged from recaveats
```

Every relation or permission newly named by an override expression must already
exist in the target definition. Composition and override writes reject newly
introduced undefined names with `SchemaError`, including undefined arrow
sources. A pre-existing undefined reference in the baseline remains a baseline
error; composition does not add a second failure for it.
When a later baseline removes a name referenced by a stored override, that
override is ignored during composition and `rebac.W010` identifies the stale
row. Other valid overrides continue to apply; operators should update or delete
the stale row.

#### Effective schema loading and generation

`LocalBackend` shares parsed effective-schema snapshots per database alias and
revision. Concurrent cold readers coordinate one load; revision reads and
schema loading run outside the process lock, which protects publication.
Only the latest revision per alias remains shared; in-flight operations may
retain their pinned tree. A racing load is retried before publication, and
override deadlines expire the composed snapshot.

Schema reads use the alias selected by the `SchemaDefinition` router; component
and override reads stay on that alias. No schema queries run at startup.
An operation pin is checked before querying the revision. Outside an evaluator,
a new operation reads the revision; a cold load reads it again after loading,
to avoid publishing a schema spanning a commit. The pin stores the revision.
Retry exhaustion uses one uncached load checked against the revision before
and after; a changing fallback fails closed.
Within a scope, repeated pinned schema reads and eligible decision-cache hits
issue no queries. There is no revision SELECT on each cache lookup.

The five policy models (definitions, relations, permissions, caveats and
overrides) own writes through their shared model/queryset, including base-manager
and reverse-relation bulk writes. An owner locks the `SchemaGeneration` row
before policy reads or writes, creating it when absent, publishes a fresh
`SchemaGeneration.revision` in the same transaction
(`SchemaGeneration.objects.advance(using=...)`), and validates the composed
policy once when the outermost owner exits (see [Writes](#writes)). A deletion
receiver covers cascades originating outside these owners. Conflict-ignoring
and upserting policy bulk creates are refused. Raw SQL policy writes bypass
the lock, the validation and the revision: a process keeps its cached policy
until a supported policy write or `rebac sync` publishes a revision.

Revision tokens are fresh identities, not rollback-reusable counters. Their
visibility follows database isolation, and they never enter deterministic
schema output. Same-process schema writes evict shared snapshots. A missing
generation row is created by the next supported policy write or sync without
restarting the backend; there is no sticky degraded mode. Before any policy
is published the revision is empty, every permission statement is fenced out
and reads are closed (see
[Kept statements and the policy fence](#kept-statements-and-the-policy-fence)).
Apply migrations and run `rebac sync` before serving. Migration `0009`
creates the generation row with an empty revision.

Every explicit sync publishes a fresh revision, an unchanged sync included.
`sync --check` is read-only.

Evaluator invalidation clears schema pins, including on subscription emissions;
an unchanged revision can reuse the shared parsed tree. Connection observers
clear pins around writes and transaction boundaries, including rollback.
Manual transaction management retains operation pins only. Permission decisions
remain uncached inside transactions. Backing rows are never stored in schema
snapshots; a statement reads them when it runs.

Decision-cache policy retains the 0.18.2 exclusions: resource types reachable
from field or attribute backings are not cacheable. Expiring relationships
bypass caching; override deadlines refresh the schema generation so a cached
grant cannot outlive its validity.
Eligible hits cost zero queries. See [PermissionEvaluator](#permissionevaluator--per-request-check-cache).
An in-flight scoped snapshot is intentional; another process's schema commit
appears at the next scope or invalidation boundary. A zero-query cache hit
cannot independently observe another process's commit.

The connection observer sees Django cursor writes, not arbitrary DB-API calls
or side effects hidden inside SELECT expressions. Such escape hatches require
an explicit evaluator invalidation boundary for the per-request decision
cache. Statements themselves read the rows as they are and need none.

An override is active while `expires_at` is null or strictly later than the
evaluation time. The composed schema refreshes at the earliest active deadline,
and grant expiry and site deadlines take effect at SQL execution. Manually installed
schemas have no database override lifecycle.

`django-zed-rebac` ships a Django admin form for `SchemaOverride`. Downstream frameworks may add GraphQL CRUD on top.

### `PermissionAuditEvent` — append-only audit

```python
class PermissionAuditEvent(models.Model):
    KIND_RELATIONSHIP_GRANT  = "rel.grant"
    KIND_RELATIONSHIP_REVOKE = "rel.revoke"
    KIND_OVERRIDE_CREATE     = "override.create"
    KIND_OVERRIDE_DELETE     = "override.delete"
    KIND_SCHEMA_SYNC         = "schema.sync"
    KIND_SUDO_BYPASS         = "sudo.bypass"

    kind               = models.CharField(max_length=32, choices=...)
    actor_subject_type = models.CharField(max_length=64)
    actor_subject_id   = models.CharField(max_length=64)
    target_repr        = models.CharField(max_length=512)
    before             = models.JSONField(null=True)
    after              = models.JSONField(null=True)
    reason             = models.TextField(blank=True)
    occurred_at        = models.DateTimeField(auto_now_add=True, db_index=True)
```

Written by every Tier 2 / Tier 3 mutation and every effective public bypass.
Queryset bypasses are audited at first evaluation or write, instance bypasses
at the first check or write, and block bypasses at entry, once per bypass.
A bypass queryset embedded in an expression (`Exists`, `Subquery`, a
`pk__in=` lookup) is audited when the statement it was resolved into is
compiled for execution, once per execution; building the expression runs no
query and writes no row, so an annotation built at import time does not touch
the database during app initialisation.
Engine-internal captures use a private non-audited path. An audit row is durable
only when its enclosing transaction commits. Append-only.

---

## Settings catalog

All settings prefixed `REBAC_`. No nested dict. Read via the public `app_settings` object.

| Setting | Default | Type | Purpose |
|---|---|---|---|
| `REBAC_BACKEND` | `"local"` | `"local"` \| `"spicedb"` | Which backend to instantiate lazily on first use. `"spicedb"` is reserved for the roadmap adapter and raises today. |
| `REBAC_RELATIONSHIP_MODEL` | `"rebac.Relationship"` | `str` | Swappable relationship model (Django convention). |
| `REBAC_LOCAL_BACKEND_STORAGE` | `"denormalized"` | `"denormalized"` \| `"registry"` | LocalBackend relationship storage shape. Registry mode is opt-in and uses `RelationshipRegistry` + `RebacResource`. |
| `REBAC_LOCAL_BACKEND_REGISTRY_BATCH_SIZE` | `5000` | `int` | Batch size for `python manage.py rebac migrate-storage`. |
| `REBAC_SPICEDB_ENDPOINT` | `None` | `str` \| `None` | Roadmap setting for the future `authzed.api.v1.Client`. Required once backend `spicedb` is implemented. |
| `REBAC_SPICEDB_TOKEN` | `None` | `str` \| `None` | Roadmap setting for the future SpiceDB preshared key. |
| `REBAC_SPICEDB_TLS` | `True` | `bool` | Roadmap setting for TLS behavior in the future SpiceDB adapter. |
| `REBAC_SPICEDB_AUTO_WRITE_SCHEMA` | `True` | `bool` | Roadmap setting for future schema auto-push. |
| `REBAC_SCHEMA_DIR` | `BASE_DIR / "rebac"` | `Path` \| `str` | Where `build-zed` writes `effective.zed`. |
| `REBAC_DEPTH_LIMIT` | `8` | `int` | Number of levels to which a recursive permission is unrolled in a compiled statement, and the cap on the permission walker (`check_new`). Within the bound a point check answers; one that cannot be decided within it raises `PermissionDepthExceeded`; a queryset scope includes only rows provable within it. Choose it to cover the deepest recursive chain in the data; statement size grows linearly with it. See [Recursion](#recursion). |
| `REBAC_TRACKED_MODELS` | `[]` | `list[str]` | Third-party backing models (`"app_label.ModelName"`), gated by explicit-sender receivers. User and Group are automatically tracked. |
| `REBAC_DEFAULT_CONSISTENCY` | `"minimize_latency"` | `str` | Default `Consistency` for checks. |
| `REBAC_CACHE_ALIAS` | `"default"` | `str` | Django cache backend name for `accessible()` cache. |
| `REBAC_LOOKUP_CACHE_TTL` | `60` (s) | `int` | TTL for `accessible()` cache. Invalidated on relationship writes for the matching `(subject, action, resource_type)`. |
| `REBAC_STRICT_MODE` | `True` | `bool` | If `True`, queryset construction without an actor (and not in `sudo()`) raises `MissingActorError`. **Production default.** |
| `REBAC_REQUIRE_SUDO_REASON` | `True` | `bool` | If `True`, `sudo()` calls without a `reason=...` raise. |
| `REBAC_ALLOW_SUDO` | `True` | `bool` | Globally disable the request-path `sudo()` bypass. Strict tenants set `False`. **Does NOT gate `system_context()`** — framework-owned jobs (migrations, fixture seeders, asset loaders) must still be able to bypass even on strict tenants; the two surfaces are deliberately split. Every block-scoped `system_context()` entry still emits a `KIND_SUDO_BYPASS` audit row, same as block-scoped `sudo()`. |
| `REBAC_GC_INTERVAL_SECONDS` | `300` | `int` | How often the expiration GC task runs. |
| `REBAC_AUTHENTICATION_MIDDLEWARE` | `"django.contrib.auth.middleware.AuthenticationMiddleware"` | `str` | Middleware path that populates `request.user`. `rebac.middleware.ActorMiddleware` must appear after this path. Frameworks that replace Django's stock auth middleware set this to their canonical middleware. |
| `REBAC_ACTOR_RESOLVER` | `"rebac.actors.default_resolver"` | `str` | Dotted-path callable that resolves `request → SubjectRef`. Override for custom identity layers (e.g., agent grants). |
| `REBAC_MCP_ACTOR_RESOLVER` | `"rebac.mcp.default_actor_resolver"` | `str` | Dotted-path callable resolving an MCP request `Context → SubjectRef`, consulted before ambient `current_actor()`. The default reads a canonical `SubjectRef` string from trusted server-populated `ctx.request_context.meta["actor_subject"]`. Invalid explicit identity fails closed. |
| `REBAC_TYPE_PREFIX` | `""` | `str` | Optional prefix for all generated resource types (multi-tenant SaaS). |
| `REBAC_SUPERUSER_BYPASS` | `True` | `bool` | If `True`, active superusers short-circuit `has_perm`; `ActorMiddleware` opens `sudo("superuser-bypass")` only when the resolver returns that user's own subject. Each elevated request emits a `KIND_SUDO_BYPASS` audit row. Suppressed when `REBAC_ALLOW_SUDO = False`. Strict tenants set this to `False`. |
| `REBAC_LINT_BARE_PREFETCH` | `True` | `bool` | Toggle for `rebac.W003` — the structural warning that an RBAC-bound model has an FK / O2O / M2M to another RBAC-bound model (a bare-string `select_related` / `prefetch_related` can load unguarded related rows). Enabled by default so the risky shape is visible; use `rebac_select_related()` / `rebac_prefetch_related()` or the Strawberry-Django optimizer for protected paths. |
| `REBAC_EVALUATOR_CACHE_SIZE` | `10000` | `int` | Max entries across the per-scope evaluator's check and accessible caches. |
| `REBAC_ZOOKIE_TRANSPORT` | `"none"` | `"none"` \| `"header"` \| `"session"` | Optional cross-request transport for the current Zookie. |
| `REBAC_ZOOKIE_HEADER_NAME` | `"X-Rebac-Zookie"` | `str` | Header name used when `REBAC_ZOOKIE_TRANSPORT = "header"`. |
| `REBAC_ZOOKIE_SESSION_KEY` | `"_rebac_zookie"` | `str` | Session key used when `REBAC_ZOOKIE_TRANSPORT = "session"`. |
| `REBAC_FIELD_READ_MODE` | `"allow"` | `"allow"` \| `"redact"` \| `"omit"` \| `"raise"` | Deny behavior for schema permissions named `read__<field>`. `"raise"` currently degrades to `"redact"` and emits `rebac.W008` until descriptor-level protected fields land. |
| `REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL` | `True` | `bool` | Bulk field redaction has no per-row caveat context; `True` treats conditional `read__<field>` results as denied. Set `False` only when conditional visibility is acceptable without context. |

Validation runs in Django's system-checks framework. `rebac.E001` validates the
backend selection, `rebac.E002` checks required SpiceDB settings, and
the optional SpiceDB client installation. Schema-dependent checks skip
backend resolution when a non-local backend is selected, so invalid backend
configuration is reported as checks rather than crashing `manage.py check`.
`rebac.W001` recognizes the shipped auth backend and its subclasses by class
identity, including the two public import paths.
Production-only checks (`--deploy`) include `rebac.W101` for
`REBAC_SPICEDB_TLS = False`.

---

## AppConfig and system checks

`apps.py`:

```python
class RebacConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name              = "rebac"
    verbose_name      = "REBAC"
    default           = True

    def ready(self):
        from . import signals
        from . import checks     # noqa: F401  — registers system checks
        signals.connect_tracked_signals()  # app registry only
```

**No queries. No model instantiation. No backend resolution at import time.** The backend singleton is constructed lazily on first access via `rebac.backend()` — this avoids `AppRegistryNotReady` and keeps `migrate` fast.

Schema-dependent checks defer an unreadable persisted schema while Django's
migration graph reports pending REBAC migrations. This lets `migrate` upgrade
old backing payloads before the current runtime validates them. Once those
migrations are applied, malformed schema data remains an error; permission
evaluation never adopts this startup tolerance.

System checks (in `rebac/checks.py`):

| ID | Severity | What it validates |
|---|---|---|
| `rebac.E001` | Error | `REBAC_BACKEND` is `"local"` or `"spicedb"`. |
| `rebac.E002` | Error | Required SpiceDB settings present when backend is `spicedb`. |
| `rebac.E003` | Error | A model with `Meta.rebac_resource_type` references a type not declared in any loaded `permissions.zed`. |
| `rebac.E004` | Error | Permission expressions parse against operator grammar. |
| `rebac.E005` | Error | `permissions.zed` declared in an `AppConfig` cannot be located on disk. |
| `rebac.E006` | Error | `REBAC_LOCAL_BACKEND_STORAGE` is `"denormalized"` or `"registry"`. |
| `rebac.E007` | Error | `REBAC_ZOOKIE_TRANSPORT` is `"none"`, `"header"`, or `"session"`. |
| `rebac.E008` | Error | `REBAC_FIELD_READ_MODE` is not one of `"allow"`, `"redact"`, `"omit"`, or `"raise"`. |
| `rebac.E009` | Error | A field-, attribute- or const-backed relation cannot be resolved: missing Django model, identity field, relation path, attribute or filter lookup; a path that ends on a different model than the declared subject type; or a const-backed relation whose target type has no schema definition. A read whose permission reaches such a relation raises `SchemaError` citing this ID; permissions that do not reach it are still decided. |
| `rebac.E010` | Error | Const-backed arrows form a schema evaluation cycle. The const-arrow validation also bounds the proposed-object preflight; the compiler's support for positive data cycles does not relax this schema restriction. |
| `rebac.E011` | Error | `Meta.rebac_subject_relation` names a relation the model's effective schema definition does not declare. |
| `rebac.E014` | Error (database) | The identity of a resource model, a field-backed target or a dynamic attribute container column cannot be compared with stored tuples: it has no canonical wire/column codec (supported: integer/auto, char/text/slug, UUID). Custom encoded field conversions that SQL cannot reproduce are refused. Fixed Boolean attribute anchors do not require a Boolean codec. |
| `rebac.E015` | Error (database) | A scoped or backing model's **write** alias differs from the relationship write alias. Models a permission reads must share the relationship database alias, because one statement joins them. Separate read replicas are allowed. |
| `rebac.E016` | Error (database) | A recursive component of the policy is refused by the compiler: a permission of the component is reached through the right-hand side of `-` inside the component, or one expression uses permissions of the component more than once. See [Recursion](#recursion). |
| `rebac.E018` | Error | A model on a permission backing path is neither `RebacMixin`, `RebacTrackedMixin`, nor explicitly tracked, so writes to it would not be gated (database check). Auto-created throughs with an owned/tracked endpoint and configured User/Group models are tracked automatically. Invalid `REBAC_TRACKED_MODELS` labels are errors too. |
| `rebac.E021` | Error | The schema declares a caveat but `cel-python` (the `caveats` extra) is not installed, so caveat bodies cannot be validated or evaluated. |
| `rebac.E023` | Error | A `RebacMixin` / `RebacTrackedMixin` model's declared base manager returns a queryset that is not a `TrackedQuerySet`, or one that filters rows. The library reads a model's rows and owns its gated writes through the base manager. (`E020` and `E022` are reserved by proposals 0011 and 0013.) |
| `rebac.W001` | Warning | `rebac.backends.RebacBackend` not in `AUTHENTICATION_BACKENDS`. |
| `rebac.W002` | Warning | A model with `Meta.rebac_resource_type` is missing `RebacMixin`. |
| `rebac.W003` | Warning | An RBAC-bound relation exists where bare `select_related("rel")` / `prefetch_related("rel")` can be unsafe outside the REBAC helpers or Strawberry-Django optimizer. |
| `rebac.W004` | Warning | Universal-admin role convention lint for role definitions. |
| `rebac.W005` | Warning | LocalBackend is still on denormalized storage and registry migration is recommended for large tables. |
| `rebac.W006` | Warning | `REBAC_ZOOKIE_TRANSPORT = "session"` without `django.contrib.sessions`. |
| `rebac.W008` | Warning | `REBAC_FIELD_READ_MODE = "raise"` currently degrades to `"redact"` until descriptor-based protected fields land. |
| `rebac.W009` | Warning | A text attribute-backed column declares a case-insensitive collation (`*_ci`, or the MySQL default), so SQL scoping and Python checks could disagree on container ids. Best-effort detection. |
| `rebac.W010` | Warning | A stored override references a relation or permission removed from the current baseline; that override is ignored until updated or deleted. |
| `rebac.W101` | Warning (`--deploy`) | `REBAC_SPICEDB_TLS = False` in production. |

`rebac.E014`, `rebac.E015`, `rebac.E016` and the backing-path form of
`rebac.E018` read the stored schema, so they run only as database checks:
`manage.py check --database <alias>`, and the checks `migrate` runs.

Users silence individual checks via Django's `SILENCED_SYSTEM_CHECKS = ["rebac.W001"]`.

---

## Authorization backend

`RebacBackend` does not authenticate; it routes Django permission checks into
REBAC. Inactive users deny. For a concrete object, `has_perm(user, perm, obj)`
maps the codename to an action and checks that object. Without an object,
mapped model permissions use an empty resource ID to ask whether the actor has
any accessible row or a row-independent grant. `has_module_perms` applies the
same model-level semantics for each registered resource's
`Meta.rebac_default_action` (or `read`) and returns true on the first
effective grant, including group, wildcard, and field-backed paths.
Proposed-row create authorization still uses `check_new`, because model-level
admission does not authorize a particular candidate's relationships.
Django's async permission walk calls `ahas_perm` and
`ahas_module_perms` on each backend. `RebacBackend` supplies those methods
through the same sync permission logic, executed in a thread-sensitive
worker so ORM access is safe from async views.

**Codename mapping.** Default mappings (`{view_, change_, delete_, add_}_<model>` → `{read, write, delete, create}`) ship in `rebac.codenames`. Per-package overrides via:

```python
# yourapp/apps.py
class YourAppConfig(AppConfig):
    rebac_codename_map = {
        "yourapp.share_post":   "share",
        "yourapp.archive_post": "archive",
    }
```

**Superuser bypass.** Preserved by default for operational ergonomics. When `REBAC_SUPERUSER_BYPASS = True` (default) and the user is an *active* superuser, two surfaces short-circuit:

1. `RebacBackend.has_perm(user, perm[, obj])` returns `True` immediately (this section's existing behaviour). Used by Django admin's "can the user see this row / use this app" probes.
2. `ActorMiddleware` opens a `sudo(reason="superuser-bypass")` bracket for the request lifetime only when its resolver returns the active superuser's own subject, so `Model.objects.with_actor(superuser).filter(...)` returns every row instead of being narrowed by `accessible()`. This matches the legacy contrib.auth contract that admin sees everything *at the QuerySet layer*, not just at the `has_perm` layer — without it, admin changelist queries would silently filter to "rows the superuser has an explicit relationship to", which is almost never what's wanted.

The middleware path routes through the public `sudo()` API, so each elevated request emits a `KIND_SUDO_BYPASS` audit row (consistent with the strict-mode invariant that bypasses are auditable) and obeys `REBAC_ALLOW_SUDO` — when sudo is globally disabled, the middleware short-circuit is suppressed too (fail-closed: a tenant that turned sudo off shouldn't get an implicit superuser elevation). Strict tenants disable both surfaces by setting `REBAC_SUPERUSER_BYPASS = False`.

---

## The unified check API

[Borrowed from Odoo 18 PR #179148. Locked.]

```python
class Backend(ABC):
    def check_access(
        self, *,
        subject: SubjectRef,
        action:  str,
        resource: ObjectRef,
        context: dict | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> CheckResult:
        """Three-state: HAS / NO / CONDITIONAL.
           Combines model-level and record-level checks.
           An empty resource ID asks whether any row is accessible, also
           considering row-independent grants. Proposed-row create
           authorization uses check_new() with candidate relationships."""

    def has_access(self, *, subject, action, resource, context=None) -> bool:
        """Boolean shorthand. CONDITIONAL collapses to False."""

    def check_bulk_permissions(
        self,
        items: Iterable[CheckItem],     # CheckItem(subject, action, resource, context=None)
        *,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> list[CheckResult]:
        """One three-state result per item, in the order given: what
           check_access() answers for that item. Mirrors SpiceDB's
           CheckBulkPermissions. An error that check_access() would raise
           for an item is raised by the call. The base implementation asks
           item by item; a backend overrides it to share work between
           items."""

    def accessible(
        self, *,
        subject:        SubjectRef,
        action:         str,
        resource_type:  str,
        context:        dict | None = None,
        consistency:    Consistency | None = None,
        at_zookie:      Zookie | None = None,
    ) -> Iterable[str]:
        """Set of resource_ids the subject has `action` on. Basis of
           `Model.objects.with_actor(actor)` queryset scoping."""

    def lookup_subjects(
        self, *,
        resource:     ObjectRef,
        action:       str,
        subject_type: str,
        context:      dict | None = None,
        consistency:  Consistency | None = None,
        at_zookie:    Zookie | None = None,
    ) -> Iterable[SubjectRef]:
        """Reverse: who has `action` on this resource?
           Powers share-with-user search and audit views."""

    def write_relationships(self, writes: Iterable[RelationshipTuple]) -> Zookie:
        """Atomically commit relationship rows. Returns a consistency token."""

    def delete_relationships(self, filter_: RelationshipFilter) -> Zookie:
        """Atomically delete matching relationship rows.

        Every field on ``RelationshipFilter`` uses wildcard-on-empty
        semantics — an empty value means "don't filter on this column"."""

    def delete_relationship(self, tuple_: RelationshipTuple) -> Zookie:
        """Atomically delete one tuple shape (exact-match on every field).

        Diverges from authzed.api.v1 by intent: SpiceDB expresses
        tuple-shaped deletes via ``WriteRelationships`` with
        ``OPERATION_DELETE``. Adding a dedicated verb keeps the local
        ergonomics — empty ``optional_subject_relation`` / ``caveat_name``
        as exact values rather than wildcards — without forcing every
        caller to construct an updates-with-operation list."""

    def schema(self) -> Schema:
        """Return the installed schema AST.

        Mirrors SpiceDB's ``ReadSchema``. Required by engine-side
        semantic checks (notably ``rebac.check_new``) that walk
        permission expressions before any row exists. LocalBackend serves
        the in-memory composed schema;
        the future SpiceDB adapter should cache the parsed result of
        ``Client.ReadSchema()``."""
```

`CheckResult` carries the three-state result, missing caveat parameters and an
optional `reason`. Unknown resource types and actions preserve their diagnostic
reason. Callers may retry conditional results with additional context.
For caveats, a declared parameter with a `None` value is missing even when
its key exists. A CEL lookup or function failure after all declared parameters
are supplied raises `CaveatUnsupportedError`; it is not a missing parameter.
Schema validation compiles CEL with cel-python and walks its syntax tree to
reject free identifier roots absent from the caveat's declared parameter list,
leading-dot identifiers, non-bare macro binders, and unsupported `reduce`.
Only declared parameters enter the CEL activation. Schemas without caveats do
not require cel-python; a caveat schema without it reports `rebac.E021` at check time.
Macro-bound variables, literals, and CEL built-ins are not free identifiers.
Runtime and coercion errors name the caveat and parameter names, never caller
context values, including through chained exception causes and contexts.

### `check_new` — preflight against not-yet-persisted resources

Auto-CRUD create paths need to authorise a row *before* it exists. The
permission expression on the resource type may reference relations that
the new row would carry once written — e.g.::

    definition blog/post {
        relation vault: blog/vault
        permission create = vault->write
    }

There are no ``Relationship`` rows on ``blog/post:<id>`` yet, so
``Backend.check_access`` short-circuits to deny. Instead the caller
supplies the relations the new row *would* point at, and
:func:`rebac.check_new` evaluates the expression against that virtual
overlay::

    from rebac import check_new, SubjectRef

    result = check_new(
        subject=SubjectRef.of("auth/user", "alice"),
        action="create",
        resource_type="blog/post",
        relationships={"vault": [SubjectRef.of("blog/vault", "v1")]},
    )
    if not result.allowed:
        raise PermissionDenied(result.reason)

Arrow hops walk into the (real) target via the active backend's
``check_access`` (answered by the compiled predicate), so all post-hop evaluation reuses the canonical
semantics — caveat-conditional outcomes propagate as
``CONDITIONAL_PERMISSION`` with the union of missing caveat
parameters. The dispatch (operator precedence, sub-permission cycle
detection, ``anonymous`` / ``authenticated`` built-ins, tri-state
combinators, ``REBAC_DEPTH_LIMIT``) uses ``rebac.schema.walker``. Persisted
checks, including caveated checks, use the compiler; a frozen copy of an
earlier scalar walker (`tests/reference_oracle.py`) serves only as a test
oracle.

Unfiltered const-backed relations are injected into the virtual overlay from the
schema. If a new `blog/post` declares `relation admin: platform/role //
rebac:const=admin`, `check_new()` behaves as if the proposed object carried
`#admin @ platform/role:admin`, then evaluates `admin->member` through the real
backend store. Non-empty or unknown caller entries for these bare-ID constants
raise `SchemaError`.

Caller-supplied overlay subjects with empty IDs or invalid subject shapes refuse
the whole preflight, as do unknown overlay relation names. The empty virtual resource
ID used internally by the walker is not an overlay subject.

`check_new` retains its existing virtual-relationship-only signature. Django
create paths construct the candidate first, including Python defaults, then
`_proposed_forward_relationships` projects the field-backed and filtered-constant
relations that `create` depends on, including dependencies through named
permissions and arrow sources. A resolved constant owns `matches_candidate`;
its predicates use typed SQL literals, including declared column collations, on
the write alias without reading a persisted source row. Unreferenced backings
are not resolved, queried, filtered or marked unknown.

Direct `check_new` callers supply filtered-constant facts through the existing
`relationships` overlay, just as they supply field-backed facts. They must
project the matching fixed target, an empty tuple, or `None` truthfully;
`check_new` evaluates those supplied facts without reading a candidate. Omitted
filtered-constant facts are unknown. Model hooks still cannot supply any
library-owned backing, and bare-ID constants cannot be overridden.

`create()`, `insert(obj)`, a new instance's `save()`, and every row of
`bulk_create()` use `check_new` as the single evaluator. A backing whose first
hop is reverse FK, reverse O2O, or many-to-many is genuinely empty on the new
row and contributes `()`. A single-hop, unfiltered forward FK/O2O that stores
the target's REBAC identity projects its prepared scalar without a query,
including fields inherited from concrete MTI parents. Non-direct identities,
forward multi-hop paths, and filtered backings resolve their targets on the
write alias; filters on resolved targets and known candidate scalar values
are evaluated by Django on that alias.

Models contribute post-save tuple facts through
`RebacMixin.proposed_relationships(self, *, using: str | None = None) -> Mapping[str, Iterable[SubjectRef | Model]]`,
which defaults to `{}`. A hook that resolves subjects must use the supplied
write database alias `using`. Field-backed relations remain library projections;
the hook declares other relations the row will carry once persisted, while
other post-insert facts remain unknown to the candidate gate. Omitted tuple
relations retain `check_new`'s empty/no-row semantics. `_check_new_model`
merges these contributions for `save()`, `create()`, `insert(obj)`, and each
`bulk_create()` candidate. Unknown relation names and field-backed or const-backed
entries raise `SchemaError`, even when unreferenced or empty. Valid unreferenced
relations are ignored without iterating or resolving their subjects; referenced
model instances use `to_subject_ref` and accept any subject form the schema
accepts for that relation, subject to the [wildcard rule](./ZED.md#public-read-access).
For example, a model that writes a `contributor` tuple for the creating actor
after save can propose `{"contributor": [actor]}` for
`relation contributor: auth/user`. The hook is trusted self-assertion: a fact
the row does not actually carry after the write can silently grant access;
omitting a fact it does carry can deny access. The gate does not verify these
promises after writing. The application must persist the promised tuples in
the same transaction. `bulk_create()` never calls `save()`, so bulk paths must
write the promised tuples themselves.

Unknown is distinct from empty: missing filtered-constant candidate facts,
database-default/expression values, unset
insert-assigned MTI parent links, and paths or filters whose facts cannot be
established before insertion contribute `None` in
`check_new(relationships=...)`. A forward path with a later reverse
or many-to-many hop is also unknown, since that set can change on insertion.
An unknown relation denies an arm referencing it directly, through an arrow,
or through a named permission, including under intersection or exclusion.
The shared walker preserves this structural unknown through `&` and `-`;
only an independently allowed union arm can authorize without it. Unreferenced
unknown relations have no effect. This structural uncertainty is a denial,
not a caveat whose missing context the caller can supply.

For referenced backings, unresolvable configuration, non-scalar prepared FK
identities, missing targets where a fetch is performed, and missing target
REBAC identities still raise before any insert. The direct-identity fast path
does not query target existence; database FK constraints remain authoritative.
An actor-scoped adding `RebacMixin` resource instance is always saved as an
insert, even when its primary key is already populated; `force_update` and
`update_fields` are invalid for all adding resource instances. Models without
a REBAC resource type retain Django's ordinary save behavior without resolving
an actor or rewriting `force_insert`. Load an existing resource row before updating it.
`bulk_create()` accepts only instances whose exact model class matches its
queryset model.
For actor-scoped multi-table inheritance, every table in the inheritance chain
is insert-only: a child `create` grant cannot authorize updates to an existing
parent row. Attaching a child table to an existing parent requires an explicit
trusted bypass or an application command that separately checks the parent write.

`insert(obj)` is the persistence seam for prepared instances from forms or
GraphQL mutation resolvers; `create(**kwargs)` constructs an instance and calls
it. It accepts only unsaved instances of the queryset's exact model, rejects a
conflicting database alias, and saves on the queryset's write alias with its actor
pinned and its explicit sudo reason cleared after the save. Queryset scope owns
the write: an actor or sudo pinned on the instance itself is replaced, so a
prepared instance carrying its own scope saves through `instance.save()` or
`Model.objects.with_actor(actor).insert(obj)`. Domain factories that must run
for both paths override `insert` on their queryset, not `create` on the
manager; `RebacManager.from_queryset` exposes the override automatically.

**Deliberately outside the ``Backend`` ABC.** ``check_new`` is a free
function, not a backend RPC, because SpiceDB ships no "check with
proposed tuples" call. A SpiceDB-mode strategy when 0.5 lands is to
``WriteRelationships`` the proposed tuples in a sub-transaction,
``CheckPermission`` against the (now-real) row, then roll back. Until
then, ``check_new`` raises a clear ``RuntimeError`` if the active
backend's ``schema()`` is not implemented.

Limitations (0.4):

* Caveats on the **top-level virtual tuples** are not supported — known
  relations in the ``relationships`` overlay are bare ``SubjectRef`` sequences
  with no caveat name or pinned context. A virtual tuple is therefore uncaveated:
  it must match an explicitly uncaveated allowed-subject alternative or is
  treated as absent, including on virtual arrow hops. Request context cannot
  make an unsupported virtual caveated tuple valid. Caveat-conditional ``create`` permissions still
  resolve correctly for the *post-hop* targets (the real rows
  ``check_access`` walks into).
* Subject-set candidates (``auth/group:eng#member``) inside a virtual
  relation list are resolved through the backend on the real group row
  — that subject-set walk costs one dispatch level.
* A caller-supplied virtual subject of a model-backed type must use that
  model's canonical identity spelling, including subjects supplied by MCP
  create relations and subject-set candidates. A non-canonical candidate
  refuses the whole preflight; it is never silently dropped from an exclusion.
  Schema-owned const targets retain their declared wire spelling. Schema-declared
  `type:*` wildcards remain subject classes rather than concrete model IDs.

---

### Schema source and projection APIs

The schema library owns source resolution, canonical rendering and additive AST
editing. `rebac.schema.resolve_schema_path(app_config)` resolves a declared
relative/absolute source (None means `permissions.zed`), returns None for absent
files and raises for non-files. `render_zed(schema)` preserves semantic headers,
directives, caveats, bindings and expressions; `include_backing=False` explicitly
omits local relation backing for SpiceDB export. Ordinary comments and source
whitespace are not AST data. `Definition.extend(...)` returns a new definition,
rejecting name collisions and permission arms without an existing target.

`permission_object_sources(schema, resource_type, permission, object_type=...)`
reports statically named objects in positive expression branches, including
constant bindings, arrows and subject sets. Union/intersection visit both sides;
exclusion omits its right subtree. Cycles terminate; missing targets contribute
nothing; unknown AST nodes fail loudly. Generic and wildcard subjects never
invent IDs. This is syntactic over-approximation for introspection, not an access
check. `roles_reaching` delegates to this same operation.

`RebacQuerySet.scoped()` returns an eagerly scoped clone suitable for SQL
subqueries, pinning the actor resolved at the call. `scoped_for_aggregate()` also
disables instance field redaction and fails closed without an actor in both
strict modes. It preserves caller-authored filters, annotations, database alias,
ordering and SQL cardinality; it adds no joins. Callers must separately validate
field-gated projection axes and the cardinality of their own joins. Explicit
actors continue to override ambient sudo. Cloning or changing actor/action must
never retain a stale scope predicate. Boolean and SQL set combinations retain
the left queryset actor/action policy across all operands; rebinding replaces
the restriction on each operand without mutating the original querysets.
Boolean combinations require REBAC querysets so empty-query fast paths cannot
return a plain unscoped manager. Native `resource_id_attr` and
`subject_id_attr` are public top-level exports. A registered model uses its one
resource identifier for both object and subject identity; legacy User/Group models
without resource metadata retain the separate user-setting fallback.
Identity attributes must read scalar instance values: `pk` includes Django's
multi-table parent-link primary keys, and relation `attname` attributes expose
their stored values. Relation descriptors returning model objects are rejected.
The relation's underlying target field owns column conversion; a parent-link
primary key needs no consumer-specific identity override.
Model identity resolution rejects `None` and empty strings before constructing
an object reference. Empty resource IDs are model-level backend-check sentinels,
asking for any accessible row or a row-independent grant; they are not the
identities of saved rows. Proposed-row create checks use `check_new()` with
the candidate's relationship overlay.
Object and subject resolution read Django metadata through the instance, so
lazy wrappers such as `AuthenticationMiddleware`'s `request.user` retain the
wrapped model's resource type, ID attribute and subject relation.
`REBAC_TYPE_PREFIX` applies when model metadata, configured
User/Group/anonymous types, or decorators generate identity. Already canonical
`ObjectRef` and `SubjectRef` values retain their wire types unchanged.

### Compiled permissions — the LocalBackend read path

`LocalBackend` answers every persisted read from one compiler: checks,
queryset scopes, `accessible()`, `lookup_subjects()`, field gates and bulk
guards. A permission is compiled to a Django `Q` predicate over the
application's own tables and the relationship table, and the database
evaluates it when the statement runs. Field-, attribute- and const-backed
relations are read from the model columns; stored relations are read from
tuples. Nothing is built, rebuilt or verified: a model write or a tuple write
is visible to the next read. The schema walker (`rebac.schema.walker`) remains
for `check_new`, which evaluates a row that does not exist yet.

The implementation is the package `rebac.compile`: `program` (the dependency
graph of a policy), `predicate` (the compiler), `conditions` and `formulas`
(caveats), `read` (the operations) and `evaluate` (what an undecided check
still needs). `rebac.watch` holds the map of the columns a policy reads, for
the write gates.

#### Tables

The library stores no row per application row. Its tables are:

| Models | Content |
|---|---|
| `SchemaDefinition`, `SchemaRelation`, `SchemaPermission`, `SchemaCaveat` | The schema baseline (Tier 1). |
| `PackageManagedRecord` | Provenance of the baseline rows. |
| `SchemaOverride` | Runtime overrides (Tier 2). |
| `Relationship`; `RelationshipRegistry` with `RebacResource` | Stored tuples (Tier 3). Both shapes have tables; the active storage mode decides which one holds the tuples. |
| `SchemaGeneration` | One row (`pk=1`) with one column, `revision`: the published policy revision. An empty value means that no policy is published. |
| `PermissionAuditEvent` | Audit events. |

#### The predicate

The compiler has one entry point, `Compiler.holds(key, at, bound)`. `key` is
`(resource_type, name)`: a relation or a permission. `at` says where the
object is in the statement being built. `bound` is `LOWER` or `UPPER`
(see [Bounds](#bounds)). The result is a `Q` whose value is never `NULL`.

An object is an identity expression, never "a row of its model":

```
At(resource_type, ref, key, row)
  ref   an expression in the current query: a column, an OuterRef, a value
  key   the model field ref is a value of; None when ref is a wire id
  row   True when the current query's own row is the object
```

A queryset scope evaluates the predicate at the model's identity column with
`row=True`. A point check evaluates it at a wire id bound as a parameter, so
an object that exists only in tuples takes part in grants and in exclusions
without a model row. Membership of such a statement constant is an `EXISTS`
with an equality on the key, so the database looks up one row or one
resource's tuples instead of building the set of every qualifying object.

A model column is never converted. Where a tuple column meets a model column,
the tuple column is converted with `identity_codec(...).to_column()` inside
the tuple subquery; a wire id that is not a canonical identity of that model
becomes `NULL` and matches nothing. A foreign key with `to_field`, or a model
with `Meta.rebac_id_attr`, is joined through the field it refers to. Two
tuple columns are compared as text. Identity codecs support integer, text and
UUID fields; `rebac.E014` reports a field whose identity cannot be compared
with stored tuples.

Each construct reads exactly one kind of source:

| Construct | Source | Predicate |
|---|---|---|
| Stored relation | tuples | The object is the resource of a live tuple of the relation whose subject admits the actor. |
| Field-backed relation | model columns | The backing path, with its filters in the same `filter()`, reaches a subject row that admits the actor. |
| Attribute-backed relation | the subject model | The object is the container named by a column value of a subject row that admits the actor. A fixed `resource`/`value` binding owns that one container; other ids of the type are read from tuples. |
| Const-backed relation | none | The fixed target admits the actor. Filters are tested on the object's row. |
| Arrow `via->p` | as `via` | The same test, where "admits the actor" is "the actor holds `p` on the subject". The arrow follows the subject's object, whatever relation suffix a declared subject shape carries. |
| `authenticated`, `anonymous` | none | A constant decided from the actor. |
| `+` | | The union of the operands at the same object. |
| `&` | | `AND`. |
| `-` | | `left AND NOT right`, with `right` compiled at the opposite bound. |

A tuple is live when it has no expiry or, on a relation declared `with
expiration`, when `expires_at` is later than the statement's clock. Its
subject shape and caveat name must match one declared allowed-subject
alternative. Its subject admits the actor when the subject is:

- the actor itself, for an actor without a relation suffix;
- the wildcard of the actor's type, when the relation declares it and the
  actor has no relation suffix;
- a subject set `T:s#rel`, when the actor holds `rel` on `T:s` or is that
  subject set itself.

The third case is the arrow mechanism applied to a relation. Where the set
is a stored relation, the sets that hold the actor are decided before the
statement (see [The actor's stored sets](#the-actors-stored-sets)); otherwise
the membership is compiled inline.

The compiler reads model rows through `_base_manager` and applies no
application filter, so soft-deleted rows are read like any other.

Override composition is `((baseline ∪ extends) − disables) & tightens`. The
composition module keeps each contributing override's deadline on its tagged
arm or operand, and the plain `compose()` derives from that same
implementation. The policy is compiled from the baseline and every extend,
loosen, disable and tighten row, expired rows included, and the statement
evaluates the deadline: an extended or loosened arm is `arm AND now <
deadline`; a disabled or tightened site is the identity from its deadline on.
A recaveat override replaces a CEL expression, which SQL cannot select, so
the expression in force is chosen when the policy is prepared and the
statement is fenced to the interval in which that choice holds.

#### Bounds

Every predicate is compiled as a lower bound or an upper bound of the true
set. A lower bound leaves out what SQL cannot decide: a tuple whose caveat is
undecided, a chain longer than the depth bound. An upper bound keeps it.
Negation swaps the bounds: in `left - right`, `right` is compiled at the
bound opposite to that of `left`. **Only a lower bound authorizes.** An
approximation therefore never grants, on either side of an exclusion.

Where neither a caveat nor a recursion is in reach of a permission, the two
bounds select the same objects.

#### Statement shape

- **A disjunction over a row is one `id IN (UNION of id sets)`.** Each arm is
  a queryset of the model's identities and the arms are united, so no `OR` is
  left above a subquery of the row's model. At an identity that is not a row
  (a tuple column, a parameter) the arms are joined with `OR`.
- **A negated disjunction is a conjunction of `NOT EXISTS`**, one per arm.
  `NOT IN` is never emitted.
- **Membership is two-valued.** An `IN` arm is `ref IS NOT NULL AND ref IN
  (…)`, and the id set excludes `NULL`.
- **An arm over a multi-valued path is its own subquery.** A path or filter
  that crosses a reverse or many-to-many relation never shares its join with
  another arm. An arm over a single-valued path is inlined into the caller's
  filter when the object is the row. Within one backing, the path and its
  filters stay in one `filter()` call, hence one join.
- **A conjunct that nests a subquery goes last.** SQLite measures an
  expression's depth through its subqueries, and a chain of `AND` is
  left-deep.

No raw SQL, trigger, database function or hand-written statement is used,
with two deliberate exceptions, in both of which the SQL is Django's own:

1. **Compiled id sets and kept statements.** An uncorrelated id set is
   compiled by Django once, through the queryset's own `Query.as_sql()`, and
   embedded as text by a custom expression. Django resolves an embedded
   queryset again inside every enclosing `filter()`, which makes the cost of
   building a nested statement grow with its depth times its size; an
   uncorrelated set refers to no outer query, so its own compiler produces
   the same SQL the nested resolution would. A whole statement is kept the
   same way (see [Kept statements and the policy fence](#kept-statements-and-the-policy-fence)).
2. **The identity conversion's guard.** `codec.to_wire()` and
   `codec.to_column()` wrap an expression in a validity check that reads it
   about ten times and depends only on the identity field, the direction and
   the connection. Django compiles that guard once around a placeholder; each
   use compiles its own expression once and takes the placeholder's
   positions. `tests/test_codec_cache.py` pins the cached SQL and parameters
   equal to the uncached ones for integer, text and UUID identities.

#### Facts about fixed objects

An arrow over a const-backed relation (`admin->member` on a fixed role) is
evaluated at a fixed object, so its value does not depend on the row. A
queryset scope decides such a fact when its statement first asks for it:

1. The fact is probed by its own small statement, at the bound the statement
   needs. A union asks for its constant arms first and stops at the first
   one that holds, and a lower bound asks for an upper one only under an
   exclusion, so a statement decides few of the facts in reach of its
   permission. A fact about a stored set (membership of the admin role held
   in tuples) is answered from the actor's stored sets and needs no probe.
2. The scope statement is compiled with each decision folded in as a
   constant. For a member of the admin role the arm is true, and a union
   that contains it needs no predicate over the rows; for everyone else the
   arm disappears, and with it the text it would repeat in every nested
   permission.
3. Each decision used is witnessed inside the scope statement: the fact's own
   predicate, or its negation for a decision that was false, is a condition
   of the statement. A decision that does not hold when the statement runs
   yields no rows.

A kept scope statement is keyed by the facts it asked for and their values,
in the order it asked. The order is learned from the first build and
replayed afterwards, so a kept statement costs its probes and nothing else.

A fact whose target lies in a recursive component that is being unrolled is
evaluated inline instead. Point checks and the other operations evaluate the
same sub-expression inline, as an uncorrelated predicate. Recursion over a
stored relation at one fixed object follows the relation's tuples from that
object (the roles a role includes), a lookup by resource per level, and
names the base once.

#### The actor's stored sets

A relation used as a subject set (`auth/group#member`, a role's `member`) is
a *stored set* when it has no backing and every subject set it admits is a
stored set too. Whether the actor belongs to such a set depends on tuples
alone, and on the actor, not on the resource. An operation therefore decides
it first:

1. **Expansion.** Starting from the actor, the operation reads the tuples
   that put the actor, or a set already found, into a stored set in reach of
   the permission, and repeats until a read finds no new set: one statement
   per level of nesting and one that finds nothing. Each tuple must be live,
   match a declared subject shape and, for the bound being decided, carry an
   admitted caveat. The result is the fixed point, so it is exact, data
   cycles included; `REBAC_DEPTH_LIMIT` does not apply to it. With a caveat
   declared on a set in reach the expansion runs once per bound.
2. **Binding.** The statement tests membership in a stored set as `id IN
   (list)`. A constant target (`admin->member` on a stored role) folds to
   true or false without a probe. An actor in no set compiles the arm away.
3. **Witness.** A statement that authorizes (the lower bound of a check, a
   scope, an enumeration) re-reads the decision in its own snapshot: every
   set of the lower bound still has the tuple that put it there, so no grant
   rests on a membership that is gone; and no live tuple puts the actor into
   a set outside the upper bound, so no exclusion misses a membership that
   has appeared. When either fails the statement selects nothing.

Inside an evaluator scope (a request under `ActorMiddleware`, an explicit
`evaluator_scope()`) the decision is kept per actor, context and stored sets
in reach, until a tuple is written in the process or the scope ends. A kept
decision can be stale when another process changes a membership; the witness
then selects nothing, so staleness denies and never grants. The depth probe
of a check carries the same witness: sets that are no longer the actor's are
a change for the residual evaluator to answer afresh, not a depth to report
as `PermissionDepthExceeded`. Outside a scope every operation decides afresh.

Several actors of one shape can be decided together
(see [Bulk checks](#bulk-checks)).

A set that admits a column-backed set (`org/team#staff` over a foreign key)
is not a stored set, and an actor found in more than 256 sets is not
decided: membership is then compiled inline, as the closure described under
[Recursion](#recursion).

#### Decided rows

A scope reaches other tables through arrows: a file through its folder, a
part through its message and that message's thread. Compiled inline, each
arrow is a subquery whose size the planner cannot know, and a hierarchy is
tested for every row of its table. A scope therefore decides the small sets
first:

1. **Arrow targets.** For an arrow over a field- or attribute-backed
   relation, the rows of the target model on which the actor holds the target
   permission are selected by their own statement. When they fit in what
   the statement may still bind, the arrow is compiled as `column IN (keys)`.
   The target's statement is compiled the same way, so its own arrows are
   lists too.
2. **Hierarchies.** For `p = base + parent->p` over a self foreign key, the
   rows that hold `base` are selected, then their children by the parent
   column, level by level to `REBAC_DEPTH_LIMIT` or until a level is empty:
   the same rows as the inline form, reached from the seeds instead of from
   every row. A scope over the hierarchy's own model uses the set as well.
3. **Fallback.** One statement binds at most 5,000 decided rows, over all
   its sets, in the order it asks for them; a decision reads one row more
   than is left and stops. A set that does not fit, a target every row of
   which holds the permission, and a node that is being decided stay inline,
   as the subquery or the ancestor chain described above. So does an arrow
   back into a recursion that is being unrolled: a decided set is the rows
   that hold the permission with the whole depth limit to spend, and inside
   its own recursion part of that depth is spent already.
4. **Witness.** A decided set is a lower bound. The scope statement re-reads
   it in its own snapshot: each listed row still holds the permission (the
   permission's own predicate, evaluated on the listed keys only). A
   hierarchy is kept by level: the seeds still hold `base`, and each row of a
   level still hangs under a row of the level above it. A chain of parents
   therefore loses a level at each step and ends at a seed, so rows that have
   closed into a cycle, or have moved deeper than they were found, fail the
   witness; a row moved under another row of the level above passes it. The
   witness is compiled with the parameters of the statement that carries it,
   so it is read at that statement's instant: a grant that expired after the
   decision fails it. When a witness fails the scope selects nothing; the
   next operation decides afresh.
5. **References the database keeps.** A witness reads the rows that exist. A
   key is therefore bound as `column IN (keys)` only where a stored reference
   proves its row: a path that ends in a forward foreign key, on which every
   forward foreign key is under a database constraint in a table Django
   manages, and whose target, if it is a multi-table model, is linked to its
   parents the same way. Every other path (a foreign key with
   `db_constraint=False`, an unmanaged table, a database that enforces no
   constraint, a path that ends in a reverse relation or crosses a
   many-to-many one, which Django may read without the target's table) reads
   the keys through the target's rows. A hierarchy over a parent column that
   is not kept is decided by the permission's own predicate, not followed
   from its seeds; that predicate tests every row's ancestors, so such a
   hierarchy is slower to decide. Django creates its constraints deferred,
   so the proof holds for committed rows: a row the reader's own transaction
   wrote and has not committed can still name a row that is gone, in a
   transaction that will fail at commit.

Decided rows are used by queryset scopes and by what is built on them
(`accessible()` without a context, bulk guards, the backed-edge gate over
more than four rows). A point check stays one statement at its one object.
A statement that carries decided keys is compiled for that operation and is
not kept. Nothing is kept between operations: the sets depend on model rows,
which change without the library seeing it.

A union is compiled constants first (an arrow over a const-backed relation,
`authenticated`), and stops at the first arm that holds outright, so an
administrator's scope decides nothing.

#### Kept statements and the policy fence

A statement is compiled once and kept as Django's SQL with placeholders. The
key of a kept statement is the policy (database alias, revision, baseline and
override rows), the operation and permission, the shape of the actor, the
database vendor, `REBAC_DEPTH_LIMIT`, the active relationship model, the
stored sets decided for the actor and, for a scope, the model and the
decisions of its facts. Actors that belong to the same sets share their
statements. The shape of the actor is
its type and relation suffix, whether its id is a canonical identity of its
model, whether the schema names its id (an allowed subject with an id, a
constant target), and whether it is authenticated or the anonymous singleton.
The actor's id, the clock, the resource id of a point check and the revision
of a manual schema are parameters bound when the statement runs.

Two bounded caches per process hold the prepared policies and the statements,
512 entries each, least recently used evicted first. A statement that
carries caveat verdicts is compiled at each use. A policy that has a recaveat
override with a deadline is prepared at each use.

Every statement carries the policy fence: it selects from the
`SchemaGeneration` row and requires the revision it was compiled for. A scope
predicate also records the revision at which the queryset was built. After a
policy change, a statement compiled for the earlier revision selects no rows
and a point check in flight answers `NO`; the next operation reads the new
policy. Before any policy is published the fence matches nothing and reads
are closed. A manual schema (`set_schema()`, `rebac.testing.install_schema()`)
has a digest of the schema as its revision, and the generation row must
exist.

#### The clock

The clock is the application's: `django.utils.timezone.now()`, read through
`rebac.clock.application_now()`, never the database's. It is one parameter per
statement, so a fact that appears under both polarities is read at one
instant. Tuple expiry and override deadlines compare with it when the
statement runs; nothing is re-evaluated when time passes.

#### Recursion

A node is recursive when it lies on a cycle of the policy's dependency graph
that traverses a relationship: a self-arrow (`parent->read`), a nested group
(`relation member: auth/user | auth/group#member`), a role that includes
roles. Recursion is unrolled to `REBAC_DEPTH_LIMIT` levels (default 8): the
bound counts the relations followed since the recursive component was
entered, whichever of its permissions they pass through. Stored sets decided
for the actor are exact and are not unrolled.

A cycle of permissions that refer to each other at the same object, without
an arrow or a subject set, is not recursion in this sense. It is resolved
from the schema as its least fixed point and consumes no depth.

| Recursive shape | Statement | Upper bound adds |
|---|---|---|
| A relation whose subject set is itself (nested groups), when it is not decided as a stored set | The sets that hold the actor directly, then the sets that contain them, level by level. The closure starts from the actor, so it is the actor's own. | One more hop reaches a set outside the closure. |
| `p = base + parent->p` over a self foreign key | The row inherits when it, or one of its nearest `REBAC_DEPTH_LIMIT` ancestors, holds `base`: a chain of joins on the parent column, with `base` tested once against all of them. Arrows of `base` to a like-named permission of another type (`drive->read`) are not recursive arms. | The row has more than `REBAC_DEPTH_LIMIT` ancestors. |
| `p = base + parent->p` over another backed path to the same model (reverse, many-to-many, filtered) | The rows that hold `base`, then the rows whose path reaches the level below. | One more hop reaches a row outside the closure. |
| `p = base + parent->p` over a stored relation | The resources of the edges whose subject holds `base`, then the resources of the edges above them. | One more hop reaches an object outside the closure. |
| Any other shape (a cycle through several types, a recursive arm that carries an override) | The body nested in itself. | Every object reached past the bound. |

The contract:

- **A lower bound holds only what is provable within the bound.** A queryset
  scope and `accessible()` include a row only when a chain of at most
  `REBAC_DEPTH_LIMIT` hops grants it. They never raise for depth.
- **A point check never answers by truncation.** When the lower bound does
  not hold and the upper bound holds only because the bound was reached, the
  check raises `PermissionDepthExceeded`. It never answers `NO` for a chain
  it did not follow to its end, and never `HAS`.
- **A closure that converges is exact.** For the three closure shapes, when
  one more hop reaches nothing new the closure is complete, data cycles
  included, and the upper bound equals the lower one: the check answers
  `HAS` or `NO`.
- **A self foreign key has no convergence test.** A row with more than
  `REBAC_DEPTH_LIMIT` ancestors, which includes every row on a cycle of the
  parent column, is granted when an ancestor within the bound holds `base`;
  otherwise a point check on it raises.
- **Permissions that recurse through each other have none either.**
  `folder#view = viewer + project->access` with `project#access = member +
  folder->view` is the nested shape. Over a data cycle a check that is not
  granted within the bound raises instead of answering `NO`
  (`tests/test_compile_regressions.py` pins it as an expected failure).

A recursive component is refused in two cases:

1. a permission of the component is reached through the right-hand side of
   `-` inside the component: `read = viewer - parent->read`, `read = viewer -
   read`;
2. one expression uses permissions of the component more than once:
   `read = parent->read + parent->read`, or two arrows that both lead back
   into the component.

Intersecting a recursive permission with something outside the component, or
excluding something outside it, is accepted: `read = (viewer + parent->read)
- banned` and `read = parent->read & member` compile. A `tighten` override on
a recursive permission is therefore accepted. A `disable` or `extend`
override that puts the recursive arrow on an excluded side is refused when it
is written.

A refused component raises `SchemaError`, whose message ends with
`(rebac.E016)`: when a permission is evaluated, and at the exit of the policy
write that would publish it (see [Writes](#writes)), so a policy written
through the owners never holds one. `rebac.E016` reports it as a system
check. `rebac.compile.program.program_errors(schema)` returns the same
diagnosis for a parsed schema.

Statement size grows linearly with `REBAC_DEPTH_LIMIT`. Measured on the scope
statement of the test schema's recursive folder permission, on SQLite:

| Shape | SQL per level | Limit at which SQLite refuses the statement |
|---|---|---|
| Stored relation | 2.5 KB | 21 |
| Filtered backed path | 3.6 KB | 19 |
| Self foreign key | under 0.1 KB | none up to 32 |

SQLite refuses with `Expression tree is too large (maximum depth 1000)`. The
figures are measurements of one schema, not bounds.

#### Caveats

A backed relation carries no caveat, so conditions exist only on tuples. A
statement cannot evaluate CEL. When a caveated relation is in reach of the
permission being evaluated, through references, arrows and subject sets, the
operation reads the distinct caveat instances of those relations in one
query: name, pinned context and `caveat_key`. It decides each instance in
Python against the pinned and the supplied context, and binds the decided
keys into the statement:

- a tuple is in a **lower** bound when it has no caveat or its key was
  decided true;
- a tuple is in an **upper** bound unless its key was decided false.

A tuple written after the preparation, with a key that was not decided,
counts in an upper bound only. There is no cap on the number of distinct
instances. A permission with no caveated relation in reach runs no such
query.

`caveat_key` is a digest of the caveat name and the pinned context, written
with the tuple by every supported write path: instance saves, raw fixture
saves, `write_relationships()` and `bulk_create()`. Reads use the stored key
with the stored context and never recompute it, because a database can
render a stored JSON number differently from the text that was written.

Sets decide caveats as point checks do, so `accessible(context=…)` agrees
with `check_access(context=…)`. Enumeration and scopes return definite
results only. A queryset scope has no request context: a tuple whose caveat
needs one is not in its lower bound.

#### Operations

| Operation | Implementation |
|---|---|
| `check_access()` | The actor's stored sets, when one is in reach. Then the lower bound at the resource id, selected from the fenced generation row: one statement, which alone can answer `HAS`. When it does not hold and neither a caveat nor a recursion is in reach, the answer is `NO`. Otherwise the upper bound is a second statement: when it fails, `NO`. Otherwise, when a recursion is in reach, a third statement probes whether the uncertainty is depth, and the residual evaluator names what is undecided. |
| `check_bulk_permissions()` | The answers of `check_access()`, with the work shared: see [Bulk checks](#bulk-checks). |
| Queryset scope (`queryset_filter()`) | `Model.filter(holds(key, At(type, identity column, identity field, row=True), LOWER))`, decided when the statement that embeds it is compiled, after the small row sets in its reach (see [Decided rows](#decided-rows)). |
| `accessible()` | The lower bound over each part of the type's universe: the model's rows, the ids that tuples name at either end, constant targets, attribute containers, and field-backed targets whose model stores no rows. Wildcard and empty ids are excluded and the result is deduplicated. |
| `lookup_subjects()` | Candidates are the subjects that tuples, backed columns and constants name on a path from the one resource; each is tested by its own point check, so the cost grows with the number of candidates. To ask about subjects already in hand, use `check_bulk_permissions()`. |
| Model-level check (empty resource id) | A row-independent grant, or any accessible identity of the type. |
| `grants_all()` | The lower bound at the empty id: only a row-independent arm can hold there. |
| Backed-edge write gate | `write` on the affected declaring rows. More than four ids that are rows of the type's model are tested by one scoped statement per 500 ids; the others by the lower bound, one by one. |
| Bulk write guard | Every resource id of the statement must be in `accessible()` for the action. |
| Field read gate `read__<field>` | `accessible()` for that permission. |
| `check_new()` | The walker over the candidate's proposed relations; an arrow into an existing object is answered by `check_access()`. |

The residual evaluator (`rebac.compile.evaluate`) never authorizes. It runs
only after SQL reported "lower bound false, upper bound true", reads the
tuples and columns on the paths of that one object, and reduces the
expression to the caveat instances and depth cuts that can still change the
answer. If a depth cut is among them the check raises
`PermissionDepthExceeded`; otherwise it returns
`CONDITIONAL_PERMISSION(missing=[...])` with the parameters still needed. The
set does not depend on the order of arms, rows or tuples. When nothing is
left undecided the answer is `NO`: a change observed between the statements
cannot become an allow.

A type whose model is unmanaged has no table the library reads without being
asked to: its objects are the ones that tuples, constants and backings name,
as for a type with no model. A backing that names a column of an unmanaged
model still reads it.

##### Bulk checks

"Which of these fifty users may read this thread" is fifty checks that
differ in the actor only. Asked one by one, each decides its actor's stored
sets and runs its own statement. `check_bulk_permissions()` answers them
with a number of statements that does not grow with the number of items, up
to a chunk of 50:

1. **Shared preparation.** The policy is read once. Items with the same
   permission and context share one set of caveat verdicts.
2. **Stored sets, together.** The actors of a chunk that have the same shape
   are expanded together: one statement selects the tuples that name any of
   them, and one statement per level of nesting selects the tuples that name
   the sets found, until a level finds no new set. Each actor's sets are
   then followed in memory from the actor itself through those tuples, so
   an actor's set is one that a chain of selected tuples leads to from that
   actor, and the tuple that first put it there is its support. The result
   is what the actor's own expansion would find, and it is witnessed the
   same way.
3. **Bounds, together.** The lower bound of each item is the statement
   `check_access()` would run, and the lower bounds of a chunk are the
   columns of one statement over the generation row. Each column carries its
   own fence and witness. Items the lower bound does not grant, and that
   have a caveat or a recursion in reach, get their upper bound the same
   way, and those still undecided their depth probe: three statements at
   most. A statement is cut once its text passes about 64 KB, so a
   permission whose statement at one object is long (a recursion over
   tuples) shares a statement between fewer items. The residual evaluator
   then runs per undecided item, as in `check_access()`.
4. **The rest.** An item with an empty resource id, or an unknown type or
   action, is answered by `check_access()`.

The decided sets are kept in the evaluator scope like those of a single
check; the call opens a scope when none is open. A membership that another
process changes during the call makes the witness of the items it affects
fail for the rest of the call: they answer `NO`, as in any evaluator scope.

#### Writes

A write changes the application's columns or the tuple table and nothing
else. The write gates keep the rules of the
[CRUD enforcement matrix](#crud-enforcement-matrix) and of invariant 5d
(a change to a backed edge requires `write` on every affected row of the
declaring type), with the same entry points: `save_base` and `delete` on the
mixins, the scoped and tracked querysets, the injected base manager, the
related-manager wrappers and the explicit-sender receivers.

- **The watch map.** `rebac.watch.watched_for(schema)` resolves, from model
  metadata alone, the models and columns that the schema's backings read:
  identities, backing paths, filters, through rows and inherited fields.
  `gate_policy(using)` returns the installed schema with its watch map, or
  `None` before a policy is published on the alias and while the library's
  tables are missing or incomplete during migrations; a write is then not
  gated as a backed edge, and reads are closed.
- **One transaction.** `rebac.watch.model_write(model=, using=, names=)` is
  the transaction an owned write runs in. It yields whether the installed
  policy reads a column the write can change.
- **Inserts** are authorized by `check_new` per candidate, including
  `proposed_relationships()`; a query over existing rows cannot authorize a
  row that is not there.
- **Stored values under a row lock.** A gate on a column the policy reads
  compares the stored value with the proposed one and checks `write` on the
  declaring rows of the old and the new edge. It reads the stored value with
  `SELECT … FOR UPDATE` when the database supports it and a transaction is
  open, which is the case in every owned write.
- **The key rule for queryset writes.** A queryset `update()` of a column
  the policy reads, and a tracked queryset `delete()` on a model the policy
  reads, select the primary keys of the statement's rows, gate exactly those
  rows from their stored values, and write exactly those rows by key, 5,000
  keys per statement. A row that starts matching the caller's filter in
  between is neither gated nor written. A denied row denies the whole
  statement.
- **Tuple writes.** `write_relationships()` validates every tuple against
  the effective schema and saves the rows in one transaction; model save
  signals run for each row. `delete_relationships()` and queryset deletes on
  the relationship models delete in one transaction. Queryset `update()` on
  relationship rows raises `NotImplementedError`, because it can change a
  tuple's identity without a tuple write; use `delete_relationships()` and
  `write_relationships()`. `bulk_create()` with `update_conflicts=True` must
  target the tuple unique constraint and update only `caveat_context`,
  `expires_at` or `written_at_xid`; anything else raises `ValueError`.
- **Deleted objects.** When a resource or subject row is deleted, the
  explicit-sender `post_delete` receiver removes the tuples that name it at
  either end, so a reused identity does not inherit a grant.
- **Third-party models** listed in `REBAC_TRACKED_MODELS`, and the configured
  User and Group models, are gated on instance saves by an explicit-sender
  `pre_save` receiver. Their deletes and their queryset `update()`,
  `bulk_create()` and `bulk_update()` are not gated. The library opens no
  transaction around their saves; the gate locks the row only when the
  caller is in one.

**Policy writes.** The five policy models (definitions, relations,
permissions, caveats and overrides), `rebac sync` and
`rebac.schema_changes()` serialize on the `SchemaGeneration` row: the owner
locks it, creating it when absent, before it reads or writes policy rows.
Each write publishes a fresh revision. When the outermost owner exits, the
composed policy is validated once under that lock, so two changes that are
each valid cannot combine into a refused one; a refusal raises `SchemaError`
and rolls the transaction back. A policy that was already refused when the
owner started is being repaired and is not validated again. Whether the
policy's backings resolve against the current models is a system check
(`rebac.E009`), not a condition of writing a row, so a stored policy can be
repaired while it disagrees with the models.

**What a gate does not lock.** A gate locks the rows it decides on. It does
not lock the tuples or the other rows its decision reads, and writers of
different rows share no lock. A grant revoked concurrently is not serialized
with a write that was authorized before the revocation committed. An
application that needs a stronger order uses a stronger transaction protocol
of its own.

#### What a consumer must know

- **There is no build step.** `migrate` and `rebac sync` are the whole
  setup. Fixture loads, raw SQL, bulk writes and data migrations need no
  command afterwards: reads see the rows as they are. A write that bypasses
  the owners is not gated, which is a question of authorization, not of
  freshness.
- **Choose `REBAC_DEPTH_LIMIT` to cover the deepest recursive chain in the
  data.** A scope silently omits a row whose grant lies past the bound; a
  point check on it raises `PermissionDepthExceeded`. Raising the limit adds
  one level of SQL to every recursive statement.
- **An unresolvable backing fails the reads that reach it.** A field,
  attribute or const backing that does not resolve against the models raises
  `SchemaError` citing `rebac.E009` from every read whose permission reaches
  the relation. Permissions that do not reach it are still decided.
- **`rebac.E014`**: every identity that a statement compares with stored
  tuples needs an integer, text or UUID field without a custom conversion.
- **`rebac.E015`**: the models a permission reads must share the
  relationship database alias, because one statement joins them.
- **`rebac.E016`**: the shape of recursion (see [Recursion](#recursion)).
- **`rebac.E018`**: a model on a backing path needs a write owner
  (`RebacMixin`, `RebacTrackedMixin`, or an entry in `REBAC_TRACKED_MODELS`),
  so that writes to it are gated.

#### Known limits

- An operation whose permission reaches a stored set costs the expansion
  statements before its own, once per evaluator scope; outside a scope, at
  every operation.
- A scope costs one statement per decided arrow target in its reach, and one
  per level of a decided hierarchy, at every operation. They are index
  lookups for a sparse actor; their number follows the schema and the depth
  of the accessible subtree, not the size of the tables.
- Permissions that recurse through each other are unrolled without a
  convergence test (see [Recursion](#recursion)).
- Past the 5,000 rows a statement may bind, a hierarchy is compiled inline
  and tested for every row: on a PostgreSQL 16 table of 123,000 rows, a page
  for an actor that holds 4,100 of them took 55 ms decided and 700 ms inline.
  An arrow's subquery is the cheaper form for a few hundred rows (9 ms
  against 30 ms at 600) and the dearer one for more (172 ms against 61 ms at
  2,000). These are measurements of one synthetic data set; the limit has
  not been tuned on production data.
- Each stored set an actor belongs to costs every scope statement a list
  entry and a witness subquery: an actor in 250 sets paid about 100 ms a
  page on that data set, most of it compiling, where an actor in 10 paid 16.
- A hierarchy whose parent column names its row by a nullable column
  (`to_field`) never gives a row without a value in that column its own
  base in the inline form, while a point check does: a scope past the row
  limit omits such a row.
- Statement size follows the unfolded permission, times the depth limit for
  a recursive node. A deployment whose chains are deeper than the default
  raises `REBAC_DEPTH_LIMIT` and pays for it in every recursive statement.
- SQLite, supported for tests, refuses a statement whose expression tree is
  deeper than 1,000; the tuple- and path-backed recursive shapes of the test
  schema reach that at a limit of about 20, and an actor in more stored sets
  than are decided reaches it at a limit of 8 where two kinds of set nest,
  because the sets are then compiled inline.
- Query plans on PostgreSQL tables of tens of millions of rows have not been
  verified. A trial at that scale is in progress, and this document makes no
  claim about its result.
- Third-party tracked models are gated on instance saves only.
- The gates inspect the ORM call, not the SQL (invariant 5d); the cases
  pinned for proposals 0011 and 0013 remain open.

### Schema introspection

Tooling that needs to answer "which relation or role reaches this permission?"
should read the effective schema through `backend().schema()` and use
`rebac.schema.introspection`, not walk AST node classes directly. The stable
helper surface is `permission_sources(schema, resource_type, permission)`,
`relation_dependencies(...)`, `permissions_reaching_relation(...)`, and
`permission_object_sources(...)`.
`PermissionSources` reports direct relations, arrows as `(via_relation,
target_permission)` pairs, built-in actor terms, and traversed same-definition
sub-permissions. The AST node types remain private implementation details so
the schema language can grow without downstream walkers silently misreading new
nodes.

`rebac.schema.accessible_is_exact(schema)` and
`live_backed_resource_types(schema)` remain public with their 0.18.2 schema-only
behavior. The former reports the absence of builtin-actor and caveat constructs;
it does not add a recursion restriction or certify that the policy compiles. The latter
conservatively propagates live-backing dependencies to resource types, including
through arrows, const targets and subject sets, for decision-cache exclusion.
Field and attribute backings seed that closure; constants and builtin actors
do not seed it on their own.

Role convention tooling should use `rebac.roles.roles_reaching(...)`, passing a
`role_resource_type` such as `"storage/role"` or `"platform/role"` rather than
assuming a single namespace.

---

## `RebacMixin` — model-layer enforcement

The headline feature. By inclusion, every model operation is gated against the effective user.

### What gets installed

1. `objects = RebacManager.from_queryset(RebacQuerySet)()` replaces the default manager.
2. `_default_manager` points at it; the base manager is an **owning, unscoped** manager. When the model declares none, the metaclass injects `_rebac_base = TrackedManager()` and names it in `base_manager_name`, even when consumers declare their own `Meta`. A model that declares `Meta.base_manager_name` keeps its manager, and so does a model whose parent model declares one: every parent model is read in base order, not only the first as in Django's own fallback, so listing `RebacMixin` first does not discard a base manager declared by an abstract parent. `base_manager_name = "_rebac_base"` on the model opts back into the injected one. A declared base manager must be built over a `TrackedQuerySet` subclass (`models.Manager.from_queryset(...)`); class creation raises `ImproperlyConfigured` otherwise, the scoped `RebacManager` included. It may add methods and refuse writes (an append-only queryset), but it must return every row: permission statements read a model's rows, and the write gates read their stored values, through `_base_manager`, so a base manager that filters rows would hide those rows from both. `rebac.E023` reports a declared base manager whose `get_queryset()` returns another queryset class or carries a filter. The base manager never applies actor scope to reads. Its writes that change a backed FK edge (reverse-FK `add(bulk=True)`, `update` of a watched FK) run the backed-edge gate of invariant 5d; an `update` of a watched scalar column through the base manager is not gated (proposal 0013), and collector `SET_NULL` rows are gated under the ambient actor only (proposal 0011).
3. `save_base` owner — create/write and field gates before consumer `pre_save` receivers.
4. `delete` owner — root gate and a deletion ContextVar carrying the root actor/bypass for collector children; explicit-sender `pre_delete` gates those children only. Owners batch identity tuple cleanup.
5. Queryset materialisation hooks (`_fetch_all()` and iterators) stamp the resolved actor onto every loaded instance. `from_db()` snapshots original field values for write checks.
6. `Meta` extension — the metaclass captures `rebac_resource_type`, `rebac_id_attr`, `rebac_default_action` and `rebac_subject_relation` (the `REBAC_META_OPTIONS` tuple), strips them before Django's `Options` sees them, and re-attaches them on `_meta`. `rebac_subject_relation` makes `to_subject_ref(instance)` emit the model's object reference as a subject set (`auth/group:<id>#member`); `rebac.E011` checks the relation exists. See [proposal 0006](./proposals/0006-model-subject-identity.md). A model with no `Meta` of its own takes the Django options (`ordering`, `indexes`, `constraints`, `permissions`, verbose names and the rest of Django's `DEFAULT_NAMES`) of the first base that has a `Meta`, as Django itself does. The `rebac_*` options are per model: they pass to a child only through a `Meta` the child writes (`class Meta(Parent.Meta)`), never to a child with no `Meta`.

### Manager and queryset surface

```python
ActorLike = Union[SubjectRef, User, Group, "AnyRebacSubject"]
# Anything that can resolve to a <type>:<id> SubjectRef:
#   - django.contrib.auth User instance       → auth/user:<id>
#   - django.contrib.auth Group instance      → auth/group:<id>#member
#   - any class decorated with @rebac_subject   → <type>:<id>
#   - a SubjectRef passed through unchanged   (covers agents/grant, agents/agent,
#                                              auth/apikey, custom)

class RebacManager:
    def get_queryset(self) -> RebacQuerySet: ...

    # Primary, generic actor scoping. Accepts any ActorLike.
    def with_actor(self, actor: ActorLike) -> RebacQuerySet: ...

    # Typed shorthands — all eventually call with_actor() internally.
    def as_user(self, user) -> RebacQuerySet: ...
    def as_agent(self, agent, *, on_behalf_of=None) -> RebacQuerySet: ...
    def with_action(self, action: str) -> RebacQuerySet: ...           # override read-scope action
    def on_field_deny(self, mode: FieldDenyMode) -> RebacQuerySet: ... # allow/redact/omit/raise
    def for_write(self) -> RebacQuerySet: ...                          # write-target: row scope kept, field redaction off
    def rebac_select_related(self, *fields) -> RebacQuerySet: ...      # guarded to-one joins
    def rebac_prefetch_related(self, *lookups) -> RebacQuerySet: ...   # scoped protected prefetches

    def sudo(self, *, reason: str) -> RebacQuerySet: ...                # gated by REBAC_ALLOW_SUDO
    def system_context(self, *, reason: str) -> RebacQuerySet: ...      # framework-job bypass, NOT gated
    def actor(self) -> SubjectRef | None: ...                           # introspection
    def effective_actor(self, *, strict: bool = False) -> tuple[SubjectRef | None, bool]: ...

class RebacQuerySet:
    def with_actor(self, actor: ActorLike) -> Self: ...
    def as_user(self, user) -> Self: ...
    def as_agent(self, agent, *, on_behalf_of=None) -> Self: ...
    def with_action(self, action: str) -> Self: ...                    # override read-scope action
    def on_field_deny(self, mode: FieldDenyMode) -> Self: ...          # override field-read deny mode
    def for_write(self) -> Self: ...                                   # write-target: row scope kept, field redaction off
    def rebac_select_related(self, *fields) -> Self: ...               # select_related + related read guard
    def rebac_prefetch_related(self, *lookups) -> Self: ...            # prefetch_related + scoped targets
    def sudo(self, *, reason: str) -> Self: ...                         # gated by REBAC_ALLOW_SUDO
    def system_context(self, *, reason: str) -> Self: ...               # framework-job bypass, NOT gated
    def effective_actor(self, *, strict: bool = False) -> tuple[SubjectRef | None, bool]: ...

    def insert(self, obj): ...                                       # persist a prepared instance

    # Standard queryset ops with REBAC-aware overrides:
    def update(self, **kwargs) -> int: ...
    def delete(self) -> tuple[int, dict]: ...
    def bulk_create(self, objs, **opts): ...
    def bulk_update(self, objs, fields, **opts): ...
    def create(self, **kwargs): ...
```

The three actor verbs are sugar over the same primitive:

| Call | What it does | When to reach for it |
|---|---|---|
| `with_actor(actor)` | Resolves `actor` to a `SubjectRef` and pins it on the queryset clone. | The default. Works for any subject type. |
| `as_user(user)` | Equivalent to `with_actor(to_subject_ref(user))` for a Django `User`. | The HTTP request path: `Post.objects.as_user(request.user)`. |
| `as_agent(agent, on_behalf_of=u)` | Equivalent to `with_actor(grant_subject_ref(agent, u))` — constructs the conventional `agents/grant:<id>#valid` subject. The application must declare `valid` as a relation and provide its relationships. | Applications whose delegation schema uses this subject convention. |
| `with_action(action)` | Pins the permission used for read-side queryset scoping instead of `read` / `Meta.rebac_default_action`. | Alternate read views such as `credential_lookup`, `list_admin`, or capability-specific resolver scopes. |
| `on_field_deny(mode)` | Pins the field-read deny mode for `read__<field>` gates instead of the global setting. | Projection-sensitive paths that want `"omit"` while the global default stays `"allow"` or `"redact"`. |
| `for_write()` | `on_field_deny("allow")` named for intent: keeps actor row scope but turns off `read__<field>` redaction so a load-then-mutate target carries every column. | Resolving an update/delete target while `REBAC_FIELD_READ_MODE` is `"redact"`/`"omit"`, where a redacted column must not be hidden from the row being written. |
| `rebac_select_related(*fields)` | Applies Django `select_related` and batch-checks selected REBAC-bound related rows before serialization. | To-one relation optimization when an unreadable related object should fail the field/query instead of leaking. |
| `rebac_prefetch_related(*lookups)` | Applies Django `prefetch_related`, rewriting bare protected lookups to `Prefetch(queryset=Related.objects.with_actor(actor))`. | Reverse, M2M, and to-many loading where protected children should be scoped rather than loaded via `_base_manager`. |

`as_agent(agent)` without `on_behalf_of` uses `to_subject_ref(agent)` unchanged. Its resource type and grants come from the consumer's identity and schema declarations. Passing `on_behalf_of=user` constructs a deterministic grant subject; it does not create a grant, intersect permissions or impersonate the user. Applications that use a different grant identity pass their own subject through `with_actor`.
The constructed grant ID is `v2_` followed by unpadded base64url of SHA-256
over four UTF-8 components in order: principal type, principal ID, agent type,
agent ID. Each component is framed by its unsigned 4-byte big-endian byte
length. The result is 46 ASCII characters, within the 64-character storage
limit and SpiceDB's object-ID character set. Subject relations (including
`#valid`) are carried separately on `SubjectRef` and are not hashed. Existing
grant IDs must be recreated along with their relationship tuples before using
the new helper.

`rebac_select_related()` preserves Django's to-one join optimization, then
checks every selected REBAC-bound related object in batches. If the actor cannot
read one of those joined rows, the queryset raises `PermissionDenied` before the
object can serialize. Related-field projections such as
`.values("folder__name")` fail closed because they bypass model-instance
materialisation and cannot carry the related-object guard. `rebac_prefetch_related()`
is the to-many counterpart: it keeps Django prefetching, but protected bare
lookups are rewritten to actor-scoped `Prefetch` querysets using the related
model's default manager, never `_base_manager`.
Every requested path is retained after its protected prefixes, including
unprotected terminal relations and their explicit `Prefetch` queryset/`to_attr`.
Loading an unprotected tail does not bypass any protected intermediate relation.
Pickling a queryset strips its pinned actor and sudo reason, as instance
pickling does, without evaluating the queryset. It also clears cached results,
field-visibility state and prefetch completion so restored results are checked
again. The receiving process must establish its own trusted actor or bypass
context before materializing or mutating it.

### `with_actor` vs `sudo` — distinct verbs

[Borrowed from Odoo's `with_user` / `sudo` distinction. Adapted with mandatory `reason` and a generic actor type.]

- `with_actor(actor)` — re-evaluate all checks **as** `actor`. The originating actor (`current_actor()`) is unchanged — `with_actor()` does NOT mutate the ContextVar; the new scope lives on the queryset clone. Audit events record both the originating actor and the queryset's pinned actor. Mirrors Odoo's `with_user(u)`, generalised to any subject type.
- `sudo(reason=...)` — request-path bypass of all REBAC checks. `current_actor()` still returns the originating subject; only `is_sudo()` flips. Mirrors Odoo's `env.su` / `env.user` independence. Mandatory `reason`. Every public bypass is audited at first use: block at entry, queryset at evaluation or write, instance at check or write. Rows follow the enclosing transaction's commit or rollback. **Gated by `REBAC_ALLOW_SUDO`** — strict tenants disable it.
- `instance.unsudo()` — clears only a per-instance sudo pin and returns the instance. It does not bind an actor; use `with_actor(actor)` when the intent is to leave sudo under a concrete subject.
- `system_context(reason=...)` — same bypass semantics as `sudo()` (same block-scoped audit kind, same reason requirement, same non-propagation through traversal), but intended for framework-owned jobs running outside a request: migrations, fixture seeders, asset loaders, scheduled maintenance. **Not gated by `REBAC_ALLOW_SUDO`** — a tenant who has disabled request-path sudo still needs to run migrations. Choose `sudo()` for request-path elevation (admin views, override layer); choose `system_context()` for framework jobs.

What `sudo()` does NOT bypass:
- App-layer `clean()` validators.
- Application code's explicit `if user.is_staff:` checks.
- Signals attached to `pre_save` / `post_save` that aren't part of the REBAC pipeline.
- `@require_permission` decorators that resolve their own actor.

`@require_permission(..., actor_arg="actor", resource_arg="resource")` binds
those names through the decorated callable's native signature, so positional,
keyword, instance-method, class-method, static-method, and defaulted arguments
have the same meaning. A configured explicit actor is always checked and takes
precedence over ambient sudo; an explicit or defaulted `None` actor fails closed
instead of falling back to ambient scope. Ambient sudo bypass applies only when
the decorator has no `actor_arg`. Named actor and resource arguments must be
declared parameters of the callable.

**Sudo does NOT propagate through relationship traversal.** This is the single largest deliberate divergence from Odoo's `env.su` semantics. In Odoo, `record.sudo().lines.user_id` reads BOTH `lines` AND `user_id` in sudo because the `env` propagates. We don't do that — see [§ Lessons from Odoo 19 — footguns we avoid](#lessons-from-odoo-19--footguns-we-avoid).

### Three actor-resolution paths

The queryset and instance APIs expose the same observer:
`effective_actor(strict=False) -> (actor, is_unscoped)`. The second tuple
member means "this resolves to unscoped/bypass evaluation", not specifically
"sudo"; it is true for explicit sudo, ambient sudo when no actor is pinned, and
non-strict no-actor fallthrough. In default observer mode a strict no-actor
state returns `(None, False)` rather than raising; materialisation and write
gates call the strict path and raise `MissingActorError`.

Resolution order:

1. **Per-queryset or per-instance sudo**, set via `.sudo(reason=...)`. Bypasses scoping; logs a structured audit event.
2. **Per-queryset or per-instance actor**, set via `.with_actor(actor)` / `.as_user(user)` / `.as_agent(agent, on_behalf_of=u)`. Stored on the queryset or instance — not on a ContextVar — so it survives chaining and DOESN'T leak across queryset boundaries.
3. **Ambient sudo**, set by `with sudo(...)` / `with system_context(...)`, only when no explicit actor is pinned.
4. **Implicit from `current_actor()`**, the contextvar populated by middleware (see [§ Middleware](#middleware)) or explicit task actor scopes.
5. **Falls through to** `REBAC_STRICT_MODE` handling: `True` → raise for gates/materialisation; `False` → full visibility.

A pinned actor (path 2) **always wins** over ambient state (paths 3-4) — there is no path by which ambient sudo or the ambient actor ContextVar can override an explicit `.with_actor(...)`. This is the inverse of Odoo's `allowed_company_ids` ambient-scope precedence; we want the explicit local scope to be the authoritative one. If code truly wants bypass inside an elevated block, it must call `.sudo(reason=...)` on that queryset or instance.

**Critical: scope sticks across writes.** A queryset created with `with_actor(actor)` produces instances tagged with that actor; `instance.save()` re-checks against the same actor, regardless of what `current_actor()` says now. This is the Odoo `with_user(...)` invariant translated into Django — the actor follows the recordset.
Only a REBAC queryset or `RebacMixin` instance can carry that pin; an unrelated
tracked model's callable attribute named `actor` does not become an actor scope.

### CRUD enforcement matrix

| Operation | Permission checked | Where |
|---|---|---|
| `Model.objects.all()` / `.filter(...)` / `.get()` / `.count()` / `.exists()` | `read` (or `Meta.rebac_default_action`; override per chain with `.with_action(action)`) | The queryset injects the backend's lazy permission predicate (the compiled scope for `LocalBackend`), falling back to `resource_id__in=<accessible(actor, action, type)>` for backends without one. |
| `Model.objects.create(**fields)` | `create` on the proposed row's forward relations | Constructs the instance and delegates to `insert()`. |
| `Model.objects.insert(obj)` | `create` on the proposed row's forward relations | Pins queryset scope on the prepared instance; the `save_base` `check_new` gate authorizes the insert. |
| `Model.objects.bulk_create(rows)` | `create` on each proposed row's forward relations | Every candidate is preflighted before Django issues insert SQL. |
| `instance.save()` (loaded/non-adding instance) | `write` on the row | `RebacMixin.save_base`, before consumer `pre_save`. |
| `instance.save()` (new instance) | `create` on the proposed row's forward relations | `save_base` projects the constructed candidate into `check_new`. |
| `instance.delete()` | `delete` on the row | `RebacMixin.delete`; explicit-sender `pre_delete` checks collector children. |
| `Model.objects.update(**kwargs)` | `write` on each affected row and every resource row whose backed FK edge changes | Manager intersects the queryset PK set with the actor's `write` scope; raises if any in-scope row is excluded. A backed FK update checks old and proposed source rows before SQL; expressions whose proposed FK cannot be resolved are refused. When the update touches a column the policy reads, the statement's primary keys are selected, those rows are gated, and exactly those rows are written by key (see [Writes](#writes)). |
| `Model.objects.delete()` | `delete` on each row | Same pattern. |

For an automatically created M2M through table, a `RebacMixin` owner needs
`write` on its row even if the table backs no relation. When the table is
watched, `add` / `remove` / `set` / `clear` also check every
resource type that owns an affected backed edge, including reverse-manager and
symmetrical self-M2M calls. The related manager preflights the changed pairs once
outside Django's own atomic block so denial audit survives that rollback.
Its `set()` wrapper accepts Django's positional and `objs=` keyword forms.
The `m2m_changed` receiver gates related-manager writes the wrapper has not
already gated, and queryset writes through
the auto-created through model's `objects` manager (`bulk_create`, `update`,
queryset `delete`) are gated; the related-manager wrapper exempts
only the exact pairs it already gated, as inserts or deletes, so rows a
consumer `m2m_changed` handler writes during the call are gated on their own.
Instance-level through writes are not gated (proposal
0011); writes through the through model's `_base_manager`, and a
related-manager call from a multi-table child of the declaring model, are
not gated (proposal 0013). Through gates use the changed FK pairs and
their source rows rather than the whole through table.
The carrying actor wins over ambient context; instance sudo does not propagate
to the related manager. Without an actor, strict mode raises `MissingActorError`
only if an affected resource type declares `write`. An unwatched through table
without a REBAC owner needs no backed-edge gate. Signal-free `bulk_create`, `update`,
`bulk_update`, and tracked deletes apply the same backed-column gate.
`bulk_update`'s per-row literal `Case`/`When` values are resolved and checked;
arbitrary ORM expressions for a proposed watched value are refused unless
explicitly bypassed.
Tracked-model `bulk_create(update_conflicts=True)` refuses updates to watched
backing columns because the insert candidate cannot identify the old edge of a conflicting
row; checked saves or explicit sudo are required.
`RebacMixin._base_manager` remains an unscoped infrastructure write path.
A reverse FK `add(bulk=True)` passes a related model value
through that manager and is gated on the declaring resource before SQL, under
the ambient actor: the actor pinned on the related manager's instance, the
moved row's own `write`, and a block `sudo` over a pinned actor are not yet
honoured there (proposal 0011).

A `bulk_update` of a watched column is gated per statement. The owner selects
the primary keys of the rows a statement writes, before its SQL, writes
exactly those rows, and resolves each row's value from the `Case` in Python: every arm
must be `When(pk=<literal>, then=Value(...))` with no default, the shape
Django emits, so an arm with another condition, an expression result or a
default is refused. An authorized update of more rows than `batch_size` is
therefore not refused for rows written by an earlier batch. The resolution is
an emulation of SQL and can be fooled: an arm whose condition is a negated
`Q`, an `F("pk")`, or a pk spelled as a string or `Value` passes the shape
check while SQL evaluates it first. Those cases are pinned as expected
failures for proposal 0013, which gates from the statement's own `Query`;
until then, `update()` with a hand-built `Case` on a watched column is an
unsupported write on a model whose declaring type grants `write`.

Write-expression read gates inspect the unresolved expression tree. A `Q`
inside a `When`, and an `F()` over a destination annotation or alias, are not
traversed, so literal SQL placed there reaches the write; also pinned for
proposal 0013.

`QuerySet.explain()` applies the same actor scope as the query it describes.
`RebacManager.raw()` and `RebacQuerySet.raw()` cannot attach a REBAC scope to arbitrary SQL and therefore
requires ambient sudo or `system_context`; it raises in ordinary actor scope
and raises `MissingActorError` without an actor in strict mode. Explicit queryset
sudo on `raw()` emits one bypass audit row; an ambient sudo block emits its row
at entry. A `scoped()` queryset made in that block emits its own audit row if
its bypass is first used after the block exits.

**Failure mode for writes:** *all-or-nothing*. Any denied row in a bulk write raises and rolls back. **Failure mode for reads:** denied rows are absent from the queryset; no raise. List endpoints return `[]` rather than 403 when the user has no rows.
Write expressions, including `F()` on an instance or in `update()` and
`bulk_update()`, may read columns before writing their result. If a source
column has a `read__<field>` gate under the active field-read mode, every
affected row must grant that read; otherwise the write is denied. Bulk denial
messages report the action without identifying or counting rows outside the
actor's read scope. `OuterRef` columns inside subqueries are reads of the
destination row and pass through the same field gate, even when the inner
query's model has no REBAC field gates. `RawSQL` and nested opaque SQL expressions
are refused for writes on models with gated fields because their source columns
cannot be enumerated. A concrete parent column of a multi-table-inheritance
resource is checked against that child's field-read gate on both queryset and
instance writes.

Actor-scoped `bulk_create(update_conflicts=True)` raises `PermissionDenied`
before writing: a proposed-row create check cannot authorize updates to existing
rows or their protected fields. Use checked instance saves for those updates,
or explicitly bypass with `.sudo(reason=...)` for a trusted bulk import.

### Field-level read gates (`read__<field>`)

Permissions named `read__<field>` are enforced after a queryset has been
row-scoped and materialised. The schema syntax is unchanged: `read__title`,
`read__salary`, and `write__title` are ordinary permission names. The shared
schema accessor `field_gated_actions(definition, verb)` discovers both read and
write field gates so the two paths cannot drift.

Field enforcement is opt-in through `REBAC_FIELD_READ_MODE` or
`.on_field_deny(mode)`. Modes are:

| Mode | Behavior |
|---|---|
| `"allow"` | Default. Do not enforce `read__<field>` gates. |
| `"redact"` | Set denied fields to `None` and record `_rebac_redacted_fields`. |
| `"omit"` | Same redaction computation, plus `_rebac_omitted_fields` so serializers can drop the key. |
| `"raise"` | Accepted for forward compatibility, but currently degrades to `"redact"` and emits `rebac.W008`; descriptor-level raising stays with the 1.x `Meta.protected_fields` roadmap item. |

With field redaction enabled, annotations, aliases, and aggregates that read a
protected column raise `PermissionDenied`: scalar SQL results cannot carry
instance-level redaction. The same guard rejects projections of protected
`rebac_select_related()` paths, even through aliases or an explicitly sudoed
root. `.for_write()` retains its explicit bypass of root field redaction.
A `values()` / `values_list()` projection is refused for the protected
fields it names; one that names only computed values names none. A set
combination (`union()`, `intersection()`, `difference()`) returns the columns
of each operand, so every operand is read, nested combinations included.
Selected SQL that was written by hand (`extra(select=...)`, `RawSQL`, a
function with a caller-supplied template) can read any column, so it counts
as reading every protected field of the model, for model instances as for
projections.
The guard attributes a column to a queryset's projection only when it is read
from that queryset's own row: directly, or through `OuterRef` from a nested
query. A column a nested query reads from its own tables belongs to that
query's scope decision, not the outer one: a bypass (`sudo` /
`system_context`) subquery may read it, and an actor-scoped subquery answers
for its own projection when it is resolved, so
`Subquery(Model.objects.with_actor(a).values("gated"))` still raises while an
`Exists` over a bypass queryset that filters on a gated column hands the outer
row only its boolean.
The `NOT EXISTS` Django builds for an `exclude()` across a to-many relation
(`Query.split_exclude`) is not an embedded queryset: it is a correlated piece
of the outer statement, like the join a `filter()` across the same relation
adds, and it is not scoped on its own. A queryset the caller passes inside
that exclude (`rel__in=Model.objects...`) keeps its own scope.
These guards inspect Django column expressions; raw SQL and arbitrary custom
SQL expressions remain outside that inspection boundary.

The engine computes visibility per row, not with a blanket `.defer()`. For each
declared `read__<field>`, it asks the backend for
`accessible(subject, action="read__<field>", resource_type=...)` through the
ambient `PermissionEvaluator` when one is open. A row whose resource id is not
in that set has the field redacted. This preserves cases where Alice may read
`title` on her own row but not on Bob's row in the same queryset.

Projection querysets that would return gated fields directly, such as
`.values("salary")`, `.values_list("salary", flat=True)`, or bare `.values()`,
fail closed under `"redact"` / `"omit"` / `"raise"`. The safe options are to
materialise model instances and let the field-visibility pass run, or project
only fields that have no declared `read__<field>` gate.

Single-instance callers can use `instance.with_actor(actor).denied_read_fields()`
for a pure decision or `instance.redacted(mode="redact")` for an eager in-place
projection. The instance path goes through `check_access("read__<field>")`, so
caveat context can be supplied. Bulk materialisation has no per-row caveat
context and therefore treats `CONDITIONAL_PERMISSION` as denied by default;
`REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL = False` flips conditional fields
to visible.

Redacted fields are fail-closed on writes. If a caller explicitly saves a
redacted field via `save(update_fields=[...])`, the save_base owner raises
`PermissionDenied`. A full `save()` on a redacted instance rewrites
`update_fields` to exclude redacted fields, preventing a display-time `None`
from overwriting the stored value.
Write expressions may use a gated source column only after its `read__<field>`
check succeeds. A subquery whose base model or a joined model declares gated
fields is refused in both instance saves and bulk updates: SQL can read rows
and aliases beyond the destination row, so a destination-row check cannot
establish source access.
`refresh_from_db()` preserves fields already redacted on that instance, even
when the refresh explicitly names one of them; refresh never reveals a hidden
database value through Django's unscoped base manager.

When the goal is to load a row *in order to mutate it* (resolve an
update/delete target, then write a different column), redaction would hide
columns the writer legitimately needs. `.for_write()` is the named shorthand
for `.on_field_deny("allow")` that turns field-read redaction off for that
queryset while leaving actor row scope intact — the row must still be one the
actor may access, but every column is materialised. It does not grant any
write permission; save/delete owner checks and collector child gates still run.

---

## Middleware

```python
class ActorMiddleware:
    """
    Reads request.user (and optionally an X-Actor-Subject header) and
    populates a ContextVar consulted by RebacManager when no explicit
    .with_actor()/.sudo() is set on the queryset.
    """
```

Add to `MIDDLEWARE` after the middleware named by
`REBAC_AUTHENTICATION_MIDDLEWARE`:

```python
MIDDLEWARE = [
    # ...
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "rebac.middleware.ActorMiddleware",
    # ...
]
```

The contextvar is exposed as `current_actor()` — works in async views, sync views, ASGI consumers, and DRF viewsets identically.

The superuser request bypass applies only when the configured actor resolver
returns that active superuser's own subject. A resolver returning an agent,
grant, API key, or no actor keeps normal permission scoping even when
`request.user` is a superuser.

## Per-request evaluator + Zookie freshness

`ActorMiddleware` brackets each request with TWO additional scopes alongside
the actor ContextVar: an evaluator scope (per-request permission check cache)
and a Zookie scope (write-then-read freshness propagation).

### `PermissionEvaluator` — per-request check cache

```python
from rebac import current_evaluator, evaluator_scope, PermissionEvaluator

with evaluator_scope() as evaluator:
    # First call hits the backend; subsequent calls with the same
    # (subject, action, resource, context) tuple come from the LRU cache.
    evaluator.check(backend(), subject=u, action="read", resource=ObjectRef("blog/post", "1"))
    evaluator.check(backend(), subject=u, action="read", resource=ObjectRef("blog/post", "1"))
    # → 1 backend call total

    # `evaluator.accessible(...)` caches list-of-ids the same way.
```

Bounded by `REBAC_EVALUATOR_CACHE_SIZE` (default `10_000`) using `OrderedDict`
Bounded cache eviction across check and accessible caches. Conditional results
(`CONDITIONAL_PERMISSION(missing=[...])`) are NOT cached — the missing caveat
params are part of the answer and the next call may supply them. Per-call
explicit `consistency` / `at_zookie` also bypass the cache.

Cache keys include backend instance identity, including reentrant checks through
different backends. Context keys preserve scalar types (`True`, `1`, and `1.0` differ).
Complex context values bypass caching, including nested dictionaries and lists.

LocalBackend retains the 0.18.2 decision-cache rules. It declines caching inside
a transaction, when the schema declares expiring relationships, or for a
resource type in the conservative field/attribute backing reachability closure
returned by `live_backed_resource_types`. Unrelated types remain cacheable.
Override deadlines refresh the composed schema and its decision generation.
Eligible cache hits cost zero queries, including no revision SELECT per lookup.
Backend relationship writes and the `post_delete` cascade
(`mark_relationships_changed()`) invalidate decision generations across local
backend instances in this process, including evaluators suspended by a nested
scope.

The old `accessible_cached`, `enable_accessible_cache`, and
`disable_accessible_cache` helpers were removed in 0.5. Use
`current_evaluator()` / `evaluator_scope()` directly.

### Zookie freshness — closes the write-then-read window

Every backend write returns a `Zookie`; `write_relationships` /
`delete_relationships` record it in the ambient `_current_zookie` ContextVar
automatically. The default consistency for subsequent reads in the same
scope auto-upgrades to `Consistency.AT_LEAST_AS_FRESH(zookie)`.

```python
from rebac import write_relationships, current_zookie, zookie_scope

with zookie_scope():
    write_relationships([...])     # → records Zookie
    # Subsequent LocalBackend reads in scope see post-write state:
    # LocalBackend reads the current database state, including writes
    # newer than the token. The token is a freshness floor, not a cutoff.
    accessible(subject=u, action="read", resource_type="blog/post")
```

LocalBackend's write witness is the existing `Relationship.written_at_xid`
column; `Zookie.token = str(<xid>)`. Reads use the current state visible through
the application's Django database connection and transaction isolation. A token
never filters out newer relationships: doing so could hide a newly added deny
edge. LocalBackend has no historical relationship versions and rejects
`Consistency.AT_EXACT_SNAPSHOT`; applications requiring historical snapshots
need a backend that implements them. Database routing and transaction isolation
must make the required writes visible on the reading connection.
The returned token is at least the highest xid allocated by any tuple write
nested in the call. A read in the same transaction sees a nested tuple write
as soon as it returns, because the statement reads the tuple table itself.
An ambient LocalBackend token only advances; a later outer
helper cannot replace a nested helper's higher token with a lower one.
Backends validate `Zookie.backend` matches their
own `kind` and raise on mismatch — a SpiceDB token handed to LocalBackend
would be interpreted as a numeric xid with garbage semantics.

**Cross-request transport** for SPA / JWT consumers is opt-in via
`REBAC_ZOOKIE_TRANSPORT`:

| Value | Behavior | Use when |
|---|---|---|
| `"none"` (default) | Single-request scope only. | Server-side rendering, internal RPC. |
| `"header"` | Request reads `X-Rebac-Zookie`; response writes it back. | SPA / mobile / JWT clients — both sides stateless. |
| `"session"` | Persists into `request.session[_rebac_zookie]`. | Server-rendered sessions where `django.contrib.sessions` is already in play. System check `rebac.W006` fires if contrib.sessions is missing. |

### GraphQL + WebSocket adapter (`rebac.graphql.strawberry`)

Behind the `[strawberry]` extra: `pip install django-zed-rebac[strawberry]`.

```python
import strawberry
from rebac.graphql.strawberry import RebacExtension, RebacChannelsConsumerMixin

schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    subscription=Subscription,
    extensions=[RebacExtension],
)
```

`RebacExtension` opens evaluator + Zookie scopes per GraphQL **operation**.
Strawberry keeps its operation hook open throughout a subscription, so the
extension clears the shared evaluator and resets the ambient Zookie in its
per-result `get_results` hook. Permission decisions are retained only within
one emission; the next tick rechecks the backend after a revocation.

For HTTP GraphQL, the operation inherits the middleware's incoming Zookie and
propagates a resolver's recorded write token back to the enclosing scope. The
middleware can then send that token through the configured response header
or session transport.

The extension also mirrors `current_evaluator()` and `current_zookie()`
onto `info.context.rebac_evaluator` / `.rebac_zookie` for resolvers that
prefer explicit DI over the ambient ContextVar. Mirror is best-effort —
read-only context types silently skip without crashing.

For WS subscriptions, compose `RebacChannelsConsumerMixin` with an async
consumer base such as Strawberry's `GraphQLWSConsumer`. Synchronous Channels
consumers are unsupported because they do not await the mixin's connection hooks:

```python
from strawberry.channels import GraphQLWSConsumer
from rebac.graphql.strawberry import RebacChannelsConsumerMixin

class GraphQLConsumer(RebacChannelsConsumerMixin, GraphQLWSConsumer):
    pass
```

Subscription invariants:
- **Actor**: connection-scoped (resolved at handshake from `scope["user"]`).
- **Evaluator**: per-emission. Revoked grants take effect at next tick.
- **Zookie**: per-emission. Naturally aligns with the write-driven nature
  of subscriptions — the change that initiated the emission carries its
  Zookie within the emission's scope.

For non-request contexts (Celery, cron, management commands), use `with sudo(reason=...)` or set the actor explicitly via `.with_actor(actor)` / `.as_user(user)` / `.as_agent(agent, on_behalf_of=user)`.

### Strawberry-Django optimizer (`rebac.graphql.strawberry_django`)

Behind the `[strawberry-django]` extra:
`pip install django-zed-rebac[strawberry-django]`.

```python
import strawberry
from rebac.graphql.strawberry import RebacExtension
from rebac.graphql.strawberry_django import RebacDjangoOptimizerExtension

schema = strawberry.Schema(
    query=Query,
    extensions=[RebacExtension, RebacDjangoOptimizerExtension],
)
```

`RebacDjangoOptimizerExtension` targets `strawberry-graphql-django`'s
`DjangoOptimizerExtension` surface while preserving REBAC invariants:

- Root querysets inherit `current_actor()` when a resolver did not already call
  `.with_actor(...)`, `.as_user(...)`, `.as_agent(...)`, or `.sudo(...)`.
- To-one relation paths keep `select_related`; selected REBAC-bound related rows
  are batch-checked before serialization. A denied joined row raises
  `PermissionDenied`.
- To-many / reverse relation paths use actor-scoped `Prefetch` querysets for
  protected targets, using `_default_manager` and never `_base_manager`.
- Optimized `.only(...)` selections keep each REBAC-bound model's configured
  `Meta.rebac_id_attr`, so row and field gates can resolve resource ids without
  lazy-loading them later.

Outside Strawberry-Django, use `rebac_select_related()` and
`rebac_prefetch_related()` directly on `RebacQuerySet`.

---

## Surface integrations

### DRF

```python
class PostViewSet(viewsets.ModelViewSet):
    queryset           = Post.objects.all()
    serializer_class   = PostSerializer
    permission_classes = [RebacPermission]
    filter_backends    = [RebacFilterBackend]
```

Default action map: `list/retrieve/metadata→read`, `create→create`,
`update/partial_update→write`, `destroy→delete`. A view's `rebac_action_map`
extends or overrides the permission class's map. Custom viewset actions must
be mapped explicitly; an unmapped action is denied. For API views without a
viewset action, HTTP methods map as `GET/HEAD/OPTIONS→read`, `POST→create`,
`PUT/PATCH→write`, `DELETE→delete`. Unknown methods are denied unless the
action map explicitly maps the lowercase method name.

Read admission requires a resolved actor, but does not require any accessible
row at the model level. `RebacFilterBackend` applies row scoping and
`has_object_permission` gates a concrete detail object: a list with no
accessible rows returns `[]`, as required by the CRUD enforcement matrix.
An object declaring a REBAC resource type through model metadata,
`@rebac_resource`, or `RebacObjectMeta` is denied if its concrete identity
cannot be resolved (including missing, empty, unsaved, and redacted IDs). An object
without a REBAC resource type remains outside this permission class's object
gate and is allowed after actor and action admission.

drf-spectacular OpenAPI emission: optional `rebac.drf.spectacular` integration adds a security requirement to operations that include `RebacPermission`. Activated automatically if `drf_spectacular` is installed.

### Celery

Automatic Celery propagation is planned; `rebac.celery` and its signal handlers
are not shipped. Pass the authenticated actor reference through your trusted
task producer and restore it explicitly in the worker:

```python
from rebac import SubjectRef, actor_context

@shared_task
def email_user_their_drafts(actor_subject: str):
    with actor_context(SubjectRef.parse(actor_subject)):
        drafts = Post.objects.filter(status="draft")
        send_email(drafts)
```

This explicit scope works in both worker and eager execution. Unscoped worker
queries raise `MissingActorError` under the default strict mode.

### MCP

```python
from mcp.server.fastmcp import Context, CurrentContext
from rebac.mcp import rebac_mcp_tool

@mcp.tool
@rebac_mcp_tool(resource_type="blog/post", action="write", id_arg="post_id")
async def edit_post(post_id: str, body: str, ctx: Context = CurrentContext()) -> dict:
    ...
```

`rebac_mcp_tool` resolves the actor through the `REBAC_MCP_ACTOR_RESOLVER`
callable first — by default
`ctx.request_context.meta["actor_subject"]`, a canonical `SubjectRef` string —
then falls back to ambient `current_actor()` only when no explicit identity
field is present. An invalid explicit identity denies even if the ambient actor
has permission. The decorator checks the permission, then runs the body inside
`actor_context`. Streaming tools scope each iterator advancement and cleanup,
restoring the consumer's actor before yielding each chunk. No
actor resolved → `PermissionDenied` (fail closed). `action="create"` routes
through `check_new` for not-yet-persisted resources. The MCP server remains
responsible for minting and validating identity — the decorator only resolves
and authorises an actor an upstream boundary already established. The default
metadata field must be set by trusted server authentication code: never trust
client-provided `_meta.actor_subject` unchanged. The server must overwrite it
with the verified identity, or configure a resolver that reads trusted
server-side authentication state. Implemented
per [proposal 0004](./proposals/0004-mcp-tool-integration.md).

Caller-supplied empty resource IDs and relationship target IDs are denied;
the backend's empty ID has a separate, internal model-level meaning. For a
model-backed resource, an `id_arg` must be canonical under that model's
identity codec before the permission check.

### GraphQL (graphene / strawberry)

```python
@rebac_resource(type="blog/post", id_attr="pk")
@strawberry.type
class PostType:
    @strawberry.field
    @require_permission("read")
    def body(self, info) -> str: ...
```

### Plain Python entities

```python
@rebac_resource(type="storage/s3_prefix", id_attr="prefix")
class S3Prefix:
    def __init__(self, prefix: str):
        self.prefix = prefix

# Manual check elsewhere:
from rebac import backend, ObjectRef
backend().check_access(
    subject = to_subject_ref(user),
    action  = "read",
    resource = ObjectRef("storage/s3_prefix", prefix),
)
```

The `@rebac_resource` decorator registers the type with the schema validator (the build emits `definition storage/s3_prefix {}` so other models can declare relationships pointing at it).

---

## Granular reusable parts

`django-zed-rebac` is built on small, composable primitives. Projects that need a piece — but not the whole `RebacMixin` — can wire them directly:

| Part | What you import | When to use |
|---|---|---|
| `Backend` ABC + `LocalBackend` | `from rebac import LocalBackend, ObjectRef, SubjectRef` | You want REBAC checks in code without touching ORM. |
| `RebacManager` standalone | `Model.objects = RebacManager.from_queryset(RebacQuerySet)()` | Drop scoping into a model without the metaclass. |
| `check_access(subject, action, resource)` | `from rebac import backend; backend().check_access(...)` | Imperative checks anywhere. |
| `@require_permission` decorator | `from rebac import require_permission` | Gate methods on plain Python classes (not just models). |
| `current_actor()` ContextVar | `from rebac import current_actor` | Read the active actor inside any code path. |
| `with actor_context(actor):` | `from rebac import actor_context` | Block-scoped actor for non-queryset code (manual `check_access` calls inside the block). |
| `with sudo(reason=...)` | `from rebac import sudo` | Block-scoped bypass for request-path elevation; logged. Gated by `REBAC_ALLOW_SUDO`. |
| `with system_context(reason=...)` | `from rebac import system_context` | Block-scoped bypass for framework-owned jobs (migrations, fixture seeders, cron, asset loaders). Logged. **Not** gated by `REBAC_ALLOW_SUDO` — strict tenants that have turned `sudo()` off still need this path. |
| `parse_zed(text)` | `from rebac.schema import parse_zed` | Tooling: round-trip `permissions.zed` to AST. |
| `to_subject_ref(actor)` | `from rebac import to_subject_ref` | Convert a registered actor to its canonical subject reference. |

---

## Management commands

Single namespace `rebac` with subcommands. Destructive overwrite is explicit.

```bash
python manage.py rebac sync                       # idempotent; respects no_update
python manage.py rebac sync --check               # CI gate; no writes; non-zero on drift
python manage.py rebac sync --force-overwrite     # destructive; bypasses no_update
                                                       # requires --yes for non-interactive
python manage.py rebac sync --force-overwrite --package=blog

python manage.py rebac check                      # doctor: validate without writes
python manage.py rebac build-zed                  # emit effective.zed for SpiceDB
python manage.py rebac explain blog/post.read     # print compiled expression
python manage.py rebac grant storage/role:viewer auth/user:42  # idempotent member grant; --caveat NAME --caveat-context JSON
python manage.py rebac revoke storage/role:viewer auth/user:42 # print deleted count; --caveat NAME; --strict fails on 0
python manage.py rebac relationships --resource storage/role:viewer # tuple listing; --subject TYPE:ID[#rel] --relation NAME --limit N
```

`rebac explain` renders the effective permission expression after active
`SchemaOverride` rows have been composed with the baseline.

No command builds, rebuilds or verifies anything derived: `sync` writes the
stored schema rows and publishes a policy revision, and permissions are
evaluated from the application's rows when they are read. Raw SQL, data
migrations over historical models, `loaddata` and bulk queryset writes need
no command afterwards.

`grant` and `revoke` route `<namespace>/role` containers through `rebac.roles`
and other containers through `rebac.memberships`. Both keep the helpers' ambient
actor, audit and Zookie behavior. `--caveat-context` requires a non-empty
`--caveat` name. Listing reads the active relationship model,
ordered by the canonical tuple key; supplied filters are exact (including an
empty subject relation), omitted filters match all rows. An omitted limit lists
all rows; zero lists none and negative limits are errors. Reference parse errors
fail with a command error. Grant/revoke reject types absent from the loaded schema;
listing accepts them so orphaned tuples remain inspectable after schema changes.

The `write-schema`, `gc-expired`, `retype-relationships`, `build-zed --check`,
and `sync --target` interfaces are planned and not implemented. Use
`sync --check` and compare generated build artifacts for current CI gates.

### `sync` lifecycle

Default mode. Idempotent. Respects `no_update`:

```
For each AppConfig that declares rebac_schema:
  1. Locate the .zed file.
  2. Parse and validate against the schema language.
  3. For each definition / relation / permission / caveat:
     a. Compute content_hash of the fragment.
     b. Look up PackageManagedRecord by (package, external_id).
     c. If not found → create Schema* row + PackageManagedRecord. (Fresh install.)
     d. If found and content_hash matches → no-op.
     e. If found and content_hash differs and no_update=True → skip + warn.
     f. If found and content_hash differs and no_update=False → update + bump
        last_synced_at.
  4. Detect orphans: PackageManagedRecord with no matching .zed fragment.
     Reported as warnings; deletion requires --force-overwrite.
  5. Validate cross-package references.
  6. Publish a fresh `SchemaGeneration.revision`, in the same transaction as
     the schema writes, and reset the in-process backend.
  7. Validate the composed policy (baseline and overrides) once, under the
     policy lock; a refused policy raises `SchemaError` and rolls the sync
     back.
```

The whole sync is one policy write: it locks the `SchemaGeneration` row
before it reads or writes a schema row and holds the lock until it commits.
It writes the stored schema rows and nothing else.

**There is no implicit "first install" path that bypasses `no_update`.** A clean DB has no `PackageManagedRecord` rows, so case 5c applies naturally and no overwrite is needed. This avoids Odoo's [bug #1023615](https://bugs.launchpad.net/openobject-server/+bug/1023615) class of upgrade footguns.

---

## Determinism

Build output (`effective.zed`) is **byte-identical** across runs, machines, Python versions, and Django versions. Conventions enforced by the build:

1. **Definition order**: alphabetical by name. NOT insertion order (depends on app loading sequence, which varies).
2. **Relations / permissions / caveats within a definition**: alphabetical by name.
3. **No timestamps in generated files.** A content hash is computed from the *sorted, canonical inputs* and emitted as a comment header.
4. **All set / dict iteration: `sorted(...)`.** Filesystem walks: `sorted(os.listdir(...))`.
5. **Generator-version stamp**: `// Generated by django-zed-rebac 1.0.0` — pinned to the installed version.
6. **Feature directives**: emit `use typechecking` exactly once and emit
   `use expiration` when any source uses the directive or declares a relation
   with expiration.

CI determinism test: run `rebac build-zed` twice in a tmpdir, byte-diff. Failure on any difference. Mirrors `manage.py makemigrations --check`.

---

## Migration safety

| Risk | Mitigation |
|---|---|
| Initial install creates a `Relationship` table with billions of rows expected | Indexes shipped in `0001_initial.py`. Migration is idempotent. |
| Project running `--backwards` to before `rebac` was installed | Every `RunSQL` operation has `reverse_sql`. The full schema is reversible. |
| Swappable `Relationship` model adopted post-install | The shipped relationship tables are concrete models; automatic swappable-model migration wiring is not implemented. |
| Adding `expires_at` later (back-port to existing relationships) | `expires_at` is nullable; existing rows get `NULL`. No data migration needed. |
| Multi-tenant prefix added later (`REBAC_TYPE_PREFIX`) | Automatic relationship retyping is not implemented. Plan a data migration before changing stored type identities. |
| Package upgrade silently overwrites admin schema edits | `no_update=True` on `PackageManagedRecord`. Conflict surfaced as warning + audit event. Force-overwrite is explicit. |
| Upgrade of a populated relationship table (`0008`) | `0008` adds `caveat_key` to `Relationship` and `RelationshipRegistry` with an empty default, then fills it for existing rows in primary-key order, 1,000 rows per batch, through the ORM. Its reverse drops the column. |
| Upgrade from a release that kept derived tables (`0007` → `0009`) | `0007` creates the tables `IndexTerm`, `IndexEdge`, `IndexMember`, `IndexCover`, `IndexWork` and `IndexState` and the columns `SchemaGeneration.index_revision` and `index_program`; `0009` drops them, referencing tables first, and creates the `SchemaGeneration` row with an empty revision when it is absent. Nothing reads those tables, so no data is carried over and no command runs after `migrate`. A fresh install runs both migrations and ends with the same tables. |
| Database without a published policy | The generation row exists with an empty revision. Every permission statement is fenced out and reads are closed until `rebac sync` publishes a revision. |
| Legacy database objects on upgrade or reversal | Fresh installs create no REBAC triggers/functions. `0005` creates the revision table without seeding row 1; its `RunPython(noop, uninstall, atomic=False)` cleans up legacy objects on reversal. `0006` retains forward cleanup and schema-owner model options. |

---

## Lessons from Odoo 19 — footguns we avoid

Odoo 19's `ir.rule` / `ir.model.access` / `env.su` / `with_user` system covers most of the same surface this plugin does and has a 15-year track record of production deployments. Four specific patterns there have repeatedly caused privilege-escalation, data-leak, and migration bugs. We engineer them out by design. Each item below names the Odoo failure mode, then the explicit non-feature in `django-zed-rebac`.

### 1. Sudo does NOT propagate through relationship traversal

**Odoo behaviour:** `record.sudo().lines.user_id` reads `record`, AND its `lines`, AND each line's `user_id` in sudo, because the `env` propagates across every recordset traversal. Once a developer writes `.sudo()` anywhere, every related read in the same chain bypasses checks. This is the canonical source of "I only sudoed for one thing" privilege-escalation bugs in mature Odoo deployments.

**Our behaviour:** `instance.sudo(reason=...)` flips the bypass flag for *this instance only*. Any FK accessor, reverse-FK manager, M2M traversal, or chained queryset on it re-resolves the actor against the carrying scope (`current_actor()`, or the queryset's pinned actor) — it does NOT inherit the sudo flag. If you genuinely need related rows under sudo, call `.sudo(reason="...")` explicitly on the inner queryset; the audit log records every bypass independently.

**Why:** transitive sudo is a contagion. Cutting it at every relationship boundary forces each bypass to be greppable, auditable, and intentional. The cost is verbosity; the win is "no surprise sudo".

### 2. No implicit "owner from `create_uid`"

**Odoo behaviour:** `ir.rule` filters routinely use `('create_uid', '=', user.id)` to mean "I created it, so I can edit it." Owner identity is derived from an audit column written automatically. Consequence: ownership is non-transferable (you can't grant someone else ownership without overriding `create_uid`, which breaks audit), and ownership is non-revocable (the column is required and the row tracks who first wrote it forever).

**Our behaviour:** ownership is an explicit `Relationship` row — `<resource>#owner @ auth/user:<id>` — written by your `post_save` signal handler or application code at create time. The audit columns (`created_by`, `created_at`) on your model are independent.

**Why:** explicit ownership is transferable (delete the row, write another), revocable (delete the row), and pluralisable (multiple owners on one resource). None of these are possible when ownership is conflated with audit. An admin granting you ownership of something you didn't create is a routine operation here; in Odoo it requires hand-overriding `create_uid`, which security-conscious deployments forbid.

### 3. No magic context keys for permission scope

**Odoo behaviour:** `allowed_company_ids`, `force_company`, `bin_size`, `mail_create_nolog`, `tracking_disable`, and a long tail of others. Each is an ambient context key that some part of the rule pipeline consults. Many are undocumented; some are checked in two places that disagree on default; a few have caused multi-tenant cross-bleed bugs over the years.

**Our behaviour:** the only ambient lever is `current_actor()`, populated by `ActorMiddleware` for HTTP and explicit `actor_context()` blocks for tasks. It is read-only at the call site (mutate via `set_current_actor()` only at framework boundaries — middleware, Celery handlers). Per-queryset `.with_actor(actor)` always wins; there is no path by which an ambient context override can mutate an explicit local scope.

**Why:** "where does the scope come from?" is a question with one answer. For tenant scoping in a single-DB SaaS, use `REBAC_TYPE_PREFIX` (configuration-time, set at request entry) or model the tenant as a resource type with its own `member` relations. Don't add a magic context key.

### 4. Soft-deleted rows participate in permission checks

**Odoo behaviour:** rule evaluations toggle `active_test=False` per call because rules must consider archived rows too. The discipline is informal — every ORM caller has to remember to flip it for permission contexts and back for normal reads. Easy to miss; bugs surface as "I have `delete` on this row but the admin UI says it doesn't exist".

**Our behaviour:** archived/inactive rows are visible to permission evaluation by default. Soft-delete is orthogonal to permission scope. If you want to hide archived rows from a list endpoint, filter at the queryset level (`Post.objects.with_actor(u).filter(archived=False)`) — but permission evaluation does not exclude them: the compiler reads model rows through `_base_manager`.

**Why:** an admin with `delete` on a soft-deleted resource needs to be able to un-archive it. If the permission layer hides it from them, the admin's only recourse is to bypass the layer entirely (sudo / direct SQL) — which is exactly the failure mode we're trying to prevent. Make the policy explicit: archived ≠ inaccessible.

### Cross-reference

These four are highest-impact. The full Odoo 19 research note (with file/line citations into the upstream tree) lives at `../odoo-research/notes/01-permissions-security.md` for contributors auditing edge cases. If you're proposing a new feature that resembles `ir.rule.domain_force` (Python evaluated at runtime against ambient context), `_check_company` (cross-relation invariants enforced at write time), or a new ambient context key, read the research note first; chances are we've ruled it out by design.

---

## Testing

Three layers of tests define the project target:

1. **Unit tests** (`pytest`): pure-Python, no database. Schema parsing, expression compilation, codename mapping, build determinism.
2. **Integration tests** (`pytest-django`, `@pytest.mark.django_db`): SQLite and PostgreSQL. `RebacMixin` end-to-end, manager scoping, compiled-permission semantics, write gates, signal handlers.
3. **SpiceDB conformance tests** (`-m spicedb`, planned): generated schemas and data evaluated by a real SpiceDB and by `LocalBackend`, answers compared. See [SpiceDB conformance suite](#spicedb-conformance-suite-planned). They do not need `SpiceDBBackend`. They drive SpiceDB directly and become the cross-backend contract tests once that backend lands.

The supported matrix is declared once, in `pyproject.toml` (`requires-python`,
the Django pin) and `.github/workflows/ci.yml`; at the time of writing that is
Python 3.14 × Django 6.0, with SQLite and PostgreSQL 16 for the `local`
backend. Test databases and temporary files must be worker-local, and fixtures
must restore process-local state between tests, so any test can run on any
worker in any order. Random ordering is opt-in with
`-p randomly --randomly-seed=137` (`make test-random`).

### Test tiers

Every test belongs to exactly one of three tiers. The tiers differ in when
they run, not in how much they are trusted: a tier 3 failure is a release
blocker like any other.

| Tier | Runs | Contents | Budget |
|---|---|---|---|
| **1. Fast** — `make check` | Every change, every push and pull request. The only gate for merging and for publishing a tag. | Ruff lint and format, strict mypy, Pyright, then the SQLite suite without `slow`, in parallel with work stealing, stopping at the first failure. | 1 minute on a developer machine, 5 minutes in CI. |
| **2. PostgreSQL delta** — `make test-pg` | Every push and pull request, as a job beside tier 1. Locally when a change touches SQL generation, transactions, locking or migrations. | Only the tests marked `postgresql` or `pg_delta`, on PostgreSQL 16. | 3 minutes. |
| **3. Release** — `make test-release` | Nightly on `main` and on demand before a release. Never inside a fix loop. | `slow` on SQLite; the whole suite including `slow` on PostgreSQL; the full `reference_exhaustive` sweep; the `schema_vendors` PostgreSQL/MySQL contracts; a randomized parallel run with seed 137; the SpiceDB conformance suite once it lands. There is no scale suite at the moment. | No measured budget at the moment; independent parallel jobs in CI, where the sweep is the longest. |

Rules that keep the tiers honest:

- **Tier 1 has a per-test budget.** A test that takes more than 2 seconds on
  a developer machine is marked `slow`. A 10-second per-test timeout in tier
  1 turns a test that outgrew the budget into a failure, not a slower loop.
  The targets that select `slow`, `reference_exhaustive` or
  `schema_vendors` raise the timeout to 300 seconds.
- **A `slow` matrix leaves a representative behind.** When a parametrized
  test is heavy because of depth, corpus size or the number of combinations,
  one small parameter set stays in tier 1 and the full matrix is `slow`. Moving
  a test to `slow` never reduces its universe or weakens its assertions.
- **`postgresql` means "requires PostgreSQL"; `pg_delta` means "runs
  everywhere, and PostgreSQL may disagree".** `pg_delta` holds tests that
  branch on the vendor, at least one test for each area that emits SQL
  (recursive scopes, compiled reads, write gates, schema write owners,
  migrations), and every test that has ever failed on PostgreSQL while passing
  on SQLite. A PostgreSQL-only failure found in tier 3 adds that test to
  `pg_delta` in the same change that fixes it.
- **A vendor-specific test skips, it does not return.** A test that cannot run
  on the current vendor calls `pytest.skip`; an early `return` reports a pass
  for something that did not run.
- **Parallel scheduling is by test, not by file** (`--dist worksteal`). No
  test may rely on sharing a worker with its module.
- **There is no scale suite at the moment.** No test holds time, statement or
  query-plan budgets for large tables. A suite that does runs alone, never
  beside parallel workers, because they distort such budgets.
- **Publishing does not wait for tier 3.** A tag publishes once tier 1 is
  green on it. Tier 3 is consulted before tagging; its nightly result on
  `main` is the release evidence.

Default `pytest` deselects `slow`, `reference_exhaustive` and
`schema_vendors`. `make test` runs the tier 1 selection serially, for
debugging only.

Tiers 1 and 2 are the two parallel jobs of `.github/workflows/ci.yml`, on
every push to `main`, every version tag and every pull request:
`test (3.14, 6.0)` runs `make check` and `postgres-delta` runs `make test-pg`
against a PostgreSQL 16 service container. Publish to PyPI reads the result of
the `test (3.14, 6.0)` job, not of the whole CI run, so a tag publishes on tier
1 alone. Tier 3 is `.github/workflows/release.yml`, nightly on `main` and on
demand, with one job per part; the reference sweep is split across six jobs by
expression shape. `make test-release` runs the same parts locally in sequence,
continues past a failing part and reports each one. `make pg-up` starts a
disposable PostgreSQL 16 container for tier 2 and the PostgreSQL parts of tier
3 and prints the `REBAC_TEST_POSTGRES_URL` to export; `make pg-down` removes
it. The commands are listed in
[CONTRIBUTING.md § Commands](../CONTRIBUTING.md#commands).

### Reference and semantics suites

The coverage matrix is the completion criterion; merely having a generator
does not cover it.

The full reference sweep uses `reference_exhaustive`, and its schedulable
cases also carry `reference_shard`. `make test-reference` overrides the
marker filter and runs every reference shard, distributed by test rather
than by file. `make test-postgres` runs the whole suite, `slow` included, on
PostgreSQL. `make test-schema-vendors` opts into disposable PostgreSQL/MySQL
contracts; MySQL 8 is covered only there. All of these are tier 3. There is
no scale suite at the moment. Drivers, environment and release gates are in
[CONTRIBUTING.md](../CONTRIBUTING.md).

| Suite | Modules | Required cases and assertions |
|---|---|---|
| Pure reference gate | `tests/test_reference_model.py`, over `tests/reference_model.py` | The opt-in `reference_exhaustive` sweep exhausts every expression through four leaves over two relations, an arrow, builtins and a constant; up to three users, two nested groups including a wildcard member, two resources, three instants, and no/partial/full caveat context. Expression cases use 32 shards per shape/universe/instant/context; direct-tuple powersets use 16. The default gate retains all named counterexamples, all one/two-leaf expressions and deterministic samples of larger trees. The reference computes each actor's set membership directly; it uses no compiler code, no ORM and no walker. |
| Differential semantics | `tests/test_compile_semantics.py`, `tests/test_read_contract.py`, with `tests/reference_harness.py` and `tests/reference_oracle.py` | Compare the compiled reads with the source facts, the frozen walker and the pure reference. Recursive schemas within and past `REBAC_DEPTH_LIMIT`, and data cycles; nested and wildcard groups; intersection and subtraction; constants with concrete bans; field paths (multi-hop, reverse, M2M, MTI, filtered), dynamic and fixed attributes; expiring tuples, override deadlines and recaveats; caveats in every operator position with all three check states; missing sets equal the reference's canonical residual set and are a subset of the walker's; subject-set actors, anonymous and empty ids; both relationship storage modes. |
| Compiler | `tests/test_compile_*.py` | The dependency graph and what it refuses (`rebac.E016`); tuple-only objects under grants and exclusions; bounds under caveats and depth on both sides of `-` and `&`; a point check past the bound raises; a scope built before a policy change closes after it; a kept statement sees a caveat instance written after it was compiled; `caveat_key` on every tuple write path and in migration `0008`. |
| Enumeration and embedding | `tests/test_read_contract.py` | Scope rows equal definite per-row results; `Subquery`, `Exists`, `__in`, `Prefetch`, field gates and bulk guards remain scoped. `lookup_subjects` agrees with the reference. Public context arguments, model-level and model-less reads keep their meaning. |
| Write effects | `tests/test_write_effects.py`, `tests/test_write_propagation*.py` | After every owner and signal path the next read reports the written state: mixin and plain re-parenting, subtree deletion, watched-field update, `bulk_update` in many batches, M2M add/remove/clear/set, collector SET_NULL/CASCADE, override create/delete/deadline, schema updates, tuple write owners. Writes outside the owners are read as stored, with no command run. A rolled-back write leaves reads as they were. Writes before the first sync, and while the library's tables are not migrated, proceed, and reads stay closed. |
| Watch map and identities | `tests/test_watch.py`, `tests/test_codec.py`, `tests/test_codec_cache.py` | The watched columns of every backing kind, resolved from metadata without reading rows; canonical identity conversion for integer, text and UUID fields, with an invalid wire id matching nothing; the cached conversion guard equal to the uncached one. |
| Policy checks | `tests/test_policy_checks.py` | System checks for settings, schemas and models, and what the library refuses as a policy. |
| Transactions and concurrency | `tests/test_write_effects.py`, `tests/test_compile_read.py` | Deterministic PostgreSQL tests: concurrent revoke and link insertion, concurrent tuple writes. Outer and savepoint rollback. A policy revision that changes while a lazy scope is compiled cannot replace the policy the scope was built for. |
| Structural | `tests/test_scope_depth.py` and others | The scope statement of a recursive permission has the same size and parse depth whatever the depth of the data. Fresh SQLite/PostgreSQL migrations install no REBAC trigger or function, and end with no table that holds a row per application row. No library raw SQL, triggers or database functions exist outside migration `0005`'s legacy uninstall routine; the two documented exceptions embed SQL that Django compiled. |
| Vendors | `schema_vendors` | SQLite and PostgreSQL CI; an opt-in MySQL 8 suite must pass before release, including the schema write owners, the upgrade path and identity codec bounds for unsigned integers. |
| Security pins | `tests/test_security_*.py` | The gates' rules, unchanged. The strict expected failures for proposals 0011 and 0013 stay pinned. |

Data cycles that exceed the frozen walker's dispatch limit use the pure
reference's finite-path least fixpoint; ordinary acyclic cases must agree with
the walker.

Statement counts and compile time per scope, and statement size as a function
of `REBAC_DEPTH_LIMIT`, are results to collect, not assumed bounds.

### SpiceDB conformance suite (planned)

The differential oracle compares the compiled reads with the library's own
frozen walker and reference model, so it proves that they agree with the
library's reading of the semantics, not that this reading is SpiceDB's.
Invariant 1 makes SpiceDB the contract, so the next step tests against
SpiceDB itself. **Planned; not part of the current build.**

- **Server.** A pinned `authzed/spicedb:<exact tag>` image started with
  `serve-testing`, an in-memory server where each distinct bearer token gets an
  isolated datastore. Each test uses a fresh token, so tests never share state
  and the suite parallelises under xdist. The dispatch depth limit is started
  above the deepest generated chain. The tag is pinned in one place, read by
  both the fixture and CI.
- **Dev.** `pytest -m spicedb`. A session fixture starts the container through
  Docker with a Docker-assigned port, or uses `REBAC_TEST_SPICEDB_ENDPOINT` when
  set. Selecting the marker without Docker or an endpoint is an error, not a
  skip. The default `pytest` run deselects the marker. The `authzed` client (the
  existing `spicedb` extra) joins the `dev` extra.
- **CI.** A job starts the pinned image in a step (`docker run … serve-testing`;
  GitHub service containers cannot pass that command), exports
  `REBAC_TEST_SPICEDB_ENDPOINT`, and runs `-m spicedb` in parallel. The job gates
  merges like the SQLite suite does.
- **Translation.** The generated schema is written with
  `render_zed(include_backing=False)`, plus `use expiration` when relations
  expire. Field, attribute and constant backings are projected into ordinary
  tuples by a test-side projector that reads the same rows the compiler
  reads; it is the prototype of the roadmap projector for `SpiceDBBackend`.
  `anonymous` and `authenticated` become synthetic wildcard relations over the
  generated subject types, with one tuple per resource, which is exact for
  generated data. Expirations are generated well in the past or future so both
  sides see the same `now`.
- **Compared.** Every call uses full consistency.
  - `CheckPermission`: permissionship, and `missing_required_context` for
    conditional results, with no, partial and full caveat context.
  - `LookupResources` and `LookupSubjects`: the definite results against
    `accessible()` and `lookup_subjects()`. Conditional results must be absent
    on our side, per the unconditional-enumeration contract.
  - Shapes: recursion, nesting, `&`, `-`, wildcards, caveats and expiration,
    at depths within both the server's limit and `REBAC_DEPTH_LIMIT`.
- **Deliberate divergences** are listed here and nowhere else, and each has a
  test pinning our side:
  - the depth bound: a set is a lower bound at `REBAC_DEPTH_LIMIT`, and a
    point check that cannot be decided within it raises
    `PermissionDepthExceeded` (SpiceDB fails at its own dispatch limit);
  - data cycles: a closure over tuples, a backed path or nested sets that
    converges within the bound answers exactly, where SpiceDB fails at its
    dispatch limit; a row on a cycle of a self foreign key is too deep in an
    upper bound;
  - `rebac.E016`: `LocalBackend` refuses a recursive component in which a
    permission of the component is reached through the right-hand side of
    `-`, or in which one expression uses permissions of the component more
    than once. Intersection with, and exclusion of, something outside the
    component is accepted. Tests pin the refusal;
  - expiring schema overrides (compared as two effective schemas, before and
    after the deadline);
  - `check_new` (no SpiceDB equivalent).

  Any other difference is a `LocalBackend` bug.
- **After it lands.** The in-process oracle stops copying the walker. It
  shrinks to a small reference model written from this specification, covering
  only the deliberate divergences and fast property tests for the SQLite suite.

The package ships `py.typed` (PEP 561). `make check` runs the same formatting,
lint, type-checking, and runtime checks locally. Integration tests exercise
schema sync drift checks and deterministic builds against the test project.

---

## Versioning

`django-zed-rebac` follows SemVer while the project is below 1.0. Minor releases
may add public API and tighten alpha contracts; patch releases are reserved for
compatible fixes.

LTS support for older Django lines was dropped before 0.7.0. The package
currently targets Django 6.0.x and Python 3.14+. Package dependencies constrain
Django and its development stubs to the audited 6.0 line; support for newer
Django feature releases requires verification of the ORM hooks this package uses.

Public API (`rebac.*` direct imports + the schema language) is intended to be
stable across patch releases. `rebac._internal.*` is private.

---

## Roadmap

| Phase | Deliverable |
|---|---|
| **0.1.0 — MVP** | `LocalBackend`; schema parser + sync command; `RebacMixin` + manager + signals; `RebacPermission` + `RebacFilterBackend`; system checks; sync/check commands; first test matrix. |
| **0.2.0 — Alpha hardening** | Schema-level built-in actor grants; action-scoped read querysets; split request-path `sudo()` from framework-job `system_context()`; hot-path schema cache invalidation. |
| **0.3.0-0.9.0 — shipped alpha core** | `ActorMiddleware`; registry storage mode; evaluator/Zookie scopes; Strawberry adapter; field-level read gates; REBAC-safe relation loading; Strawberry-Django optimizer; field-backed structural relations; LocalBackend hardening. |
| **0.11.0 — MCP adapter** | `rebac.mcp.rebac_mcp_tool` decorator for FastMCP; actor resolution from request metadata (`REBAC_MCP_ACTOR_RESOLVER`); capability/resource gating; create-shaped actions via `create_relations`; sync, async, and streaming (async-generator) tool bodies. See [proposal 0004](./proposals/0004-mcp-tool-integration.md). |
| **0.11.x — async ORM scoping** | Verified the async ORM surface inherits scoping via Django's `sync_to_async` wrappers; closed the two bypasses (`aiterator()`, `aggregate()`/`aaggregate()`) that summarised/streamed rows outside the actor's scope. See Open questions § 3. |
| **0.19.0–0.22.x — retained features** | Filtered constants and generic paths; shared per-revision schema snapshots; policy-model write owners; scoped queryset embedding; metadata-derived versions and test isolation. |
| **0.23.0–0.24.2 — retained features** | Complete subject expansion in `lookup_subjects()`; the reference and differential suites; PostgreSQL CI and the tiered test suite; backed-edge write gates (invariant 5d); declared base managers; `rebac.schema_changes()`. |
| **After 0.24.2 — compiled permissions** | [Compiled permissions](#compiled-permissions--the-localbackend-read-path) are the single persisted `LocalBackend` read path ([proposal 0015](./proposals/0015-permissions-compiled-to-queries.md)): a permission is a query over the application's tables and the tuple table, the library stores no row per application row, recursion is unrolled to `REBAC_DEPTH_LIMIT`, gated queryset writes follow the key rule, and policy writes serialize on the generation row. Implemented; the trial on a consumer database at scale is pending. |
| **Next — SpiceDB conformance suite** | [SpiceDB conformance suite](#spicedb-conformance-suite-planned): generated cases checked against a pinned `spicedb serve-testing` in dev (`-m spicedb`) and CI; test-side projector for backings; deliberate divergences listed and pinned. The in-process oracle then stops copying the walker. |
| **Then — `SpiceDBBackend`** | `authzed-py` adapter; `WriteSchema` auto-push; the conformance suite becomes its cross-backend contract tests; SpiceDB Zookie translation; the projector grows from the test-side prototype. |
| **1.0.0 — Stable release** | Full docs, CI matrix green, stable audit/logging contracts, `select_related` compiler hook (or carved to 1.1). |
| **1.x** | `select_related` SQL compiler; bulk operations; `Meta.protected_fields` (descriptor-based field gating / true `"raise"` mode complementing [`read__<field>`](#field-level-read-gates-readfield)); PostgreSQL RLS defense-in-depth track. |

---

## Open questions

1. **Relationship table partitioning at scale.** Above ~100M rows, statements that read tuples can slow even with the indexes shipped. Worth designing a `(resource_type)` LIST partition scheme? **Lean: yes, post-1.0**, document the threshold and shipped migration helper.

2. **Swappable User dependency.** `auth/user` is hardcoded as a subject type label. Projects with `AUTH_USER_MODEL` aliases (`accounts.User`) need... what? Lean: a `REBAC_USER_TYPE` setting (default `"auth/user"`), plus `to_subject_ref()` consults `settings.AUTH_USER_MODEL` to decide. Settle in 0.1.

3. **Async ORM support.** *Resolved (0.11.x).* No separate async manager API is needed. Django implements every async `QuerySet` method (`aget` / `acount` / `aexists` / `afirst` / `aupdate` / `adelete` / `acreate` / `__aiter__` / `ain_bulk` / `aget_or_create` / …) as a `sync_to_async` wrapper around the sync method `RebacQuerySet` already overrides, so scoping is inherited and the `current_actor()` ContextVar carries into the worker thread — `await Post.objects.as_user(u).aget(...)` enforces with no extra code. The two methods that compute *without* routing through the sync `iterator` / `_fetch_all` path are overridden to re-apply scope: `aiterator()` (builds the row iterable directly) and `aggregate()` / `aaggregate()` (summarises the query without materialising rows). `bulk_create()` and `abulk_create()` preflight every proposed row through `check_new()` and stamp the queryset actor onto inserted instances. Actor-scoped conflict updates (`update_conflicts=True`) fail closed because a create grant cannot authorize changes to existing rows; use checked individual saves or explicit sudo for those upserts.

4. **Override layer precedence vs caveats.** When a `SchemaOverride` tightens a permission AND a caveat returns `CONDITIONAL`, what wins? Lean: tightening wins (security-fail-closed). Documented as a doctor warning.

5. **MCP authentication standardisation.** Tracked in [proposal 0004](./proposals/0004-mcp-tool-integration.md). As of May 2026, `ctx.request_context.meta` is the de facto channel for actor identity. If MCP adds a typed identity field in 2026/2027, the plugin should adopt it without a major bump.

6. **Per-tenant override scope.** Today `SchemaOverride` is global (one row applies to all tenants). For SaaS, we'll need a `tenant_id` column. Lean: ship 1.0 without it (single-tenant), add `REBAC_TENANT_RESOLVER` callable in 1.x driven by real demand.

7. **Web admin for the override layer.** v1.0 ships a Django admin form. A standalone admin SPA (separate optional package, `django-zed-rebac-admin`) could be more usable. Defer — gather user feedback first.

8. **Multi-database relationship resolution.** Relationship rows, scoped
models and backing-path models must share the operation's database alias,
because one statement joins them; `rebac.E015` rejects cross-database routing.
Querysets carry their alias into compiled reads; write owners use their write
alias throughout a gated write. The public
`check_access()` / `accessible()` signatures remain alias-free and use backend
routing. Candidate projection uses the write alias. Arbitrary cross-database
authorization remains unsupported; explicit alias handling is not a distributed
transaction or permission join.

9. **Query plans at scale.** Compiled statements have not been verified on PostgreSQL tables of tens of millions of rows. A trial on a consumer database is in progress ([proposal 0015](./proposals/0015-permissions-compiled-to-queries.md), gate G1). The shape under test is the one of [Statement shape](#statement-shape): a disjunction as a union of id sets.

10. **Recursion deeper than the bound.** `REBAC_DEPTH_LIMIT` is the only mechanism: a deployment with deeper chains raises it and pays one level of SQL per unit. A closure table for one recursive relation, or a recursive common table expression, would remove the bound (proposal 0015 § 8); neither is planned, and a recursive CTE needs SQL the project does not write by hand.

11. **The write gates.** With nothing to maintain after a write, the gates are the only reason to intercept writes. Two positions are open (proposal 0015 § 13): keep invariant 5d and finish proposal 0011 on top of the compiled predicate, or narrow the rule to the row that holds the changed column, which would let the tracked mixin, `REBAC_TRACKED_MODELS` and the Python emulation of ORM expressions go. The second is a policy change and the owner's decision, with `tests/test_security_*.py` as the bar.

12. **Ordering of grant changes and gated writes.** A gate locks the rows it decides on, not the tuples or parent rows its decision reads. Whether the library should offer a stronger ordering than the database's isolation level gives is open; lean: no, document the contract.

13. **Budgets.** There is no scale suite at the moment. Statement counts per save, SQL size and compile time per scope are quantities to measure before budgets are set.

---

## Appendix — what `django-zed-rebac` is not

- **Not a User model.** Use `django.contrib.auth.models.User` or any swappable `AUTH_USER_MODEL`.
- **Not an authentication system.** Use `django-allauth`, `dj-rest-auth`, `simple-jwt`, `python-social-auth`, or your own.
- **Not a session manager.** Django's session middleware is fine.
- **Not a multi-tenant database router.** Use `django-tenants` or `django-organizations`. `django-zed-rebac` is orthogonal — REBAC works within whatever tenant scope the project provides. (You CAN use `REBAC_TYPE_PREFIX = "tenant_acme/"` for soft-tenant scoping if rows-per-tenant fit in one DB.)
- **Not a GraphQL admin layer.** A future `django-zed-rebac-admin` package may add one; v1 ships a Django admin form for `SchemaOverride` and a CLI for `Relationship` introspection. Higher-level frameworks may layer their own admin surfaces on top.
- **Not an audit-log system.** A future `django-zed-rebac-audit` package may add one; v1 ships `PermissionAuditEvent` and emits structured logs.
- **Not a policy DSL** like Polar or Cedar. The schema language is SpiceDB's `.zed`, REBAC-first. ABAC fragments are expressed via caveats.
