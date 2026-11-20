# 0023: No inheritance through links

Status: **draft, built at the owner's request on 2026-11-19 so that it can be measured, and revised on 2026-11-20
after an independent review; not accepted.** One setting, `REBAC_INHERIT_THROUGH_LINKS`, `True` by default. The
contract is specified in `docs/ARCHITECTURE.md § Policies without inheritance through links`. (0022 is reserved
for a shelved prototype.)

## Problem

A read permission in a composed application policy inherits other records' reads through links. A thread is
readable because a task, a page or a project that points at it is readable; that task is readable because its
project is; and so on, transitively. On the benchmark's application policy (147 definitions, 808 permissions) a
`read` reaches up to 71 other permissions over 33 types, and the statement that decides whether one message is
readable is 119 KB.

Nothing tells an owner what that inheritance costs, or which access depends on it, short of rewriting the policy
by hand. This proposal is a switch that stops it, so that cost and lost access can be compared on a real
deployment by changing one setting.

## Contract

`True` evaluates the published policy: no statement changes. `False` evaluates a narrower policy derived from it.

**Kinds of relation.** Every relation an arrow walks is classified on the definition that declares it, from its
resolved backing (`rebac.field_backing`), in this order:

- *parent*: backed by a single forward foreign key or one-to-one column of the row itself (the multi-table parent
  link included), with no further hop in its path, and with filters, if any, that name only the row's own
  columns;
- *constant*: backed by `rebac:const`;
- *same type*: its allowed subject types are the declaring type itself, whatever its backing (a folder in a
  folder, a role in a role, a self many-to-many, `parent__parent`);
- *membership*: no allowed subject type has a model that stores rows (groups, roles, anchors declared only in the
  policy);
- *link*: everything else: a path of several hops, a filter over another table, a reverse, many-to-many or generic
  relation to another type, an attribute backing, a stored relation that names records of another type, a
  backing that does not resolve.

**The rule.** In a granting position an arrow `link->P` is evaluated as `link->lent(P)`, what the linked record
grants directly: `P` with the record's own named permissions followed to what they lend, and every arrow removed
except those through a constant relation or a membership (kept as written, so their target is evaluated as the
narrowed policy evaluates it) and through the multi-table parent link (followed to what the same record lends in
its parent table). Arrows of a record's own permissions through a parent, a constant, a same-type relation or a
membership are kept.

**Granting positions only.** A position is granting when it is under an even number of right-hand sides of a
subtraction. Under an odd number nothing is narrowed and nothing is removed, in a record's own permissions and in
what it lends alike.

**The invariant.** For every actor, action and object, what the narrowed policy grants is a subset of what the
published policy grants.

**Generated names are not names.** A version of `P` that differs from `P` is a permission of its own in the
narrowed policy: `lent(P)`, `excluded(P)` (`P` under an exclusion, narrowed again under a second one) and
`excluded(lent(P))`. The narrowing refuses a policy with a name the parser could not produce, so no declared
name it evaluates holds a parenthesis, collides with a generated one or is a field gate (decision 6). Every public entry resolves its action against the published
policy, so a generated name is an unknown action there, exactly as with the setting `True`; `LocalBackend.schema()`
is the published policy, so nothing public lists one.

**A name is read as the compiler reads it.** Inside an expression `authenticated` and `anonymous` are the actor's
state, whatever a definition declares under those names (`rebac.schema.ast.is_builtin_reference`): such a reference
is never versioned. An arrow leads to the declared member of that name. Where a generated permission stands for an
unchanged declared permission of such a name, it is that permission's expression rather than a reference to it,
which would read as the actor's state. A policy that reads a name two ways (`rebac.schema.ast.name_conflicts`: a
built-in's name as a relation, a definition or a subject type; a name declared as two relations, two permissions,
or a relation and a permission) is refused (`rebac.E025`, kept): the narrowing asks whether a name is a permission
where the compiler reads a relation first. New input may not hold one (refused at `set_schema` and at
stored-policy writes); a stored policy that already does is reported by `rebac.W014`.

**The evaluated policy is public, for proofs, and experimental.** A consumer that proves things about the policy
(that a permission decomposes into parts it evaluates apart) must read the policy the library evaluates.
`LocalBackend.evaluated_schema()` returns it, importable but outside the stable API like the other entry points of
this draft, and a `LocalBackend` convenience with no SpiceDB counterpart (an exception to invariant 1 on its merits): with the setting `True` the published policy, the object `schema()` returns; with `False` the narrowed
one, the very object decisions read, kept per schema snapshot (one object per snapshot, narrowed once; another after
a policy change or a change of the setting). It holds generated permissions (`rebac.links.is_generated(name)`), which
are not actions: they must not be shown to users or passed to any entry. It must not be mutated. A proof from its
text does not hold while any override is stored (`SchemaOverride` rows; the library's prover then proves nothing),
nor on a type that reads a name two ways. `schema()` stays the published policy, the one listing, rendering and
validating against declared names read. `rebac.links.is_generated` and `rebac.links.inherits_through_links` are its
experimental companions.

**Only a boolean means anything.** `True` and `False` choose the policy. Any other value (the string `"False"` an
environment variable gives, `0`, `None`) has no safe reading: the setting is declared with the library's typed
settings (`rebac.conf`) as one that raises, so system check `rebac.E028` reports it by name, a check that reads it
reports that it did not run, and every decision raises `ImproperlyConfigured` until it is fixed, while a write that
decides nothing proceeds. Read as truthy, as it
was, `"False"` silently evaluated the published policy, which grants more than the narrower one asked for, and
`rebac.W012` did not fire; read as falsy, a value meant as on would take access away unannounced. Raising is the one
reading that grants nothing the deployment did not ask for and cannot go unnoticed.

**Where it applies.** `LocalBackend` narrows each composition it evaluates once and keeps the result: the snapshot's
own composition, which `check_new` and the compiled policy's residual evaluator read, and, where there are override
rows, the compiled policy's tagged composition. The field gates find their fields in the published policy and decide
them under the narrowed one, as every decision is; it declares the same names. Compiled reads, point and bulk checks,
`held`, `held_many`, `accessible`, `grants_all`, `permission_expression`, `lookup_subjects`, `check_new`, the field
gates, the walk, the row scope, structural prefetch and the compile check `rebac.E016` see the same policy. The
published policy, `rebac sync`, `rebac build-zed`, `rebac.skeleton.export`, relationship validation and the write
gates' watched columns read the published policy, so the setting needs no sync and can be changed back at any time.
`rebac.W012` warns while it is `False`.

## What is lost, and what is gained

Lost: every grant that reached a record through a link and is not one of the linked record's own grants.

- A record reached through a link lends its own relations (subject sets included), its constant roles and its
  memberships. It does not lend what it inherits from its parents (a page's space, a file's folder), from its
  hierarchy (a folder's parent folders), or what its own links lend.
- A group that is a Django model (`auth/group` mapped to `django.contrib.auth.models.Group`) stores rows, so an
  arrow through a relation to it is a link. An arrow to a relation of the group (`member`, its subject sets
  included) is unchanged; an arrow to a permission of the group lends only that permission's direct part.
- The loss is in granting positions only: an exclusion reached through a link still excludes in full.

Gained: smaller statements, with fewer subqueries, for every read that crosses a link (measured below). Nothing
becomes readable that was not.

**What it does not narrow.** A walk within one type is a hierarchy by rule and is kept whole, whatever its backing:
a self many-to-many, a reverse foreign key within a type, `parent__parent`. These are among the expensive shapes
(recursive id sets), and this setting leaves them as they are.

## Design decisions

1. **The parent link lends what the same record lends in its parent table** (`ptr->lent(Q)`), not the parent
   table's whole permission, confirmed by the owner. The split into tables is a storage detail: a multi-table
   record lends what a single-table record lends.
2. **A constant arrow and a membership arrow in what a record lends are kept as written**, so their target is
   evaluated as the narrowed policy evaluates it, role hierarchy included (the owner's decision of 2026-11-20).
   `relation viewer: role#member` and `relation squad: role` with `squad->member` say the same thing at the same
   cost, so what is lost must not depend on how the policy spells it. What a record lends is therefore more than
   its own rows: through a kept constant or membership arrow it lends the role's narrowed permission whole, with
   that type's same-type hierarchy (kept whole, as everywhere), its constants and memberships, and those
   constants' targets' foreign-key parent chains (a parent arrow is kept in a type's own permissions). It is
   bounded in one respect only: the role's links, and its targets' links, are narrowed again, in their own
   narrowed permissions. A role type that stores rows, reached through a relation that is not a constant, is a
   link like any other.
3. **Exclusions read the published operand, under a name of its own.** A permission `P` used under an exclusion is
   evaluated as published there (`excluded(P)`), even though its granting form (the declared name) is narrowed.
   Without this a narrowed exclusion would take away less and admit rows: the bench-only prototype had that
   defect, and so had this build's first draft until a test caught it.
4. **Narrowed after composition.** Overrides compose with the published baseline first, so an override of a target
   permission (a timed disable on a thread's read) applies to what that thread lends. A rebuilt node carries the
   override tags of the node it replaces, a kept node keeps its identity, and a tagged node is simplified only
   where the result means the same before its deadline and after it.
5. **Only what differs is generated.** A link whose target lends all of `P` keeps its arrow and is not reported. A
   version of `P` that equals `P` is not generated.
6. **Generated names outside the grammar, enforced at both ends** (revised after two reviews). The first draft
   named versions `lent__P`, identifiers a policy could declare; a declared one on a type an arrow reached was
   evaluated in its place, and generated names were accepted as actions, where `excluded__P` grants rows `P`
   denies. Names with a parenthesis are outside the grammar, but the second review showed that only the parser
   enforced the grammar: a `Schema` built in code (`set_schema`, `Definition.extend`) or read from stored rows
   could declare `lent(read)`, and the narrowed arrow `linked->lent(read)` then evaluated it. Now the parser's own
   grammar (`name_errors`, from the lexer's patterns, not a second grammar) refuses new input: `set_schema` and a
   stored-policy write that adds such a name. A stored policy that already holds one keeps loading and deciding
   with the setting `True` (refusing it at load, as the third review showed, took a deployment's authorization
   down to protect a setting that is off by default); the database check `rebac.W013` reports it, as a warning so
   that `migrate`, which runs the database checks, is not stopped by a policy that decides as it did. The
   narrowing therefore checks its own input, and refuses, as a kept `rebac.E025`, a policy with a name the
   language cannot spell, a generated name that the type itself or any type an arrow reaches declares, and a
   definition with two permissions of one name: the one place a stored name of that kind stops decisions, and only where the setting is `False`. Public entries answer a generated name with the
   published policy, and `LocalBackend.schema()` returns the published policy; the evaluated one is internal. The
   narrowed policy is therefore not a `.zed` schema; nothing stores, exports or publishes it, and `rebac explain`
   renders it only to explain it.
7. **Read at each use, keyed everywhere, one source.** The setting is read at each snapshot. The snapshot's
   composition (the overrides active when it was read; it expires at the first of their deadlines) is narrowed as
   it stands, without selecting override rows again, and that one object is what `check_new` walks and what the
   compiled policy's residual evaluator reads. The compiled policy narrows its tagged composition too where there
   are override rows, because that composition keeps rows the statements witness and is a different policy text;
   it is kept by the baseline's identity and the rows. Two narrowings that must agree would be the one place an
   override is selected twice; there is none. The compiled policy's key, the decision cache's
   generation and the schema facts carry the setting; statements, grant profiles, row conditions and structural
   proofs are keyed by the policy. A change inside a process takes effect at the next operation, with no reset and
   without a signal. A scope already built (a `Q` from `queryset_filter`) keeps the policy it was built under.
   The two policies share a revision, so the revision fence of a compiled statement does not tell them apart: the
   policy is a dimension of a queryset's scope decision (the 0.38.0 rule), beside its actor, bypass and database,
   read from the setting at no statement, with the active backend's in-process schema revision, which no fence
   tells apart either. A queryset evaluated or embedded under one value and read under the other builds its scope
   again, and `iterator()` compares the decision again where its statement compiles; an eager `scoped()` queryset
   keeps the value it was built under, as it keeps its actor.
8. **Failure is one typed error; only refusals are kept.** The walks are iterative, so no expression the compiler
   accepts is too deep to narrow. Every failure is a `NarrowingError` (`rebac.E025`): decisions fail closed, writes
   that decide nothing are untouched. A refusal the narrowing makes about the policy (decision 6) is deterministic:
   it is kept per composed policy and the system check reports it for the same policy. Any other exception is
   raised as the error but not kept, so the next operation narrows again.
9. **The report reads the published policy**, whatever the setting, so it can be read before switching:
   `rebac.links.narrowed_arrows()` and `manage.py rebac links [--type T] [--json]`.

## Review of 2026-11-20

An independent review fuzzed the transformation (5,000 seeds per mode: no unsound result, no compiler refusal
introduced, the default path byte-identical) and reported, with what changed:

| Finding | Change |
|---|---|
| A declared `lent__P` on a type an arrow reaches but that has no `P` was evaluated for it, unseen by the collision check | Generated names cannot be declared |
| Generated names were accepted as actions; `excluded__P` grants rows `P` denies | Every public entry resolves its action against the published policy |
| A failed narrowing took relationship writes and ungated model writes down, was retried at every operation, and could hit a recursion limit below the compiler's | Writes and the write gates' columns read the published policy; walks are iterative; one typed error, kept |
| Narrowing ran at every operation while an override deadline was ahead, and three times per published policy otherwise | One narrowing per composed policy, kept and shared |
| Four planted defects passed the default suite | A set-semantics fuzzer of the transformation in the default suite, targeted tests, random policies with the missing kinds and paths |
| Documents claimed more than the code | Corrected |

A second review reported, with what changed:

| Finding | Change |
|---|---|
| Only the parser enforced the name grammar: a programmatic or stored `lent(read)` was evaluated by a narrowed arrow | The parser's grammar is required of every installed, loaded and written schema; the narrowing refuses a declared generated name, on the type and on every type an arrow reaches, and duplicate names |
| The snapshot's narrowing selected override rows again; composing expired ones passed the suite | The snapshot's own composition is narrowed as it stands, the one source `check_new` and the residual evaluator read; tests with an expired arm and a disable |
| The every-entry test ran on compiled statements only; unwrapping the queryset entry passed it | It runs under the row scope in both modes, the bounded walk and structural prefetch, with a named query and a prefetch |
| Every failure was kept, so a transient one stuck until the policy changed | Only refusals about the policy are kept |
| `LocalBackend.schema()` listed generated names, and the published-policy guard was public | `schema()` is the published policy; the evaluated one and the guard are internal; `roles_reaching` and `rebac.rowscope.explain` treat a generated name as the published policy does |
| The fuzzer's port had weakened the `lent(P)` condition | Restored: `lent(P)` within `P` |
| "Bounded" understated what is lent through a kept arrow | Decision 2 states it |

An integration pass with structural prefetch and display rows found, with what changed:

| Finding | Change |
|---|---|
| A permission declared as `authenticated` and named inside an expression was versioned as that permission, though the compiler reads the name as the actor's state: under an exclusion the narrowed policy granted what the published one denies (`pub - authenticated`), and a generated alias `lent(authenticated) = authenticated` granted every authenticated actor | Such a reference is never versioned; a generated permission standing for such a declared one is its expression; a relation of such a name it would stand for is refused. The fuzzer has a variant that declares the name, with the compiler's reading in its reference |

An audit of a consumer framework against the setting found, with what changed:

| Finding | Change |
|---|---|
| A consumer's own proofs about the policy could read the evaluated policy only through an internal method | `LocalBackend.evaluated_schema()`, public, with `is_generated` and `inherits_through_links` |
| The string `"False"` from an environment variable read as truthy: the published policy, silently, and no `rebac.W012` | Only the booleans choose; any other value is `rebac.E028` (the typed settings) and every decision raises |

A review of the joined stack found, with what changed:

| Finding | Change |
|---|---|
| A definition that declares a relation and a permission of one name: the narrowing versioned the permission where the compiler reads the relation, and granted what the published policy denies | The narrowing refuses a policy that reads a name two ways; new input may not hold one (`set_schema`, stored writes); `rebac.W014` reports a stored one; the fuzzer lets a permission take a relation's name |
| The draft's entry points were in the semver-stable public API block | Experimental and outside it; `evaluated_schema()` is an exception to invariant 1 |
| `manage.py rebac links` raised `ImproperlyConfigured` for a setting that is not a bool | A command error naming the check (`rebac.E028`) |

A third review confirmed the soundness work and found one operational problem, with what changed:

| Finding | Change |
|---|---|
| A stored policy holding a name no `.zed` text can spell (written with SQL) no longer loaded: every decision, relationship write, `manage.py check` and `rebac sync` raised | It loads and decides as in 0.37.0; the database check `rebac.W013` reports it, a warning, so `migrate` proceeds; only a write that adds such a name is refused; with the setting `False` the narrowing refuses it (`rebac.E025`) |
| `name_errors` accepted `nil` and slashes in a reference or an arrow's relation, and a caveat reference named `expiration`; one error could be reported twice | Refused; reported once |

A review of the three mechanisms together found, with what changed:

| Finding | Change |
|---|---|
| A queryset reused the scope its query carried under the same actor, bypass and database, and both policies share a revision: a queryset embedded, evaluated or counted under `True` and read under `False` ran the published scope (an embedded `filter(pk__in=...)`, `values()` and `Exists`, `iterator()` after a list), and from `False` to `True` the narrower one | The policy is part of the decision, compared where the 0.38.0 rule compares the rest; an eager `scoped()` queryset keeps its policy, as it keeps its actor (`tests/test_inherit_through_links_decision.py`) |
| A list that mixes declared and generated names answered wholly by the published policy, and `queryset_filter` without its guard, passed the suite, which read only the generated name's answer from a mixed call and never called `queryset_filter` itself | Tests of each declared name in a mixed list and of the backend's entries called directly |
| The report omitting arrows that dispatch to several types passed: every report test used arrows to one type | The report against the narrowed policy on random policies with arrows to two types |
| Dropping an arm's tag where an exclusion's subtrahend empties, or a site's where an intersection's operand empties, passed: the tests checked a subset, not what the narrowed policy means after a deadline | Exact answers before a deadline and after it, for pending and expired overrides |
| Four sentences said the field gates read the evaluated policy | They find their fields in the published policy and decide under the evaluated one, which declares the same names |
| `roles_reaching` names, under `False`, roles the evaluated policy no longer lets reach a permission | Stated as a known limit, with the call that gives the evaluated answer |
| Serving a kept narrowing by identity alone, without checking it was made from the same object, passed | A test that puts another object's narrowing at that identity |

A second look found, with what changed:

| Finding | Change |
|---|---|
| `iterator()` decided its scope where it was called and compiled at its first rows, where its embedded querysets were decided: called under one value and read under the other, it read the first policy, or mixed both in one statement | It compares the decision again where its statement compiles and builds its scope once more if the policy changed |
| An in-process schema is fenced by the backend instance a scope was built with: a queryset kept across `install_schema()` read the replaced policy (older than this proposal) | The decision carries the active backend's in-process revision (under Fixed in the changelog); an eager `scoped()` queryset keeps the scope it built, as it keeps its actor, by its own contract, a limit kept on purpose: after `install_schema()` it reads the replaced policy, after `set_schema()` on its own backend no row |
| The refusals of generated names by `rebac.structural` and `rebac.display.admits_reading` had no test of their own here | A test of `edges()`, `explain()` and `admits_reading()` |
| The changelog called the narrowed policy the object every decision reads | It says what statements are compiled from when override rows are selected |

## Measurements

On the benchmark's application policy (`tests/bench`: 147 definitions, 808 permissions, 1,038 arrows):

- **Arrows by the kind of relation they walk:** 515 constant, 362 parent, 130 link, 21 membership, 10 same type.
- **Of the 130 link arrows** 91 are narrowed (48 generic relations, 30 paths of several hops, 11 reverse
  relations, 2 filters over another table), 37 are kept because their target lends all of the permission, and 2
  are under an exclusion and left whole. 51 permissions hold a narrowed arrow, 10 of them a `read`.
- **The narrowed policy** adds 63 `lent(P)` and 4 `excluded(P)` permissions. One narrowing takes 41 ms the first
  time in a process (classifying relations resolves model fields) and 18 ms after, once per composed policy;
  compiling the published policy's recursion program takes 5 ms.
- **What a `read` reaches**, counted as permissions and subject sets reached through arrows and subject sets,
  transitively, a record's own named permissions opened (the count of the policy census that motivated this
  proposal): published, a median of 6 over 5 types and at most 71 over 33, with 15 reads past 50; narrowed, a
  median of 5 over 4 types and at most 40 over 23, with none past 50. A message's read: 51 over 25 types, then 25
  over 14; a thread's 47 over 23, then 19 over 12; a file's 66 over 30, then 19 over 12.
- **Memberships lent** (design decision 2) change nothing on this policy: none of its 21 membership arrows is in
  what a narrowed target lends.

The benchmark at its small size (7,900 model rows, 4,753 relationships), PostgreSQL 17, registry layout, JIT
off, median of 5 (statements / SQL / wall; planning, execution), measured on a shared machine with a load average
of 55 to 85:

| Read | Actor | Published | No inheritance through links |
|---|---|---|---|
| One message with what a reader shows | heavy | 14 / 906 KB / 748 ms; 535, 82 ms | 14 / 414 KB / 274 ms; 186, 50 ms |
| | light | 10 / 441 KB / 339 ms; 251, 41 ms | 10 / 264 KB / 151 ms; 106, 20 ms |
| First page of threads with messages and parts | heavy | 6 / 350 KB / 285 ms; 205, 43 ms | 6 / 175 KB / 174 ms; 81, 31 ms |
| | light | 4 / 190 KB / 144 ms; 105, 19 ms | 4 / 119 KB / 84 ms; 51, 12 ms |
| A thread's messages with parts and people | heavy | 8 / 384 KB / 302 ms; 215, 46 ms | 8 / 206 KB / 160 ms; 95, 33 ms |
| | light | 5 / 211 KB / 154 ms; 115, 22 ms | 5 / 139 KB / 87 ms; 58, 14 ms |

As many statements are issued; their SQL is 34 to 54 percent smaller, and the reads take 1.6 to 2.7 times less
time, most of the difference in planning. These three reads return the same rows under both policies for the
cast's heavy, light, team and author members, except the first page of threads, where a narrower policy can bring
other threads into the page.

**What is lost on the same data.** For each of 127 models with a `read` permission and rows (up to 400 each) and
five members of the cast (heavy, light, team, author, administrator), `held` under both policies: the published
policy grants 11,845 rows, the narrowed one 11,256. 589 rows are lost (5.0 percent) and none is gained. 20 models
lose rows; the most are tag assignments (220 of 700), files (142 of 1,618), file attachments (91 of 989),
messages (29 of 1,095) and message participants (20 of 1,146). The light member loses most (245), the
administrator 9: a knowledge-graph entity is readable only through the pages of the passages that are its
evidence, a passage lends nothing of that, and under the narrowed policy no one reads those entities.

## Known limits

- **Classification depends on the models.** It is derived once per composed policy and process. A change that
  alters it without publishing a revision (a model's resource type, `REBAC_TYPE_PREFIX`) is seen after any
  setting change or a process restart, as every derived policy fact is.
- **A relation that does not resolve is a link.** A broken backing raises `SchemaError` (`rebac.E009`) where it is
  compiled, in either policy; in what a record lends it is removed instead.
- **Both policies' statements share the statement cache** after a change of the setting, until least recently used
  ones are evicted.
- **It is a different policy, not a mechanism.** `rebac.testing.compare_mechanisms` reports what it loses against
  the published policy; the benchmark runs `nolinks` without comparing its results, as it runs `plain`, because a
  page of a narrower policy can hold rows the published page did not. The invariant is the test suite's.
- **The report lists an arrow under the permission it is written in.** An arrow that is narrowed only where its
  permission is used under two exclusions (in `excluded(P)`) is listed under `P`.
- **Not specified by SpiceDB.** The published policy is what `build-zed` emits; the narrowed one is evaluated by
  the local backend only, and is not a schema SpiceDB could read.
- **`rebac.roles.roles_reaching` reads the published policy.** Under `False` it names a superset of the roles the
  evaluated policy lets reach a permission: a role reached only through an arrow the narrowing removes or narrows
  is still named, though no decision grants through it. `roles_reaching(..., schema=backend().evaluated_schema())`
  gives the evaluated policy's answer.

## Tests

`tests/test_inherit_through_links_fuzz.py` checks the transformation alone against a set-semantics reference: random
policies of two to four types with random classifications, multi-type relations, arrow targets that are relations
on some types, wildcards, subject sets, and overrides tagged as `compose_tagged` tags them, evaluated before and
after a deadline. Every declared permission of the narrowed policy grants a subset of the published one, `lent(P)`
a subset of `P`, `excluded(P)` a superset, and the narrowed policy evaluates wherever the published one does. The
default suite runs 8,000 policies, half of them declaring a permission named `authenticated`, the slow tier 152,000
more.

`tests/test_inherit_through_links.py`:

- the classification of each kind on test models;
- the transformation on small policies: a link, a target that lends everything, one and two subtractions, an
  intersection, an exclusion inside what is lent, a named permission under one and two exclusions, the parent
  link in and under an exclusion, constants, memberships, arrows to several types, field-gate names, depth;
- rows kept and lost for every kind, on SQLite and PostgreSQL;
- every public entry with every generated name of a policy, against the published answer, under each read
  mechanism (compiled statements, the row scope in both modes, the bounded walk, structural prefetch), the
  backend's `queryset_filter` and `permission_expression` called directly among them; a list that mixes declared
  and generated names (`held_many`, `check_bulk_permissions`, the evaluator's batch), in either order, compiled and
  through the bounded walk: each declared name answered as when asked alone, by the narrowed policy, each generated
  one by the published policy; the reviewers' cases;
- names: schemas built in code with names the language cannot spell, refused at installation, at a stored write
  and by the narrowing; with the names check removed, the narrowing still refuses a declared generated name on the
  type and on every type an arrow reaches, and duplicate names; a stored policy that holds one keeps deciding, and
  `rebac.W013` is a warning that `check --database` and `migrate` report and proceed past;
- with the setting `True` nothing is narrowed and statements are byte-identical after a round trip; a change of the
  setting with no signal and no reset, in and out of an evaluator scope, decision cache and schema facts included;
- a narrowing that fails: reads fail closed, writes proceed, the checks report it; a refusal is kept, an
  unexpected failure is retried by the next operation;
- `check_new` and the compiled policy read one narrowing, with an expired override arm and with a disable;
  `schema()` and `roles_reaching` list only published names, and under `False` `roles_reaching` names a superset of
  the evaluated policy's roles, which it names given that policy; `rebac.structural.edges()`, `explain()` and
  `rebac.display.admits_reading()` refuse a generated name;
- one narrowing per composition, with and without a pending deadline and without overrides, served only to the
  object it was made from when another holds its identity; a timed arm and a timed union arm in what
  is lent; an override of the linked permission; an extend whose arm is an exclusion, in the parent table's
  `excluded(lent(P))` that a multi-table child lends, and a tighten whose site is an intersection, in `lent(P)`,
  each losing an operand to the narrowing, pending (read before its deadline and at it) and expired, in both
  storage layouts: the tags held and the exact answer of both policies, so that a tag dropped where an operand
  empties fails; a node with no tag simplified where an operand is removed;
- two families of random policies over the database, on every read path (lists, `permission_expression`, `held`,
  `held_many`, `check_access`, `accessible`, `lookup_subjects`, `grants_all`, `check_new`, inside an evaluator
  scope), the narrowed policy within the published one and each policy equal to itself across paths. The first,
  over threads, profiles, messages and parts, has stored policies with random overrides (timed and untimed) and the
  bounded walk and the row scope on some seeds; the second, over folders, posts, shelves, attachments and
  multi-table branches, has same-type relations, a many-to-many, generic relations both ways, the parent link in a
  hierarchy and links to two types. The default suite runs one small case of each; the slow tier 198 cases in both
  storage layouts;
- the report, `rebac links`, `rebac explain`, the export and `rebac.W012`, `rebac.E025`; the report against the
  narrowed policy on 300 random policies whose arrows dispatch to one type or two: an arrow `via->target` of `T#P`
  is listed if and only if the narrowed `T#P` or `T#excluded(P)` holds `via->lent(target)`.

`tests/test_inherit_through_links_decision.py`: the policy in a queryset's scope decision, in both directions of a
change, in both storage layouts, in and out of an evaluator scope: a queryset embedded in a statement REBAC does not
scope (`In`, `In` over `values()`, `Exists`); every position the 0.38.0 rule covers, with the statement sent byte for
byte the one built under the policy in force; one object evaluated by each path that leaves no result cache and then
by every path (a list, `len()`, `bool()`, `count()`, `exists()`, `aggregate()`, `iterator()`, a lookup on its query,
`Exists()`, a deep copy) and the async forms; `update()`, `delete()` and `bulk_update()`, by their own scope and by
an embedded queryset; an eager `scoped()` queryset, a `Q` and a built expression keeping their policy, and a
statement that embeds the eager queryset compiling it as it is; `iterator()` called under one value and read under
the other, by its own scope and embedded in a plain and in a scoped statement, building its scope once more; a
queryset, as it is and embedded, kept across `install_schema()` and across `set_schema()`; one build per
change of the setting and none while it is unchanged; a bypass deciding no policy.
