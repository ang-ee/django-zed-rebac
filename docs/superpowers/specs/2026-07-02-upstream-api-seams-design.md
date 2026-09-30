# Upstream API Seams Design

## Purpose

Expose four library-owned REBAC contracts as public APIs so downstream applications can delete private probes and local reimplementations:

- effective actor resolution probes in application mixins
- relationship storage shape sniffing in application adapters
- permission AST walking in role tooling
- application managers' type-level create fallback branches

Item 5 from the original request, aggregate-safe scoping and `rebac_select_related` ownership, stays out of this pass unless a test exposes a regression. `RebacQuerySet.aggregate()`, `count()`, and `exists()` are already actor-scoped; downstream aggregate wrappers can delegate to those APIs.

## Scope

This pass implements items 1-4:

1. Public effective actor resolution on querysets and instances.
2. Mode-agnostic relationship model query/projection helpers.
3. Persisted-schema permission introspection helpers.
4. Const-backed virtual tuple injection in `check_new()`.

The implementation must be test-first. Public behavior should be documented in `docs/ARCHITECTURE.md`; `docs/ZED.md` must document const-backed create preflight injection and rejection of caller-supplied virtual tuples for const-backed relations.

## Public Effective Actor Resolution

### Contract

Add a public method to both `RebacQuerySet` and `RebacMixin`:

```python
def effective_actor(self, *, strict: bool = False) -> tuple[SubjectRef | None, bool]:
    """Return ``(actor, is_unscoped)`` for this queryset or instance."""
```

The tuple shape is intentional. `SubjectRef | None` alone cannot distinguish "sudo bypass" from "no actor".
The second element means "this surface resolves to unscoped/bypass evaluation",
not specifically "the caller entered sudo". It is true for explicit sudo,
ambient sudo that wins precedence, and non-strict no-actor fallthrough.

### Semantics

`effective_actor(strict=False)` is an observer and must not raise in the default mode. It returns:

- `(actor, False)` when a scoped actor is resolved.
- `(None, True)` when per-object or per-queryset sudo is active, or when ambient sudo wins because no actor was explicitly pinned.
- `(None, True)` when `REBAC_STRICT_MODE=False` and no actor is present.
- `(None, False)` when `REBAC_STRICT_MODE=True` and no actor is present, unless `strict=True`.

When `strict=True`, the method raises `MissingActorError` for the strict-mode no-actor case. Queryset materialisation and write gates continue to use the strict behavior internally.

### Precedence

The library currently has a pre-existing divergence: querysets check a pinned
actor before ambient sudo, while `RebacMixin.check_access()` checks ambient sudo
before a pinned actor. This pass deliberately unifies both surfaces on the
fail-closed rule that explicit scope beats ambient scope.

Unified precedence:

1. Per-instance or per-queryset sudo.
2. Per-instance or per-queryset pinned actor.
3. Ambient sudo.
4. Ambient `current_actor()`.
5. Strict-mode handling.

This preserves the current queryset behavior, changes instance `check_access()`
in the fail-closed direction, and supports an elevated-block pattern where
code pins an actor to compute a user's view inside a broader
`system_context()` or `sudo()` scope. If code truly wants bypass, it must use
per-object or per-queryset `.sudo(reason=...)`.

### Compatibility

Keep `RebacQuerySet._resolve_effective_actor()` as a private compatibility wrapper for one minor release. It should call `effective_actor(strict=True)`, carry a deprecation note in its docstring, and name the removal target. No third copy of the resolution rule should remain.

`RebacMixin.check_access()` should call `self.effective_actor(strict=True)` so
there is one resolution rule for instances. Queryset materialisation and write
gates should call `self.effective_actor(strict=True)` directly or through the
compatibility wrapper.

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

Application adapters and role tooling can replace storage-specific filter/order
plumbing with public relationship helpers.

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

`PermissionSources` is a frozen public result shape:

```python
@dataclass(frozen=True, slots=True)
class PermissionSources:
    direct_relations: frozenset[str]
    arrows: frozenset[tuple[str, str]]
    builtins: frozenset[str]
    subpermissions: frozenset[str]
```

- `direct_relations` contains relation refs directly reached by the permission or by sub-permissions it references.
- `arrows` contains `(via_relation, target_permission)` pairs.
- `builtins` contains bare schema actor terms such as `anonymous` and `authenticated`.
- `subpermissions` contains same-definition permission names traversed while collecting sources.

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

The role convention is library-owned, but the namespace is not. Callers pass `role_resource_type` such as `"platform/role"` or `"storage/role"`.

### Downstream Deletion Check

Role tooling can delete its direct AST walk over `PermBinOp`, `PermRef`, and `PermArrow`.

## `check_new` Const Overlay

### Contract

Before evaluating a not-yet-persisted object, `check_new()` must merge schema-derived const-backed relations into the caller's virtual relationship overlay.

For:

```zed
definition blog/post {
    relation admin: platform/role // rebac:const=admin
    permission create = parent->create + admin->member
}
```

the preflight overlay behaves as if the new object carried:

```text
blog/post:<virtual>#admin @ platform/role:admin
```

The tuple is virtual only. The subsequent reachability check remains real: `admin->member` dispatches into the active backend for `platform/role:admin#member`, including caveats and subject-set membership. LocalBackend 0.23.0 answers that persisted check from the index.

### Merge Rules

- Caller-supplied relationships remain authoritative for ordinary relations.
- Const-backed relations are appended when the schema declares them.
- Caller-supplied entries for const-backed relations are rejected with `SchemaError`. Silently merging them would let a caller add `admin @ platform/role:editor` beside the schema-derived `admin @ platform/role:admin`, and the relation type union would allow the wrong role id to grant create.

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

Remove application managers' type-level create fallback branches in the same
downstream upgrade. If a fallback survives, the over-grant survives.

## Documentation And PR Notes

Downstream upgrade notes should identify the removed local implementations:

- effective actor probes
- storage-specific relationship adapters
- role-tooling AST walks
- type-level create fallback branches

`docs/ARCHITECTURE.md` should document:

- `effective_actor(strict=False)` and the `(actor, is_unscoped)` tuple semantics
- relationship manager helpers and `resolve_subjects()`
- schema introspection helper stability, with AST nodes private
- const-backed relation injection in `check_new()`

`docs/ZED.md` must also document that `check_new()` injects const-backed virtual tuples for create preflight and rejects caller-supplied virtual tuples for synthetic const relations.

## Testing Strategy

Tests should be added or extended in:

- `tests/test_managers.py` for queryset `effective_actor()` default and strict behavior.
- `tests/test_mixin.py` for instance `effective_actor()` precedence, especially pinned actor over ambient sudo, and for `check_access()` using the same rule.
- Parametrized actor-resolution tests that assert `check_access()` and `effective_actor()` resolve consistently across pinned, ambient, per-object sudo, and ambient sudo combinations.
- relationship storage tests, ideally parametrized over `REBAC_LOCAL_BACKEND_STORAGE`, for filtering, ordering, `wire_values()`, and `resolve_subjects()`.
- new schema introspection tests covering refs, arrows, binops, built-ins, sub-permission refs, and parameterized role resource types.
- `tests/test_preflight.py` for const-backed overlay and the parent-specific deny regression.

The full verification target after implementation is the focused test set plus the repository's normal test command from `pyproject.toml` or existing Makefile.
