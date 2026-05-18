# Changelog

All notable changes to `django-zed-rebac` are tracked here. The project is in
pre-1.0; breaking changes within a minor version are explicitly called out.

## [Unreleased] — 0.4 (proposal 0001: registry storage)

### Added

- **Two LocalBackend storage shapes.** `REBAC_LOCAL_BACKEND_STORAGE`
  selects between:
  - `"denormalized"` (default in 0.4) — the historical four-CharField
    `Relationship` shape.
  - `"registry"` (default in 0.5) — `RelationshipRegistry` storing
    `resource_fk` / `subject_fk` as integer FKs into a shared
    `RebacResource` table. ~5-10x index-density gain on the hot path
    plus FK-CASCADE cleanup when the backing Django row is deleted.
- New models `rebac.models.RebacResource` and `rebac.models.RelationshipRegistry`
  + manager `RelationshipRegistryManager` (string-kwarg translation).
- New helper `rebac.models.active_relationship_model()` returns the model
  selected by the storage-mode setting. All engine code (`LocalBackend`,
  `rebac.relationships`, `rebac.roles`) routes through this helper, so the
  storage flip is a settings change — not a code change.
- New management subcommand:
  ```bash
  python manage.py rebac migrate-storage --to registry [--from denormalized] \
      [--batch 5000] [--dry-run]
  ```
  Copies rows between the two shapes; idempotent re-runs; row-count parity
  check at the end.
- New setting `REBAC_LOCAL_BACKEND_REGISTRY_BATCH_SIZE` (default `5000`)
  controls batch size for `migrate-storage`.
- New system checks:
  - `rebac.E006` — `REBAC_LOCAL_BACKEND_STORAGE` must be `"denormalized"`
    or `"registry"`.
  - `rebac.W005` — surfaces a "consider migrating" warning when the
    setting is `"denormalized"`. Silence with `SILENCED_SYSTEM_CHECKS = ["rebac.W005"]`
    if the warning is noise for the deployment.
- New cascade signal handler `_rebac_cascade_resource` (registry mode
  only) — drops the matching `RebacResource` row when a `RebacMixin`-
  bearing Django row is deleted, so the FK CASCADE sweeps every tuple
  the resource appeared in.

### Fixed

- **`build-zed` emitter no longer drops `AllowedSubject.id`.** The
  emitter rendered `angee/role:admin#member` as `angee/role#member`
  (members of any role) and produced non-deterministic ordering for two
  subjects differing only in `id`. Both regressions are now pinned by
  test_build_zed.py.
- Parser emits a clearer `ParseError` when a specific-id in a subject
  term isn't identifier-shaped (`role:42`, `role:obj-admin`,
  `role:sub/admin`). The constraint is documented on
  `AllowedSubject.id`.
- `_builtin_actor_matches` in `LocalBackend` now delegates to
  `actors.is_anonymous_actor` instead of reimplementing the predicate
  inline.
- `to_subject_ref` raises `NoActorResolvedError` for a user-model
  instance with `is_authenticated=False` instead of silently
  downgrading to the anonymous actor. The request-path resolver
  (`default_resolver`) still fails safe via its existing
  `except NoActorResolvedError` branch.
- Narrowed `except Exception` in `check_universal_admin_in_roles` to
  `(DatabaseError, RuntimeError)` (RuntimeError is required for
  pytest-django's DB access guard); broader exceptions now log at
  DEBUG rather than being silently swallowed.
- Dropped per-instance resolver cache + `setting_changed` receiver in
  `ActorMiddleware`. `get_actor_resolver()` is cheap and
  `app_settings` already invalidates on settings changes.

### Documentation

- `ARCHITECTURE.md` gains a "Storage modes" section covering the
  two shapes, when to use each, the migration command, and the 0.5
  default flip.
- `ARCHITECTURE.md` and `docs/ZED.md` now reference `REBAC_ANONYMOUS_TYPE`
  consistently with the new spec instead of describing untyped
  `anonymous:*`.
- `README.md` highlights gain a bullet for the storage modes.

### Rollout plan

- **0.4** (this release) ships the new tables + manager + migration
  command, default `"denormalized"`. Existing deployments see no
  behaviour change unless they opt in. `rebac.W005` surfaces for
  everyone, encouraging migration.
- **0.5** flips the default to `"registry"`. Operators who haven't
  migrated get a `rebac.E007` error at startup pointing at the
  migration command.
- **0.6** drops the denormalized code path entirely. The `Relationship`
  model is removed; `RelationshipRegistry` is renamed to `Relationship`.

## [0.2.0]

Prior releases — see git history.
