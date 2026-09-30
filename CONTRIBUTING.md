# Contributing

## Local setup

- Python 3.14+
- `uv` installed

```bash
uv venv --python 3.14
make install-dev
```

## Canonical commands

```bash
make lint
make test
make test-parallel
make test-index
make check
```

## Test and lint policy

- Run `make check` before opening a PR: Ruff lint and formatting, strict mypy,
  Pyright, and the default test suite. Development setup includes optional
  caveat, DRF, and Strawberry dependencies so their tests run too.
- `make test` and `make check` keep normal ordering and run serially for debugging.
  `make test-parallel` runs the default suite with `-n auto --dist loadfile`,
  matching CI and keeping each test module on one worker. To check isolation
  under reproducible random ordering, run
  `uv run --no-sync pytest -q -n auto --dist loadfile -p randomly --randomly-seed=137`.
  Random ordering is disabled by default; use `-n 0` or `-p no:xdist` to debug serially.
- Default pytest configuration deselects `index_exhaustive`, `slow` and
  `schema_vendors`. `index_exhaustive` selects the complete reference sweep;
  `index_shard` labels independently schedulable cases within it. Default runs
  retain every named regression, all one/two-leaf expressions and deterministic
  samples of larger trees. A default pass alone does not satisfy the full gate.
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

```bash
make test-index             # all tests/test_index_*.py, default marker selection
make test-index-reference   # all reference tests, including the full exhaustive sweep
```

The full reference sweep is opt-in because it crosses every expression tree
through four leaves with the specified actor/resource universes, contexts and
instants. It uses `--dist load` to distribute parametrized cases; `loadfile`
would put the entire sweep on one worker. To run only exhaustive shards:

```bash
uv run --no-sync pytest tests/test_index_reference.py -m index_exhaustive -n auto --dist load
```

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

For PostgreSQL, supply a disposable database and a role allowed to create test
databases. Pytest-django adds worker-specific test database names. The settings
module fails if the URL is absent; it never silently uses SQLite.

```bash
uv pip install 'psycopg[binary]>=3.2,<4'
export REBAC_TEST_POSTGRES_URL='postgresql://postgres:rebac-test@127.0.0.1:5432/rebac_test'
make test-index-postgres    # index suites, including PostgreSQL locking cases
make test-postgres          # default suite, including all ported behavioral tests
```

These targets select `tests.settings_postgres`. The `postgresql` marker is a
requirement label for transaction/concurrency cases, not an opt-in replacement
for running ordinary index and behavioral tests on PostgreSQL.

Disposable vendor contracts require Docker and both native drivers:

```bash
uv pip install 'psycopg[binary]>=3.2,<4' mysqlclient
make test-schema-vendors
```

The fixture owns ephemeral PostgreSQL 16/MySQL 8 containers and removes them on
exit. MySQL 8 remains an opt-in release gate, including identity codecs and the
streamed `bulk_create` path, unsigned integer bounds and same-table source snapshots. PostgreSQL CI does not replace this gate.

For a release, run `make check`, the full reference sweep, the PostgreSQL suite,
the vendor contracts, and a randomized default parallel run with seed 137.
Record actual measurements for rebuild time, SQL shape, index row counts,
maintenance fan-out and streamed Python rows and formula work. Written assertions are not results.

## CI matrix

CI validates:

- Python 3.14
- Django 6.0
- Parallel SQLite and PostgreSQL 16 default suites, including the index and
  ported behavioral suites. PostgreSQL uses a service container, `psycopg`,
  `tests.settings_postgres`, and `REBAC_TEST_POSTGRES_URL`.
- The full reference sweep and Docker PostgreSQL/MySQL vendor contracts are
  explicit local release gates. A SpiceDB conformance job (`-m spicedb` against a pinned
  `serve-testing` container) is planned after 0.23.0.

## Commit hygiene

- Keep changes focused and atomic.
- Add/update tests for behavior changes.
