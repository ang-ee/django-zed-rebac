# `django-zed-rebac` — Architecture

> Status: **alpha implementation guide** — specifies 0.23.0 (the permission index).
> Last updated: 2026-09-29
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
  - `LocalBackend` — pure Django. Permissions are read from a derived permission index that the library keeps current in the same transaction as every source write. Zero infrastructure; query size independent of graph depth.
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
python manage.py migrate              # creates source and permission-index tables
python manage.py rebac sync           # loads permissions.zed and builds the index
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
| **Permission** | A computed expression over relations. Defined by the schema, never written by applications; `LocalBackend` keeps a derived, rebuildable index of its holders (see [Permission index](#permission-index--the-localbackend-read-path)). | `permission read = owner + viewer` |
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
├─ Derived: PERMISSION INDEX (LocalBackend) ─────────────────────┤
│  Source: Tiers 1–3 plus backed application columns              │
│  Store:  rebac_membership, rebac_grant                          │
│  Loader: maintained by the write owners, same transaction;      │
│          `rebac index rebuild` recomputes from the sources      │
│  Editor: none — library-owned cache                             │
└────────────────────────────────────────────────────────────────┘
```

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

`LocalBackend` derives the relation's edges from the Django column through the
model's `_base_manager`, so application default managers cannot move the
authorization boundary, and writes to that column maintain the permission index
(see [Maintenance](#maintenance-and-transaction-ownership)). Tuple
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
validates every Django path, lookup, and target model. Index derivation and
maintenance share these resolved owners.

An edge change requires `write` on every affected resource row whose declared
backing watches the changed column or through table, provided that resource
type declares a `write` permission. This includes reverse FK and M2M accessors,
`RebacTrackedMixin` deletes, queryset writes through an auto-created through
model, scalar predicate columns, nested field paths whose source is another
model, and both mirror rows of a symmetrical self-M2M. `RebacMixin` deletes
are not gated; the collector's CASCADE and SET_NULL rows are gated under the
ambient actor, not the actor pinned on the deleted row; instance-level
through-model writes are neither gated nor maintained, so `rebac index verify`
reports them as drift (all three: proposal 0011). The gate finds source rows through the unchanged
prefix of each affected path, before mutation and under the carrying or ambient
actor. Bulk owners snapshot watched columns per model, resolve changed FK
targets and reverse sources once per watched field, and check the union of
affected IDs with one scoped permission query per declaring resource type.
Through-table source capture materializes the source FK values of changed
rows, then queries only declaring rows at the path prefix before the M2M hop.
Using the full path would compare target PKs to source PKs and miss edges when
the two sequences differ.
Instance sudo does not carry through a related manager. A denied
declaring resource is audited after the failed owner transaction unwinds.
Tracked models can be backing sources even when their own saves have no
resource write gate. A backing type without a permission literally named
`write`, including resource types that name it `edit` or `update`, is maintained
without an actor gate; consumers protect those columns with Django permissions
(proposal 0011). Attribute-backed changes check both the old and proposed
virtual container IDs when the container key changes.

Dynamic attribute containers are named by the canonical Python spelling of the
column value (`ResolvedAttributeBacking.container_id_of`); a non-canonical id
such as `"01"` for integer `1` names no container. Derivation reads attribute
columns in SQL, which follows the column's database collation, while the
walker compares Python strings exactly. Give attribute columns a deterministic,
case-sensitive collation (MySQL's default `*_ci` collations are not) so the
index and the oracle agree. Dynamic attribute fields need a canonical
integer, text or UUID codec (`rebac.E014`). Boolean, Date and Decimal dynamic
containers are refused in 0.23.0; a fixed `resource`/`value` anchor does not
encode its attribute value as an identity and remains supported. `rebac.W009`
warns, best-effort, about case-insensitive attribute collations.

#### Backings are `LocalBackend`-only until the projector ships

Every backing kind below is derived into the index by `LocalBackend` and omitted from
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
and through arrows. Filter columns are watched fields: updating them maintains
the index without tuple writes. Filtered constants never produce type-level
index rows. Native `.zed` rendering retains the JSON directive
deterministically; SpiceDB export continues to omit backing metadata.

For **unfiltered** constants, resolution is fixed-target rather than per-row,
which has two consequences in
`LocalBackend`:

- **One type-level index row, not one per resource.** Because the target object
  is the same for every row, the index stores the grant once with
  scope `(resource_type, "*", "$type")`, distinct from wildcard subject terms.
  New rows need no per-resource grant for this constant. Scopes include the
  type-level grant arm; a named subtraction site applies any resource-specific ban at read time.
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
│  │  permission index  │    │  planned authzed adapter │          │
│  │  + cel-python for  │    │  roadmap implementation  │          │
│  │  caveated checks   │    │                          │          │
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

    # Decorators
    require_permission, rebac_resource,

    # Backend interface
    Backend, LocalBackend, SpiceDBBackend,
    CheckResult, Consistency, Zookie,
    ObjectRef, SubjectRef, RelationshipTuple,

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
```

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
| **Runtime-editable `includes` + `effective_member`** | Hierarchy editable at runtime without a schema PR. Roles declare `relation includes: <namespace>/role` + `permission effective_member = member + includes->effective_member`; resources hold a direct role object and arrow to `effective_member`. `rebac.roles.imply(parent=..., child=...)` writes the direct child-role tuple. LocalBackend materializes this reachability during maintenance. |

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

Source models hold relationships, the schema baseline, provenance and overrides.
Registry, audit and schema-generation models support those surfaces. The seven
internal index tables are listed under [Tables](#tables).

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

**Frozen contract.** The shape mirrors `authzed.api.v1.Relationship` exactly. Renames are breaking. Indexes are critical (permission-index derivation reads them on every relationship write and rebuild) and ship in the initial migration — never as a documentation step.

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
  Permission-index derivation reuses these indexes heavily, so the gain
  compounds in rebuilds and write maintenance.
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
a new operation observes the revision and `index_revision` together; a cold
load checks the witness again to avoid publishing a schema spanning a commit.
The pin stores both witnesses. Retry exhaustion uses one uncached load checked
against the paired witness before and after; a changing fallback fails closed.
Within a scope, repeated pinned schema reads and eligible decision-cache hits
issue no queries. There is no revision SELECT on each cache lookup.

The five policy models (definitions, relations, permissions, caveats and
overrides) own writes through their shared model/queryset, including base-manager
and reverse-relation bulk writes. They acquire the maintenance lock before
policy reads or writes, rebuild affected definitions and dependents, then
publish `SchemaGeneration.revision` and matching `index_revision` in the same
transaction. `SchemaGeneration.objects.advance(using=...)` publishes the token;
callers must arrange maintenance before publishing readiness. A deletion
receiver covers cascades originating outside these owners. Conflict-ignoring
and upserting policy bulk creates are refused. Raw SQL policy writes bypass
these guarantees and require sync/rebuild before serving reads.

Revision tokens are fresh identities, not rollback-reusable counters. Their
visibility follows database isolation, and they never enter deterministic
schema output. Same-process schema writes evict shared snapshots. Missing
witness rows are repaired by supported schema writes or sync/rebuild without
restarting the backend; there is no sticky degraded mode. Permission reads
against a missing, unmigrated or mismatched index fail closed with
`SchemaError` / `rebac.E013`. Apply migrations and run `rebac sync` or
`rebac index rebuild` before serving. Migration `0005` does not seed the
revision row; `0007` seeds only the maintenance lock.

Every explicit sync publishes a fresh revision. If the index is unready, even
an unchanged sync rebuilds everything before setting `index_revision`.
`sync --check` is read-only.

Evaluator invalidation clears schema pins, including on subscription emissions;
an unchanged revision can reuse the shared parsed tree. Connection observers
clear pins around writes and transaction boundaries, including rollback.
Manual transaction management retains operation pins only. Permission decisions
remain uncached inside transactions. Backing rows are never stored in schema
snapshots; supported writes maintain their index representation.

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
an explicit evaluator invalidation boundary, and any bypassed source/index
maintenance additionally requires rebuild. Invalidation alone cannot repair
derived rows.

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
| `REBAC_DEPTH_LIMIT` | `8` | `int` | Hard cap on the permission walker (`check_new`). Does not bound permission-index reads or derivation, which terminate on cycles by fixpoint. |
| `REBAC_TRACKED_MODELS` | `[]` | `list[str]` | Third-party backing models (`"app_label.ModelName"`); explicit-sender save/delete receivers. User and Group are automatically tracked. |
| `REBAC_INDEX_CONDITION_LIMIT` | `256` | `int` | Maximum caveat instances in a normalized logical contribution per `(scope, node, holder, site)`. Exceeding it raises `SchemaError` and rolls back the write. |
| `REBAC_INDEX_LOOKUP_LIMIT` | `64` | `int` | Maximum static read-plan lookups for a permission. `rebac.E019` rejects larger plans during checks. |
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
`rebac.E017` requires a positive integer condition limit (booleans are invalid).
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
| `rebac.E009` | Error | A field-, attribute- or const-backed relation cannot be resolved: missing Django model, identity field, relation path, attribute or filter lookup; a path that ends on a different model than the declared subject type; or a const-backed relation whose target type has no schema definition. |
| `rebac.E010` | Error | Const-backed arrows form a schema evaluation cycle. The existing const-arrow validation remains unchanged in 0.23.0, including for preflight; the index's support for positive data cycles does not relax this schema restriction. |
| `rebac.E011` | Error | `Meta.rebac_subject_relation` names a relation the model's effective schema definition does not declare. |
| `rebac.E013` | Warning (database; historical ID retained) | The permission index is not built for the current schema revision (`SchemaGeneration.index_revision` differs from the revision), or was derived by a different program (`index_program` differs from the compiled program's digest). Allows migrations and fresh test databases to reach `rebac sync` or `rebac index rebuild`; permission reads still fail closed. |
| `rebac.E014` | Error | A resource identity field, field-backed target identity or dynamic attribute container column has no canonical wire/column codec (supported: integer/auto, char/text/slug, UUID). Custom encoded field conversions that SQL cannot reproduce are refused. Fixed Boolean attribute anchors do not require a Boolean codec. |
| `rebac.E015` | Error | A scoped or backing model's **write** alias differs from the relationship write alias, so its writes cannot maintain the index in the same transaction. Separate read replicas are allowed. |
| `rebac.E016` | Error | A named intersection or subtraction site is on a recursive dependency cycle, including a self-loop, or a generated node name exceeds 64 characters. Prints the cycle or offending node and any introducing override. |
| `rebac.E017` | Error | `REBAC_INDEX_CONDITION_LIMIT` is not a positive integer, or is a boolean. |
| `rebac.E018` | Error | A backing-path model is neither `RebacMixin`, `RebacTrackedMixin`, nor explicitly tracked. Auto-created throughs with an owned/tracked endpoint and configured User/Group models are tracked automatically. Invalid `REBAC_TRACKED_MODELS` labels are errors too. |
| `rebac.E019` | Error | A permission's static read plan exceeds `REBAC_INDEX_LOOKUP_LIMIT`, or the limit is not a positive integer. The diagnostic prints the plan. |
| `rebac.E021` | Error | The schema declares a caveat but `cel-python` (the `caveats` extra) is not installed, so caveat bodies cannot be validated or evaluated. |
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
``check_access`` (answered by the permission index), so all post-hop evaluation reuses the canonical
semantics — caveat-conditional outcomes propagate as
``CONDITIONAL_PERMISSION`` with the union of missing caveat
parameters. The dispatch (operator precedence, sub-permission cycle
detection, ``anonymous`` / ``authenticated`` built-ins, tri-state
combinators, ``REBAC_DEPTH_LIMIT``) uses ``rebac.schema.walker``. Persisted
checks, including caveated checks, use the index; the former LocalBackend
walker is retained only as a frozen test oracle.

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

### Permission index — the LocalBackend read path

Every persisted LocalBackend read uses one derived index: checks, scopes,
`accessible()`, `lookup_subjects()`, field gates and bulk guards. Relationships
and declared backings remain the source of truth. The index is rebuildable and
maintained in the source transaction. The walker remains for `check_new` and
the frozen test oracle. No alternative scope compiler is installed.

#### Stored sets and terms

The index stores monotone sets only: union, arrows, recursion and backings.
Each intersection or subtraction is a named *site*, held by reference in a
grant row. No set operation is expanded into per-actor rows. Reads evaluate
sites against the current index. This follows the approved Leopard design.

| Model / table | Fields | Keys and indexes |
|---|---|---|
| `IndexTerm` / `rebac_term` | `type`, `object_id`, `relation` | Unique triple. |
| `IndexEdge` / `rebac_edge` | `resource`, `resource_type`, `relation`, `subject`, `target`, `source`, `expires_at`, condition/key | Unique `(resource, relation, subject, source, condition_key)`; latest expiry retained. |
| `IndexMember` / `rebac_membership` | `member`, `member_type`, `set`, `expires_at`, condition/key, `pass_id`, `round` | Unique `(member, set, condition_key)`; indexes `(set, member)`, `(pass_id, round)`. |
| `IndexCover` / `rebac_grant` | `scope`, `resource_type`, `node`, `holder`, `site` (Char 64, default `""`), `expires_at`, condition/key, `pass_id`, `round` | Unique `(scope, node, holder, site, condition_key)`; indexes `(resource_type, node, site, holder)`, `(scope, node)`, `(holder, site, node)`, `(pass_id, round)`. |
| `IndexWork` / `rebac_index_work` | `pass_id`, `kind`, `term`, `node`, `phase` | Materialized old/new states and region; indexes include `(pass_id, phase, kind)`. |
| `IndexState` / `rebac_index_state` | `key` | One global lock row per alias. |
| `SchemaGeneration` | `index_revision`, `index_program` | Readiness witnesses: the policy revision the index was derived for, and the digest of the program that derived it. |

Every index foreign key to `IndexTerm` retains its database constraint and
uses `on_delete=DO_NOTHING`. The library deletes dependent rows before terms.
Public `QuerySet.delete()` takes Django's fast path on these internal tables.

A plain grant row (`site=""`) grants to its holder and the holder's members.
A site grant row delegates to the named site. The site is evaluated at the
object being checked when the row's holder is its own scope (a site used as an
arm, and type-level rows), and at the holder otherwise (an arrow target or a
constant target). A type-level holder of another type never occurs: arrows and
constants instantiate it at their concrete target.
Reserved terms include `(T,"*","")` for a wildcard,
`("$authenticated","*","")`, the distinct anonymous class term, and
`(T,"*","$type")` for a type-level scope. Literal `"*"` resource IDs are
rejected on writes. Wildcards match concrete actors of their type, never
subject-set actors; authenticated matches any resolved nonempty actor except
the anonymous singleton. Unknown actors can match wildcard memberships.

Expiry is the latest across alternative paths of the earliest expiry along
each path. `TIME_MAX` means never. A row is active when `expires_at > now`,
where `now` is bound at statement execution. The sentinels support aware and
naive datetimes according to `USE_TZ`. Relationship expirations must lie
strictly between them. No graph re-derivation is needed when time passes.

The index gives every row of a resource model a term, so that enumeration
lists a row that holds a permission only through a type-level grant; the
vacuum keeps the object terms of every defined type for the same reason. It reads
those rows only when Django manages the model's table. An unmanaged model may
be the anchor of a type whose objects exist only in relationships, such as a
role, and have no table at all: the objects of its type are the ones that
relationships, constants and backings name, as for a type with no model. A
backing that names a column of an unmanaged model still reads it.

Both relationship storage modes use the same index on the relationship write
alias. `rebac.E015` refuses cross-alias backings. Identity codecs support
integer, text and UUID fields, including vendor-specific limits and canonical
spelling; unsupported fields raise `rebac.E014`.

#### Program and read plan

The immutable program contains every relation and permission, plus internal
mono operand nodes and named sites. Internal nodes are named by content:
`<permission>.<digest>`, where the digest covers the node's operands or lowered
expression and the override that contributed it. A name never changes meaning
when an override is added or removed. Operands of a site are always nodes; a
nested site is wrapped in a mono node. Compile-time lowering runs to a fixpoint:
`X - nil = X`; `X & nil = nil & X = nil - X = nil`. A site with a deadline
is never lowered: a tighten to `nil` means "deny until the deadline". A
permission with only nil arms is nil.

The program is compiled from the baseline and every override row, expired rows
included, so it does not depend on the clock. Its digest is published with the
index as `SchemaGeneration.index_program`. Its watched-model map covers identities, backing paths,
filters, through rows and inherited fields. The program stores SCC strata and
the declared userset relations.

Override composition is `((baseline ∪ extends) − disables) & tightens`.
The composition module keeps each contributing override's deadline on its
tagged arm or operand, and the plain `compose()` derives from that same
implementation. Extend and loosen rows limit the expiry of the arm they add.
A disable or tighten site becomes the identity at its deadline. Recaveat
changes the caveat definition read at query time, so projection retains
recaveatable leaves.

For node `N`, `held_sites(N)` lists named sites that can occur among its
holders through monotone edges. The static plan size is
`lookups(N) = 1 + Σ[lookups(left(s)) + lookups(right(s))]` over those sites.
`rebac.E016` rejects a site on a recursive SCC, including a self-loop,
and names the cycle and introducing override. The cycle graph contains
derivation dependencies only: references, arrows and site operands. A
relation's subject sets are not dependencies, because grants hold them by
reference. `rebac.E019` rejects a permission whose plan exceeds
`REBAC_INDEX_LOOKUP_LIMIT` (default 64), printing the plan.

Both checks run as system checks for declared schemas, and in the policy write
owners for every schema or override write, `sync` included. A refused program
raises `SchemaError` and rolls the write back, so a read never discovers one.
A runtime `disable` or `tighten` on a recursive permission puts a site on the
cycle and is refused the same way.

#### Derivation

Each rule is a set-based queryset streamed through `stream_create`:

| Rule | Rows for mono node N at resource type T |
|---|---|
| Relation r | Each edge `(R,r,subject)` yields `(R,N,subject,"")`. |
| Reference M | Copy M at the same scope, retaining holder and site. |
| Arrow `via->p` | For each edge `(R,via,t)`, copy p's rows at t, taking the earlier edge/row expiry and conjunction of conditions. Follow t's object even when the edge subject has a userset suffix. Type-level target rows apply to each edge of the target type: each type-level row, and there are few, is applied with one indexed query over the arrow's edges to targets of its type. Instantiate a type-level site holder at t. |
| Builtin and unfiltered constant | Produce one type-level row with a reserved holder, or target rows at type level. |
| Site s as an arm | For each scope with any left-operand row, emit `(R,N,R,s)` with the latest expiry of those rows and no condition. A type-level left row emits `(T*,N,T*,s)`. |

A concrete row is stored even when a type-level row implies it, so a
rebuild and incremental maintenance produce identical rows. Recursive strata
use insert-only semi-naive rounds
with `(pass_id, round)`: later rounds join the preceding delta. Data cycles
terminate at a finite fixpoint without `REBAC_DEPTH_LIMIT`.

Membership closure covers declared userset relations, including wildcard
and subject-set members. Grants hold a userset term by reference, so a
membership write never rewrites the grants of the set's consumers. A relation
has grant rows when a node references it: a permission that names it, an arrow
that targets it, or a site that takes it as an operand. A write to such a
relation re-derives its rows and their derivation dependents. A relation no
node references has no grant rows and is read from the closure.

A fully pinned caveat is evaluated at derivation when the result is definite
and no recaveat override targets that caveat: a true result stores the row
without a condition, a false result stores no row. Split ORs of distinct
`IN (subquery)` branches into separate queries. Stored conditions contain
only `and`/`or` formulas over caveat instances. Python handles only rows
that carry formulas. The normalized logical contribution is bounded by
`REBAC_INDEX_CONDITION_LIMIT`; exceeding it raises `SchemaError` and
rolls the write back. Index writes use Django `bulk_create` and run inside
the owner's `atomic(savepoint=False)`, without a savepoint per batch.
No raw SQL, trigger, database function or undocumented ORM API is used,
with one deliberate exception: the queryset-scope plan cache (0.23.1) keeps the
SQL that Django compiles for a plan, through `Query.get_compiler()`, and embeds
it in the outer statement from a custom expression. The SQL is Django's own; no
statement is written by hand.

The exception stays because no public-API design comes close. Embedding a
queryset makes Django clone and re-resolve every expression node of it
(`Query.resolve_expression`), and for a plan that is about 70% of compile time.
Measured on an eleven-lookup plan: reusing the built plan and compiling it
each time takes 18.5 ms; a compact actor set stored in the index, 17.4 ms;
matching the actor with a join instead of subqueries, 18.8 ms. Even a
placeholder actor set, which is incorrect, only reaches 10.9 ms. The cached
SQL takes about 1 ms.

#### Reads

One `member(node, x, actor, polarity)` compiler tests whether the actor
satisfies the node at object expression `x`. A queryset scope passes the
row's encoded identity and a constant actor. A point check passes two
constant terms. `lookup_subjects()` passes a constant resource and an
`OuterRef` candidate subject. The compiled predicate is one `EXISTS` per
node, where `T*` is the type-level scope of the node's type:

```
member(N,x) = EXISTS g IN grants:
    g.node = N AND g.scope IN (x, T*) AND active AND condition allowed by polarity
    AND (  (g.site = "" AND g.holder IN H(actor, polarity))
        OR, for each site s in held_sites(N):
             g.site = s AND type(g.holder) = type(s)
             AND sat(s, CASE WHEN g.holder = g.scope THEN x ELSE g.holder END) )

sat(s,y) = member(left(s),y) AND member(right(s),y)      for &
         = member(left(s),y) AND NOT member(right(s),y)  for -
```

Each site compiles once, so SQL size is bounded by the plan: at most
`a + b·k` for `k` lookups.

A queryset scope tests each row's term against one uncorrelated subquery: the
terms of the type that the actor holds the node on. The SQL of that subquery
depends only on the program, the node and the shape of the actor (its type and
relation, whether its id is empty, whether it is the anonymous singleton), so
the library compiles it once per process for each of those keys, in a bounded
cache of 128 plans. The actor's id, the clock and the manual-schema revision
are parameters, prepared when the statement that embeds the plan compiles; the
readiness fence runs when it executes. The queries nest, and an object is a column of one
enclosing query; the compiler numbers the queries from the outside in and
renders an object for the level that references it.

`H(actor, polarity)` contains the actor's own term; the wildcard term of its
type when the actor has no relation suffix, empty and `*` ids included; the
memberships found from both terms, definite in a positive position and
possible in a negative one; `$authenticated` unless the actor is the anonymous
singleton or has an empty id; and the anonymous class term for that singleton
only. Under the right side of subtraction, use every active row and possible
membership; in a positive position, use unconditional rows and definite
membership. A nested subtraction reverses polarity again. A deadline on a
disable or tighten site makes the site the identity from that instant on,
decided when the statement executes.

Subqueries are correlated only through `EXISTS` against the object or holder
expression. Multi-valued joins keep related constraints in one
`filter(Q(...) & Q(...))`, or use `Exists`/`OuterRef`. SQL size is
independent of data and of data depth, with or without `context`.

A check answers from two predicates: `HAS` when the definite one holds, `NO`
when the possible one fails. Otherwise it reads, in one statement, the rows of
the read plan that the actor can match, and evaluates the same recursion in
Python, returning the three check states.

A conditional result reports the parameters it still needs. A node holds
through alternative paths; a path is a row's condition, then the membership
that makes the actor a holder or the site the row holds. A path that cannot
hold needs nothing, and neither does a path that needs everything a shorter
one needs. A conditional site needs what its conditional operands need. The
set does not depend on the order of arms, rows or tuples, and is within the
set the walker reports: the walker also reports what failing paths and
unneeded alternatives would need, in an order-dependent way.

`accessible()` returns scope identities. Subject enumeration draws candidates
from the holders of the node's rows and of the left operand of each held
site, recursively, expands them through possible memberships, filters them
with `member()`, then sorts by wire reference. Class holders (`authenticated`,
`anonymous`) contribute no candidates, as in the walker; a stored wildcard
subject is returned as `type:*`. Enumeration admits only definite results. A
model-level check (empty resource id) is `member(N, T*)` or any accessible
row; `T*` also stands for a resource that has no term yet.

A statement cannot evaluate a caveat. For a read with `context`, the formulas
on the rows of the read plan are decided before the statement is built and
named to it in one parameter, so the statement is the same whatever the number
of caveat instances. A row is definite when its formula holds and possible
unless its formula fails; a formula written after the preparation counts as
possible only.

Every read statement requires the index to be published for the current
policy revision and derived by the program the statement was compiled with.
A statement that carries verdicts prepared from a pinned schema also requires
that schema's revision and closes at its earliest override deadline. A
mismatch raises `rebac.E013` for an immediate read and yields no rows for a
lazy queryset evaluated later. Pending lazy querysets see committed index
changes at evaluation time; already populated Django result caches retain
their normal behavior.

#### Maintenance and commands

Tuple owners, resource/tracked mixin owners, and queryset write owners wrap
the source write and maintenance in one transaction on its write alias.
`Relationship` and `RelationshipRegistry` queryset deletes are tuple-owned:
they capture each matching tuple before deletion and update the index in the
same transaction. Queryset `update()` on relationship rows is refused because
it can change a tuple's wire identity or policy without a matching tuple
write owner; use `delete_relationships()` and `write_relationships()`.
Their `bulk_create()`, including conflict updates, is tuple-owned as well: it
captures existing matching tuple identities before SQL and derives the new
state before returning. A batch of 500 tuples or more (`BULK_REBUILD_ROWS`)
rebuilds the whole index inside the same owner instead of deriving
incrementally: region expansion is bounded by a write's neighbourhood and
pathological for a seed, while a full rebuild is the pass `rebac index
rebuild` runs and the scale budgets bound. Conflict updates must target the tuple unique constraint
and update only `caveat_context`, `expires_at`, or `written_at_xid`; a primary-key
upsert or tuple-identity move is refused with `ValueError`.
`write_relationships()` runs under one tuple owner, interns registry references
in bulk, and saves rows without a second owner or savepoint per tuple. Model
save signals still run for each row, so consumer signal writes remain visible
at the outer Zookie; repeated tuple identities take the last supplied metadata.
It raises `NotImplementedError` for an unsupported write operation, not an
authorization denial.
Instance saves and deletes follow the same tuple owner, capturing the old
tuple before mutation and deriving the new state after mutation. A nested
tuple write derives its effects before returning its Zookie, even inside an
open model owner. It leaves the outer owner's captured work intact for a final
derivation after the source write. Only the outer owner consumes work, vacuums
terms, or rebuilds a changed schema. This extra derivation is the cost of
read-your-writes correctness within a transaction. A nested pass currently
re-derives the accumulated outer region; proposal 0009 tracks that cost.
Plain third-party tracked models use explicit-sender signals; their callers
must use `atomic()` or `ATOMIC_REQUESTS`. In autocommit, the receiver
logs and warns under decision D2. `rebac.E018` rejects unowned,
untracked backing paths. Explicit-sender cascade and M2M receivers cover
writes without an owner; no sender-free receiver is installed.

An index exists to be maintained once a policy is installed on the alias.
Before the first `sync`, a source write proceeds without a pass, the index
stays unpublished and reads stay closed. The same holds while migrations
run and the library's own tables are missing or lack a column: a write that
cannot read the readiness witness is not a write to maintain. `sync` then
builds the whole index.

A pass reads and derives only the resource types its region contains. It
reads no whole source table, model table or index table, so its cost does not
depend on the size of the index or of the schema. A full rebuild does.

Each pass locks `IndexState("global")` before source reads, captures old
identities durably in `IndexWork`, applies the source write, projects new
edges, materializes the affected region and deletes and re-derives it in
dependency order. Region closure follows same-resource dependencies,
incoming arrows, membership ancestors and old/new backing paths. It does not
follow holders: a grant that holds a set by reference does not change when
the set's members do. Nor does it take in an edge's target: an edge belongs
to its source, and no rule reads the edges that point at an object, so
writing a row does not re-derive the other rows that share its target. A write to a relation no node
references repairs only memberships. Schema owners rebuild
affected definitions and dependents and publish `index_revision` only
after success. A missing lock row after flush is repaired in the owner.
Work rows and unused snapshot terms are cleared at pass end. The index
logger records phases, strata, nodes and rules with rows in, rows out,
Python rows and elapsed seconds.

`rebac index rebuild [--type T ...] [--database ALIAS]` is idempotent
under the same lock and vacuums unused terms in dependency order.
`rebac index verify` re-derives in a rolled-back transaction and compares
a streaming sorted merge of full row payloads: term identities, site,
expiry and condition, excluding surrogate IDs and bookkeeping. Drift
produces a nonzero exit status. Raw fixtures and unsupported bulk writes
require rebuild.

#### Known limits

Reads containing sites grow with the static read plan, bounded by E019.
Sites on recursive cycles are refused by E016, for declared schemas and for
runtime overrides alike; the walker evaluated those per object, so a
`disable` or `tighten` on a recursive permission that 0.22 accepted is now
refused when written. An arrow into a node with type-level rows materializes
one row per edge for each type-level row. The index has no depth limit: where
the walker raises `PermissionDepthExceeded`, the index terminates and
answers. Broad graph fan-out may still make maintenance expensive, and the
0.23.0 lock is global per alias. SQLite, supported for tests, must allow a
deeply nested statement: SQLite 3.45, the system library of Ubuntu 24.04, has
a fixed parser stack and refuses a read plan of nine lookups with `parser
stack overflow`; SQLite 3.46.1 accepts it.

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
it does not add a recursion restriction or certify index readiness. The latter
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
2. `_default_manager` points at it; the metaclass injects `base_manager_name` naming an **owning, unscoped** manager, even when consumers declare their own `Meta`. It never applies actor scope to reads. Its writes maintain the index, and those that change a backed FK edge (reverse-FK `add(bulk=True)`, `update` of a watched FK) run the backed-edge gate of invariant 5d; an `update` of a watched scalar column through the base manager is not gated (proposal 0013), and collector `SET_NULL` rows are gated under the ambient actor only (proposal 0011).
3. `save_base` owner — create/write and field gates before consumer `pre_save` receivers.
4. `delete` owner — root gate and a deletion ContextVar carrying the root actor/bypass for collector children; explicit-sender `pre_delete` gates those children only. Owners batch identity tuple cleanup.
5. Queryset materialisation hooks (`_fetch_all()` and iterators) stamp the resolved actor onto every loaded instance. `from_db()` snapshots original field values for write checks.
6. `Meta` extension — the metaclass captures `rebac_resource_type`, `rebac_id_attr`, `rebac_default_action` and `rebac_subject_relation` (the `REBAC_META_OPTIONS` tuple), strips them before Django's `Options` sees them, and re-attaches them on `_meta`. `rebac_subject_relation` makes `to_subject_ref(instance)` emit the model's object reference as a subject set (`auth/group:<id>#member`); `rebac.E011` checks the relation exists. See [proposal 0006](./proposals/0006-model-subject-identity.md).

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
| `Model.objects.all()` / `.filter(...)` / `.get()` / `.count()` / `.exists()` | `read` (or `Meta.rebac_default_action`; override per chain with `.with_action(action)`) | The queryset injects the backend's lazy permission predicate (the permission-index scope for `LocalBackend`), falling back to `resource_id__in=<accessible(actor, action, type)>` for backends without one. |
| `Model.objects.create(**fields)` | `create` on the proposed row's forward relations | Constructs the instance and delegates to `insert()`. |
| `Model.objects.insert(obj)` | `create` on the proposed row's forward relations | Pins queryset scope on the prepared instance; the `save_base` `check_new` gate authorizes the insert. |
| `Model.objects.bulk_create(rows)` | `create` on each proposed row's forward relations | Every candidate is preflighted before Django issues insert SQL. |
| `instance.save()` (loaded/non-adding instance) | `write` on the row | `RebacMixin.save_base`, before consumer `pre_save`. |
| `instance.save()` (new instance) | `create` on the proposed row's forward relations | `save_base` projects the constructed candidate into `check_new`. |
| `instance.delete()` | `delete` on the row | `RebacMixin.delete`; explicit-sender `pre_delete` checks collector children. |
| `Model.objects.update(**kwargs)` | `write` on each affected row and every resource row whose backed FK edge changes | Manager intersects the queryset PK set with the actor's `write` scope; raises if any in-scope row is excluded. A backed FK update checks old and proposed source rows before SQL; expressions whose proposed FK cannot be resolved are refused. When the update touches watched fields, the permission index is maintained set-based in the same transaction. |
| `Model.objects.delete()` | `delete` on each row | Same pattern. |

For an automatically created M2M through table, a `RebacMixin` owner needs
`write` on its row even if the table backs no relation. When the table is
watched, `add` / `remove` / `set` / `clear` also check every
resource type that owns an affected backed edge, including reverse-manager and
symmetrical self-M2M calls. The related manager preflights the changed pairs once
outside Django's own atomic block so denial audit survives that rollback.
Its `set()` wrapper accepts Django's positional and `objs=` keyword forms.
The `m2m_changed` receiver maintains the index, while queryset writes through
the auto-created through model's `objects` manager (`bulk_create`, `update`,
queryset `delete`) are tracked and gated; the related-manager wrapper exempts
only the exact pairs it already gated, as inserts or deletes, so rows a
consumer `m2m_changed` handler writes during the call are gated on their own.
Instance-level through writes are neither gated nor maintained (proposal
0011); writes through the through model's `_base_manager`, and a
related-manager call from a multi-table child of the declaring model, are
not gated (proposal 0013). Through captures use the changed FK pairs and
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
`RebacMixin._base_manager` remains an unscoped infrastructure write path and
maintains the index. A reverse FK `add(bulk=True)` passes a related model value
through that manager and is gated on the declaring resource before SQL, under
the ambient actor: the actor pinned on the related manager's instance, the
moved row's own `write`, and a block `sudo` over a pinned actor are not yet
honoured there (proposal 0011).

A `bulk_update` of a watched column is gated per statement. The owner freezes
the rows a statement writes, before its SQL, under a per-statement tag in the
work table, and resolves each row's value from the `Case` in Python: every arm
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
The guard attributes a column to a queryset's projection only when it is read
from that queryset's own row: directly, or through `OuterRef` from a nested
query. A column a nested query reads from its own tables belongs to that
query's scope decision, not the outer one: a bypass (`sudo` /
`system_context`) subquery may read it, and an actor-scoped subquery answers
for its own projection when it is resolved, so
`Subquery(Model.objects.with_actor(a).values("gated"))` still raises while an
`Exists` over a bypass queryset that filters on a gated column hands the outer
row only its boolean.
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
nested in the call. Inside an open model owner, a nested tuple write derives
its index effects before returning, so a read in the same transaction at that
token sees them. An ambient LocalBackend token only advances; a later outer
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
python manage.py rebac index rebuild [--type T ...]   # derive the permission index from its sources
python manage.py rebac index verify  [--type T ...]   # CI / periodic: diff against a rolled-back rebuild; non-zero on drift
```

`rebac explain` renders the effective permission expression after active
`SchemaOverride` rows have been composed with the baseline.

`index rebuild` and `index verify` are specified under
[Index commands](#commands). Run `rebuild` after any write that
bypasses the index owners: raw SQL, data migrations over historical models,
`loaddata` with `raw=True`, or bulk queryset writes to tracked third-party models.

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
  6. Recompile in-memory expression tree.
  7. Rebuild the permission index for the definitions whose effective
     expressions changed and their dependents (all definitions on a fresh
     database), and set `SchemaGeneration.index_revision`, in the same
     transaction as the schema writes.
```

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
| Upgrade adds the permission-index tables to a populated database | The migration creates empty tables and leaves `index_revision` unset. Entry readiness checks raise `SchemaError` (`rebac.E013`) until `rebac sync` or `rebac index rebuild` runs. The setup system check is a warning so migrations can finish. An index becoming unready after scope construction is fenced out by the permission statement. |
| Legacy database objects on upgrade or reversal | Fresh installs create no REBAC triggers/functions. `0005` creates the revision table without seeding row 1; its `RunPython(noop, uninstall, atomic=False)` cleans up legacy objects on reversal. `0006` retains forward cleanup and schema-owner model options. `0007` seeds only `IndexState("global")`; sync/rebuild publishes readiness. |

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

**Our behaviour:** archived/inactive rows are visible to permission evaluation by default. Soft-delete is orthogonal to permission scope. If you want to hide archived rows from a list endpoint, filter at the queryset level (`Post.objects.with_actor(u).filter(archived=False)`) — but the permission walk over `Relationship` does not exclude them.

**Why:** an admin with `delete` on a soft-deleted resource needs to be able to un-archive it. If the permission layer hides it from them, the admin's only recourse is to bypass the layer entirely (sudo / direct SQL) — which is exactly the failure mode we're trying to prevent. Make the policy explicit: archived ≠ inaccessible.

### Cross-reference

These four are highest-impact. The full Odoo 19 research note (with file/line citations into the upstream tree) lives at `../odoo-research/notes/01-permissions-security.md` for contributors auditing edge cases. If you're proposing a new feature that resembles `ir.rule.domain_force` (Python evaluated at runtime against ambient context), `_check_company` (cross-relation invariants enforced at write time), or a new ambient context key, read the research note first; chances are we've ruled it out by design.

---

## Testing

Three layers of tests define the project target:

1. **Unit tests** (`pytest`): pure-Python, no database. Schema parsing, expression compilation, codename mapping, build determinism.
2. **Integration tests** (`pytest-django`, `@pytest.mark.django_db`): SQLite and PostgreSQL. `RebacMixin` end-to-end, manager scoping, index semantics and maintenance, signal handlers.
3. **SpiceDB conformance tests** (`-m spicedb`, planned right after 0.23.0): generated schemas and data evaluated by a real SpiceDB and by `LocalBackend`, answers compared. See [SpiceDB conformance suite](#spicedb-conformance-suite-planned). They do not need `SpiceDBBackend`. They drive SpiceDB directly and become the cross-backend contract tests once that backend lands.

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
| **3. Release** — `make test-release` | Nightly on `main` and on demand before a release. Never inside a fix loop. | `slow` on SQLite; the whole suite including `slow` on PostgreSQL; `scale` alone on both; the full `index_exhaustive` sweep; the `schema_vendors` PostgreSQL/MySQL contracts; a randomized parallel run with seed 137; the SpiceDB conformance suite once it lands. | About 12 minutes on an 18-core machine; independent parallel jobs in CI, where the sweep is the longest. |

Rules that keep the tiers honest:

- **Tier 1 has a per-test budget.** A test that takes more than 2 seconds on
  a developer machine is marked `slow`. A 10-second per-test timeout in tier
  1 turns a test that outgrew the budget into a failure, not a slower loop.
  The targets that select `slow`, `scale`, `index_exhaustive` or
  `schema_vendors` raise the timeout to 300 seconds.
- **A `slow` matrix leaves a representative behind.** When a parametrized
  test is heavy because of depth, corpus size or the number of combinations,
  one small parameter set stays in tier 1 and the full matrix is `slow`. Moving
  a test to `slow` never reduces its universe or weakens its assertions.
- **`postgresql` means "requires PostgreSQL"; `pg_delta` means "runs
  everywhere, and PostgreSQL may disagree".** `pg_delta` holds tests that
  branch on the vendor, at least one test for each area that emits SQL
  (recursive scopes, index reads, maintenance, schema write owners,
  migrations), and every test that has ever failed on PostgreSQL while passing
  on SQLite. A PostgreSQL-only failure found in tier 3 adds that test to
  `pg_delta` in the same change that fixes it.
- **A vendor-specific test skips, it does not return.** A test that cannot run
  on the current vendor calls `pytest.skip`; an early `return` reports a pass
  for something that did not run.
- **Parallel scheduling is by test, not by file** (`--dist worksteal`). No
  test may rely on sharing a worker with its module.
- **Budgets are measured alone.** `scale` holds time, statement and query-plan
  budgets and never runs beside parallel workers.
- **Publishing does not wait for tier 3.** A tag publishes once tier 1 is
  green on it. Tier 3 is consulted before tagging; its nightly result on
  `main` is the release evidence.

Default `pytest` deselects `slow`, `index_exhaustive`, `schema_vendors` and
`scale`. `make test` runs the tier 1 selection serially, for debugging only.

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

### Permission index suites

The coverage matrix is the completion criterion; merely having a generator
does not cover it.

The scale suite runs alone: `make test-scale` and `make test-scale-postgres`.
The full reference sweep uses
`index_exhaustive` and its schedulable cases also carry `index_shard`.
`make test-index-reference` overrides the marker filter and runs every reference
shard, distributed by test rather than by file. `make test-postgres` runs
the whole suite, `slow` included, on PostgreSQL. `make test-schema-vendors`
opts into disposable PostgreSQL/MySQL contracts; MySQL 8 is covered only
there. All of these are tier 3. `make test-index` and
`make test-index-postgres` run the index modules with the default selection on
SQLite and on PostgreSQL; they are focused runs, not tiers. Drivers,
environment and release gates are in
[CONTRIBUTING.md](../CONTRIBUTING.md).

| Suite | Required cases and assertions |
|---|---|
| Pure reference gate | The opt-in `index_exhaustive` sweep exhausts every expression through four leaves over two relations, an arrow, builtins and a type-level constant; up to three users, two nested groups including a wildcard member, two resources, three instants, and no/partial/full caveat context. Expression cases use 32 shards per shape/universe/instant/context; direct-tuple powersets use 16. The default gate retains all named counterexamples, all one/two-leaf expressions and deterministic samples of larger trees. The reference computes each actor's set membership directly. |
| Differential semantics | Compare `rebac.index.read` directly with both the frozen walker and the pure reference. Depth 0–50; recursive schemas and data cycles; nested/wildcard groups; non-recursive intersection/subtraction; type-level constants with concrete bans; field paths (multi-hop, reverse, M2M, MTI, filtered), dynamic/fixed attributes and constants; expiring tuples/overrides and recaveats; caveats in every operator position with all three check states; missing sets equal the reference's canonical residual set and are a subset of the walker's; subject-set actors, anonymous and empty IDs; both relationship storage modes. |
| Enumeration and embedding | Scope rows equal definite per-row results; `Subquery`, `Exists`, `__in`, `Prefetch`, field gates and bulk guards remain scoped. Complete `lookup_subjects` agrees with the reference, with explicit assertions for D4 differences from the old incomplete enumeration. Preserve public context arguments, model-level and model-less reads. |
| Maintenance | Every owner/signal path; mixin/plain re-parenting, subtree deletion, watched-field update, bulk_update without duplicate passes, M2M add/remove/clear/set, collector SET_NULL/CASCADE, override create/delete/deadline, sync including unchanged-sync-after-migrate readiness, conditional deny memberships and aliases. Verify reports zero drift after each supported write. Unsupported writes drift and rebuild repairs them. Payload corruption (not just natural-key changes) is detected. |
| Transactions and concurrency | Deterministic PostgreSQL tests: concurrent revoke/link insertion, initial lock-row lifecycle, outer/savepoint rollback, maintenance failure after mutation, schema-read races. Source and index roll back together for owned transactions; D2's plain-model autocommit warning/error behavior is pinned separately. |
| Structural | For static plan size `k`, SQL length is at most `a + b·k` for fixed `a` and `b`; the same plan has identical SQL size at data depths 1 and 50. Verify held sites, lookup counts and nil lowering. Ordinary writes touch no unaffected rows; a write to a userset relation no node references changes zero grant rows, and no membership write changes a grant that holds the set. Fresh SQLite/PostgreSQL migrations install no REBAC trigger/function. No library raw SQL, triggers, database functions or undocumented ORM internals exist outside migration `0005`'s legacy uninstall routine. |
| Scale | Rebuild at resource scales 1, 2 and 4: statements, index rows and elapsed time grow at most linearly. With resources fixed and users multiplied by four, grant rows remain constant and statements rise by at most 1.5×. Enforce a rebuild statement budget and no per-batch savepoint. Record `python_rows`, rows per table and the five largest schema read plans. |
| PostgreSQL plans | `QuerySet.explain()` on fresh, unanalyzed tables shows index conditions for read lookups. |
| Vendors | SQLite and PostgreSQL CI; an opt-in MySQL 8 suite must pass before release, including streamed same-table writes, unsigned integer codec bounds, timezone/sentinel behavior and full payload comparisons. |

Data cycles that exceed the frozen walker's dispatch limit use the pure
reference's finite-path least fixpoint; ordinary acyclic cases must agree with
the walker. The harness must never compare production `check_access` with the
walker while production itself still delegates there.

Record rebuild time, statements, rows per table and `python_rows` at scales
1, 2 and 4, the fixed-resource users ×4 case, grant rows changed by a
membership write, and the five largest read plans with SQL lengths.
Measurements are results to collect, not assumed bounds.

### SpiceDB conformance suite (planned)

The 0.23.0 differential oracle compares the index with the library's own
walker, so it proves the index reproduces current behaviour, not that current
behaviour is right. Invariant 1 makes SpiceDB the contract, so the next step
tests against SpiceDB itself. **Planned for the release after 0.23.0; not part
of the 0.23.0 build.**

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
  tuples by a test-side projector that reads the same rows the index derives
  from; it is the prototype of the roadmap projector for `SpiceDBBackend`.
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
    at depths up to the server's limit.
- **Deliberate divergences** are listed here and nowhere else, and each has a
  test pinning our side:
  - data cycles (SpiceDB fails at its dispatch limit; the index returns the
    least fixpoint);
  - `rebac.E016`: LocalBackend 0.23.0 rejects `&` or `-` on any recursive
    dependency cycle, including self-loops. Monotone intersections and
    recursion through the left of subtraction are deferred to 0.24; negative
    cycles (recursion through the right of subtraction) stay forbidden
    permanently. Tests pin the rejection and the non-recursive wrapper pattern;
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
| **0.23.0 — permission index** | [Permission index](#permission-index--the-localbackend-read-path) ships as the single persisted `LocalBackend` read path: six internal tables of monotone sets and named sites, read-plan-bounded SQL, synchronous maintenance with a per-alias global lock, `rebac index rebuild`/`verify`, complete subject expansion, checks E013–E019, differential/reference suites, and PostgreSQL CI. |
| **After 0.23.0 — SpiceDB conformance suite** | [SpiceDB conformance suite](#spicedb-conformance-suite-planned): generated cases checked against a pinned `spicedb serve-testing` in dev (`-m spicedb`) and CI; test-side projector for backings; deliberate divergences listed and pinned. The in-process oracle then stops copying the walker. |
| **Next — `SpiceDBBackend`** | `authzed-py` adapter; `WriteSchema` auto-push; the conformance suite becomes its cross-backend contract tests; SpiceDB Zookie translation; the projector grows from the test-side prototype. |
| **1.0.0 — Stable release** | Full docs, CI matrix green, stable audit/logging contracts, `select_related` compiler hook (or carved to 1.1). |
| **1.x** | `select_related` SQL compiler; bulk operations; `Meta.protected_fields` (descriptor-based field gating / true `"raise"` mode complementing [`read__<field>`](#field-level-read-gates-readfield)); PostgreSQL RLS defense-in-depth track. |

---

## Open questions

1. **Relationship table partitioning at scale.** Above ~100M rows, index derivation and `rebac_grant` itself can slow even with the indexes shipped. Worth designing a `(resource_type)` LIST partition scheme? **Lean: yes, post-1.0**, document the threshold and shipped migration helper.

2. **Swappable User dependency.** `auth/user` is hardcoded as a subject type label. Projects with `AUTH_USER_MODEL` aliases (`accounts.User`) need... what? Lean: a `REBAC_USER_TYPE` setting (default `"auth/user"`), plus `to_subject_ref()` consults `settings.AUTH_USER_MODEL` to decide. Settle in 0.1.

3. **Async ORM support.** *Resolved (0.11.x).* No separate async manager API is needed. Django implements every async `QuerySet` method (`aget` / `acount` / `aexists` / `afirst` / `aupdate` / `adelete` / `acreate` / `__aiter__` / `ain_bulk` / `aget_or_create` / …) as a `sync_to_async` wrapper around the sync method `RebacQuerySet` already overrides, so scoping is inherited and the `current_actor()` ContextVar carries into the worker thread — `await Post.objects.as_user(u).aget(...)` enforces with no extra code. The two methods that compute *without* routing through the sync `iterator` / `_fetch_all` path are overridden to re-apply scope: `aiterator()` (builds the row iterable directly) and `aggregate()` / `aaggregate()` (summarises the query without materialising rows). `bulk_create()` and `abulk_create()` preflight every proposed row through `check_new()` and stamp the queryset actor onto inserted instances. Actor-scoped conflict updates (`update_conflicts=True`) fail closed because a create grant cannot authorize changes to existing rows; use checked individual saves or explicit sudo for those upserts.

4. **Override layer precedence vs caveats.** When a `SchemaOverride` tightens a permission AND a caveat returns `CONDITIONAL`, what wins? Lean: tightening wins (security-fail-closed). Documented as a doctor warning.

5. **MCP authentication standardisation.** Tracked in [proposal 0004](./proposals/0004-mcp-tool-integration.md). As of May 2026, `ctx.request_context.meta` is the de facto channel for actor identity. If MCP adds a typed identity field in 2026/2027, the plugin should adopt it without a major bump.

6. **Per-tenant override scope.** Today `SchemaOverride` is global (one row applies to all tenants). For SaaS, we'll need a `tenant_id` column. Lean: ship 1.0 without it (single-tenant), add `REBAC_TENANT_RESOLVER` callable in 1.x driven by real demand.

7. **Web admin for the override layer.** v1.0 ships a Django admin form. A standalone admin SPA (separate optional package, `django-zed-rebac-admin`) could be more usable. Defer — gather user feedback first.

8. **Multi-database relationship resolution.** Index tables, relationship rows,
scoped models and backing-path models must share the operation's database alias;
`rebac.E015` rejects cross-database routing that prevents transactional
maintenance. Querysets carry their alias into index reads; write owners use
their write alias and actual backend throughout maintenance. The public
`check_access()` / `accessible()` signatures remain alias-free and use backend
routing. Candidate projection uses the write alias. Arbitrary cross-database
authorization remains unsupported; explicit alias handling is not a distributed
transaction or permission join.


9. **Read cost across shared sub-permissions.** *Resolved (0.23.0)* by the [permission index](#permission-index--the-localbackend-read-path): scopes compile a schema-bounded read plan over monotone sets.

10. **Index maintenance lock granularity.** 0.23.0 uses one global `IndexState` lock per database alias, acquired before source reads. Per-type locks remain an open question after contention measurements and a proof covering schema writes, vacuum and old/new dependency discovery.

11. **Write fan-out budget.** Maintenance is synchronous and unbounded, and reports rows touched through the `rebac.index` log. A write that re-parents a large subtree rewrites every descendant's rows in its transaction. Lean: no budget until a deployment measures one; a budget must fail the write, never defer maintenance.

12. **Holder interning.** Resolved in 0.23.0: `IndexTerm` interns `(type, object_id, relation)`, and index rows carry foreign keys. Vacuum runs under the maintenance lock.

13. **Intersection and subtraction storage.** *Resolved (0.23.0):* named sites hold set operands by reference; reads evaluate them without materializing actors. E016 still refuses a site on a recursive cycle.

---

## Appendix — what `django-zed-rebac` is not

- **Not a User model.** Use `django.contrib.auth.models.User` or any swappable `AUTH_USER_MODEL`.
- **Not an authentication system.** Use `django-allauth`, `dj-rest-auth`, `simple-jwt`, `python-social-auth`, or your own.
- **Not a session manager.** Django's session middleware is fine.
- **Not a multi-tenant database router.** Use `django-tenants` or `django-organizations`. `django-zed-rebac` is orthogonal — REBAC works within whatever tenant scope the project provides. (You CAN use `REBAC_TYPE_PREFIX = "tenant_acme/"` for soft-tenant scoping if rows-per-tenant fit in one DB.)
- **Not a GraphQL admin layer.** A future `django-zed-rebac-admin` package may add one; v1 ships a Django admin form for `SchemaOverride` and a CLI for `Relationship` introspection. Higher-level frameworks may layer their own admin surfaces on top.
- **Not an audit-log system.** A future `django-zed-rebac-audit` package may add one; v1 ships `PermissionAuditEvent` and emits structured logs.
- **Not a policy DSL** like Polar or Cedar. The schema language is SpiceDB's `.zed`, REBAC-first. ABAC fragments are expressed via caveats.
