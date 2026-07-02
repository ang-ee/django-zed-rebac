# `django-zed-rebac` — Architecture

> Status: **alpha implementation guide** — reflects the 0.9.0 codebase.
> Last updated: 2026-05-30
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
  - `LocalBackend` — pure-Django evaluation over relationship rows. Zero infrastructure.
  - `SpiceDBBackend` — roadmap adapter for the official [`authzed`](https://pypi.org/project/authzed/) Python client. The class is present as a clear stub, not a supported runtime backend yet.
- **A `RebacMixin` model mixin** that, by inclusion, replaces `Manager.objects` with a permission-aware variant. Every read scopes to the effective user; every write checks before SQL is issued.
- **Three storage tiers, three editors:**
  - **Tier 1 — Structural.** Per-package `permissions.zed`, code-shipped, DB-loaded.
  - **Tier 2 — Override.** Admin-editable tweaks on top of the package baseline.
  - **Tier 3 — Relationship.** The actual edges in `Relationship` rows.
- **One unified check API:** `check_access(op)` / `has_access(op)` / `accessible(op)` (borrowed from Odoo 18's PR #179148 unification). No model-level vs record-level split at the call site.
- **`Model.objects.with_actor(actor)` / `instance.sudo(reason=...)`** — distinct verbs for distinct intents. The actor is any `SubjectRef` — a Django `User`, a registered `Agent`, an `agents/grant` (agent-acting-on-behalf-of-user), an `auth/apikey`, or any `@rebac_subject`-registered object. `as_user(u)` and `as_agent(agent, on_behalf_of=u)` are typed shorthands. Mandatory `reason` on bypass, originating uid preserved through bypass for audit (Odoo `env.su` / `env.user` independence).

Subject types named `auth/<x>` (`auth/user`, `auth/group`) are emitted by the plugin because they map onto `django.contrib.auth.User` / `Group`. Everything else — `auth/apikey`, `agents/agent`, `agents/grant`, custom service-account types, etc. — lives in the consumer's own apps (`auth/apikey` in your auth-extension app; `agents/agent` and `agents/grant` in an `agents` app you control). The plugin ships no `Agent` / `Grant` / `Service` schema fragment.
- **Strict-by-default**: a queryset that escapes its actor scope raises `MissingActorError` rather than silently returning all rows.
- **Designed-for-AI-agents**: the canonical Authzed *Grant* pattern is supported out of the box. The agent's effective permission on any resource is the structural intersection of (a) the user's grants, (b) the agent's declared capabilities — enforced by the schema graph, not by app-layer ANDs.

What `django-zed-rebac` deliberately does **not** ship: a `User` model, auth providers, login UI, session handling, GraphQL admin endpoints. Those are orthogonal — use `django.contrib.auth` (default) or any of `django-allauth` / `dj-rest-auth` / your own. Downstream frameworks may layer on top to provide polymorphic Subject types (`auth/apikey`, `agents/agent`, `agents/grant`, …), GraphQL admin surfaces, and Grant-pattern wiring; nothing here is coupled to any specific framework.

For schema authoring, see [ZED.md](./ZED.md).

---

## Quickstart

Three steps, ~15 lines total.

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
python manage.py migrate                   # creates Relationship + Schema* tables
python manage.py rebac sync            # loads permissions.zed into Schema* tables
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
| **Permission** | A computed expression over relations. Never stored, always evaluated. | `permission read = owner + viewer` |
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
│  Loader: applied at app-ready on top of Tier 1                  │
│  Editor: admins                                                 │
├─ Tier 3: RELATIONSHIPS ────────────────────────────────────────┤
│  Source: signals, sharing UIs, sharing APIs, Django FK fields    │
│  Store:  Relationship, plus field-backed structural relations    │
│  Loader: written transactionally; evaluated by CTE              │
│  Editor: application code + admins                              │
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

`LocalBackend` resolves direct checks, arrows, `accessible()`, and
`lookup_subjects()` from the Django column through the model's `_base_manager`
so application default managers cannot move the authorization boundary. Tuple
writes/deletes targeting the backed relation raise `SchemaError` with the
actionable Django field to update instead.

Only explicit forward FK/one-to-one bindings are supported in this tier. The
schema validator rejects field-backed relations with multiple subject types,
subject sets, wildcards, specific ids, caveats, or expiration; the `rebac.E009`
system check verifies that the Django field exists and points at the declared
resource type. A future `SpiceDBBackend` projector should use the same
metadata to materialize those structural relations into SpiceDB tuples.

### Const-backed (synthetic) relations

A relation may instead be annotated with `// rebac:const=<id>` to resolve to one
fixed object id for *every* row of the declaring type, with no stored tuple and
no model column:

```zed
definition blog/post {
    relation admin: angee/role // rebac:const=admin
    permission read = owner + admin->member
}
```

Every `blog/post` behaves as if it held `#admin @ angee/role:admin`, so
`admin->member` is "is the actor a member of `angee/role:admin`?" — answered from
that one role's membership rows, never a per-post grant. This is the schema-level
"static relationship" SpiceDB never shipped (issues #346 / #1266); it is the
idiomatic way to express GCP-IAM's "a role bound at a scope covers every resource
under it" without a container object. It shares the field-backing constraints
(exactly one concrete subject type; no subject sets, wildcards, ids, caveats, or
expiration), parses to `ConstBinding` on `Relation.backing`, persists as
`{"kind": "const", "target_id": ...}`, and `rebac.E009` requires the declaring
type to have a Django model (the const *target* need not — it is typically a
virtual role namespace).

Resolution is fixed-target rather than per-row, which has two consequences in
`LocalBackend`:

- **`accessible()` is whole-type, not enumerated.** Because the target object is
  the same for every row, `grants_all()` answers "does the subject hold `target`
  on `const:<id>`?" with a single check; when true the queryset layer adds *no*
  `id__in` filter (it would otherwise list the entire table). `lookup_subjects`
  and direct (`permission x = const_relation`) references resolve to the fixed
  target the same way.
- **SpiceDB projection is one tuple per source row.** Unlike field-backing's
  one-tuple-per-FK, a `SpiceDBBackend` projector must materialize the synthetic
  edge for every row of the source type (or model it as a `parent`/`platform`
  hierarchy), since SpiceDB has no static-relationship primitive. The local
  synthesis stays free of that cost.

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
│  │  local graph walk  │    │  planned authzed adapter │          │
│  │  on Relationship   │    │  roadmap implementation  │          │
│  │  + cel-python      │    │                          │          │
│  └────────────────────┘    └──────────────────────────┘          │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

**Three layers, one boundary.** The schema (`.zed` files → `Schema*` tables → in-memory expression tree) is the contract. Both backends honour it. Application code never imports a backend directly — it goes through the `Backend` ABC instance resolved from `REBAC_BACKEND` at app-ready.

**Where each integration hooks:**

| Surface | Hook | What it does |
|---|---|---|
| Django ORM | `RebacMixin` metaclass | Replaces `objects` with `RebacManager`; wires pre-save / pre-delete signals; installs `from_db` actor propagation. |
| DRF | `RebacPermission` (BasePermission) + `RebacFilterBackend` (BaseFilterBackend) | Per-action permission check on viewsets; queryset filter on list endpoints. |
| Celery | `before_task_publish` + `task_prerun` signals | Injects `actor_id` into task headers on enqueue; restores into ContextVar on worker. |
| MCP (FastMCP) | `rebac.mcp.rebac_mcp_tool` decorator | Resolves the actor (ambient `current_actor()`, then `REBAC_MCP_ACTOR_RESOLVER` reading `ctx.request_context.meta["actor_subject"]`), checks the target permission, then runs the body inside `actor_context`. Reads the context by duck-typing — no SDK import. See [proposal 0004](./proposals/0004-mcp-tool-integration.md). |
| GraphQL (strawberry) | `rebac.graphql.strawberry.RebacExtension` + `RebacChannelsConsumerMixin` | Opens evaluator/Zookie scopes per operation and per subscription emission. Use `require_permission` or actor-scoped querysets inside resolvers. |
| GraphQL (Strawberry-Django) | `rebac.graphql.strawberry_django.RebacDjangoOptimizerExtension` | Wraps Strawberry-Django's optimizer with REBAC-safe relation loading: guarded `select_related` for to-one paths and actor-scoped protected prefetches. |
| Plain Python | `@rebac_resource(type=..., id_attr=...)` | Registers the class as a known resource type for explicit `check_access()` calls. |

---

## Public API surface

```python
from rebac import (
    # Mixin and managers
    RebacMixin, RebacManager, RebacQuerySet,

    # Decorators
    require_permission, rebac_resource,

    # Backend interface
    Backend, LocalBackend, SpiceDBBackend,
    CheckResult, Consistency, Zookie,
    ObjectRef, SubjectRef, RelationshipTuple,

    # Errors
    PermissionDenied, MissingActorError, CaveatUnsupportedError,
    PermissionDepthExceeded, NoActorResolvedError,

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
from rebac.celery import propagate_actor
from rebac.mcp    import rebac_mcp_tool, default_actor_resolver, get_mcp_actor_resolver
from rebac.schema import parse_zed, validate_schema   # for tooling
from rebac.roles  import grant, revoke, roles_of, members_of   # role-as-namespace helpers
```

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

Schemas reference it two ways — both match the same subject at check time:

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

The bare keyword `anonymous` matches the canonical anonymous SubjectRef;
the bare keyword `authenticated` matches anything else with a real id.

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

Granting Alice the `storage/role:object_viewer` role then lights up
`read` on every `storage/file`, no per-file Relationship rows needed.

**Role hierarchy** is stock SpiceDB — three recipes, none of which require
engine changes:

| Recipe | When to use |
|---|---|
| **Type-union inclusion** | Fixed compile-time hierarchy. Add the narrower role's `:<id>#member` to the wider role's type union: `relation member: auth/user \| storage/role:object_admin#member`. The narrower-role members flow through to every role declaring this union entry. Best for universal-admin (`angee/role:admin#member`). |
| **Per-resource permission composition** | Per-resource viewer/editor/admin tiers. Each resource declares `permission read = viewer + editor + admin` so granting `object_admin` lights up read/write/delete automatically. Most explicit; grep-able. Default choice for CRUD-shape roles. |
| **Runtime-editable `includes` + `effective_member`** | Hierarchy editable at runtime without a schema PR. Roles declare `relation includes` + `permission effective_member = member + includes`; resources reference `#effective_member`. `rebac.roles.imply(parent=..., child=...)` writes the tuple. Adds one engine hop per check. |

**System / framework roles** (migrations, asset loaders) use
`rebac.actors.sudo` / `system_context` — they are not modelled as
roles. `rebac.roles` is exclusively for actor-grantable roles.

### Universal-admin convention

The "I'm in every role" tier is expressed as **a single role object plus
a type-union entry in every other `<namespace>/role` definition**:

```zed
// Ship once in your framework's meta-addon
definition angee/role {
    relation member: auth/user | auth/group#member
}

// Every other addon's role:
definition storage/role {
    relation member: auth/user
                   | auth/group#member
                   | angee/role:admin#member   // the universal-admin entry
}

definition knowledge/role {
    relation member: auth/user
                   | auth/group#member
                   | angee/role:admin#member
}
```

Granting `rebac.roles.grant(actor=alice, role="angee/role:admin")` then
makes alice a member of every opted-in role — `storage/role:object_viewer`,
`knowledge/role:vault_editor`, etc. — automatically, without per-role
plumbing. The `:admin#member` subject reference in the type union uses
the canonical SpiceDB `<type>:<id>#<relation>` syntax (supported by the
parser since v0.3.x).

The convention's name and role identity are configurable via
``REBAC_UNIVERSAL_ADMIN_ROLE`` (default ``"angee/role:admin"``). The
``rebac.W004`` system check warns when a ``<namespace>/role`` definition
is missing this entry from its ``member`` type union. Set
``REBAC_UNIVERSAL_ADMIN_ROLE = None`` to disable the check in
security-locked environments where the universal-admin tier is
unacceptable.

---

## Models

Six tables ship with the plugin. The first is the core REBAC store; the next four are the schema baseline + provenance; the last is the override layer.

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

**Frozen contract.** The shape mirrors `authzed.api.v1.Relationship` exactly. Renames are breaking. Indexes are critical (the local graph walk reads them on every check) and ship in the initial migration — never as a documentation step.

**Swappability.** Projects that need to extend the model (audit FKs, multi-tenant prefix, etc.) declare a custom subclass and point `REBAC_RELATIONSHIP_MODEL = "myapp.MyRelationship"`. The plugin uses [`swapper`](https://pypi.org/project/swapper/) to keep migrations correct across this swap. Default behaviour: `swapper` returns the built-in `rebac.Relationship`.

**`written_at_xid` (Zookie equivalent).** Populated on save:
- PostgreSQL: `txid_current()` via a default expression.
- MySQL: monotonic timestamp (microsecond precision).
- SQLite: package-global `time.monotonic_ns()` counter (test-mode only — not for production).

`Zookie` consistency tokens encode `f"{backend_kind}.{xid}"`. Tokens are **not portable** across backends; if a project flips `REBAC_BACKEND` from `local` to `spicedb`, persisted Zookies in caches must be drained.

**`expires_at`.** Mirrors SpiceDB's [`use expiration`](https://authzed.com/docs/spicedb/concepts/schema#use-expiration) feature (GA in v1.40+). Expired rows are evaluated as absent at check time; a periodic GC task (`rebac.gc.expire_relationships`) deletes them every 5 minutes by default.

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
translate string kwargs into FK-side lookups (`resource_fk__resource_type`,
etc.) at the QuerySet layer, so chained filters work without consumer code
changes.

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
  The local graph walk reuses these indexes heavily, so the gain compounds in
  `accessible()` evaluation.
- FK cascade: when a Django row backed by `RebacMixin` is deleted, the
  `post_delete` signal handler drops the matching `RebacResource` row,
  and the FK CASCADE on `RelationshipRegistry` sweeps every tuple that
  referenced it. Denormalized mode requires the caller to issue a
  follow-up `Relationship.objects.filter(...).delete()`.
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

Loaded from each app's `permissions.zed` at sync time. Read by `LocalBackend` at app-ready into an in-memory expression tree.

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

Composition rule (applied to permission expressions at app-ready):

```
effective_expr = (baseline_expr + extends) AND tightens
                                 minus disables
                                 with caveats merged from recaveats
```

Compiled once at app-ready into the in-memory expression tree; cached, invalidated on `SchemaOverride` writes via signal.

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

Written by every Tier 2 / Tier 3 mutation and every `sudo(reason=...)` invocation. Append-only.

---

## Settings catalog

All settings prefixed `REBAC_`. No nested dict. Read via the public `app_settings` object.

| Setting | Default | Type | Purpose |
|---|---|---|---|
| `REBAC_BACKEND` | `"local"` | `"local"` \| `"spicedb"` | Which backend to instantiate at app-ready. `"spicedb"` is reserved for the roadmap adapter and raises today. |
| `REBAC_RELATIONSHIP_MODEL` | `"rebac.Relationship"` | `str` | Swappable relationship model (Django convention). |
| `REBAC_LOCAL_BACKEND_STORAGE` | `"denormalized"` | `"denormalized"` \| `"registry"` | LocalBackend relationship storage shape. Registry mode is opt-in and uses `RelationshipRegistry` + `RebacResource`. |
| `REBAC_LOCAL_BACKEND_REGISTRY_BATCH_SIZE` | `5000` | `int` | Batch size for `python manage.py rebac migrate-storage`. |
| `REBAC_SPICEDB_ENDPOINT` | `None` | `str` \| `None` | Roadmap setting for the future `authzed.api.v1.Client`. Required once backend `spicedb` is implemented. |
| `REBAC_SPICEDB_TOKEN` | `None` | `str` \| `None` | Roadmap setting for the future SpiceDB preshared key. |
| `REBAC_SPICEDB_TLS` | `True` | `bool` | Roadmap setting for TLS behavior in the future SpiceDB adapter. |
| `REBAC_SPICEDB_AUTO_WRITE_SCHEMA` | `True` | `bool` | Roadmap setting for future schema auto-push. |
| `REBAC_SCHEMA_DIR` | `BASE_DIR / "rebac"` | `Path` \| `str` | Where `build-zed` writes `effective.zed`. |
| `REBAC_DEPTH_LIMIT` | `8` | `int` | Hard cap on recursive permission walks. Matches SpiceDB default. |
| `REBAC_DEFAULT_CONSISTENCY` | `"minimize_latency"` | `str` | Default `Consistency` for checks. |
| `REBAC_CACHE_ALIAS` | `"default"` | `str` | Django cache backend name for `accessible()` cache. |
| `REBAC_LOOKUP_CACHE_TTL` | `60` (s) | `int` | TTL for `accessible()` cache. Invalidated on relationship writes for the matching `(subject, action, resource_type)`. |
| `REBAC_PK_IN_THRESHOLD` | `10000` | `int` | Above this size, `accessible()` returns a JOIN instead of materialising `pk__in`. |
| `REBAC_STRICT_MODE` | `True` | `bool` | If `True`, queryset construction without an actor (and not in `sudo()`) raises `MissingActorError`. **Production default.** |
| `REBAC_REQUIRE_SUDO_REASON` | `True` | `bool` | If `True`, `sudo()` calls without a `reason=...` raise. |
| `REBAC_ALLOW_SUDO` | `True` | `bool` | Globally disable the request-path `sudo()` bypass. Strict tenants set `False`. **Does NOT gate `system_context()`** — framework-owned jobs (migrations, fixture seeders, asset loaders) must still be able to bypass even on strict tenants; the two surfaces are deliberately split. Every block-scoped `system_context()` entry still emits a `KIND_SUDO_BYPASS` audit row, same as block-scoped `sudo()`. |
| `REBAC_GC_INTERVAL_SECONDS` | `300` | `int` | How often the expiration GC task runs. |
| `REBAC_AUTHENTICATION_MIDDLEWARE` | `"django.contrib.auth.middleware.AuthenticationMiddleware"` | `str` | Middleware path that populates `request.user`. `rebac.middleware.ActorMiddleware` must appear after this path. Frameworks that replace Django's stock auth middleware set this to their canonical middleware. |
| `REBAC_ACTOR_RESOLVER` | `"rebac.actors.default_resolver"` | `str` | Dotted-path callable that resolves `request → SubjectRef`. Override for custom identity layers (e.g., agent grants). |
| `REBAC_MCP_ACTOR_RESOLVER` | `"rebac.mcp.default_actor_resolver"` | `str` | Dotted-path callable resolving an MCP request `Context → SubjectRef`, used by `rebac_mcp_tool` when no ambient `current_actor()` is set. The default reads the canonical `SubjectRef` string at `ctx.request_context.meta["actor_subject"]`. |
| `REBAC_TYPE_PREFIX` | `""` | `str` | Optional prefix for all generated resource types (multi-tenant SaaS). |
| `REBAC_SUPERUSER_BYPASS` | `True` | `bool` | If `True`, active superusers short-circuit `has_perm` AND run inside an `ActorMiddleware`-opened `sudo("superuser-bypass")` bracket so QuerySet scoping lifts too. Each elevated request emits a `KIND_SUDO_BYPASS` audit row. Suppressed when `REBAC_ALLOW_SUDO = False`. Strict tenants set this to `False`. |
| `REBAC_LINT_BARE_PREFETCH` | `True` | `bool` | Toggle for `rebac.W003` — the structural warning that an RBAC-bound model has an FK / O2O / M2M to another RBAC-bound model (a bare-string `select_related` / `prefetch_related` can load unguarded related rows). Enabled by default so the risky shape is visible; use `rebac_select_related()` / `rebac_prefetch_related()` or the Strawberry-Django optimizer for protected paths. |
| `REBAC_EVALUATOR_CACHE_SIZE` | `10000` | `int` | Max entries across the per-scope evaluator's `check_access` and `accessible` LRU caches. |
| `REBAC_ZOOKIE_TRANSPORT` | `"none"` | `"none"` \| `"header"` \| `"session"` | Optional cross-request transport for the current Zookie. |
| `REBAC_ZOOKIE_HEADER_NAME` | `"X-Rebac-Zookie"` | `str` | Header name used when `REBAC_ZOOKIE_TRANSPORT = "header"`. |
| `REBAC_ZOOKIE_SESSION_KEY` | `"_rebac_zookie"` | `str` | Session key used when `REBAC_ZOOKIE_TRANSPORT = "session"`. |
| `REBAC_FIELD_READ_MODE` | `"allow"` | `"allow"` \| `"redact"` \| `"omit"` \| `"raise"` | Deny behavior for schema permissions named `read__<field>`. `"raise"` currently degrades to `"redact"` and emits `rebac.W008` until descriptor-level protected fields land. |
| `REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL` | `True` | `bool` | Bulk field redaction has no per-row caveat context; `True` treats conditional `read__<field>` results as denied. Set `False` only when conditional visibility is acceptable without context. |

Validation runs in the system-checks framework at every `manage.py` invocation. Missing required keys for the chosen backend raise `Error` with check ID `rebac.E001`. Wrong types raise `rebac.E002`. Production-only checks (`--deploy`) include `rebac.W101` for `SPICEDB_TLS = False`.

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
        from . import signals    # noqa: F401  — connects pre/post save handlers
        from . import checks     # noqa: F401  — registers system checks
```

**No queries. No model instantiation. No backend resolution at import time.** The backend singleton is constructed lazily on first access via `rebac.backend()` — this avoids `AppRegistryNotReady` and keeps `migrate` fast.

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
| `rebac.W001` | Warning | `rebac.backends.RebacBackend` not in `AUTHENTICATION_BACKENDS`. |
| `rebac.W002` | Warning | A model with `Meta.rebac_resource_type` is missing `RebacMixin`. |
| `rebac.W003` | Warning | An RBAC-bound relation exists where bare `select_related("rel")` / `prefetch_related("rel")` can be unsafe outside the REBAC helpers or Strawberry-Django optimizer. |
| `rebac.W004` | Warning | Universal-admin role convention lint for role definitions. |
| `rebac.W005` | Warning | LocalBackend is still on denormalized storage and registry migration is recommended for large tables. |
| `rebac.W006` | Warning | `REBAC_ZOOKIE_TRANSPORT = "session"` without `django.contrib.sessions`. |
| `rebac.W008` | Warning | `REBAC_FIELD_READ_MODE = "raise"` currently degrades to `"redact"` until descriptor-based protected fields land. |
| `rebac.W101` | Warning (`--deploy`) | `REBAC_SPICEDB_TLS = False` in production. |

Users silence individual checks via Django's `SILENCED_SYSTEM_CHECKS = ["rebac.W001"]`.

---

## Authorization backend

```python
# rebac/backends.py
class RebacBackend:
    """
    Django auth backend. Routes per-object has_perm() through the REBAC engine.
    Does not authenticate (returns None from authenticate()).
    """

    def authenticate(self, request, **credentials):
        return None  # let downstream backends authenticate

    def has_perm(self, user_obj, perm: str, obj=None) -> bool:
        if not getattr(user_obj, "is_active", False):
            return False
        if obj is None:
            return False  # let ModelBackend handle model-level checks
        from . import backend
        from .actors import to_subject_ref
        from .resources import to_object_ref

        rebac_action = _codename_to_action(perm)         # "blog.view_post" → "read"
        if rebac_action is None:
            return False

        return backend().has_access(
            subject = to_subject_ref(user_obj),
            action  = rebac_action,
            resource = to_object_ref(obj),
        )

    def has_module_perms(self, user_obj, app_label):
        return False
```

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
2. `ActorMiddleware` opens a `sudo(reason="superuser-bypass")` bracket for the request lifetime, so `Model.objects.with_actor(superuser).filter(...)` returns every row instead of being narrowed by `accessible()`. This matches the legacy contrib.auth contract that admin sees everything *at the QuerySet layer*, not just at the `has_perm` layer — without it, admin changelist queries would silently filter to "rows the superuser has an explicit relationship to", which is almost never what's wanted.

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
           Works on empty references (model-level only) — pass an empty resource_id."""

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
        permission expressions before any row exists; ``lookup_subjects``
        reverse walks will also lean on it once they grow past direct-
        relation rows. LocalBackend serves the in-memory composed schema;
        the future SpiceDB adapter should cache the parsed result of
        ``Client.ReadSchema()``."""
```

`CheckResult` is `(allowed: bool, conditional_on: list[str], reason: str | None)`. The `conditional_on` field lists caveat parameter names whose context wasn't supplied — the caller may retry.

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
``check_access``, so all post-hop evaluation reuses the canonical
semantics — caveat-conditional outcomes propagate as
``CONDITIONAL_PERMISSION`` with the union of missing caveat
parameters. The dispatch (operator precedence, sub-permission cycle
detection, ``anonymous`` / ``authenticated`` built-ins, tri-state
combinators, ``REBAC_DEPTH_LIMIT``) reuses the shared walker that
backs ``LocalBackend._eval_permission``.

Const-backed relations are injected into the virtual overlay from the schema.
If a new `blog/post` declares `relation admin: angee/role //
rebac:const=admin`, `check_new()` behaves as if the proposed object carried
`#admin @ angee/role:admin`, then evaluates `admin->member` through the real
backend store. Callers must not supply virtual tuples for const-backed
relations; those are synthetic schema facts, so `check_new()` raises
`SchemaError` for non-empty caller entries on a const-backed relation name.

**Deliberately outside the ``Backend`` ABC.** ``check_new`` is a free
function, not a backend RPC, because SpiceDB ships no "check with
proposed tuples" call. A SpiceDB-mode strategy when 0.5 lands is to
``WriteRelationships`` the proposed tuples in a sub-transaction,
``CheckPermission`` against the (now-real) row, then roll back. Until
then, ``check_new`` raises a clear ``RuntimeError`` if the active
backend's ``schema()`` is not implemented.

Limitations (0.4):

* Caveats on the **top-level virtual tuples** are not supported — the
  ``relationships`` overlay is a bare ``SubjectRef`` sequence with no
  caveat context. Caveat-conditional ``create`` permissions still
  resolve correctly for the *post-hop* targets (the real rows
  ``check_access`` walks into).
* Subject-set candidates (``auth/group:eng#member``) inside a virtual
  relation list are resolved through the backend on the real group row
  — that subject-set walk costs one dispatch level.

---

### Schema introspection

Tooling that needs to answer "which relation or role reaches this permission?"
should read the effective schema through `backend().schema()` and use
`rebac.schema.introspection`, not walk AST node classes directly. The stable
helper surface is `permission_sources(schema, resource_type, permission)`,
`relation_dependencies(...)`, and `permissions_reaching_relation(...)`.
`PermissionSources` reports direct relations, arrows as `(via_relation,
target_permission)` pairs, built-in actor terms, and traversed same-definition
sub-permissions. The AST node types remain private implementation details so
the schema language can grow without downstream walkers silently misreading new
nodes.

Role convention tooling should use `rebac.roles.roles_reaching(...)`, passing a
`role_resource_type` such as `"storage/role"` or `"angee/role"` rather than
assuming a single namespace.

---

## `RebacMixin` — model-layer enforcement

The headline feature. By inclusion, every model operation is gated against the effective user.

### What gets installed

1. `objects = RebacManager.from_queryset(RebacQuerySet)()` replaces the default manager.
2. `_default_manager` points at it; `_base_manager` is intentionally **left unfiltered** (Django uses `_base_manager` for its own internal lookups — FK reverse caching, M2M intermediate tables — and these break if filtering is applied there).
3. Pre-save signal handler — `write` permission check before INSERT/UPDATE.
4. Pre-delete signal handler — `delete` permission check.
5. `from_db()` override — every loaded instance carries the actor that scoped its queryset.
6. `Meta` extension — the metaclass reads `rebac_resource_type` and registers the model with the type registry.

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
| `as_agent(agent, on_behalf_of=u)` | Equivalent to `with_actor(grant_subject_ref(agent, u))` — resolves to an `agents/grant:<id>#valid` subject. | Agent runtimes and MCP servers where a Grant is the canonical actor. |
| `with_action(action)` | Pins the permission used for read-side queryset scoping instead of `read` / `Meta.rebac_default_action`. | Alternate read views such as `credential_lookup`, `list_admin`, or capability-specific resolver scopes. |
| `on_field_deny(mode)` | Pins the field-read deny mode for `read__<field>` gates instead of the global setting. | Projection-sensitive paths that want `"omit"` while the global default stays `"allow"` or `"redact"`. |
| `for_write()` | `on_field_deny("allow")` named for intent: keeps actor row scope but turns off `read__<field>` redaction so a load-then-mutate target carries every column. | Resolving an update/delete target while `REBAC_FIELD_READ_MODE` is `"redact"`/`"omit"`, where a redacted column must not be hidden from the row being written. |
| `rebac_select_related(*fields)` | Applies Django `select_related` and batch-checks selected REBAC-bound related rows before serialization. | To-one relation optimization when an unreadable related object should fail the field/query instead of leaking. |
| `rebac_prefetch_related(*lookups)` | Applies Django `prefetch_related`, rewriting bare protected lookups to `Prefetch(queryset=Related.objects.with_actor(actor))`. | Reverse, M2M, and to-many loading where protected children should be scoped rather than loaded via `_base_manager`. |

`as_agent(agent)` without `on_behalf_of` resolves to a bare `agents/agent:<id>` subject (the agent acting standalone, with only its declared capabilities — no user grants). Use this only for system-initiated agent runs; for end-user-driven agent runs always pass `on_behalf_of=user`. The `agents/agent` and `agents/grant` definitions are NOT auto-emitted — they live in the consumer's own `agents` app, which references this plugin's `auth/user`.

`rebac_select_related()` preserves Django's to-one join optimization, then
checks every selected REBAC-bound related object in batches. If the actor cannot
read one of those joined rows, the queryset raises `PermissionDenied` before the
object can serialize. Related-field projections such as
`.values("folder__name")` fail closed because they bypass model-instance
materialisation and cannot carry the related-object guard. `rebac_prefetch_related()`
is the to-many counterpart: it keeps Django prefetching, but protected bare
lookups are rewritten to actor-scoped `Prefetch` querysets using the related
model's default manager, never `_base_manager`.

### `with_actor` vs `sudo` — distinct verbs

[Borrowed from Odoo's `with_user` / `sudo` distinction. Adapted with mandatory `reason` and a generic actor type.]

- `with_actor(actor)` — re-evaluate all checks **as** `actor`. The originating actor (`current_actor()`) is unchanged — `with_actor()` does NOT mutate the ContextVar; the new scope lives on the queryset clone. Audit events record both the originating actor and the queryset's pinned actor. Mirrors Odoo's `with_user(u)`, generalised to any subject type.
- `sudo(reason=...)` — request-path bypass of all REBAC checks. `current_actor()` still returns the originating subject; only `is_sudo()` flips. Mirrors Odoo's `env.su` / `env.user` independence. Mandatory `reason`. The block-scoped context manager writes a `PermissionAuditEvent` with kind `sudo.bypass`. **Gated by `REBAC_ALLOW_SUDO`** — strict tenants disable it.
- `system_context(reason=...)` — same bypass semantics as `sudo()` (same block-scoped audit kind, same reason requirement, same non-propagation through traversal), but intended for framework-owned jobs running outside a request: migrations, fixture seeders, asset loaders, scheduled maintenance. **Not gated by `REBAC_ALLOW_SUDO`** — a tenant who has disabled request-path sudo still needs to run migrations. Choose `sudo()` for request-path elevation (admin views, override layer); choose `system_context()` for framework jobs.

What `sudo()` does NOT bypass:
- App-layer `clean()` validators.
- Application code's explicit `if user.is_staff:` checks.
- Signals attached to `pre_save` / `post_save` that aren't part of the REBAC pipeline.
- `@require_permission` decorators that resolve their own actor.

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
4. **Implicit from `current_actor()`**, the contextvar populated by middleware (see [§ Middleware](#middleware)) and by Celery prerun hooks.
5. **Falls through to** `REBAC_STRICT_MODE` handling: `True` → raise for gates/materialisation; `False` → full visibility.

A pinned actor (path 2) **always wins** over ambient state (paths 3-4) — there is no path by which ambient sudo or the ambient actor ContextVar can override an explicit `.with_actor(...)`. This is the inverse of Odoo's `allowed_company_ids` ambient-scope precedence; we want the explicit local scope to be the authoritative one. If code truly wants bypass inside an elevated block, it must call `.sudo(reason=...)` on that queryset or instance.

**Critical: scope sticks across writes.** A queryset created with `with_actor(actor)` produces instances tagged with that actor; `instance.save()` re-checks against the same actor, regardless of what `current_actor()` says now. This is the Odoo `with_user(...)` invariant translated into Django — the actor follows the recordset.

### CRUD enforcement matrix

| Operation | Permission checked | Where |
|---|---|---|
| `Model.objects.all()` / `.filter(...)` / `.get()` / `.count()` / `.exists()` | `read` (or `Meta.rebac_default_action`; override per chain with `.with_action(action)`) | `RebacQuerySet.get_queryset()` injects a `pk__in=<accessible(actor, action, type)>` clause (or a JOIN above the threshold). |
| `Model.objects.create(**fields)` | `create` on the model class | `RebacManager.create()` calls `check_access(actor, "create", ObjectRef(type, ""))` first. |
| `Model.objects.bulk_create(rows)` | `create` once per page | Single class-level check. |
| `instance.save()` (PK present) | `write` on the row | Pre-save signal handler. |
| `instance.save()` (new instance) | `create` on the model class | Pre-save handler dispatches based on `_state.adding`. |
| `instance.delete()` | `delete` on the row | Pre-delete signal handler. |
| `Model.objects.update(**kwargs)` | `write` on each affected row | Manager intersects the queryset PK set with `accessible(actor, "write", type)`; raises if any in-scope row is excluded. |
| `Model.objects.delete()` | `delete` on each row | Same pattern. |

**Failure mode for writes:** *all-or-nothing*. Any denied row in a bulk write raises and rolls back. **Failure mode for reads:** denied rows are absent from the queryset; no raise. List endpoints return `[]` rather than 403 when the user has no rows.

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
redacted field via `save(update_fields=[...])`, the pre-save path raises
`PermissionDenied`. A full `save()` on a redacted instance rewrites
`update_fields` to exclude redacted fields, preventing a display-time `None`
from overwriting the stored value.

When the goal is to load a row *in order to mutate it* (resolve an
update/delete target, then write a different column), redaction would hide
columns the writer legitimately needs. `.for_write()` is the named shorthand
for `.on_field_deny("allow")` that turns field-read redaction off for that
queryset while leaving actor row scope intact — the row must still be one the
actor may access, but every column is materialised. It does not grant any
write permission; the pre-save / pre-delete checks still run.

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
LRU eviction across BOTH check and accessible caches. Conditional results
(`CONDITIONAL_PERMISSION(missing=[...])`) are NOT cached — the missing caveat
params are part of the answer and the next call may supply them. Per-call
explicit `consistency` / `at_zookie` also bypass the cache.

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
    # `written_at_xid <= cutoff` filters every Relationship read in
    # the evaluation walk. The same public API is reserved for the
    # planned SpiceDB adapter.
    accessible(subject=u, action="read", resource_type="blog/post")
```

LocalBackend's witness is the existing `Relationship.written_at_xid` column;
`Zookie.token = str(<xid>)`. Backends validate `Zookie.backend` matches their
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

`RebacExtension` opens fresh evaluator + Zookie scopes per GraphQL
**operation** — and for subscriptions that means **per emission**, not
per connection. A long-lived WebSocket subscription that started 2 hours
ago doesn't serve cached pre-revocation grants on the next tick.

The extension also mirrors `current_evaluator()` and `current_zookie()`
onto `info.context.rebac_evaluator` / `.rebac_zookie` for resolvers that
prefer explicit DI over the ambient ContextVar. Mirror is best-effort —
read-only context types silently skip without crashing.

For WS subscriptions, compose `RebacChannelsConsumerMixin` with whichever
consumer base your stack uses:

```python
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from strawberry.channels import GraphQLWSConsumer
from rebac.graphql.strawberry import RebacChannelsConsumerMixin

class GraphQLConsumer(RebacChannelsConsumerMixin, GraphQLWSConsumer):
    pass
```

Subscription invariants:
- **Actor**: connection-scoped (resolved at handshake from `scope["user"]`).
- **Evaluator**: per-emission. Revoked grants take effect at next tick.
- **Zookie**: per-emission. Naturally aligns with the write-triggered nature
  of subscriptions — the change that triggered the emission carries its
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

Default action map: `list/retrieve→read`, `create→create`, `update/partial_update→write`, `destroy→delete`. Per-viewset overrides via `rebac_action_map`.

drf-spectacular OpenAPI emission: optional `rebac.drf.spectacular` integration adds a security requirement to operations that include `RebacPermission`. Activated automatically if `drf_spectacular` is installed.

### Celery

`rebac.celery` connects `before_task_publish` (producer-side) and `task_prerun` (worker-side) signals. Wired automatically by `RebacConfig.ready()` — no per-project setup. Inside a `@shared_task`, the manager picks up the actor from the contextvar that `task_prerun` set:

```python
@shared_task
def email_user_their_drafts(user_id: int):
    # current_actor() is already populated from task headers
    drafts = Post.objects.filter(status="draft")   # scoped automatically
    send_email(drafts)
```

**Eager-mode caveat.** `before_task_publish` does NOT fire when `CELERY_TASK_ALWAYS_EAGER = True`. The plugin handles this by falling back to `task_prerun` — which DOES fire in eager mode — reading `current_actor()` directly.

### MCP

```python
from mcp.server.fastmcp import Context, CurrentContext
from rebac.mcp import rebac_mcp_tool

@mcp.tool
@rebac_mcp_tool(resource_type="blog/post", action="write", id_arg="post_id")
async def edit_post(post_id: str, body: str, ctx: Context = CurrentContext()) -> dict:
    ...
```

`rebac_mcp_tool` resolves the actor (ambient `current_actor()` first, then the
`REBAC_MCP_ACTOR_RESOLVER` callable — by default
`ctx.request_context.meta["actor_subject"]`, a canonical `SubjectRef` string),
checks the permission, and only then runs the body inside `actor_context`. No
actor resolved → `PermissionDenied` (fail closed). `action="create"` routes
through `check_new` for not-yet-persisted resources. The MCP server remains
responsible for minting and validating identity — the decorator only resolves
and authorises an actor an upstream boundary already established. Implemented
per [proposal 0004](./proposals/0004-mcp-tool-integration.md).

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
| `with_actor_queryset(qs, actor)` | `from rebac.querysets import with_actor_queryset` | Apply scoping ad-hoc to any queryset. |
| `check_access(subject, action, resource)` | `from rebac import backend; backend().check_access(...)` | Imperative checks anywhere. |
| `@require_permission` decorator | `from rebac import require_permission` | Gate methods on plain Python classes (not just models). |
| `current_actor()` ContextVar | `from rebac import current_actor` | Read the active actor inside any code path. |
| `with actor_context(actor):` | `from rebac import actor_context` | Block-scoped actor for non-queryset code (manual `check_access` calls inside the block). |
| `with sudo(reason=...)` | `from rebac import sudo` | Block-scoped bypass for request-path elevation; logged. Gated by `REBAC_ALLOW_SUDO`. |
| `with system_context(reason=...)` | `from rebac import system_context` | Block-scoped bypass for framework-owned jobs (migrations, fixture seeders, cron, asset loaders). Logged. **Not** gated by `REBAC_ALLOW_SUDO` — strict tenants that have turned `sudo()` off still need this path. |
| `parse_zed(text)` | `from rebac.schema import parse_zed` | Tooling: round-trip `permissions.zed` to AST. |
| `BackendPermission` checker | `from rebac.backends import to_subject_ref` | Identity-to-subject conversion (extension point). |

---

## Management commands

Single namespace `zed-rebac` with subcommands. Two destructive flags, both explicit, neither implicit.

```bash
python manage.py rebac sync                       # idempotent; respects no_update
python manage.py rebac sync --check               # CI gate; no writes; non-zero on drift
python manage.py rebac sync --force-overwrite     # destructive; bypasses no_update
                                                       # requires --yes for non-interactive
python manage.py rebac sync --force-overwrite --package=blog
python manage.py rebac sync --force-overwrite --target=blog/post.read

python manage.py rebac check                      # doctor: validate without writes
python manage.py rebac build-zed                  # emit effective.zed for SpiceDB
python manage.py rebac build-zed --check          # CI gate for the build artifact
python manage.py rebac write-schema               # push current schema to SpiceDB
python manage.py rebac gc-expired                 # one-shot expiration GC
python manage.py rebac explain blog/post.read     # print compiled expression
```

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

CI determinism test: run `rebac build-zed` twice in a tmpdir, byte-diff. Failure on any difference. Mirrors `manage.py makemigrations --check`.

---

## Migration safety

| Risk | Mitigation |
|---|---|
| Initial install creates a `Relationship` table with billions of rows expected | Indexes shipped in `0001_initial.py`. Migration is idempotent. |
| Project running `--backwards` to before `rebac` was installed | Every `RunSQL` operation has `reverse_sql`. The full schema is reversible. |
| Swappable `Relationship` model adopted post-install | `swapper.dependency()` already wired in shipped migrations. New custom model gets a fresh migration that the project author writes. |
| Adding `expires_at` later (back-port to existing relationships) | `expires_at` is nullable; existing rows get `NULL`. No data migration needed. |
| Multi-tenant prefix added later (`REBAC_TYPE_PREFIX`) | Changing the prefix requires `manage.py rebac retype-relationships --from=... --to=...`. Documented prominently. |
| Package upgrade silently overwrites admin schema edits | `no_update=True` on `PackageManagedRecord`. Conflict surfaced as warning + audit event. Force-overwrite is explicit. |

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

**Our behaviour:** the only ambient lever is `current_actor()`, populated by `ActorMiddleware` for HTTP and Celery prerun hooks for tasks. It is read-only at the call site (mutate via `set_current_actor()` only at framework boundaries — middleware, Celery handlers). Per-queryset `.with_actor(actor)` always wins; there is no path by which an ambient context override can mutate an explicit local scope.

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
2. **Integration tests** (`pytest-django`, `@pytest.mark.django_db`): in-memory SQLite + real Postgres. `RebacMixin` end-to-end, manager scoping, signal handlers.
3. **Future cross-backend contract tests**: once `SpiceDBBackend` lands, run the same suite against `LocalBackend` and SpiceDB (for example via [`testcontainers-spicedb`](https://pypi.org/project/testcontainers-spicedb/)).

Current GitHub CI gates `ruff` and `pytest` on Python 3.14 + Django 6.0. The
target compatibility matrix is:

```
Python:  3.14
Django:  6.0
DB:      sqlite (unit) · postgres-15 (integration) · postgres-16 (integration)
Backend: local
```

The package ships `py.typed` (PEP 561) and keeps the public API annotated. Full
strict type-checking remains a release-hardening target; the current CI gate is
lint + runtime tests.

---

## Versioning

`django-zed-rebac` follows SemVer while the project is below 1.0. Minor releases
may add public API and tighten alpha contracts; patch releases are reserved for
compatible fixes.

LTS support for older Django lines was dropped before 0.7.0. The package
currently targets Django 6.0+ and Python 3.14+.

Public API (`rebac.*` direct imports + the schema language) is intended to be
stable across patch releases. `rebac._internal.*` is private.

---

## Roadmap

| Phase | Deliverable |
|---|---|
| **0.1.0 — MVP** | `LocalBackend`; schema parser + sync command; `RebacMixin` + manager + signals; `RebacPermission` + `RebacFilterBackend`; system checks; sync/check commands; first test matrix. |
| **0.2.0 — Alpha hardening** | Schema-level built-in actor grants; action-scoped read querysets; split request-path `sudo()` from framework-job `system_context()`; hot-path schema cache invalidation. |
| **0.3.0-0.9.0 — shipped alpha core** | `ActorMiddleware`; Celery signal handlers; registry storage mode; evaluator/Zookie scopes; Strawberry adapter; field-level read gates; REBAC-safe relation loading; Strawberry-Django optimizer; field-backed structural relations; LocalBackend hardening. |
| **0.11.0 — MCP adapter** | `rebac.mcp.rebac_mcp_tool` decorator for FastMCP; actor resolution from request metadata (`REBAC_MCP_ACTOR_RESOLVER`); capability/resource gating; create-shaped actions via `create_relations`; sync, async, and streaming (async-generator) tool bodies. See [proposal 0004](./proposals/0004-mcp-tool-integration.md). |
| **0.11.x — async ORM scoping** | Verified the async ORM surface inherits scoping via Django's `sync_to_async` wrappers; closed the two bypasses (`aiterator()`, `aggregate()`/`aaggregate()`) that summarised/streamed rows outside the actor's scope. See Open questions § 3. |
| **Next — `SpiceDBBackend`** | `authzed-py` adapter; `WriteSchema` auto-push; cross-backend contract tests; SpiceDB Zookie translation. |
| **1.0.0 — Stable release** | Full docs, CI matrix green, stable audit/logging contracts, `select_related` compiler hook (or carved to 1.1). |
| **1.x** | `select_related` SQL compiler; bulk operations; `Meta.protected_fields` (descriptor-based field gating / true `"raise"` mode complementing [`read__<field>`](#field-level-read-gates-readfield)); PostgreSQL RLS defense-in-depth track. |

---

## Open questions

1. **Relationship table partitioning at scale.** Above ~100M rows, the local graph walk can slow even with the indexes shipped. Worth designing a `(resource_type)` LIST partition scheme? **Lean: yes, post-1.0**, document the threshold and shipped migration helper.

2. **Swappable User dependency.** `auth/user` is hardcoded as a subject type label. Projects with `AUTH_USER_MODEL` aliases (`accounts.User`) need... what? Lean: a `REBAC_USER_TYPE` setting (default `"auth/user"`), plus `to_subject_ref()` consults `settings.AUTH_USER_MODEL` to decide. Settle in 0.1.

3. **Async ORM support.** *Resolved (0.11.x).* No separate async manager API is needed. Django implements every async `QuerySet` method (`aget` / `acount` / `aexists` / `afirst` / `aupdate` / `adelete` / `acreate` / `__aiter__` / `ain_bulk` / `aget_or_create` / …) as a `sync_to_async` wrapper around the sync method `RebacQuerySet` already overrides, so scoping is inherited and the `current_actor()` ContextVar carries into the worker thread — `await Post.objects.as_user(u).aget(...)` enforces with no extra code. The two methods that compute *without* routing through the sync `iterator` / `_fetch_all` path are overridden to re-apply scope: `aiterator()` (builds the row iterable directly) and `aggregate()` / `aaggregate()` (summarises the query without materialising rows). Caveat: `bulk_create` skips signals in both sync and async (the standard Django limitation) — write enforcement requires per-row `save()`.

4. **Override layer precedence vs caveats.** When a `SchemaOverride` tightens a permission AND a caveat returns `CONDITIONAL`, what wins? Lean: tightening wins (security-fail-closed). Documented as a doctor warning.

5. **MCP authentication standardisation.** Tracked in [proposal 0004](./proposals/0004-mcp-tool-integration.md). As of May 2026, `ctx.request_context.meta` is the de facto channel for actor identity. If MCP adds a typed identity field in 2026/2027, the plugin should adopt it without a major bump.

6. **Per-tenant override scope.** Today `SchemaOverride` is global (one row applies to all tenants). For SaaS, we'll need a `tenant_id` column. Lean: ship 1.0 without it (single-tenant), add `REBAC_TENANT_RESOLVER` callable in 1.x driven by real demand.

7. **Web admin for the override layer.** v1.0 ships a Django admin form. A standalone admin SPA (separate optional package, `django-zed-rebac-admin`) could be more usable. Defer — gather user feedback first.

---

## Appendix — what `django-zed-rebac` is not

- **Not a User model.** Use `django.contrib.auth.models.User` or any swappable `AUTH_USER_MODEL`.
- **Not an authentication system.** Use `django-allauth`, `dj-rest-auth`, `simple-jwt`, `python-social-auth`, or your own.
- **Not a session manager.** Django's session middleware is fine.
- **Not a multi-tenant database router.** Use `django-tenants` or `django-organizations`. `django-zed-rebac` is orthogonal — REBAC works within whatever tenant scope the project provides. (You CAN use `REBAC_TYPE_PREFIX = "tenant_acme/"` for soft-tenant scoping if rows-per-tenant fit in one DB.)
- **Not a GraphQL admin layer.** A future `django-zed-rebac-admin` package may add one; v1 ships a Django admin form for `SchemaOverride` and a CLI for `Relationship` introspection. Higher-level frameworks may layer their own admin surfaces on top.
- **Not an audit-log system.** A future `django-zed-rebac-audit` package may add one; v1 ships `PermissionAuditEvent` and emits structured logs.
- **Not a policy DSL** like Polar or Cedar. The schema language is SpiceDB's `.zed`, REBAC-first. ABAC fragments are expressed via caveats.
