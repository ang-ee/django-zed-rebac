# Proposal 0009: maintenance that propagates only actual changes

**Status:** superseded by [proposal 0015](./0015-permissions-compiled-to-queries.md).
It was implemented in 0.24.2; with permissions compiled to queries there is
no maintenance pass. The text below is kept as written.

## Problem

A maintenance pass computes its whole region before deriving anything. Any term
whose rows *might* change pulls in every dependent: the scopes whose arrows read
it, the sets that contain it, and so on to a fixpoint. The pass then deletes and
re-derives every row of that region.

Most of that work rewrites rows that come out identical. Measured on the
painkiller development seed (SQLite, 0.23.2, first 121 passes of the seed test):

| | Count |
|---|---|
| Region scopes re-derived | 747 |
| …whose grant rows changed | 376 |
| Grant rows re-derived | 7,571 |
| …that differ from before | 1,696 (22%) |

In the heaviest passes, 30 to 61 scopes are re-derived and exactly one, the
written object, changes. The seed ran 682 passes and 49 minutes of maintenance
on 0.23.1; per-write cost for this schema is seconds, and a seed through the
owners holds the index lock for all of it.

## Rule

A scope's rows for a node are a function of four inputs:

1. the scope's own edges for the relations the node reads;
2. the rows of the nodes it references at the same scope, sites included;
3. the rows at each arrow target for the arrow's target node;
4. the type-level rows those arrows apply to targets of their type.

If none of its inputs changed, its rows cannot change. A pass therefore needs to
re-derive a scope only when one of its inputs actually changed, and it can find
out stratum by stratum.

## Design

The owner, capture and projection steps are unchanged: the pass captures the
written objects, projects their new edges, and knows which scopes' edges changed.

1. **Seed.** The seed is the set of scopes and sets whose edges changed. There is
   no up-front closure.
2. **Memberships.** Derive memberships for the seed sets and compare them with
   the rows before. A set whose members changed brings in the sets that contain
   it; repeat to a fixpoint. Grants hold sets by reference, so membership changes
   never propagate into grants.
3. **Grants, one stratum at a time, in dependency order.** For each stratum:
   - its region is the seed's scopes, plus every scope with an input among the
     rows changed in earlier strata: a same-scope reference, an arrow whose
     target's rows for the target node changed, or an arrow to a type whose
     type-level rows changed;
   - read the region's current rows for the stratum's nodes, delete them,
     derive, and compare: the scopes whose rows differ (in any payload field:
     holder, site, expiry, condition) are this stratum's changes;
   - a recursive stratum repeats this until no new scope joins, on top of the
     existing semi-naive rounds.
4. Work rows, logging and the lock are unchanged.

The comparison reads the region's rows into Python, as `rebac index verify`
does; it is bounded by the region, not by the index. Conditions and derivation
stay in SQL.

## Algorithm (binding)

The first implementation of the design above was unsound in three places: a
recursive stratum deleted rows of other strata, a membership cycle kept a
revoked member, and an arrow missed changes to a via relation that has no
grant rows. All three are the same mistake: deciding "nothing changed" from
rows that were themselves stale, or from the wrong rows. This section replaces
the informal steps with rules that make that impossible. Where it differs from
§ Design, this section wins.

**The one invariant.** A derivation step may read a row only if every input
of that row is final. "Unchanged" is concluded only by comparing rows before
and after such a step. Two situations break the invariant if handled
naively, and each gets its own rule: recursion (a row can support itself
through a cycle) and inputs that are not rows (edges).

**Vocabulary.** A *pass* has the seed terms its owners captured. `E` is the
set of `(resource term, relation)` whose projected edges differ after
projection, split into `E+` (an edge was added) and `E-` (an edge was removed
or its payload changed: expiry, condition, source). `X` is the set of
`(scope term, node)` whose grant rows differ after their stratum was derived.

### 1. Edges

Project the seed terms' edges and write only the difference (as implemented;
review found it sound). Record `E+` and `E-`.

### 2. Memberships

The set graph is recursive in the data (a set may contain sets, in cycles), so
it is one recursive unit and follows the recursion rule.

- **Seed sets** `S0`: every captured set term of the pass, whether or not its
  edges changed. (A new set term with no edge change still needs its members
  derived: a const-backed userset on a newly created row.)
- **If `E-` touches any set in `S0`** (a member or contained set was removed,
  or a membership payload changed): the region is `S0` plus every set that
  contained a seed before the pass, transitively, read from the membership
  rows as they stand before any deletion, to a fixpoint. Delete the region's
  member rows in one step and derive the region to its fixpoint. No
  comparison is needed and none is trusted: this is what 0.24.1 does, and it
  is the only sound treatment of a removal inside a cycle, because a set that
  is not cleared keeps the revoked member and hands it back.
- **Otherwise (additions only)**: derivation is monotone, so every existing
  row is still true and a set outside the region can only be missing rows,
  never hold wrong ones. Process in waves: delete and derive the wave's sets,
  compare each set's rows before and after, and the next wave is every set
  that directly contains a changed set (through an edge whose subject is the
  changed set), including sets already processed. Stop when a wave changes
  nothing. Rows only grow, so it terminates at the least fixpoint.

Grants hold sets by reference, so membership changes never enter `X`.

### 3. Grants, stratum by stratum in dependency order

For stratum `K`, the affected scopes `D_K` are the union of:

1. **Edge inputs.** For every node `N` of `K` and every relation `r` that `N`
   reads as edges, the scopes `S` with `(S, r)` in `E`. A node reads as edges:
   its own relation if it is a relation node, and the `via` relation of each of
   its arrows. Arrows read the edge table, not the via relation's grant rows,
   so a via relation that is used only as a subject set (no grant rows) still
   seeds its arrows. This is an input of `N` at `S`, not something learned
   from the via node's rows.
2. **Same-scope inputs.** The scopes `S` with `(S, N')` in `X` for a node
   `N'` that `N` references at the same scope. A site (`&`, `-`) stores no
   rows; it is changed at `S` when either operand is.
3. **Arrow inputs.** For each arrow `via -> N_t` of `N`: the scopes with a
   `via` edge to a target `T` with `(T, N_t)` in `X`; and, when the type-level
   scope of the target type is in `X` for `N_t`, every scope with a `via`
   edge to that type.

Then:

- **Non-recursive stratum** (one node, no self-reference): region is `D_K`.
  Read its rows, delete them, derive, read again, compare. Scopes whose rows
  differ in any field (holder, site, expiry, condition, condition key) go
  into `X`.
- **Recursive stratum**: close `D_K` first. The region is `D_K` plus every
  scope that reads, through an arrow of a node of `K` to a node of `K`, a
  scope already in the region, to a fixpoint (the type-level case as in 3).
  Delete the region's rows for the stratum and derive to the stratum's
  fixpoint, then compare into `X`. The closure is taken before deriving for
  the same reason as in memberships: a scope left out could hand back a row
  that only existed because of the scope being recomputed. Recursive strata
  are positive (the program refuses `&` and `-` inside a cycle, `rebac.E016`),
  which is what makes delete-then-derive converge to the right fixpoint.

**Rows of a stratum are addressed by its `(type, node)` pairs**, never by the
product of its types and its node names: a two-type stratum
`{(folder, view), (project, access)}` must not touch `(folder, access)`.

There is no `membership_only` shortcut. A write to a relation that no node
reads and no arrow follows produces an empty `D_K` for every stratum, which
is the same saving, derived from the inputs rather than asserted by a flag.

### 4. Bookkeeping

Regions and change sets may be held in Python, but no statement may carry a
parameter list that grows with a region: reads, deletes and region writes go
through fixed-size chunks (5,000 ids) or a subquery over the work table.
A region of 33,000 scopes must complete on SQLite.

Nested passes (0.24.0) keep their rule: a nested finish derives its own
effects and leaves the outer pass's captured rows alone; `E` and `X` belong to
one finish.

## Correctness

By induction over the steps. Edges are compared against the previous
projection, so `E` is exact. For memberships, the removal branch recomputes a
region that is closed under "contained a seed", so no row outside it depended
on anything removed; the additions branch is monotone and its waves reach
every set that gains a row. For grants, when stratum `K` starts, every earlier
stratum is final, so the inputs listed in 3 are final and `D_K` contains every
scope with a changed input; a scope outside `D_K` has unchanged inputs and
therefore unchanged rows. In a recursive stratum the closure makes the region
closed under "reads within the stratum", so nothing outside it can depend on a
row being recomputed. A removed row is a difference, so revocations propagate
through `X`.

The drift oracle (`verify`: the maintained index against a full rebuild)
checks the claim on every existing case, and a seeded randomised test checks
it after every step on schemas built from the shapes above: two-type recursive
strata, membership cycles of length two to five, arrows whose via is a
subject-set-only relation, exclusion and intersection, expiring and caveated
tuples.

## Cost

Per stratum, one read of the region's rows and one comparison. In exchange, the
seed's measured re-derivation drops roughly fourfold overall, and 30- to 60-fold
in its heaviest passes. Passes that change many scopes cost about what they cost
today.

## Tests

- Every existing maintenance and derivation test, with its drift check.
- New cost tests: a write whose effect stays on its own object re-derives only
  that object; a write to a thread that changes no derived row re-derives none of
  the thread's messages; a revocation still reaches every dependent.
- The painkiller seed, re-measured: passes, rows re-derived, rows changed, time.

## Not in scope

- Batching writes across a bulk operation. Once a pass costs what its change
  costs, batching matters much less; it can be proposed separately.
- Finer locks than one per database (decision D3).
