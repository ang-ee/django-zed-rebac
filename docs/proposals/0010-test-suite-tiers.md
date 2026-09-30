# Proposal 0010: a tiered test suite, with the security and coverage gaps closed

## Problem

A change takes about two hours to verify, and the suite still misses real
defects.

**Time.** Measured on 0.23.2 (18-core laptop; GitHub runner in brackets):

| | |
|---|---|
| Default suite, serial (`make check`) | 33 min |
| Default suite, parallel with work stealing | 2 min 8 s [16 min 25 s with `loadfile`] |
| Same suite on PostgreSQL 16 | [25 min 30 s] |
| Reference sweep (10,512 cases) | target 60 min on 16 workers |

The verification chain in `CLAUDE.md` asked for all of these on every change.
Three causes account for nearly all of it:

- `make check` runs the suite serially.
- `--dist loadfile` pins `test_recursive_queryscope.py`, a third of all test
  time, to one worker; CI's wall time is that file.
- 63 tests of 5 s or more take 39% of the time, 212 of 2 s or more take 61%.
  They are depth-50 and cost-independence tests multiplied across parameter
  matrices. The PostgreSQL job re-runs all of them although 12 tests are
  PostgreSQL-specific.

**Protection.** Line coverage of the default suite is 90.3% (branches 3,046 of
3,574), but a review against the contract found behaviour that reads as
fail-open and has no test, and whole surfaces with none:

- `ActorMiddleware` decides the superuser bypass from `request.user`, not from
  the actor the resolver returned.
- `RebacPermission.has_object_permission` allows when `to_object_ref` raises.
- Queryset and instance `.sudo()` write no audit row.
- A caveat parameter passed as `None` counts as supplied.
- An empty MCP `id_arg` becomes `ObjectRef(type, "")`.
- A pickled `RebacQuerySet` keeps its sudo reason and actor.
- `grant_subject_ref` builds an id that can collide.
- `rebac.admin` (0% covered), the `check` and `explain` commands, six system
  checks and the DRF adapter against real DRF have no tests.
- The exhaustive sweep compares the walker with the reference model; it never
  reaches the production index.

## Rule

The tier contract is in [ARCHITECTURE.md § Test tiers](../ARCHITECTURE.md#test-tiers).
This proposal implements it and adds one rule:

**A test for a defect lands before the fix, as a strict expected failure.** It
is marked `xfail(strict=True, reason="<what is wrong>")`. The gate stays green
while the defect is open; the fix cannot land without removing the mark, because
a strict expected failure that passes is a failure. Fixing the defects is not
part of this proposal: each is a behaviour change with its own spec entry.

## Design

### 1. Markers and selection (`pyproject.toml`)

| Marker | Meaning | Default run |
|---|---|---|
| `slow` | Over 2 s locally. Full matrices, depth and corpus tests. | deselected |
| `pg_delta` | Runs on every vendor; PostgreSQL may disagree. | selected |
| `postgresql` | Requires PostgreSQL; skips elsewhere. | selected (skips) |
| `scale` | Budgets measured alone. | deselected |
| `index_exhaustive` | Full reference sweep. | deselected |
| `schema_vendors` | Docker PostgreSQL/MySQL contracts. | deselected |

The per-test timeout drops from 300 s to 10 s for the default selection;
targets that select `slow`, `scale` or `index_exhaustive` pass `--timeout=300`.

A heavy parametrized test is split, not moved: the smallest meaningful
parameter set stays unmarked and the rest carries `slow` through
`pytest.param(..., marks=pytest.mark.slow)`. Depth 50 becomes a `slow` case
beside a depth 3 case of the same test. No universe shrinks and no assertion
weakens.

`pg_delta` is seeded from: every test that branches on `connection.vendor`;
one test per SQL-emitting area (recursive scopes, index reads, index
maintenance, schema write owners, migrations, streamed `bulk_create`); and the
tests that failed only on PostgreSQL in CI runs 36662724583, 36663784437 and
36666224411.

### 2. Commands (`Makefile`)

| Command | Tier | What it runs |
|---|---|---|
| `make test-fast` | loop | last failures first, stop at first failure |
| `make check` | 1 | lint, format, mypy, pyright, then `pytest -n auto --dist worksteal -x` |
| `make test` | 1 | the same selection, serial, for debugging |
| `make test-pg` | 2 | `-m "postgresql or pg_delta"` without `slow`, on `tests.settings_postgres`, `--timeout=60` |
| `make test-slow` | 3 | `-m slow` on SQLite |
| `make test-postgres` | 3 | default selection plus `slow` on PostgreSQL |
| `make test-scale`, `make test-scale-postgres` | 3 | budgets, alone |
| `make test-index-reference` | 3 | the full sweep |
| `make test-schema-vendors` | 3 | Docker PostgreSQL/MySQL contracts |
| `make test-random` | 3 | default selection, `-p randomly --randomly-seed=137` |
| `make test-release` | 3 | all of tier 3 in sequence, continuing past a failure and reporting each part; run `make check` separately |
| `make pg-up`, `make pg-down` | — | start and remove a disposable `postgres:16` container and print the `REBAC_TEST_POSTGRES_URL` to export |

`make test-parallel` is removed; `make check` replaces it.

### 3. Workflows (`.github/workflows`)

- **`ci.yml`**: two parallel jobs on pushes to `main`, tags and pull requests.
  `test (3.14, 6.0)` keeps its name (branch protection) and runs `make check`.
  `postgres-delta` runs `make test-pg` against a service container.
- **`release.yml`**: nightly on `main` and `workflow_dispatch`. Independent
  jobs: slow SQLite, full PostgreSQL, scale SQLite, scale PostgreSQL, reference
  sweep, vendor contracts, randomized order.
- **`postgres.yml`** is deleted; its jobs move to `release.yml`.
- **`publish-pypi.yml`** still triggers when `ci.yml` completes, but a workflow's
  conclusion now covers both jobs. It reads the result of the
  `test (3.14, 6.0)` job alone and publishes when that job succeeded, so a
  tier 2 failure does not block a tag.
- The reference sweep runs as six jobs in `release.yml` (one for everything
  below four leaves, one per four-leaf shape); on one runner it would take
  about four hours.

### 4. Defect tests (strict expected failures)

New files, so they do not collide with existing suites:

| File | Cases |
|---|---|
| `tests/test_security_middleware_bypass.py` | superuser `request.user` with a resolver returning a different actor, sync and async: not sudo, queryset scoped |
| `tests/test_security_adapters.py` | DRF object permission on an unresolvable instance; MCP `id_arg=""`; `create_relations` with `"type:"`; non-canonical ids (`"01"`, `" 1"`, `"+1"`) against a type-level grant with a concrete ban |
| `tests/test_security_sudo_audit.py` | queryset `.sudo()`, `.system_context()` and instance `.sudo()` each write a `sudo.bypass` row; the row survives an outer rollback |
| `tests/test_security_caveat_none.py` | a declared parameter passed as `None` yields CONDITIONAL on `check_access`, `accessible` and a scoped queryset |
| `tests/test_security_pickle_queryset.py` | a pickled sudo or actor-pinned queryset restores without either |
| `tests/test_security_related_writes.py` | reverse-FK and M2M `add`/`remove`/`set`/`clear` by a read-only actor and with no actor; `Relationship.objects` delete then check; `update(x=F(gated))`; `refresh_from_db()` on a redacted instance; bulk-guard message carries no foreign ids |
| `tests/test_security_strict_terminals.py` | every terminal queryset method without an actor in strict mode raises `MissingActorError`, parametrized |
| `tests/test_security_identity.py` | `grant_subject_ref` collisions; override naming an undefined relation is refused; `has_module_perms` with only expired, caveated or banned rows |

A case whose current behaviour turns out correct lands as an ordinary passing
test. A case that fails is `xfail(strict=True)` with the defect named.

### 5. Coverage tests

| File | Cases |
|---|---|
| `tests/test_admin.py` (settings variant with admin, sessions, messages) | changelists load; relationship and audit admins are read-only; override add sets `created_by` and audits |
| `tests/test_commands_check_explain.py` | `rebac check` ok and parse error; `rebac explain` hit, miss, with an override |
| `tests/test_public_api.py` | every name in `rebac.__all__` resolves; error hierarchy; documented public list matches |
| `tests/test_backend_selection.py` | `REBAC_BACKEND` `spicedb` without client or endpoint, unknown value |
| `tests/test_checks_and_conf.py` | E001, E002, E006, W001, W005, W101 triggered and silent; the W006 test fixed |
| `tests/test_parser.py` | `-` associativity and precedence against `&`; block comments; unterminated string, comment, caveat body; duplicate relation or permission in a definition |
| `tests/test_build_zed.py` | through `call_command`; output re-parses; two subprocesses with different `PYTHONHASHSEED` give identical bytes; `use expiration` emitted |
| `tests/test_drf.py` | a `ModelViewSet` through `APIRequestFactory`: list, retrieve, create, update, destroy |
| `tests/test_caveats.py` | one caveat per parameter type; compile error; runtime error |
| `tests/test_permissions_mixin.py`, `tests/test_auth_backend.py` | `get_*_permissions`; the async methods |

The same rule applies: a test that exposes a defect is a strict expected
failure.

### 6. Remaining churn

- Merge the `actor` axis of `test_positive_recursion_has_no_read_depth_limit`
  into one loop per chain (24 cases to 8; the chain and grant are identical).
- Make `test_tuple_maintenance_and_rebuild_use_write_alias` run with
  `tests.backend_setup.sqlite_alias`; it skips today because no second alias
  exists.

Not done here, each needs a comparison run first: dropping the storage axis from
tests that never touch relationship rows, and the six registry parity tests in
`test_local_backend_registry.py`.

## Verification

Commands, in the order a contributor meets them. Only the first two belong in a
change.

```bash
make install-dev                      # once

make test-fast                        # fix loop
make check                            # tier 1, under a minute

make pg-up                            # prints: export REBAC_TEST_POSTGRES_URL=...
make test-pg                          # tier 2, under three minutes
make pg-down

make test-release                     # tier 3, about 12 minutes on 18 cores; nightly in CI
gh workflow run release.yml           # the same, on GitHub
gh run list --workflow release.yml --limit 1
```

Acceptance:

- `make check` passes in under 60 s locally and its pytest step in under 5 min
  in CI.
- `make test-pg` passes in under 3 min.
- `pytest --collect-only -q -m "slow or not slow"` collects at least as many
  cases as before the change minus the churn removed: nothing is lost to a tier.
- `pytest --durations=20` on the default selection shows no test over 2 s.
- Every new `xfail` is strict and names its defect.

Measured on the branch (18-core laptop, PostgreSQL 16 in Docker):

| Command | Result | Time |
|---|---|---|
| `make check` | 2,909 passed, 4 skipped, 63 expected failures | 66 s |
| `make test-pg` | 92 passed, 2 skipped | 13 s |
| `make test-slow` | 140 passed | 45 s |
| `make test-postgres` | 3,051 passed, 2 skipped, 63 expected failures | 227 s |
| `make test-scale` / `test-scale-postgres` | 4 passed, 1 skipped / 5 passed | 12 s / 21 s |
| `make test-index-reference` | 10,575 passed | 352 s |
| `make test-random` | 2,909 passed, 4 skipped, 63 expected failures | 73 s |
| `make test-schema-vendors` | not run: `mysqlclient` needs the MySQL client libraries | — |

## Not in scope

- Fixing the defects the new tests expose.
- Pointing the exhaustive sweep at the production index. It is the right next
  step and changes what the sweep costs; it needs its own proposal.
- Reducing fixture setup cost (16% of test time: the autouse index build and
  `transaction=True` modules).
- Whether tier 3 should gate publishing. It does not: a tag publishes on tier 1.
