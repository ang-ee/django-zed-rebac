# 0018: Inherited constant-arm witnesses

Status: accepted for the 0.33.1 candidate, with the denial-only revocation
contract below. Final review corrections passed the native release gates and
3,000 SQLite plus 600 PostgreSQL randomized in/out-of-scope comparisons.
No public API or setting is added.

## Contract and owners

A scope such as `part.read = message->read` can reach a constant administrator
arm without having one of its own. Inside an evaluator scope, positive constant
witnesses let that read use a selective statement.

Eligibility follows positive untagged union references and single-valued field
arrows. Intersections, exclusions, recursive components, tagged subtrees, and
stored, attribute and constant arrows stop discovery. `ResolvedFieldBacking`
owns whether its path and filters are single-valued. `CompileProgram` memoizes
reachability; policy/actor-shape metadata keeps the eligible target keys.
Invalid backings still raise `SchemaError` citing `rebac.E009` at the compiler's
relation owner.

`Compiler.constant_arms` excludes permissions whose ordinary lower bound folds
to TRUE for the actor's shape, including compound Boolean expressions. This
uses the ordinary compiler, without witnesses or folding, and keeps exploratory
id sets out of the witness statement. TRUE targets still become `fk IS NOT NULL`,
including dangling FK values; FALSE targets keep ordinary folding.

The existing memoized `Compiler._holds` substitutes a positive witnessed key
only at the lower bound: its uncorrelated constant-arm recheck alone, with live
target membership. Other grants are not retained in that predicate. Target
selections on witnessed paths are inlined; other id sets retain ordinary CTE
sharing. The existing traversal owns recursion, visits, Boolean folding and
membership. No alternate permission-evaluation path is introduced.

## Preparation, counts and revocation

Own and inherited probes share one SELECT when both are pending, deduplicated
by SQL, parameters, actor and database. They reuse the root operation's caveat
verdicts. A read with no own witness pays at most one extra first-read witness
per evaluator scope; repeats pay none, including with expiration or in atomic
blocks. If a point `check_access()` first caches a negative own witness, the
first list can still cost two statements (inherited probe and data), followed
by one for repeats. A cached positive own witness skips pending targets.

Inherited answers are scope-local plan choices retained even after library
writes or where decision caching is disabled. A negative keeps the complete
ordinary predicate; a positive selects a live constant recheck. A library
role revoke can therefore make a **fresh** child queryset return `[]` despite
other target grants. An own-witness read of that target re-decides the shared
entry after the write, so reading the target first can restore those other
grants in a subsequent child read. Results within one scope are order-dependent.

This is explicitly denial-only: stale inherited choices never admit extra rows.
The next evaluator scope returns the exact ordinary result. Out-of-band
revocation can narrow own witnesses too, but own witnesses re-decide after
library writes; inherited staleness is not limited to prepared querysets or
out-of-band writers. No cached inherited answer grants access. Outside an
evaluator, and for actors without positive inherited witnesses, ordinary
statements remain unchanged. There is no render-time preparation.

## Acceptance criteria and evidence

The release matrix uses 50,000 parent rows and 500,000 child rows, both storage
layouts, JIT on/off, inside/outside evaluators and narrow/broad count/list reads.
Completed reads must match expected rows. Changed administrator narrow plans
must improve at least 10x over 0.33.0; changed cells must not exceed both 1.25x
baseline and 25 ms. Byte-identical SQL/parameters with the same structural plan
are equal; their timing variation is recorded. Only the exact baseline
limitations below are exempt from timeout or multi-row CTE-rescan failures.

The accepted comparison completed 384 PG16 cells: 376 equal row results and
eight matching timeout limitations. All 24 changed administrator narrow cells
exceeded 10x (38.78–308.69x); all 48 changed administrator cells met the ceiling.
PG17 k=1,4,16 embedding SQL/parameters and 24 measured planner-memory values
matched 0.33.0. Native scale targets subsequently passed with eight expected
timeouts on each vendor and 56 expected PG17 rescan cells.

The embedding harness asserts SQL/parameter identity and records planning
memory plus its baseline delta. It has no independent memory ceiling for
identical statements; the formerly unreachable ceiling branch is removed.
Reports default to pytest's temporary directory; `REBAC_SCALE_REPORT` selects
a persistent directory. Planning summaries have distinct names and never
replace the main matrix summaries.

Regression tests cover compound TRUE targets with dangling FKs, revocation,
embeddings, caveats and statement counts. The randomized reference tests cover
both layouts and three actor forms with unions, intersections, exclusions,
caveats, expiration, wildcards and subject sets. Half the seeds deliberately
reach inherited substitution and assert a non-empty witnessed set. A deferred-
constraint variant adds dangling message and part rows. Release verification
also runs the independent randomized in/out-of-scope comparison, including
post-write subset checks and exact results in fresh scopes.

## Known limitations / follow-up

**Operational note.** With a positive inherited witness, an unfiltered count can
plan as a parallel hash join over the target table, which allocates dynamic
shared memory. PostgreSQL running in Docker with the default 64 MB `/dev/shm`
then fails with "could not resize shared memory segment"; give such containers
a larger `--shm-size` (tier 3 uses 1 GB), as PostgreSQL in containers generally
requires for parallel queries.

Two scale limitations predate 0.33.1 and reproduce on 0.33.0 (`65754b2`).
Their follow-up belongs to this section of proposal 0018, not to inherited
witness semantics:

- **Sparse-member broad listing:** eight cells per PostgreSQL vendor (both
  storage layouts, evaluator modes and JIT modes, `single` shape) time out at
  the unchanged 30-second statement timeout on 50,000 messages / 500,000 parts.
  Frozen 0.33.0 SQL, ordered parameters and non-executing plan fingerprints
  bound the exception. The harness checks all three before omitting execution;
  reports distinguish this bounded known limitation from an executed read or
  verified row result. Follow up with a selective sparse-listing plan and then
  remove these eight entries.
- **PG17 shared-read CTE rescans:** 56 measured narrow cells have identical
  SQL, parameters, structural plan and rescan count to 0.33.0: 48 non-admin
  cells plus eight outside-evaluator administrator cells. They still execute
  the full row, timing and plan checks. Only the exact recorded rescan failure
  is expected; changed identity, a different rescan count, or a resolved
  rescan is a hard failure (strict unexpected pass). Follow up with the PG17
  shared-plan shape and remove the corresponding entries when it improves.

The per-cell mechanism reports pytest subtest xfails, never marks an entire
32-cell fixture xfail. SQL/parameter/plan drift and every unrelated assertion
remain failures. Frozen evidence applies only to the recorded vendor, fixture
size and `work_mem`; thresholds, timeouts and data sizes are unchanged. The
baseline manifest lives in `tests/fixtures/scope_scale_0330.json`. Its origins
and canonical fingerprint method are documented alongside the harness. Pytest
9's built-in subtests are a development-test requirement so a known cell cannot
abort or hide the rest of its fixture.
