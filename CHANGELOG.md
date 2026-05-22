# Changelog

All notable changes to `django-zed-rebac` are tracked here. The project is in
pre-1.0; breaking changes within a minor version are explicitly called out.

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
- **Default flips to `"registry"` in v0.4**; `"denormalized"` removed
  in v0.5.

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
