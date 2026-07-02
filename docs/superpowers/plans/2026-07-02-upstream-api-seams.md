# Upstream API Seams Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish upstream REBAC APIs that let Angee delete private actor probes, relationship storage sniffing, schema AST walking, and the create fallback over-grant.

**Architecture:** Add public observer APIs while keeping enforcement fail-closed, expose relationship helpers at the active model manager/queryset boundary, centralize schema introspection in `rebac.schema.introspection`, and make `check_new()` inject schema-owned const tuples while rejecting caller-supplied tuples for synthetic relations. All behavior is covered with failing tests before production edits.

**Tech Stack:** Python 3.14, Django 6.0 ORM, pytest-django, existing `rebac` LocalBackend/schema AST modules.

---

### Task 1: Correct And Verify Actor Resolution

**Files:**
- Modify: `src/rebac/managers.py`
- Modify: `src/rebac/mixins.py`
- Modify: `tests/test_managers.py`
- Modify: `tests/test_mixin.py`

- [ ] **Step 1: Write failing queryset tests**

Add tests to `tests/test_managers.py`:

```python
def test_queryset_effective_actor_observer_does_not_raise_without_actor() -> None:
    manager = RebacManager()
    manager.model = Post

    assert manager.get_queryset().effective_actor() == (None, False)


def test_queryset_effective_actor_strict_raises_without_actor() -> None:
    manager = RebacManager()
    manager.model = Post

    with pytest.raises(MissingActorError):
        manager.get_queryset().effective_actor(strict=True)


def test_queryset_pinned_actor_beats_ambient_sudo(alice) -> None:
    manager = RebacManager()
    manager.model = Post
    actor = SubjectRef.of("auth/user", str(alice.pk))

    with sudo(reason="test.ambient"):
        assert manager.with_actor(actor).effective_actor() == (actor, False)
```

- [ ] **Step 2: Write failing instance tests**

Add tests to `tests/test_mixin.py`:

```python
def test_instance_effective_actor_observer_does_not_raise_without_actor(post):
    with sudo(reason="test.load"):
        instance = type(post).objects.get(pk=post.pk)
    instance._rebac_actor = None

    assert instance.effective_actor() == (None, False)


def test_instance_pinned_actor_beats_ambient_sudo_for_check_access(alice, bob, post):
    _grant_owner(alice, post)
    with sudo(reason="test.load"):
        instance = type(post).objects.get(pk=post.pk)
    instance.with_actor(bob)

    with sudo(reason="ambient"):
        assert instance.effective_actor() == (SubjectRef.of("auth/user", str(bob.pk)), False)
        assert not instance.has_access("read")


@pytest.mark.parametrize("ambient_sudo", [False, True])
def test_instance_check_access_uses_effective_actor_rule(alice, bob, post, ambient_sudo):
    _grant_owner(alice, post)
    with sudo(reason="test.load"):
        instance = type(post).objects.get(pk=post.pk)
    instance.with_actor(bob)

    if ambient_sudo:
        with sudo(reason="ambient"):
            assert not instance.has_access("read")
    else:
        assert not instance.has_access("read")
```

- [ ] **Step 3: Run actor tests and verify RED**

Run: `uv run pytest tests/test_managers.py::test_queryset_effective_actor_observer_does_not_raise_without_actor tests/test_managers.py::test_queryset_effective_actor_strict_raises_without_actor tests/test_managers.py::test_queryset_pinned_actor_beats_ambient_sudo tests/test_mixin.py::test_instance_effective_actor_observer_does_not_raise_without_actor tests/test_mixin.py::test_instance_pinned_actor_beats_ambient_sudo_for_check_access tests/test_mixin.py::test_instance_check_access_uses_effective_actor_rule -q`

Expected: FAIL because `effective_actor` is not public yet, and the instance sudo crossover currently grants.

- [ ] **Step 4: Implement actor resolution**

In `RebacQuerySet`, add:

```python
def effective_actor(self, *, strict: bool = False) -> tuple[SubjectRef | None, bool]:
    if self._rebac_sudo_reason is not None:
        return (None, True)
    if self._rebac_actor is not None:
        return (self._rebac_actor, False)
    if _is_sudo_ambient():
        return (None, True)
    ambient = _current_actor()
    if ambient is not None:
        return (ambient, False)
    if app_settings.REBAC_STRICT_MODE:
        if strict:
            raise MissingActorError(
                f"Queryset on {self.model.__name__} resolved without an actor. "
                "Use .with_actor(actor), .as_user(user), .as_agent(agent, on_behalf_of=user), "
                "or .sudo(reason='...')."
            )
        return (None, False)
    return (None, True)
```

Change `_resolve_effective_actor()` to call `effective_actor(strict=True)` and update its docstring with the deprecation/removal note.

In `RebacMixin`, add the same public method with instance state. Change `check_access()` to call `self.effective_actor(strict=True)` after the no-resource-type shortcut, return `CheckResult.has(reason="unscoped")` when the second tuple element is true, and dispatch to the backend with the resolved actor otherwise.

- [ ] **Step 5: Run actor tests and verify GREEN**

Run the same command from Step 3.

Expected: PASS.

### Task 2: Add Relationship Query Helpers And Subject Resolution

**Files:**
- Modify: `src/rebac/models/relationship.py`
- Modify: `src/rebac/models/__init__.py`
- Modify: `src/rebac/relationships.py`
- Modify: `src/rebac/__init__.py`
- Modify: `tests/test_resource_registry.py`

- [ ] **Step 1: Write failing relationship helper tests**

Add tests to `tests/test_resource_registry.py`:

```python
@pytest.mark.django_db
@pytest.mark.parametrize("model_cls", [Relationship, RelationshipRegistry])
def test_relationship_helpers_filter_and_order_wire_rows(model_cls):
    model_cls.objects.create(resource_type="storage/file", resource_id="b", relation="viewer", subject_type="auth/user", subject_id="2")
    model_cls.objects.create(resource_type="storage/file", resource_id="a", relation="owner", subject_type="auth/user", subject_id="1")

    rows = list(model_cls.objects.for_resource("storage/file", "a").order_by_subject().wire_values())

    assert rows == [{
        "resource_type": "storage/file",
        "resource_id": "a",
        "relation": "owner",
        "subject_type": "auth/user",
        "subject_id": "1",
        "optional_subject_relation": "",
        "caveat_name": "",
    }]


@pytest.mark.django_db
@pytest.mark.parametrize("model_cls", [Relationship, RelationshipRegistry])
def test_for_subject_optional_relation_none_means_any_relation(model_cls):
    model_cls.objects.create(resource_type="storage/file", resource_id="a", relation="viewer", subject_type="auth/group", subject_id="eng", optional_subject_relation="member")
    model_cls.objects.create(resource_type="storage/file", resource_id="b", relation="viewer", subject_type="auth/group", subject_id="eng")

    any_rows = list(model_cls.objects.for_subject("auth/group", "eng", optional_relation=None).order_by_resource().wire_values())
    direct_rows = list(model_cls.objects.for_subject("auth/group", "eng", optional_relation="").wire_values())

    assert [row["resource_id"] for row in any_rows] == ["a", "b"]
    assert [row["resource_id"] for row in direct_rows] == ["b"]


@pytest.mark.django_db
def test_resolve_subjects_maps_registered_models_to_rows():
    from django.contrib.auth import get_user_model
    from rebac.relationships import resolve_subjects

    user = get_user_model().objects.create(username="subject")
    refs = [SubjectRef.of("auth/user", str(user.pk)), SubjectRef.of("missing/type", "1")]

    assert resolve_subjects(refs) == {SubjectRef.of("auth/user", str(user.pk)): user}
```

- [ ] **Step 2: Run relationship tests and verify RED**

Run: `uv run pytest tests/test_resource_registry.py::test_relationship_helpers_filter_and_order_wire_rows tests/test_resource_registry.py::test_for_subject_optional_relation_none_means_any_relation tests/test_resource_registry.py::test_resolve_subjects_maps_registered_models_to_rows -q`

Expected: FAIL because helper methods and `resolve_subjects` do not exist.

- [ ] **Step 3: Implement relationship helpers**

Add `RelationshipQuerySet` / `RelationshipManager` for the denormalized model with `for_resource`, `for_subject`, `order_by_resource`, `order_by_subject`, and `wire_values`.

Add the same public methods to `RelationshipRegistryQuerySet`, translating ordering/projection through `resource_fk` and `subject_fk`.

In `rebac.relationships`, add:

```python
def resolve_subjects(refs: Iterable[SubjectRef]) -> dict[SubjectRef, models.Model]:
    refs_by_type: dict[str, list[SubjectRef]] = {}
    for ref in refs:
        refs_by_type.setdefault(ref.subject_type, []).append(ref)
    resolved: dict[SubjectRef, models.Model] = {}
    for subject_type, refs_for_type in refs_by_type.items():
        model = model_for_resource_type(subject_type)
        if model is None:
            continue
        ids = {ref.subject_id for ref in refs_for_type}
        rows = model._base_manager.filter(**{f"{resource_id_attr(model)}__in": list(ids)})
        by_id = {model_resource_id(row): row for row in rows}
        for ref in refs_for_type:
            row = by_id.get(ref.subject_id)
            if row is not None:
                resolved[ref] = row
    return resolved
```

Export `RelationshipManager`, `resolve_subjects`, and keep the active-model API unchanged.

- [ ] **Step 4: Run relationship tests and verify GREEN**

Run the same command from Step 2.

Expected: PASS.

### Task 3: Add Persisted Schema Introspection

**Files:**
- Create: `src/rebac/schema/introspection.py`
- Modify: `src/rebac/schema/__init__.py`
- Modify: `src/rebac/roles.py`
- Create: `tests/test_schema_introspection.py`

- [ ] **Step 1: Write failing introspection tests**

Create `tests/test_schema_introspection.py` with tests for:

```python
SCHEMA_TEXT = """
definition auth/user {}

definition storage/role {
    relation member: auth/user
}

definition blog/folder {
    relation owner: auth/user
    permission read = owner
}

definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user | storage/role:object_viewer#member
    relation folder: blog/folder
    relation admin: storage/role // rebac:const=admin

    permission base = owner + viewer
    permission read = base + folder->read + authenticated
    permission manage = admin->member
}
"""


def test_permission_sources_collects_refs_arrows_builtins_and_subpermissions():
    schema = parse_zed(SCHEMA_TEXT)
    sources = permission_sources(schema, "blog/post", "read")
    assert sources.direct_relations == frozenset({"owner", "viewer"})
    assert sources.arrows == frozenset({("folder", "read")})
    assert sources.builtins == frozenset({"authenticated"})
    assert sources.subpermissions == frozenset({"base"})


def test_permissions_reaching_relation_uses_subpermissions():
    schema = parse_zed(SCHEMA_TEXT)
    assert permissions_reaching_relation(schema, "blog/post", "owner") == frozenset({"base", "read"})


def test_roles_reaching_is_parameterized_by_role_resource_type():
    schema = parse_zed(SCHEMA_TEXT)
    assert roles_reaching("blog/post", "read", role_resource_type="storage/role", schema=schema) == frozenset({ObjectRef("storage/role", "object_viewer")})
    assert roles_reaching("blog/post", "manage", role_resource_type="storage/role", schema=schema) == frozenset({ObjectRef("storage/role", "admin")})
```

- [ ] **Step 2: Run introspection tests and verify RED**

Run: `uv run pytest tests/test_schema_introspection.py -q`

Expected: FAIL because `rebac.schema.introspection` and `roles_reaching` do not exist.

- [ ] **Step 3: Implement introspection**

Create `PermissionSources` and traversal functions in `src/rebac/schema/introspection.py`. Export them from `rebac.schema.__init__`.

Add `roles_reaching()` to `src/rebac/roles.py`, defaulting to `backend().schema()` when `schema is None`, and collecting `AllowedSubject.id` roles plus const-backed role targets.

- [ ] **Step 4: Run introspection tests and verify GREEN**

Run: `uv run pytest tests/test_schema_introspection.py -q`

Expected: PASS.

### Task 4: Fix `check_new` Const Overlay And Validation

**Files:**
- Modify: `src/rebac/preflight.py`
- Modify: `tests/test_preflight.py`

- [ ] **Step 1: Write failing preflight tests**

Add tests to `tests/test_preflight.py`:

```python
CONST_CREATE_SCHEMA_TEXT = """
definition auth/user {}

definition auth/role {
    relation member: auth/user
}

definition blog/vault {
    relation owner: auth/user
    permission create = owner
    permission write = owner
}

definition blog/post {
    relation vault: blog/vault
    relation admin: auth/role // rebac:const=superadmin

    permission create_admin = admin->member
    permission create = vault->create
}
"""


def _const_backend():
    b = LocalBackend()
    b.set_schema(parse_zed(CONST_CREATE_SCHEMA_TEXT))
    return b


def test_create_via_const_backed_relation_injects_virtual_tuple(backend):
    b = _const_backend()
    b.write_relationships([
        RelationshipTuple(
            resource=ObjectRef("auth/role", "superadmin"),
            relation="member",
            subject=_user("alice"),
        )
    ])

    result = check_new(
        subject=_user("alice"),
        action="create_admin",
        resource_type="blog/post",
        backend=b,
    )

    assert result.allowed


def test_create_via_const_backed_relation_denies_non_member(backend):
    b = _const_backend()

    result = check_new(
        subject=_user("bob"),
        action="create_admin",
        resource_type="blog/post",
        backend=b,
    )

    assert not result.allowed


def test_create_rejects_caller_supplied_const_relation_overlay(backend):
    b = _const_backend()
    with pytest.raises(SchemaError, match="const-backed"):
        check_new(
            subject=_user("alice"),
            action="create_admin",
            resource_type="blog/post",
            relationships={"admin": [SubjectRef.of("auth/role", "editor")]},
            backend=b,
        )


def test_create_via_parent_arrow_denies_when_actor_can_create_elsewhere(backend):
    b = _const_backend()
    b.write_relationships([
        RelationshipTuple(
            resource=ObjectRef("blog/vault", "allowed"),
            relation="owner",
            subject=_user("alice"),
        )
    ])

    result = check_new(
        subject=_user("alice"),
        action="create",
        resource_type="blog/post",
        relationships={"vault": [SubjectRef.of("blog/vault", "denied")]},
        backend=b,
    )

    assert not result.allowed
```

- [ ] **Step 2: Run preflight tests and verify RED**

Run: `uv run pytest tests/test_preflight.py::test_create_via_const_backed_relation_injects_virtual_tuple tests/test_preflight.py::test_create_via_const_backed_relation_denies_non_member tests/test_preflight.py::test_create_rejects_caller_supplied_const_relation_overlay tests/test_preflight.py::test_create_via_parent_arrow_denies_when_actor_can_create_elsewhere -q`

Expected: FAIL because const overlay injection/rejection is not implemented.

- [ ] **Step 3: Implement const overlay merge**

In `src/rebac/preflight.py`, import `ConstBinding` and `SchemaError`. Add a helper that copies caller relationships, rejects non-empty entries for const-backed relation names, and appends `SubjectRef.of(allowed.type, backing.target_id)` for each schema const relation.

Call this helper after loading the definition and before building the `WalkContext`.

- [ ] **Step 4: Run preflight tests and verify GREEN**

Run the same command from Step 2.

Expected: PASS.

### Task 5: Document Public API And Author-Facing Semantics

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/ZED.md`

- [ ] **Step 1: Update architecture docs**

Document:

- `effective_actor(strict=False)` and `(actor, is_unscoped)`.
- explicit pinned actor beating ambient sudo.
- relationship helper methods and `resolve_subjects()`.
- schema introspection helper-level stability and private AST nodes.
- `check_new()` const tuple injection and rejection.

- [ ] **Step 2: Update ZED docs**

In the const-backed relation section, document create preflight behavior:

- const-backed relations are injected as virtual tuples for `check_new()`.
- callers must not supply virtual tuples for const-backed relation names.

- [ ] **Step 3: Run doc-adjacent focused tests**

Run: `uv run pytest tests/test_create_gate.py tests/test_const_backed_relations.py tests/test_preflight.py -q`

Expected: PASS.

### Task 6: Final Verification And Commit

**Files:**
- All modified files.

- [ ] **Step 1: Run focused suite**

Run: `uv run pytest tests/test_managers.py tests/test_mixin.py tests/test_resource_registry.py tests/test_schema_introspection.py tests/test_preflight.py -q`

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest -q`

Expected: PASS.

- [ ] **Step 3: Run lint**

Run: `uv run ruff check .`

Expected: PASS.

- [ ] **Step 4: Commit**

Run:

```bash
git add docs/superpowers/specs/2026-07-02-upstream-api-seams-design.md docs/superpowers/plans/2026-07-02-upstream-api-seams.md src/rebac tests docs
git commit -m "feat: publish upstream rebac seam APIs"
```

Expected: commit succeeds on `codex/upstream-api-seams`.
