# 0025: Bounded reads

Status: **proposed, experimental.** Implemented behind `REBAC_BOUNDED_READ`, off by default, for the owner to
accept, narrow or withdraw. With the setting off no statement changes. Five rulings are open (below, For the
owner), the first of them the eligibility this text describes: conditions on keys, and every key one the reader
already holds.

**What it discloses.** With the setting on, a read is decided by its candidates only where every key its conditions
name is one the reader already holds: a parent the reader may read, or a key that a row the reader read in the same
evaluation refers to. A reader who can observe the statements a read sends, or its latency, then learns, for a
parent they may read, how many of its children they may not read, up to 256; and, from the number and kind of the
walk's lookups, what holds on such a child: which arms of the permission hold on it, as far as the lookups differ
with them (a grant to the reader that an exclusion removes, a grant to a group). Of a row whose key or whose parent
the reader does not already hold they learn nothing, but for this: with `"identifiers"`, the walk follows a hidden
child's foreign keys and grants to groups, and its lookups show what holds on the rows they name. Below, What a
reader learns, with the measurements.

## Problem

A compiled scope builds every id set its permission reaches, whatever the read's own conditions select. On the
benchmark's application policy, the files of one folder cost what a page of every file costs: the statement
computes every file and every task the reader may read, then meets the folder's few rows at the end. Restricting
the compiled statement to the candidate rows (`pk IN (…)`, the inline scope) saves nothing on such a policy:
PostgreSQL still builds every id set it references more than once (the `files` page for the heavy reader at the
medium size: 728 ms bounded to 50 candidates, 709 ms whole).

The walk (`REBAC_BOUNDED_WALK = "decide"`) decides a few rows by one small indexed lookup per relation the
permission reaches, without the id sets. It answers `held()`; nothing has used it for a list read.

## The change

With the setting on, a list read whose conditions are all on keys the reader holds (below, What a read must be)
and select at most 256 rows is decided by those rows:

1. **Parents.** Where its conditions name parents by a foreign key that the running evaluation has not admitted (a
   prefetch admits the parents it has just read), the read first reads those parents as the reader would:
   `Parent.objects.with_actor(actor).filter(pk__in=[…])`, one statement for all the parents of one model, the
   statement that read sends today. If any is not returned, the read sends its compiled statement and reads no key.
2. **Candidates.** One statement selects the primary keys the read's conditions select, without the permission:
   `SELECT <key> FROM <the model's table> WHERE <the read's conditions> LIMIT 257`. No join, ordering, slice or
   projection of the read reaches it, so none of them changes which keys it finds.
3. **Decision.** If 256 or fewer come back, they are every row the conditions select. The walk decides exactly
   those keys (`rebac.walk.decided_alone`) by one small indexed lookup per relation. It may ask the compiled
   statement for a part it cannot read (a parent's permission, a recursion, a caveated relation, another
   permission of the question's own type that reaches less than half of what the question reaches), for the ids
   that reach that part, as `REBAC_BOUNDED_WALK = "decide"` does; it never asks it for the read's own question,
   nor for a permission of its type that reaches half of what it reaches (below, The guard).
4. **Rows.** The read's own statement runs with the admitted keys in place of its scope (`pk IN (…)`): its
   conditions, joins, projection, ordering and slice as written, so the slice is taken after the decision.

If 257 keys come back, the read sends its compiled statement, unchanged. It never reads a second batch of
candidates. What a read that keeps its statement costs depends on where it is refused: one the rule or the guard
refuses, or whose compiled scope admits nothing, sends today's statements and nothing more; one that names a
parent the reader may not read sends the parent's read and then today's statements; one with 257 candidates sends
the parent's read where there is one, the key read and then today's statements; one whose walk begins and then
ends (below, The guard) has spent those and the walk's lookups so far. On the benchmark's policy: `folder`, sparse
reader, medium size, 15 statements against 3 (321 against 340 ms, ranges overlapping); one message with everything
shown, sparse reader, 43 against 10, where several of its reads walk.

The statements: the parents' read where there is one, the key read, the walk's lookups and the compiled parts it
asks for, the rows. Their number depends on the policy and on what the lookups find, not on the number of
candidates: at most 256 keys are one batch of the walk (`_CHUNK` is 500); a compiled part is asked once, in one
statement, and only of a type that stores rows, for at most 500 ids; a relation with more than 2,000 subject rows
on the candidates ends the walk (`_ROWS`), and the read then sends its compiled statement. The walk's lookups each
name one relation, and the rows' statement is the read's own. A compiled part is the library's compiled statement
for that part, bounded to its ids: its size is that part's, as today's statement's is the whole permission's. A
read that selects nothing sends the parents' read and the key read alone. What follows the rows (stamping,
structural admission of the rows as parents, prefetches, field redaction, the `rebac_select_related()` guards) is
the same code on the same rows.

`REBAC_BOUNDED_READ` takes `True` or `"identifiers"`; they differ in what the walk may read of a candidate it does
not admit (below, What crosses into the process).

## What a read must be

`rebac.bounded.refusal(queryset, query)` is the rule, read before any row from the decision and the query the
evaluation decided. A read is decided by its candidates where the statement that reads the admitted keys returns
what the compiled statement returns (every row it reads is one row of the model, kept or dropped with its key, in
an order the statement determines), where its conditions are on keys alone, and where every key they name is
one the reader holds. Everything else keeps the compiled statement, and the library logs why (`rebac.bounded`, at
debug level):

| Refused | Why |
|---|---|
| A key the reader does not hold (`rebac.bounded._unheld`): a primary key that no row the running evaluation read for the same actor, on the same database, under the same policy, refers to by a foreign key (`get(pk=…)`, `filter(pk__in=[…])`, `in_bulk()` at the root of a request); a parent named by a foreign key that the reader may not read, or that does not exist; a read with no condition; an `isnull` condition | The key read runs the read's conditions without the permission. On keys the reader supplies, what follows would say whether hidden rows have them; on a parent the reader may not read, how many hidden children it has. |
| A condition of any other form than one on the model's own primary key or a foreign-key column of its own table (`thread_id`, or `thread` compared with a key), by `exact`, `in` or `isnull`, against literal values, joined to the others by `AND`: a condition on any other column, a range or pattern lookup, an `OR`, a negation (`exclude()`, `~Q`), a transform, an expression, `F()` or `Value()` on either side, a condition through a join, an annotation in a condition, a manager's own filter of another form (`rebac.bounded._conditions`) | The key read runs the read's conditions without the permission, and what follows shows what it found (below, What a reader learns). A reader who chooses a filter on content would learn whether hidden rows match it, how many, and their values prefix by prefix. |
| A bypass, no actor, `REBAC_ENABLED = False` | No scope to replace. |
| `iterator()`, `aiterator()`, `count()`, `exists()`, `aggregate()`, `explain()`, `update()`, `delete()`, an embedding | Not served: only an evaluation that fills the result cache (`_fetch_all`) is. A count over 256 candidates is a count that needs every candidate decided; a write selects its rows by a scope of its own. |
| A scope an earlier evaluation built (an eager `scoped()`, an object counted and then listed) | It reads what it pinned then (its policy; in an evaluator scope, the grant profile and constant-arm witnesses, which only leave rows out). The walk would read them now. |
| A structural delivery | No permission statement to replace. |
| A model whose identity is not its integer primary key | The candidates are keys; the walk reads integer keys that are the identity. |
| A set combination, a sliced `|`/`&` operand | Each operand has a scope of its own. |
| `select_for_update()` | A locking read decides and locks in one statement; the bounded read decides in statements that lock nothing. |
| `distinct(*fields)` | Kept as specified; the rows it keeps are chosen after the scope. |
| An aggregate, a grouping, a window | The statement's rows are not rows of the model, or depend on other rows. |
| `extra()`, `RawSQL`, a hand-written template, a random ordering | The columns and rows it reads cannot be read from the query. |
| A `FilteredRelation`, a join that can repeat a row (a reverse foreign key, a many-to-many) | A row read once per related row: more statement rows than model rows, tied in any ordering. |
| Any query nested in it: `Subquery`, `Exists`, `__in=queryset`, `readable()` of a gated field, `exclude()` across a to-many relation | Another statement inside this one, with a scope of its own. |
| An ordering that does not end ties by the primary key, unless the conditions bind a unique column to one value | Tied rows come in an order neither statement determines, and a slice of them is not determined either. A prefetch's child read is ordered by the child model's default ordering alone: without the key in it, several children keep their statement. |
| A third-party iterable class | It may read the statement in its own way. |
| A scope built under another policy than the one in force; a policy with a stored override of any kind; a policy that reads a name two ways | The walk reads the policy in force as text; an override makes what is evaluated depend on more than the text. |
| An actor that is a subject set, anonymous or without an id | The walk does not read them. |
| A question the walk would hand to the compiled statement (below, The guard) | That statement costs what the read's own costs: the walk would only add to it. |
| More than 256 candidates | Found by the key read: the compiled statement follows it. |
| An actor whose compiled scope admits nothing (`compile.read.scope_admits_nothing`) | Inside an evaluator scope the grant profile folds the statement to nothing; it costs less than a key read. |

The conditions are read from the query's `WHERE` clause, wherever they came from: the caller, a related manager
(`thread.scopemessage_set`), a prefetch's child read (a reverse one, `thread__in=[parents]`, and a forward one,
which Django writes `pk IN ((1,), (2,))` and the rule reads as `pk IN (1, 2)`), a manager's own default filter.

Which keys the reader holds is read from the record the running evaluation keeps (`structural._Delivery`, the
record structural prefetch keeps; it is opened while either setting is on). While a prefetch runs it holds, for the
actor, by database and policy, the rows the evaluation has admitted and, while this setting is on, the keys those
rows hold in their foreign keys. It serves only the thread, the asyncio task and the actor that opened it, and ends
with the prefetch. A reverse prefetch's child read names parents the evaluation has just admitted, under the parent
model's default action: they are held, at no statement. A forward prefetch's read names keys that the rows just
read refer to: they are held. Any other parent named, by a read at the root of a request or a related manager, is
read as the reader would read it, once for all the parents of each model named (step 1 above). A primary key named
otherwise is one the reader supplies, and keeps today's statement. Keeping the record costs no statement: about 0.4
µs per admitted row with the setting on, against 0.3 µs without the foreign keys (1,000 rows with one foreign key,
median of 50; the record is kept only for prefetches). A forward prefetch of several rows is read unordered by
Django, so it keeps its statement for its ties (For the owner, 4); one of one row is decided. An ordering may name
any column, a joined one included: the key read drops it, and the rows' statement orders the admitted rows alone,
as today's statement orders today's rows, so the ordering changes neither which keys are read nor what the walk
reads.

Of the 26 public read entry points the exploration counted, the mechanism serves, over conditions on held keys,
iteration, `len()` and `bool()`; slicing and indexing; `get()` (with no key of the client's); `first()`, `last()`,
`earliest()` and `latest()`; `values()` and `values_list()`; and a prefetch's child read (a reverse one ordered by
the key, a forward one of one row). `in_bulk()` and `get(pk=…)` name keys the client supplies and keep their
statement. It does not serve `iterator()` and `aiterator()`: they stream a read the bound would rarely admit. Every
other entry point keeps its statement.

## What a reader learns

The key read runs the read's conditions without the permission, and what follows depends on what it finds: no
further statement where it finds no key; the walk's lookups, and the rows' statement if the walk admits a row,
where it finds 1 to 256; today's statement where it finds 257. The rule lets it run only on keys the reader
holds, so what it finds is among the children of parents the reader may read, or among the rows that the
reader's own rows refer to. A reader who can observe the statements a read sends, or its latency, learns:

- **for a parent they may read, how many of its children they may not read**, exactly, up to 256: a read of
  that parent's children and of parents whose children the reader can count (`thread_id__in=[T, P]`) is decided
  by the walk below 257 candidates, and sends today's statement above. With no hidden child the read sends the
  parent's read and the key read; with one or three, six statements; with 300, the parent's read, the key read
  and today's statement;
- **what holds on such a child**, from the number and kind of the walk's lookups: an arm of a union is read only
  while a candidate is left undecided, and the right side of an exclusion or an intersection only for the
  candidates its left side holds. Of one hidden child in a thread the reader may read: with `True`, a child
  granted to the reader and muted costs a lookup where a child with no grant, or one granted to a group, costs a
  compiled part, six statements each; with `"identifiers"`, a child granted to a group costs seven statements,
  one with no grant, or granted and muted, six. Whether such a child meets a filtered constant's filters is read
  the same way;
- with `"identifiers"`, the walk also follows a hidden child's foreign keys and grants to groups, and its lookups
  show what holds on the rows they name (a row another of its foreign keys names, the group a grant on it names),
  rows the reader need not hold.

Of a row whose key or whose parent the reader does not already hold, nothing else. A key the reader names itself
keeps today's statement, whether a row has it or not: a hidden key and an absent one send the same statement. A
parent the reader may not read, or that does not exist, costs the parent's read and then today's statement,
whether it has no children, three or 300: the same statements, byte for byte. The parent's read is the reader's own
read of the parents named, the statement that read sends today: its statements and its outcome depend only on the
parents the reader named and whether they may read them. A parent the reader may not read and one that does not
exist send the same statements. Against today, what differs is this: of a parent the reader may read whose
children are all hidden, the key read follows the parent's read, where an unreadable parent's read is followed by
today's statement; that is whether the reader may read the parent, which the reader learns today by reading it.

`tests/test_bounded_read.py` pins these statement counts
(`test_a_read_by_keys_the_reader_does_not_hold_says_nothing_of_hidden_rows`,
`test_the_children_of_a_parent_the_reader_may_not_read_send_the_same_statements`,
`test_the_hidden_children_of_a_readable_parent_show_in_the_statements`,
`test_the_bound_tells_how_many_hidden_children_a_readable_parent_has`,
`test_the_lookups_follow_what_holds_on_a_hidden_child_of_a_readable_parent`), so a change that widens or narrows
what a reader learns is seen. The latency of these reads under the held-key rule is not yet measured; the
measurements below are of the earlier forms.

The rule was narrowed twice before it took this form. With any condition, a reader who controls a filter on a
content column (any API with field filters) learned whether a hidden row matches it and, padding the filter with
rows they can read and bisecting, exactly how many, and so hidden values prefix by prefix
(`filter(label__startswith="secret-a")`: 5 statements against 1; on PostgreSQL 8.2 against 2.2 ms, one request
classified correctly 94 to 96 percent of the time; today 3.9 against 3.4 ms, 62 percent). With conditions on keys
but any keys, a reader learned whether a row with a given primary key exists (8.8 against 3.0 ms, 95 percent; today
3.8 against 3.1 ms, 71 percent), how many hidden rows carry a given foreign-key value, a parent they may not read
included (9.0 against 3.0 ms, 93 percent; today 48.9 against 49.6 ms, 49 percent), and which parent a hidden row
has.

## The guard

The walk asks the compiled statement for what it cannot read: a recursive component or a relation that admits a
caveated subject, whole; a relation or a permission whose union holds an arm it cannot read (a backing it does not
read, a wildcard or fixed subject it cannot follow), for the ids those arms leave; and below an exclusion, where
nothing may be asked, the nearest permission above it, whole. Asked for a part on another type (a thread's
`read_granted` for the threads of 50 messages) that statement is bounded to the ids that reach it. Asked for the
read's own question, it is the statement the read would have sent, after the walk: the exploration's `tasks` read
lost that way (348 ms today, 469 ms decided by candidates): parts its read reaches below its own union, which the
walk cannot read, are handed over with the question.

`rebac.walk.compiler_part(policy, resource_type, action)` reads the permission as the walk reads it, from the
policy alone, and names why the walk would hand over the question itself, or ask for a part on a type that stores
no rows (asked in batches of point checks, whose number grows with the ids). The read then keeps its compiled
statement without reading a key. Another permission of the question's own type, compiled for the candidates,
costs what its reach costs: a file's `read_granted` reaches 153 of the 158 relations its `read` reaches and is the
same statement in all but name, a message's `read_granted` reaches 37 of 125. Whether the walk must compile it
depends on whether the readable arms settle every candidate, which only the rows say. Where it would compile one
that reaches half of what the question reaches or more (`walk._reach`, the relations a key reaches), the walk
ends (`_Walk._compiled` refuses it) and the read sends its compiled statement, having spent the key read and the
lookups so far; where the readable arms settle every candidate, the read is decided. The answer of
`compiler_part` is kept on the policy the backend evaluates (`structural.current_policy()`, the object whose key
`compile.read.policy_key` gives), in `_Policy.walked`, so a policy change starts afresh and nothing else is cached.
On the benchmark's application policy it refuses `projects/task` (an arm of its read reaches
`proposals/round#retired`, a path through several relations) and `storage/folder` (recursive), and lets messages,
threads, files, parties and changesets through.

The half is a heuristic, and reach does not predict cost. A message's `withheld_keeper` and `read_granted`, 28 and
37 of the 125 relations a message's read reaches, are the parts the walk asks of the compiled statement for one
thread's messages; they executed in 25 and 124 ms where the statement the bounded read replaced executed in
1,237 ms (the review's `EXPLAIN ANALYZE`, heavy reader, medium size). A thread's `read_granted`, 45 of the 118
relations a thread's read reaches, below the half and so asked, took 762 and 812 ms on 47 candidates in two runs
where the whole of today's `inbox` read for that reader took 360 ms (small size, median). What a part the rule
refuses would have cost is not measured.

A read the walk ends this way costs more than today: the parents' read, the key read and the lookups so far, then
today's statements. On the benchmark's policy at the medium size these still walk and then keep their statement:
one folder's files for the sparse reader (it ends at a file's `read_granted`, 153 of the 158 relations a file's
read reaches; 15 statements against 3), and, of one message with everything shown, the sender's handle (a handle's
`read_granted`, 20 of 20) and for the sparse reader the file of its part (a file's `read_granted`, after asking for
a drive's and a folder's read; the whole read 43 statements against 10). A page of rows no longer walks: it names
no key, and keeps its statement before any.

The guard is static, and conservative: it refuses a question the walk could have answered when readable arms
settle every candidate. What it cannot see is found while walking, and ends the walk: a permission of the
question's type that reaches half of what it reaches, a relation with more than 2,000 subject rows on the
candidates, a part on more than 500 ids, a candidate that is not a canonical id. The read then sends its compiled
statement. A test holds that over the random policies, wherever `compiler_part` finds nothing, `decided_alone`
answers with what `held` answers, or answers nothing only where it would compile such a permission of the
question's type; another, that the walk ends there and the read sends today's statements after the key read and
the lookups so far.

## The decision rule

Since 0.38.0 every statement a queryset sends carries the scope decided for that evaluation, and a queryset keeps
nothing of one evaluation for the next. The bounded read runs inside `_fetch_all`, after the evaluation has
decided (actor, bypass, database) and built its scope, after the bypass audit and the field-read guards, and
reads on the decided database: the key read, the walk's lookups (relationships included) and the rows. The query
with the admitted keys serves that evaluation alone; the queryset keeps the query it decided, with its compiled
scope, so a count, an iterator or the next evaluation decide again. Each dimension of a decision either reads
today's answer through the bounded read or keeps the compiled statement:

| Dimension | Behaviour |
|---|---|
| `sudo()`, `system_context()`, a bypass block | Compiled (unscoped) statement, byte for byte. |
| `REBAC_ENABLED = False` | Plain statement, byte for byte. |
| A pinned actor, an ambient actor, a related manager's actor | Decided by candidates, same rows. |
| Strict mode with no actor | `MissingActorError` before any statement, as before. |
| Non-strict mode with no actor | Unscoped statement, byte for byte. |
| Another database alias | Every statement on that alias; the other's grants admit nothing. |
| An evaluator scope with a grant profile | Decided by candidates, same rows; an actor the profile folds to nothing keeps the compiled statement. |
| Stored overrides | Compiled statement, byte for byte. |
| A caveated relation, a recursion under `REBAC_DEPTH_LIMIT` | As the question itself or below an exclusion: compiled statement, byte for byte. As another part: asked of the compiled statement for that part, with no request context and with the whole depth limit, as the read's own statement reads it; same rows. |
| Both storage layouts | Decided by candidates, same rows. |
| Server-side binding | The key read's limit is literal; the rows' statement binds the keys as parameters. Tier 2 runs the tests under both bindings. |

## What crosses into the process

The compiled statement keeps every row the actor may not read inside the database. The bounded read does not:

- **`True`: keys.** Of a row the actor may not read, its primary key enters the process (among the 257 the key
  read returns, and as a parameter of the walk's lookups), and with it what the walk reads from that key alone:
  the grants on the row that name the actor or everyone (the relationship rows a lookup returns), and whether the
  row meets a filtered constant's filters (the lookup returns the keys of those that do). No other column of such
  a row is selected. The walk decides only a permission it reads from the candidates' keys alone: grants that
  name the actor or everyone, constant (`rebac:const`) arms and their filters, `authenticated`, unions,
  intersections and exclusions of these. Where an arm follows a foreign key of the candidate (`thread->read`) or
  reads a grant to a subject set or a parent on it (`reader: auth/group#member`), the walk does not read it, since
  it would select that foreign key, or the subject of that grant, of every candidate. The permission holding such
  an arm is of the question's type, so the walk ends there and the read keeps its statement, unless the arms it
  can read settle every candidate first, or the arm is a relation the walk can ask the compiled statement for
  inside the database; where that permission is the question itself, the guard refuses before any key is read
  (`compiler_part(..., keys_only=True)` names it). The walk refuses the lookup again when it would run.
- **`"identifiers"`: keys and identifiers.** The walk also reads, of each candidate, the foreign keys the
  permission follows (a message's thread) and the grants on it that name a subject set or a parent (a thread's
  reader groups), and follows them to the rows they name. Those are keys of other rows and subject identifiers,
  never content, but they are data of rows the actor may not read: which thread a hidden message is in, which
  groups may read a hidden thread.

A test captures every statement of the key read and the walk over the random policies, in both modes, and
fails where one selects from a model table any column but the key (`True`) or the key and the foreign keys
(`"identifiers"`); two more fail where keys mode would select a candidate's foreign key or the subject of a grant
on it.

Where they then appear: as parameters of the walk's lookups, so in the database's statement and slow-query logs
and `auto_explain` (PostgreSQL's `pg_stat_statements` normalizes them), Django's `django.db.backends` debug log
and `connection.queries` under `DEBUG`, and SQL breadcrumbs of an error reporter; and in Python frame locals when
a lookup raises (a timeout, a database error), which error reports capture. The rows' statement carries the
admitted keys only.

## Several statements, several states

A compiled answer is one statement and one state of the data. A bounded read is three steps: the key read, the
walk's lookups, the rows. Under READ COMMITTED each reads what was committed when it runs, so a result can be
assembled from adjacent states: a row inserted after the key read is not among the candidates; a grant revoked
between two lookups can admit a row that held in neither state; a row whose conditions change between the key
read and the rows' statement is read by its new values. `rebac.snapshot.read_snapshot()` does not change this: it
is one transaction, and under READ COMMITTED a transaction's statements each read their own state. Every
statement reads the same data only in a transaction that reads one snapshot: on PostgreSQL, one at REPEATABLE
READ or SERIALIZABLE (the connection's `OPTIONS["isolation_level"]`, or the application's own `SET TRANSACTION`
before the transaction's first statement); on SQLite, any transaction. Even there each statement reads at its own
instant: a deadline (a grant's expiry) can fall between them. This is the walk's
caveat and structural prefetch's, and the reason the setting is off by default.

The walk never admits a row the actor did not hold when it read the row's grants. The row's columns, though, are
read by the rows' statement, after the walk: a transaction that removes the actor's grant and rewrites the row,
committed between the walk's lookup and the rows' statement, gives the reader the new content of a row they no
longer hold, a result neither the state before the write nor the state after it gives. The window is the time
from the lookup that admitted the row to the rows' statement: the rest of the walk and the rows' statement, a few
statements of one read (the whole bounded `folder` read of the heavy reader, 12 statements, took 46 to 60 ms at
the medium size). `test_the_rows_statement_reads_what_was_committed_after_the_walk` pins it with a second
connection that commits between the walk and the rows: under READ COMMITTED on PostgreSQL, inside
`read_snapshot()` too, and on SQLite outside a transaction, the read returns the thread with the label written
after the revocation; in a PostgreSQL transaction at REPEATABLE READ it returns the state before the write.

Inside an evaluator scope the compiled statement reads the actor's grant profile and constant-arm witnesses as of
the scope's first read, and a grant written by another process later is read only once the scope ends (proposal
0019). The walk reads grants as they are when it runs. A bounded read can therefore admit, inside one evaluator
scope, a row whose grant another process wrote after the scope's first read, which the compiled statement leaves
out until the scope ends.

## Verification

- `tests/test_bounded_read.py`, in tier 1 and in tier 2 under both bindings:
  - the differential: random policies of four generators (the inherited-witness and subtracting generators, the
    application-shaped policies and a generator whose threads are read by key alone), both storage layouts, three
    actors, every model, 24 to 32 reads each (lists, slices before and after the first row, indexing, unordered
    reads, `get()`, `first()`, `last()`, `in_bulk()`, `len()`, `bool()`, `values()`, `values_list()`, `count()`,
    `exists()`, joined and guarded joins, nested, ordered and forward prefetches, conditions on keys the reader
    holds and on keys it does not, conditions the rule refuses), each in both modes, inside and outside an
    evaluator scope, with structural prefetch off and on: the same outcome, rows and order, or the same exception;
    and every statement of the key read and the walk selects from model tables no column but those the mode lets
    cross;
  - every refusal in the table above sends the compiled statement byte for byte (a bypass audit row's
    timestamp aside), in both modes, and logs its own reason; a read naming a parent the reader may not read
    sends the parents' read, byte for byte the reader's own, and then the compiled statement;
  - the record of held keys serves one evaluation, one actor and one database;
  - the bound: 256 candidates decided, 257 sends the parents' read, the key read and then the compiled
    statement byte for byte;
  - the statement count of a decided read, the same for 1 candidate and for 200; what a reader learns, by the
    statement counts above; the walk that ends at a permission as wide as the question;
  - each dimension of the decision; keys mode reads nothing of a candidate but its key, and refuses a read
    through a foreign key; the guard's reasons, kept per policy; the guard and the walk agree over the random
    policies; a commit between the walk and the rows; nothing of the mechanism runs while the setting is off; the
    system check.
- Planted defects: each check of the rule, the walk's belts, the bound, the slice, the order and the guard,
  dropped or moved one at a time in a copy of the source, fails at least one test of the file, except where
  another check refuses the same reads first (the change's report lists each and what caught it).
- The policy benchmark's smoke (`make bench-smoke`) runs every scenario under `bounded_keys`, `bounded` and
  `all_bounded` beside `today` and `all`, fails if a result differs, and fails if a bounded mechanism that decided
  nothing by its candidates sent other statements than `today` beside its key reads. With the setting off the
  statements the smoke sends were compared byte for byte with those of the commit beneath this change
  (`--statements`).

## Measured

The benchmark's application policy (`tests/bench`), the medium size (383,000 model rows), registry layout,
PostgreSQL 17, JIT off, client binding, the three reads the mechanism exists for. Each cell: wall ms of the read,
median of five interleaved passes and their range in parentheses (each pass reads every scenario and reader under
every mechanism once, after one untimed read, in an order rotated from pass to pass); then the statements the read
sends and, under a bounded mechanism, how many of its list reads were decided by their candidates. The machine was
loaded by other work throughout (load average 23 to 57), and this change's own test runs went on beside the
passes: compare within a row, and only where the ranges do not overlap. Every mechanism read the same rows.

| Read | Reader | `today` | `all` | `bounded` | `all_bounded` | `bounded_keys` |
|---|---|---:|---:|---:|---:|---:|
| `folder` | heavy | 1,726 (1,003–2,818), 5 st | 1,227 (936–2,148), 5 st | 124 (117–208), 13 st, 1 decided | 100 (75–133), 13 st, 1 decided | 203 (159–290), 13 st, 1 decided |
| `folder` | sparse | 340 (250–682), 3 st | 300 (246–705), 3 st | 321 (290–769), 15 st, 0 decided | 429 (267–583), 15 st, 0 decided | 522 (301–732), 11 st, 0 decided |
| `transcript` | heavy | 2,444 (2,267–6,786), 9 st | 684 (665–1,736), 9 st | 2,110 (1,810–4,953), 25 st, 1 decided | 360 (309–693), 25 st, 1 decided | 2,267 (2,068–5,155), 9 st, 0 decided |
| `transcript` | sparse | 323 (276–741), 5 st | 96 (83–215), 5 st | 352 (318–658), 22 st, 1 decided | 110 (99–234), 22 st, 1 decided | 289 (252–1,014), 5 st, 0 decided |
| `selected` | heavy | 4,826 (3,579–9,710), 14 st | 1,468 (1,257–3,802), 14 st | 3,887 (3,352–8,822), 27 st, 1 decided | 1,378 (1,194–2,779), 27 st, 1 decided | 5,399 (3,486–8,216), 26 st, 1 decided |
| `selected` | sparse | 575 (441–1,061), 10 st | 264 (200–611), 10 st | 734 (562–958), 43 st, 1 decided | 351 (278–517), 43 st, 1 decided | 629 (471–974), 33 st, 1 decided |

The parents' read on its own (the reader's read of the one folder or thread named, inside an evaluator scope as the
benchmark reads, median of five): the folder, 107 ms and 5 statements for the heavy reader (its grant profile
read with it), 18 ms and 2 for the sparse; the thread, 136 ms and 47 ms.

What the ranges support: one folder's files for the heavy reader, 124 against 1,726 ms with the setting alone
(ranges 117–208 and 1,003–2,818), 100 against 1,227 with the other mechanisms. Every other pair's ranges overlap.

Which reads are served (the library's debug log): one folder's files for the heavy reader, the folder proven by
the parents' read; for the sparse reader the walk ends at a file's `read_granted` and the read keeps its
statement. One thread's messages, the thread proven by the parents' read. Of one message with everything shown, the
thread, read by a forward prefetch of the key the message refers to; the message itself, read by a key the client
supplies, keeps its statement, and the sender's handle, and for the sparse reader the file of its part, walk and
then keep theirs. The children read by a reverse prefetch keep their statement for their ordering. `bounded_keys`
decides the heavy reader's folder and the thread of the message, nothing else. `tasks` is refused by the guard;
a page of threads (`inbox`) or of parties (`people`) names no key and sends today's statements, with no key read,
at both sizes.

Before the rule required held keys, and before it required keys at all, the same reads were measured at both sizes
(the change's report): the folder read of the heavy reader took 50 ms against 2,044 at the medium size, without
the parents' read; a page at the small size walked and kept its statement, at up to 13 statements against 3.

## Known limits

- **Eligibility is narrow.** At the root of a request, only a read of the children of parents the reader may
  read is served (one folder's files, one thread's messages). A filtered list (a search, a status or date filter,
  a filter through a relation), a page of rows with no condition and a read by keys the client supplies
  (`get(pk=…)`, `in_bulk()`) keep their statement, without a key read. Inside a prefetch, a child read is served
  where its ordering ends ties: a reverse one ordered by the key, a forward one of one row.
- **`True` decides little of the benchmark's application policy.** A message's, a part's, a participant's and a
  reaction's read each follow a foreign key in their own union, and keep their statement at no cost (the guard
  refuses before any key is read). A thread's, a file's and a party's follow theirs inside another permission of
  their own type, which the walk cannot read without that foreign key: it ends there, after the key read and the
  lookups so far, unless the arms it can read settle every candidate.
- **A walk that ends costs more than today.** Above, The guard: the parents' read, the key read and the lookups so
  far come before today's statements. So does a read naming a parent the reader may not read: the parents' read
  comes first.
- **Tied orderings.** A list ordered by a column that is not unique and not followed by the primary key keeps
  its compiled statement unless its conditions name one row: `order_by("-created_at")` should be
  `order_by("-created_at", "-pk")`. A prefetch's child read is ordered by the child model's default ordering
  alone, so a child read of several rows keeps its statement unless that ordering, or its `Prefetch` queryset's,
  ends in the key.
- **Volatile values.** An ordering that reads the clock is read at the rows' statement only; a random ordering
  (`order_by("?")`) refuses the read, but a hand-written function in the ordering that returns a new value per
  row (`Func(function="random")`) is not recognised. A condition can no longer read the clock or draw a random
  value: the rule refuses any condition that is not a key compared with literal values.
- **`REBAC_TRUSTED_IDS`.** With it on, a relationship row stored under an id that is not its key's canonical text
  is read by its number on SQLite and makes PostgreSQL raise; the walk passes over it, as the compiled statement
  does with the setting off.
- **Joins that repeat a row** keep the compiled statement: a projection or an annotation across a reverse foreign
  key or a many-to-many relation.
- **Profile staleness.** Above: inside an evaluator scope a bounded read can be fresher than the compiled
  statement.
- **Statement shapes under server-side binding.** The rows' statement names as many parameters as admitted keys,
  so a prepared-statement cache holds one shape per count, up to 256 per read; Django removes repeated values
  from `IN`, so they cannot be padded to fewer shapes.
- `explain()` shows the compiled statement, not the bounded read.

## For the owner

1. **Eligibility.** As built, a read is eligible only where every condition is on the model's own key or a
   foreign key of its own table, by `exact` or `in` against literal values, joined by `AND`, and every key it
   names is one the reader holds: a parent the reader may read, or a key a row the reader read in the same
   evaluation refers to. The coordinator's ruling on the review, to be confirmed. Wider forms served more reads
   and disclosed more (above, What a reader learns). A read at the root of a request is served only through a
   foreign key to parents the reader may read (one folder's files, one thread's messages); a read by keys the
   client supplies, or by no condition, keeps its statement.
2. **The parents' read.** The rule reads the parents a read names as the reader would read them, by today's
   statement: one statement for all the parents of one model, whose statements and outcome depend only on the
   parents named and whether the reader may read them. The walk would answer the same question for less (a walk
   point check on one folder or thread took 6 to 32 ms against 19 to 182 ms compiled, measured before this form was
   built), but its lookups follow what holds on each parent named, readable or not, so the number of statements
   would say, of a parent the reader may not read, which arms of the permission hold on it, and whether it exists
   (on the test policy, two statements for a thread the member may not read, one for a thread that does not exist).
   The compiled `held()` differs too: it reads a key that names no row by a further point check (one statement for
   the hidden thread, two for the absent one). The reader's own read sends one statement for either. The owner may
   prefer the walk, with that disclosure stated.
3. **What may cross.** The brief this was built to says that of a row the actor may not read, nothing but its
   key may enter the process. The walk cannot decide a permission that follows a foreign key, or reads a grant
   to a group, without reading that foreign key or that grant's subject for every candidate. `True` keeps the
   rule and, on the benchmark's policy, decides little (Measured); `"identifiers"` is what the exploration
   measured, and lets keys of other rows and subject identifiers cross, and, by its lookups, shows what holds on
   the rows they name. Choose one, or both as built.
4. **Tie order.** An unsliced read whose ordering leaves ties returns the same rows either way, in an order
   neither statement determines. The rule refuses it to keep the order identical; accepting it would serve
   unordered prefetches (Django reads every forward prefetch unordered, and a reverse one in the child model's
   default ordering) and lists ordered by a timestamp.
5. **The guard's reading.** Read strictly ("where the walk would hand anything to the compiled statement"), the
   guard refuses every scenario model of the benchmark's policy but changesets: a thread's `channel` names a
   channel by a key that is not an integer, a file's roles recurse. As built it refuses where the question itself
   would be compiled (the `tasks` loss) or a part would be asked of a type without rows, ends the walk where it
   would compile a permission of the question's type that reaches half of what the question reaches (a heuristic,
   above, which the owner may prefer stricter or looser), and lets it ask the compiled statement for other parts,
   as the exploration's measured form did. The static half cannot see whether readable arms settle every
   candidate; a static rule that refused every possible same-type part would refuse the folder read, the largest
   gain measured.
