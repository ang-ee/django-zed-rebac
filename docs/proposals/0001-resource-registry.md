# Proposal 0001 — `RebacResource` registry + integer FK schema for `LocalBackend`

**Target version:** v0.4 (additive; default off; default flip in 0.5).
**Status:** Approved by upstream maintainer (see *Decisions already locked* below); ready to implement.
**Scope:** `LocalBackend` only. `SpiceDBBackend` is unaffected.

---

## Why

The `Relationship` table stores `resource_type`, `resource_id`, `subject_type`, `subject_id` as `CharField(max_length=64)`. The three hot indexes carry up to ~192 bytes per entry → ~40 entries per Postgres index page. With integer FKs the same keys are ~16 bytes → 500+ entries per page. For a million-row table this is the difference between fitting the hot index in shared_buffers vs not. The recursive-CTE in `LocalBackend` re-walks this index multiple times per check; the gain compounds.

Two correctness-adjacent wins land alongside:

- **Cascade delete** — when a `storage.File` row is deleted, its tuples die with it (no orphan GC).
- **Referential integrity** — can't write a tuple to a non-existent resource.

`SpiceDBBackend` is unaffected (gRPC, doesn't touch the local table). This is purely a `LocalBackend` optimisation.

## Decisions already locked

1. **Upstream PR** (not a downstream patch). Lands in this repo as v0.4.
2. **Settings flag**: `REBAC_LOCAL_BACKEND_STORAGE = "denormalized" | "registry"`. Default `"denormalized"` in 0.4 (so existing users aren't surprised); flip default to `"registry"` in 0.5 once telemetry confirms parity.
3. **Cascade via Django signals**: `post_delete` on `RebacMixin` models removes the matching `RebacResource` row; CASCADE FK on `Relationship.resource_fk` cleans up tuples.
4. **Wire shape unchanged**. `RelationshipTuple` (the public type) and `Relationship.objects.create(resource_type=..., resource_id=...)` kwargs stay string-based. The integer FKs are an internal `LocalBackend` detail.

## Concrete schema

### New model — `rebac.RebacResource`

```python
# src/rebac/models/resource.py (NEW)
class RebacResource(models.Model):
    id = models.BigAutoField(primary_key=True)
    resource_type = models.CharField(max_length=64)
    resource_id = models.CharField(max_length=64)

    # Optional reverse pointer to the Django row this resource represents.
    # NULL for synthetic resources (role objects like storage/role:object_viewer,
    # wildcards like auth/user:*, subject-set sources without a Django model).
    content_type = models.ForeignKey(
        "contenttypes.ContentType", null=True, blank=True, on_delete=models.CASCADE,
        related_name="+",
    )
    object_pk = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        app_label = "rebac"
        constraints = [
            models.UniqueConstraint(
                fields=["resource_type", "resource_id"],
                name="rebac_resource_uniq",
            ),
        ]
        indexes = [
            # For the cascade-from-Django-row lookup path.
            models.Index(fields=["content_type", "object_pk"], name="rebac_resource_ct_idx"),
        ]

    @classmethod
    def upsert_ref(
        cls,
        resource_type: str,
        resource_id: str,
        *,
        content_type=None,
        object_pk: str = "",
    ) -> "RebacResource":
        """INSERT ... ON CONFLICT DO NOTHING RETURNING id. Single round-trip.
        On conflict, returns the existing row. content_type/object_pk are
        populated lazily — first writer wins; later writers can fill them
        in if NULL.
        """
        ...
```

### Refactored `Relationship` (registry mode)

```python
# src/rebac/models/relationship.py — when REBAC_LOCAL_BACKEND_STORAGE == "registry"
class RelationshipRegistry(models.Model):
    resource_fk = models.ForeignKey(
        "rebac.RebacResource", on_delete=models.CASCADE, related_name="+",
        db_column="resource_fk_id",
    )
    relation = models.CharField(max_length=64)
    subject_fk = models.ForeignKey(
        "rebac.RebacResource", on_delete=models.CASCADE, related_name="+",
        db_column="subject_fk_id",
    )
    optional_subject_relation = models.CharField(max_length=64, blank=True, default="")
    caveat_name = models.CharField(max_length=64, blank=True, default="")
    caveat_context = models.JSONField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    written_at_xid = models.BigIntegerField(default=0, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["resource_fk", "relation"], name="rebac_reg_rel_fwd_idx"),
            models.Index(fields=["subject_fk", "relation"], name="rebac_reg_rel_rev_idx"),
            models.Index(
                fields=["subject_fk", "optional_subject_relation"],
                name="rebac_reg_rel_subset_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["resource_fk", "relation", "subject_fk",
                        "optional_subject_relation", "caveat_name"],
                name="rebac_relationship_reg_uniq",
            ),
        ]

    # Backwards-compatible property accessors so existing code keeps working
    # when introspecting individual instances.
    @property
    def resource_type(self) -> str: return self.resource_fk.resource_type
    @property
    def resource_id(self) -> str: return self.resource_fk.resource_id
    @property
    def subject_type(self) -> str: return self.subject_fk.resource_type
    @property
    def subject_id(self) -> str: return self.subject_fk.resource_id
```

### Why two parallel models — Option C

We considered three storage shapes:

- **Option A** — one model with conditional fields driven by the setting. Hard to migrate; rejected.
- **Option B** — two model classes via an abstract base + setting-driven `Meta`. Manager hides which is active.
- **Option C (recommended)** — Keep the existing `Relationship` unchanged for `"denormalized"`. Add `RelationshipRegistry` only used in `"registry"` mode. Both tables ship on disk; the active one is selected at request time. Migration is a one-shot.

C is lowest-risk: existing code paths don't move, the migration command is a clean copy between two tables, and ops can run both schemas during the cutover window.

## Manager — string→FK translation

Public CRUD keeps the string kwargs unchanged:

```python
# Same call site, both storage modes
Relationship.objects.create(
    resource_type="storage/file", resource_id="abc",
    relation="viewer",
    subject_type="auth/user", subject_id="42",
)
```

The active manager translates:

```python
class RelationshipManager(models.Manager):
    def create(self, **kwargs):
        if app_settings.REBAC_LOCAL_BACKEND_STORAGE == "registry":
            resource_fk = RebacResource.upsert_ref(
                kwargs.pop("resource_type"), kwargs.pop("resource_id"),
            )
            subject_fk = RebacResource.upsert_ref(
                kwargs.pop("subject_type"), kwargs.pop("subject_id"),
            )
            kwargs["resource_fk"] = resource_fk
            kwargs["subject_fk"] = subject_fk
        return super().create(**kwargs)

    # filter(), get(), get_or_create(), etc. apply the same translation
    # when string kwargs are passed.
```

Batch upserts via `bulk_create` MUST batch the `upsert_ref` calls (single `INSERT ... ON CONFLICT ... RETURNING id` per batch). Otherwise tuple writes go N+1.

## Cascade plumbing

```python
# src/rebac/signals.py — new handler
@receiver(post_delete)
def _rebac_cascade_resource(sender, instance, **kwargs):
    """Remove the RebacResource row for any deleted RebacMixin-using model.

    The FK cascade on Relationship.resource_fk / .subject_fk then removes
    the tuples. Subject-side cascade (e.g. deleting an auth.User) is
    handled by the same handler because _is_rebac_bound returns True for
    any registered subject type or RebacMixin-bearing model.
    """
    if not _is_rebac_bound(sender):
        return
    if app_settings.REBAC_LOCAL_BACKEND_STORAGE != "registry":
        return
    resource_type = sender._meta.rebac_resource_type
    resource_id = str(_resource_id(instance))
    RebacResource.objects.filter(
        resource_type=resource_type, resource_id=resource_id,
    ).delete()
```

## `LocalBackend` recursive CTEs

The existing CTEs in `src/rebac/backends/local.py` join `Relationship` to itself on string columns. In registry mode they join on integer FKs. The recursion shape is unchanged; only the join condition changes.

```sql
-- denormalized (current)
SELECT * FROM rebac_relationship r1
JOIN rebac_relationship r2
  ON r2.resource_type = r1.subject_type
 AND r2.resource_id = r1.subject_id
 AND r2.relation = r1.optional_subject_relation
WHERE ...

-- registry (new)
SELECT * FROM rebac_relationshipregistry r1
JOIN rebac_relationshipregistry r2
  ON r2.resource_fk_id = r1.subject_fk_id
 AND r2.relation = r1.optional_subject_relation
WHERE ...
```

`accessible_cached()` cache keys should remain string-based at the public surface (the cache lives in `actors.py` and uses `str(subject)` keys) — no change to caching code.

## Settings

```python
# src/rebac/conf.py
"REBAC_LOCAL_BACKEND_STORAGE": "denormalized",  # | "registry"
```

System checks:

- **`rebac.E006`**: error on any value other than `"denormalized"` / `"registry"`.
- **`rebac.W005`**: warning when set to `"denormalized"` post-0.4 — "the registry shape ships a 5-10x index density gain; consider migrating before 0.5 where it becomes default."

## Migration command

```bash
python manage.py rebac migrate-storage [--from denormalized] [--to registry] [--batch 10000] [--dry-run]
```

Behaviour:

1. Reads every row from the source table.
2. Upserts a `RebacResource` row per unique `(resource_type, resource_id)` and `(subject_type, subject_id)` pair.
3. Writes the corresponding `RelationshipRegistry` row.
4. On `--to denormalized`, the reverse.
5. Verifies row-count parity at the end. Aborts on mismatch.
6. The source table is **not** dropped — the operator flips `REBAC_LOCAL_BACKEND_STORAGE` only when migration completes, then can drop manually.

## Files to touch

| File | Change |
|---|---|
| `src/rebac/models/resource.py` | NEW — `RebacResource` model + `upsert_ref` classmethod |
| `src/rebac/models/__init__.py` | Export `RebacResource`, `RelationshipRegistry` |
| `src/rebac/models/relationship.py` | Add `RelationshipRegistry` alongside existing `Relationship` |
| `src/rebac/managers.py` | New `RelationshipManager` that translates string kwargs to FKs when `"registry"` |
| `src/rebac/signals.py` | New `_rebac_cascade_resource` handler on `post_delete` |
| `src/rebac/migrations/000X_rebac_resource.py` | NEW — schema migration for the new tables |
| `src/rebac/backends/local.py` | Switch CTE joins on the active storage flag; route reads to the active table |
| `src/rebac/relationships.py` | `write_relationships` / `delete_relationships` honour the setting (mostly transparent via manager) |
| `src/rebac/conf.py` | New setting + check error for invalid values |
| `src/rebac/checks.py` | `rebac.W005` warning when on `"denormalized"` post-default-flip; `rebac.E006` for invalid value |
| `src/rebac/management/commands/rebac.py` | New `migrate-storage` subcommand |
| `tests/test_resource_registry.py` | NEW — tests for `RebacResource`, `upsert_ref`, cascade |
| `tests/test_local_backend_registry.py` | NEW — mirror of `test_local_backend.py` but `REBAC_LOCAL_BACKEND_STORAGE = "registry"` |
| `tests/test_migrate_storage.py` | NEW — covers the migration command in both directions |
| `tests/conftest.py` | Optional fixture that parametrises tests across both storage modes |
| `README.md` | Add the storage-mode setting + perf note to highlights |
| `docs/ARCHITECTURE.md` | New "Storage modes" section under `Models`; document the migration command; note the 0.5 default flip |
| `CHANGELOG.md` | 0.4 entry covering the new setting, migration command, and planned 0.5 default flip |

## Test coverage required

- `RebacResource.upsert_ref` is idempotent under contention (two concurrent inserts produce one row, both return the same id).
- `Relationship.objects.create(resource_type=..., resource_id=...)` works identically in both storage modes (the public API is identical).
- Deleting a Django-model resource cascades to its `Relationship` rows in registry mode; doesn't break in denormalized mode.
- Deleting an unrelated row that happens to share a `pk` doesn't touch the wrong tuples (content_type discrimination).
- All existing `LocalBackend` tests pass in **both** storage modes (parametrise the suite — see `tests/conftest.py`).
- `migrate-storage` round-trips: `denormalized → registry → denormalized` produces a byte-identical export.
- `migrate-storage --dry-run` is read-only.
- `rebac.E006` fires on invalid setting; `rebac.W005` fires on the default-flip warning.

## Out of scope — do NOT change

- `SpiceDBBackend` — gRPC, doesn't touch the local table.
- The wire shape (`RelationshipTuple`, `SubjectRef`, `ObjectRef`) — public API stays string-based.
- The `Relationship.objects.create(...)` kwargs — same string args, different storage internally.
- The schema parser, AST, or `.zed` grammar.
- The check API (`check_access` / `has_access` / `accessible`) — same semantics.
- `sudo()` / `system_context()` / actor resolution — unchanged.
- `rebac.roles` — unchanged (uses the same manager surface).
- `auth/anonymous` and the W004 lint — unchanged.

## Acceptance

- All 245+ existing tests pass in `denormalized` mode (default).
- All 245+ existing tests pass in `registry` mode (run via override or parametrised fixture).
- New tests added for `RebacResource`, cascade, migration command.
- `python manage.py rebac migrate-storage --to registry --dry-run` on a populated DB reports the row count without writes.
- README + ARCHITECTURE.md updated.
- CHANGELOG entry for 0.4 covers the new setting + migration command + the planned 0.5 default flip.

## Context from prior work (read before touching shared files)

Recent additions in v0.3.x that touch overlapping files; none conflict with this work but the agent should be aware:

- `src/rebac/actors.py` — added `ANONYMOUS_ACTOR`, `anonymous_actor()`, `is_anonymous_actor()`. Default resolver returns the anonymous SubjectRef for unauthenticated requests.
- `src/rebac/middleware.py` — `ActorMiddleware` now opens the `accessible_cached()` bracket per request and caches the resolver lookup.
- `src/rebac/roles.py` — module: `grant` / `revoke` / `roles_of` / `members_of` / `imply` / `unimply` / `implies_of` / `implied_by_of` (the `<namespace>/role` convention layer). The registry-storage work MUST ensure `rebac.roles` continues to work identically in both storage modes (its writes go through `Relationship.objects.get_or_create` / `.create`).
- `src/rebac/schema/ast.py` + `src/rebac/schema/parser.py` — `AllowedSubject` gained an `id` field; parser accepts `<type>:<id>#<relation>` shape in relation type unions.
- `src/rebac/checks.py` — added `rebac.W004` (universal-admin convention lint).
- `src/rebac/conf.py` — new settings `REBAC_ANONYMOUS_TYPE`, `REBAC_UNIVERSAL_ADMIN_ROLE`.

None of these conflict with the registry work, but the following files are shared: `conf.py`, `checks.py`, `actors.py`, `signals.py`. Pull from main before starting.

## Rollout plan (for the human reviewer)

- **0.4** ships the new tables + manager + migration command, default `"denormalized"`. Existing deployments see no behaviour change unless they opt in. `rebac.W005` starts surfacing for everyone, encouraging migration.
- **0.5** flips the default to `"registry"`. Operators who haven't migrated get a `rebac.E007` error at startup pointing at the migration command. Migration is documented as a one-shot prerequisite.
- **0.6** drops the denormalized code path entirely. The `Relationship` model is removed; `RelationshipRegistry` is renamed to `Relationship`. CHANGELOG flags it as breaking.
