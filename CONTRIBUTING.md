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
make check
```

## Test and lint policy

- Run `make check` before opening a PR: Ruff lint and formatting, strict mypy,
  Pyright, and the complete test suite. Development setup includes optional
  caveat, DRF, and Strawberry dependencies so their tests run too.
- Migrations under `tests/testapp/migrations/` are generated fixtures. We do not require import sorting or mutable-class-attribute lint rules there.
- Prefer regenerating test migrations when model shape changes, rather than hand-editing generated structures.

## CI matrix

CI validates:
- Python 3.14
- Django 6.0
- SQLite integration tests; PostgreSQL and cross-backend coverage remain targets.

## Commit hygiene

- Keep changes focused and atomic.
- Add/update tests for behavior changes.
