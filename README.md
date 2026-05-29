# django-zed-rebac

> SpiceDB-compatible REBAC for any Django project. Drop in, declare your permission schema in a per-app `permissions.zed` file, and every queryset, save, and method call is gated against the effective user.

---

> **Status: alpha.** The package is published on PyPI and the core local REBAC path is usable: `LocalBackend`, `RebacMixin`/manager, schema parser, `rebac sync`, caveats, expirations, schema overrides, audit events, middleware, system checks, DRF helpers, Celery actor propagation, and the Strawberry GraphQL adapter. `SpiceDBBackend` and MCP integration are roadmap work. Track the milestones at [docs/ARCHITECTURE.md § Roadmap](./docs/ARCHITECTURE.md#roadmap).

---

## What it is

`django-zed-rebac` ports [SpiceDB](https://github.com/authzed/spicedb)'s relation-based access control model into Django. You author your permission schema as a SpiceDB-native `.zed` file alongside your app; the plugin loads it into DB tables on install with `noupdate=True` semantics that preserve admin edits, and admins tweak overrides through the admin UI (or your own GraphQL layer).

The backend interface is designed around one Python API:

- **`LocalBackend`** — pure Django, evaluates permissions against local relationship rows. Zero external infrastructure. Suitable up to moderate graph sizes and depth <= 8.
- **`SpiceDBBackend`** — planned adapter for the official [`authzed`](https://pypi.org/project/authzed/) Python client. The class exists today as a clear stub, but `REBAC_BACKEND = "spicedb"` is not a supported runtime path yet.

Add the mixin to your model and `Post.objects.all()` returns only what the user can read. Add `Model.objects.with_actor(actor)` for explicit actor scoping in Celery tasks, GraphQL resolvers, management commands, and other non-HTTP entrypoints — `actor` can be a Django `User`, a registered `Agent`, an `agents/grant` (agent-acting-on-behalf-of-user, shipped by your `agents` app), or anything `@rebac_subject`-registered. Typed shorthands `as_user(user)` and `as_agent(agent, on_behalf_of=user)` cover the common cases. The plugin itself only ships `auth/user` and `auth/group` schema (mapped onto `django.contrib.auth`); `agents/agent`, `agents/grant`, `auth/apikey`, and other subject types live in your own apps.

## Quickstart

```python
# settings.py
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    # ...
    "rebac",
    "blog",
]

AUTHENTICATION_BACKENDS = [
    "rebac.backends.RebacBackend",
    "django.contrib.auth.backends.ModelBackend",
]

REBAC_BACKEND = "local"
```

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

```python
# blog/apps.py
class BlogConfig(AppConfig):
    name       = "blog"
    rebac_schema = "permissions.zed"   # relative to the app's package dir

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

```bash
python manage.py migrate                  # creates Relationship + Schema* tables
python manage.py rebac sync           # loads permissions.zed into Schema* tables
```

```python
# blog/views.py
def post_detail(request, pk):
    post = get_object_or_404(Post.objects.with_actor(request.user), pk=pk)
    return render(request, "post.html", {"post": post})
```

That's the end-to-end flow. The same `Post.objects.with_actor(...)` pattern works in DRF viewsets, Celery tasks, GraphQL resolvers, management commands, and future MCP tools — the actor can be a Django `User`, an `Agent`, an `agents/grant`, or any registered subject. Typed shorthands `as_user(request.user)` and `as_agent(agent, on_behalf_of=request.user)` cover the common cases.

## Documentation

| Doc | When to read it |
|---|---|
| **[docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)** | You're integrating, contributing, or evaluating fit. Architecture, public API, the three storage tiers, settings, surface integrations, determinism, testing, roadmap. |
| **[docs/ZED.md](./docs/ZED.md)** | You're writing permission schemas. How to define permissions for users, groups, AI agents (Grant pattern), Celery tasks, future MCP tools, hierarchical resources, time-bound access, arbitrary Python entities. Patterns library and anti-patterns. |

## Why use this

| Problem | Existing options | What `django-zed-rebac` does |
|---|---|---|
| Per-object permissions in Django | `django-guardian` (per-object ACL via GenericFK; no JOIN propagation; no graph traversal) | True REBAC graph; SpiceDB-compatible; manager-level queryset scoping; cross-relation propagation. |
| Run SpiceDB-style permissions locally without infrastructure | None — SpiceDB itself is a Go binary that needs Postgres + a sidecar | `LocalBackend`: pure-Django graph evaluation over relationship rows. Same API surface the planned `SpiceDBBackend` will use. |
| AI-agent authorization | Cedar (no graph traversal); Casbin (in-memory post-filter); Polar/Oso (deprecated 2023) | Native Authzed Grant pattern: an agent acting on behalf of a user receives the *structural intersection* of the user's grants and the agent's declared capabilities — enforced by the schema graph, not by app-layer ANDs. |
| Permission scoping outside HTTP | Manual `if user.has_perm(...)` everywhere | `Model.objects.with_actor(actor)` works in Celery tasks, cron, management commands, plain Python, and future MCP servers. The actor is generic: Django `User`, `Agent`, `agents/grant`, `auth/apikey`, or any registered subject. |
| Strict-by-default (no silent leaks) | `django-guardian` returns all rows when nothing scopes; easy to forget | Querysets without an actor raise `MissingActorError` rather than returning everything. Bypass requires an explicit `reason`; block-scoped `sudo()` is logged. |
| Admin-editable policy with safe upgrades | `django-guardian` per-object ACL only; no rule overrides | Tier-2 `SchemaOverride` model: tighten / loosen / disable / extend a package-shipped baseline at runtime. `noupdate=True` semantics preserve admin edits across upgrades, mirroring Odoo's `ir.model.data`. |

## Highlights

- **Three storage tiers, three editors.** Tier 1 (structural, code-shipped `.zed`) → Tier 2 (override, admin-editable) → Tier 3 (relationships, runtime data). Clear ownership rule per tier — see [docs/ARCHITECTURE.md § Conceptual model](./docs/ARCHITECTURE.md#conceptual-model).
- **Anonymous subject built-in.** `auth/anonymous:*` is shipped alongside `auth/user` and `auth/group`. The default resolver returns it for unauthenticated requests; schemas reference it via the bare `anonymous` keyword or the `auth/anonymous:*` wildcard. Subject type configurable via `REBAC_ANONYMOUS_TYPE`. See [docs/ARCHITECTURE.md § Anonymous subject](./docs/ARCHITECTURE.md#anonymous-subject--built-in).
- **Two LocalBackend storage shapes.** `REBAC_LOCAL_BACKEND_STORAGE = "denormalized"` (default) stores `resource_type` / `resource_id` / `subject_type` / `subject_id` as wide string columns — the historical shape. `REBAC_LOCAL_BACKEND_STORAGE = "registry"` collapses them into two integer FKs into a shared `RebacResource` table, yielding ~5-10x index-density gain on the hot path plus FK-CASCADE cleanup when the underlying Django row is deleted. Migration between the two is a one-shot via `python manage.py rebac migrate-storage --to registry`. See [docs/ARCHITECTURE.md § Storage modes](./docs/ARCHITECTURE.md#storage-modes).
- **Predefined-role helpers (`rebac.roles`).** GCP-style role-as-resource pattern packaged as `grant` / `revoke` / `roles_of` / `members_of` plus `imply` / `unimply` / `implies_of` / `implied_by_of` for runtime-editable hierarchy. Roles live as objects in `<namespace>/role` resource types; grants are `Relationship` rows. Three role-hierarchy recipes — type-union inclusion (`relation member: ... | parent_role:X#member`), per-resource permission composition (`permission read = viewer + editor + admin`), runtime-editable `includes`/`effective_member`. See [docs/ARCHITECTURE.md § `rebac.roles`](./docs/ARCHITECTURE.md#rebacroles--predefined-role-helpers).
- **Universal-admin lint (`rebac.W004`).** Optional system check that warns when a `<namespace>/role` definition is missing the universal-admin role's `#member` subject in its `member` type union. Configurable via `REBAC_UNIVERSAL_ADMIN_ROLE` (default `"angee/role:admin"`); set to `None` to disable. Catches the "I forgot to thread the admin override through" footgun at startup.
- **Unified check API.** `check_access(op)` / `has_access(op)` / `accessible(op)` — one entrypoint family, borrowed from [Odoo 18's PR #179148](https://github.com/odoo/odoo/pull/179148) unification. No model-level vs record-level split at the call site.
- **One mixin gates everything.** Add `RebacMixin` to a model, declare `Meta.rebac_resource_type`, and queries / writes / method calls / FK reverse accessors are all permission-aware. No per-viewset wiring.
- **Field-level read gates.** Schema permissions named `read__<field>` redact or omit denied fields at queryset materialisation time when `REBAC_FIELD_READ_MODE = "redact" | "omit"` (default `"allow"`). Redacted fields are excluded from later full saves so a display-time `None` cannot overwrite the stored value.
- **`with_actor(actor)` ≠ `sudo(reason=...)`.** Distinct verbs for distinct intents. `with_actor` re-evaluates checks as that subject (user, agent, grant, apikey, …); `sudo` bypasses them with mandatory `reason` and audit-log entry. Originating uid preserved through bypass for audit. Sudo does NOT propagate through relationship traversal — every related read re-resolves against the carrying scope.
- **Strict by default.** A queryset without an actor scope raises rather than leaking. Bypass requires an explicit reason; block-scoped `sudo()` writes a structured audit event.
- **Drop-in DRF integration.** `permission_classes = [RebacPermission]` + `filter_backends = [RebacFilterBackend]`. Per-action permission map; customisable.
- **GraphQL + WebSocket-aware.** Per-request `PermissionEvaluator` LRU-caches `check_access` / `accessible` calls — a single GraphQL query that fans out across 50 nested resolvers makes 1 backend call per `(actor, action, resource)`, not 50. The Strawberry adapter (`pip install django-zed-rebac[strawberry]`) ships `RebacExtension` (per-operation scope, per-emission for subscriptions) and `RebacChannelsConsumerMixin` (actor resolved at WS handshake; per-emission cache reset means revoked grants take effect on the next subscription tick). See [docs/ARCHITECTURE.md § Per-request evaluator + Zookie freshness](./docs/ARCHITECTURE.md#per-request-evaluator--zookie-freshness).
- **Write-then-read freshness via Zookie ContextVar.** Every write returns a `Zookie`; subsequent LocalBackend reads in the same scope auto-upgrade to `Consistency.AT_LEAST_AS_FRESH(zookie)`. Cross-request transport (SPA / JWT) is opt-in via `REBAC_ZOOKIE_TRANSPORT = "header" | "session"`. LocalBackend uses `Relationship.written_at_xid` as the freshness witness; the same public API is reserved for the planned SpiceDB adapter.
- **Celery actor propagation built in.** `before_task_publish` injects the actor into task headers; `task_prerun` restores it on the worker. Inside `@shared_task`, scoping happens transparently.
- **MCP integration planned.** MCP tools can already be modeled as schema resources, but the `rebac_mcp_tool` decorator is future work tracked in [proposal 0004](./docs/proposals/0004-mcp-tool-integration.md).
- **Three-state checks.** Like SpiceDB, `check_access()` returns `HAS_PERMISSION`, `NO_PERMISSION`, or `CONDITIONAL_PERMISSION(missing=[...])` — the latter lists which caveat fields the caller must supply for a definitive answer.
- **Typed package.** Ships `py.typed` and keeps the public API annotated so downstream projects and IDEs can reason about the manager/queryset surface.
- **`noupdate=True` upgrade safety.** Admin schema edits are preserved across package upgrades. Destructive overwrite is an explicit `--force-overwrite` flag, never an implicit side effect of install vs upgrade. Engineers Odoo's `-i` footgun out.
- **Deterministic build.** `python manage.py rebac sync --check` is a CI gate that returns non-zero on schema drift, mirroring `migrate --check`.

## Compatibility

| Python | Django | Status |
|---|---|---|
| 3.14 | 6.0 | ✅ supported and covered by CI |

Versioning follows SemVer while the project is below 1.0: minor releases may add public API and tighten alpha contracts; patch releases are reserved for compatible fixes.

Database support: PostgreSQL 13+ (production target), MySQL 8+ (supported), SQLite (test/dev only — local graph-walk performance is not production-grade). The `Relationship` table ships with all required indexes in `0001_initial.py`.

## Comparison

| Library | Per-object | Graph traversal | SpiceDB-compatible | AI-agent pattern | Maintained |
|---|:-:|:-:|:-:|:-:|:-:|
| `django-zed-rebac` | ✅ | ✅ | ✅ | ✅ Grant pattern | (in development) |
| `django-guardian` | ✅ (ACL via GenericFK) | ❌ | ❌ | ❌ | ✅ |
| `django-rules` | ❌ (predicate engine) | ❌ | ❌ | ❌ | ✅ |
| `django-spicedb` | proxy only | ✅ (via SpiceDB) | ✅ | ❌ | ❌ (early/inactive) |
| `casbin-django-orm-adapter` | ✅ (ACL, in-memory) | partial | ❌ | ❌ | ✅ |
| `django-oso` | ✅ | ✅ | ❌ | ❌ | ❌ (deprecated 2023) |
| `django-rls` (PostgreSQL RLS) | ✅ | DB-level only | ❌ | ❌ | ✅ |
| `zanzipy` | ✅ | ✅ | partial | ❌ | ❌ (early/single-author) |

`django-zed-rebac` is the first Django package targeting full SpiceDB schema-language compatibility AND a working in-process backend AND a first-class AI-agent pattern AND admin-editable policy with safe upgrades — none of the others combine all four.

## Backends in detail

```
┌─ Your application ──────────────────────────────────────────────┐
│   RebacMixin / RebacPermission / @rebac_resource                  │
│                            │                                      │
│            ┌───────────────▼──────────────────┐                  │
│            │  rebac.backends.Backend (ABC)   │                  │
│            │   check_access  has_access        │                  │
│            │   accessible    lookup_subjects   │                  │
│            └───────────────┬──────────────────┘                  │
│              ┌─────────────┴────────────┐                        │
│              │                          │                        │
│   ┌──────────▼──────────┐   ┌───────────▼───────────┐           │
│   │  LocalBackend       │   │  SpiceDBBackend        │           │
│   │  local graph walk + │   │  planned authzed       │           │
│   │  cel-python caveats │   │  adapter               │           │
│   └─────────────────────┘   └────────────────────────┘           │
└──────────────────────────────────────────────────────────────────┘
```

`LocalBackend` is the supported runtime backend today. `SpiceDBBackend` is on the roadmap behind the same `Backend` interface; persisted consistency tokens (`Zookie`s) are intentionally backend-tagged and will not be portable across that future swap.

## What `django-zed-rebac` is NOT

- **Not a User model.** Use `django.contrib.auth.models.User` or any swappable `AUTH_USER_MODEL`.
- **Not an authentication system.** Use `django-allauth`, `dj-rest-auth`, `simple-jwt`, or your own.
- **Not a session manager.** Django's session middleware is fine.
- **Not a multi-tenant database router.** Use `django-tenants` or `django-organizations`. `django-zed-rebac` is orthogonal — it works inside whatever tenant scope the project provides. (For soft tenancy in a single DB, see `REBAC_TYPE_PREFIX` in [ARCHITECTURE.md](./docs/ARCHITECTURE.md).)
- **Not a GraphQL admin layer.** A future `django-zed-rebac-admin` package may add one; v1 ships a Django admin form for `SchemaOverride`. Higher-level frameworks may layer their own admin surfaces on top.
- **Not a policy DSL** like Polar or Cedar. The schema language is SpiceDB's `.zed`, REBAC-first. ABAC fragments are expressed via caveats.

## Status & roadmap

This is an **alpha** package. The architecture is settled (see [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)) and releases are published to PyPI. Milestones:

- **0.1** — `LocalBackend` MVP, schema parser + sync command, `RebacMixin`, system checks, sync/check commands.
- **0.2** — Alpha hardening: schema-level built-in actors, action-scoped queryset reads, split `sudo()` / `system_context()`, and efficient schema cache invalidation.
- **0.3-0.7** — shipped middleware, Celery propagation, registry storage mode, evaluator/Zookie scopes, Strawberry adapter, field-level read gates, and LocalBackend hardening.
- **Next** — `SpiceDBBackend` adapter and MCP tool integration ([proposal 0004](./docs/proposals/0004-mcp-tool-integration.md)).
- **1.0** — Stable release with full docs and CI matrix green.

Track the full plan in [docs/ARCHITECTURE.md § Roadmap](./docs/ARCHITECTURE.md#roadmap).

## Contributing

See `CONTRIBUTING.md` for local setup and checks. Design feedback is welcome via GitHub issues — schema-language proposals, missing scenarios, integration-surface concerns, anything in [ARCHITECTURE.md § Open questions](./docs/ARCHITECTURE.md#open-questions) you'd push back on.

## License

Apache-2.0, matching `authzed-py`, `cel-python`, and `spicedb` itself.

## Acknowledgments

`django-zed-rebac` is a faithful Django port of the model described in [Google's Zanzibar paper](https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/) and as implemented by [SpiceDB](https://github.com/authzed/spicedb). The schema language and API surface mirror SpiceDB's conventions exactly. The Grant pattern for AI-agent authorization is from [Authzed's Secure AI Agents tutorial](https://authzed.com/docs/spicedb/tutorials/ai-agent-authorization). The unified check API (`check_access` / `has_access` / `accessible`) is borrowed from [Odoo 18 PR #179148](https://github.com/odoo/odoo/pull/179148). The `noupdate=True` upgrade-safety semantic is borrowed from Odoo's `ir.model.data`. Caveat evaluation in `LocalBackend` uses [`cel-python`](https://github.com/cloud-custodian/cel-python).
