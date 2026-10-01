# ROADMAP

## 1.0: integrate at Django's ORM, not at the consumer's models

The engine has the shape it will keep: the SpiceDB schema, permissions
compiled to queries over the application's own tables
([proposal 0015](./proposals/0015-permissions-compiled-to-queries.md)),
strict-by-default scoping, audited bypass, and three test tiers. What is not
fixed is the layer that connects the engine to Django. That layer attaches to
each model
(`RebacMixin`, `RebacTrackedMixin`) and then chases every other way Django
touches a row: patched related managers, a replaced through-model manager,
signals on third-party models, collector hooks, twelve `ContextVar`s. Four
fix rounds in 0.24.0 each closed some write paths and opened others, because
the gate re-implements Django's query semantics in Python and the engine
never sees the statement Django is about to run. The remaining holes are
pinned as strict expected failures (`tests/test_security_proposal_0011.py`,
`_0012.py`, `_0013.py`) and are the acceptance suite for this release.

1.0 moves the integration to the one place every ORM statement passes
through, Django's SQL compilers, and removes everything that existed to
work around not being there. It is one breaking migration for consumers,
so the storage and identity changes ride with it.

### Order of work

1. **Proposal 0013 spike: read scope at the compiler.** A `rebac.db.backends.postgresql`
   wrapper whose select compiler adds the scope predicate for the base-table
   alias only. Gate: the SQL is byte-identical with `RebacQuerySet` on
   `tests/test_queryset_permission_parity.py` and `tests/test_scope_*.py`,
   SQLite and PostgreSQL. If it is not, the design is wrong at the cheapest
   point to find out. Days, not weeks.
2. **Read scope for every alias.** Joined tables, `Subquery`, `Exists`,
   combined queries, prefetches; field read gates as `NULL` projection of
   gated columns. The scope of an alias is the compiled predicate at that
   alias's identity column (proposal 0015 § 9). `select_related` and bare
   `prefetch_related` become safe; W003 is retired.
3. **Writes at the insert, update and delete compilers.** One gate computed
   from the statement's `Query`: frozen rows as a subquery, written values
   evaluated by the database, the backed-edge rule of invariant 5d applied
   once (proposal 0011's rule).
   Delete the related-manager patches, the through-manager swap, the
   `_base_manager` injection, the tracked signals, `RebacTrackedMixin`,
   `REBAC_TRACKED_MODELS` and the write overrides on `RebacMixin` and
   `RebacQuerySet`. Each pinned test in
   `test_security_proposal_0011.py` and `_0013.py` turns green as its path
   moves; that is the progress meter. Nothing is maintained after a write, so
   this step exists for the gates alone; how far it goes follows the decision
   on the gates (proposal 0015 § 13).
4. **The actor carrier.** `with_actor()` stores the actor on the `Query`
   (`_hints`), so a pinned actor still outranks the ambient one and reaches
   subqueries and prefetches; `actor_context()` and the middleware stay the
   ambient source; `RebacMixin` keeps `check_access`, `with_actor` and `sudo`
   as conveniences and becomes optional. Opt-in moves to the schema: a model
   is a resource because the `.zed` schema declares its type.
5. **Single storage.** Drop registry mode (`RelationshipRegistry`,
   `RebacResource`, the lookup-rewriting queryset, `migrate-storage`, W005,
   `REBAC_LOCAL_BACKEND_STORAGE`) and the storage axis in tests. One source
   table in wire form; the migration refuses to run while registry rows
   exist.
6. **Identity as a stored column: dropped.** A compiled statement never
   converts a model column; a tuple column is converted where the two meet
   (proposal 0015 § 2), so no resource model needs a stored wire id.
7. **One bypass primitive** with an explicit audit flag and reason, replacing
   the `sudo`/`system_context` block, queryset and instance variants and the
   engine's reason-string captures. **Overrides as AST operations** on named
   nodes, validated at save time, with stale narrowing overrides failing
   closed per name (proposal 0012's override half).
8. The SpiceDB conformance suite remains planned in parallel with the above;
   it does not touch the compilers.

### What 1.0 does not change

The schema language and SpiceDB wire compatibility, the permission compiler
(`rebac.compile`) and what the library stores, the audit event table,
the DRF, MCP and GraphQL adapters, the system-check framework, and the test
tiers. `raw()`, `extra()` and `cursor.execute` stay outside the compilers
and stay refused under scope.

### Consumer migration, once

Set `DATABASES[...]["ENGINE"]` to the REBAC wrapper (a check, `rebac.E022`,
fails startup if a resource model's database is not wrapped); run the storage
migration; remove `RebacTrackedMixin` and `REBAC_TRACKED_MODELS`;
drop `with_actor` from `select_related`/`prefetch_related` workarounds.

### Measurements that decide it

- Spike parity: byte-identical scope SQL on the parity suites.
- Write cost: one gate query per affected declaring type per statement; a
  100,000-row update on SQLite and PostgreSQL completes within a budget still
  to be set (there is no scale suite at the moment).
- Query plans of compiled scopes on a PostgreSQL database of tens of millions
  of rows (proposal 0015, gate G1): under trial, not verified.
- The 0.24.0 probe corpus (`scratchpad` rounds r3 to r7, folded into
  `tests/test_security_*.py`): every case denies.
- `make test-release` green on SQLite, PostgreSQL 16 and MySQL 8.

## Product roadmap

- [x] 0.24.0 test tiers, 49 engine fixes, pinned design gaps (proposals 0010
  to 0013).
  - Shipped surface: `make check` / `make test-pg` / `make test-release`;
    94 security tests; the fixes listed in CHANGELOG 0.24.0; strict expected
    failures for every known fail-open path, each naming its proposal.
  - Follow-ups: 1.0 above.

- [x] Permissions compiled to queries (proposal 0015; unreleased).
  - Shipped surface: `rebac.compile`; no table that holds a row per
    application row and no build step; recursion unrolled to
    `REBAC_DEPTH_LIMIT`; gated queryset writes that select, gate and write
    their rows by key; policy writes serialized on the generation row;
    migrations `0008` and `0009`. It replaces the derived permission store of
    0.23.0 with its commands, checks and settings (CHANGELOG, Unreleased).
  - Follow-ups: the trial on a consumer database at scale (gate G1); the
    decision on the write gates (proposal 0015 § 13); a scale suite; a
    recursion mechanism without a bound, if a deployment needs one.

- [x] 0.23.0: complete subject expansion, W010, reference/differential suites
  and PostgreSQL CI.

- [ ] SpiceDB conformance suite.
  - Why: the differential oracle compares the compiled reads with the
    library's own walker and reference model, which proves parity with the
    library's reading of the semantics, not with SpiceDB. SpiceDB is the
    contract (AGENTS.md invariant 1).
  - Outcome: `pytest -m spicedb` checks generated schemas and data against a
    pinned `spicedb serve-testing` container in dev and in a CI job, comparing
    `CheckPermission` (including caveat `missing_required_context`),
    `LookupResources` and `LookupSubjects`. Deliberate divergences (the depth
    bound, data cycles, E016 refusals of recursive components, expiring
    overrides, `check_new`) are listed in ARCHITECTURE and pinned by
    tests. The in-process oracle then shrinks to a spec-written reference
    model. ARCHITECTURE.md § SpiceDB conformance suite has the design.
  - Include: the test-side projector of field, attribute and constant
    backings into tuples; it seeds the `SpiceDBBackend` projector below.

- [ ] Implement `SpiceDBBackend`.
  - Why: the public backend boundary is in place, but
    `src/rebac/backends/spicedb.py` is still an explicit stub.
  - Outcome: `REBAC_BACKEND = "spicedb"` becomes a supported runtime path,
    with `authzed-py` wiring, schema push, Zookie translation, and
    cross-backend contract tests.
  - Include: a library-owned projector/reconciler for every live backing —
    forward FK, reverse/many-to-many and filtered paths declared with
    `// rebac:field=...`, attribute containers declared with
    `// rebac:attribute=...`, and const relations — so SpiceDB receives
    ordinary relationship tuples while consumers keep writing normal Django
    fields. ARCHITECTURE.md § Field-backed structural relations lists the
    projection burden per kind.

- [x] Implement MCP tool integration (0.11.0).
  - Outcome: proposal 0004 ships as `rebac.mcp.rebac_mcp_tool` with
    fail-closed actor resolution and sync/async tool support.

- [x] Decide the LocalBackend registry-storage default: resolved by 1.0 step 5,
  single storage. Registry mode stays opt-in until then and is not promoted.

## Code review follow-ups

This document captures **technical-debt and best-practice follow-ups** identified during a repository review.

## Priority 0 — Quality gates and CI hygiene

- [x] Add a default local/CI bootstrap (`make test` or `just test`) that installs required test deps before running checks.
  - Why: current test run fails in a fresh environment because `django` is not installed.
  - Outcome: contributors get deterministic one-command validation.

- [x] Tighten lint scope so tooling rules are intentional for generated Django migrations.
  - Why: Ruff currently flags test migrations for import sorting and mutable class attributes; those files are framework-generated and often should be excluded from stylistic rewrites.
  - Outcome: less noise, fewer false-positive failures, clearer signal in CI.

- [x] Add a CI matrix for Python/Django versions already declared in metadata.
  - Why: compatibility is documented broadly, but repository checks should continuously verify claims.
  - Outcome: early detection of version-specific regressions.

## Priority 1 — Packaging and repository cleanliness

- [x] Remove committed `src/django_zed_rebac.egg-info/*` from source control and add ignore rules.
  - Why: egg-info artifacts are build outputs and can drift from source of truth (`pyproject.toml`).
  - Outcome: cleaner diffs and fewer accidental release metadata mismatches.

- [x] Add `MANIFEST.in` (or explicit setuptools config) review to ensure package data is deliberate.
  - Why: the project depends on schema/runtime files and typed marker (`py.typed`); packaging should be explicit and tested.
  - Outcome: predictable wheels/sdists.

## Priority 2 — Django best-practice hardening

- [x] Add system checks validating required middleware ordering when `ActorMiddleware` is enabled.
  - Why: docs say it must be after `AuthenticationMiddleware`; an automated check prevents subtle runtime behavior bugs.
  - Outcome: safer integration by default.

- [x] Add explicit tests for settings cache invalidation (`setting_changed`) behavior.
  - Why: `app_settings` caches values; cache invalidation is critical to predictable tests and runtime overrides.
  - Outcome: protects against regressions in config behavior.

- [x] Replace internal parser dependency (`_Parser`) usage with a stable public parser API abstraction.
  - Why: relying on a private symbol creates refactor risk and hidden coupling.
  - Outcome: easier maintenance and safer parser evolution.

## Priority 3 — Documentation debt reduction

- [x] Add a dedicated `CONTRIBUTING.md` with required toolchain, setup steps, and canonical commands.
  - Why: repo has strong architecture docs but contributor workflow is implicit.
  - Outcome: faster onboarding and fewer environment-specific failures.

- [x] Document lint/test policy for migrations and generated files.
  - Why: teams need explicit guidance on when to regenerate vs manually edit migration files in tests.
  - Outcome: consistent review expectations.
