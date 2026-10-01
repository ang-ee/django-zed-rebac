# Proposal 0012: stale overrides and autocommit writes

## Problem

Two lifecycle gaps let the effective permission widen without any write that
says so. Each is pinned by a strict expected failure naming this proposal.

1. **A stale narrowing override is dropped whole.** A `disable` or `tighten`
   override of `auditor + is_active` narrows `read`. When a later schema sync
   removes `auditor` from the baseline, the override no longer parses against
   the schema, so 0.24.0 ignores it and raises `rebac.W010`. `read` reverts to
   the full baseline and the users `is_active` excluded regain access. The
   only signal is a warning on the next `manage.py check`
   (`tests/test_overrides.py`, narrowing case).
2. **Autocommit writes on tracked third-party models lose their old state.**
   An autocommit `user.save()` that changes an attribute-backed `last_name`
   while a consumer `pre_save` handler writes a tuple emits no D2 warning
   (0.24.0 suppressed it for `auth.User` and `auth.Group`), and the user keeps
   read on `test/bucket:a` after moving to `b`. The consumer's owner reaps the
   old-state work rows that the autocommit pass never registered
   (`maintain.py`, `defer_signal_pass`), and a concurrent process can do the
   same (`tests/test_signals_owners.py`).

## Rule

- **An override that no longer resolves fails closed for the arm it cannot
  evaluate.** For a narrowing override (`disable`, `tighten`), an unresolved
  name is `nil`: `disable` of an unresolved name disables nothing further but
  the override still applies to the names it can resolve; `tighten` with an
  unresolved name intersects with `nil`, which denies. For a widening override
  (`extend`, `loosen`), an unresolved name contributes nothing. Either way the
  effective permission is never wider than it was before the baseline
  changed. `W010` stays as the signal; it is a warning about a row that needs
  editing, not about access.
- **`sync` refuses a baseline change that strands a narrowing override** unless
  `--force` is given, and names the rows. An operator who removes a relation
  that an emergency `disable` depends on must say so.
- **Old-state capture is owned by the pass that captured it.** A pass's work
  rows are reaped only by that pass or by a rebuild, never by another owner's
  exit. An autocommit pass registers itself like any other, and D2's warning
  is restored for every tracked model, `auth.User` included; the consumer
  silences it by wrapping the write in `atomic()`, as the spec already says.

## Design

1. `composition.py`: resolve each override arm against the composed schema
   per name, substituting `nil` for an unresolved name in a narrowing arm and
   dropping it in a widening arm; attach the unresolved names to the `W010`
   message. The parser's structured reference issues (0.24.0) provide the
   names.
2. `management/commands/rebac.py sync`: before publishing, compose the new
   baseline with the stored overrides; if any narrowing override gains an
   unresolved name, fail with the row ids unless `--force`.
3. `index/maintain.py`: tag work rows with their pass id (they already carry
   `region`); the reap at owner exit deletes only rows of the exiting pass and
   its nested passes. Register autocommit passes in `defer_signal_pass`.
   Restore the D2 warning for all tracked models.

## Correctness

Composition is monotone under the rule: replacing an unresolved name by `nil`
in a narrowing arm can only shrink the result, and dropping it from a widening
arm can only shrink the result, so a baseline change never widens access
through a stale row. Pass-scoped reaping means an owner can only lose state it
captured itself, which it reads before SQL and consumes at its own exit.

## Cost

One extra composition per `sync`. Work-row reaping gains a pass-id predicate on
an indexed column.

## Tests

- The two strict expected failures become ordinary tests, plus the `extend`
  case already present.
- `sync` against a baseline that strands a `tighten`: refused, then accepted
  with `--force`, with the row named in the output.
- Concurrent owners on PostgreSQL: a consumer owner exiting while an
  autocommit pass holds captured state; the autocommit pass derives from its
  own old state. Marked `postgresql`.

## Not in scope

- Validating override expressions at admin save time against future baselines.
- Per-tenant override scope (ARCHITECTURE § open questions).
