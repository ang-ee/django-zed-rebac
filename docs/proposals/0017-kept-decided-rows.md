# Proposal 0017: decided rows kept for a unit of work

**Status:** draft for the owner's decision. Requested by a consumer whose list
pages decide the same rows again before every statement over one type.
Changes one deliberate rule of `docs/ARCHITECTURE.md` § Decided rows ("Nothing
is kept between operations").

## Problem

A scope reaches other tables through arrows. A file is readable through its
drive, its folder chain, and through each record it is attached to: a message
part through its message, thread, channel and integration, a task through its
project, a proposal through its round. Before the statement that lists files
is compiled, the scope decides the small sets behind those arrows (§ Decided
rows): one statement per arrow target, each selecting the target rows on
which the actor holds the permission, bound into the file statement as
`column IN (keys)`. The decision belongs to one operation and is dropped with
it.

A page that runs several statements over the same type pays the decisions
again for each. Measured on a consumer's messages page (four senders, one
GraphQL query; tests settings with its composed policy):

| | Queries |
|---|---|
| The page without the avatar fields | 55 |
| The page with them: four file statements, each preceded by its decisions | 147 |
| Decisions before each file statement | 22 |
| The same 22 statements, same SQL text, four times | 88 |

Opening an evaluator scope (`ActorMiddleware`, or `evaluator_scope()`) does
not change the count: the scope already keeps the actor's stored-set
memberships (one statement on this page), but not decided rows.

The rule exists for a reason the spec gives: the sets depend on model rows,
which change without the library seeing it. This proposal keeps them anyway,
within one evaluator scope, and states what that costs.

## Rule

Inside an evaluator scope, the rows decided for a node (an arrow target's
permission, a hierarchy) are kept and reused by every later operation of the
same actor, context and policy, until the scope ends or the process sees a
write that can change them. A kept decision stays a lower bound: every
statement that binds it re-reads it in its own snapshot, as today, so a kept
decision can hide a row and never grants one. Before a statement reuses kept
decisions, one statement confirms that they still hold; a decision that does
not is dropped and decided afresh. Outside an evaluator scope nothing
changes.

## Design

### 1. What is kept, and under which key

The decider of an operation (`_Rows` in `rebac/compile/read.py`) keeps, per
node key, what it found: the identities (`_Decision.ids`), or a hierarchy's
levels, or the fact that the node did not fit or holds for every row
(`None`). It does not keep the witness: a witness is compiled with the
parameters of the statement that carries it (§ Decided rows, 4), so it is
compiled again, from the kept rows, by each operation that reuses them.

The key extends the one the scope already keeps actor sets under
(`_Operation.sets_kept`):

- the operation's shape: the policy key (schema revision, overrides), the
  actor's shape, the database alias and vendor, the depth limit, the
  relationship model;
- the actor and the caveat context;
- the node key;
- the relationship generation (bumped by every tuple write in the process);
- the row generation (§ 3).

A context that cannot be keyed (a nested value) keeps nothing, as for actor
sets. Kept decisions share the evaluator's bound for actor sets
(`REBAC_EVALUATOR_CACHE_SIZE`), least recently used dropped first.

### 2. Reuse

When the decider is asked for a node it has not decided in this operation,
it looks for a kept decision under the key. A kept decision is used as if it
had just been decided, with one difference: it is recorded as kept. It
counts against the statement's row budget (5,000 rows over all its sets) like
any decision; one that does not fit what is left stays inline, as today.

### 3. What drops a kept decision

- **A tuple write in the process:** the relationship generation changes.
- **A model write the library owns:** a process-wide row generation, bumped
  wherever a write to a resource model, or to a column or through row the
  policy reads, passes through the library: instance `save()` and
  `delete()` of a rebac model, its queryset `update()`, `delete()`,
  `bulk_create()` and `bulk_update()`, its base manager, many-to-many
  through rows the policy watches, and the collector's cascades. A row the
  actor creates in the request (a message with its parts) is therefore seen
  by the next statement.
- **A schema change:** the policy key changes.
- **The scope ending.**
- **A failed confirmation (§ 4).**

What the library does not see does not drop anything: raw SQL, a write
through a model that is not a rebac resource and that a field backing
traverses, another process. Such a write can add a row the actor may now
read; the kept decision does not have it, so the row is hidden until the
scope ends or an owned write drops the decision. It cannot grant: a row
that left the set fails the witness.

### 4. Confirmation

Today a failed witness makes the statement select nothing, and the next
operation decides afresh. With kept decisions the next operation would bind
the same stale rows and fail again, for the rest of the scope. So an
operation that is about to bind kept decisions first runs one statement:
`SELECT` of the witnesses of all the kept decisions it binds, each as a
boolean. A decision whose witness fails is dropped, decided afresh, and kept
in its place; the others are used. The statement that binds them still
carries their witnesses, as today, so a change between the confirmation and
the statement denies.

On the measured page: the first file statement decides its 22 sets and keeps
them; each of the next three runs one confirmation instead of 22 decisions.
88 queries become 25, and the page 147 becomes about 84.

### 5. Transactions

A decision read inside a transaction can name rows the transaction wrote.
When the transaction (or a savepoint) rolls back, those rows are changed or
gone. A changed row that no longer holds fails the witness, and the
confirmation drops the decision. A gone row grants nothing: a key is bound
only where a stored reference proves its row, and otherwise read through the
target's rows (§ Decided rows, 5). Kept decisions therefore need no
transaction bookkeeping and are kept under `ATOMIC_REQUESTS` too. (The evaluator's check cache keeps
its own rule of declining inside a transaction; it caches answers, which
carry no witness.)

### 6. Who is affected

Only operations inside an evaluator scope: a request under `ActorMiddleware`,
code under `evaluator_scope()`. A Celery task or a management command that
opens one scope for a long unit of work keeps its decisions for that long:
the guidance is one scope per unit of work, as for the check cache. A scope
is per context (`ContextVar`), so it is never shared between requests, tasks
or ASGI calls, and the key holds the actor, so it is never shared between
actors. `sudo()` and `system_context()` compile no scope and are unaffected.

### 7. SpiceDB

None. Decided rows are how `LocalBackend` compiles a scope; a SpiceDB backend
answers `LookupResources` itself. The schema language is unchanged.

## What a consumer changes

Nothing, beyond running requests and tasks inside an evaluator scope. A test
harness that executes a GraphQL schema directly under `actor_context()`
(without the middleware) needs `with evaluator_scope():` to measure what a
request costs.

## Tests

- Query counts: two statements over the same type in one scope decide once
  and confirm once; outside a scope both decide; the measured shape (an
  arrow chain of several levels and a hierarchy) as a library fixture.
- Invalidation: a tuple write, an instance save, a queryset update, a create
  of a row in the chain, a delete and a cascade between two statements each
  make the second statement decide afresh and see the change.
- Confirmation: a row revoked between two statements is not returned, and the
  statements after it in the scope see the correct rows (no stuck empty
  scope); a row revoked between the confirmation and the statement denies.
- Isolation: two actors in one scope, an `actor_context()` switch, nested
  scopes, a `sudo()` statement between two scoped ones, two concurrent
  scopes in threads.
- Equivalence: the differential suites (reference model, frozen walker) run
  with a scope open and kept decisions in use give the same answers as
  without.
- Transactions: a decision taken in a savepoint that rolls back.

## Decisions for the owner

1. **The trade.** Keep decided rows within a scope (this proposal) and accept
   that a write the library does not see can hide a newly readable row until
   the scope ends; or keep the current rule.
2. **Row generation granularity.** One process-wide counter bumped by every
   owned write (this proposal: simple, drops everything on any write), or one
   per resource type in the decision's reach (keeps more across writes to
   unrelated models, more bookkeeping).
3. **Confirmation.** One statement per reusing operation (this proposal), or
   none: a stale decision then empties every later statement that binds it
   until the scope ends.
4. **A switch.** Whether a setting turns keeping off (for example
   `REBAC_EVALUATOR_KEEP_ROWS`), or it is always on inside a scope.

## Not in scope

- Deciding the arrow targets of one statement together, a level of the
  dependency chain per round trip instead of a statement per target. It
  reduces the first statement's decisions (22 to about 6 on the measured
  page) and is independent of this proposal; it needs per-target limits
  inside one compound statement, which SQLite restricts.
- Keeping anything across scopes or requests.
- The evaluator's check and `accessible()` caches.

After approval: ARCHITECTURE § Decided rows and § `PermissionEvaluator`,
CHANGELOG.
