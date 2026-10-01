# Proposal 0015: permissions compiled to queries over the application's tables

**Target version:** a green-field re-implementation of `LocalBackend`
evaluation; candidate shape for 1.0.
**Status:** draft for review. The decision gates at the end are open.
**Supersedes, if accepted:** proposals 0009, 0011 and 0014, the write half of
0013 and the maintenance half of 0012.

## Problem

Since 0.23.0 `LocalBackend` answers every read from a derived permission
index. The index copies what Django already stores, twice:

1. each backed column value becomes a row of `rebac_edge`, with its two
   identities interned as text in `rebac_term`;
2. each arrow over that edge becomes rows of `rebac_grant`: one per object,
   holder and permission node.

Both copies must follow the application's columns, so the library intercepts
every way Django writes a row, and because an FK change now moves permission
rows, invariant 5d gates those writes by re-deriving in Python what the ORM is
about to do. Proposal 0005 introduced field backing so that the column would
be the only copy of the fact. The dual-write it set out to remove is still
there; the library owns it now.

Measured on consumer deployments, 2026-10-01:

| | |
|---|---|
| Index rows for one database | 15.7 million terms, 3.6 GB, for 19 actors |
| Share that are children following a parent | 14.6 million (message parts, edges, participants) |
| First build | proportional to every row of every resource model |
| One comment post (0.24.1, PostgreSQL) | 4.5 s, 1,308 statements, four maintenance passes |
| The cheapest of those passes | 82 statements to insert 10 index rows |
| The dearest | 292 statements, 0.97 s, no row changed |
| One model save in the test schema (SQLite) | 102 statements, 50 to 60 ms |
| A development seed (0.23.1) | 682 passes, 49 minutes of maintenance |

Three properties are not fixable inside the design:

- **Size and build follow the application's rows**, not the grants. Most rows
  hold no grant of their own.
- **The answer can be stale.** Fixture loads, bulk writes on plain models,
  through-model instance writes and autocommit saves on tracked models bypass
  maintenance (W010, D2, the pins of proposal 0011). An authorization answer
  then depends on a cache that ordinary Django code can skip.
- **Writes serialize.** Every watched write holds one lock row per database
  for its whole transaction.

Proposals 0009, 0011, 0013 and 0014 each repair a consequence of the copies.
None removes them.

## Rule

1. **The library stores no row per application row.** Its tables hold policy
   (the schema tiers), grants (`Relationship`) and audit events. Nothing is
   built, rebuilt or verified.
2. **A permission is a predicate over the declaring model**, compiled from
   the schema. Structure is read from the application's own columns when the
   statement runs: an arrow over a backed relation is a Django lookup.
3. **A write to an application row is that statement and its gate.** No
   capture, no pass, no lock.

`LocalBackend` shares SpiceDB's schema language, API and semantics
(invariant 1). It does not share its storage: SpiceDB keeps a tuple store
because it cannot see application tables, and `LocalBackend` can.

## Prior art in this repository, and what differs

0.21 and 0.22 compiled scopes at read time (`backends/local_query.py`,
`local_flat.py`, `local_recursive.py`, removed in 0.23.0). That compiler was
abandoned for four reasons, and this proposal takes a different position on
each:

| 0.22 | Here |
|---|---|
| Compared identities as text: `CAST(pk AS text) IN (wire ids)`, so the model's own index could not be used | Compare in the model column's native type; convert on the tuple side only (§ 2) |
| Fell back to enumerating ids in Python and embedding them (`ConvertedRelationIds`) when a conversion was not native | Never enumerate; an unsupported identity is refused by `rebac.E014` as today |
| Unrolled recursion and added frontier probes so that overflow raised exactly where the walker raised (RG-01, RG-03, RG-04) | Overflow denies; a recursive node in a negative position is refused (§ 9) |
| Recompiled the predicate through the ORM for every statement | Compile once per schema revision; the accepted plan-cache exception stays available (gate G5) |

## Design

### 1. What is compiled

For a resource type `T` with model `M`, permission or relation `N` and actor
`a`, the compiler returns a `Q` on `M`:

```
P(T, N, a)  →  Q        "the rows of M on which a holds N"
```

`Backend.queryset_filter()` already exists as the seam; `LocalBackend`
returns this `Q`. Everything else is built from it:

| Operation | Implementation |
|---|---|
| Queryset scope | `M.filter(P)` |
| `accessible()` | `M.filter(P).values_list(<identity>)` |
| Point check without caveats | `M.filter(<identity>=id).filter(P).exists()` |
| Bulk guard for `update()` / `delete()` | the statement's rows `.exclude(P)` must be empty: one query |
| Field read gate `read__f` | the same predicate as a boolean annotation |

A type with no table (a role namespace, an attribute container) is compiled
over the tuple table alone, in the same way, yielding wire ids.

### 2. Identity domains

Each type has one identity domain: the column type of its model's identity
field (`pk` or `Meta.rebac_id_attr`), or wire text when it has no table.

- A set is always produced in the domain of its type.
- A model column is never converted. Where a tuple column meets a model
  column, the tuple column is converted, inside the tuple subquery, with the
  existing guarded `identity_codec(...).to_column()`. An invalid wire id
  becomes `NULL` and matches nothing, on every vendor.
- `to_wire()` on a model column disappears from every read.

So a scope uses the application's primary-key and foreign-key indexes as they
are, and the conversion cost is per matching tuple, not per row.

### 3. How each construct compiles

`ids(T, N, a)` below is `M.filter(P(T, N, a)).values(<identity>)`: a subquery,
never a list.

| Construct | Predicate on a row `x` of `M` |
|---|---|
| Stored relation `r` | `x.id IN (tuples of (T, r) whose subject matches a, § 4)` |
| Forward FK backing, subject is the actor's type | `x.fk = a` |
| Forward FK backing, used through an arrow `via->p` | `x.fk IN ids(T', p, a)` |
| Path, reverse or M2M backing (0008), with filters | `EXISTS` over the existing `ResolvedFieldBacking.queryset()`, target in `ids(T', p, a)` |
| Arrow over a stored relation | `x.id IN (tuples of (T, via) whose subject id is in ids(T', p, a))` |
| Unfiltered constant `via->p` | `EXISTS(<a holds p on the fixed target>)`: uncorrelated |
| Filtered constant | the filters on `x`, and the same `EXISTS` |
| Attribute backing | a subquery on the subject model keyed by the actor |
| `authenticated`, `anonymous` | a constant decided from the actor |
| `+` | `OR` |
| `&` | both operands as id sets: `x.id IN left AND x.id IN right` |
| `-` | `x.id IN left AND NOT (x.id IN right)` |

Every backing kind of proposals 0005 and 0008 keeps its declaration and its
meaning. Generality was expensive only because each kind needed projection,
capture, watch maps and gates; at read time a path is a path.

Set operands are always id sets on the row's own identity, never negated
lookups on a nullable column, so a `NULL` foreign key cannot turn an exclusion
into `NULL` (the 0.22.1 rule).

Multi-valued joins follow the repository rule: one `Exists`/`OuterRef` or one
`filter(Q & Q)` per join, never chained filters on the same path.

### 4. Subjects

A stored tuple grants the actor when its subject is:

- the actor itself;
- the wildcard of the actor's type, when the relation allows it;
- a subject set `T':s#rel` on which the actor holds `rel`.

The third case is compiled exactly like an arrow: the tuple's subject id is
in `ids(T', rel, a)`. Subject sets and arrows are one mechanism, and nested
groups need no separate closure. A group that contains groups is a recursive
relation and falls under § 9.

### 5. Time and the schema fence

- The clock is one expression bound when the statement executes (the
  existing `_ExecutionTime`). Expiring tuples compare `expires_at` with it.
- Override arms and sites keep their deadlines (`compose_tagged`): an
  extended arm is `arm AND now < deadline`; a disabled or tightened site is
  the identity from its deadline on. A lazy queryset evaluated after a
  deadline is therefore correct without recompiling.
- Every statement carries one `EXISTS` on `SchemaGeneration` for the revision
  it was compiled for, as today: a queryset built before a policy change
  returns no rows after it.

### 6. Caveats

A backed relation carries no caveat, so conditions exist only on tuples.

- **Sets** (scopes, `accessible()`, bulk guards) are two-valued, as SpiceDB's
  lookups are. In a positive position a tuple counts when it is unconditional
  or its caveat is decided true; in a negative position it counts unless
  decided false. This is the polarity rule of the current reads.
- Deciding a caveat for a set needs its distinct instances evaluated before
  the statement is built (the current `_Verdicts`). Stage 1 ships without
  that: a caveated tuple is undecided in sets. Stage 2 adds it (gate G4).
- **Point checks** stay three-valued: definite predicate holds, `HAS`;
  possible predicate fails, `NO`; otherwise the walker evaluates that one
  object and reports the missing parameters.

### 7. One object: the walker

`schema/walker.py` already evaluates one object in three states with caller
callbacks; `check_new` uses it today. It becomes the single-object executor:

- conditional point checks (§ 6);
- `check_new` on an unsaved instance, whose backed relations are the
  instance's own column values;
- `lookup_subjects()`: expand upward from the one resource, collect the
  subjects that tuples and backed holder columns name, expand subject sets,
  then keep the candidates the check admits.

The compiler and the walker implement one semantics twice. A differential
suite holds them together (§ Tests); the walker is the oracle it already is
in `tests/index_oracle.py`.

### 8. Writes

- **Tuples.** `write_relationships()` and the delete calls validate, write
  and advance the Zookie. Nothing is derived, so a nested write has nothing
  to finish and a read inside the transaction sees it.
- **Resource rows.** `RebacMixin.save_base` and `delete` keep their gates:
  `check_new` for an insert, `write` (and `write__<field>`) for an update,
  `delete` for a delete. Queryset `update`, `delete` and `bulk_create` use
  the one-query guard of § 1.
- **Every other model.** No tracking. `RebacTrackedMixin`,
  `REBAC_TRACKED_MODELS`, the tracked signals, the injected base manager, the
  through-manager swap and the related-manager patches are removed. A raw
  fixture, a bulk write and a through-row `create()` are correct the moment
  they commit, because nothing was copied.
- **Dangling tuples.** A deleted resource or subject row still has its tuples
  removed by the existing explicit-sender `post_delete` receiver, because a
  reused identity must not inherit a grant. Deletes that send no signal need
  `rebac relationships prune`, which is a housekeeping command and not a
  correctness dependency of reads on existing rows.

**Invariant 5d is replaced** (gate G3). Its rule existed because a column
write moved rows of an index. The new rule: a write to a column is authorized
as a write of the row that holds it, by that row's `write` and
`write__<field>` when its model is a resource; a non-resource model is
protected by the consumer, as 5d already says for types without `write`.
A schema that derives one type's permission from another model's rows makes
those rows part of its policy; the schema guide says so.

### 9. Recursion

A node is recursive when it lies on a cycle of the compile graph: a
self-arrow (`parent->read`), a nested group, or a cycle through several
types. SQL joins a fixed number of steps, and the ORM has no recursive common
table expression, so recursion needs a decision (gate G2). The proposal for
stage 1:

- **Bounded unrolling.** A linear cycle (one recursive reference in its
  body, in a positive position) is compiled `REBAC_DEPTH_LIMIT` times
  (default 8): level 0 replaces the reference with nil, level `k` with level
  `k - 1`. For a backed self-FK this is `x.parent IN (level k-1)`.
- **Overflow denies.** A chain longer than the bound grants nothing past it.
  `rebac check` gains a query that reports backed chains deeper than the
  bound. The walker keeps raising `PermissionDepthExceeded`.
- **Negative positions are refused.** A truncated set on the right of `-`
  would grant. A recursive node that is reachable in a negative position is
  a schema error, in the system check and in the policy write owners
  (successor of E016).
- **Non-linear cycles are refused** (two recursive references in one body),
  as 0.21 did, before the SQL can grow exponentially.

If a consumer's data needs more than the bound allows, the next step is a
closure table for that one relation, over the objects of the recursive type
only, and still nothing per row of the types that follow it.

### 10. What the library stores, and what is removed

Stored: `SchemaDefinition`, `SchemaRelation`, `SchemaPermission`,
`SchemaCaveat`, `SchemaOverride`, `SchemaGeneration`, `Relationship`,
`PermissionAuditEvent`.

Removed:

- `rebac/index/` (about 5,600 lines) and `models/index.py`; a migration drops
  `rebac_term`, `rebac_edge`, `rebac_membership`, `rebac_grant`,
  `rebac_index_work` and `rebac_index_state`;
- the maintenance owners, deferred passes and the per-alias lock;
- in `signals.py`, everything but the tuple cleanup on delete and the schema
  receivers;
- `TrackedQuerySet`, `TrackedManager`, `RebacTrackedMixin`,
  `REBAC_TRACKED_MODELS`, base-manager injection and E023;
- `rebac index rebuild` and `rebac index verify`;
- checks E013, E015, E016, E017, E018, E019 and W010 in their current
  meaning; D2.

Added: one package, `rebac/compile/` (the predicate compiler, identity
domains, unrolling). Budget: under 1,000 lines.

Kept as they are: the parser and AST, composition and overrides, caveats,
`Relationship` and its validation, actors, `with_actor` / `sudo` / strict
mode, the evaluator, audit, the DRF, MCP and GraphQL adapters, `build-zed`.

### 11. Other backends

`queryset_filter()` returning `None` keeps today's fallback to
`accessible()` id lists, so the `Backend` API is unchanged. A later
`SpiceDBBackend` can use the same split this design makes: Django resolves
the structural part of a permission, and the backend answers for the objects
that hold grants. That would ship grants and anchors to SpiceDB instead of
one tuple per application row. It is not part of this proposal.

## Correctness

- **No staleness.** A read sees the columns and tuples of its own snapshot.
  There is no derived state to disagree with them.
- **Fail closed.** An invalid tuple id converts to `NULL`; an unresolvable
  backing is a schema error; a chain past the bound is denied; a recursive
  node in a negative position is refused; an undecided caveat is absent in a
  positive position and present in a negative one.
- **Pinned actor, strict mode, sudo, audit** are unchanged: they live in the
  manager and the mixin, not in evaluation.
- **Deliberate divergences from SpiceDB** to list for the conformance suite:
  overflow denies in sets where SpiceDB errors; the refusals of § 9.

## Cost

- A scoped read is one statement with one subquery per arm of the unfolded
  permission. Its size follows the schema, not the data. A plan-size check
  replaces E019.
- PostgreSQL plans it against real tables with real statistics. The risk is
  a permission with several `OR` paths on a large table and no selective
  filter; gate G1 measures exactly that.
- A save costs its gate (one or two statements) and the write. The comment
  post above becomes the application's own statements plus gates.
- `lookup_subjects()` costs in proportion to the candidates above one
  resource. It was complete and index-backed in 0.23; it is complete and
  walker-backed here.

## Migration for consumers

- Schemas, directives and the public API of `rebac` do not change.
- Remove `RebacTrackedMixin` and `REBAC_TRACKED_MODELS`; a declared base
  manager no longer needs to be a `TrackedQuerySet`.
- Run the migration that drops the index tables. No rebuild exists.
- Code that relied on 5d's cross-type gate must gate the model that holds
  the column.

## Tests

- **Differential:** compiled predicate against the walker, on generated
  schemas and data, for every construct of § 3, on SQLite and PostgreSQL.
  The existing reference and oracle suites are retargeted.
- **No copy:** after any sequence of model writes, no `rebac` table has a
  new row; after a raw fixture, a base-manager `update` and a through-row
  `create()`, reads are correct with no command run.
- **Statement budgets:** a resource save is at most its gate statements plus
  the write; the comment-post shape has a fixed budget independent of the
  schema's size.
- **Identity:** an invalid wire id in a tuple matches no row on each vendor;
  a scoped read's plan uses the model's own indexes (no cast on a model
  column in the compiled SQL).
- **Recursion:** depth bound, overflow denial, the negative-position and
  non-linear refusals.
- **Security pins:** the cases of `tests/test_security_*.py` are re-derived
  from the rules above; those that pin index drift are deleted with it.

## Decision gates

- **G1, read plans.** Hand-written filters for the consumer's messaging
  chain on the 15.7-million-row database: thread page, newest 50 across
  threads, counts, point checks, for an admin and the heaviest non-admin.
  Requested 2026-10-01; pending. If the plans are poor for the several-path
  permissions, compile each path as an id set and union them before going
  further.
- **G2, recursion.** Which types in real schemas arrow into themselves, and
  how deep. Bounded unrolling (stage 1) or a closure for the one relation.
- **G3, invariant 5d.** Accept the replacement rule of § 8.
- **G4, caveats in sets.** Stage 1 without `_Verdicts`, stage 2 with.
- **G5, compilation cost.** Build the `Q` through the public ORM per
  statement, or reuse the accepted plan-cache exception of 0.23.1. Measured,
  not assumed.

## Order of work

1. The compiler for non-recursive schemas and the differential suite,
   beside the index, behind no public switch.
2. Reads move to the compiler; point checks to compiler plus walker.
3. Gates move to the one-query guard; the maintenance owners, tracking and
   5d machinery are deleted.
4. The migration drops the index tables; commands and checks go.
5. Recursion per gate G2; caveats in sets per gate G4.
6. ARCHITECTURE, ZED, AGENTS (invariants 3, 5d and the pitfalls on base
   managers) and the ROADMAP are rewritten to match.

## Relation to the 1.0 roadmap

- Step 3 (write compilers) is no longer needed for maintenance or 5d.
- Steps 1 and 2 (read scope for every alias) remain worth doing and become
  simpler: the scope of an alias is a `Q` on that alias's model.
- Step 5 (single storage) stands. Step 6 (identity as a stored column) is
  unnecessary: no model column is converted.
- Step 8 (0009) is moot.

## Not in scope

- `SpiceDBBackend` and its projector.
- Scoping `select_related` joins (0013's read half).
- A recursive CTE, which needs SQL the project does not write by hand.
