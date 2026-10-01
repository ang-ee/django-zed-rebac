# Proposal 0013: integrate at the ORM's SQL compilers, not at the models

## Problem

The engine (schema, index, derivation, maintenance passes) is sound. The
layer that connects it to Django is not, because Django has no plugin point
for "every model", so the integration attaches to each model (`RebacMixin`,
`RebacTrackedMixin`) and then chases every other way Django touches a row:

| Path | How it is intercepted today |
|---|---|
| `save()` / `delete()` on a mixin model | `save_base` and `delete` overrides |
| `update()`, `bulk_update()`, `bulk_create()`, queryset `delete()` | `RebacQuerySet` / `TrackedQuerySet` overrides |
| reverse-FK `add`/`remove`/`set`/`clear`, M2M in both directions | patched related-manager classes, a `ContextVar` of pre-gated pairs, an `m2m_changed` receiver |
| auto-created through tables | the through model's `objects` manager replaced |
| collector CASCADE and SET_NULL | `post_delete` receivers, `_base_manager` injection, delete scopes in a `ContextVar` |
| third-party models (`User`, `Group`, `REBAC_TRACKED_MODELS`) | `pre_save`/`post_save`/`pre_delete` receivers, the D2 autocommit warning |
| `raw()`, `explain()`, pickling, `refresh_from_db()` | one override each |
| read scope | `RebacQuerySet._fetch_all` and friends; `select_related` is unsafe, bare `prefetch_related` warned by W003 |

Each path is its own gate with its own actor resolution and its own
exemption. Four fix rounds between 2026-09-30 and 2026-10-01 each closed some
paths and opened others; the final review of 16e0c25 still found an
MTI child bypassing the M2M gate through lineage matching, a crafted `Case`
that the Python re-implementation of CASE resolves differently from SQL, and
`RawSQL` reaching a write through a `Q` or an annotation that the Python
expression walker does not traverse. Every one of these is the same defect:
the integration re-implements a piece of Django's query semantics in Python
and gets it slightly wrong. The engine never sees the statement Django is
about to run.

Django has exactly one place every ORM statement passes through:
`SQLCompiler`, `SQLInsertCompiler`, `SQLUpdateCompiler` and
`SQLDeleteCompiler`. Every path in the table above, including the collector,
related managers, through tables, third-party models, `select_related` joins
and subqueries, hands a `Query` to one of them before SQL exists. The `Query`
carries the model, the columns written, the where clause and the joins, and
the compiler produces the exact SQL.

## Rule

A statement that reads a resource table is scoped, a statement that writes a
resource or watched table is gated and maintained, and both happen at
compilation, from the `Query` the statement is built from. There is one gate,
one scope and one maintenance entry; a path that compiles is covered.

## Design

### Installation

The consumer selects REBAC's database backend:

```python
DATABASES = {"default": {"ENGINE": "rebac.db.backends.postgresql", ...}}
```

`rebac.db.backends.{postgresql,sqlite3,mysql}` subclass Django's backends and
set `compiler_module = "rebac.db.compiler"`. Nothing else changes in the
backend. `django-tenants` has shipped on this mechanism for years.

Models no longer opt in. A model is a resource because the schema declares its
type and `rebac_resource` or the registry maps the type to the model; a table
is watched because a backing path reads it. `RebacMixin` keeps its instance
conveniences (`check_access`, `with_actor`, `sudo`) and loses its write
machinery; `RebacTrackedMixin`, `REBAC_TRACKED_MODELS` and the tracked
signals go.

### Reads: `SQLCompiler`

`as_sql()` rewrites the `Query` before compiling: for every alias that refers
to a resource table, the scope predicate for the carrying actor and action is
added to that alias, whether it is the base table, a `select_related` join, a
`Subquery`, an `Exists` or a combined query. The predicate is the one
`index.read.scope_q` builds today, so SQL for the base-table case stays
byte-identical with 0.24 (the parity suite pins this). No actor in strict mode
raises `MissingActorError` at compile time; a bypass compiles with no
predicate. `select_related` and bare `prefetch_related` become safe, which
closes the open question ARCHITECTURE reserves a custom compiler for, and
W003 is retired.

Field read gates move here too: a gated column of a resource alias is
projected as `NULL` unless the actor holds `read__<field>`, so `values()`,
annotations, `F()` copies, `RawSQL` over a column and `refresh_from_db()`
are covered by the one rewrite instead of one check per API.

### Writes: the insert, update and delete compilers

Before producing SQL, the write compilers:

1. Resolve the actor from the carrier (below) and the bypass state.
2. Freeze the statement's rows in the database: the `Query`'s where clause,
   as a subquery, not a materialised pk list.
3. Compute the edge changes from the `Query`: the written columns and, for a
   watched column, the values the statement will assign, evaluated by the
   database (`SELECT <expression> FROM <frozen rows>`), never re-implemented
   in Python. A value the database cannot evaluate ahead of the write does
   not exist; there is no `Case` emulation.
4. Run the one gate: `write` (or the type's declared write permission) on
   every affected resource row of every declaring type whose backing watches a
   changed column or through table, plus the create and delete gates for
   inserts and deletes. This is proposal 0011's rule, applied once.
5. Open the index owner for the statement and capture old state, then let
   Django run the SQL, then capture new state. The collector's cascade rows,
   `bulk_update`'s batches and related-manager writes all arrive here as
   ordinary statements, so they are gated and maintained with no special
   case; the D2 autocommit distinction disappears because the statement and
   its maintenance are one compiler call.

### The actor carrier

`with_actor(actor)` stores the actor on the `Query` (Django's `_hints`
mechanism, which clones with the query), so a pinned actor still outranks the
ambient one (invariant 5) and reaches subqueries and prefetches built from the
same queryset. `actor_context()` and the middleware remain the ambient source.
Instance sudo pins on the instance as now and is read by the compiler of that
instance's own statement only (invariant 5a).

### What stays outside

`cursor.execute`, `raw()` and `extra()` bypass the compilers' rewrites, as
they bypass every ORM guarantee; they stay refused under scope and documented
as such. `Relationship` writes keep the tuple owner. SpiceDB as a backend is
unaffected: the compilers consult the active backend for the scope predicate,
and for SpiceDB they fall back to `accessible()` id lists as today.

## Correctness

Maintained and gated paths coincide by construction: both are the set of
statements the compilers see. The gate decides on values the database
computed from the statement's own `Query`, so arm order, coercion and vendor
casts cannot make SQL disagree with the decision. Frozen rows are a subquery
over the statement's predicate, so a concurrent change to an unwatched column
cannot widen the written set past the gated one on READ COMMITTED. The read
rewrite applies the predicate per alias, so a join cannot expose a row the
base-table scope would hide.

## Cost

- One backend module per vendor and a settings line for every consumer:
  breaking, and the reason this is a 1.0 change.
- The compiler classes are not covered by Django's deprecation policy. They
  are stable in practice and the project pins Django 6.0; each Django upgrade
  re-runs the parity suite.
- A table-name lookup on every statement in the application, including
  statements that touch no REBAC table.
- Two evaluations for a watched-column write: the gate's `SELECT` of the
  values, then the write.

## Migration

0.24.x keeps the current integration. 1.0 ships the backends and the
compilers, removes `RebacTrackedMixin`, `REBAC_TRACKED_MODELS`, the
related-manager patches, the through-manager swap, the tracked signals, the
`_base_manager` injection, the write overrides on `RebacMixin` and
`RebacQuerySet`, and W003; `RebacMixin` becomes optional. A system check
(`rebac.E022`) fails startup when a resource model is served by a database
whose engine is not a REBAC backend, so a missed settings line fails closed.

This proposal replaces proposal 0011 and the maintenance half of proposal
0012 (pass-scoped capture); 0012's override rules stand on their own.

## Tests

- The parity suite: for every write path in the table above, the SQL the
  compiler emits and the gate decision are compared with the 0.24 behaviour
  on SQLite and PostgreSQL; the read scope SQL for base-table queries is
  byte-identical.
- The review probe corpus from the 0.24 rounds (`r3` to `r7`: crafted
  `Case`, `RawSQL` through `Q` and annotations, MTI children, mirrored pairs,
  consumer handlers during related-manager calls, pk-changing updates,
  collector rows, through-table base-manager writes) becomes a permanent
  tier-1 suite; every case must deny.
- `select_related` and bare `prefetch_related` on a resource relation return
  scoped rows.
- A write of 100,000 rows on SQLite and PostgreSQL completes with one gate
  query per declaring type.
- `E022` fires for a non-REBAC engine and is silent for each wrapper.

## Decision gate

Before implementation: a spike that installs the PostgreSQL wrapper and
rewrites only the read scope for the base-table alias, run against
`tests/test_queryset_permission_parity.py` and `tests/test_scope_*.py`. If the
emitted SQL is not byte-identical with `RebacQuerySet` for those suites, the
design is wrong at the cheapest point to find out.

## Not in scope

- The single storage layout (its own proposal); the compilers work with either.
- The identity codec's read-side validation (`_Conversion.as_sql`).
- `SpiceDBBackend`.
