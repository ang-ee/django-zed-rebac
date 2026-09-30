# Proposal 0009: maintenance that propagates only actual changes

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

## Correctness

Each stratum re-derives every scope whose inputs changed, because inputs from
earlier strata are known when the stratum starts, and inputs from its own stratum
are covered by the repeat. A scope left out has unchanged inputs and therefore
unchanged rows. A removed row counts as a change, so revocations propagate. The
drift oracle (every maintenance test ends with `verify`, which compares the
maintained index with a full rebuild) checks this on every existing case.

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
