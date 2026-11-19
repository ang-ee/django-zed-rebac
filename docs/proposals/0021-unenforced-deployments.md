# 0021: Unenforced deployments

Status: **draft, not accepted.** One setting, `REBAC_ENFORCE_PERMISSIONS`, `True` by default. The contract is
specified in `docs/ARCHITECTURE.md § Unenforced deployments`.

## Problem

Two legitimate uses have no supported answer today.

- **A deployment with one trusted user.** A private installation of an application built on this library has
  nobody to protect rows from. It still pays for every scope, guard and field gate, and its operator still has to
  keep an administrator role assigned for anything to be visible.
- **Measuring an application's own queries.** To see what `select_related` and `prefetch_related` cost in an
  application, or to compare a read with and without the library, the only tools are `sudo()` blocks around each
  read (which audit, need a reason, and are not honoured by a queryset that carries an actor) or
  `_base_manager`, which is a different code path from the one being measured.

## Contract

With `REBAC_ENFORCE_PERMISSIONS = False`:

1. Every read is Django's own read. A queryset gets no scope whatever actor it carries, a strict call without an
   actor does not raise, related rows load without a guard, no field is redacted.
2. Every write is allowed, without an actor.
3. Every check answers yes: `check_access`, `held` and `held_many` for every candidate, `grants_all`,
   `accessible` for every stored row.
4. The library issues no permission statement on these paths.
5. Relationships and the policy are still written and synchronised. Removing the setting restores enforcement
   on the same database, with the grants written meanwhile.
6. `lookup_subjects` still answers from the policy: it is a question about the policy, not a check.

## What it is not

- **Not a bypass.** `sudo()` and `system_context()` are blocks with a reason that are audited. This is a state of
  the deployment: no audit row is written, because there is no decision to record.
- **Not per request, per user or per model.** It is read from settings. An application that needs some callers
  unrestricted has roles and `sudo()`.

## Guard rails

- System check `rebac.W011` (a warning) whenever the setting is `False`.
- One `WARNING` on the `rebac` logger when the application starts, for a server that never runs the checks.
- The name states the consequence in plain words and the default is the safe value, so the unsafe state has to be
  written out as `REBAC_ENFORCE_PERMISSIONS = False`.

## Implementation

- `actors.resolve`, through which every queryset, model instance and signal gate resolves its authority,
  answers `(None, True)` first. `actors.bypassing()` replaces the direct reads of the ambient sudo state in the
  bulk write paths and the decorators.
- `LocalBackend`'s decision methods answer before compiling anything.

## Tests

`tests/test_enforcement_off.py`: the default still enforces; reads with no actor, an actor that holds nothing and
one that holds something return every row with no permission statement; `rebac_select_related` and
`rebac_prefetch_related` issue Django's statements alone; no field is redacted; every check allows; every write is
allowed without an actor; a guarded function runs; relationships written meanwhile decide again when the setting
is restored; the system check fires.
