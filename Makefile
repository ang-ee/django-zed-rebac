.PHONY: install-dev test test-parallel test-fast test-index test-index-reference test-postgres test-index-postgres test-schema-vendors lint format-check typecheck check ci

INDEX_TESTS := $(wildcard tests/test_index_*.py)

install-dev:
	uv pip install -e '.[dev,caveats,drf,strawberry]'

test:
	uv run --no-sync pytest -q

test-parallel:
	uv run --no-sync pytest -q -n auto --dist loadfile --durations=25

test-index:
	uv run --no-sync pytest -q -n auto --dist loadfile --durations=25 $(INDEX_TESTS)

# Fix loop: re-run last failures first, stop at the first failure, balance work
# across workers. Use the full `test-parallel` run only at checkpoints.
test-fast:
	uv run --no-sync pytest -q -n auto --dist worksteal --lf --ff -x --durations=10

# Override the default marker expression to include every reference shard.
# loadfile would put the entire sweep on one worker; load distributes shards.
test-index-reference:
	uv run --no-sync pytest -q tests/test_index_reference.py -m 'index_exhaustive or not index_exhaustive' -n auto --dist load --durations=25

# tests.settings_postgres reads REBAC_TEST_POSTGRES_URL; no implicit SQLite fallback.
test-postgres:
	uv run --no-sync pytest -q --ds=tests.settings_postgres -n auto --dist loadfile --durations=25

test-index-postgres:
	uv run --no-sync pytest -q --ds=tests.settings_postgres -n auto --dist loadfile --durations=25 $(INDEX_TESTS)

test-schema-vendors:
	REBAC_TEST_SCHEMA_VENDORS=1 uv run --no-sync pytest -q -m schema_vendors

lint:
	uv run --no-sync ruff check src/ tests/

format-check:
	uv run --no-sync ruff format --check src/ tests/

typecheck:
	uv run --no-sync mypy --strict src/
	uv run --no-sync pyright src/

check: lint format-check typecheck test

ci: install-dev lint format-check typecheck test-parallel
