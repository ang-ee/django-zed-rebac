"""The tier 1 check for a parameter tested for NULL (``tests/conftest.py``).

PostgreSQL under psycopg's server-side binding refuses each flagged statement
with a string parameter, and accepts each passed one except those listed as
unread, which the check does not look for. The check reads SQL text only, so
it is pinned here without a database.
"""

from __future__ import annotations

import pytest

from tests.conftest import _untyped_null_test

FLAGGED = [
    "SELECT 1 WHERE %s IS NULL",
    "SELECT 1 WHERE %s IS NOT NULL",
    "select 1 where %s is not null",
    "SELECT 1 WHERE (%s) IS NULL",
    "SELECT ((%s)) IS NULL",
    "SELECT %s /* note */ IS NULL",
    "SELECT %s -- note\n IS NULL",
    "SELECT 1 WHERE /* note */ %s IS NULL",
    "SELECT %s\nIS\nNULL",
    "SELECT %s ISNULL",
    "SELECT %s NOTNULL",
    "SELECT NOT %s IS NULL",
    "SELECT %s IS DISTINCT FROM NULL",
    "SELECT %s IS NOT DISTINCT FROM NULL",
    "SELECT CASE WHEN %s IS NULL THEN 1 END",
    "SELECT TRUE AND %s IS NULL",
    "SELECT COALESCE(TRUE, %s IS NULL)",
    # PostgreSQL types an int, but the same statement is bound to a string for
    # another identity type: the shape is refused whatever the test binds.
    "SELECT %s IS NULL",
]

PASSED = [
    "SELECT 1 WHERE col IS NULL AND x = %s",
    "SELECT %s IS DISTINCT FROM %s",
    "SELECT COALESCE(%s, NULL) IS NULL",
    "SELECT COALESCE('x', %s) IS NULL",
    "SELECT upper(%s) IS NULL",
    "SELECT 'x' = %s IS NULL",
    "SELECT %s::text IS NULL",
    "SELECT CAST(%s AS text) IS NULL",
    "SELECT 1 + %s IS NULL",
    "SELECT %s IN ('a') IS NULL",
    "SELECT '%%s IS NULL'",
    "SELECT %s IS NOT NULLIF",
]

# Refused by PostgreSQL and not read by the check: the tier 2 run under
# server-side binding is the guard for these.
UNREAD = [
    "SELECT -%s IS NULL",
    "SELECT pg_typeof(%s)",
    "SELECT concat(%s)",
]


@pytest.mark.parametrize("sql", FLAGGED)
def test_a_parameter_tested_for_null_is_flagged(sql):
    assert _untyped_null_test(sql)


@pytest.mark.parametrize("sql", PASSED + UNREAD)
def test_any_other_statement_passes(sql):
    assert not _untyped_null_test(sql)
