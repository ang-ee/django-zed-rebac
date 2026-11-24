# 0025: Bounded reads

Status: **proposed, experimental.** Implemented behind `REBAC_BOUNDED_READ`, off by default, for the owner to
accept, narrow or withdraw. With the setting off no statement changes. Three rulings are open (below, For the
owner).

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

With the setting on, a list read whose own conditions select at most 256 rows is decided by those rows:

1. **Candidates.** One statement selects the primary keys the read's conditions select, with no permission, no
   ordering and no slice, `LIMIT 257`. It selects the key column alone.
2. **Decision.** If 256 or fewer come back, they are every row the conditions select. The walk decides exactly
   those keys (`rebac.walk.decided_alone`) by one small indexed lookup per relation. It may ask the compiled
   statement for a part it cannot read (a parent's permission, a recursion, a caveated relation, another
   permission of the question's own type that reaches less than half of what the question reaches), for the ids
   that reach that part, as `REBAC_BOUNDED_WALK = "decide"` does; it never asks it for the
   read's own question, nor for a permission of its type that reaches half of what it reaches (below, The
   guard).
3. **Rows.** The read's own statement runs with the admitted keys in place of its scope (`pk IN (…)`): its
   conditions, joins, projection, ordering and slice as written, so the slice is taken after the decision.

If 257 keys come back, the read sends its compiled statement, unchanged. It never reads a second batch of
candidates, so a read costs at most one statement more than today, of at most 257 keys.

The statements: the key read, the walk's lookups and the compiled parts it asks for, the rows. Their number
depends on the policy and on what the lookups find, not on the number of candidates: at most 256 keys are one
batch of the walk (`_CHUNK` is 500); a compiled part is asked once, in one statement, and only of a type that
stores rows, for at most 500 ids; a relation with more than 2,000 subject rows on the candidates ends the walk
(`_ROWS`), and the read then sends its compiled statement. The walk's lookups each name one relation, and the
rows' statement is the read's own. A compiled part is the library's compiled statement for that part, bounded to
its ids: its size is that part's, as today's statement's is the whole permission's. A read that selects nothing
sends the key read alone. What follows the rows (stamping, structural admission of the rows as parents, prefetches, field
redaction, the `rebac_select_related()` guards) is the same code on the same rows.

`REBAC_BOUNDED_READ` takes `True` or `"identifiers"`; they differ in what the walk may read of a candidate it does
not admit (below, What crosses into the process).

## What a read must be

`rebac.bounded.refusal(queryset, query)` is the rule, read before any row from the decision and the query the
evaluation decided. A read is decided by its candidates where the statement that reads the admitted keys returns
what the compiled statement returns: every row it reads is one row of the model, kept or dropped with its key,
in an order the statement determines. Everything else keeps the compiled statement:

| Refused | Why |
|---|---|
| A bypass, no actor, `REBAC_ENABLED = False` | No scope to replace. |
| `iterator()`, `aiterator()`, `count()`, `exists()`, `aggregate()`, `explain()`, `update()`, `delete()`, an embedding | Not served: only an evaluation that fills the result cache (`_fetch_all`) is. A count over 256 candidates is a count that needs every candidate decided; a write selects its rows by a scope of its own. |
| A scope an earlier evaluation built (an eager `scoped()`, an object counted and then listed) | It reads what it pinned then (its policy; in an evaluator scope, the grant profile and constant-arm witnesses, which only leave rows out). The walk would read them now. |
| A structural delivery | No permission statement to replace. |
| A model whose identity is not its integer primary key | The candidates are keys; the walk reads integer keys that are the identity. |
| A set combination, a sliced `|`/`&` operand | Each operand has a scope of its own. |
| `select_for_update()` | The key read would lock rows the actor may not read. |
| `distinct(*fields)` | Kept as specified; the rows it keeps are chosen after the scope. |
| An aggregate, a grouping, a window | The statement's rows are not rows of the model, or depend on other rows. |
| `extra()`, `RawSQL`, a hand-written template, a random ordering | The columns and rows it reads cannot be read from the query. |
| A `FilteredRelation`, a join that can repeat a row (a reverse foreign key, a many-to-many) | A row read once per related row: more statement rows than model rows, tied in any ordering. A filter across such a relation is refused for that reason. |
| Any query nested in it: `Subquery`, `Exists`, `__in=queryset`, `readable()` of a gated field, `exclude()` across a to-many relation | Another statement inside this one, with a scope of its own. |
| An ordering that does not end ties by the primary key, unless the conditions bind a unique column to one value | Tied rows come in an order neither statement determines, and a slice of them is not determined either. A prefetch's child read is ordered by the child model's default ordering alone: without the key in it, several children keep their statement. |
| A third-party iterable class | It may read the statement in its own way. |
| A scope built under another policy than the one in force; a policy with a stored override of any kind; a policy that reads a name two ways | The walk reads the policy in force as text; an override makes what is evaluated depend on more than the text. |
| An actor that is a subject set, anonymous or without an id | The walk does not read them. |
| A question the walk would hand to the compiled statement (below, The guard) | That statement costs what the read's own costs: the walk would only add to it. |
| More than 256 candidates | Found by the key read: the compiled statement follows it. |
| An actor whose compiled scope admits nothing (`compile.read.scope_admits_nothing`) | Inside an evaluator scope the grant profile folds the statement to nothing; it costs less than a key read. |

Of the 26 public read entry points the exploration counted, the mechanism serves iteration, `len()` and `bool()`;
slicing and indexing; `get()`; `first()`, `last()`, `earliest()` and `latest()`; `in_bulk()` (one key, or
ordered reads); `values()` and `values_list()`; and a prefetch's child read (ordered by the key). It does not
serve `iterator()` and `aiterator()`: they stream a read the bound would rarely admit. Every other entry point
keeps its statement.

## The guard

The walk asks the compiled statement for what it cannot read: a recursive component or a relation that admits a
caveated subject, whole; a relation or a permission whose union holds an arm it cannot read (a backing it does not
read, a wildcard or fixed subject it cannot follow), for the ids those arms leave; and below an exclusion, where
nothing may be asked, the nearest permission above it, whole. Asked for a part on another type (a thread's
`read_granted` for the threads of 50 messages) that statement is small. Asked for the read's own question, it is
the statement the read would have sent, after the walk: the exploration's `tasks` read lost that way (348 ms
today, 469 ms decided by candidates): parts its read reaches below its own union, which the walk cannot read,
are handed over with the question.

`rebac.walk.compiler_part(policy, resource_type, action)` reads the permission as the walk reads it, from the
policy alone, and names why the walk would hand over the question itself, or ask for a part on a type that stores
no rows (asked in batches of point checks, whose number grows with the ids). The read then keeps its compiled
statement without reading a key. Another permission of the question's own type, compiled for the candidates,
costs what its reach costs: a file's `read_granted` reaches 153 of the 158 relations its `read` reaches and is the
same statement in all but name, a message's `read_granted` reaches 37 of 125. Whether the walk must compile it
depends on whether the readable arms settle every candidate, which only the rows say. Where it would compile one
that reaches half of what the question reaches or more (`walk._reach`, the relations a key reaches), the walk
ends (`_Walk._compiled` refuses it) and the read sends its compiled statement, having spent the key read and the
lookups so far; where the readable arms settle every candidate, the read is decided. The half is a cost estimate,
not a proof: a part that reaches less can still cost more, if its relations are the large ones. The answer of `compiler_part` is kept on the policy
the backend evaluates (`structural.current_policy()`, the object whose key `compile.read.policy_key` gives), in
`_Policy.walked`, so a policy change starts afresh and nothing else is cached. On the benchmark's application policy it refuses
`projects/task` (an arm of its read reaches `proposals/round#retired`, a path through several relations) and
`storage/folder` (recursive), and lets messages, threads, files, parties and changesets through.

The guard is static, and conservative: it refuses a question the walk could have answered when readable arms
settle every candidate. What it cannot see is found while walking, and ends the walk: a permission of the
question's type that reaches half of what it reaches, a relation with more than 2,000 subject rows on the
candidates, a part on more than
500 ids, a candidate that is not a canonical id. The read then sends its compiled statement. A test holds that
over the random policies, wherever `compiler_part` finds nothing, `decided_alone` answers with what `held`
answers, or answers nothing only where it would compile such a permission of the question's type.

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

- **`True`: keys only.** Of a row the actor may not read, at most its primary key enters the process: among the
  257 the key read returns, and as parameters of the walk's lookups. No other column of such a row is selected.
  The walk decides only a permission it reads from the candidates' keys alone: grants that name the actor or
  everyone, constant (`rebac:const`) arms and their filters, `authenticated`, unions, intersections and
  exclusions of these. Where an arm follows a foreign key of the candidate (`thread->read`) or reads a grant to a
  subject set or a parent on it (`reader: auth/group#member`), the walk does not read it, since it would select
  that foreign key, or the subject of that grant, of every candidate. The permission holding such an arm is of the
  question's type, so the walk ends there and the read keeps its statement, unless the arms it can read settle
  every candidate first; where that permission is the question itself, the guard refuses before any key is read
  (`compiler_part(..., keys_only=True)` names it). The walk refuses the lookup again when it would run.
- **`"identifiers"`: keys and identifiers.** The walk also reads, of each candidate, the foreign keys the
  permission follows (a message's thread) and the grants on it that name a subject set or a parent (a thread's
  reader groups), and follows them to the rows they name. Those are keys of other rows and subject identifiers,
  never content, but they are data of rows the actor may not read: which thread a hidden message is in, which
  groups may read a hidden thread.

Where they then appear: as parameters of the walk's lookups, so in the database's statement and slow-query logs
and `auto_explain` (PostgreSQL's `pg_stat_statements` normalizes them), Django's `django.db.backends` debug log
and `connection.queries` under `DEBUG`, and SQL breadcrumbs of an error reporter; and in Python frame locals when
a lookup raises (a timeout, a database error), which error reports capture. The rows' statement carries the
admitted keys only.

## Timing

How long a read takes says whether more than 256 rows, hidden ones included, match its conditions: under 256 the
read costs the key read and the walk, over 256 the key read and the compiled statement (76 ms against 1,427 ms
for the folder read of the heavy reader at the medium size, Measured). With a filter the reader chooses (a search), that is a
threshold oracle over hidden rows. The walk's own lookups vary with the hidden candidates' grants and parents too.
Today's statement varies with hidden rows only by the work they cost inside one statement.

## Several statements, several states

A compiled answer is one statement and one state of the data. A bounded read is three steps: the key read, the
walk's lookups, the rows. Under READ COMMITTED each reads what was committed when it runs, so a result can be
assembled from adjacent states: a row inserted after the key read is not among the candidates; a grant revoked
between two lookups can admit a row that held in neither state; a row whose conditions change between the key
read and the rows' statement is read by its new values. Inside one snapshot (a transaction on a connection
configured for repeatable read, `rebac.snapshot.read_snapshot()`, any transaction on SQLite) every statement reads
the same data, but each at its own instant: a deadline (a grant's expiry, a condition that reads the clock) can
fall between them. This is the walk's caveat and structural prefetch's, and the reason the setting is off by
default.

Inside an evaluator scope the compiled statement reads the actor's grant profile and constant-arm witnesses as of
the scope's first read, and a grant written by another process later is read only once the scope ends (proposal
0019). The walk reads grants as they are when it runs. A bounded read can therefore admit, inside one evaluator
scope, a row whose grant another process wrote after the scope's first read, which the compiled statement leaves
out until the scope ends. It never admits a row the actor did not hold when the walk read it.

## Verification

- `tests/test_bounded_read.py`, in tier 1 and in tier 2 under both bindings:
  - the differential: random policies of four generators (the inherited-witness and subtracting generators,
    the application-shaped policies and a generator whose threads are read by key alone), both storage layouts,
    three actors, every model, 16–20 reads each (lists, slices before and after the first row, indexing,
    unordered reads, `get()`, `first()`, `last()`, `in_bulk()`, `len()`, `bool()`, `values()`,
    `values_list()`, `count()`, `exists()`, joined and guarded joins, nested prefetches), each in both modes,
    inside and outside an evaluator scope: the same outcome, rows and order, or the same exception;
  - every refusal in the table above sends the compiled statement byte for byte (a bypass audit row's
    timestamp aside);
  - the bound: 256 candidates decided, 257 sends the key read and then the compiled statement byte for byte;
  - the statement count of a decided read, the same for 1 candidate and for 200;
  - each dimension of the decision; keys mode reads nothing of a candidate but its key, and refuses a read
    through a foreign key; the guard's reasons, kept per policy; the guard and the walk agree over the random
    policies; nothing of the mechanism runs while the setting is off; the system check.
- The tier 1 suite run whole with the setting on, in each mode, as a further differential: every failure is an
  assertion on statement counts or statement text (listed in the change's report), none on rows.
- Planted defects (a walk that admits one key too many, a slice taken before the decision, a rule that lets an
  embedded queryset through, a read past the bound decided by its candidates) fail the tests; the change's report
  lists what caught each.
- The policy benchmark's smoke (`make bench-smoke`) runs every scenario under `bounded_keys`, `bounded` and
  `all_bounded` beside `today` and `all`, fails if a result differs, and fails if a bounded mechanism that decided
  nothing by its candidates sent other statements than `today` beside its key reads. With the setting off the
  statements the smoke sends were compared byte for byte with those of the commit beneath this change
  (`--statements`).

## Measured

The benchmark's application policy (`tests/bench`), the medium size (383,000 model rows), registry layout,
PostgreSQL 17, JIT off, client binding; wall ms, median of 3. Measured while other suites loaded the machine (load
average 43 to 56): today's own reads ran 2.7 to 3.7 times slower than in the exploration's quiet runs, and the
same nine statements of one read executed in 2.4 s in one run and 3.3 s in another. Compare within a row.
`bounded` is the setting (`"identifiers"`) alone; `all_bounded` adds it to every other mechanism (`all`), the
exploration's measured form. Results were equal under every mechanism.

| Read | Today | `all` | `bounded` | `all_bounded` | `bounded_keys` | Decided by candidates |
|---|---:|---:|---:|---:|---:|---|
| `folder` heavy (one folder's files) | 1,427 | 1,324 | 76 | 52 | 115 | yes |
| `folder` sparse | 407 | 440 | 383 | 357 | 379 | no: the walk ends at the files' `read_granted` |
| `transcript` heavy (one thread's messages) | 3,809 | 1,888 | 6,096 | 581 | 6,119 | the messages, not their children |
| `transcript` sparse | 376 | 92 | 272 | 91 | 300 | the messages |
| `selected` heavy (one message, everything shown) | 4,777 | 2,260 | 5,080 | 1,337 | 5,713 | the message |
| `selected` sparse | 880 | 530 | 628 | 227 | 672 | the message |
| `tasks` heavy | 1,042 | 908 | 1,315 | 843 | 1,350 | no: refused by the guard, same statements as today |
| `tasks` sparse | 107 | 93 | 118 | 96 | 142 | no (guard) |
| `inbox`, `files`, `people`, `changes`, heavy and sparse | | | | | | no: more than 256 candidates, one key read added |

Statements and planning plus execution (ms), from the same runs: `folder` heavy, 5 statements and 1,394 ms today,
12 and 25 ms bounded; `transcript` heavy, 9 and 3,732 today, 24 and 5,561 bounded, 24 and 636 with every
mechanism; `selected` heavy, 14 and 4,361 today, 28 and 5,613 bounded, 28 and 1,158 with every mechanism. On
its own the setting loses on the heavy reader's `transcript` and `selected`: the walk asks the compiled
statement for the thread's `read_granted` and `chatter_read` (45 and 100 of the 118 relations a thread's read
reaches), and a compiled statement bounded to one thread builds its id sets whole, as the read's own does; the
children keep their own statements, which Django's unordered prefetch does not let the setting replace. With the
other mechanisms (trusted ids, structural prefetch) the same parts are cheap and the children are delivered.
At the small size, same load: `folder` heavy 228 against 62 ms; `transcript` and `selected` within noise of
today alone and of `all` combined, where planning dominates (full tables in the change's report).

## Known limits

- **`True` decides little of the benchmark's application policy.** A message's, a part's, a participant's and a
  reaction's read each follow a foreign key in their own union, and keep their statement at no cost (the guard
  refuses before any key is read). A thread's, a file's and a party's follow theirs inside another permission of
  their own type, which the walk cannot read without that foreign key: it ends there, after the key read and the
  lookups so far, unless the arms it can read settle every candidate.
- **Alone, it can be slower than today.** Where the walk asks the compiled statement for a large part on another
  type (a thread's permissions under one thread's messages), that statement builds its id sets whole, as the
  read's own does, and two such parts can cost more than the read's own statement (Measured: `transcript` and
  `selected` for the heavy reader). The cost rule reads only parts of the question's own type.
- **Tied orderings.** A list ordered by a column that is not unique and not followed by the primary key keeps
  its compiled statement unless its conditions name one row: `order_by("-created_at")` should be
  `order_by("-created_at", "-pk")`. A prefetch's child read is ordered by the child model's default ordering
  alone, so a child read of several rows keeps its statement unless that ordering, or its `Prefetch` queryset's,
  ends in the key.
- **Volatile values.** A condition or an ordering that reads the clock is read at the key read and again at the
  rows' statement (the consistency contract above); a random value (`Random()`, `order_by("?")`) refuses the read,
  but a hand-written function that returns a new value per row (`Func(function="random")`) is not recognised.
- **`REBAC_TRUSTED_IDS`.** With it on, a relationship row stored under an id that is not its key's canonical text
  is read by its number on SQLite and makes PostgreSQL raise; the walk passes over it, as the compiled statement
  does with the setting off.
- **Joins that repeat a row** keep the compiled statement: a filter across a reverse foreign key or a
  many-to-many relation.
- **Profile staleness.** Above: inside an evaluator scope a bounded read can be fresher than the compiled
  statement.
- **Statement shapes under server-side binding.** The rows' statement names as many parameters as admitted keys,
  so a prepared-statement cache holds one shape per count, up to 256 per read; Django removes repeated values
  from `IN`, so they cannot be padded to fewer shapes.
- `explain()` shows the compiled statement, not the bounded read.

## For the owner

1. **What may cross.** The brief this was built to says that of a row the actor may not read, nothing but its
   key may enter the process. The walk cannot decide a permission that follows a foreign key, or reads a grant
   to a group, without reading that foreign key or that grant's subject for every candidate. `True` keeps the
   rule and, on the benchmark's policy, decides little (Measured); `"identifiers"` is what the exploration
   measured, and lets keys of other rows and subject identifiers cross. Choose one, or both as built.
2. **Tie order.** An unsliced read whose ordering leaves ties returns the same rows either way, in an order
   neither statement determines. The rule refuses it to keep the order identical; accepting it would serve
   unordered prefetches and lists ordered by a timestamp.
3. **The guard's reading.** Read strictly ("where the walk would hand anything to the compiled statement"), the
   guard refuses every scenario model of the benchmark's policy but changesets: a thread's `channel` names a
   channel by a key that is not an integer, a file's roles recurse. As built it refuses where the question itself
   would be compiled (the `tasks` loss) or a part would be asked of a type without rows, ends the walk where it
   would compile a permission of the question's type that reaches half of what the question reaches (a cost
   estimate, which the owner may prefer stricter or looser), and lets it ask the compiled statement for other
   parts, as the exploration's measured form did. The static half cannot see whether readable arms settle
   every candidate; a static rule that refused every possible same-type part would refuse the folder read, the
   largest gain measured.
