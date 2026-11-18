# 0019: Actor grant profiles

Status: **accepted by the owner on 2026-11-18 for 0.35.0 and amended before release.** The first build passed
tiers 1 and 2 and failed the scale matrix; **the planner's bound** below is the amendment, which the owner
approved the same day with the eight re-taken PostgreSQL 17 manifest cells. **0.35.1 lifts the subject-set
rule**, with the owner's approval and 24 more re-taken cells (Known limits). **0.35.2 bounds what reading the
profile costs** (Reading the profile, below), which the first two releases left unmeasured. It generalises the
negative constant-arm fold and the scope-local contract of proposal 0018 from constant arms to stored grants.
The contract is specified in `docs/ARCHITECTURE.md § Actor grant profiles`.

## Problem

A statement is compiled with every arm its permission can follow. For a member of a large composed policy most
of those arms are stored relations the actor holds no row in: reader and editor grants on types they were never
shared, roles they are not in, group subject sets when they are in no group. The database still plans every one
of them, in every statement.

Every system in the prior-art survey resolves the principal's facts before it emits SQL (Odoo's cached rule
domains, Oso's principal facts, RLS initPlans). This engine resolves them inside every statement.

## Contract

**Profile.** For an actor and a database, the set of stored relation keys `(resource_type, relation)` in which
at least one relationship row names the actor, a wildcard of the actor's type, or a stored subject set the
actor is in, the subject sets followed to a fixed point over relationship rows. Expiry and caveats are not read,
so it is a superset of what the actor holds now.

**Reading the profile** (0.35.2, `_read_profile`). Its cost must not grow with the rows that name the actor: a
consumer that writes one relationship for each record a user creates gives its users as many rows as they have
created records. 0.35.0 and 0.35.1 read every such row and looked each one up as a possible subject set.

| Rows naming the actor, registry layout | 0.35.1 | 0.35.2 |
|---|---|---|
| a few | 0.5–1.3 ms, one statement | 0.9–2.2 ms, one statement |
| 25,000 (scale fixture) | 52–53 ms | 7–9 ms, two statements |
| 100,000 (a consumer's composed policy) | 240 ms | 4–14 ms, two statements |

- One statement reads at most 1,000 rows that name the actor or a wildcard of its type, and at most 1,000 that
  name a subject set the actor is in, and counts them by key (`_PROFILE_ROWS`).
- The subject sets are the closure of the actor's memberships over the relations the schema allows as subject
  sets (`auth/group#member`). A row is never looked up as a set it cannot be. The profile is kept per policy for
  that reason.
- Within both bounds the profile is exact, as before.
- Where a part stops at its bound, a second statement asks, for each relation name that can hold stored rows,
  whether one row of that name names the subject: one index probe per name, on
  `(subject, relation)`. The profile then holds `(PROFILE_ANY, relation)`, that relation on every resource type.
  It is wider than the exact profile and never narrower, so the direction of every guarantee below is unchanged.
- For the part that names subject sets, the sets are first listed by a statement of their own. An actor in more
  than 64 of them (`_PROFILE_SETS`) has no profile.

**Decidable arms.** In `Compiler._stored_relation`, the allowed-subject groups that end at the actor: the plain
subject group, and a subject-set group whose set relation is stored and admits only plain subjects and other
such sets (`Compiler._decidable_set`). A wildcard group, an arrow group (the row's subject is an object, not
the actor) and a subject set whose relation is backed are not decidable and compile as today.

**Rule.** A decidable group of a stored relation whose key is not in the profile is not compiled, **in a
granting position only**. Under a subtraction a missing arm would admit instead of deny, so nothing is left out
inside the operand of an exclusion, in the depth statement, or in the convergence test of a recursion
(`Compiler._negated`). The ordinary traversal folds the algebra. No ids are decided or bound, and no alternate
evaluation path is added.

**The planner's bound.** Where the profile folds a constant arm in a statement that builds an id set, the arm's
place keeps a condition that never holds and that PostgreSQL cannot fold: `(SELECT FALSE)`
(`Compiler._left_out`, `_PlannerBound`). The reason is what the full statement does for the planner, not for
the rows. A constant arm may admit every row of its type, so PostgreSQL plans every id set of that type as
large as the table and joins by hash. The first build removed the arm, and PostgreSQL then trusted its estimate
of the actor's own rows. Where that estimate is low, a statement that embeds the scope twice scans the id set
once for each row. Measured at 50,000 messages and 500,000 parts, for an actor with 25,000 direct grants:

| | Full statement | Arm removed | Arm removed, bound kept |
|---|---|---|---|
| Registry layout, broad read | 316–488 ms | statement timeout (30 s) | equal to the full statement |
| Grants reached through a group, broad read (the subject-set rule lifted) | 100–200 ms | about five times slower | equal to the full statement |
| Registry layout, skewed statistics, the actor named by its registry row | 307–312 ms | statement timeout (30 s) | 429–509 ms against 494–535 ms alternated |

The skewed fixture adds 150 actors with 25,000 grants each and a million with one, so that the statistics'
most-common list no longer holds the actor read.

PostgreSQL plans the scalar subquery as a parameter it does not know. The arm therefore keeps the table's size
in the estimate and runs nothing, because the false parameter gates its scan. PostgreSQL has no other way to be
told that a set may be large. The bound is emitted on PostgreSQL only, only where the full statement would
compile the arm, and not in a witness statement or a check of the one object the arm names, which size no id set.
Naming the actor by its registry row instead of joining the registry on its type and id was tried first. It
fixes the estimate where the actor is in the column's most-common list and nowhere else, so it was dropped.

**Scope.** A profile exists only inside an evaluator scope, for an actor that is a plain subject (no subject
relation, not anonymous, a real id). It is read by one statement at the first compile for that actor and
database, kept for the scope, and read again after a library relationship write
(`mark_relationships_changed`). Outside an evaluator scope every statement is compiled as today. 0.35.0 also
required that the actor hold no row through a stored subject set, a rule added when the first build's
group-granted actor read five times slower. The planner's bound removed that slow plan, and 0.35.1 lifts the
rule (see Known limits).

**Kept statements.** The profile is part of the statement key beside the actor's shape, so a kept statement is
shared by every actor with the same profile.

## Freshness

- **A grant is revoked or expires after the profile is read.** Its arm is still compiled and evaluated live.
  Exact.
- **A point check is exact, as before.** A check that holds was answered by live arms. A check that does not
  hold reads the profile again before it answers no, and is decided anew when the profile moved. That costs one
  statement for a denied check and none for an allowed one.
- **A read may be narrower until the scope ends, never wider.** A grant of a kind the actor did not hold,
  written by another process or behind the library after the profile was read, is missing from a read's
  statement. This is the limit 0018 already documents for constant arms. A check in the same scope refreshes
  the profile, after which reads are exact again.
- **An explicit consistency requirement** reads the profile at that call.
- **Transactions.** A profile read after this thread wrote relationship rows in a transaction that is still open
  is not kept: those rows may roll back, and a kept profile would then deny what the actor holds. Until that
  transaction ends it is read at each compile. The transaction is named by its outermost `atomic` block and
  forgotten on commit, or when the connection is next seen outside it; a transaction that writes no relationship
  row, and any later transaction, keeps its profile.

## Counts

One extra statement per evaluator scope, actor and database at the first read; one after each library
relationship write that a read follows; one for a denied point check, whether or not the profile moved; one per
compile while the thread's transaction holds uncommitted relationship writes, where a queryset read compiles
twice (its scope, then its statement). The profile also folds constant arms the actor cannot hold, which removes
their witness statements.

Each of those readings is one statement for an actor within the bounds, two where the rows that name the actor
pass the bound, and three where the rows that name its subject sets do.

## Results

Tier 1: 4,441 pass. Tier 2 on PostgreSQL 17: 990 pass. Lint, format, mypy and pyright pass.

The scale matrix (50,000 messages, 500,000 parts, both storage layouts, three shapes, inside and outside a
scope) on PostgreSQL 16, the release tier's version: 16 pass and the 8 frozen known limitations are unchanged,
with no manifest edit. On PostgreSQL 17 every cell passes or is a frozen known limitation; eight of those were
re-taken for this release (Known limits).

Fifty-three existing tests pinned what the profile changes and were re-taken one by one, each against the
statements actually issued: 51 statement or parameter counts (the profile's own statement, witnesses that are no
longer issued, arms that are no longer compiled), the SQL text of the member statement, and the documented limit
"after a write behind the library a read stays narrower than a check until the scope ends", which now ends when a
check refreshes the profile. Where a count moved, the test now also asserts how many of its statements are the
profile's. No test that compares decisions with the reference changed.

`tests/test_actor_profiles.py` adds the contract's own cases: the counts, the library and behind-the-library
grant, the consistency requirement, revocation, rollbacks of a grant and of a revocation, a transaction after a
committed or rolled-back write, a transaction the application ends itself (PostgreSQL), nested subject sets and
wildcards, a denied check after the profile moved, and the direction of staleness as a property. For random
policies a profile with keys removed (none, each one, a random subset of each size) must only lose rows, for reads
and for checks. The inherited-witness generator subtracts too rarely to show a missing arm under a subtraction, so
a second generator makes every permission subtract. With the granting-position rule removed, 61 of those cases
fail. `tests/test_constant_arms.py` gains an actor who is a member of another role, so the kept-false witness of
proposal 0018 stays covered where the profile folds nothing.

Measured on a consumer framework's composed policy (129 readable types), PostgreSQL 17, the prototype's setting
off and on, identical rows on every document:

| Member reads | Off | On |
|---|---|---|
| Selected message, wall | 1,227 ms | 691 ms |
| Selected message, statements | 25 | 24, the profile's included |
| Selected message, SQL | 1,618 KB | 523 KB |
| Selected message, planner allocation | 1,246,520 KiB | 710,627 KiB |
| Largest statement, permission id sets | 174 | 138 |
| Administrator, selected message | 23 statements, 144 ms | 24 statements, 112 ms |

The bound costs part of what the first build measured without it (454 KB, 643,905 KiB, 23 statements).

On a consumer's locked write path (one evaluator scope around the unit of work, two concurrent writers): the
lock is held 0.67–0.74 s instead of 1.79–2.05 s, permission SQL per unit falls from about 3,900 KB to about
1,310 KB, and one profile statement is issued per unit. An evaluator scope alone, with profiles off, does not
shorten that path.

## Decisions taken

1. **The statement-count pins move.** A scope costs one more statement; the pinned tests were re-taken one by one
   with this proposal as the reason.
2. **A setting.** `REBAC_ACTOR_PROFILES` (default on) stays as a switch.
3. **Outside evaluator scopes** nothing changes. A consumer whose work runs outside one (tasks, workflow steps)
   gains only when it opens a scope around a unit of work.

## Known limits

- **0.35.0 gave no profile to an actor that holds a grant through a stored subject set; 0.35.1 does.** With
  the planner's bound the matrix's two group-granted actors read as fast with a profile as with the full
  statement: 0.90 to 1.05 times its time over 16 cells in both layouts on PostgreSQL 16 (eight runs each in
  balanced order), and equal within noise on PostgreSQL 17. Their statements change, so 24 frozen cells of the
  scale manifest were re-taken with the owner's approval, each after the capture run showed the same
  limitation under the new statement: the sparse-member listing timeout (four cells on PostgreSQL 16, four on
  17) and the PostgreSQL 17 shared-read rescan with the same count (16 cells). No other cell of either matrix
  failed.
- **Eight PostgreSQL 17 manifest cells were re-taken, with the owner's approval.** The directly granted
  actor's narrow reads in the shared shape, inside a scope, both layouts and JIT modes, are frozen as the
  PostgreSQL 17 shared-read rescan of proposal 0018. The profiled statement rescans the same way, with the same
  rescan count, and reads as fast (332–359 ms against 329–397 ms in the registry layout). Its text is new, so
  those eight entries now hold the 0.35.0 statement's fingerprints. The release tier runs PostgreSQL 16 and is
  not affected.
- **The bound keeps the full statement's caution, and its cost.** A sparse actor's broad read is planned as if
  it could see every row, as today. The first build read such a cell in 2.3 ms where the full statement takes
  25 to 114 ms; that gain is given up.
- A type with no constant arm has no bound. Its id sets are estimated from the actor's rows with or without a
  profile, as today.
- A statement is kept per profile, so the 512-entry statement cache serves fewer permissions when many distinct
  profiles are in use. Keying by the part of the profile a permission can reach (below) is the remedy.
- **Past the bound a relation is known by name, not by type.** An actor with more than 1,000 rows keeps the arms
  of a relation it holds compiled on every type that declares a relation of that name. In the scale matrix the
  directly granted actor's statement is 11–13 KB that way, against 4 KB with exact keys and 17 KB in full.
  Exact types for a relation with few rows would need a bounded read per name; it is not built.
- **Memberships are still read in full.** The closure of the sets an actor is in reads each membership row, as
  every statement already does. An actor in very many sets pays for them; one in more than 64 whose sets hold
  more than 1,000 rows has no profile.
- **A relation name is probed for every schema relation that can hold stored rows.** A policy with hundreds of
  distinct relation names makes the second statement that long. A composed policy of 541 relations has 25 such
  names: an 8.6 KB statement that plans in 1.6 ms and runs in 0.2 ms.
- **The statements are built by the ORM at each reading.** For the actor with 100,000 rows the two statements
  take about 4 ms and the reading about 9 ms; for an actor with a few rows, 0.6 ms and 1.9 ms. Keeping their
  text per policy, as permission statements are kept, would remove most of the difference. It is not built.
- **16 PostgreSQL 17 manifest cells were re-taken for 0.35.2.** The directly granted and the group-granted
  actors of the scale matrix both pass the bound, so their statements name relations by name. The capture run
  showed the frozen rescan unchanged in each cell (same count), and no other cell failed.
- Two cases keep reading the profile at each compile longer than they need to, which costs statements and never
  rows: a rolled-back transaction whose outermost `atomic` object is entered again (a decorated function), until
  the thread is seen outside a transaction; and a transaction the application ends itself, until autocommit
  returns.
- Uncommitted writes are recorded per database alias. Two aliases that share one connection (a test mirror) are
  not known to each other.

## Not in this proposal

- **User-naming columns** ("no row names this actor as owner"). Measured with a schema rewrite as a further
  2.8x, but any ordinary write can change the answer, so it needs write tracking per table.
- **Keying by the part of the profile a permission can reach,** which would share statements between more
  actors. The prototype keys by the whole profile.
- **Flattened per-table rules** as an output for review and budgets (access paths per type).
- **Arrow and wildcard groups, and recursion's flat forms,** which the profile does not narrow.
