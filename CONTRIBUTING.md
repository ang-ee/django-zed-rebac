# Contributing

## Local setup

- Python 3.14+
- `uv` installed

```bash
uv venv --python 3.14
make install-dev
```

## Commands

The tiers are specified in
[ARCHITECTURE.md § Test tiers](./docs/ARCHITECTURE.md#test-tiers). A change
runs the fix loop and tier 1, and tier 2 when it touches SQL generation,
transactions, locking or migrations. Tier 3 runs nightly and before a release.

```bash
# Fix loop and tier 1
make test-fast              # last failures first, stop at the first failure
make check                  # tier 1: ruff lint and format, mypy, pyright, then the SQLite
                            # suite in parallel, stopping at the first failure
make test                   # the tier 1 selection, serial, for debugging

# Tier 2: the PostgreSQL delta
make pg-up                  # prints: export REBAC_TEST_POSTGRES_URL=...; run that line
eval "$(make -s pg-up)"     # the same, exported in one step
make test-pg                # tests marked postgresql or pg_delta, slow excluded
make pg-down                # remove the container

# Tier 3: before a release, never inside a change
make test-release           # every part below in sequence, then a pass/fail summary
make test-slow              # slow, on SQLite
make test-postgres          # the whole suite including slow, on PostgreSQL
make test-scale             # time, statement and plan budgets, alone, on SQLite
make test-scale-postgres    # the same on PostgreSQL
make test-index-reference   # the full reference sweep
make test-schema-vendors    # Docker PostgreSQL 16 and MySQL 8 contracts
make test-random            # the tier 1 selection in random order, seed 137

# Focused runs, not a tier
make test-index             # tests/test_index_*.py with the default selection
make test-index-postgres    # the same on PostgreSQL
make test-pg ARGS=tests/test_index_read.py      # ARGS adds pytest arguments to any target
uv run --no-sync pytest -m slow --timeout=300 tests/test_x.py::test_y   # one slow test
uv run --no-sync pytest tests/test_index_reference.py -m index_exhaustive \
    -n auto --dist load --timeout=300           # only the exhaustive shards

# The nightly Release workflow: tier 3 on GitHub
gh workflow run release.yml                     # start a run on main now
gh workflow run release.yml --ref <branch>      # or on another branch
gh run list --workflow release.yml --limit 5    # recent runs and their conclusions
gh run watch <run-id>                           # follow a run until it ends
gh run view <run-id>                            # the result of each job
gh run view <run-id> --log-failed               # the log of each failed step
```

- `make pg-up` needs Docker. It starts `postgres:16` as `rebac-test-pg` on a
  Docker-assigned port with the credentials CI uses, waits until it accepts
  connections and prints the export line; run again, it reuses the container
  and prints the same line. The data lives in memory and is gone after
  `make pg-down`.
- Any other disposable PostgreSQL works: point `REBAC_TEST_POSTGRES_URL` at a
  database whose role may create databases. Pytest-django adds worker-specific
  test database names. `tests.settings_postgres` fails if the URL is absent;
  it never uses SQLite.
- The PostgreSQL targets need the driver:
  `uv pip install 'psycopg[binary]>=3.2,<4'`. The vendor contracts need Docker
  and both drivers: `uv pip install 'psycopg[binary]>=3.2,<4' mysqlclient`.
  Each target checks its prerequisites first and fails naming what is missing;
  none of them skips.
- Every test has a 10-second timeout, fixtures included. The targets that
  select `slow`, `scale`, `index_exhaustive` or `schema_vendors` pass
  `--timeout=300`; `make test-pg` passes `--timeout=60`, because the timeout
  counts each worker's test-database creation on PostgreSQL. An explicit `-m` replaces the default marker expression, so
  a `slow` test run by node id needs `-m slow` as well as the longer timeout.
- `make test-release` does not stop at a failing part. It prints each part's
  result and time and exits non-zero if any part failed; without
  `REBAC_TEST_POSTGRES_URL`, Docker or the drivers, the parts that need them
  fail. Tier 1 is not part of it: run `make check` as well.
- On GitHub each tier 3 part is its own job, and the reference sweep is split
  across six jobs by expression shape. `gh run view` lists every job with its
  result; a failed job's log names the failing tests.

## Test and lint policy

The three tiers, their budgets and the rules for placing a test are specified
in [ARCHITECTURE.md § Test tiers](./docs/ARCHITECTURE.md#test-tiers). In
practice:

- **While fixing:** run the tests you touched by node id, or `make test-fast`.
- **Before every commit and PR:** `make check`. It must stay under a minute
  locally. Development setup includes optional caveat, DRF, and Strawberry
  dependencies so their tests run too.
- **When the change touches SQL generation, transactions, locking or
  migrations:** also `make test-pg`.
- **Before a release, not before a commit:** `make test-release`, or read the
  nightly run on `main`.
- A new test that takes more than 2 seconds is marked `slow`, leaving a small
  representative case unmarked. A test that needs a particular vendor skips
  on the others; it never returns early.
- `make test` runs the tier 1 selection serially, for debugging.
  `make test-random` checks isolation under a reproducible random order.
- Default pytest configuration deselects `index_exhaustive`, `slow`,
  `schema_vendors` and `scale`. `scale` holds time, statement and query-plan
  budgets, which run alone (`make test-scale`, `make test-scale-postgres`)
  because parallel workers distort them. `index_exhaustive` selects the complete reference sweep;
  `index_shard` labels independently schedulable cases within it. Default runs
  retain every named regression, all one/two-leaf expressions and deterministic
  samples of larger trees. A default pass alone does not satisfy the release gate.
- Pytest-django owns worker database isolation (SQLite is in memory per process).
  File-backed database fixtures and generated files must use `tmp_path` or
  `tmp_path_factory`; restore registry changes and process-wide caches between
  tests. Disposable vendor Docker fixtures use unique container names and
  Docker-assigned host ports. Install the PostgreSQL/MySQL drivers and use
  `make test-schema-vendors` to exercise those contracts.
- SpiceDB conformance tests (planned after 0.23.0; see ARCHITECTURE.md
  § SpiceDB conformance suite) will run with `pytest -m spicedb`. They start a
  pinned `spicedb serve-testing` container through Docker, or use
  `REBAC_TEST_SPICEDB_ENDPOINT`. Selecting the marker without either is an
  error, not a skip.
- Migrations under `tests/testapp/migrations/` are generated fixtures. We do not require import sorting or mutable-class-attribute lint rules there.
- Prefer regenerating test migrations when model shape changes, rather than hand-editing generated structures.

## Permission index suites

The full reference sweep (`make test-index-reference`) is opt-in because it
crosses every expression tree through four leaves with the specified
actor/resource universes, contexts and instants. It uses `--dist load` to
distribute parametrized cases; `loadfile` would put the entire sweep on one
worker. [Commands](#commands) has the invocation for the exhaustive shards
alone.

Explicit `-m` overrides the default marker expression. The complete sweep must
run before release; do not reduce its universe or weaken assertions to shorten
it. Tests compare the actual index reader with the frozen walker and the
independent reference model. The coverage matrix and required measurements
are in [ARCHITECTURE.md](./docs/ARCHITECTURE.md#permission-index-suites).

Expression cases split into 32 shards per shape/universe/instant/context;
direct-tuple powersets split into 16 shards. Proposed timing budgets are a
60-second default reference gate, 120 seconds per full shard on one worker,
and 60 minutes for the full sweep on 16 workers. These are targets to measure,
not measured performance or permission to trim coverage.

The PostgreSQL targets select `tests.settings_postgres`. The `postgresql`
marker is a requirement label for transaction/concurrency cases, not an opt-in
replacement for running ordinary index and behavioral tests on PostgreSQL.

The vendor contract fixture owns ephemeral PostgreSQL 16/MySQL 8 containers and
removes them on exit. MySQL 8 remains an opt-in release gate, including identity codecs and the
streamed `bulk_create` path, unsigned integer bounds and same-table source snapshots. PostgreSQL CI does not replace this gate.

Tier 3 (`make test-release`) is the `slow` tests, the whole suite on
PostgreSQL, the scale budgets on both vendors, the full reference sweep, the
vendor contracts, and a randomized default parallel run with seed 137. Run it
before a release, not before a commit. Record actual measurements for rebuild time, SQL shape, index row counts,
maintenance fan-out and streamed Python rows and formula work. Written assertions are not results.

## CI matrix

Python 3.14 and Django 6.0, in three workflows:

- **CI** (`.github/workflows/ci.yml`) runs on pushes to `main`, on version
  tags and on pull requests, as two parallel jobs. `test (3.14, 6.0)` runs
  `make check`: tier 1, and the check branch protection requires.
  `postgres-delta` runs `make test-pg` against a `postgres:16` service
  container: tier 2.
- **Release** (`.github/workflows/release.yml`) runs tier 3 nightly on `main`
  at 03:17 UTC and on demand, one job per part: `slow` on SQLite, the whole
  suite on PostgreSQL 16, the scale budgets on each vendor, the reference
  sweep as six jobs split by expression shape, the Docker PostgreSQL/MySQL
  vendor contracts, and the random-order run. Its result on `main` is the
  release evidence; [Commands](#commands) shows how to start and read it with
  `gh`.
- **Publish to PyPI** runs after CI completes on a `v*` tag and publishes when
  the `test (3.14, 6.0)` job passed. It reads that job's result, because the
  conclusion of the CI run also covers `postgres-delta`; neither tier 2 nor
  Release holds a tag back. It does not run the tests again: it verifies that
  the tag, `pyproject.toml` and the installed package agree, builds, checks
  and uploads. Run it by hand with a tag to publish again.

A SpiceDB conformance job (`-m spicedb` against a pinned `serve-testing`
container) is planned after 0.23.0.

## Commit hygiene

- Keep changes focused and atomic.
- Add/update tests for behavior changes.
