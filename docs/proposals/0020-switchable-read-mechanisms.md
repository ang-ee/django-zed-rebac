# 0020: Switchable read mechanisms

Status: **draft, not accepted.** Every mechanism below is a setting that is off by default, so that a consumer
can turn each on, compare cost and results with `rebac.testing.compare_mechanisms`, and decide. Nothing here
changes a statement while its setting is off. None is released.

## Problem

A read is authorized today by one statement shape: the rows whose id is in the set of ids the actor holds. That
shape is right for an actor with few grants reading a large table. It is the wrong way round for the common read
of an application: an actor with many grants reading a page of rows and the rows related to them. On the scale
fixture (50,000 threads and messages, 500,000 parts; an actor granted 25,000 threads; registry layout,
PostgreSQL 16, JIT off) 100 threads with their messages and parts cost 234 ms in 6 statements and 20 KB of SQL.
django-guardian reads the same data in 28–30 ms.

Where the time goes:

- the two child statements each repeat the parent's permission, though the policy says a child of a readable
  parent is readable;
- the root statement builds all 25,000 granted ids, converting each from text with a validity guard, before it
  meets `id <= 200`;
- a bounded question (which of these 100 ids may the actor read) is one statement over the whole reachable
  policy, however few the ids.

## The mechanisms

| Setting | What it changes | What keeps today's statement |
|---|---|---|
| `REBAC_STRUCTURAL_PREFETCH` | Children of parents a prefetch just admitted are read by foreign key, where the policy proves the parent's action grants theirs | An intersection or exclusion above the arm, a filtered or generic backing, an override |
| `REBAC_ROW_SCOPE` (`True`, `"named"`) | The scope of a statement a queryset runs itself is written as conditions on each row (`EXISTS` a grant that reaches it) | A recursion, a caveated or expiring relation, a backing the walk does not read, more than 24 subqueries, an embedded, combined, counted or aggregated queryset |
| `REBAC_BOUNDED_WALK` (`"prove"`, `"decide"`) | `held` and `held_many` are answered by lookups on the rows the ids name; `"decide"` reads the whole permission and answers no as well | The compiled statement is asked for a recursive or caveated part alone; ids that name no row keep their compiled answer |
| `REBAC_DISPLAY_FIELDS` (`"fallback"`, `True`) | A related row `rebac_select_related` loaded is delivered with its declared display fields where the actor may not read it (`True`: always, without the check) | A model that declares no display fields |
| `REBAC_TRUSTED_IDS` | A stored id of an integer identity is converted by the cast alone | Text and UUID identities |
| `rebac.snapshot.read_snapshot()` | One transaction for a unit of reading in which the library keeps decisions | A transaction that already holds relationship writes |

Each module's docstring is its specification: `rebac.structural`, `rebac.rowscope`, `rebac.walk`,
`rebac.display`, `rebac.snapshot`.

## What a consumer gives up

- **Structural prefetch, row scope, the walk:** nothing in results. They are tested to return what the compiled
  statement returns (below). The row scope issues no profile or witness statement.
- **Row scope needs PostgreSQL's JIT off.** A statement of correlated subqueries is estimated expensive and the
  JIT compiles each one: a 40-subquery scope over three rows ran 6 s with `jit = on`, 2 ms with it off. The
  library sets no database option.
- **Row scope asks each row the caller's conditions select.** Where those select many rows and the actor holds
  few, the id set would have named the few. Measured on 50,000 threads for an actor with three grants at the end
  of the id range, the first page in id order cost 19.5 ms against 33 ms compiled, so this fixture does not show
  the loss; a table a hundred times larger would. `"named"` confines it to queries whose own conditions bind
  their primary key or a foreign key to literal values.
- **Display fields `True`:** a related row of a model that declares display fields is never delivered whole
  through `rebac_select_related`, even to an actor who may read it. Read the row itself for more.
- **Trusted ids:** a relationship row whose id is not the canonical text of its integer key makes PostgreSQL
  raise on the statement and SQLite read it by its numeric value. With the guard such a row is passed over.

## Measured

Scale fixture, flat groups and roles, PostgreSQL 16, JIT off, median of 5. Statements / SQL / time. A range
is two runs.

| Read by the actor with 25,000 grants | Registry layout | Denormalized layout |
|---|---|---|
| 100 threads with messages and parts, today | 6 / 20 KB / 234 ms | 6 / 18 KB / 172 ms |
| … trusted ids | 6 / 14 KB / 157 ms | 6 / 11 KB / 105 ms |
| … structural prefetch | 6 / 8.5 KB / 64 ms | 6 / 6.9 KB / 29 ms |
| … row scope | 3 / 14.5 KB / 37 ms | 3 / 10.5 KB / 24 ms |
| … structural prefetch and row scope | 3 / 3.9 KB / 23 ms | 3 / 3.1 KB / 13.5 ms |
| 200 messages joined to their thread, today | 5 / 15 KB / 336–358 ms | 5 / 13 KB / 91 ms |
| … the walk decides | 7 / 15 KB / 90 ms | 7 / 12 KB / 67 ms |
| … row scope and the walk decides | 4 / 9.4 KB / 24–25 ms | 4 / 7.4 KB / 10 ms |
| … row scope and display fields | 1 / 5.6 KB / 8.4–8.6 ms | 1 / 4.0 KB / 7.2 ms |
| Which of 100 part ids, half of them readable, today | 4 / 12 KB / 71–78 ms | 4 / 10 KB / 61 ms |
| … the walk proves | 8 / 16 KB / 86–97 ms | 8 / 13 KB / 64 ms |
| … the walk decides | 8 / 8.3 KB / 20–21 ms | 8 / 7.1 KB / 7.1 ms |

- An actor granted 2,500 threads through a group: 60 ms to 5.6 ms for the first read.
- PostgreSQL 17, registry layout: 23.5 ms and 8.3 ms for the two combined rows. Its baseline for the joined
  read is 275 ms.
- With recursive groups and roles in the policy the row scope keeps the compiled scope and changes nothing;
  structural prefetch (64 ms), trusted ids (42 ms with it) and the deciding walk (74 to 27 ms) still apply.
- django-guardian on the same data: 3 statements, 2.1 KB, 28–30 ms.

## Verification

- For each mechanism, random policies in both storage layouts where the result with the setting on equals the
  result with it off: 80 cases for structural prefetch, 320 for the walk, 200 for the row scope, 40 for trusted
  ids. 120 of the walk's and 120 of the row scope's are policies shaped like an application's, in which every
  part is one the mechanism reads, so they exercise it and not its fallback.
- Planted bugs fail them: an ignored exclusion, a subject set taken as held, an expired row counted, ignored
  backing filters.
- The whole suite with a setting on: no result differs. What fails pins a statement count, a witness entry or
  statement text: 14 tests with the walk deciding, 30 with the row scope.
- Both the walk and the row conditions carry the compiled statement's fence: they answer nothing while the
  published policy revision is not the one they read.

## Known limits

- **Policy width.** A condition on a row cannot be named and reused, so a permission that others refer to is
  written out at each use. Each subquery is planned at every execution (0.3–0.4 ms in the registry layout). The
  row scope stops at 24. `rowscope.explain(model, actor, action)` says whether it applies and what stops it.
- **Field backings the walk reads:** one enforced foreign key between integer keys, with source filters. A path
  through several relations, a reverse or many-to-many relation, a generic foreign key, an attribute backing and
  a non-integer key keep the compiled statement.
- **Recursion** is asked of the compiled statement by the walk and keeps the compiled scope whole for the row
  scope. An actor-side closure (the groups and roles the actor is in, resolved once per statement) is not built.
- **The scale matrix was not run for these settings.** They are off by default and leave the frozen statements
  unchanged; a setting proposed as a default needs the matrix in both layouts first.
- **A foreign key is read from its column** by the walk: its target is taken to be a row, which an enforced key
  guarantees once the transaction commits.
