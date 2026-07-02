# Upstream API Seams Design

## Purpose

Expose four library-owned REBAC contracts as public APIs so downstream Angee code can delete private probes and local reimplementations:

- effective actor resolution probes in `angee/base/mixins.py`
- relationship storage shape sniffing in `angee/base/rebac.py`
- permission AST walking in `iam/roles.py`
- the `AngeeManager.check_create` type-level fallback branch

Item 5 from the original request, aggregate-safe scoping and `rebac_select_related` ownership, stays out of this pass unless a test exposes a regression. `RebacQuerySet.aggregate()` is already actor-scoped in the current codebase.

## Scope

This pass implements items 1-4:

1. Public effective actor resolution on querysets and instances.
2. Mode-agnostic relationship model query/projection helpers.
3. Persisted-schema permission introspection helpers.
4. Const-backed virtual tuple injection in `check_new()`.

The implementation must be test-first. Public behavior should be documented in `docs/ARCHITECTURE.md`; ZED docs only need updates if const-backed create preflight semantics need an author-facing note.

## Public Effective Actor Resolution

### Contract

Add a public method to both `RebacQuerySet` and `RebacMixin`:

```python
def effective_actor(self, *, strict: bool = False) -> tuple[SubjectRef | None, bool]:
    """Return ``(actor, is_sudo)`` for this queryset or instance."""
```

The tuple shape is intentional. `SubjectRef | None` alone cannot distinguish "sudo bypass" from "no actor".

### Semantics

`effective_actor(strict=False)` is an observer and must not raise in the default mode. It returns:

- `(actor, False)` when a non-sudo actor is resolved.
- `(None, True)` when per-object or ambient sudo is active.
- `(None, True)` when `REBAC_STRICT_MODE=False` and no actor is present.
- `(None, False)` when `REBAC_STRICT_MODE=True` and no actor is present, unless `strict=True`.

When `strict=True`, the method raises `MissingActorError` for the strict-mode no-actor case. Queryset materialisation and write gates continue to use the strict behavior internally.

### Precedence

The mixin method must match `RebacMixin.check_access()` and queryset behavior:

1. Per-instance or per-queryset sudo.
2. Ambient sudo, which overrides a pinned actor at check time.
3. Per-instance or per-queryset pinned actor.
4. Ambient `current_actor()`.
5. Strict-mode handling.

This deliberately fixes Angee's current local "pinned else ambient" observer rule, which misses ambient sudo precedence.

### Compatibility

Keep `RebacQuerySet._resolve_effective_actor()` as a private compatibility wrapper for one minor release. It should call `effective_actor(strict=True)`, carry a deprecation note in its docstring, and name the removal target. No third copy of the resolution rule should remain.

## Relationship Query Surface

### Contract

Expose storage-mode-agnostic helpers through the active relationship model's queryset/manager. Both `Relationship` and `RelationshipRegistry` must support the same public surface:

```python
def for_resource(self, resource_type: str, resource_id: str) -> Self: ...
def for_subject(
    self,
    subject_type: str,
    subject_id: str,
    optional_relation: str | None = None,
) -> Self: ...
def order_by_resource(self) -> Self: ...
def order_by_subject(self) -> Self: ...
def wire_values(self) -> Iterable[dict[str, Any]]: ...
```

Use `None` as the "any optional subject relation" sentinel. Empty string remains the exact "no optional relation" value.

`wire_values()` returns the same row contract already used by `rebac.relationships` audit snapshotting:

```python
{
    "resource_type": str,
    "resource_id": str,
    "relation": str,
    "subject_type": str,
    "subject_id": str,
    "optional_subject_relation": str,
    "caveat_name": str,
}
```

Registry mode must project through `resource_fk` / `subject_fk` internally; callers must never need to inspect `_meta.fields`.

### Generic Subject Resolution

Add a generic resolver owned by the library:

```python
def resolve_subjects(refs: Iterable[SubjectRef]) -> dict[SubjectRef, models.Model]:
    ...
```

It should use the existing resource registry (`model_resource_type()`, `model_for_resource_type()`, and per-model id attrs) instead of special-casing users. Missing models or missing rows are omitted. Downstream code can layer display-label policy on top.

### Downstream Deletion Check

This deletes `angee/base/rebac.py` and replaces filter/order plumbing in `iam/roles.py` and `integrate/models.py` with public relationship helpers.

## Schema Introspection

### Source Of Truth

Introspection must read the persisted, effective schema used by enforcement, not re-parse `.zed` files. The implementation should obtain the schema through `backend().schema()` or accept an explicit `Schema` for tests/tools. This keeps the hub aligned with `rebac sync`, `SchemaOverride`, and startup checks.

### Stable API

Create `rebac.schema.introspection` with helper-level stability. AST node classes remain implementation details.

Initial helpers:

```python
def relation_dependencies(
    schema: Schema,
    resource_type: str,
    permission: str,
) -> frozenset[str]: ...

def permission_sources(
    schema: Schema,
    resource_type: str,
    permission: str,
) -> PermissionSources: ...

def permissions_reaching_relation(
    schema: Schema,
    resource_type: str,
    relation: str,
) -> frozenset[str]: ...
```

The helpers own traversal over `PermRef`, `PermArrow`, `PermBinOp`, `PermNil`, built-ins, and sub-permission refs. Future expression nodes must be handled here before they become public behavior.

### Role Helper

Add a role-specific helper in `rebac.roles` or a nearby module:

```python
def roles_reaching(
    resource_type: str,
    permission: str,
    *,
    role_resource_type: str,
    schema: Schema | None = None,
) -> frozenset[ObjectRef]: ...
```

The role convention is library-owned, but the namespace is not. Callers pass `role_resource_type` such as `"angee/role"` or `"storage/role"`.

### Downstream Deletion Check

This shrinks `iam/roles.py` by deleting its direct AST walk over `PermBinOp`, `PermRef`, and `PermArrow`.

## `check_new` Const Overlay

### Contract

Before evaluating a not-yet-persisted object, `check_new()` must merge schema-derived const-backed relations into the caller's virtual relationship overlay.

For:

```zed
definition blog/post {
    relation admin: angee/role // rebac:const=admin
    permission create = parent->create + admin->member
}
```

the preflight overlay behaves as if the new object carried:

```text
blog/post:<virtual>#admin @ angee/role:admin
```

The tuple is virtual only. The subsequent reachability check remains real: `admin->member` dispatches into the active backend for `angee/role:admin#member`, including caveats and subject-set traversal.

### Merge Rules

- Caller-supplied relationships remain authoritative for ordinary relations.
- Const-backed relations are appended when the schema declares them.
- If a caller also supplies entries for a const-backed relation, the merged overlay should include both but normal relation type filtering still applies. A follow-up validation may reject caller-supplied tuples for synthetic relations, but this pass only needs create preflight parity with local evaluation.

### Security Regression

Add a deny-side regression for the over-grant class:

- schema: create permission depends on a parent FK arrow
- actor has create/write permission in one parent
- new row points at a different parent where the actor lacks permission
- `check_new()` denies
- no type-level create fallback is needed

Also add the const-backed positive/negative create tests:

- member of const role can create through `admin->member`
- non-member cannot create

### Downstream Deletion Check

This removes the fallback branch from `AngeeManager.check_create` in the same downstream pin bump. If the fallback survives, the over-grant survives.

## Documentation And PR Notes

The PR description must name the Angee deletions explicitly:

- `angee/base/mixins.py` effective actor probe
- `angee/base/rebac.py`
- AST walk in `iam/roles.py`
- `AngeeManager.check_create` fallback branch

`docs/ARCHITECTURE.md` should document:

- `effective_actor(strict=False)` and the tuple semantics
- relationship manager helpers and `resolve_subjects()`
- schema introspection helper stability, with AST nodes private
- const-backed relation injection in `check_new()`

## Testing Strategy

Tests should be added or extended in:

- `tests/test_managers.py` for queryset `effective_actor()` default and strict behavior.
- `tests/test_mixin.py` for instance `effective_actor()` precedence, especially ambient sudo over pinned actor.
- relationship storage tests, ideally parametrized over `REBAC_LOCAL_BACKEND_STORAGE`, for filtering, ordering, `wire_values()`, and `resolve_subjects()`.
- new schema introspection tests covering refs, arrows, binops, built-ins, sub-permission refs, and parameterized role resource types.
- `tests/test_preflight.py` for const-backed overlay and the parent-specific deny regression.

The full verification target after implementation is the focused test set plus the repository's normal test command from `pyproject.toml` or existing Makefile.
