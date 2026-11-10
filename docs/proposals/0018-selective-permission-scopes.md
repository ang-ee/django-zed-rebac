# Proposal 0018: selective permission scopes

**Status:** implemented; compatible performance fix for 0.33.

## Evidence

`make test-pg-scale` seeds threads, messages and parts with a recursive admin
role, direct grants, nested groups and parent arrows. An indexed fragment
filter selects 104 parts, followed by `COUNT(DISTINCT message_id) GROUP BY
fragment_id`, or a 50-row listing. Both tuple storage layouts run. Before any
compiler changes, PostgreSQL 16.14 with `work_mem=4MB`, 150,000 threads,
150,000 messages and 1,500,000 parts produced:

| Storage | Count execution | Listing execution | Count SQL bytes | Count plan nodes |
|---|---:|---:|---:|---:|
| denormalized | 953.258 ms | 1003.580 ms | 14,890 | 73 |
| registry | 1041.214 ms | 1156.568 ms | 17,038 | 125 |

Both plans scan 1.5 million parts, hash the visible message ids, then join the
104 indexed fragment matches. The universal role arm scans every message and
thread as a UNION operand. Median count wall times over three executions were
783.842 ms and 885.595 ms respectively. Execution figures above include
`EXPLAIN ANALYZE` instrumentation.

The supplied deployment plan also shows repeated CTE scans. The hypothesis
is partly confirmed: PostgreSQL normally materializes multiply referenced
CTEs and cannot push the caller's restriction into them. However, most
resource sets in this synthetic baseline are *inlined* and still produce
whole-table hash joins. `WITH RECURSIVE` does not make every definition
recursive. See PostgreSQL's [materialization contract](https://www.postgresql.org/docs/16/queries-with.html#QUERIES-WITH-CTE-MATERIALIZATION).

## Decisions

**(a) Correlate the scope to the caller's native identity.** Django resolves
one indexed row witness, embedded as a scalar `SELECT true … LIMIT 1` with
`COALESCE(…, false)`. A CASE around EXISTS prevented semi-join pull-up but
still allowed hashed alternatives for shared targets; the scalar witness
prevents both forms of whole-child-table enumeration.
Within that row, disjunction uses Boolean OR. The previous special path that
interpreted small integer `WHERE` candidates is deleted; the database handles
all caller restrictions and identities. A kept template includes the resolved
identity SQL and Django alias namespace in its key, preventing accidental
reuse across differently aliased embeddings.

**(b) Test a single-path field arrow's target by its native key.** A correlated scalar
subquery selects `true` from an allowed target, with `LIMIT 1` and
`COALESCE(…, false)`. Plain correlated `EXISTS` was insufficient: PostgreSQL
could plan a hashed alternative over all parents, and those unused alternatives
also triggered expensive JIT compilation. The scalar form avoids that
alternative. Filter for permission *before* limiting: identities need not be
unique, and any matching allowed row must witness the arrow. This matters
under exclusion as well as positive arrows. Source filters, generic-target
checks and multi-valued join isolation retain their existing owners.

**(c) Keep universal operands as SQL Booleans.** The compiler classifies
row-independent expressions from the schema. A fixed-role membership stays
an uncorrelated SQL witness, outside any resource UNION, and can short-circuit
narrower OR arms. Immutable builtins still fold. It does not call
`grants_all()` and then omit a mutable fact: revocation between preparation
and execution must still deny. Existing evaluator constant-arm witnesses
remain fenced and rechecked by their statement. No superuser flag is read.

**(d) Share equal policy fences within the statement.** A content-addressed
`rebac_fence_…` definition reads the generation row once. Actor-independent
fences share even when differently bound actor definitions must remain
nested. Every embedding retains its `EXISTS` at its original Boolean position;
sharing never turns an optional or excluded fence into a global requirement.
Different revisions or deadline intervals remain distinct. Non-parametric
probes bind their own execution clock, including when invoked while another
statement compiles. No result is cached across statements.

**What 0.32 keeps.** Actor membership, stored/attribute identity sets and
recursive closures retain content-addressed CTE sharing and their existing
materialization policy. A trial with inlined shared Boolean projections kept
SQL compact but expanded a nine-level field-arrow diamond (512 paths) to
518,805 KiB of PostgreSQL planner memory and a 124.5-second test. That shape
is rejected. Reused field-target sets and their descendants retain ordinary
id-set sharing. An independent universal witness precedes access to the shared
set, so a universal grant does not demand its resource enumeration; otherwise
the shared set decides the target. Lookup of the shared set is itself scalar
and restricted by native key, so a singly referenced CTE can push that key into
its rows. Plain IN membership instead produced enormous estimated costs and
expensive JIT for a skipped branch. The fallback is compiled under the
condition that its universal disjuncts are false: equal row-independent SQL
witnesses are false throughout that fallback, including inherited paths.
These are branch-local algebraic assumptions, not cached database decisions;
their full SQL/parameter signatures include the bound and recursion path's
actual predicate. A different caveat bound or role witness is not assumed.
The compiler memo includes this assumption set and whether it is compiling a
row or an enumeration. The fallback retains UNION-based enumeration, so the
remaining sparse grants can drive its resource lookups. This removes universal
paths from the fallback's cardinality estimates as well as its runtime work.
Single-path chains use scalar lookups
without a wrapping CTE.
The universal shortcut keeps the current bound and recursion path; recursive
target nodes retain their normal decision. No new SQL adapter is introduced;
Django produces permission SQL through the existing django-cte boundary.

## Semantics

| Operator or condition | Preserved meaning |
|---|---|
| `A + B` | Boolean disjunction at one identity; an all operand suffices without enumerating resource rows. |
| `A & B` | Conjunction; an all operand leaves B's constraints. |
| `A - B` | A and the anti-witness of B, with B compiled at the opposite bound. All on the left never removes B. No `NOT IN` or nullable authorization Boolean. |
| Field arrows | A matching live backing edge and an allowed target at the same bound. Non-unique identities use an existential allowed-row witness. |
| Stored/attribute arrows and recursion | Existing membership sets, recursive closure/depth handling and bounds; no semantic change. |
| Caveats and expiry | Existing lower/upper verdicts and statement clock; undecided caveats never authorize the lower bound, including on the right of exclusion. `CheckResult` remains three-state. |
| Wildcards | Existing declared subject shapes and membership rules; universal algebra does not add a wildcard or broaden a write permission. |
| Strict mode | The manager still rejects a missing actor before scope evaluation. Explicit actor precedence and sudo behavior are unchanged. |

All witnesses read the same statement snapshot and bound clock. Sharing or
short-circuiting changes evaluation work, not permission facts. Both storage
layouts compile through these same operators. Deterministic
graph traversal and signatures retain deterministic SQL; schema emission is
unchanged. No public API, storage migration or proposal 0013 write-gate rule
changes.

## Measurements and validation

Final run: local Docker PostgreSQL 17.10 on ARM, `work_mem=4MB`, **450,000
threads, 450,000 messages and 6,750,000 parts**, strict mode on, superuser
bypass off. Saved pre-fix queries were replayed on this same fixture and
returned identical rows. Timings are instrumented execution, in milliseconds:

| Storage | Count before → after | Listing before → after | Count wall after | Count planning memory before → after |
|---|---:|---:|---:|---:|
| denormalized | 3912.685 → 0.283 | 4129.756 → 0.261 | 3.420 ms | 4528 → 2584 KiB |
| registry | 4385.001 → 0.558 | 4619.343 → 0.349 | 5.941 ms | 6651 → 4220 KiB |

All **20** count/listing cases passed: admin, member and denied actors; a
shared parent target; and multiple scope embeddings that reference the same
shared set. Across them, execution was 0.251–0.799 ms and median wall time
3.138–5.941 ms. Multiple-embedding counts took 0.483/0.591 ms, with
3798/5533 KiB planning memory. Every plan read the generation table once,
examined at most 104 rows per node (including rejected rows and loops), and
had no JIT. The nine-level branching case now uses 13,630 SQL bytes and
3,292 KiB planner memory, below its 8 MiB regression budget; it passes on
SQLite and PostgreSQL. These are warm local measurements, not production
latency estimates. The complete [measurement record](./0018-measurements.json)
includes SQL size, compilation, planning, memory and timing for every case.

The benchmark compares exact result rows when replaying a saved baseline,
checks both execution and median wall time below one second, and bounds
actual plan work by the 104 selected candidates. PostgreSQL 17+ records
`EXPLAIN (MEMORY)`; PostgreSQL 16 runs without that option.

Reproduce the large fixture (after exporting the URL printed by `make pg-up`):

```bash
REBAC_SCALE_MESSAGES=450000 REBAC_SCALE_PARTS=6750000 \
  REBAC_SCALE_REPORT=/tmp/rebac-scope-results make test-pg-scale
```

For planning memory, start a separate PostgreSQL 17 container with
`make pg-up PG_IMAGE=postgres:17 PG_CONTAINER=rebac-scope-pg17` and export its
printed URL. `REBAC_SCALE_BASELINE` accepts a directory of saved
`<storage>-<query>-query.json` files containing SQL and parameters. Capture
those before modifying the compiler; replay verifies identical admin results
against the same newly seeded fixture. Reports include admin, nested-group/
direct-member and denied-actor count/listing cases, shared targets and multiple
embeddings. Baseline replays cover the original admin cases. Fixtures use `generate_series`; no handwritten SQL
implements permission behavior.

Required checks: `make check` (4,078 passed, 3 skipped, 43 expected failures;
Ruff, strict mypy and Pyright green), `make test-pg` on PostgreSQL 16 (670
passed, 4 skipped, 1 expected failure), and the touched nine-level branching
regression (passed). Small plan, fence-sharing and semantic regressions run
in tiers 1 and 2, with both storage layouts where applicable. Tier 3 was not
run; no schema emitter or sync code changed.

## Limits and release

These measurements cover selective synthetic reads. Broad listings can pay
one scalar target lookup per selected row; tuple membership, attribute sets
and recursive closures may still be unbounded. Reused target sets without a
sufficient universal witness also retain their unbounded enumeration. The
consumer's complete operation
and its memory gate still need measurement on its own schema/data. MySQL's
release-only vendor suite was not run. No production latency claim follows
from this fixture.

Recommend **0.33.1 (patch)**: this corrects query planning for existing scope
semantics with no API, storage or authorization-contract change. SQL and
content-hashed CTE snapshots change. Keep the version bump as a release
proposal; this work does not tag or publish.
