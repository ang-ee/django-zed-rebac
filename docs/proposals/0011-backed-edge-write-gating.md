# Proposal 0011: write gating for backed edges

## Problem

A field-backed relation turns an ordinary column or through row into a
permission edge. Changing that column changes who can read what, so the change
needs an authorization decision. Invariant 5d (added with 0.24.0) states the
rule for the common paths, and the code gates M2M writes, reverse-FK accessors,
tracked saves and symmetrical mirror edges. Three groups of paths are still
ungated or gated against the wrong actor. Each is pinned by a strict expected
failure in `tests/test_security_proposal_0011.py` naming this proposal.

1. **Deletes and collector side effects.** `RebacMixin.delete()` and queryset
   `delete()` lift backed edges on another type without checking that type's
   `write`. With `folder.read = viewer - items->locked`, the owner of a locked
   post deletes the post and gains read on the folder. CASCADE and SET_NULL
   also need a gate using the initiating actor's scope. The current column
   gate catches actorless collector updates, but a pinned actor on the row
   being deleted can still differ from the ambient actor used for collected
   rows. The equivalent explicit `post.folder = None; post.save()` is denied.
2. **Reverse-FK `add(bulk=True)`.** The gate reads the ambient actor instead
   of the actor pinned on the folder (invariant 5), a block `sudo` bypasses it
   even when an actor is pinned, and the moved row's own `write` is never
   checked: a folder viewer can re-parent someone else's post, while
   `bulk=False` denies it.
3. **Instance-level through-model writes.** `through.objects.create()`,
   `through(...).save()`, `get_or_create()` and instance `delete()` on an
   auto-created through model are neither gated nor maintained: the actor
   gains the edge after the next rebuild or the next legitimate write to the
   same folder. Queryset writes through the same model are gated.
4. **The `write` boundary.** The exemption for "subject-only" types is
   implemented as "any type without a permission literally named `write`". A
   resource type that names its write permission `edit`, or declares only
   `read`, is maintained without a gate and without an actor: in strict mode
   with no actor, `BackingEntry.objects.create(...)` grants read on
   `test/backinground`.

The three fix rounds that produced 5d each closed some paths and opened others
because the rule was applied path by path. This proposal states it once, at the
level of the index program, and derives every path from it.

## Rule

A write is a set of edge changes. The index program already computes, for any
changed column or through row, the set of (declaring type, affected resource
ids, relation) triples whose edges change; that computation is what
maintenance uses to find its region. The gate is a function of that set, not of
the Django API that produced the write:

- For every affected declaring type that declares a write-class permission,
  the acting subject must hold it on every affected resource id. The
  write-class permission is `write` by default and can be named per type with
  `// rebac:write_permission=<name>` (new schema directive; mirrors how
  `rebac_default_action` names the read-class action).
- A type that declares no write-class permission is exempt only if it declares
  no permissions at all (a pure subject type such as `auth/user`). A type with
  permissions but no write-class permission is a schema error (`rebac.E020`):
  the author must either name the write permission or declare the relation
  unbacked.
- The acting subject is resolved once per write, in the documented order:
  pinned actor on the queryset or instance, then ambient sudo, then
  `current_actor()`, then strict-mode failure. Instance sudo does not reach
  the gate (5a). A pinned actor outranks a block sudo (5).
- The row being moved is itself a resource whose edges change, so it is in the
  affected set and is checked like any other.
- Deletes are writes whose edge changes are removals. The collector's
  CASCADE and SET_NULL rows are collected before any SQL runs, so the affected
  set is known up front.

## Design

1. `rebac.index.program` exposes `edge_changes(model, rows, fields)` returning
   the affected triples for a proposed change, reusing the watch tables it
   already builds for maintenance. Maintenance and the gate call the same
   function, so a path cannot be maintained without being gated.
2. One gate, `rebac.signals.gate_edge_changes(changes, actor)`, batches the
   check per declaring type with one scoped existence query per type (the
   scoped queryset for the write-class action filtered to the affected ids,
   compared by count). It emits one denial audit row per denied type after
   the owner's transaction has unwound.
3. Call sites shrink to: `save_base` (field changes), the collector hook
   (deletes, CASCADE, SET_NULL, in one batch before deletion), `m2m_changed`
   (through rows), and the related-manager wrappers (reverse-FK `add`,
   `remove`, `set`, `clear` in both `bulk` modes). Each computes its change set
   and calls the gate; none decides policy.
4. The `rebac:write_permission` directive is parsed, validated (`E020` when a
   backed type with permissions lacks it and has no `write`), rendered, and
   covered by `build-zed` determinism.

## Correctness

The gate and maintenance derive from one function, so the set of gated paths
equals the set of maintained paths; a write the index would notice is a write
the gate sees. Deletes are gated before the collector runs, so a denied delete
leaves both source and index untouched. The actor order is the documented
resolution order, so 5 and 5a hold by construction.

## Cost

One existence query per affected declaring type per write, in place of the
per-row and per-field queries that remain today. The collector hook adds no
query: it reads the rows the collector already loaded.

## Tests

- The strict expected failures named above become ordinary tests.
- A parity test: for every write path (save with each field kind, queryset
  update, instance and queryset delete, CASCADE, SET_NULL, M2M in both
  directions, reverse-FK `add` in both `bulk` modes, `set`, `clear`,
  symmetrical self-M2M), the set of types the gate checked equals the set of
  types maintenance touched.
- `E020` fires for a backed type with permissions and no write-class
  permission, and is silent for `auth/user`.
- The statement-count test from 0.24.0 (one M2M add touches O(changed rows)).

## Not in scope

- Gating writes to the `Relationship` model itself; those go through the
  backend API or the tuple owner (0.24.0).
- Field read gates on write expressions (0.24.0, `RawSQL` and subqueries).
- Which permission an adapter maps a Django `change_*` codename to; that is
  `codenames.py`.
