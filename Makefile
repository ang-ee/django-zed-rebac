.PHONY: install-dev lint format-check typecheck check ci \
	test test-fast test-pg \
	test-slow test-postgres test-reference \
	test-schema-vendors test-random test-release \
	pg-up pg-down require-postgres require-docker require-vendor-drivers

# Test tiers: docs/ARCHITECTURE.md § Test tiers. Commands: CONTRIBUTING.md § Commands.

PYTEST := uv run --no-sync pytest -q
PG := --ds=tests.settings_postgres
# One PostgreSQL server serves every worker's test database. Beyond a few
# workers it is the bottleneck, and fresh, unanalyzed tables under that load
# have produced cursor fetches that ran for the better part of an hour.
PG_WORKERS ?= 4
# Extra pytest arguments for any test target: make test-pg ARGS=tests/test_read_contract.py
ARGS ?=

# An explicit -m replaces the addopts expression in pyproject.toml, so each
# expression below is written in full. Targets without -m use the addopts one:
# not reference_exhaustive, slow or schema_vendors.
PG_DELTA_MARKS := (postgresql or pg_delta) and not slow and not reference_exhaustive and not schema_vendors
SLOW_MARKS := slow and not reference_exhaustive and not schema_vendors
FULL_MARKS := not reference_exhaustive and not schema_vendors
REFERENCE_MARKS := reference_exhaustive or not reference_exhaustive

# The default per-test timeout is 10 s (pyproject.toml). Targets that select
# slow, reference_exhaustive or schema_vendors tests raise it.
LONG := --timeout=300

# Tier 3, in the order test-release runs it.
RELEASE_PARTS := test-slow test-postgres test-reference test-schema-vendors test-random

# A disposable PostgreSQL 16 with the credentials CI uses.
PG_CONTAINER := rebac-test-pg
PG_IMAGE := postgres:16

install-dev:
	uv pip install -e '.[dev,caveats,drf,strawberry]'

lint:
	uv run --no-sync ruff check src/ tests/

format-check:
	uv run --no-sync ruff format --check src/ tests/

typecheck:
	uv run --no-sync mypy --strict src/
	uv run --no-sync pyright src/

# Fix loop: re-run last failures first, stop at the first failure, balance work
# across workers by test.
test-fast:
	$(PYTEST) -n auto --dist worksteal --lf --ff -x --durations=10 $(ARGS)

# Tier 1: the gate for merging and publishing.
check: lint format-check typecheck
	$(PYTEST) -n auto --dist worksteal -x --durations=10 $(ARGS)

# The tier 1 selection, serial, for debugging.
test:
	$(PYTEST) $(ARGS)

# Tier 2: the PostgreSQL delta. Tests that are also slow run in test-postgres.
# The timeout counts each worker's test-database creation, several seconds on
# a freshly started PostgreSQL, so it is wider than tier 1's.
test-pg: require-postgres
	$(PYTEST) $(PG) -n $(PG_WORKERS) --dist worksteal -m '$(PG_DELTA_MARKS)' --timeout=60 --durations=10 $(ARGS)

# Tier 3 parts.
test-slow:
	$(PYTEST) -n auto --dist worksteal -m '$(SLOW_MARKS)' $(LONG) --durations=25 $(ARGS)

# The whole suite on PostgreSQL: the default selection plus slow.
test-postgres: require-postgres
	$(PYTEST) $(PG) -n $(PG_WORKERS) --dist worksteal -m '$(FULL_MARKS)' $(LONG) --durations=25 $(ARGS)

# Every test in the reference module, including the exhaustive shards. loadfile
# would put the entire sweep on one worker; load distributes shards.
test-reference:
	$(PYTEST) tests/test_reference_model.py -m '$(REFERENCE_MARKS)' -n auto --dist load $(LONG) --durations=25 $(ARGS)

# Docker PostgreSQL 16 / MySQL 8 contracts. Container start-up counts toward
# each test's timeout.
test-schema-vendors: require-docker require-vendor-drivers
	REBAC_TEST_SCHEMA_VENDORS=1 $(PYTEST) -m schema_vendors $(LONG) $(ARGS)

# The tier 1 selection in a reproducible random order.
test-random:
	$(PYTEST) -n auto --dist worksteal -p randomly --randomly-seed=137 --durations=10 $(ARGS)

# Every tier 3 part in sequence. A failing part does not stop the rest; the
# summary lists each part and the target fails if any part failed.
test-release:
	@status=0; summary=""; \
	for part in $(RELEASE_PARTS); do \
		echo "==> make $$part"; \
		start=$$(date +%s); \
		if $(MAKE) --no-print-directory $$part; then result=pass; else result=FAIL; status=1; fi; \
		summary="$$summary  $$result  $$part ($$(( $$(date +%s) - start )) s)\n"; \
	done; \
	printf '\ntest-release:\n%b' "$$summary"; \
	exit $$status

# Data lives in memory (tmpfs) and is gone once the container stops. Status goes
# to stderr and only the export line to stdout, so `eval "$(make -s pg-up)"`
# sets the variable in the calling shell. Re-running pg-up reuses the container
# and prints the same line.
pg-up: require-docker
	@running=$$(docker container inspect -f '{{.State.Running}}' $(PG_CONTAINER) 2>/dev/null); \
	if [ -z "$$running" ]; then \
		docker run -d --name $(PG_CONTAINER) \
			-e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=rebac-test -e POSTGRES_DB=rebac_test \
			-p 127.0.0.1::5432 --tmpfs /var/lib/postgresql/data \
			$(PG_IMAGE) >/dev/null || exit 1; \
	elif [ "$$running" != true ]; then \
		docker start $(PG_CONTAINER) >/dev/null || exit 1; \
	fi; \
	echo "Waiting for $(PG_CONTAINER) to accept connections." >&2; \
	ready=0; \
	for _ in $$(seq 1 60); do \
		if docker exec $(PG_CONTAINER) pg_isready -q -h 127.0.0.1 -U postgres -d rebac_test; then \
			ready=1; break; \
		fi; \
		sleep 1; \
	done; \
	if [ $$ready -ne 1 ]; then \
		echo "$(PG_CONTAINER) did not accept connections within 60 s; see docker logs $(PG_CONTAINER)." >&2; \
		exit 1; \
	fi; \
	hostport=$$(docker port $(PG_CONTAINER) 5432/tcp | head -n 1); \
	echo "export REBAC_TEST_POSTGRES_URL=postgresql://postgres:rebac-test@127.0.0.1:$${hostport##*:}/rebac_test"

pg-down:
	@if docker container inspect $(PG_CONTAINER) >/dev/null 2>&1; then \
		docker rm -f $(PG_CONTAINER) >/dev/null && echo "Removed $(PG_CONTAINER)." >&2; \
	else \
		echo "$(PG_CONTAINER) is not running." >&2; \
	fi

# Prerequisite checks. A target that needs PostgreSQL or Docker fails here with
# what is missing; it never skips or falls back to SQLite.
require-postgres:
	@if [ -z "$$REBAC_TEST_POSTGRES_URL" ]; then \
		echo "REBAC_TEST_POSTGRES_URL is not set. Run 'make pg-up' and export the URL it prints." >&2; \
		exit 1; \
	fi
	@uv run --no-sync python -c 'import psycopg' 2>/dev/null || { \
		echo "psycopg is not installed: uv pip install 'psycopg[binary]>=3.2,<4'" >&2; \
		exit 1; \
	}

require-docker:
	@docker info >/dev/null 2>&1 || { \
		echo "Docker is not available. Start the Docker daemon and retry." >&2; \
		exit 1; \
	}

require-vendor-drivers:
	@uv run --no-sync python -c 'import MySQLdb, psycopg' 2>/dev/null || { \
		echo "The vendor contracts need both drivers: uv pip install 'psycopg[binary]>=3.2,<4' mysqlclient" >&2; \
		exit 1; \
	}

ci: install-dev check
