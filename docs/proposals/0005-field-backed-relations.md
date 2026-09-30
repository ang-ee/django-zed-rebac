# Proposal 0005 - Field-backed relations

**Target version:** LocalBackend backings shipped; index integration in 0.23.0. SpiceDB projection tracks the
`SpiceDBBackend` roadmap item.
**Status:** Implemented for forward/reverse/M2M and filtered paths under
`LocalBackend`; SpiceDB projection remains phase 2.
**Scope:** One optional field on the `Relation` AST plus a comment-directive carrier in the parser;
LocalBackend projects a backed relation from Django columns into its derived
permission index, with write guards and schema validation. SpiceDB sync is phase 2.

## Why

A structural relation duplicates a Django foreign key. `storage/file#drive` says the same thing as
`File.drive_id`. Without a backing declaration the host
application has to keep the relationship row in step with the column on every write — a `post_save` signal or
equivalent. The column and the row are two sources of truth for one fact.

- Under `LocalBackend` that is a same-database dual-write the host can *almost* keep atomic.
- Under the planned `SpiceDBBackend` it becomes a cross-store dual-write — a Postgres column versus
  SpiceDB's datastore across a gRPC boundary, with no shared transaction and Zookie lag. The sync is
  most fragile exactly where the stakes are highest.

The package ships no FK→tuple sync. The former `REBAC_SYNC_DJANGO_GROUPS`
setting had no implementation. Consumers hand-rolled sync per model: duplicated,
unvalidated, and easy to get subtly wrong (a missed `update_fields`, a bulk write that skips
signals, a partial failure that desynchronizes the two stores).

For a structural relation the tuple is a denormalized copy of the column, and the column is the
source of truth. A relation declared as *backed by* a Django field needs no
application-owned tuple synchronization. The library derives and maintains its
index from that fact; writes bypassing its owners require rebuild.

This is additive. Grant relations that have no column (`owner`, `editor`, `viewer`, `group#member`)
are unchanged: they remain stored tuples, written at the point the grant is decided.

## Design

### 1. Declaration

A relation may be annotated as backed by a model field. The annotation rides in a comment directive
so the `.zed` text stays byte-for-byte valid SpiceDB schema (the comment is invisible to SpiceDB
tooling) while this package's parser lifts it onto the AST:

```zed
definition storage/file {
    relation drive:  storage/drive   // rebac:field=drive
    relation folder: storage/folder  // rebac:field=folder
    relation owner:  auth/user                       // grant — a stored tuple, unchanged

    permission read  = drive->read + folder->read + owner
    permission write = drive->write + owner
}
```

Only explicit directives are in scope. Name-convention inference is deliberately deferred: the
binding lives in the schema, never in the consuming model. The model stays a plain Django model with
a plain FK.

### 2. AST change (`src/rebac/schema/ast.py`)

`Relation` gains one optional field. Default `None` preserves every existing constructor call and
all current behavior:

```python
@dataclass(frozen=True, slots=True)
class FieldBinding:
    path: str              # Django lookup path, e.g. "drive" (forward FK) or "roster__user"
    filters: tuple[tuple[str, Any], ...] = ()   # source-model lookups, same join (proposal 0008)

@dataclass(frozen=True, slots=True)
class Relation:
    name: str
    allowed_subjects: tuple[AllowedSubject, ...]
    with_expiration: bool = False
    backing: FieldBinding | None = None   # NEW
```

A backed relation is constrained at schema-load time:

- exactly one `AllowedSubject`, a concrete type — no wildcard, no subject-set, no `id`-pinned term
  (a column points at one row of one type);
- `with_expiration = False` and no caveat (a column carries neither expiry nor caveat context).

Violations raise `SchemaError` at load, and surface through a Django system check so mismatches fail
fast rather than at first query.

### 3. Projection into the permission index (LocalBackend)

In 0.23.0, `LocalBackend` derives backed edges with Django querysets and reads
permissions from the index. The shared backing resolver follows Django metadata
and the model's **base manager**, so default-manager filtering (for example,
soft deletion) cannot move the authorization boundary.

A forward FK projects one edge to its target; a null FK projects no edge.
Reverse, M2M and filtered paths project qualifying source/target pairs with
filters bound to the same join. Subject and resource identities use canonical
SQL codecs; unsupported custom conversions are refused by `rebac.E014`.
Arrows, union and membership closure are derived from those edges. Checks,
scopes, `accessible` and complete `lookup_subjects` all consume the result,
including covers, per-cover exceptions, conditions and validity intervals.

Supported source writes and index maintenance share one transaction and the
per-alias global lock. `RebacMixin` and queryset write owners open it; plain
backing-path model saves require caller-owned `atomic()` or `ATOMIC_REQUESTS`.
Signal-free bulk writes to plain models, raw fixtures, historical migration
models and direct SQL bypass maintenance and require `rebac index rebuild`
before reads. `rebac index verify` detects full-payload drift.

### 4. Writes are a column operation, not a tuple operation

`write_relationships` / `delete_relationships` targeting a backed relation raise `SchemaError`
("relation `drive` on `storage/file` is field-backed; set `File.drive` instead"). This is the guard
that keeps the dual-write from creeping back in: the only way to change a backed relation is to
change the column. Grant relations are unaffected.

### 5. Queryset scoping

`RebacQuerySet` requests the backend's permission predicate. LocalBackend uses
the same fixed-shape index predicate for stored and backed edges, with concrete
and type-level covers and their exceptions. Django retains caller filters,
annotations and aliases. Graph depth does not add query arms.

### 6. SpiceDB (phase 2, with the `SpiceDBBackend` roadmap item)

SpiceDB cannot read a Postgres column, so under SpiceDB a backed relation must still exist as tuples
in SpiceDB's datastore. The same `backing` declaration drives a **library-owned write-through
projector** that mirrors column changes into
SpiceDB tuples (post-commit, with Zookie handling), implemented once here rather than per consumer.
The `WriteSchema` push prints a backed relation as an ordinary relation; SpiceDB never sees the
directive.

The promise holds on both backends: declare the binding once, write no sync code. `LocalBackend`
maintains its index synchronously with supported column writes;
`SpiceDBBackend` will project it remotely with Zookie tracking.

## Migration for existing consumers

1. Add `// rebac:field=<path>` to the structural relations whose tuples mirror an FK.
2. Delete the host's `post_save`/`post_delete` sync handlers for those relations.
3. Drop any now-redundant stored rows for backed relations. A dedicated pruning command is a
   follow-up convenience, not part of the LocalBackend implementation.
4. Ensure every backing-path source write uses the documented transaction and
   ownership rules, then run sync/rebuild before serving reads.

Grant relations and their write sites are untouched.

## Tests

- Direct check: backed forward FK resolves true/false from the column; null FK denies.
- Arrow: `drive->read` derives the target's grants onto the source; deep paths retain fixed-shape reads.
- `accessible`: backed arrow returns exactly the rows whose FK target grants access; mixed
  `drive->read + owner` unions column-derived and tuple-derived ids.
- Reads use `_base_manager` — a default-manager filter on the model does not change the result.
- Schema load rejects a backed relation with a wildcard, subject-set, caveat, expiration, or
  multiple subject types; the system check reports a missing/mismatched field.
- `write_relationships`/`delete_relationships` on a backed relation raise `SchemaError`.
- `.zed` round-trips: the directive parses onto `Relation.backing`; `WriteSchema` output omits it
  and is valid SpiceDB schema.
- LocalBackend reads of a backed relation issue no `Relationship` query.

## Acceptance

- `Relation.backing` exists on the AST; the parser lifts the comment directive; default `None` keeps
  existing schemas and constructors unchanged.
- `LocalBackend` derives backed relations from columns and serves direct checks,
  scopes, `accessible` and complete `lookup_subjects` through the index.
- Writing a backed relation tuple is rejected with an actionable message.
- Schema-load validation and a Django system check guard the constraints.
- ARCHITECTURE and ZED docs document field-backed relations and the write-guard; the ROADMAP records
  the phase-2 projector under the `SpiceDBBackend` item.
- No change to grant-relation behavior, the stored-tuple path, or SpiceDB schema output for
  un-backed relations.
