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
make check
```

## Test and lint policy

- Run `make check` before opening a PR: Ruff lint and formatting, strict mypy,
  Pyright, and the complete test suite. Development setup includes optional
  caveat, DRF, and Strawberry dependencies so their tests run too.
- `make test` and `make check` keep normal ordering and run serially for debugging.
  `make test-parallel` runs the complete suite with `-n auto --dist loadfile`,
  matching CI and keeping each test module on one worker. To check isolation
  under reproducible random ordering, run
  `uv run --no-sync pytest -q -n auto --dist loadfile -p randomly --randomly-seed=137`.
  Random ordering is disabled by default; use `-n 0` or `-p no:xdist` to debug serially.
- Pytest-django owns worker database isolation (SQLite is in memory per process).
  File-backed database fixtures and generated files must use `tmp_path` or
  `tmp_path_factory`; restore registry changes and process-wide caches between
  tests. Disposable vendor Docker fixtures use unique container names and
  Docker-assigned host ports. Opt in with `REBAC_TEST_SCHEMA_VENDORS=1` and install
  the PostgreSQL/MySQL Python drivers to exercise those contracts.
- Migrations under `tests/testapp/migrations/` are generated fixtures. We do not require import sorting or mutable-class-attribute lint rules there.
- Prefer regenerating test migrations when model shape changes, rather than hand-editing generated structures.

## CI matrix

CI validates:
- Python 3.14
- Django 6.0
- Parallel SQLite integration tests; PostgreSQL/MySQL schema contracts are opt-in
  locally, and cross-backend coverage remains a target.

## Commit hygiene

- Keep changes focused and atomic.
- Add/update tests for behavior changes.
