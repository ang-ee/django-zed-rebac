# CLAUDE.md

Guidance for Claude Code working in the `django-zed-rebac` repository.

> See `docs/ARCHITECTURE.md` and `docs/ZED.md` for the design contract. Those docs are
> the source of truth.

---

## Project overview

`django-zed-rebac` is a **standalone, drop-in REBAC plugin for any Django 6.0
project** (the supported matrix is pinned in `pyproject.toml`; see § Tooling). SpiceDB-compatible schema language, two interchangeable
backends (`LocalBackend` recursive-CTE in pure Django; `SpiceDBBackend` over
`authzed-py`), strict-by-default queryset scoping, AI-agent Grant pattern,
MCP / Celery / DRF / GraphQL adapters.

**Status: pre-alpha.** Tier-1 source has landed (LocalBackend, mixin/manager,
parser, sync command, system checks); SpiceDBBackend, caveat evaluation, and
adapter modules are in flight per the ARCHITECTURE.md roadmap. Layout:

```
django-zed-rebac/
├── README.md                 # Public pitch + quickstart + comparison
├── CLAUDE.md                 # This file
├── docs/
│   ├── ARCHITECTURE.md               # Implementation specification
│   └── ZED.md                # Schema-authoring guide
├── pyproject.toml
├── src/rebac/            # Python module — see § Naming below
└── tests/
```

The Python module is **`rebac`** (Django convention: hyphens →
underscores; matches `django-rest-framework` → `rest_framework`,
`django-celery-beat` → `django_celery_beat`).

---

## Documentation hierarchy

Read in this order before changing anything substantive:

| Doc | Purpose | When to read |
|---|---|---|
| `README.md` | Public-facing pitch. What the package is, why use it, comparison to alternatives. | Always — it's the elevator pitch. |
| `docs/ARCHITECTURE.md` | Implementation specification. Architecture, public API, settings catalog, surface integrations (DRF/Celery/MCP/GraphQL), determinism, testing, roadmap. | Before adding any code, changing a public API, or touching settings/migrations. |
| `docs/ZED.md` | User-facing schema authoring guide. Patterns library, anti-patterns, scenarios (users/groups, agents, MCP tools, Celery tasks). | Before changing the schema parser/builder, or documenting a new authoring scenario. |

**If a behaviour isn't specified, propose a spec change first.** Don't
implement undocumented behaviour and then patch the spec to match — that's
how design intent erodes.

---

## Naming — locked

| Concept | Value |
|---|---|
| Pip distribution | `django-zed-rebac` |
| Python module | `rebac` |
| Settings prefix | `REBAC_*` |
| Management command | `manage.py rebac <subcommand>` |
| System-check IDs | `rebac.E001` … `rebac.W101` |
| Schema header comments | `// @rebac_package`, `// @rebac_package_version`, `// @rebac_schema_revision` |
| App label | `rebac` |
| Public Python imports | `from rebac import ...` |

These are stable. Don't propose alternatives without a spec PR first.

---

## Critical project invariants

These are non-negotiable. Violations break either the SpiceDB-compatibility
contract or the security-critical strict-by-default posture.

### 1. SpiceDB compatibility is a public contract

The schema language, terminology (subjects / resources / relations /
permissions / caveats / Zookies), and the `Backend` API surface mirror SpiceDB
so that flipping `REBAC_BACKEND = "spicedb"` is a **configuration change,
not a code change**.

- **Don't** add `LocalBackend`-only conveniences that have no SpiceDB
  equivalent (e.g., a "list all permissions for this user across all resource
  types" RPC SpiceDB doesn't expose).
- **Do** mirror `authzed.api.v1` method names in the Python `Backend` ABC
  (snake_case wrappers around `CheckPermission`, `LookupResources`,
  `LookupSubjects`, `WriteRelationships`, `WriteSchema`,
  `ExpandPermissionTree`).
- **Do** emit `use typechecking` at the top of every generated `.zed` file
  (catches mutually-exclusive-type intersections at WriteSchema time).

### 2. Canonical separators (per SpiceDB / Zanzibar)

- `:` separates **type from id**: `auth/user:42`, `drive/file:fil_abc`.
- `#` separates an **object reference from a relation/permission name**:
  `drive/file:fil_abc#viewer` (subject set), `drive/file#read` (the abstract
  permission).
- Wire form for a relationship row:
  `<resource_type>:<resource_id>#<relation>@<subject_type>:<subject_id>[#<subject_relation>]`.

**`drive/file:read` is wrong.** It would parse as "the resource of type
`drive/file` with id `read`". Use `drive/file#read` whenever you mean "the
permission `read` on the type `drive/file`". The CLI / Backend API never
glues type and permission together — they're always separate arguments.

### 3. Strict-by-default is non-negotiable

A queryset that escapes its actor scope **must raise `MissingActorError`**,
not silently return all rows. This is the django-scopes / django-tenants
design that prevents the worst data-leakage bug class.

- `REBAC_STRICT_MODE = True` is the production default.
- Bypass requires explicit `.sudo(reason="...")` or `with sudo(reason="..."):`.
  Both write a structured audit-log event.
- Empty `sudo()` calls without `reason` raise when
  `REBAC_REQUIRE_SUDO_REASON = True` (default).
- **Superuser carve-out.** When `REBAC_SUPERUSER_BYPASS = True` (default),
  two surfaces short-circuit for active superusers: (a)
  `RebacBackend.has_perm` returns `True` immediately, and (b)
  `ActorMiddleware` opens a `sudo(reason="superuser-bypass")` bracket for
  the request lifetime so QuerySet `accessible()` scoping also lifts —
  matching the contrib.auth contract that admin sees every row. The
  middleware path routes through the public `sudo()`, so each elevated
  request emits a `KIND_SUDO_BYPASS` audit row and obeys
  `REBAC_ALLOW_SUDO` (when sudo is globally disabled, the bypass is
  suppressed — fail-closed). Tenants that don't want either short-circuit
  set `REBAC_SUPERUSER_BYPASS = False`. **Don't** add a third superuser
  shortcut anywhere else (e.g. inside the manager or the engine itself);
  the two existing surfaces are the entire carve-out.

### 4. Three-state `CheckResult`

`Backend.check_access()` returns one of `HAS_PERMISSION`, `NO_PERMISSION`,
`CONDITIONAL_PERMISSION(missing=[...])` — **not a bare `bool`**. The
`CONDITIONAL` state mirrors SpiceDB's behaviour when caveat context is
partially supplied; callers may retry with additional context. Don't simplify
to `bool` — that erases the layered-checks invariant.

### 5. `with_actor` is the primary verb; `as_user` / `as_agent` are sugar

The generic verb is `.with_actor(actor)`, where `actor` is any `ActorLike`:
a Django `User`, a registered `Agent`, an `agents/grant` `SubjectRef`, an
`auth/apikey` `SubjectRef`, or anything `@rebac_subject`-decorated. Two typed
shorthands cover the common cases:

- `.as_user(user)` — Django `User` shortcut (≡
  `with_actor(to_subject_ref(user))`).
- `.as_agent(agent, on_behalf_of=user)` — agent acting via grant (≡
  `with_actor(grant_subject_ref(agent, user))`).

When a queryset is created with any of the above, every instance loaded from
it carries the resolved `SubjectRef`. A subsequent `instance.save()` checks
against that same actor, regardless of what `current_actor()` says now.

- **Don't** name a queryset method `as_user` and have the implementation be
  different from `with_actor(to_subject_ref(user))`. There is exactly one
  code path; the shorthands are sugar.
- **Don't** mutate a `ContextVar` inside `with_actor()`. The actor lives on
  the queryset instance.
- **Don't** re-resolve the actor on `save()` — that breaks the Celery / cron
  invariant where work continues under the original requester after their
  HTTP request ended.
- **Do** stamp the actor onto materialised instances via the queryset
  `_fetch_all` hook, not via `from_db` (the queryset's actor isn't visible
  inside `from_db`).
- **Do** make per-queryset `.with_actor(...)` strictly higher-priority than
  `current_actor()` ContextVar. Ambient context never overrides explicit
  local scope.

### 5a. Sudo does NOT propagate through relationship traversal

The single largest deliberate divergence from Odoo's `env.su` semantics. In
Odoo, `record.sudo().lines.user_id` reads BOTH `lines` AND `user_id` in sudo
because `env` propagates through every traversal. We don't:

- `instance.sudo(reason=...)` flips the bypass for *this instance only*. Any
  FK accessor / reverse-FK manager / M2M / chained queryset on it re-resolves
  the actor against the carrying scope (`current_actor()` or the queryset's
  pinned actor) — it does NOT inherit the sudo flag.
- If you genuinely need related rows under sudo, the inner queryset must call
  `.sudo(reason="...")` again. The audit log records every bypass
  independently — that's the point.
- **Don't** add a "sudo flag propagates through `from_db`" shortcut.
  Transitive sudo is a contagion; cutting it at every relationship boundary
  forces each bypass to be greppable. See
  [ARCHITECTURE.md § Lessons from Odoo](./docs/ARCHITECTURE.md).

### 5b. No implicit "owner from `created_by`"

Odoo's `ir.rule` filters routinely use `('create_uid', '=', user.id)` to
derive ownership from the audit column. We don't. Ownership is an explicit
`Relationship` row (`<resource_type>:<id>#owner @ auth/user:<id>`) written by
your `post_save` signal handler or application code. The
`created_by` / `created_at` audit columns on your model are independent.

- **Don't** add a "default owner = creator" path that derives ownership from
  a write column. Even if it seems convenient. Ownership must be transferable
  and revocable; conflating with audit makes both impossible without breaking
  audit.

### 5c. Soft-deleted rows participate in permission checks

Archived/inactive rows are visible to permission evaluation by default.
Soft-delete is orthogonal to permission scope — an admin with `delete` on an
archived resource needs to be able to un-archive it. If callers want to hide
archived rows, they filter at the queryset level
(`Post.objects.with_actor(u).filter(archived=False)`); the permission walk
over `Relationship` does not exclude them.

- **Don't** introduce an `active_test`-style toggle (Odoo's per-call
  footgun) that flips visibility from inside the permission layer. It's a
  top-level policy.

### 6. Determinism is load-bearing

`python manage.py rebac build-zed` must emit byte-identical
`effective.zed` across runs / machines / Python versions / Django versions.

- Sort definitions, relations, permissions, caveats — alphabetical by name.
- `json.dumps(..., sort_keys=True, indent=2, ensure_ascii=False)` + trailing
  `\n` for any JSON sidecars.
- No timestamps, no `datetime.now()`, no `uuid4()`, no `random.*`.
- All set / dict iteration: `sorted()`. All filesystem walks:
  `sorted(os.listdir(...))`.
- The CI determinism test runs the build twice in a tmpdir and byte-diffs.

### 7. Operator precedence in permission expressions

SpiceDB's expression grammar binds `+` (union) tighter than `&`
(intersection) tighter than `-` (exclusion). **Always emit explicit
parentheses** in compound expressions. Single-line schema fragments without
parens are a footgun even when they parse.

### 8. Wildcards only on read-shaped permissions

`auth/user:*` (or any `type:*` wildcard) **must not appear** in
write/delete/create permissions. The schema doctor (`rebac.W001`-class)
emits a warning at build time when a wildcard relation feeds a non-read
permission. Don't suppress the check; fix the schema.

### 9. Agent delegation belongs to consumer schemas

Consumer schemas must explicitly model any intersection of requester grants
and agent capabilities. The engine does not impersonate a grant's owner or
derive permissions from an agent's identity. Use declared relation subject sets
and relation-to-permission arrows; a suffix such as `#valid` is valid only when
`valid` is a relation, never when it names a computed permission.

`agents/agent` and `agents/grant` (and `auth/apikey`, `auth/service`, etc.)
are NOT shipped by this plugin. They live in the consumer's apps. The
plugin's configured User/Group mappings default to `auth/user` and `auth/group`;
automatic base-schema emission is not implemented. When you reject a PR that adds an
`agents/*` definition to `rebac/permissions.py`, this is why.

---

## Standalone-ness rule

This package **must work in any Django project as a standalone REBAC
engine**. Hard rule:

- **No imports from any specific consumer framework** anywhere in source,
  tests, examples, or docs. The engine is identity-agnostic and framework-
  agnostic; it knows nothing about any consumer.
- **No references to a specific consumer framework** in user-facing docs
  (`README.md`, `docs/`).
- **No `[tool.<framework>]` config conventions** in `pyproject.toml`.

Downstream frameworks may adopt `django-zed-rebac` as a dependency. The
relationship is one-way: **consumers depend on the engine, never the
reverse**. The plugin is identity-agnostic by design — it knows nothing
about a consumer's `User` model, request envelope, sqid IDs, or composition
engine.

The acceptable adapter point is `REBAC_ACTOR_RESOLVER` — a dotted-path
setting that lets a downstream package supply a custom
`request → SubjectRef` resolver. Standalone consumers use the default.

---

## What this package is NOT (drift signals)

Reject scope creep that turns this into anything other than a REBAC engine.
These belong in OTHER packages, never here:

- **Not a User model.** Use `django.contrib.auth.models.User` or any
  `AUTH_USER_MODEL`. The spec says so explicitly; if a PR adds a `User`
  subclass, reject it.
- **Not an authentication system.** No login views, no password reset, no
  OAuth. `django-allauth` / `dj-rest-auth` / `django-otp` exist for that.
- **Not a session manager.** Django's session middleware is fine.
- **Not a multi-tenant database router.** `django-tenants` and
  `django-organizations` solve this. The plugin works inside whatever tenant
  scope the project provides.
- **Not an audit-log system.** v1 emits structured logs only and the
  `PermissionAuditEvent` table; a future `django-zed-rebac-audit` package
  may add a queryable log; not in scope here.
- **Not a policy DSL** like Polar or Cedar. The schema language is SpiceDB's
  `.zed`, REBAC-first. ABAC fragments go through caveats.
- **Not a User permission UI.** Admins use Django admin or a downstream
  package; the override-layer admin in `rebac.admin` is bounded to
  surfacing the existing schema, not a generic policy editor.

If a feature request blurs one of these lines, **the answer is "different
package, not here"**.

---

## Implementation guidelines

### Tooling

Per `docs/ARCHITECTURE.md § Testing`:

- **Build:** `setuptools` via `pyproject.toml` (PEP 621). Source layout:
  `src/rebac/`.
- **Lint:** `ruff` with line-length 100.
- **Format:** `ruff format` (Black-compatible).
- **Type-check:** `mypy --strict` AND `pyright` — both must pass on CI. Ship
  `py.typed`.
- **Test:** `pytest` + `pytest-django` for integration; pure-Python `pytest`
  for unit. Cross-backend contract tests via `testcontainers-spicedb`,
  opt-in marker.
- **CI matrix:** Python 3.14 × Django 6.0 (`requires-python = ">=3.14"`,
  `django>=6.0,<6.1`; `ruff`/`mypy` target 3.14, so 3.14-only syntax such as
  PEP 758 `except A, B:` is in play) × sqlite for unit tests. Broader
  matrices are a release decision, not the current contract.
- **DjangoVer** for releases:
  `<DJANGO_MAJOR>.<DJANGO_FEATURE>.<PACKAGE_VERSION>`.

### AppConfig

Follow `docs/ARCHITECTURE.md § AppConfig and system checks` exactly:

```python
# rebac/apps.py
class RebacConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name              = "rebac"
    label             = "rebac"
    verbose_name      = "REBAC"
    default           = True

    def ready(self):
        from . import checks   # noqa: F401  — register system checks
        from . import signals  # noqa: F401  — connect pre/post-save handlers
```

**Two lines in `ready()`.** No queries. No model instantiation. No backend
resolution at import time. Backend singleton is constructed lazily on first
access via `rebac.backend()`.

### Migrations

- `Relationship` indexes ship in `0001_initial.py` — never as a
  documentation step.
- Every `RunSQL` operation has `reverse_sql`.
- Use `swapper` if `REBAC_RELATIONSHIP_MODEL` is genuinely swappable;
  otherwise mark `class Meta: managed = True` (default) and ship the
  standard migration.
- Migrations must run on PostgreSQL 13+, MySQL 8+, SQLite (test only). The
  recursive-CTE syntax differs slightly across these — test all three in CI.

### Settings

- All settings prefixed `REBAC_`. **No nested dict** (defeats
  `SILENCED_SYSTEM_CHECKS`).
- Read via `app_settings` object exposed by `rebac.conf`.
- Validation routed through Django's system checks framework (IDs
  `rebac.E001`, etc.). **Don't** raise at import time or in `ready()`.
- Production-only checks (`--deploy`) for things like
  `REBAC_SPICEDB_TLS = False`.

### Public API surface

Settled in `docs/ARCHITECTURE.md § Public API surface`. Adding to it requires a spec
update; removing from it is a breaking change requiring a major bump.

```python
# What's PUBLIC and semver-stable:
from rebac import (
    RebacMixin,
    require_permission, rebac_resource, rebac_subject,
    Backend, LocalBackend, SpiceDBBackend, backend,
    CheckResult, Consistency, Zookie, PermissionResult,
    ObjectRef, SubjectRef, RelationshipTuple, ActorLike,
    PermissionDenied, MissingActorError, CaveatUnsupportedError,
    PermissionDepthExceeded, NoActorResolvedError, SchemaError,
    current_actor, set_current_actor, actor_context,
    sudo, system_context,
    to_subject_ref, grant_subject_ref, to_object_ref,
    write_relationships, delete_relationships,
    app_settings,
)
from rebac.drf import RebacPermission, RebacFilterBackend
from rebac.celery import propagate_actor          # 0.3+
from rebac.mcp import rebac_mcp_tool                # 0.6+
```

`with_actor` itself is a method on `RebacManager` / `RebacQuerySet`, not
a top-level import. The top-level `actor_context(actor)` is the
context-manager equivalent for non-queryset code paths.

Anything under `rebac._internal.*` is private and may break in any minor
release.

---

## Common pitfalls

### Don't import models in `apps.py` at module level
`AppRegistryNotReady`. Import inside `ready()` or inside the function that
uses them.

### Don't import RebacMixin from `rebac/__init__.py` eagerly
`__init__.py` is loaded during `INSTALLED_APPS` boot, before models can be
defined. Use the lazy `__getattr__` shim already in place — eager top-level
imports of model classes deadlock the apps registry.

### Don't query the database in `ready()`
Breaks `migrate`, `makemigrations`, test DB setup. Validation goes through
system checks.

### Don't replace `_base_manager` with the scoped manager
Django uses `_base_manager` for FK reverse caching, M2M intermediate
handling, etc. — these break if filtering applies. Install the scoped
manager as `objects` (`_default_manager`); leave `_base_manager` unfiltered.
This is why bare-string `prefetch_related("rel")` doesn't auto-scope and the
spec requires the explicit `Prefetch(queryset=...)` form.

### Don't forget the operator-precedence parens
`a + b & c` ≠ `a + (b & c)`. Always emit parens in compound expressions.
The build's typecheck won't catch a precedence mistake — only logic bugs at
runtime will.

### Don't break the `Relationship` shape
The fields `(resource_type, resource_id, relation, subject_type, subject_id,
optional_subject_relation, caveat_name, caveat_context, expires_at,
written_at_xid)` are wire-compatible with `authzed.api.v1.Relationship`.
**Renames are breaking.** Adding a field is fine if it has a sane default.

### Don't put non-Django keys into `class Meta:`
Django's `Options` rejects unknown Meta attrs. The mixin uses a custom
`RebacModelBase` metaclass that strips `rebac_resource_type` /
`rebac_default_action` from Meta before delegating to `ModelBase`, then
restores them on `_meta` post-construction. Don't add new captured names
without extending the metaclass.

### Don't ship a non-deterministic build
Single biggest CI hazard. Re-test determinism whenever you touch the emitter:

```bash
python manage.py rebac build-zed
md5 rebac/effective.zed > /tmp/h1
rm rebac/effective.zed
python manage.py rebac build-zed
md5 rebac/effective.zed > /tmp/h2
diff /tmp/h1 /tmp/h2 || echo "FAIL: non-deterministic"
```

Do this in CI, not just locally.

### Don't add Caveat-only ABAC features that don't translate to `.zed`
If you add a feature only the `LocalBackend` can do (e.g. a Python lambda
evaluator), it breaks the SpiceDB swap promise. The contract: every feature
must compile to a valid `.zed` schema accepted by SpiceDB's `WriteSchema`.

### Don't reach for `select_related` on RBAC models in v1
The plugin doesn't auto-scope SQL JOINs in v1 — the custom SQL compiler
that does is reserved for v1.x (post-stable). For now, use
`prefetch_related(Prefetch("rel", queryset=Related.objects.with_actor(actor)))`
with the explicit queryset. The system check warns on bare-string
`prefetch_related` against an RBAC model; treat it as a real warning. For
defense-in-depth on Postgres, enable Row-Level Security at the database
level using `REBAC_RLS_*` settings (post-1.0).

---

## Workflow

When implementing or modifying the plugin:

1. **Read the relevant spec section.** `docs/ARCHITECTURE.md` for behaviour;
   `docs/ZED.md` for user-facing schema authoring.
2. **One concern per PR.** A change that touches the manager AND the build
   emitter is two PRs.
3. **Update specs first if the change is structural.** A new public API
   needs a spec entry before code lands.
4. **Run the verification chain** before reporting complete:
   - `ruff check src/ tests/`
   - `ruff format --check src/ tests/`
   - `mypy --strict src/`
   - `pyright src/`
   - `pytest` (all tests)
   - `pytest -m spicedb` (when Docker available)
   - `python manage.py rebac sync --check` (in the integration test
     project)
5. **Determinism test on every emitter touch.** See
   [§ Don't ship a non-deterministic build](#dont-ship-a-non-deterministic-build).
6. **No backwards-compat shims during 0.x.** Lockstep breaking changes are
   allowed pre-1.0; document them in `CHANGELOG.md`. After 1.0, follow
   [DjangoVer](https://www.b-list.org/weblog/2024/nov/18/djangover/).

---

## Open design questions tracked in specs

The spec calls out several open questions; don't re-decide them ad hoc:

- **`select_related` SQL compiler propagation** (v1.x).
- **PostgreSQL RLS defense-in-depth track** (post-1.0).
- **Relationship table partitioning at scale** (post-1.0).
- **Async ORM support** (0.5+).
- **Override-layer precedence vs caveat CONDITIONAL** (security-fail-closed
  lean).
- **MCP standardised identity field** (watching upstream MCP spec).
- **Per-tenant override scope** (1.x).

If you encounter one of these in a PR, reference the spec entry and either
resolve it (with a spec update) or note it as deferred. **Don't silently
land a partial decision in code.**
