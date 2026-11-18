# 0019: Actor grant profiles

Status: **accepted by the owner on 2026-11-18 for 0.35.0.** It generalises the negative constant-arm fold and
the scope-local contract of proposal 0018 from constant arms to stored grants. The contract is specified in
`docs/ARCHITECTURE.md § Actor grant profiles`.

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

**Decidable arms.** In `Compiler._stored_relation`, the allowed-subject groups that end at the actor: the plain
subject group, and a subject-set group whose set relation is stored and admits only plain subjects and other
such sets (`Compiler._decidable_set`). A wildcard group, an arrow group (the row's subject is an object, not
the actor) and a subject set whose relation is backed are not decidable and compile as today.

**Rule.** A decidable group of a stored relation whose key is not in the profile is not compiled, **in a
granting position only**. Under a subtraction a missing arm would admit instead of deny, so nothing is left out
inside the operand of an exclusion, in the depth statement, or in the convergence test of a recursion
(`Compiler._negated`). The ordinary traversal folds the algebra. No ids are decided or bound, and no alternate
evaluation path is added.

**Scope.** A profile exists only inside an evaluator scope, for an actor that is a plain subject (no subject
relation, not anonymous, a real id). It is read by one statement at the first compile for that actor and
database, kept for the scope, and read again after a library relationship write
(`mark_relationships_changed`). Outside an evaluator scope every statement is compiled as today.

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

## Results

Tier 1: 4,438 pass. Tier 2 on PostgreSQL 17: 989 pass. Lint, format, mypy and pyright pass.

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
| Selected message, wall | 977–997 ms | 365–387 ms |
| Selected message, statements | 25 | 23, the profile's included |
| Selected message, SQL | 1,618 KB | 454 KB |
| Selected message, planner allocation | 1,246,520 KiB | 643,905 KiB |
| Largest statement, permission id sets | 174 | 121 |
| Administrator, selected message | 23 statements, 64–72 ms | 24 statements, 60–66 ms |

On a consumer's locked write path (one evaluator scope around the unit of work, two concurrent writers): the
lock is held 0.29–0.30 s instead of 1.47–1.49 s, permission SQL per unit falls from about 3,900 KB to about
710 KB, and one profile statement is issued per unit. An evaluator scope alone, with profiles off, does not
shorten that path.

## Decisions taken

1. **The statement-count pins move.** A scope costs one more statement; the pinned tests were re-taken one by one
   with this proposal as the reason.
2. **A setting.** `REBAC_ACTOR_PROFILES` (default on) stays as a switch.
3. **Outside evaluator scopes** nothing changes. A consumer whose work runs outside one (tasks, workflow steps)
   gains only when it opens a scope around a unit of work.

## Known limits

- A statement is kept per profile, so the 512-entry statement cache serves fewer permissions when many distinct
  profiles are in use. Keying by the part of the profile a permission can reach (below) is the remedy.
- The profile's statement follows every stored subject set the actor is in. Its cost for an actor with very many
  grants or memberships has not been measured.
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
