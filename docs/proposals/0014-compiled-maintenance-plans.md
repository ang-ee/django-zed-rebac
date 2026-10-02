# Proposal 0014: maintenance statements compiled once per schema revision

**Status:** superseded by [proposal 0015](./0015-permissions-compiled-to-queries.md).
There is no maintenance statement left to compile. The identity conversion's
guard cache from step 1 stays (`rebac.codec`). The text below is kept as
written.

## Problem

A write spends more time building ORM expressions than running SQL.

Measured on the APM stack (PostgreSQL, 0.24.1, one comment post: six model
saves, a follow and a notification `bulk_create`; request 4.5 s, about 1,300
statements, four maintenance passes):

| | |
|---|---|
| `Query.resolve_expression` calls | 263,881 |
| `copy.copy` calls | 328,726 |
| `compiler.compile` calls | 225,534 |
| `index/write.py:projected_rows` | 8,148 calls |
| `index/write.py:stream_create` | 393 calls |
| `index/codec.py` `as_sql` | 1,653 calls |
| PostgreSQL wait | about 1.5 s of 4.5 s |

About 2.1 s is inside `IndexMaintenance.finish()`; the rest is write-time
capture outside it (`_capture`, `capture_values`). Proposal 0009 removes most
of the work inside `finish()` that re-derives unchanged scopes. What remains,
on both sides, has two causes:

1. **Every pass rebuilds its statements through the ORM.** `capture_values`,
   `add_terms`, `expand_region`, `project_edges`, `derive_memberships` and
   `derive_nodes` build their querysets from scratch on each call. The
   statements depend only on the schema program and the model metadata; what
   varies between calls is the pass id, the phase, a region or a set of primary
   keys. Django re-resolves and recompiles the whole expression tree each
   time, which is the same cost the read side measured before 0.23.1 (about
   70% of compile time for a plan).
2. **Derived rows pass through Python.** `stream_create` iterates a select
   through a server-side cursor and writes the rows back with `bulk_create`,
   in batches of 256. A row is serialised by the database, parsed by the
   driver, turned into a model instance and serialised again.

## What is allowed today

ARCHITECTURE § Permission index: "No raw SQL, trigger, database function or
undocumented ORM API is used, with one deliberate exception: the
queryset-scope plan cache (0.23.1) keeps the SQL that Django compiles for a
plan, through `Query.get_compiler()` ... The SQL is Django's own; no statement
is written by hand." The exception was accepted on measurements: 17 to 19 ms
per scope through public API against about 1 ms cached.

This proposal asks for the same exception on the write side, in two steps that
are decided separately.

## Step 1: cache the compiled statement, bind the varying values

For each maintenance statement, compile once per (program digest, database
alias, statement kind) and keep Django's `(sql, params)` with the varying
values as named placeholders: pass id, phase, region id, statement tag. Each
call binds values and executes through the connection's cursor wrapper, so
`execute_wrapper`s, `CaptureQueriesContext` and the audit observer still see
the statement.

- The SQL is produced by Django's compiler from the same querysets the code
  builds today; no statement is written by hand. The cache key includes the
  program digest, so a schema change recompiles.
- Variable-length inputs (a list of primary keys) do not go into the cached
  statement: they are written to the work table first, as `snapshot_queryset`
  already does with its per-statement tag, and the cached statement joins the
  work table by tag.
- Reads that return rows (`capture`, the comparison reads of proposal 0009)
  use the cached select and iterate the cursor.
- A test pins that the cached SQL for each statement kind equals what the
  uncached queryset compiles to, for every schema in the index test suite, on
  SQLite and PostgreSQL. That is the guard against the cache drifting from the
  ORM.

This step removes cause 1 and needs no new rule beyond extending the existing
exception to maintenance statements.

## Step 2 (decision gate): insert derived rows in the database

`stream_create` exists because the project decided, on 2026-09-29, that rows
pass through Python in bounded batches rather than through a hand-rolled
`INSERT ... SELECT`. That decision stands unless this gate overturns it.

The option: for the statements whose select is already Django-compiled (step
1), execute `INSERT INTO <table> (<columns>) <compiled select>` so the
database moves the rows. The insert prefix is one fixed string per index table,
generated from model metadata (`_meta.db_table`, column names through
`connection.ops.quote_name`); the select is Django's.

What it changes:

- Model defaults, `pre_save` on fields, and `bulk_create`'s conflict handling
  no longer run for derived rows. The index tables use none of the first two;
  conflict handling (`ignore_conflicts` for terms) would have to be expressed
  per vendor (`ON CONFLICT DO NOTHING`, `INSERT IGNORE`), which is hand-written
  SQL beyond the fixed prefix.
- `python_rows`, the budget the scale tests assert, becomes zero for these
  statements; the budgets move to statement counts and row counts.

The gate is a measurement, taken after proposal 0009 and step 1 have landed:
the share of maintenance time still spent in `projected_rows` and
`bulk_create` on the comment-post profile and on the scale suite. If rows
through Python are under a fifth of what remains, step 2 is not worth a second
exception and is dropped. If they dominate, it is proposed again with those
numbers, restricted to tables with no conflict handling first.

## Correctness

Step 1 changes when SQL is compiled, not what it is: the pinned-equality test
holds the cached and uncached forms together, and the drift oracle runs after
every maintenance test as before. Step 2, if taken, changes how rows travel,
not which rows: the same select feeds the same table.

## Cost and target

Target from the stack: a six-write request spends under one second in
maintenance on PostgreSQL. Step 1's expected effect is the read side's: an
order of magnitude on statement preparation. The cache holds one entry per
statement kind per program per alias; a schema with 40 definitions has a few
hundred.

## Tests

- Cached SQL equals freshly compiled SQL per statement kind, per schema, per
  vendor.
- A schema revision change invalidates the cache (the next pass compiles
  again and derives under the new program).
- `CaptureQueriesContext` and connection `execute_wrapper`s observe cached
  statements.
- The comment-post shape as a scale test: `resolve_expression` call count per
  write bounded by a constant independent of the number of relations in the
  schema.
- Every existing maintenance, derivation and scale test with its drift check.

## Not in scope

- Proposal 0009's change propagation (lands first).
- Proposal 0013's compiler integration; the cached statements are
  maintenance-internal and unaffected by where the gates live.
- MySQL-specific conflict syntax (only relevant to step 2).
