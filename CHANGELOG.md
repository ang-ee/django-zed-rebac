# Changelog

All notable changes to `django-zed-rebac` are tracked here. The project is in
pre-1.0; breaking changes within a minor version are explicitly called out.

## [Unreleased]

### Fixed

- Resolve model resource and subject identity through instance metadata, so
  Django's lazy user and model wrappers retain the wrapped model's type, ID
  attribute and subject relation. Authenticated requests using a model-backed
  user no longer fail while resolving `AuthenticationMiddleware`'s lazy user.

## [0.17.1] — 2026-09-13

### Fixed

- Model identity resolution rejects `None` and empty strings before they can
  become shared invalid graph IDs. Valid zero-valued and UUID identities keep
  their existing wire representation.
- Unrelated Django model deletions skip subject resolution and relationship
  cleanup. Resource, User, Group and registered-subject cleanup retains the
  canonical identity resolver and deletion database alias.
- Repair the virtual-identity test fixture so Django initialization cannot
  shadow its computed value; the virtual live-backing regression suite now
  runs.
- The revocation regression keeps surviving direct grants in its expected
  scope instead of asserting they disappear.
- Live backing accepts ORM-queryable scalar virtual identities, including
  public IDs encoded from an existing integer primary key. Django fields own
  lookup preparation and result conversion; graph IDs and stored tuples keep
  their existing identity. Missing, model-object and non-queryable identity
  attributes remain invalid.
- Accept scalar `pk` identities on multi-table children and explicit relation
  ID attributes such as `parent_ptr_id`. Django's parent-link primary keys
  retain their native scalar conversion; relation descriptors that return
  model objects remain invalid.
- Preserve the native column optimization for unfiltered forward FK/O2O
  relations. Filtered, reverse and M2M paths correlate source rows by their
  primary key, without casting encoded public IDs. A foreign key targeting a
  different field from the REBAC identity resolves through the target model.
- Schema-dependent system checks defer unreadable persisted data while REBAC
  migrations are pending, so the ordinary migration command can apply
  `0004_field_backing_path`. Runtime evaluation remains strict, and the
  released migration is unchanged.
- Relationship cleanup covers configured Django User and Group subjects as
  well as model resources, using the deletion database alias in both storage
  modes. Unrelated identities and grants remain intact.

### Documentation

- Clarify that consumer schemas own agent delegation: the engine builds the
  grant subject but neither impersonates the grant's owner nor derives
  permissions from an agent's identity. `README.md`, `docs/ARCHITECTURE.md`,
  `docs/ZED.md` and both contributor guides now state the same contract.
- State the contributor CI matrix once: Python 3.14 × Django 6.0 × SQLite as
  declared in `pyproject.toml` and the CI workflow. `docs/ARCHITECTURE.md`
  no longer lists a Postgres integration matrix that CI does not run.

### Corrected contract — consumer migration required

- Removed the permission-named subject-set behavior added in 0.17.0.
  Relationship subjects may reference a declared relation (`group:id#member`),
  never a computed permission (`role:id#effective_member`). Schema loading and
  tuple writes reject that shape. Checking a permission or evaluating a
  `relation->permission` arrow remains supported.
- Runtime role hierarchy uses `relation includes: role` and
  `permission effective_member = member + includes->effective_member`.
  `rebac.roles.imply` writes a direct child-role object. Resource schemas
  likewise grant a plain role object and arrow to its `effective_member`.
- Consumers must migrate their schema and corresponding tuples together.
  Rewrite known implication edges from `@role:id#effective_member` to
  `@role:id`; move computed-role grants to the matching direct role relation.
  Preserve caveats, context and expiration. There is no generic suffix rewrite
  or automatic deletion of incompatible grants. See the role-hierarchy upgrade
  guidance in `docs/ARCHITECTURE.md`.

The native membership API, model-owned subject identity, filtered live field
and attribute backings, and cache-freshness protections introduced in 0.17.0
remain available. The original 0.16.3 FK/SQL scoping and encoded-ID optimizations
are retained.

## [0.17.0] — 2026-09-13

### Added

- `rebac.memberships`: direct `member`-tuple grant, exact (caveat-aware)
  revocation and enumeration for any container type; `containers_of` accepts
  Django lookups on the container so callers filter in SQL. `rebac.roles`
  composes it and keeps owning role-spec parsing and hierarchy.
- Model-owned subject identity: a `RebacMixin` model converts to a `SubjectRef`
  from its own `Meta.rebac_resource_type` / `rebac_id_attr`, optionally as a
  subject set via `Meta.rebac_subject_relation` (`rebac.E011` checks it).
  `rebac.resources.model_for_subject_type` is the single inverse mapping used
  by field/attribute backing and `resolve_subjects`.
- Live filtered ORM relation paths (`rebac:field={"path":...,"filters":...}`)
  and scalar attribute-backed containers (`rebac:attribute=...`), with shared
  parsing, persistence, direct evaluation, enumeration and lazy SQL scoping.
- `rebac.schema.introspection.named_object_refs`, `relation_is_writable`,
  `live_backed_resource_types` and `accessible_is_exact`.
- `rebac.W009` warns about case-insensitive collations on text attribute
  columns (best effort).
- Migration `0004_field_backing_path`, which rewrites stored field backings to
  the new `path` key (see Changed).

### Changed

- **Stored field-backing key.** `SchemaRelation.backing` spells the Django
  lookup path `path` instead of `attname` (`FieldBinding.path`). Run
  `manage.py migrate` (migration 0004 rewrites existing rows), then an
  ordinary `manage.py rebac sync`, which refreshes the provenance hash; admin
  edits under `no_update` are still reported as drift. The loader accepts only
  the new key.
- **Type prefix applies to every generated identity.** With
  `REBAC_TYPE_PREFIX` set, the configured user, group and anonymous subject
  types and `@rebac_subject` types are prefixed like model resource types.
  Prefixed deployments must declare the prefixed subject types in their
  schema and migrate stored subject tuples (no automatic retyping).
- **Decision-cache bypass is per resource type.** `LocalBackend` declines to
  cache decisions only for resource types that can reach live field or
  attribute backing (conservative schema reachability), instead of for the
  whole schema; transaction and expiration bypasses are unchanged.
  `rebac.backends.local.mark_relationships_changed()` is the LocalBackend seam
  that invalidates decisions after out-of-band row changes (the `post_delete`
  cascade uses it); it is not part of the top-level `rebac` export surface.
- **Relationship garbage collection on delete.** Deleting a `RebacMixin` row
  now removes every tuple naming it, as resource or subject, in both storage
  modes on the deleting alias. Denormalized deployments can drop their manual
  `post_delete` `Relationship.objects.filter(...).delete()` sweep.
- `RebacPermissionsMixin` adds no unconditional superuser shortcut; it walks
  the configured backend chain. `REBAC_SUPERUSER_BYPASS` governs
  `RebacBackend`, so that backend must be in `AUTHENTICATION_BACKENDS` for the
  permission-level bypass to apply.
- Permission-named subject sets (`type:id#permission`) resolve through the
  permission evaluator.
- Arrows through field- or attribute-backed relations cost a bounded number
  of queries in direct checks when the schema declares no caveats and no
  built-in actor terms; otherwise they keep the per-target tri-state walk.
- Unsaved `RebacMixin` instances raise `NoActorResolvedError` when used as a
  subject, as unsaved users already did.
- `FieldBinding`, `ConstBinding` and `AttributeBinding` no longer carry a
  `kind` attribute; the class is the discriminator and the codec owns the
  persisted `kind` key.
- Backing directive JSON is rendered with `ensure_ascii=False`, matching the
  project's JSON convention; output remains byte-stable.

### Removed

- The unimplemented `REBAC_SYNC_DJANGO_GROUPS` setting. Use live field backing
  for Django-owned membership or native membership tuples as the sole store.

## [0.16.3] — 2026-09-12

### Fixed

- Compile the queryset expiration predicate against the application clock
  (`timezone.now()`) rather than the database clock (`Now()`). The graph and
  enumeration paths already filter expiry with `timezone.now()`, so the SQL
  path could previously disagree with `accessible()` inside the app/DB
  clock-skew window; both strategies now bind the same instant.
- Resolve tuple-derived grants for non-native identities from the queryset's
  own database alias instead of the default one, so the ids and the
  surrounding `EXISTS` subqueries agree on where relationship rows live. Only
  affects multi-database setups; single-database projects are unchanged. The
  tri-state-evaluator sub-branches remain a documented boundary — see
  ARCHITECTURE.md "Open questions" (multi-database relationship resolution).

### Removed

- `REBAC_PK_IN_THRESHOLD` setting. It had no readers after local queryset SQL
  scoping became schema-driven rather than size-driven; tuning it changed
  nothing. Projects that set it can drop it (unknown settings are ignored).

## [0.16.2] — 2026-09-12

### Fixed

- Match Django's SQL-expression parameter tuple contract under current strict
  type checking. Version 0.16.1 was tagged but its PyPI publication was blocked
  by this type-check failure; 0.16.2 includes the encoded-ID corrections below.

## [0.16.1] — 2026-09-12

### Fixed

- Resource fields that encode their SQL values into different public IDs now use
  Django's conversion for tuple-derived grants. Sharing and exclusions no longer
  compare encoded wire IDs to raw database values. Use this release instead of
  0.16.0 when a resource ID field has a custom converter or virtual storage.
- Native FK columns own structural arrow membership, even when the target has
  an encoded public ID or a non-PK resource identity. Large field-owned corpora
  remain in SQL; only explicit tuple-grant branches needing conversion enumerate.
- Converted tuple branches resolve at SQL compilation, preserving revocation in
  pending scopes. Downstream caveats and recursion retain whole-expression
  fallback, including the negative arm of exclusions.
- Restore the established whole-type grant shortcut to avoid expensive redundant
  predicates on broad role scopes. Skip impossible direct/wildcard/subject-set
  reads according to the declared relation alternatives.

## [0.16.0] — 2026-09-12

### Added

- Local queryset authorization compiles acyclic, non-caveated permissions to
  native SQL predicates. Field ownership, tuple sharing, subject sets, arrows,
  constant roles, unions, intersections and exclusions no longer enumerate all
  readable resource IDs in Python. Both relationship storage modes are supported.
- Permission predicates remain lazy and retain actor/action rebinding and
  aggregate cardinality. Tuple revocation before SQL evaluation is reflected in
  pending queries and scoped subqueries, without a permission-result cache.
- Other backends retain the existing resource-enumeration path. Recursive and
  caveated expressions fall back as a whole to the conservative evaluator;
  explicit `accessible()` enumeration and write authorization are unchanged.

### Fixed

- Same-definition permission-alias cycles terminate in the enumeration fallback,
  matching the existing individual-check rule that denies a repeated branch.

## [0.15.2] — 2026-09-05

### Fixed

- Persisted permission schemas belong to each evaluator and database connection.
  Concurrent requests no longer replace each other's schema or decision cache.
- Read-only work inside Django `atomic()` reuses its schema snapshot instead of
  rebuilding the entire AST for every field or prefetched relation. Native SQL
  writes, bulk updates and manual savepoint rollback invalidate retained schemas;
  outer rollback and reused Atomic objects cannot retain temporary grants.
  Permission decisions remain uncached inside transactions.
- Same-connection bulk schema edits also invalidate autocommit snapshots. Changes
  committed by another connection remain visible at the next evaluator boundary.
- Manually managed transactions bypass decision caching as well, preventing a
  rolled-back grant from surviving in an evaluator. Snapshot observers and their
  no-op transaction markers are removed on scope exit without disturbing other
  Django SQL wrappers or commit callbacks.

### Clarified

- Application-defined mutating SELECT functions and direct driver writes require
  explicit evaluator invalidation; standard ORM and Django-cursor DML are observed.

## [0.15.1] — 2026-09-05

### Fixed

- Permission-aware prefetches preserve unprotected terminal relations after
  protected prefixes. Both string paths and explicit `Prefetch` objects retain
  their complete lookup, including custom querysets and `to_attr`, while each
  protected prefix keeps its actor scope and field gates. This prevents dropped
  prefetches and per-row lazy queries without propagating root sudo to related rows.

## [0.15.0] — 2026-09-05

### Added

- Public `rebac.schema` source resolution, canonical source-preserving rendering,
  allowed-subject rendering and `Definition.extend()` editing. Source rendering
  preserves headers/directives and local field/constant bindings; SpiceDB export
  explicitly uses `include_backing=False`. The management command shares these
  APIs instead of maintaining a second renderer/resolver.
- `permission_object_sources()` reports statically named positive object sources,
  including const bindings, arrows, subject sets and cycles. It is schema
  introspection, never authorization.
- Public `resource_id_attr()` and `subject_id_attr()` exports retain distinct
  resource and actor setting fallbacks.
- Documented top-level `RebacManager` and `RebacQuerySet` imports are exposed
  lazily without loading Django model classes during app initialization.
- `QuerySet.scoped()` returns an eagerly scoped clone for SQL projections and
  subqueries, pinning its resolved actor. `scoped_for_aggregate()` adds explicit
  fail-closed behavior without an actor, independent of strict mode, and disables
  instance field redaction. Callers still own projection-axis validation and
  the cardinality of their own SQL joins.

### Changed

- Django and its development stubs are constrained to the supported 6.0.x
  line. Fresh installations cannot silently select an unverified Django
  feature release with incompatible ORM and admin hooks.
- **Security hardening (alpha contract changes):** actor-scoped bulk upserts
  (`bulk_create(update_conflicts=True)`) and unmapped DRF actions now fail
  closed. Configure custom DRF actions through `rebac_action_map`, and use
  checked saves or explicit sudo for bulk conflict updates.
- `REBAC_UNIVERSAL_ADMIN_ROLE` now defaults to `None`. Set an application-owned
  role reference to enable the lint; the standalone engine no longer assumes
  a particular consumer's admin namespace.
- Normal schema sync preserves stale policy rows and reports drift. Deletion
  requires `--force-overwrite`, with `--yes` in non-interactive runs as specified.
- LocalBackend reads current database-visible state for freshness tokens and
  rejects `AT_EXACT_SNAPSHOT`, which requires historical relationship storage.
  Older tokens no longer hide newer deny relationships.
- DB-backed schemas refresh per request/evaluator scope or unscoped public
  backend operation, so policy edits in other workers take effect on the next
  scope. This adds one schema load per scope; recursive reads reuse the AST.
- LocalBackend bypasses decision caching inside database transactions and for
  schemas declaring expiring relationships. This prevents grants surviving
  rollback or tuple expiry; relationship mutations invalidate decisions across
  backend instances and nested evaluator scopes.
- CI and `make check` now gate formatting, strict mypy, and Pyright as well as
  lint and tests. Contributor requirements match Python 3.14 / Django 6.0;
  Celery docs now state that automatic propagation is not shipped.
- Tag publication runs the full verification chain and strict distribution
  metadata checks before uploading to PyPI, and retains the built artifacts.
- `roles_reaching()` delegates to object-source introspection. It now follows
  arrow-to-relation and subject-set targets and omits exclusion-right branches
  from positive role classification. Named sources may still be ineffective due
  to tuples, intersections or caveats; this helper must not authorize access.
- Explicit actors override ambient sudo on both eager projection APIs, matching
  normal queryset actor precedence. Explicit queryset sudo still bypasses scope.

### Fixed

- The documented top-level `RebacManager` and `RebacQuerySet` imports now
  resolve lazily, preserving Django app-loading safety.
- Channels adapter documentation now limits the mixin to async consumers;
  synchronous Channels consumers do not await its connection hooks.
- Stored caveat parameters take precedence over request context; invalid
  boolean inputs and non-boolean caveat results cannot become truthy grants.
  Relationship writes and reads enforce the declared caveat/expiration shape,
  and `with expiration` parses as a relation modifier. Preflight virtual tuples
  cannot bypass required caveats; their current shape supports only explicitly
  uncaveated subject alternatives.
- Conditional or built-in exclusion branches cannot over-grant resource
  lookups; expired arrow edges are absent, and reverse subject lookups check
  the complete permission rather than treating candidate tuples as grants.
- Evaluator caches cannot reuse another backend's grants or conflate boolean
  and numeric context values. Nested context values safely bypass caching.
- Expired schema overrides stop granting through warm schema and evaluator
  caches. Subscription invalidation refreshes schema snapshots too, and
  transaction-local policy grants cannot remain cached after rollback.
- Explicit queryset actors now carry through `create()` / `bulk_create()`;
  bulk inserts enforce create permission. Save/delete signals share the same
  actor resolver, so ambient sudo cannot override a pinned actor.
- Every materialized copy of a joined related row receives actor stamping and
  field redaction. Async iteration and projections no longer inherit root sudo
  across protected relationships; expression aliases cannot hide guarded reads.
- Async iterator field discovery and projection guards run in the worker thread,
  preserving field redaction when schemas must be loaded from the database.
- Strawberry subscription caches clear at each emission; GraphQL scopes retain
  inherited freshness tokens and propagate writes back to HTTP middleware.
- MCP streams restore the consumer's actor between chunks and during cross-task
  cleanup; malformed explicit identity cannot fall back to an ambient actor.
- ASGI middleware resolves Django's lazy session user and probes superuser
  status through a worker thread, avoiding synchronous ORM access on the loop.
- DRF honors per-view action mappings and keeps empty scoped lists valid.
- Changing actor/action or applying sudo to an eagerly scoped queryset replaces
  its old authorization restriction while preserving caller-authored predicates.
  Lazy queryset clones also remove stale scope predicates before re-evaluation.
- Boolean and SQL set combinations apply the left queryset actor/action policy
  to every operand, including for native SQL subquery compilation. A combined
  unscoped branch can no longer bypass the eager scope; rebinding replaces each
  operand restriction without mutating source querysets. Empty-query boolean
  fast paths retain the left actor. Boolean combinations with plain unscoped
  QuerySets now raise TypeError because Django can return the unscoped operand
  itself; combine REBAC querysets instead. SQL set combinators support plain
  operands by applying policy to each underlying model query.
- Numeric CEL literals and source columns following string literals survive
  schema parsing and rendering.


## [0.14.1] — 2026-07-17

### Fixed

- Corrected the role-reach documentation in `docs/ARCHITECTURE.md` and
  `rebac.roles`, which claimed that granting a role lights up a permission on
  every row of a type with no per-resource rows. A pinned-id `#member` allowed
  subject is a *grantable* subject: the grant opens the permission only on
  resources carrying a linking tuple, and the local backend never synthesises
  one. Const-backed relations remain the tuple-free path for role reach.
  Documentation only — no behaviour change.

## [0.14.0] — 2026-07-02

### Added

- Added `RebacMixin.unsudo()`, which clears an instance-level sudo pin and
  returns the instance without binding an actor. `with_actor(actor)` remains the
  bind-and-clear path for code that wants to leave sudo under a concrete actor.
- Added `RelationshipReadError` for registry-mode relationship reads that use a
  denormalized wire field in an expression surface the library cannot translate
  cleanly.

### Fixed

- Registry relationship storage now translates denormalized wire field names
  through `Q(...)` filters, `values()`, `values_list()`, and `order_by()`.
  Consumers can use the natural relationship read shape in both
  `REBAC_LOCAL_BACKEND_STORAGE` modes instead of branching on field layout.

## [0.13.0] — 2026-07-02

### Added

- Public effective-actor resolution for instances and querysets via
  `effective_actor(strict=False)`, returning the resolved actor and whether
  evaluation is unscoped. The private queryset resolver remains as a
  compatibility wrapper for one minor release.
- Mode-agnostic relationship query helpers for filtering, ordering, and
  projecting relationship rows across denormalized and registry-backed storage.
- Generic `resolve_subjects()` reverse lookup for `SubjectRef` values registered
  with Django models.
- Persisted-schema introspection helpers for permission dependencies and
  role reachability.

### Changed

- Instance permission checks now route actor resolution through the same public
  helper used for observation, with explicit pinned actors taking precedence
  over ambient sudo while per-instance sudo remains a bypass.

### Fixed

- `check_new()` now injects schema-derived const-backed relationship tuples into
  the virtual overlay used for create checks, and rejects caller-supplied tuples
  for const-backed relations. This closes the parent-specific create over-grant
  that could occur when callers fell back to type-level create checks.

## [0.12.1] — 2026-06-26

### Fixed

- **`evaluator_scope` tolerates cross-context teardown.** The scope's
  `ContextVar` reset raised `ValueError: Token was created in a different
  Context` when the scope was entered in one context and torn down from
  another — e.g. a Strawberry `on_operation` extension whose `AsyncExitStack`
  drives teardown while an error unwinds. The reset failure then masked the
  real operation error (such as an invalid GraphQL `where`), making it
  undebuggable. Cleanup now swallows that cross-context `ValueError`; the
  `ContextVar` is discarded with its context, so nothing leaks.

## [0.12.0] — 2026-06-23

### Added

- **`.for_write()` queryset/manager verb.** A named shorthand for
  `.on_field_deny("allow")` for the load-then-mutate case: resolving an
  update/delete target must materialise every column of the row, so
  `read__<field>` redaction must not hide columns from the loaded instance —
  yet the row must still be one the actor may access. `for_write()` turns field
  redaction off for that queryset while leaving actor **row** scope intact (the
  scope filter is still applied on materialisation). It grants no write
  permission of its own; the pre-save / pre-delete checks still run. Reach for
  it when `REBAC_FIELD_READ_MODE` is `"redact"` / `"omit"` and a redacted column
  must not be hidden from the row being written.

### Changed

- Project metadata: authorship transferred to Angee, Inc. (`hi@angee.ai`) and
  the repository moved to `https://github.com/ang-ee/django-zed-rebac`.
  Homepage, documentation, and issue-tracker URLs in the package metadata
  updated accordingly. No code or API changes.

## [0.11.1] — 2026-06-15

### Fixed

- **Async/aggregate scoping holes (unscoped reads).** Two `RebacQuerySet`
  paths summarised or streamed rows outside the actor's scope:
  - `aiterator()` — Django builds the async row iterable directly instead of
    routing through the overridden sync `iterator()` / `_fetch_all`, so
    `async for row in qs.as_user(u).aiterator()` returned rows `u` could not
    read. Now overridden to apply the scope filter, actor stamping,
    field-visibility redaction, and `rebac_select_related` guards (DB-touching
    steps run off the event loop via `sync_to_async`).
  - `aggregate()` / `aaggregate()` — computed against the query without
    materialising rows, so `qs.as_user(u).aggregate(Count("pk"))` counted the
    whole table. Now applies scope first; this closes the hole in **both** the
    sync `aggregate()` and the async `aaggregate()` wrapper.
- Hardened the audit fallback executor's `atexit` shutdown to tolerate
  interpreter-teardown ordering — when the stdlib's own `concurrent.futures`
  atexit hook runs first, `submit()` is skipped instead of raising "cannot
  schedule new futures after shutdown".
- `LocalBackend.check_access` on an empty `resource_id` (the model-level "may
  this subject create a new row of this type?" gate the `pre_save` create
  signal relies on) now honours resource-independent grants — the built-in
  `authenticated` / `anonymous` actors and const-backed arrows — by evaluating
  the permission against the not-yet-persisted row. Previously a permission
  like `create = authenticated` denied every create because no accessible row
  existed yet. Relation-based create permissions (`create = owner`) still
  resolve through the `accessible()` fallback, and row-dependent terms evaluate
  `False` against the empty id so nothing is spuriously granted.

### Notes

- No separate async manager API is needed: Django implements the rest of the
  async ORM surface (`aget` / `acount` / `aexists` / `afirst` / `aupdate` /
  `adelete` / `acreate` / `__aiter__` / `ain_bulk` / …) as `sync_to_async`
  wrappers around the sync methods `RebacQuerySet` already overrides, so
  scoping is inherited and the `current_actor()` ContextVar carries into the
  worker thread. `await Post.objects.as_user(u).aget(...)` enforces directly.
  See ARCHITECTURE.md § Open questions #3.

## [0.11.0] — 2026-06-15

### Added

- Added the FastMCP adapter `rebac.mcp.rebac_mcp_tool` (proposal 0004): a
  decorator that resolves the already-authenticated actor from the MCP request
  context, checks the target permission, and only then runs the tool body.
  Authentication stays the transport's job; the decorator only authorises.
  Exported `rebac_mcp_tool`, `default_actor_resolver`, and
  `get_mcp_actor_resolver` (lazily from the top-level `rebac` package). Actor
  resolution is pluggable via `REBAC_MCP_ACTOR_RESOLVER`.
- Actor resolution is **ctx-first, ambient fallback**: the per-call request
  context (`ctx.request_context.meta["actor_subject"]`) is the explicit
  identity and outranks the ambient `current_actor()` ContextVar, so a leaked
  ambient actor cannot override the authenticated caller.
- Create-shaped actions (`action="create"`) route through `rebac.check_new`
  and accept a `create_relations` mapping (relation name → the call argument
  holding the subject the new row would point at, as a canonical ref string),
  so a relation/arrow-based create permission (e.g. `create = parent->write`)
  can authorise a not-yet-persisted row.
- Sync functions, coroutine functions, and async generators (streaming tools)
  are all supported; the body runs inside `actor_context(actor)` so any
  queryset it builds scopes to the resolved actor without re-resolving it.
- The adapter is SDK-neutral — it reads the FastMCP context shape by
  duck-typing and never imports the `mcp` SDK. Added the optional `mcp` extra
  (`pip install django-zed-rebac[mcp]`) for running an MCP server alongside it.

### Fixed

- `default_actor_resolver` returns `None` on a missing **or malformed**
  `actor_subject`, so bad request metadata is a clean fail-closed deny rather
  than an uncaught `ValueError` (500).
- A `create`/`id_arg` parameter whose default the caller omits no longer
  targets a bogus `<type>:None` row — it falls back to `resource_id` / the
  singleton `"*"`.
- A `CONDITIONAL_PERMISSION` result fail-closes but now names the missing
  caveat parameters in the raised `PermissionDenied` so the caller can retry
  with context.
- The MCP `Context` is located by the conventional `ctx` / `context`
  parameter name, then by an argument whose type name is `Context` **and**
  that carries a `request_context` attribute, so an unrelated argument of some
  other `Context`-named class is not mistaken for the context.
- `hide_id_arg` emits a warning when set (documented no-op against FastMCP
  1.27) instead of silently doing nothing.

## [0.10.0] — 2026-06-01

### Added

- Added const-backed (synthetic) relations via `// rebac:const=<id>`: a relation
  that resolves to one fixed object id for every row of the declaring type, with
  no stored tuple and no model field. This is the schema-level "static
  relationship" SpiceDB never shipped (issues #346 / #1266); `LocalBackend`
  synthesises the edge at evaluation time. The canonical use is a universal-admin
  arrow — `relation admin: angee/role // rebac:const=admin` with
  `permission read = owner + admin->member` — so admin reach is one role
  membership, not one `Relationship` row per resource.
- Added `ConstBinding` AST node, parser support, `SchemaRelation.backing`
  round-tripping for `{"kind": "const", "target_id": ...}`, and `rebac.E009`
  system-check coverage for const bindings on types with no Django model.
- `// rebac:const=<id>` accepts the full SpiceDB object-id grammar (hyphens,
  leading digits / ULIDs / sqids, and `/ _ | = +`), not just Python-identifier
  ids — a target id like `role-admin` no longer silently parses as un-backed.
- Two new system checks for const-backed relations: `rebac.E009` now also rejects
  a const relation whose **target type** has no schema definition (a typo that
  would otherwise silently deny), and `rebac.E010` rejects const arrows that form
  an evaluation **cycle** (which would recurse to the depth limit on every check)
  — both caught at `manage.py check` / `rebac sync --check` time.

### Fixed

- Tuple writes/deletes to const-backed relations now raise `SchemaError`
  (the relation is synthetic and holds no tuples).
- Const-backed relations referenced **directly** in a permission expression
  (`permission read = admin`) and surfaced via `lookup_subjects` now resolve
  to the fixed const target; previously only the arrow form (`admin->member`)
  was wired up, so the direct/`lookup_subjects` paths silently denied even the
  const target subject. `accessible()` over a direct const reference likewise
  returns every row of the source type when the const target grants.
- The Strawberry-Django optimizer now honours **ambient** sudo: `_pin_current_actor`
  leaves a queryset unscoped when `is_sudo()` is active (the `ActorMiddleware`
  superuser bypass or an explicit `sudo()` block), not only when a sudo reason is
  stamped on the queryset. Previously the optimizer re-pinned the current actor
  and silently defeated the bypass at the queryset layer.

### Performance

- A granting const arrow now reports `grants_all()` as the whole-type grant it
  is, so queryset scoping takes the unrestricted path (adds no filter) instead
  of enumerating every row of the source type into an `id__in` clause. Since the
  arrow's target object is fixed, this is a single check rather than a full-table
  materialisation — important for "admin sees everything" reads at scale.

### Tooling

- Added `pyright` to the `dev` extra and a `[tool.pyright]` config; `src/` is now
  clean under both `mypy --strict` and `pyright` (Django reverse-manager / FK
  `_id` / dynamic-manager gaps are bridged with typed `TYPE_CHECKING`
  annotations and narrow per-line ignores, since `pyright` has no django-stubs
  plugin).

### Notes

- Like field-backing, const-backing is a `LocalBackend` synthesis with no SpiceDB
  equivalent; a SpiceDB backend would need the edge materialised as tuples.

## [0.9.0] — 2026-05-30

### Added

- Added explicit field-backed structural relations via
  `// rebac:field=<field>`, allowing `LocalBackend` to resolve forward FK and
  one-to-one relations from Django model fields instead of duplicate
  `Relationship` rows.
- Added `SchemaRelation.backing`, parser/AST support, and `rebac.E009` system
  checks for field binding mismatches.
- Added `auth/user` target support for field-backed relations, honoring
  `REBAC_USER_ID_ATTR`.

### Fixed

- Field-backed relations now fail loudly if their declared binding cannot
  resolve instead of falling back to stale stored tuples.
- Tuple writes/deletes to field-backed relations now raise `SchemaError` with
  the Django field to update instead.

### Documentation

- Documented field-backed relations in the ZED and architecture guides, added
  proposal 0005, and recorded SpiceDB projection/reconciliation as phase 2.
- Updated the README roadmap summary to include the 0.8 relation-loading,
  Strawberry-Django optimizer, and 0.9 field-backed relation work.

## [0.8.0] — 2026-05-29

### Added

- Added `rebac_select_related()` and `rebac_prefetch_related()` queryset /
  manager helpers for permission-aware relation loading. Guarded
  `select_related` keeps to-one JOIN performance while raising before an
  unreadable related row can serialize; protected prefetches are rewritten to
  actor-scoped `Prefetch` querysets.
- Added the `strawberry-django` extra and
  `rebac.graphql.strawberry_django.RebacDjangoOptimizerExtension`, a
  Strawberry-Django optimizer wrapper that preserves upstream `only`,
  `annotate`, `select_related`, and `prefetch_related` optimizations while
  routing protected relations through the REBAC helper surface.

### Documentation

- Removed proposal docs for work already shipped in code and recorded in this
  changelog: registry storage, evaluator/Zookie/Strawberry, and field-level
  read gates.
- Added proposal 0004 for the not-yet-implemented MCP tool adapter.
- Clarified that `SpiceDBBackend` remains roadmap work and that registry
  storage is still opt-in in 0.8.0.

## [0.7.0] — 2026-05-29

### Added — field-level read gates (proposal 0003)

- Added `REBAC_FIELD_READ_MODE = "allow" | "redact" | "omit" | "raise"` and
  `.on_field_deny(mode)` for queryset/manager field-read deny behavior.
  Schema permissions named `read__<field>` now redact denied fields at
  materialisation time when enabled; `"omit"` also records
  `_rebac_omitted_fields` for projection layers. `"raise"` is accepted for
  forward compatibility and degrades to `"redact"` with system check
  `rebac.W008`.
- Added instance helpers `denied_read_fields()`, `with_field_deny()`, and
  `redacted()` for explicit single-row projection with caveat context.
- Redacted fields are excluded from full saves and fail closed when explicitly
  named in `save(update_fields=[...])`, preventing a presentation-time `None`
  from overwriting stored data.
- Projection querysets that would return gated fields directly now fail closed
  in enforced modes, and iterator-based model materialisation applies the same
  redaction pass as normal queryset evaluation.

### Changed

- Factored shared field-gate discovery into
  `rebac.schema.walker.field_gated_actions(definition, verb)` and reused the
  evaluator-aware `accessible()` routing for both row scoping and field
  visibility.
- `REBAC_LINT_BARE_PREFETCH` now defaults to `True`, so bare relation
  prefetch checks run by default unless explicitly opted out.

### Fixed — authorization hardening

- Public LocalBackend relationship writes now validate the relation and
  subject shape against the installed schema before persisting rows. Stale
  invalid relationship rows are ignored by checks and accessible-resource
  enumeration.
- Create preflight now validates virtual relation candidates against relation
  type unions before evaluating permissions.
- Bulk queryset update/delete guards now scan the affected rows through a
  system context, preventing unreadable rows from disappearing from the guard
  while still being mutated.
- Schema sync now routes relation and permission rows through the shared row
  sync path, recomputes row hashes from actual payloads, prunes stale
  package-managed definitions/caveats and child rows, and rejects duplicate
  definitions/caveats across installed app schemas even during targeted
  package syncs.
- Model resource identity resolution now consistently honours
  `Meta.rebac_id_attr` across object refs, managers, signals, mixins, auth,
  DRF filtering, and field visibility.
- DRF permission and filter helpers now prefer the ambient current actor over
  `request.user`, keeping grant-backed agent flows scoped to the resolved
  actor.

### Internal — test coverage

- Added a dedicated LocalBackend end-to-end suite with fake users/resources
  covering public backend methods, caveated checks, grant/revoke lifecycle,
  queryset read scoping, protected field reads/writes, and agent grant
  shorthands.

## [0.5.0] — 2026-05-23

### Changed — Django 6.0+ only (BREAKING)

- Minimum supported Django is now **6.0** (was 4.2). The `4.2` and
  `5.2` trove classifiers are dropped and `dependencies` pins
  `django>=6.0`. This lets the engine rely unconditionally on the
  async session API (`SessionBase.aget` / `aset`), `transaction.aatomic`,
  and the 6.0 async stack without runtime feature detection.

### Removed — pre-1.0 deprecation shims

- Deleted `rebac.actors.accessible_cached`,
  `enable_accessible_cache`, and `disable_accessible_cache`. These were
  0.4-era aliases kept behind a `DeprecationWarning`; per CLAUDE.md's
  "no backwards-compat shims during 0.x" rule they're removed outright.
  Use `rebac.evaluator.current_evaluator()` / `evaluator_scope()`
  directly.

### Internal — strict typing

- The package now type-checks clean under `mypy --strict` with
  `django-stubs` (added as a dev dependency and wired through the mypy
  plugin); both `src/` and `tests/` are covered. `RebacQuerySet` /
  `RebacManager` are generic over the model so the actor verbs preserve
  the concrete row type through chaining. No runtime behaviour change.

### Added — dual-mode (sync + async) `ActorMiddleware`

- `rebac.middleware.ActorMiddleware` now advertises both
  `sync_capable = True` and `async_capable = True`. At install time it
  detects whether Django passed a coroutine `get_response` and (via
  `asgiref.sync.markcoroutinefunction`) marks itself as awaitable, so
  Django routes through the new `__acall__` coroutine instead of
  wrapping the sync `__call__` in `async_to_sync`. In a pure-async
  stack this collapses the sync↔async thread sandwich that previously
  produced the doubled "During handling of the above exception, another
  exception occurred" traceback on client-disconnect `CancelledError`.

  The async path mirrors the sync path exactly — same `evaluator_scope`,
  `zookie_scope`, and `sudo(reason="superuser-bypass")` brackets (all
  three are ContextVar-only, safe inside `async def`). Two refinements
  are async-only:

  - Zookie **session transport** uses `request.session.aget` / `aset`
    so the session load never forces a synchronous DB call when running
    under ASGI.
  - The configured `REBAC_ACTOR_RESOLVER` may now be declared
    `async def`; the async path awaits it. The sync path is unchanged
    and continues to call the resolver synchronously.

  No setting changes, no migration. Sync-only deployments see no
  behavioural difference.

### Added — `asudo` / `asystem_context` / `aemit_audit_event`

- `rebac.asudo(reason=...)` and `rebac.asystem_context(reason=...)` —
  ``@asynccontextmanager`` siblings of ``sudo`` / ``system_context``.
  Same audit-row guarantee, same ContextVar bookkeeping, but the
  ``KIND_SUDO_BYPASS`` row is written through
  ``PermissionAuditEvent.objects.acreate`` so the INSERT runs on the
  event loop instead of via the sync ORM. Async views, async tasks,
  and the dual-mode `ActorMiddleware` should reach for these in
  preference to the sync helpers.
- `rebac.aemit_audit_event(...)` — async variant of
  `emit_audit_event`. ``defer_to_commit=False`` awaits ``acreate``
  directly; ``defer_to_commit=True`` registers the on-commit callback
  via ``asgiref.sync.sync_to_async(transaction.on_commit,
  thread_sensitive=True)`` so callers using ``transaction.aatomic()``
  see the same connection that holds their atomic block — the
  on-commit hook lands in the right transaction's queue, not on a
  fresh worker thread with its own (uncommitted) connection. Pure
  autocommit callers see the equivalent immediate-write behaviour
  the sync :func:`emit` already offers.
- ``ActorMiddleware.__acall__`` now opens the superuser bypass via
  ``asudo`` instead of the sync ``sudo`` → worker-thread audit hop.
  The worker-thread fallback in ``rebac.audit._write_now`` stays as a
  belt-and-braces safety net for any sync ``sudo()`` reachable from
  an event loop, but the supported path for new async code is
  ``asudo`` / ``aemit_audit_event``.

## [0.4.0] — 2026-05-22

### Added — preflight against not-yet-persisted resources

- **`rebac.check_new(*, subject, action, resource_type, relationships=None,
  backend=None, context=None) -> CheckResult`** — three-state preflight
  for create-style permissions. Authorises a row *before* it exists by
  evaluating the schema's permission expression against a caller-supplied
  virtual `relation → subjects` overlay. Arrow hops walk into the real
  target via `backend.check_access`, so caveat-conditional outcomes on
  the target propagate as `CONDITIONAL_PERMISSION` with the union of
  missing parameter names. Built-in actor terms (`anonymous` /
  `authenticated`), subject-set candidates (`auth/group:eng#member`
  inside a virtual relation), `<type>:*` wildcards, the `+ & -`
  operators, sub-permission references, and `REBAC_DEPTH_LIMIT` are all
  honoured via the shared walker.

  Documented in `docs/ARCHITECTURE.md § check_new`. Free function by
  intent — SpiceDB ships no "check with proposed tuples" RPC, so this
  deliberately lives outside the `Backend` ABC. A SpiceDB-mode
  implementation in 0.5+ will likely use a write-then-rollback
  sub-transaction strategy.

### Added — `Backend.schema()` abstract method (BREAKING for external subclasses)

- **`Backend.schema() -> Schema`** — promoted from a `LocalBackend`
  private to an ABC method. Mirrors SpiceDB's `ReadSchema`; required by
  engine-side semantic checks (notably `check_new`) that walk
  permission expressions before any row exists. `SpiceDBBackend`
  carries a `raise NotImplementedError` stub until 0.5 wires
  `Client.ReadSchema()`. **External `Backend` subclasses must
  implement `schema()`** — adding the abstract method without a
  fallback is intentional, per CLAUDE.md's "no backwards-compat shims
  during 0.x" rule.

### Changed — shared AST walker (`rebac.schema.walker`)

- Refactored `LocalBackend._eval_permission`'s permission-expression
  dispatcher into a reusable, injection-shaped tri-state walker at
  `rebac.schema.walker`. Operator precedence, sub-permission cycle
  detection, depth bookkeeping, `OR/AND/MINUS` tri-state combinators,
  and the `anonymous` / `authenticated` built-in actor matching now
  live in one place. `LocalBackend` and `check_new` both go through it
  via caller-supplied `resolve_relation` / `resolve_arrow` callbacks.
  Pure internal restructure — no behaviour change for existing
  `check_access` / `accessible` callers.

## [0.3.2] — 2026-05-18

Follow-up patch addressing review findings against 0.3.1.

### Added

- **`RelationshipFilter.caveat_name`** — filter form is now
  caveat-aware (wildcard-on-empty, same as the other fields).
  Closes the gap where 0.3.1 added a singular caveat-exact delete
  but the plural/filter form still couldn't target by caveat at all.
- **Audit target string includes caveat** — `_format_target` now
  appends ` with <caveat_name>` when the relationship is caveated, so
  grants/revokes of caveated rows are distinguishable in the audit
  log from their uncaveated counterparts.

### Changed

- **`rebac.roles.grant` / `imply` wrap write + read-back in
  `transaction.atomic()`** — closes the `Relationship.DoesNotExist`
  window where a concurrent `revoke` between the upsert and the
  follow-up `.get()` could surface a spurious exception.
- **`rebac.roles.revoke` / `unimply` wrap presence-check + delete in
  `transaction.atomic()`** — the returned `0`/`1` count is now
  consistent within the same transaction snapshot rather than
  best-effort across two queries.
- **`LocalBackend.delete_relationship` / `delete_relationships` wrap
  their operations in `transaction.atomic()`** — for parity with
  `write_relationships` and to make the snapshot-then-delete sequence
  in the public helpers atomic with the backend write.
- **`chain_resolvers` docstring no longer claims pickle-safety** —
  the returned closure is not actually picklable. Re-clarified as
  intended for module-level assignment + dotted-import via
  `REBAC_ACTOR_RESOLVER`.

### Documentation

- `docs/ARCHITECTURE.md` public-API surface now lists
  `delete_relationship`, `chain_resolvers`, `bearer_token` and notes
  the deliberate SpiceDB divergence of singular
  `Backend.delete_relationship` (to be lowered through
  `WriteRelationships` with `OPERATION_DELETE` in 0.4).

## [0.3.1] — 2026-05-18

### Added — composable actor resolvers

- **`rebac.chain_resolvers(*resolvers, terminal=default_resolver)`** —
  compose multiple actor resolvers into a single callable. Tries each
  resolver in order; the first non-`None` `SubjectRef` wins. Falls
  through to `terminal` (default: `default_resolver`) when every
  supplied resolver declines. Pass `terminal=None` to disable the
  fallback. Lets downstream addons stack alternative credential paths
  (bearer-token → API key, service header → service account, …)
  without re-deriving the user/anonymous resolution that the library
  already ships.
- **`rebac.bearer_token(request)`** — parse a `Bearer <token>` value
  out of `request.META["HTTP_AUTHORIZATION"]`. Case-insensitive scheme
  match per RFC 7235; returns an empty string when no Bearer
  credential is present so callers can short-circuit on falsiness.
  Pairs with `chain_resolvers` so downstream resolvers don't
  re-implement header parsing.

Both helpers are exported at the top level (`from rebac import
chain_resolvers, bearer_token`) and also available on
`rebac.actors`.

### Added — `Backend.delete_relationship` (singular)

- **`Backend.delete_relationship(tuple_: RelationshipTuple) -> Zookie`**
  — a singular companion to the filter-shaped
  `delete_relationships(filter_)`. Where the filter form treats empty
  `optional_subject_relation` / `caveat_name` as wildcards ("don't
  filter on this field"), the singular form treats them as **exact
  values**, so callers can delete one specific shape without
  collaterally removing subject-set or caveated rows that share the
  rest of the key. Exposed at the top level as
  `rebac.delete_relationship`. `LocalBackend` implements;
  `SpiceDBBackend` stubs to match the existing
  `delete_relationships` stub.

### Changed — `rebac.roles` mutations route through public helpers

- `grant` / `revoke` / `imply` / `unimply` now call
  `write_relationships` and `delete_relationship` instead of bare ORM
  `get_or_create` / `filter().delete()`. Side-effect: every role
  mutation now emits the standard `KIND_RELATIONSHIP_GRANT` /
  `KIND_RELATIONSHIP_REVOKE` audit row and stamps a zookie into the
  ambient freshness ContextVar — the role layer was previously the
  only mutating surface that bypassed those.

## [0.3.0] — 2026-05-17

Three substantial feature drops since 0.2.0: built-in anonymous subject + role
helpers, registry-shaped relationship storage (proposal 0001), and a per-request
permission evaluator + Zookie freshness propagation + Strawberry/Channels
adapter for GraphQL-over-WebSocket subscriptions (proposal 0002).

### Added — auth/anonymous + `rebac.roles` (initial 0.3 cycle)

- **Built-in anonymous subject.** `auth/anonymous:*` ships alongside
  `auth/user` and `auth/group`. The default resolver returns it for
  unauthenticated requests; schemas reference it as the
  `auth/anonymous:*` wildcard or the bare `anonymous` schema keyword.
  Configurable via `REBAC_ANONYMOUS_TYPE`.
- **`rebac.roles` convention helpers** — `grant` / `revoke` /
  `roles_of` / `members_of` plus `imply` / `unimply` / `implies_of` /
  `implied_by_of` for runtime-editable role hierarchy. Wraps the
  GCP-style "role as a resource" pattern; grants are `Relationship`
  rows on `<namespace>/role` objects.
- **`AllowedSubject.id` schema-side specific ids.** Type unions can
  now reference single objects via the `<type>:<id>` /
  `<type>:<id>#<relation>` shapes — the canonical universal-admin
  pattern (`angee/role:admin#member`). Constrained to identifier-shaped
  ids at the parser level.
- **`rebac.W004` universal-admin lint** — warns when a
  `<namespace>/role` definition is missing the universal-admin role's
  `#member` subject in its `member` type union. Configurable via
  `REBAC_UNIVERSAL_ADMIN_ROLE` (default `"angee/role:admin"`).
- Configurable auth middleware: `REBAC_AUTHENTICATION_MIDDLEWARE`
  (default `"django.contrib.auth.middleware.AuthenticationMiddleware"`)
  lets frameworks that replace Django's stock auth middleware tell
  rebac's `E003` / `E004` order checks which path to look for.
- Parser now accepts top-level keywords (`use`, `relation`,
  `permission`, etc.) as relation and permission names — `permission
  use = owner` parses cleanly, matching SpiceDB's own grammar.

### Added — proposal 0001 (registry storage shape)

- **`REBAC_LOCAL_BACKEND_STORAGE = "denormalized" | "registry"`** —
  selects between the historical four-CharField shape (default in
  0.3.x) and a new `RelationshipRegistry` shape with two integer FKs
  into a shared `RebacResource` table. ~5-10× index-density gain on
  the hot path plus FK-CASCADE cleanup when the backing Django row
  is deleted.
- New models `rebac.models.RebacResource`,
  `rebac.models.RelationshipRegistry`, manager
  `RelationshipRegistryManager` (string-kwarg translation), helper
  `rebac.models.active_relationship_model()`.
- New management subcommand `python manage.py rebac migrate-storage
  --to registry [--from denormalized] [--batch N] [--dry-run]`.
  Bidirectional, idempotent, parity-checked.
- New settings: `REBAC_LOCAL_BACKEND_REGISTRY_BATCH_SIZE` (default
  `5000`).
- New system checks: `rebac.E006` (invalid storage value),
  `rebac.W005` (migrate-to-registry recommendation when on
  `denormalized`).
- Cascade signal handler `_rebac_cascade_resource` (registry mode
  only).
- Registry storage remains opt-in in 0.7.0; any default flip or
  denormalized-path removal is deferred to a future minor release.

### Added — proposal 0002 (evaluator + Zookie freshness + Strawberry/Channels)

- **`PermissionEvaluator`** — per-scope LRU cache for `check_access`
  and `accessible` calls. Bounded by `REBAC_EVALUATOR_CACHE_SIZE`
  (default `10_000`). Conditional results never cached; per-call
  explicit `consistency` / `at_zookie` bypass cache. The evaluator
  rides on `_current_evaluator` ContextVar — async-safe across
  `asyncio.create_task` / Strawberry resolvers.
- **`current_evaluator()` / `evaluator_scope()`** — public API in
  `rebac.evaluator`. `ActorMiddleware` opens a scope per request;
  `RebacExtension` opens one per GraphQL operation (per emission for
  subscriptions).
- **Zookie freshness ContextVar** — `current_zookie()`,
  `record_zookie()`, `zookie_scope()`, `effective_consistency()` in
  `rebac.consistency`. `write_relationships` / `delete_relationships`
  auto-record the post-write Zookie; subsequent reads auto-upgrade
  to `Consistency.AT_LEAST_AS_FRESH`. Uses an internal `_NO_SCOPE`
  sentinel so writes outside an open scope don't leak across
  requests/tests.
- **Backend ABC `at_zookie` parameter** — `check_access`,
  `accessible`, `lookup_subjects` accept `at_zookie: Zookie | None`
  for freshness-pinned reads. LocalBackend translates to
  `written_at_xid <= cutoff` on every Relationship read in the
  evaluation walk. `write_relationships` returns a Zookie whose
  token equals the batch's actual max-xid watermark.
- **Cross-request Zookie transport** — `REBAC_ZOOKIE_TRANSPORT`:
  `"none"` (default), `"header"` (`REBAC_ZOOKIE_HEADER_NAME`,
  default `X-Rebac-Zookie`), `"session"`
  (`REBAC_ZOOKIE_SESSION_KEY`, default `_rebac_zookie`).
- **`rebac.graphql.strawberry` adapter** — behind `[strawberry]`
  extra (`pip install django-zed-rebac[strawberry]`).
  `RebacExtension` (per-operation evaluator + Zookie scope; mirrors
  state onto `info.context.rebac_evaluator` / `.rebac_zookie`) and
  `RebacChannelsConsumerMixin` (actor resolution at WS handshake).
  Subscription invariants: actor connection-scoped, evaluator +
  Zookie per-emission, so revoked grants take effect on the next
  tick.
- New system checks: `rebac.E007` (invalid Zookie transport value),
  `rebac.W006` (session transport without `django.contrib.sessions`).

### Fixed

- **`build-zed` emitter no longer drops `AllowedSubject.id`.** Both
  the rendered output and the deterministic sort key now include
  the specific-id slot. Pinning regression tests added.
- Parser emits a clearer `ParseError` when a specific-id isn't
  identifier-shaped (`role:42`, `role:obj-admin`, `role:sub/admin`).
- `_builtin_actor_matches` in `LocalBackend` now delegates to
  `actors.is_anonymous_actor` instead of reimplementing the
  predicate inline.
- `to_subject_ref(user)` where `user.is_authenticated` is False now
  raises `NoActorResolvedError` instead of silently downgrading to
  the anonymous actor. The request-path resolver still fails safe
  via its existing `except NoActorResolvedError` branch.
- Narrowed `except Exception` in `check_universal_admin_in_roles` to
  `(DatabaseError, RuntimeError)`; broader exceptions now log at
  DEBUG rather than being silently swallowed.
- Dropped per-instance resolver cache + `setting_changed` receiver
  in `ActorMiddleware`. `get_actor_resolver()` is cheap and
  `app_settings` already invalidates on settings changes.

### Deprecated

- `rebac.actors.accessible_cached` — alias for the evaluator's
  `accessible()`; emits `DeprecationWarning` (once per process).
- `rebac.actors.enable_accessible_cache` /
  `rebac.actors.disable_accessible_cache` — aliases for
  `evaluator_scope()` enter/exit. Same single-shot
  `DeprecationWarning` pattern. **Removed in 0.5** alongside the
  denormalized storage path.

### Documentation

- `ARCHITECTURE.md` gains "Storage modes" (proposal 0001) and
  "Per-request evaluator + Zookie freshness" (proposal 0002)
  sections.
- `ARCHITECTURE.md` and `docs/ZED.md` reference `REBAC_ANONYMOUS_TYPE`
  consistently with the new spec.
- `README.md` highlights bullets for the storage modes and the
  GraphQL/WebSocket-aware evaluator + Zookie freshness.
- Two new proposal docs landed under `docs/proposals/`.

### Stats

353 tests pass (up from 240 at 0.2.0). 113 new tests across the cycle.

## [0.2.0]

Prior releases — see git history.
