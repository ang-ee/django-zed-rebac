# 0022: Structural delivery under an implied action

Status: **proposed.** Implemented behind the experimental `REBAC_STRUCTURAL_PREFETCH` of
[proposal 0020](./0020-switchable-read-mechanisms.md), off by default, for the owner to accept or withdraw. With the
setting off no statement changes.

## Problem

Structural prefetch reads the children of parents a prefetch just admitted by foreign key, with no permission
statement, where the policy proves that holding the parent's action grants the children's. Until now the proof
asked for the child's arm to name exactly the action the parent was admitted under: a comment whose `read` has the
arm `document->read` was delivered only below documents admitted under `read`.

An application's lists do not read that way. A project's page lists documents under `browse`, and reads their
comments under `read`, through `document->read`. On such a policy the mechanism never applies, although every
document that grants `browse` grants `read`:

```zed
definition doc/project {
    relation member: auth/user
    relation guest: auth/user
    permission read = member + guest
    permission browse = member
}
definition doc/document {
    relation project: doc/project // rebac:field=project
    relation author: auth/user
    relation hidden: auth/user
    permission read = (project->read + author) - hidden
    permission browse = (project->browse + author) - hidden
}
definition doc/comment {
    relation document: doc/document // rebac:field=document
    permission read = document->read
}
```

## The change

An arm `rel->T` of the child's action is accepted under a parent admitted under `A` when the policy proves that `A`
implies `T` on the parent's type: for every actor and every row of that type, under every state of the data, whoever
is granted `A` is granted `T`. `A == T` is the case that held before. Everything else about a delivery stays as
proposal 0020 specified it: the parents were read in the running prefetch by a queryset scoped for the actor and
`A`, the child query is bound to them by a mandatory `fk IN (…)`, the arm's relation is backed by exactly that
foreign key, unfiltered and to the parent type alone, the rows carry their actor stamp and field gates apply.

`rebac.structural.implies(policy, resource_type, held, granted)` is the proof. It reads the policy the backend
evaluates (the composed schema `compile.read._policy` returns) and is kept per policy key beside the delivery proofs:
a policy change starts a new key, and the kept proofs are dropped by `compile.read.reset()` or once 4,096 are
kept. `rebac.structural.explain(...)` says
why an edge is refused. `rebac.structural.current_policy()` is the policy these functions take (`explain` and
`parent_implies_children` take it when none is passed; `implies` takes a policy as its first argument), and
`rebac.structural.edges()`, for tests and tooling rather than a request path, lists, for it, every edge the mechanism serves with the parent actions
it serves it under (the child model, the foreign key, the parent model, the child's action and those parent
actions), so that a consumer can pin and audit what turning the setting on reads without a statement. The listing
agrees with what `delivers` does on the same policy: on the random policies below, a delivery of a child query bound
to an admitted parent happens exactly under the parent actions the edge lists.

## The rules

`e1 ⇒ e2` reads: every row admitted by the compiled lower bound of `e1` is admitted by that of `e2`, for any actor,
data, caveat context and instant, the same on both sides. The prover is syntactic: it may fail to prove a true
implication, and must not prove a false one. Each rule is sound on its own, so any order of search is sound.

| Rule | Why it holds |
|---|---|
| `e ⇒ e`; `nil ⇒ e` | One expression has one value. `nil` grants nothing. |
| `e ⇒ a + b` if `e ⇒ a` or `e ⇒ b` | A union admits whatever either operand admits. |
| `a + b ⇒ e` if `a ⇒ e` and `b ⇒ e` | A union admits nothing beyond its operands. |
| `a & b ⇒ e` if `a ⇒ e` or `b ⇒ e` | An intersection admits only what each conjunct admits: a conjunct may be dropped on the left. |
| `e ⇒ a & b` if `e ⇒ a` and `e ⇒ b` | A conjunct may be added on the right only when the left implies it as well. |
| `a - d ⇒ b - d'` if `a ⇒ b` and `d' ⇒ d` | A row admitted on the left is in `a`, so in `b`, and not in `d`, so not in `d'`. The subtrahends are compared at the opposite bound, where the compiler reads them (see Recursion). An identical subtrahend is the common case. |
| `a - d ⇒ e` if `a ⇒ e` | An exclusion admits only what its minuend admits. |
| A name means what it means to the compiler | Inside an expression `authenticated` and `anonymous` are the actor's state, whatever the definition declares under those names; any other name, and a name checked or followed to, is its relation, else its permission (its expression), else nothing. The compiler and the prover resolve names through the same two functions, `rebac.schema.ast.is_builtin_reference` and `member`. A relation, a built-in name, an unknown name and an arrow are equal only to themselves. |
| `r->x ⇒ r->y` if `x ⇒ y` on the subject type | When `r` admits exactly one subject type, as plain objects (no subject set, wildcard or fixed id), both sides follow the same edges, with the same caveat and expiry, to the same objects. |

On the policy above, `browse ⇒ read` on a document takes the exclusion with the identical subtrahend `hidden`,
the union on each side, and the arrow `project->browse ⇒ project->read`, lifted to the project, where `browse` is
`member`, an operand of `read`.

## Recursion

A recursive permission is read to `REBAC_DEPTH_LIMIT` hops, counted from where its recursive component was entered
on the way. At the lower bound, less depth admits fewer rows. A subtrahend is read at the opposite bound, where a
recursion cut at the limit counts as holding at its frontier: there, less depth withholds more. Text that appears
on both sides is therefore read alike only where the side that must admit more is not read with less depth:

- **At the lower bound, the granted side never opens a recursive name, nor is granted one as a root.** Each
  component it names is then entered afresh, with the whole depth; the held side has as much or less.
  `below_read = parent->tree_read` is an operand of `tree_read = (parent->tree_read + viewer) + editor` and does
  imply it, but with a depth limit of 2 it reads a folder three hops below the viewer's, which `tree_read` does not.
  For the same reason `tree_inbox = parent->tree_inbox + viewer` is not proven to grant `tree_read`, though it does
  by induction. The other way round (`tree_read` implies a permission that names it through `parent`) is proven.
- **Inside a subtrahend, neither side opens a recursive name.** `x - ((parent->tree_read + viewer) + editor)` is not
  proven to imply `x - tree_read`: the second subtrahend, read deeper inside its own recursion, can hold where the
  first does not. The simple rule refuses a recursion on either side inside a subtrahend. What it gives up is the
  one sound case: in the comparison `d' ⇒ d`, the held permission's subtrahend `d` may be read with less depth,
  which only makes the held permission withhold more, so opening a recursion in `d` would be sound. The
  benchmark's policy needs no such proof.
- **A goal met again while it is being proven is refused.** The search never assumes what it is proving, so every
  conclusion it keeps rests on a finite proof. Assuming the open goal instead is not sound in this search: with
  `p = q + viewer`, `q = p + editor` (an alias cycle), `a = p & q` and `t = editor + owner`, a conclusion drawn
  about `q` while `p ⇒ t` was assumed would be kept after the assumption fails, and `a ⇒ t` proven.

The delivered edge itself can be part of a recursion: a folder's `read` is `parent->read + viewer`, and its children
are folders. The parent was admitted with its permission's component entered at the parent. A child whose action,
or a permission its union opens on the way to the arm, is in that component reads the parent's permission across
the delivered edge, one hop into the component, with one hop less of the limit. Such an edge is refused, whether
the parent was admitted under the arm's action itself or under one that implies it. An arrow from outside the
recursion (a document's `folder->read`) enters it afresh at the parent and is delivered. This was in the released
mechanism; see the changelog.

## Between the parents' statement and the children's

A delivery assembles one decision from two statements, and two things can change between them:

- **The data.** Under READ COMMITTED each statement reads what was committed when it runs. Inside one snapshot
  (repeatable read, or any transaction on SQLite) the data is the same for both.
- **The clock.** Each statement reads its own instant. A row that expires, a caveat that reads the time, or an
  override's deadline can fall between the two statements, inside a snapshot as well.

Proposal 0020 accepts that window for structural prefetch, and this change neither widens nor narrows it. The
prover's own argument is made at one instant and one state, the same for both sides.

The policy does not change between them. The proof is made on the policy the children are decided under, and the
parents' rows prove something only if their statement was built under that policy too. An evaluation reads one
pinned policy throughout, prefetches included, but a queryset scoped eagerly (`scoped()`) keeps the statement it was
built with, and a policy may have changed since. The parents' rows are therefore recorded with the key of the policy
their scope was built under (`compile.read.policy_key`, carried on the scoped query beside its decision), and
children are delivered only under the same key: under another, they keep their statement. Without the key, parents
admitted under `a = direct` and read after the policy became `a = t` delivered children of `message->t` that
neither policy grants.

## What is refused, and why

- **An exclusion added on the right.** `e ⇒ b - d` needs `e` and `d` disjoint, which the text does not say.
- **A subtrahend on the right that withholds more.** `a - d ⇒ b - d'` needs `d' ⇒ d`; differing subtrahends are
  accepted only when that is proven.
- **A conjunct added on the right** that the left does not imply.
- **A relation with several subject types,** and two relations to one type: lifting would need the implication on
  every subject type, and two relations are two sets of edges.
- **Recursion,** as above.
- **Overrides of every kind.** While the policy is composed with any stored override (extend, loosen, disable,
  tighten, recaveat), nothing is proven but `A == A`, and no edge is delivered at all. The reason is not the window
  above: the prover reads the policy's text, and an override makes what is evaluated depend on more than the text.
  An arm or a site added by an override holds until its deadline; a recaveat replaces a caveat's expression until
  its own, and the compiled statement is fenced on that deadline. The prover does not read those tags, so it proves
  nothing while one is stored.

- **A type that reads a name two ways** (`rebac.schema.ast.name_conflicts`). Rule 1 compares text, and a relation
  checked by name was read as a reference to it: a relation named `authenticated` then compared equal to the
  built-in of that name inside an expression, where it is the actor's state, so `read = authenticated` was proven to
  grant the relation and an edge through it was delivered. A name declared as a relation and as a permission is two
  things to the readers that ask for one or the other. Nothing is proven about such a type but `A == A`. New input
  may not hold one; a stored policy from before the rule was held at runtime can (`rebac.W014`).

Caveats, expiry and wildcards need no rule of their own. The prover identifies a relation only with itself, and a
caveat instance's verdict depends on its name, pinned context and the request's context, not on the permission
being read (`compile.conditions`): a conditional or expiring grant implies under its own condition. The delivered
edge itself is a backed relation, which cannot carry a caveat or an expiry; the prover checks it anyway.

## Verification

- One test per rule and one per refusal on small policies (`tests/test_structural_implication.py`): among them a
  permission named `authenticated`, two relations of one type, `nil` on the right, a subtrahend taken for a grant,
  an alias cycle on the held side, a goal shared by two types, a recursion on the granted side and in a subtrahend
  (each with the data it protects against), and an override of each kind with and without a deadline.
- Random policies whose permissions are derived from one another by shapes the prover accepts and shapes it must
  refuse, with grants, subject sets, caveated, expiring and wildcard rows, a permission named `authenticated`, an
  alias cycle, the same permission names on two types, two relations to one type, and permissions beside and in
  the subtrahends of a recursion over a chain one hop longer than the depth limit: for each implication the prover
  claims, the rows each actor is granted under `A` are a subset of those under `T`. 60 seeds in both storage
  layouts, six of them in the default suite and the rest `slow`.
- The structural differential of proposal 0020 reads its threads under each of their actions, so that children are
  delivered below parents admitted under another action than their arm names.
- At the depth limit, in both storage layouts: a tree read under its own recursion, an arrow into a recursion from
  another type, and a recursion across two types through the delivered edge (`tests/test_structural_depth_frontier.py`).
- Planted defects in the prover and the delivery fail the default suite; the report of the change lists each.

## Measured

The benchmark's application policy (`tests/bench`), the small size, registry layout, PostgreSQL 17, JIT off. A
page of 25 messages admitted under the action an inbox lists them under, each with its parts, participants,
reactions and stars, read under `read` (scenario `inbox_messages`); statements / SQL / wall time, median of 5, while
other suites ran on the machine.

| Actor | Today | Structural prefetch |
|---|---|---|
| Heavy member | 8 / 535 KB / 484 ms | 8 / 62 KB / 52 ms |
| Light member | 6 / 304 KB / 223 ms | 6 / 36 KB / 27 ms |
| Author | 7 / 193 KB / 152 ms | 7 / 29 KB / 33 ms |
| Outsider | 6 / 164 KB / 132 ms | 6 / 21 KB / 29 ms |
| Administrator | 7 / 9.2 KB / 18 ms | 7 / 6.5 KB / 14 ms |

Results are equal under both settings. Each child statement went from 115–120 KB and about 100 ms to under 0.5 KB
and 1–2.4 ms; the parent's statement and the profile reads are unchanged. Of the policy's 10,666 pairs of a child
edge and a parent action, 394 are delivered; the edge-by-edge listing for one parent type is
`python -m tests.bench.run edges --parent <label> --action <action>`.
