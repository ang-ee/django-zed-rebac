# Changelog

All notable changes to `django-zed-rebac` are tracked here. The project is in
pre-1.0; breaking changes within a minor version are explicitly called out.

## [Unreleased]

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
