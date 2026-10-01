# Proposal 0015: permissions compiled to queries over the application's tables

**Target version:** a green-field re-implementation of `LocalBackend`
evaluation; candidate shape for 1.0.
**Status:** draft 3 (2026-10-01), revised after a design review and after
first measurements on a consumer database. The decision gates at the end are
open. An experimental compiler and test-only shadow hook now exist beside
the index; production reads and write maintenance have not switched.
**Supersedes, if accepted:** proposals 0009 and 0014, and the maintenance
half of 0012. Proposals 0011 and 0013 are narrowed, not superseded (§ 10).

## What changed since draft 1

### Implementation direction after the second review

The owner authorized the isolated implementation and prefers clean breaking
changes over compatibility shims. Build and validate the compiler beside the
index first. G1 remains a hard stop before switching reads or deleting state.
Keep the live main checkout and deployment untouched. Full caveats belong in
the implementation; keep the existing authorization gates, including 5d,
while their separate policy decision remains open. Remove obsolete index
commands and private imports at cutover rather than retaining no-op shims.

The refinements below are part of the prototype contract. Authorization must
remain sound before performance optimizations are accepted.

The prototype is not yet a completed implementation of this contract.
Structural recursion at a bound of 16 executes the 12-hop folder fixture on
SQLite and PostgreSQL; the real folder dataset still needs measurement.
Exact actor-side closure remains unresolved: a membership cycle
whose last base grant is revoked still appears depth-uncertain to the
bounded compiler, while the index proves denial. That shadow failure is a
cutover blocker, not an accepted change to the test expectation. The
large-table sparse/no-access UNION measurements are also still pending.

Prototype validation on this branch:

- The normal verification chain passes: lint, format, strict mypy, pyright,
  3,288 fast SQLite tests and 173 PostgreSQL delta tests. Migration drift
  checks pass. Tier 3 was not run.
- The complete fast SQLite shadow run has 3,235 passing tests and 43
  failures: 19 depth exceptions, four bounded-result differences, 16 query
  construction timeouts, two SQLite expression-limit errors, and two
  existing index SQL-budget assertions affected by the comparison hook.
  The depth exceptions include unresolved role cycles as well as the
  proposed structural-depth contract change; they are not all expected.
  Two of those exceptions, caused by alias-only permission cycles, were
  subsequently fixed and verified by focused shadow tests. The complete
  shadow run has not been repeated after that fix; the other failure groups
  remain open. No blanket exception or accepted-failure list makes it green.
- A 12-hop self-FK fixture at bound 16 establishes that the supported flat
  shape executes on both databases. Other positive recursive shapes still
  produce excessive ORM expression expansion. Those failures block G2;
  increasing the default bound does not fix them.

A review found seven gaps. Each is resolved in the section named:

| Finding | Resolution |
|---|---|
| Identity sets taken from model rows drop an object that exists only in tuples, so a tuple-defined deny can vanish | Membership is evaluated at an identity expression, never at "a row of the model" (§ 2, § 3) |
| Removing the manager hooks removes checks on the row being changed, and 5d is policy, not storage | This proposal changes no authorization rule. Gates keep their rules and entry points; only their mechanics change (§ 10) |
| A guard query followed by a write is not atomic under READ COMMITTED; policy writes lose their lock | A gated queryset write locks its rows, decides on them and writes them by key; policy writes serialize on the generation row (§ 10) |
| A bulk-create cannot be guarded by a query over existing rows | Inserts keep `check_new` per candidate (§ 10) |
| A foreign key's target column need not be the target's REBAC identity | An identity expression carries the field it is a value of (§ 2) |
| Staging caveats makes `accessible(context=…)` disagree with a point check; the frozen walker is order-dependent | No staging: sets decide caveats as 0.24 does; the reference semantics stay the authority (§ 6) |
| Refusing recursion in negative positions rejects ordinary group bans | Nothing is refused for position: a negative position is widened by the rows too deep to decide (§ 8) |

Draft 3 adds two things the first measurements forced:

- **Statement shape** (§ 4). Alternative paths OR-ed as separate `IN`
  subqueries ran into a 120-second statement timeout on tables of six to
  eight million rows. A disjunction is therefore compiled as a union of id
  sets, never as `OR` over subqueries.
- **Actor-side facts are decided, not joined** (§ 5), and the recursion they
  contain is exact. Only structural recursion is unrolled, and real data
  already exceeds the default depth: a folder chain of 12 hops against a
  limit of 8 (§ 8, gate G2).

## Problem

Since 0.23.0 `LocalBackend` answers every read from a derived permission
index. The index copies what Django already stores, twice:

1. each backed column value becomes a row of `rebac_edge`, with its two
   identities interned as text in `rebac_term`;
2. each arrow over that edge becomes rows of `rebac_grant`: one per object,
   holder and permission node.

Both copies must follow the application's columns, so every write to a
watched model runs a maintenance pass under one lock per database. Proposal
0005 introduced field backing so that the column would be the only copy of
the fact. The dual-write it set out to remove is still there; the library
owns it now.

Measured on consumer deployments, 2026-10-01:

| | |
|---|---|
| Index rows for one database | 15.7 million terms, 3.6 GB, for 19 actors |
| Share that are children following a parent | 14.6 million |
| First build | proportional to every row of every resource model |
| One comment post (0.24.1, PostgreSQL) | 4.2 s, 1,308 statements, four maintenance passes |
| …of which index statements | 1,059 statements, 1.05 s in the database |
| …of which the application's own | 249 statements, 0.14 s |
| …Python time | 3.0 s, dominated by building ORM expressions for the passes |
| A pass that changes nothing | 292 statements, 0.97 s |
| The slowest statements of that request | scoped reads of 36 to 56 KB of SQL, planned in 7 to 20 ms, executed in 2 to 7 ms |
| One model save in the test schema (SQLite) | 102 statements, 50 to 60 ms |

Three properties are not fixable inside the design:

- **Size and build follow the application's rows**, not the grants.
- **The answer can be stale.** Fixture loads, bulk writes on plain models,
  through-model instance writes and autocommit saves on tracked models
  bypass maintenance (W010, D2, the pins of proposal 0011).
- **Writes serialize** on one lock row per database.

Proposals 0009 and 0014 make the passes cheaper. They do not remove them.

## Scope

This proposal changes **how a permission is evaluated and what the library
stores**. It changes **no authorization rule**: every gate that exists today
(resource `write` / `create` / `delete`, field gates, invariant 5d and its
entry points) keeps its rule, and `tests/test_security_*.py` stay valid as
they are, except the cases that pin index drift.

Simplifying the gates is a separate decision (§ 13). Once no write needs
maintenance, the gates are the only reason left to intercept writes, and
that question can be judged on its own.

## Rule

1. **The library stores no row per application row.** Its tables hold policy
   (the schema tiers), grants (`Relationship`) and audit events. Nothing is
   built, rebuilt or verified.
2. **A permission is a predicate, compiled from the schema, evaluated at an
   identity.** Structure is read from the application's own columns when the
   statement runs: an arrow over a backed relation is a Django lookup.
3. **Every predicate is compiled as a lower bound or an upper bound of the
   true set, and negation swaps them.** Whatever cannot be decided in SQL (an
   undecided caveat, a chain past the depth bound) is left out of a lower
   bound and kept in an upper bound, so an approximation never grants.

`LocalBackend` shares SpiceDB's schema language, API and semantics
(invariant 1). It does not share its storage: SpiceDB keeps a tuple store
because it cannot see application tables, and `LocalBackend` can.

## Prior art in this repository, and what differs

0.21 and 0.22 compiled scopes at read time (`backends/local_query.py`,
`local_flat.py`, `local_recursive.py`; see `git show bd6ca63`). That compiler
was abandoned for four reasons, and this proposal takes a different position
on each:

| 0.22 | Here |
|---|---|
| Compared identities as text: `CAST(pk AS text) IN (wire ids)`, so the model's own index could not be used | A model column is never converted; a tuple column is, where the two meet (§ 2) |
| Fell back to enumerating ids in Python and embedding them (`ConvertedRelationIds`) | Never enumerates resource ids; an unsupported identity is refused by `rebac.E014` as today |
| Unrolled recursion with frontier probes inside every scoped statement so that overflow raised (RG-01, RG-03, RG-04) | A scope is a lower bound and carries no probe; only a point check that cannot be decided raises (§ 8) |
| Two evaluators answered point checks and scopes, and drifted | One compiled predicate answers both (§ 9) |

## Design

### 1. One function

```
holds(node, at, bound) → Q
```

- `node` is `(type, name)`: a relation or a permission.
- `at` says where the object is in the statement being built (§ 2).
- `bound` is `LOWER` or `UPPER`.

It returns a two-valued `Q` that can be used in whatever queryset `at`
belongs to. Everything the backend answers is built from it (§ 9).

### 2. Identity expressions

An object is never "a row of its model". It is an expression and the meaning
of its value:

```
At(type, ref, key, row)
  ref   an expression in the current query: a column, an OuterRef, a Value
  key   the model field ref is a value of, or None when ref is a wire id
  row   True when the current query's own row is the object
```

- `key` is the model's REBAC identity field for a scope, the foreign key's
  `target_field` for an arrow over a forward FK, and `None` for a tuple
  column. A `to_field` foreign key or a `Meta.rebac_id_attr` identity is
  therefore joined through the field it really refers to
  (`ResolvedFieldBacking.targets_identity_directly()` decides when the two
  coincide, as today; `tests/test_virtual_live_backing.py` pins it).
- **A model column is never converted.** Where a tuple column meets a model
  column, the tuple column is converted with the existing guarded
  `identity_codec(...).to_column()`, inside the tuple subquery. An invalid
  wire id becomes `NULL` and matches nothing. `to_wire()` on a model column
  is unnecessary in predicate comparisons. Enumeration serializes native
  values to wire strings at its output boundary; it must not cast indexed
  model columns to text to test membership.
- Two tuple columns are compared as text, with no conversion.

**The universe of a type** is the union of its model rows, concrete ids named
by either tuple endpoint or field-backing endpoint, constant targets, and
attribute containers. Exclude wildcard and empty-id sentinels and deduplicate
by canonical wire identity. Membership never consults the universe:
an arm that reads tuples does not require a model row, and an arm that reads
a column does not require a tuple. Only enumeration walks the universe
(§ 9). This is what keeps a deny that exists only in tuples.

### 3. Arms

Each arm reads exactly one kind of source.

| Construct at `at` | Source | Predicate |
|---|---|---|
| Stored relation `r` | tuples | `at.ref ∈ resource ids of the tuples of (type, r) whose subject admits the actor (§ 5)` |
| Backed relation, forward FK, holder is the actor | the row | `fk = actor`, inline when `at.row`; otherwise `at.ref ∈ rows where fk = actor` |
| Arrow `via->p`, `via` a forward FK | the row | `holds((T', p), At(T', fk, fk.target_field, row=False))` |
| Arrow `via->p`, `via` a path, reverse or M2M backing, with filters | the row | one `EXISTS` over the existing `ResolvedFieldBacking.queryset()`, whose target rows satisfy `holds((T', p), row)` |
| Arrow `via->p`, `via` stored | tuples | `at.ref ∈ resource ids of the tuples of (type, via) whose subject, as an `At` of `T'` with `key=None`, satisfies `holds((T', p), …)`` |
| Constant `via->p` | none | `holds((T', p), At(T', Value(target_id), None, False))`, uncorrelated; a filtered constant adds its filters on the row |
| Attribute backing | the subject model | a subquery on the subject model keyed by the actor, or by the container value |
| `authenticated`, `anonymous` | none | a constant decided from the actor |
| `+` | | `OR` of the operands at the same `at` |
| `&` | | `AND` |
| `-` | | `left AND NOT right`, with the right operand compiled at the opposite bound |

Every backing kind of proposals 0005 and 0008 keeps its declaration and its
meaning. Generality cost projection, capture, watch maps and codecs when
each kind had to be copied; at read time a path is a path.

Multi-valued joins follow the repository rule: one `Exists`/`OuterRef` or
one `filter(Q & Q)` per join.

### 4. Two-valued arms and statement shape

`NOT` is only sound over predicates that are never `NULL`:

- an `IN` arm is `ref IS NOT NULL AND ref IN (subquery)`, and its subquery
  excludes `NULL` (a converted invalid id, a null column);
- an arm over a nullable foreign key is false, not `NULL`, when the column
  is null (the 0.22.1 rule, kept).

The table of § 3 says what each construct means. How it is written matters
as much. The current hypothesis is that `OR` prevents the useful semi-join
plan and leaves subplans whose hashing depends on `work_mem`; the pending
EXPLAIN report must confirm that diagnosis. On the measured database (`work_mem` 4 MB)
several OR-ed subqueries on a multi-million-row table did not finish in 120
seconds. The index build was running on the same server, so the numbers are
inflated, but the shape is the weak one. The compiler therefore normalizes
before it emits:

- **A disjunction is a union of id sets.** `A + B` at a row is
  `id IN (ids(A) UNION ids(B))`: one semi-join, each branch a plain join
  chain that the planner can drive from the actor's grants.
- **A conjunction is `AND` of semi-joins.**
- **A negated disjunction is a conjunction of `NOT EXISTS`**, one correlated
  anti-join per branch: `NOT (B1 + B2)` is `NOT EXISTS(B1) AND NOT
  EXISTS(B2)`. `NOT IN` is never emitted.
- An arm that reads only the row's own columns stays an inline predicate
  inside its branch.

No `OR` is left above a subquery. (0.22.1 reached the same rule from the
other side: "independent primary-key membership sets".) Gate G1 measures
this shape, not the hand-written OR.

### 5. Subjects

A tuple admits the actor when its subject is:

- the actor itself;
- the wildcard of the actor's type, when the relation allows it and the
  actor has no relation suffix;
- a subject set `T':s#rel`, when `holds((T', rel), At(T', subject_id, None,
  False))`.

The third case is the arrow mechanism. Subject sets, nested groups and role
inclusion need nothing of their own; a group that contains groups is a
recursive node (§ 8).

**Actor-side facts are decided, not joined.** A sub-expression evaluated at
a constant object does not depend on the row: a constant arrow
(`admin->member` on a fixed role), a fixed attribute container, a builtin.
In the measured schema one such arm stands in almost every permission. The
compiler treats these as actor-side facts. Immutable facts fold immediately;
mutable facts require the snapshot protocol below. Cache schema-shaped plans,
not unwitnessed mutable decisions across statements:

- a member of the admin role needs no resource-dependent predicate; any
  required statement-level witness remains, instead of a union branch that
  lists every row;
- for everyone else the arm disappears, and with it the statement text it
  would have repeated in every nested permission;
- recursion that lives entirely on this side (a role that includes roles,
  reached through a constant) is closed by iterating over the tuple table
  from the actor to a fixpoint. The intended closure has no structural depth
  limit. Its cost depends on the actor's actual membership graph; it must not
  be assumed small for every consumer.

Only actor-only immutable builtins may be folded without a database witness.
For a mutable tuple or backing fact, querying in `as_sql` is not sufficient:
READ COMMITTED can observe a revocation between that query and the statement
that reads application rows. A folded decision must retain a same-statement
witness for its truth (including a false decision used under negation), or
use a proven coherent snapshot across the entire operation. A schema-revision
fence alone does not witness tuple or backing changes.

Start with uncorrelated SQL predicates for mutable constant targets; they
avoid listing all resource rows and remain part of the statement's snapshot.
Optimize them into guarded decided branches only when tests and measurements
establish both correctness and benefit. Request caches must not preserve a
revoked folded allow, and a cached plan never embeds an unwitnessed decision.
Exact actor-side recursion must obey the same snapshot rule; do not substitute
an unsafe multi-statement walker merely to avoid a depth bound.

Permission aliases that recurse without traversing a relationship are a
different case: they stay at the same identity. Resolve their positive least
fixed point from the schema, retaining each reachable base arm. Revisiting
such an alias contributes false in both bounds; it does not consume the
structural depth budget or introduce depth uncertainty. A component that
contains an arrow or subject-set traversal is not an alias-only component.

One candidate for tuple-only facts is a graph certificate: capture every tuple
at each visited dependency location, including false caveats and expired rows.
In the final statement, require that no relevant row lies outside the captured
fingerprints and that their count is unchanged, together with schema and time
witnesses. For a witness `W`, a decided fact contributes `W AND lower` and
`NOT W OR upper`. Any new path must enter through a visited dependency. A
failed certificate is uncertainty, not depth overflow or a definitive denial.
Point checks can retry; ordinary lazy querysets can only fail closed without
an additional evaluation protocol. This is a candidate optimization, not a
claim of exact statement-snapshot answers under concurrent changes. Exact
actor-side closure and its concurrency contract remain a cutover obligation.

### 6. Caveats and the three states

A backed relation carries no caveat, so conditions exist only on tuples.

- `Relationship` (both storage shapes) gains `caveat_key`: a write-owned
  digest label of the caveat name and its pinned JSON payload. The tuple
  owner writes it; the library owns every supported tuple write. Reads
  evaluate the stored context and use its stored label, without requiring a
  rehash to match: PostgreSQL JSONB can expand scientific notation into an
  integer. Equivalent stored payloads may have different labels; different
  payloads must never be deliberately merged. In particular, `1` and `1.0`
  can produce different caveat verdicts and retain different labels.
  The wire shape is unchanged. Before compiler reads run, an additive
  migration backfills both relationship tables in deterministic bounded
  batches, including the inactive storage shape. Partial saves, supported
  bulk writes, upserts, fixtures and storage conversion keep the key current.
  An unknown key counts only in an upper bound, never as an unconditional grant.
- Before a statement that can reach a caveat is built, the distinct caveat
  instances of the relations in its plan are decided against the pinned and
  the request context, as `_Verdicts` does today. The condition limit is per
  normalized logical contribution, not a global cap of 256 different caveats
  across the resources in a list query. A schema with no reachable caveat pays
  nothing.
- A tuple is in a **lower** bound when it is unconditional or decided true,
  and in an **upper** bound unless it is decided false.

So sets decide caveats exactly as 0.24 does, and `accessible(context=…)`
agrees with a point check. Enumeration returns definite results only, which
is this library's documented contract; SpiceDB's lookups can also return
conditional results, and that difference is unchanged.

A point check is three-valued: `LOWER` holds, `HAS`; `UPPER` fails, `NO`;
otherwise the result is conditional and names the parameters it still needs.
Only a lower-bound SQL statement can return `HAS`. The other bounds may be
queried separately to avoid repeating a large recursive expression in one
SELECT; later statements may deny, report missing inputs or raise, but cannot
combine their observations into an allow.
Those are computed for that one object by the reference semantics
(`tests/index_reference.py`: paths over caveat instances, independent of the
order of arms and tuples), fed by queries for the tuples and columns on the
object's paths. The frozen walker of `tests/index_oracle.py` is not the
authority for missing parameters: `test_missing_parameters_do_not_depend_on_
the_order_of_arms` shows why.

### 7. Time and the schema fence

- The clock is constant throughout one database statement (Django's `Now()`,
  or one shared execution-time parameter). Expiring tuples compare
  `expires_at` with it. Calling the Python clock separately for each SQL
  occurrence is unsafe when the same fact appears under both polarities.
- Override arms and sites keep their deadlines (`compose_tagged`): an
  extended arm is `arm AND now < deadline`; a disabled or tightened site is
  the identity from its deadline on. Compile from the baseline and all
  permission overrides, including expired ones. Selecting an effective
  permission with the application clock and evaluating it with the database
  clock can otherwise restore a grant before a restriction expires.
- Every statement carries one `EXISTS` on `SchemaGeneration` for the
  revision it was compiled for, as today: a queryset built before a policy
  change returns no rows after it. Caveat-expression overrides are evaluated
  in Python, so their verdicts carry a SQL witness for the time interval in
  which that caveat expression applies, including a lower boundary for an
  already-expired override. A statement outside that interval closes; a
  subsequent lazy execution prepares fresh verdicts. Permission overrides
  use their tagged SQL deadlines rather than this blanket fence.

### 8. Recursion

A node is recursive when it lies on a cycle of the compile graph: a
self-arrow (`parent->read`), a nested group, a role that includes roles.

- **Bounded unrolling.** `R_0` is the body with the recursive reference
  replaced by nil; `R_k` is the body with the reference replaced by
  `R_(k-1)`. The compiler emits `R_D`, `D = REBAC_DEPTH_LIMIT` (default 8).
- **Lower bound:** `R_D`. A chain longer than `D` grants nothing past it.
- **Upper bound:** `R_D OR deep`, where `deep` is "a chain of more than `D`
  hops starts here": for a backed self-FK, `parent^(D+1) IS NOT NULL`; for
  tuples, `D + 1` nested existence tests. A ban whose chain is too long to
  decide therefore applies. No schema is refused for where it uses a
  recursive node outside its own dependency cycle.
- **Monotone cycles only.** Reject a strongly connected dependency component
  containing a negative edge (recursion through the right side of `-`).
  Linearity alone is insufficient: `read = authenticated - parent->read`
  does not have a nil-seeded lower bound. External subtraction of a positive
  recursive component is supported by swapping bounds.
- **Point checks:** when the lower bound fails and the upper bound holds
  with residual uncertainty attributable to depth, the check raises
  `PermissionDepthExceeded`. Track depth as a distinct unresolved atom
  through the complete expression, including subtraction and mixed caveats.
  Definitive short circuits discard irrelevant uncertainty. A remaining
  depth atom raises; otherwise return the canonical missing caveat inputs.
  Never return `CONDITIONAL(missing=[])`. Sets do not raise for depth.
- **Linear cycles only.** A cycle whose body references it more than once
  would unroll exponentially and is refused by the system check and by the
  policy write owners (the successor of E016), as 0.21 refused it.

Unrolling applies to **structural** recursion, the kind that runs through
the application's rows. Actor-side recursion is exact (§ 5). For a backed
self-FK the levels are flat: level `k` is "the `k`-th ancestor is in the
base set", a chain of joins on the parent column, and the statement is the
union of the levels.

Inventory of one consumer schema (129 definitions): eight types have a
permission that reaches itself, all single-node and linear, with no cycle
through several types.

| Recursive permission | Kind | Deepest chain in the data |
|---|---|---|
| a folder's `parent->read`, `parent->write` | structural, self-FK | 12 hops, 66,668 folders, no cycles |
| a page's `parent->read`, `write`, `delete` | structural, self-FK | 2 rows |
| a run's `parent->read` | structural, self-FK | no rows |
| one group permission through a self-relation | structural | flat |
| four role types, `includes->effective_member` | actor-side, tuples | no tuples |

A file inherits the folder recursion through its folder column. Nothing on
the read path of the messaging types, which hold 14.6 of the 15.7 million
rows, is recursive. No reference in a negative position reaches a recursive
node.

**The default depth of 8 is below the real folder chain of 12.** A lower
bound at 8 would hide files that the index shows today. Gate G2 chooses
among:

1. **Raise the bound** for a deployment that needs it
   (`REBAC_DEPTH_LIMIT = 16`). No state. The statement grows by one join
   chain per level, and `rebac check` reports chains past the bound.
2. **A closure table for the one recursive relation**, over the objects of
   the recursive type only (folders, not files). No depth limit, small, but
   writes to that one column are tracked again.
3. **A recursive common table expression.** The natural SQL for it, with no
   state and no limit; Django has no public API for it, so it means SQL the
   project does not write by hand, or a dependency.

The proposal builds option 1 first, because it needs nothing new, and
measures it on the folder data before reads switch.

### 9. What is built from `holds`

| Operation | Implementation |
|---|---|
| Queryset scope | `M.filter(holds(node, At(T, identity column, identity field, row=True), LOWER))`, returned by the existing `Backend.queryset_filter()` seam |
| Point check | the same predicate at `At(T, Value(id), None, False)`, selected from the fenced `SchemaGeneration` row; one statement may prove `HAS`, with separate uncertainty probes allowed by § 6; no model row required |
| `accessible()` | the lower bound over each part of the universe: rows, tuple-named ids, constant targets, attribute containers |
| `lookup_subjects()` | candidates gathered upward from the one resource (tuple subjects, subject-set members, backed holder columns), filtered by `LOWER`; omit uncertain candidates without aborting definite results |
| `check_new` | unchanged: the walker over the candidate's proposed relations, with arrow hops answered by the point check |
| Field read gate `read__f` | the scope predicate of `read__f` as a boolean annotation |
| Empty-id model-level check | a row-independent grant or any definitely accessible identity in the complete universe; never a literal `id=""` lookup |
| `grants_all()` | a conservative row-independent proof using the same expression semantics |

A point check and a scope share the lower predicate at two kinds of `at`:
`HAS` agrees with inclusion for an existing row. Their uncertainty surfaces
differ: a point check reports missing caveat inputs or raises on relevant
depth uncertainty; enumeration omits uncertain candidates. Conditional
evaluation cannot construct an allow from graph facts read in several
READ COMMITTED snapshots that never coexisted; SQL remains the allow authority.

### 10. Writes

**Maintenance is removed.** `IndexMaintenance`, `model_write`,
`tuple_owner`, old/new capture, deferred passes, snapshot terms and the
`IndexState` lock go. A raw fixture, a bulk write and a through-row
`create()` are reflected in reads the moment they commit, because nothing
was copied.

**Gates keep their rules and their entry points.** `RebacMixin.save_base`
and `delete`, the scoped and tracked querysets, the related-manager wrappers,
the `m2m_changed` receiver and invariant 5d stay. Their mechanics change:

- The gate's reads go through the compiled predicate.
- **Inserts** keep `check_new` per candidate, including
  `proposed_relationships()`; a query over existing rows cannot authorize a
  row that is not there.
- **Queryset `update` and `delete`** follow one contract: lock the
  statement's rows (`SELECT … FOR UPDATE` on the primary keys), decide on
  exactly those rows, and write exactly those rows by key, in one
  transaction. Rows that start matching the filter later are not written,
  and a denied row denies the whole statement. This replaces the snapshot
  that the index tables held under the global lock.
- **Instance saves** lock their row in the gate query.
- **What is not serialized any more:** a concurrent change to a grant or to
  a parent's column is not serialized with the target-row gate. Authorization
  uses the gate statement's snapshot, not transaction commit order. A
  revocation may commit before a write authorized earlier. Applications that
  need stronger ordering require an explicit stronger transaction protocol;
  SERIALIZABLE also requires application-level retries.
- **Policy writes** (schema rows, overrides, `sync`) take the
  `SchemaGeneration` row `FOR UPDATE` and validate the composed program under
  it, so two valid changes cannot combine into a refused one. Create-or-lock
  the singleton before policy reads or mutations, including after flush and
  fresh installation. Test concurrent first writers. SQLite needs its
  supported transaction serialization, not a `FOR UPDATE` no-op.
- **Dangling tuples.** A deleted resource or subject row still has its
  tuples removed by the existing explicit-sender `post_delete` receiver; a
  reused identity must not inherit a grant.

Proposal 0011's pins (for example the moved row's own `write` on a reverse
FK `add(bulk=True)`) stay open and stay pinned. Proposal 0013's read half
stays as it is; its write half is no longer needed for maintenance and is
needed for gates exactly as far as § 13 decides.

### 11. What the library stores, and what is removed

Stored: `SchemaDefinition`, `SchemaRelation`, `SchemaPermission`,
`SchemaCaveat`, `SchemaOverride`, `SchemaGeneration`, `Relationship`
(with `caveat_key`), `PermissionAuditEvent`.

Removed by this proposal:

- `rebac/index/` except `conditions.py`, `codec.py` and `time.py`, which
  move; `models/index.py`; a migration drops `rebac_term`, `rebac_edge`,
  `rebac_membership`, `rebac_grant`, `rebac_index_work`, `rebac_index_state`;
- the maintenance owners and every call to them in `mixins.py`,
  `managers.py`, `signals.py`, `relationship.py`, `roles.py`,
  `memberships.py`, `schema_write.py`;
- `rebac index rebuild` and `rebac index verify`;
- checks E013 (readiness), E015, E017, E018 and W010 in their current
  meaning; D2.

Not removed by this proposal: `RebacTrackedMixin`, `TrackedQuerySet`, the
injected base manager, the related-manager wrappers and the gate code in
`signals.py`. They exist for gates now, not for maintenance (§ 13).

Added: `rebac/compile/` (the predicate compiler, identity expressions,
unrolling) and the single-object evaluator for conditional results.

### 12. Other backends

`queryset_filter()` returning `None` keeps today's fallback to
`accessible()` id lists, so the `Backend` API is unchanged. A later
`SpiceDBBackend` can use the same split: Django resolves the structural part
of a permission and the backend answers for the objects that hold grants,
instead of one tuple per application row. Not part of this proposal.

### 13. Follow-up, decided separately: the gates

After this proposal the gates are the only reason to intercept writes. Two
positions, for a proposal of its own:

- **Keep 5d as it is** and finish proposal 0011 on top of the compiled
  predicate: the affected declaring rows of a change are a queryset through
  the backing path, and the check is one statement.
- **Narrow it:** a column or M2M field is part of the row of the model that
  declares it, and changing it through any Django API requires that row's
  `write`. This equals 5d for every backing declared on the model that holds
  the column. It differs only for backings declared from another type
  (reverse and multi-hop paths), which would be gated by the holding row
  alone. It would let `RebacTrackedMixin`, `REBAC_TRACKED_MODELS`, the
  tracked signals and the Python emulation of ORM expressions go.

The second is the larger simplification and is a policy change, so it is
the owner's decision, with `tests/test_security_*.py` as the bar.

## Correctness

- **No staleness.** A read sees the columns and tuples of its own snapshot.
- **Approximations never grant.** A lower bound omits what it cannot decide;
  an upper bound keeps it; negation swaps the two.
- **No row required.** A tuple-defined grant or deny holds for an object
  with no model row.
- **One predicate** for point checks and scopes.
- **Pinned actor, strict mode, sudo, audit** are unchanged.

Deliberate differences to list in ARCHITECTURE and for the conformance
suite: sets are lower bounds at the depth limit; a point check past the
limit raises; objects on a structural data cycle are treated as too deep in
an upper bound. Actor-side cycles still require the exact closure of § 5.

## Cost

- A scoped read is one statement whose size follows the unfolded
  permission, times `D + 1` for a recursive node. A plan-size check replaces
  E019. The index's own scoped reads are 36 to 56 KB today, so size is a
  budget to measure, not a new risk.
- PostgreSQL plans against real tables with real statistics. The risk is a
  permission with several `OR` paths on a large table for an actor who can
  see little or nothing; gate G1 measures it.
- A save costs its gate and the write. The comment post above becomes the
  application's 249 statements plus gates.
- A gated queryset write reads the primary keys it will write.
- `lookup_subjects()` costs in proportion to the candidates above one
  resource.

## Compatibility

Unchanged: `.zed` schemas and directives, the names exported by `rebac`,
relationship storage in both shapes, the gates' rules.

Changed, observably:

- reads no longer require `sync` to have built anything, and never report
  E013 for a missing index;
- a point check on a chain deeper than `REBAC_DEPTH_LIMIT` raises
  `PermissionDepthExceeded` again (0.23 answered at any depth); sets omit
  what lies past the limit;
- concurrent grant changes are no longer serialized against writes (§ 10);
- `rebac index …` commands, the index tables and their checks are gone;
- `Relationship` rows gain `caveat_key`.

## Tests

- **Shadow differential over the existing suite.** While the index still
  exists, a test-only hook answers every `LocalBackend` read both ways and
  fails on any difference in tests populated from source relationships and
  application rows. Synthetic index-only fixtures cannot be shadowed: retain
  them only while the index exists, or replace them with source-based cases.
  Compare lazy scopes at execution after changes and deadlines; keep shadow
  instrumentation out of query-budget tests. Tier 1 and the PostgreSQL delta
  exercise the compiler before switching. Expected differences are listed
  by name (depth overflow), not tolerated in bulk.
- **Reference semantics:** `tests/index_reference.py` stays the authority;
  the reference and differential suites are retargeted from the index to
  the compiler.
- **Security suites:** `tests/test_security_*.py` unchanged, minus the cases
  that pin drift.
- **No copy:** after any sequence of model writes no `rebac` table has a new
  row; after a raw fixture, a base-manager `update` and a through-row
  `create()`, reads are correct with no command run.
- **Tuple-only objects:** a grant and a deny on an id with no model row, in
  a subject set and behind an arrow.
- **Identity:** `to_field` and `rebac_id_attr` targets; an invalid wire id
  matches nothing on each vendor; no cast on a model column in compiled SQL.
- **Bounds:** undecided caveats and over-deep chains on both sides of `-`
  and `&`; point-check overflow raises.
- **Concurrency:** a row that starts matching between gate and write is not
  written; two policy writes that are each valid and jointly refused.
- **Budgets:** statement counts for a resource save and for the
  comment-post shape; SQL size and compile time per scope.

## Decision gates

- **G1, plans.** On the 15.7-million-row database, with compiler-generated
  SQL in the shape of § 4: thread page, newest 50 across threads, counts,
  point checks, for an admin, the heaviest non-admin, a sparse actor and an
  actor with no access. Report compile, planning and execution time.
  Hand-written filters only establish feasibility; their first result is
  that the OR shape does not finish and must not be emitted. The union shape
  is being measured (requested 2026-10-01, report pending). If the union
  shape is also poor for sparse actors, stop: the design does not hold.
- **G2, recursion.** The inventory of § 8 is done for one schema. Decide
  among the three options there with the folder data: statement size and
  plans at a bound of 16. Verify the supported SQLite and PostgreSQL engines
  can execute that statement shape, and resolve the actor-side closure
  obligation in § 5; increasing the structural bound does not resolve it.
- **G3, contracts.** The compatibility list above, accepted before reads
  switch: depth behaviour, the concurrency contract, `caveat_key`.
- **G4, compilation cost.** Public ORM per statement, or the accepted
  plan-cache exception of 0.23.1. Measured.
- **G5, the gates.** § 13, separately.

## Order of work

1. `rebac/compile/` beside the index, used by nothing, with the shadow
   differential hook. Non-recursive constructs first, then recursion, then
   caveat verdicts.
2. Shadow suite green on SQLite and PostgreSQL; G1 and G2 measured; G3
   accepted.
3. Reads switch to the compiler. The index is still maintained, so a
   regression can be reverted by one commit.
4. Maintenance is removed; gates move to the row-lock contract.
5. The migration drops the index tables; commands and checks go.
6. ARCHITECTURE, ZED, AGENTS and the ROADMAP are rewritten to match.

## Relation to the 1.0 roadmap

- Steps 1 and 2 (read scope for every alias) become simpler: the scope of an
  alias is `holds` at that alias's identity column.
- Step 3 (write compilers) is not needed for maintenance; whether it is
  needed for gates follows § 13.
- Step 5 (single storage) stands. Step 6 (identity as a stored column) is
  unnecessary: no model column is converted.
- Step 8 (0009) is moot.

## Not in scope

- Any change to an authorization rule (§ 13).
- `SpiceDBBackend` and its projector.
- Scoping `select_related` joins (0013's read half).
- A recursive CTE, which needs SQL the project does not write by hand.
