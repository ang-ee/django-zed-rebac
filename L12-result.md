# L12 result — collected delete batch order

## Change

- `src/rebac/mixins.py:346–348`: call Django's `Collector.sort()` and sort each already-collected model set by primary key before REBAC checks and `collected_for_delete` delivery. `delete_collected` is the only batch sender on this write path. Sorting performs no database query.
- `docs/ARCHITECTURE.md:2953`: specify that batch model and row order matches Django's `pre_delete` order.
- `tests/test_delete_batches.py:147`: a pair whose set iteration starts with the higher primary key reaches the batch receiver and `pre_delete` in ascending key order.
- `tests/test_delete_batches.py:190`: a collected child batch reaches receivers before its parent, matching Django's model order.
- `tests/test_delete_batches.py:343`: update an existing cascaded through-row test to expect Django's child-before-parent order.

## Verification

All commands ran from the workspace root. The temporary uv cache at `/private/tmp/rebac-l12-uv-cache` was cleared afterward with `UV_CACHE_DIR=/private/tmp/rebac-l12-uv-cache uv cache clean` (130 files removed; the path is absent). No live stack or port 5433 was used.

| Backend / check | Exact command | Result |
|---|---|---|
| SQLite, focused | `UV_CACHE_DIR=/private/tmp/rebac-l12-uv-cache uv run --no-sync pytest -q tests/test_delete_batches.py::test_declared_through_gate_can_refuse_after_batches_and_roll_back_receiver_writes tests/test_delete_batches.py::test_collected_batch_sorts_reversed_set_pair_by_primary_key tests/test_delete_batches.py::test_collected_batches_follow_django_parent_child_model_order` | 3 passed in 1.36s |
| SQLite, full tier 1 | `UV_CACHE_DIR=/private/tmp/rebac-l12-uv-cache make check` | Ruff lint passed; Ruff format checked 280 files; mypy strict passed on 76 source files; pyright reported 0 errors and warnings; pytest: 4,152 passed, 6 skipped, 45 xfailed in 54.87s. |
| PostgreSQL, focused | `UV_CACHE_DIR=/private/tmp/rebac-l12-uv-cache REBAC_TEST_POSTGRES_URL=postgres://postgres:rebac-test@127.0.0.1:32768/rebac_l12 uv run --no-sync pytest -q --ds=tests.settings_postgres tests/test_delete_batches.py::test_collected_batch_sorts_reversed_set_pair_by_primary_key tests/test_delete_batches.py::test_collected_batches_follow_django_parent_child_model_order tests/test_delete_batches.py::test_declared_through_gate_can_refuse_after_batches_and_roll_back_receiver_writes --create-db` | 3 passed in 2.46s |
| PostgreSQL, tier 2 delta | `UV_CACHE_DIR=/private/tmp/rebac-l12-uv-cache REBAC_TEST_POSTGRES_URL=postgres://postgres:rebac-test@127.0.0.1:32768/rebac_l12 uv run --no-sync pytest -q --ds=tests.settings_postgres -n 4 --dist worksteal -m '(postgresql or pg_delta) and not slow and not reference_exhaustive and not schema_vendors' --timeout=60 --durations=10 --create-db` | 645 passed, 4 skipped, 1 xfailed in 57.56s. |

The first tier 1 run exposed the existing assertion at `tests/test_delete_batches.py:343` expecting the old order; it passed after correction. The first PostgreSQL attempt stopped before collection because `psycopg` was absent. I installed the test driver into this workspace's virtual environment with `UV_CACHE_DIR=/private/tmp/rebac-l12-uv-cache uv pip install --python .venv/bin/python 'psycopg[binary]>=3.2,<4'` (installed psycopg and psycopg-binary 3.3.8), then reran PostgreSQL successfully.

Tier 3 (`make test-release`, including slow, reference, vendor, and random-order suites) was not run, as required by `AGENTS.md`. `rebac sync --check` was not run because neither schema nor sync changed.
