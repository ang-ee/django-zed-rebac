# Proposal 0005 - Field-backed relations

**Target version:** future minor release (LocalBackend resolution). SpiceDB projection tracks the
`SpiceDBBackend` roadmap item.
**Status:** Partially implemented for explicit forward FK/one-to-one field bindings under
`LocalBackend`; SpiceDB projection remains phase 2.
**Scope:** One optional field on the `Relation` AST plus a comment-directive carrier in the parser;
LocalBackend resolves a backed relation from a Django model column instead of a stored tuple; a
write-guard and schema-load validation around it. No walker change. SpiceDB sync is phase 2.

## Why

A structural relation duplicates a Django foreign key. `storage/file#drive` says the same thing as
`File.drive_id`. Today the relation exists *only* as a row in the `Relationship` table, so the host
application has to keep that row in step with the column on every write — a `post_save` signal or
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
source of truth. The engine should read the column directly. A relation declared as *backed by* a
Django field needs no stored tuple, no sync, and cannot drift — there is only one fact.

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

### 3. Resolution (LocalBackend only; the walker is untouched)

The tri-state walker already delegates the two side-effectful steps to backend-supplied callbacks —
`ResolveRelation` and `ResolveArrow` on `WalkContext` (`src/rebac/schema/walker.py:50`, `:63`,
`:77`). All field-backed logic lives in `LocalBackend`'s implementations of those callbacks; operator
precedence, sub-permission cycles, depth, and tri-state combinators stay the walker's single
responsibility.

`LocalBackend` needs a `resource_type → Django model` resolver. This already exists in practice:
`RebacMixin` requires `Meta.rebac_resource_type`, and `RebacResource` carries the
`content_type`/`object_pk` back-pointer. Reads must go through the model's **base manager** so a
default manager's app-level filtering (e.g. soft-delete) cannot move the authorization boundary.

- **Direct relation** — `subject ∈ drive on (storage/file, X)` with `drive` backed by forward FK
  `drive`: true iff `subject` is `storage/drive:<id>` and `File._base_manager.filter(pk=X,
  drive_id=subject.subject_id).exists()`. No tuple read. A null FK yields `False`.
- **Arrow** — `drive->read`: read the target id from the row
  (`File._base_manager.filter(pk=X).values_list("drive_id", flat=True)`), then evaluate `read` on
  `storage/drive:<that id>` at `depth + 1`, exactly as a tuple arrow would.
- **`accessible(subject, action, storage/file)`** — for a permission that reduces to a backed arrow
  (`read = drive->read`), compute the recursive grant set once via the existing machinery
  (`accessible(subject, "read", "storage/drive")`), then the accessible files are
  `File._base_manager.filter(drive_id__in=<those ids>)`. Branches are composed and unioned the same
  way the walker unions a permission expression, so mixed permissions
  (`drive->read + owner + editor`) combine the column-derived set with the tuple-derived set.
- **`lookup_subjects`** — for a backed relation the subject is read straight from the column.

### 4. Writes are a column operation, not a tuple operation

`write_relationships` / `delete_relationships` targeting a backed relation raise `SchemaError`
("relation `drive` on `storage/file` is field-backed; set `File.drive` instead"). This is the guard
that keeps the dual-write from creeping back in: the only way to change a backed relation is to
change the column. Grant relations are unaffected.

### 5. Queryset scoping

`RebacQuerySet._apply_scope_in_place` keeps its contract — `accessible(...)` returns resource ids and
the scope is `Q(pk__in=...)`. A later optimization may push a backed arrow down to
`Q(<path>_id__in=<accessible target ids>)`, which is smaller and index-friendly, but that is not
required for correctness and is out of scope here.

### 6. SpiceDB (phase 2, with the `SpiceDBBackend` roadmap item)

SpiceDB cannot read a Postgres column, so under SpiceDB a backed relation must still exist as tuples
in SpiceDB's datastore. The same `backing` declaration drives a **library-owned write-through
projector** that mirrors column changes into
SpiceDB tuples (post-commit, with Zookie handling), implemented once here rather than per consumer.
The `WriteSchema` push prints a backed relation as an ordinary relation; SpiceDB never sees the
directive.

The promise holds on both backends: declare the binding once, write no sync code. `LocalBackend`
reads the column live (strongly consistent, no Zookie); `SpiceDBBackend` projects it (eventual,
Zookie-tracked, same as any SpiceDB relation).

## Migration for existing consumers

1. Add `// rebac:field=<path>` to the structural relations whose tuples mirror an FK.
2. Delete the host's `post_save`/`post_delete` sync handlers for those relations.
3. Drop any now-redundant stored rows for backed relations. A dedicated pruning command is a
   follow-up convenience, not part of the LocalBackend implementation.

Grant relations and their write sites are untouched.

## Tests

- Direct check: backed forward FK resolves true/false from the column; null FK denies.
- Arrow: `drive->read` walks the column to the target and evaluates there; depth increments.
- `accessible`: backed arrow returns exactly the rows whose FK is in the recursive grant set; mixed
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
- `LocalBackend` resolves backed relations from columns across direct/arrow/`accessible`/
  `lookup_subjects`, with no walker change.
- Writing a backed relation tuple is rejected with an actionable message.
- Schema-load validation and a Django system check guard the constraints.
- ARCHITECTURE and ZED docs document field-backed relations and the write-guard; the ROADMAP records
  the phase-2 projector under the `SpiceDBBackend` item.
- No change to grant-relation behavior, the stored-tuple path, or SpiceDB schema output for
  un-backed relations.
