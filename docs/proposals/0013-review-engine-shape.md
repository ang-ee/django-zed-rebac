# Review of proposal 0013 after 0.25.0: a database engine instead of managers and querysets

**Status:** report, 2026-10-02. Not a proposal and not a decision. It reads
[proposal 0013](./0013-orm-compiler-integration.md), the 1.0 section of
[ROADMAP.md](../ROADMAP.md) and [proposal 0015](./0015-permissions-compiled-to-queries.md)
§ 10 and § 13 against the code released as 0.25.0 and against Django 6.0.4 as
installed. Nothing was run for it: every statement about Django comes from
reading its source, and every statement about this library from reading its
code and the test markers.

## Conclusion

Moving enforcement into Django's SQL compilers removes the class of defect
that six review rounds kept finding in 0.25.0: the library deciding in Python
what a statement will select or write. It does that for reads that compose
(joins, subqueries, set combinations), for field gates, and for writes that
Django issues on its own.

It does not remove three things, and proposal 0013 as written assumes it does:

1. **Intent.** Django's own reads (cascade collection, uniqueness checks,
   `refresh_from_db`, the existence check in `save()`, related-manager
   bookkeeping) reach the same compiler class as the application's reads, with
   nothing that marks them. "Every statement that reads a resource table is
   scoped" would scope them too, and a scoped cascade leaves orphans or fails
   a foreign key.
2. **The actor.** A compiler never sees the queryset or, for an `UPDATE` or a
   `DELETE`, the instance. `Query._hints`, which 0013 and the roadmap name as
   the carrier, does not exist. An attribute set on a `Query` survives cloning,
   but Django builds a fresh `Query` for every related manager, every
   `save()`, every cascade and every default prefetch.
3. **Snapshots.** The witnesses of decided rows and sets exist because
   decisions and the statement that uses them run in separate READ COMMITTED
   statements. Where the predicate is attached does not change that.

So the shape that holds up is not "an engine instead of managers". It is an
engine that enforces, with a thin manager layer that says what the caller
meant and who the caller is. The enforcement code (about 4,100 of the 5,134
lines in the six integration modules) moves or goes; the verbs stay.

The recommended next step is the spike 0013 already asks for, with two things
added to its gate (§ 7).

## 1. The tensions, and what the engine does to each

"Engine" below means compiler classes selected through the database backend.

| Tension met in 0.25.0 | Root cause | With the engine |
|---|---|---|
| Projection guard found incomplete in four review rounds (unions, joined columns, instance-form unions, inherited fields) | The guard works out in Python what a query selects | **Solved.** Each operand of a combination and each subquery is compiled by its own compiler instance, which holds the real select list and `klass_info`. A gated column is rewritten where it is selected; there is nothing to refuse and no layout to work out |
| A plain queryset as root of a union drops the scope of a rebac operand (pinned) | Django compiles operands itself; the scope lived on the queryset | **Solved**, given a rule for statements without a carrier (§ 3) |
| A gated field a joined multi-table child inherits is not guarded (pinned) | The guard does not know which model a join came through | **Solved.** `Join.join_field.related_model` and `parent_alias` give the model of every alias |
| `filter(folder__name__startswith=…)`, `order_by("folder__name")`, `select_related` read rows of another type unscoped | Scope is applied to the base table only | **Solved for rows**, with a semantic decision to make (§ 4.2). Filtering and ordering on a gated *field* is a separate oracle and needs its own rule (§ 4.3) |
| `Case` in `bulk_update` resolved differently by Python and by SQL; literal SQL behind a `Q` or an annotation (seven pinned tests) | The write gate reads the ORM call | **Solved.** The values a statement will assign are selected from the statement's own rows by the database |
| Through-model `_base_manager` writes, base-manager `update` of a watched scalar, MTI child related-manager calls (pinned) | Each path needs its own interception | **Solved.** They are ordinary `INSERT` / `UPDATE` / `DELETE` statements |
| Collector CASCADE and SET_NULL gated under the ambient actor, reverse-FK `add(bulk=True)` ignores the pinned actor (0011 pins) | The pinned actor lives on a queryset or an instance the path does not pass through | **Not solved by the engine.** The compiler sees pk batches and no instance. A carrier is still needed (§ 3.2) |
| Hand-written SQL in a selection (now: under sudo only) | Cannot be inspected | **Unchanged**, but stated once: the compiler sees `RawSQL` and `extra` as nodes of the statement and refuses them on a statement that touches a resource table under an actor |
| Field redaction loads `accessible()` for the whole type, per gated field | Redaction happens after rows are materialised | **Solved.** The field's predicate is part of the row's own statement, in the same snapshot |
| Witness complexity: hierarchy false allow, decision clock, "does this key prove the row" | Decide-then-bind across statements at READ COMMITTED | **Not solved.** The engine adds one lever (§ 5) |
| A consumer wants a batched insert of multi-table child rows inside the library's `bulk_create` | The create gate lives in the library's `bulk_create` | **Dissolves.** Any insert path, including Django's private `_batched_insert` called by the consumer, reaches the insert compiler and is gated there |
| Third-party models (`User`, `Group`) gated on instance saves only | They cannot take the mixin | **Solved for writes.** Reads of such models still need a decision on who opts in (§ 3.3) |
| Seven statements of overhead per write call (two savepoint pairs, two revision reads, audit) | One owner per manager call | **Changes shape:** one gate per statement. Not measured |

## 2. What a Django engine can and cannot see

Django 6.0.4; paths relative to `django/`.

**The mechanism.** `Query.get_compiler` asks `connection.ops.compiler(name)`
for one of five classes (`db/models/sql/query.py:370`,
`db/backends/base/operations.py:380`). Every ORM read and write passes
through one: iteration, `count`, `exists`, `aggregate`, `explain`, `update`,
`bulk_update`, `bulk_create`, `Model.save`, `delete` and its cascades,
related managers, through-table writes, prefetches, subqueries
(`Query.as_sql`, `query.py:1323`, builds a new compiler on the same
connection) and each operand of a set combination
(`db/models/sql/compiler.py:584`).

**What the compiler holds.** The live `Query`: `alias_map` with a `Join` per
alias (`table_name`, `parent_alias`, `join_type`, `join_field`), the where
tree, the select list and, after `get_select`, `klass_info` with the position
of every column of every selected model. The model of an alias follows from
`join_field.related_model`.

**What it does not hold.**

- The queryset, and for `save()` → `UPDATE` and for `DELETE`, the instance
  (`db/models/base.py:1190`: values and `pk=` only). `INSERT` does carry the
  instances (`InsertQuery.objs`).
- Any mark of origin. `Collector.related_objects` (`deletion.py:410`),
  `refresh_from_db` (`base.py:739`), `_save_table` (`base.py:1086`), forward
  and reverse one-to-one descriptors (`related_descriptors.py:170`, `:449`),
  foreign key validation (`fields/related.py:1125`) and unique validation
  (`base.py:1538`) are ordinary `SELECT`s.
- Raw SQL: `raw()`, `cursor.execute`, `RunSQL`. `extra()` fragments pass
  through as opaque strings; Django itself uses `extra(select=…)` in
  many-to-many prefetch (`related_descriptors.py:1160`).
- Rows already in Python: `_result_cache`, prefetch and foreign-key caches.

**Hazards specific to these classes.**

- The same compiler class builds DDL fragments (constraints, indexes,
  generated fields, `db_default`) and runs `Q.check()` with `Query(None)`.
  Every hook must be a no-op there.
- Compiling is not executing. `str(qs.query)`, `explain`, and probes that
  call only `get_select` or `get_order_by` compile without running;
  `SQLUpdateCompiler.as_sql` can run a `SELECT`. Read predicates belong in
  `as_sql`; gates belong in the execute paths, of which `UPDATE` has two in
  6.0 (`execute_sql` and the new `execute_returning_sql`).
- The four write and aggregate classes subclass the base `SQLCompiler`, not
  the vendor's, and the vendors override different classes (PostgreSQL the
  insert compiler, MySQL the update and delete compilers). One
  `rebac.db.compiler` module cannot serve three vendors; the composable form
  is one mixin per class, applied over whatever the backend's
  `ops.compiler(name)` returns.
- `QuerySet.update()` is not atomic, `save()` is atomic only for multi-table
  models, and deleting a single object takes a path with no transaction. A
  gate that locks rows must open its own `atomic` block.
- The compiler files are not covered by Django's deprecation policy. 5.2 →
  6.0 restructured `SQLInsertCompiler.as_sql` and added
  `execute_returning_sql`; 6.1 renames several hooks and removes PostgreSQL's
  `SQLCompiler` subclass. Each Django feature release needs a pass.

## 3. Three things proposal 0013 does not settle

### 3.1 Intent: which statements are scoped

0013's rule ("a statement that reads a resource table is scoped") is too
strong. Today the meaning is carried by the manager: `objects` is scoped and
strict, `_base_manager` is not. Django relies on the second for its own
correctness, and the project rule "don't replace `_base_manager` with the
scoped manager" exists for that reason. An engine that scopes everything
re-creates that mistake at a lower level, where it cannot be undone per path.

The rule that keeps today's meaning:

- A statement is scoped when its root `Query` carries a scope marker. The
  scoped manager sets it. Every alias of a resource table in that statement,
  every subquery and every operand inherits it.
- A statement whose root is a resource model and carries no marker is
  Django's or the base manager's, and is not scoped. Strict mode stays where
  it is: a scoped queryset with no actor raises `MissingActorError`.
- A statement whose root is *not* a resource model but joins or combines a
  resource table is the open case. Scoping its resource aliases under the
  ambient actor closes the pinned plain-root union and the same leak through
  a join. Django's own reads of this shape are few (cascade collection and
  through-table bookkeeping select one table). This needs the spike to
  confirm against the whole suite.

Writes are different: a gate applies to every statement that writes a watched
column or a resource row, whoever built it. That is the point of the engine.

### 3.2 The actor: what carries it

- `Query._hints` does not exist; `_hints` is on managers and querysets and
  only reaches the database router. What does work: an attribute set on the
  `Query` survives `clone()` (`query.py:396` copies `__dict__`), so it reaches
  `filter`, `update`, `exists`, `count`, subqueries and explicit `Prefetch`
  querysets built from the same queryset.
- It is lost wherever Django starts a new `Query`: every manager call, and
  therefore related managers, forward descriptors, default prefetches,
  `refresh_from_db`, `save()`, cascades and through-table writes.

So invariant 5 ("an instance loaded under an actor saves under that actor")
cannot be met by the engine alone. Three sources, in order:

1. The marker on the `Query` (pinned actor or bypass), for statements built
   from a scoped queryset.
2. A context variable set by the model's `save` / `delete` for the duration
   of the call, carrying the instance's pinned actor. This needs the mixin on
   the model; it is the one piece of write machinery that stays. It also
   gives cascades and reverse-FK `add` the pinned actor that proposal 0011's
   pinned tests ask for.
3. The ambient actor, for everything else (third-party models).

Consequence for the roadmap's step 4: "`RebacMixin` becomes optional" holds
for models that accept ambient-actor semantics. A model that wants a pinned
actor on its instances, or field gates on its instances (§ 4.3), keeps the
mixin. Stamping the actor on loaded instances still happens in the queryset's
fetch hook; the compiler does not build instances.

A context variable is read when the statement is compiled, which is when it
is evaluated. A queryset iterated in a streaming response after the
middleware has returned has no ambient actor; under strict mode that raises,
which is the right failure.

### 3.3 Opt-in: what makes a model a resource

0013 moves opt-in to the schema: a model is a resource because a type is
declared for it. For read scope and for the backed-edge gates that is sound.
For the row's own create, write and delete gates it would put `User` and
`Group` under `auth/user#write` the moment the schema declares it, and
`update_last_login` then fails at login. Keeping those gates opt-in per model
(the mixin, or a registry flag) avoids a class of surprises in third-party
code; the engine makes either choice implementable.

## 4. Reads in detail

### 4.1 Where the predicate goes

- Base table of a `SELECT`: AND-ed into the where clause the compiler builds
  in `pre_sql_setup`. This is the case the 0013 decision gate tests, and the
  compiled predicate (`compile.read.scope_q`) is already an expression whose
  SQL is produced at compile time, so byte-identical output is plausible.
- A joined alias: for a LEFT OUTER join the predicate must be in the ON
  clause. There is no per-statement hook for that
  (`get_extra_restriction` is per field; `FilteredRelation` is part of join
  identity). What is left is overriding `get_from_clause` and compiling a
  wrapper around the `Join`, without mutating `alias_map`. The source
  anticipates such wrappers but does not promise them.
- The predicate must be self-contained SQL correlated on the alias's identity
  column (an `EXISTS` or `IN`), which is what the compiled predicate already
  is at `At(type, column, field, row=True)`.

### 4.2 A decision the engine forces: what an unreadable related row means

- A join that `filter()` or `exclude()` added: the base row is excluded when
  the related row is not readable. This is Odoo's `any` and closes the filter
  oracle.
- A join that `select_related`, `values`, an annotation or `order_by` added:
  the related row reads as NULL and the base row stays. Django makes
  `select_related` over a non-null foreign key an INNER join, which would
  drop the base row; the wrapper has to compile it as LEFT OUTER. The related
  object is then `None` while `fk_id` is set, which application code may not
  expect.

Neither is specified today. Both belong in the spec before step 2 of the
roadmap is built.

### 4.3 Field gates

0013 says a gated column "is projected as `NULL` unless the actor holds
`read__<field>`". Three findings:

- **Rewrite the select list, not column compilation.** Replacing the SQL of a
  selected column with `CASE WHEN <read__f at this alias> THEN col END` in
  `get_select` covers model instances, `values()`, joined models and every
  operand of a combination. Intercepting every `Col` instead would also reach
  join conditions, DDL, `GROUP BY` and `DISTINCT ON`, which compare SQL text;
  the failure modes are not verified and it is not needed.
- **Computed values and filters are not covered by the select list.** An
  annotation over the column, a filter on it and an ordering by it compile
  the column elsewhere. Annotations and aggregates over a gated column can be
  refused, as today, or have the same `CASE` applied inside the expression.
  Filtering and ordering on a gated field is an oracle today and stays one
  unless the rule is extended; that is a policy choice.
- **A NULL on an instance is written back.** `save()` writes every loaded
  field, and the update compiler cannot tell a redacted `None` from a real
  one. Today the mixin remembers redacted fields and leaves them out of
  `update_fields`. With the engine, the statement can select one boolean per
  gated field, which Django sets on the instance as an attribute, and the
  mixin reads it. A model with field gates therefore keeps the mixin.
  Dropping the column instead (deferral) does not work: access triggers
  `refresh_from_db`, the column is dropped again, and the access fails.

What goes: `visible_id_sets` and its whole-type `accessible()` loads, the
projection guard and its helpers, `relation_loading.py` (395 lines), W003.

## 5. Witnesses and snapshots

The engine does not make decided rows or decided sets unnecessary; they exist
for the planner. It also does not make their witnesses unnecessary; those
exist for READ COMMITTED.

What it adds: the engine owns the connection. It can read the isolation level
at connect, and when a statement and its decisions run inside one
`atomic()` block at REPEATABLE READ or above (or on SQLite), compile without
witnesses. Proposal 0015 § 5 already allows "a proven coherent snapshot".
The costs are the ones the Odoo comparison listed: isolation is the
consumer's setting, serialization failures need a retry loop Django does not
have, and sets kept across transactions in an evaluator scope still need the
witness.

Independent of the engine, and smaller: binding an arrow's decided keys
through the target's own rows (`col IN (SELECT key FROM target WHERE key IN
(decided) AND holds(row))`) drops a row that left the set instead of emptying
the page, and removes the "does this key prove the row" rule.

## 6. Writes in detail

The roadmap's step 3 stands, with these corrections.

- **Frozen rows.** 0013 says "as a subquery, not a materialised pk list";
  0015 § 10 says lock the statement's rows by key, decide on those, write
  those by key. The second is what 0.25.0 does and what Django's own update
  compiler does when it materialises ids. The compiler form is:
  `SELECT pk[, <assigned expressions>] … WHERE <statement's where> FOR UPDATE`,
  decide, then write `pk IN (locked keys)`. The assigned expressions
  evaluated in that `SELECT` are what removes the `Case` emulation.
- **Two execute paths for `UPDATE`.** A gate only in `execute_sql` misses
  `save()` calls that return values (`execute_returning_sql`).
- **Transactions.** The compiler opens `atomic` itself.
- **Upserts.** `bulk_create(update_conflicts=True)` changes rows the compiler
  has not read. Keep refusing it under an actor.
- **Inserts.** The instances are visible; `check_new` per candidate stays.
- **Deletes and cascades.** They arrive as pk batches with no origin. The
  gate applies; the actor is the one from § 3.2. Scoping the collector's
  *reads* must not happen (§ 3.1).
- **MySQL.** No `UPDATE … RETURNING`, no self-select in a subquery on plain
  MySQL. The key-list form works; the vendor suite has to cover it.
- **The rule itself.** Whether to keep invariant 5d or narrow it to the row
  that holds the column (0015 § 13) is still the owner's decision. The engine
  makes the full rule implementable in SQL (affected declaring rows are a
  queryset through the backing path over the frozen rows, old and new). The
  narrow rule needs none of that.

What goes: the write overrides on `RebacQuerySet` and `TrackedQuerySet`, the
related-manager patches, the through-manager swap, the injected base manager
and E023, the tracked signals, `RebacTrackedMixin`, `REBAC_TRACKED_MODELS`,
the `Case` emulation and the pre-gated pair tracking: most of `signals.py`
(1,204 lines).

## 7. The spike, and what to add to its gate

0013's decision gate: a PostgreSQL wrapper that rewrites only the base-table
scope, with SQL byte-identical to `RebacQuerySet` on the parity and scope
suites. Keep it, and add:

1. **Installation by composition.** Implement the wrapper as mixins applied
   over `ops.compiler(name)`, not as a compiler module per vendor. Gate: the
   same wrapper works over `postgresql`, `sqlite3` and PostGIS's backend
   unchanged.
2. **Inertness.** With the wrapper installed and no marker on any query, the
   whole tier 1 suite passes unchanged, including migrations, DDL compile and
   `Q.check`. This proves the hooks are no-ops where they must be.
3. **The carrier.** The marker is an attribute on the `Query`, set by the
   existing manager. Gate: a scoped queryset used as a `Subquery`, in a
   union, and through an explicit `Prefetch` keeps its actor with the
   queryset-level scope code switched off.
4. **Overhead.** Time per statement added to an unmarked statement, measured.

Kill criteria: SQL that is not byte-identical for the base-table case; a hook
that cannot be made inert for DDL; or a join predicate that needs mutating
`alias_map`.

After the spike, in this order, each with its pinned tests turning green as
the progress meter: joins and combinations (§ 4.1, § 4.2); field gates in the
select list (§ 4.3); the update compiler's gate with database-evaluated
values; inserts and deletes; then removal of the old layer path by path.

## 8. Costs and risks

- A settings change for every consumer, or an engine wrapper that names its
  base engine. A check (`rebac.E022`) must fail startup when a resource model
  is served by a connection without the compilers; otherwise a missed setting
  fails open.
- Private Django API, touched in feature and sometimes patch releases. The
  project pins one Django feature release; each new one needs a parity run
  before it is supported.
- Every statement in the process passes through the hooks, including
  sessions, the admin and other libraries.
- Composition with another library that also replaces compilers depends on
  that library's classes cooperating through the MRO. Not verified for any
  third-party engine: none is installed here, and 0013's remark that
  `django-tenants` ships on this mechanism could not be checked.
- The semantic decisions in § 3.1, § 4.2 and § 4.3 change observable
  behaviour (a related object that reads as `None`; a base row excluded by a
  filter across an unreadable relation).
- Size, estimated and not measured: of about 5,100 lines in the six
  integration modules, roughly 3,000 go, roughly 1,100 stay (verbs and
  instance API, the watch map, the `check_new` walker), and the engine
  package is new code on the order of 1,500 lines across five compiler
  mixins.

## 9. Corrections to proposal 0013 and the roadmap

- `Query._hints` does not exist (0013 § The actor carrier; ROADMAP step 4).
  Use an attribute on the `Query`, and a context variable for statements
  Django builds.
- "Instance sudo … is read by the compiler of that instance's own statement"
  works for `INSERT` only; `UPDATE` and `DELETE` do not carry the instance.
- "A statement that reads a resource table is scoped" needs the marker rule
  of § 3.1.
- "Models no longer opt in" holds for read scope and backed-edge gates, not
  for pinned actors, field gates on instances, or (by recommendation) the
  row's own write gates.
- "`rebac.db.backends.{postgresql,sqlite3,mysql}` … set `compiler_module =
  "rebac.db.compiler"`": one module cannot serve three vendors.
- "Frozen rows as a subquery, not a materialised pk list" contradicts 0015
  § 10 and what Django's update compiler does.
- "Projected as `NULL`" needs the write-back rule of § 4.3.
- The table of intercepted paths still lists `post_save` receivers and
  `index.read.scope_q`; there is no `post_save` receiver, and the function is
  `compile.read.scope_q`. The roadmap says twelve context variables; there
  are ten.
- `projection_field_names` in `field_visibility.py` has no caller since
  0.25.0 and can be removed.

## 10. Decisions for the owner

1. The scoping rule for statements without a marker (§ 3.1), in particular
   the plain root that joins or combines a resource table.
2. What an unreadable related row means under `select_related` and under
   `filter` (§ 4.2).
3. Whether filtering and ordering on a gated field is refused, redacted or
   left as it is (§ 4.3).
4. Whether the row's own create, write and delete gates follow the schema or
   stay opt-in per model (§ 3.3).
5. Invariant 5d: keep or narrow (0015 § 13). The engine makes either
   implementable.
6. Isolation: whether to support a witness-free mode for consumers who run
   REPEATABLE READ with retries (§ 5).
7. Installation: an explicit engine per vendor, or one wrapper over a named
   base engine.
8. The Django support policy once the library depends on compiler internals.
