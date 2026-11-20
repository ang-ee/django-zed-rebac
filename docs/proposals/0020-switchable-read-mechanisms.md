# 0020: Switchable read mechanisms

Status: **accepted by the owner on 2026-11-19 for 0.36.0, as experimental settings.** Every mechanism below is a
setting that is off by default, so that a consumer
can turn each on, compare cost and results with `rebac.testing.compare_mechanisms`, and decide. Nothing here
changes a statement while its setting is off. Each may change or be withdrawn in a minor release: they are in the
release to be measured, and the known limits below are part of what is accepted.

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
| `REBAC_STRUCTURAL_PREFETCH` | Children of parents a prefetch just admitted are read by foreign key, where the policy proves the parent's action grants theirs (an action that implies the arm's: [proposal 0022](./0022-structural-delivery-under-an-implied-action.md)) | An intersection or exclusion above the arm, a filtered or generic backing, an override |
| `REBAC_ROW_SCOPE` (`True`, `"named"`) | The scope of a statement a queryset runs itself is written as conditions on each row (`EXISTS` a grant that reaches it) | A recursion, a caveated or expiring relation, a backing the walk does not read, more than 24 subqueries, an embedded, combined, counted or aggregated queryset |
| `REBAC_BOUNDED_WALK` (`"prove"`, `"decide"`) | `held` and `held_many` are answered by lookups on the rows the ids name; `"decide"` reads the whole permission and answers no as well | The compiled statement is asked for a recursive or caveated part alone; ids that name no row keep their compiled answer |
| `REBAC_DISPLAY_FIELDS` (`"fallback"`, `True`) | A related row `rebac_select_related` loaded is delivered with its declared display fields where the actor may not read it (`True`: always, without the check) | A model that declares no display fields |
| `REBAC_TRUSTED_IDS` | A stored id of an integer identity is converted by the cast alone | Text and UUID identities |
| `rebac.snapshot.read_snapshot()` | One transaction for a unit of reading in which the library keeps decisions | A transaction that already holds relationship writes |

Each module's docstring is its specification: `rebac.structural`, `rebac.rowscope`, `rebac.walk`,
`rebac.display`, `rebac.snapshot`. Proposal 0024 extends `REBAC_DISPLAY_FIELDS` to a foreign-key prefetch
whose queryset asks for display rows (`for_display()`).

## What a consumer gives up

- **Structural prefetch, row scope, the walk:** nothing in results. They are tested to return what the compiled
  statement returns (below). The row scope issues no profile or witness statement.
- **Row scope needs PostgreSQL's JIT off.** A statement of correlated subqueries is estimated expensive and the
  JIT compiles each one: a 40-subquery scope over three rows ran 6 s with `jit = on`, 2 ms with it off. The
  library sets no database option.
- **Row scope asks each row the caller's conditions select.** Where those select many rows and the actor holds
  few, the id set would have named the few. On 50,000 threads, for an actor with three grants at the end of the
  id range, the first page in id order cost 19.5 ms against 33 ms compiled: no loss at that size. On 3.2 million
  rows of which the actor holds none, a page cost 152 ms against 17 ms compiled (the benchmark below). `"named"`
  confines it to queries whose own conditions bind their primary key or a foreign key to literal values.
- **Display fields `True`:** a related row of a model that declares display fields is never delivered whole
  through `rebac_select_related`, even to an actor who may read it. Read the row itself for more. Only a
  `RebacMixin` model can declare display fields: its `save` leaves hidden columns alone.
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

## A real policy at scale

The fixture above reaches a handful of relations from each read. `tests/bench` runs a project's real policy over
generated data (`docs/ARCHITECTURE.md § The policy benchmark`); the policy here is a composed application's, 147
definitions over 142 models, in which a message's read reaches 125 relations. Registry layout, PostgreSQL 17, JIT
off. `plain` is `REBAC_ENABLED = False` (proposal 0021): Django's own queries.

**One message with its thread, sender, parts, files and people**

| Model rows | Heavy member, today | every setting on | Administrator | Plain |
|---|---|---|---|---|
| 7,900 | 14 statements, 906 KB, 490 ms (385 ms planning, 596 MiB planner memory) | | 14 KB, 12 ms | |
| 383,000 | 802 KB, 1.9–2.3 s (1.7–2.0 s execution) | 357 KB, 0.7–0.8 s | 14 KB, 21 ms | 10 statements, 3 KB, 3.9 ms |
| 15.3 million | 28.8 s | 17.7 s | 29 ms | |

**383,000 rows, heavy member** (statements / SQL / wall, median of 3)

| Read | Today | Every setting on |
|---|---|---|
| A thread's messages with parts and people | 9 / 387 KB / 1,330 ms | 9 / 120 KB / 394 ms |
| First page of threads with messages and parts | 7 / 353 KB / 1,266 ms | 7 / 188 KB / 651 ms |
| Newest 50 files | 5 / 194 KB / 813 ms | 5 / 164 KB / 717 ms |
| Which of 100 files are readable | 5 / 195 KB / 710 ms | 24 / 171 KB / 613 ms |
| Two single checks | 10 / 368 KB / 1,044 ms | 10 / 298 KB / 945 ms |

The denormalized layout is about twice as fast on the same reads (the message: 1,147 ms today, 406 ms with every
setting on, PostgreSQL 16).

**15.3 million rows, 5.6 million relationships** (wall time of one run each; today, then every setting on)

| Read | Light member | Heavy member | Author | Administrator |
|---|---|---|---|---|
| First page of threads | 12.1 s, 6.6 s | 11.7 s, 14.0 s | 2.6 s, 1.5 s | 35 ms |
| One message | 18.5 s, 7.4 s | 28.8 s, 17.7 s | 4.5 s, 2.0 s | 29 ms |
| Newest 50 files | 47.1 s, 46.0 s | 8.3 s, 11.5 s | 37.8 s, 37.1 s | 18 ms |
| Which of 100 files are readable | 45.7 s, 44.9 s | 7.4 s, 21.0 s | 38.2 s, 38.1 s | 22 ms |
| Two single checks | 7.0 s, 6.4 s | 8.0 s, 15.8 s | 2.0 s, 2.1 s | 22 ms |
| Newest 50 changesets | 17 ms, 152 ms | 16 ms, 168 ms | 609 ms, 516 ms | 12 ms |

What this shows:

- **On a policy this wide the cost is the policy.** A read that Django answers in 4 ms costs a member seconds at
  383,000 rows and tens of seconds at 15 million, in every statement shape. The administrator, whose statements
  fold to a constant arm, stays at tens of milliseconds.
- **The settings help most where children are read through their parents** (the message, the transcript: 2.8 to
  3.4 times at 383,000 rows) and little on a single wide statement (the files page).
- **At 15 million rows they are not reliably a gain.** The heavy member's bounded check went from 7.4 s to 21 s
  and its two single checks from 8 s to 16 s. These are single runs, and the cause was not investigated.
- **The member with fewer grants is slower than the one with more** on the files page (47 s against 8 s): the
  plans differ. Not investigated.

## Verification

- For each mechanism, random policies in both storage layouts where the result with the setting on equals the
  result with it off: 80 cases for structural prefetch, 320 for the walk, 200 for the row scope, 40 for trusted
  ids. 120 of the walk's and 120 of the row scope's are policies shaped like an application's, in which every
  part is one the mechanism reads, so they exercise it and not its fallback. The default suite runs all 80 of
  structural prefetch's and the first seeds of the others (68 cases); the rest are marked `slow`.
- Planted bugs fail them: an ignored exclusion, a subject set taken as held, an expired row counted, ignored
  backing filters.
- On that application's policy, every scenario of the benchmark returns the same rows with and without the settings, for
  four of the cast on SQLite (`make bench-smoke`) and for the reads in the tables above on PostgreSQL 16 and 17.
- The whole suite with a setting on: no result differs. What fails pins a statement count, a witness entry or
  statement text: 14 tests with the walk deciding, 30 with the row scope.
- Both the walk and the row conditions carry the compiled statement's fence: they answer nothing while the
  published policy revision is not the one they read.

## Known limits

- **Consistency.** A compiled answer is one statement and reads one state of the data. Structural prefetch and
  the walk are several: each statement reads the state committed when it runs, so under READ COMMITTED a
  conjunction can be assembled from adjacent states (children read under parents admitted a moment earlier; a
  row read under a parent it has since left; an exclusion read after it was lifted and the grant withdrawn).
  Inside one snapshot (a transaction on a connection configured for repeatable read, or any transaction on
  SQLite) the data is the same for every statement; each still reads its own instant, so a deadline (a row's
  expiry, a caveat that reads the time) can fall between two of them. Whether a decision may be assembled from
  adjacent states is the application's choice to make, as it already is for a prefetch; it is the reason these
  settings are off by default. A policy revision published during a walk is handled: the walk reads the fence
  again after an exclusion.
- **Policy width.** A condition on a row cannot be named and reused, so a permission that others refer to is
  written out at each use. Each subquery is planned at every execution (0.3–0.4 ms in the registry layout). The
  row scope stops at 24. `rowscope.explain(model, actor, action)` says whether it applies and what stops it. On
  the benchmark's application policy it applies to 31 of 130 models with a read permission; the other 99 reach a median of 32
  relations and up to 185.
- **Field backings the walk reads:** one enforced foreign key between integer keys, with source filters. A path
  through several relations, a reverse or many-to-many relation, a generic foreign key, an attribute backing and
  a non-integer key keep the compiled statement.
- **Recursion** is asked of the compiled statement by the walk and keeps the compiled scope whole for the row
  scope. An actor-side closure (the groups and roles the actor is in, resolved once per statement) is not built.
- **The scale matrix was not run for these settings.** They are off by default and leave the frozen statements
  unchanged; a setting proposed as a default needs the matrix in both layouts first.
- **A foreign key is read from its column** by the walk: its target is taken to be a row, which an enforced key
  guarantees once the transaction commits.
