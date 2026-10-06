# Proposal 0016: relations backed by a GenericForeignKey

**Status:** implemented in 0.26.0 (2026-10-06), approved as drafted with one
narrowing found in implementation: the target's identity must be its primary
key (§ 3). Requested by a consumer that hand-rolls the same check in three
places. The behaviour is specified in `docs/ARCHITECTURE.md` § Relations
backed by a GenericForeignKey.

## Problem

A polymorphic edge model joins one row of its own kind to a row of any model
through a `GenericForeignKey`: a file attached to a record, a knowledge page
bound to a record, a tag assigned to a record. The edge's permissions follow
its target: creating or deleting the edge needs `write` on the target, and
listing edges needs `read` on it.

The schema cannot say that today. A field backing must end in a forward
foreign key, a one-to-one, a many-to-many or a reverse relation, and a field
backed relation admits exactly one subject type. A `GenericForeignKey` is
none of these, so the edge's permissions can only follow its other end (the
file, the tag), and the consumer checks the target by hand: it resolves the
actor, honours sudo, finds the target's resource type, canonicalises the
target across multi-table inheritance and calls `check_access`. The consumer
has three copies; one of them checks `read` where the other two check
`write`.

What already works: a field backing may traverse a `GenericRelation` from the
target's side (`rebac:field=bindings__reader`), and the content-type and
object-id columns it reads are watched by the write gates
(`tests/test_generic_path_backing.py`).

## Rule

A field backing may end in a `GenericForeignKey` of the declaring model. The
relation keeps exactly one subject type `T`: it holds the edge rows whose
content type is `T`'s model and names the target by primary key. An edge that
can point at several types declares one relation per type. Every permission
on the edge is an ordinary expression over those relations, and every gate is
the one that already applies to the edge model as a resource.

## Design

### 1. Declaration

```zed
definition tags/tag_assignment {
    relation tag:   tags/tag      // rebac:field=tag
    relation party: parties/party // rebac:field=target
    relation file:  storage/file  // rebac:field=target

    permission create = (party->write + file->write)
    permission delete = (party->write + file->write)
    permission read   = (tag->read & (party->read + file->read))
}
```

- One subject type per relation, no subject relation (`T#member`), no
  wildcard. Filters on the edge model's own columns are allowed, as for any
  field backing.
- Several relations may name the same `GenericForeignKey`, one per type.
- An app that makes its records attachable contributes its relation and its
  arms to the edge's definition. A consumer that composes schemas from
  several apps appends relations and unions arms; it does not need to add
  types to an existing relation.
- An edge whose target is of a type no relation names, or of a model with no
  resource type, has no arm in `create`: it is refused under an actor. Nothing
  passes by default.

### 2. Content type and the canonical target

A row's **canonical model** is its concrete model (a proxy is unwrapped), then
the topmost concrete multi-table ancestor that has a resource type. Rows of an
MTI child and of its typed parent share one primary key, so an edge to either
is stored once, under the ancestor's content type. A row with no typed
ancestor has no canonical model.

- A relation over `T` matches the content type of `T`'s model. A system check
  refuses a relation whose type's model is not its own canonical model (it
  has a typed concrete MTI ancestor): edges stored canonically would never
  match it.
- `rebac.generic_target(row) -> GenericTarget` returns `content_type`,
  `object_id` (the primary key) and `ref` (the `ObjectRef` at the canonical
  model's type and identity). It raises `ValueError` for a row with no
  canonical model. `GenericTarget.lookups(model, name)` returns the filter
  keywords for the named `GenericForeignKey` of an edge model
  (`{"content_type": ..., "object_id": ...}` under that field's own column
  names).
- An edge that stores a non-canonical content type matches no relation, so
  its creation is refused under an actor and it grants nothing.

### 3. Reads

At an edge row, the relation over `T` with target permission `p` is

```
content_type_id = <ct of T's model>
AND object_id IN (SELECT pk FROM <T's model> WHERE holds(T#p) at the row's identity)
```

The target's identity must be its primary key: the object id stores a
primary key, nothing joins it to another column, and every reader of a field
backing (the residual evaluator, enumeration, the walker) reads the stored
value as the target's identity. A relation over a type with another identity
is refused (`rebac.E009`), as is an object id field whose type cannot hold
the primary key. Nothing constrains `object_id`, so decided keys are always
read through the target's rows (ARCHITECTURE § Decided rows, item 5).

- A scope over the edge model is the compiled predicate, as for any resource:
  the edges of one record are
  `Edge.objects.with_actor(a).filter(**generic_target(record).lookups(Edge, "target"))`,
  one statement, no check per row.
- Point checks, `accessible()`, `lookup_subjects()` (the edge's target is a
  candidate) and `check_new()` (the candidate's content type and object id
  become a proposed relationship) read the same columns.
- An edge whose target row is gone names no row and grants nothing. Edges are
  not deleted with their target unless the target model declares a
  `GenericRelation` to them; that stays the consumer's choice.

### 4. Writes

- **Create** needs `create` on the edge as proposed: the candidate's content
  type and object id become a proposed relationship of the matching relation,
  read with one lookup of the target's identity when it is not the primary
  key. Instance saves, `create()`, `bulk_create()` and related-manager adds
  use the existing create gate.
- **Delete** needs `delete` on the stored edge, through the existing instance
  and queryset gates.
- **Moving an edge** (changing its content type or object id) changes which
  relation the row backs, and the existing rule (invariant 5d) checks only
  the edge as stored. Under an actor such an update is refused, for instance
  saves and queryset updates alike; an edge is moved by deleting it and
  creating a new one. Checking the new target on a queryset update would mean
  evaluating the assigned expressions in Python, which the project does not
  add (AGENTS.md, invariant 5d).
- Tuple writes to these relations raise `SchemaError`, as for every backed
  relation.
- Bypass is unchanged: instance sudo, queryset sudo, ambient sudo and
  `system_context` lift the edge's gates as they lift any resource's.

### 5. A check as the effective actor

`rebac.check_permission(action, resource, *, actor=None, context=None) ->
CheckResult` is the function form of `@require_permission`: an explicit actor
first, then ambient sudo (answers `HAS`), then `current_actor()`; with none,
`NoActorResolvedError`. `resource` is an `ObjectRef` or a model instance.
`@require_permission` calls it. Instances keep `RebacMixin.check_access`.

### 6. SpiceDB

Each relation is an ordinary relation with one subject type. `build-zed` omits
the backing as for every field backing. A SpiceDB projection of an edge row is
one tuple, `tags/tag_assignment:<id>#party@parties/party:<id>`. Nothing here
exists only in `LocalBackend`.

## What a consumer changes

1. Declare one relation per target type on the edge's definition, backed by
   the `GenericForeignKey`, and write the edge's `create`, `delete` and
   `read` as expressions over them.
2. Store targets with `rebac.generic_target(row)`; a helper that computes the
   canonical target can go, or stay as an alias.
3. Remove the hand-written target checks and the `system_context` around
   edge inserts and deletes: create and delete edges through the scoped
   manager under the actor.
4. List a record's edges with the scoped edge queryset filtered by
   `generic_target(record).lookups(...)`.
5. Move an edge by deleting and creating it.

## Tests

- Schema: a backing that ends in a `GenericForeignKey` is accepted with one
  subject type and refused with a subject relation, a wildcard or several
  types; the canonical-model check fires for an MTI child's type.
- Reads, against the reference model and the walker: scope, point check,
  `accessible()`, `lookup_subjects()`, decided and inline forms; a target
  stored under its MTI ancestor; a proxy target; a target whose identity is
  not its primary key; a dangling edge.
- Writes: create allowed and denied per type; an untyped or undeclared target
  refused; `bulk_create`; instance and queryset delete; an update of the
  target columns refused under an actor and allowed under sudo; a pinned
  actor beating the ambient one.
- `check_permission`: explicit actor, ambient sudo, no actor.

## Decisions for the owner

1. **Shape.** One relation per target type over a `GenericForeignKey` (this
   proposal), or a declaration on the edge model outside the schema (for
   example `GenericEdge("target", write="write")`). The second accepts any
   typed target without listing types, but cannot be written in `.zed`, has
   no SpiceDB form, and adds a write interception path of the kind proposal
   0013 removes.
2. **Moving an edge:** refused under an actor (this proposal), or checked as
   a delete and a create, which a queryset update cannot do without Python
   emulation.
3. **Names:** `rebac.generic_target` / `GenericTarget` and
   `rebac.check_permission`.

## Not in scope

- A relation over "any type": SpiceDB has no such relation.
- Deleting edges with their target.
- Merging subject types into one relation from several schema fragments.

After approval: ARCHITECTURE § Field-backed structural relations and § Public
API surface, a ZED.md pattern, CHANGELOG.
