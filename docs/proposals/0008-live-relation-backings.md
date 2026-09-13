# Proposal 0008: live set and attribute relation backings

## Problem

Field-backed relations currently support one forward foreign key. Django also
stores authorization structure in set-valued relations and ordinary target
attributes. Mirroring those facts into relationship rows creates two writers
and drift.

## Declarations

The existing simple field spelling remains valid:

```zed
relation folder: storage/folder // rebac:field=folder
```

An options-bearing field binding uses a JSON object:

```zed
relation member: auth/user // rebac:field={"path":"members","filters":{"members__is_active":true}}
```

`path` is a Django relation lookup path from the definition's model to the
single allowed subject model. It may traverse forward, reverse, or many-to-many
relations. `filters` is an optional object of Django lookup names to JSON scalar
values, applied to the source model. Through-row and target predicates must
share a single filter operation so they bind to the same membership row.

An attribute binding derives members of a virtual container from the single
allowed subject model:

```zed
definition accounts/kind {
    relation member: auth/user // rebac:attribute={"field":"kind"}
    relation active_member: auth/user // rebac:attribute={"field":"kind","filters":{"is_active":true}}
}

definition platform/role {
    relation member: auth/user // rebac:attribute={"field":"is_superuser","resource":"admin","value":true}
}
```

Without `resource` and `value`, the target field equals the current virtual
resource id. `resource` and `value` must be supplied together; they restrict the
binding to that one container id and compare the field to the explicit JSON
scalar. Other IDs keep their stored membership. Filters further constrain
target rows. Dynamic container IDs are accepted only in the field's canonical
Python spelling, so boolean or integer aliases cannot resolve to a differently
spelled resource during reverse lookup.

## Ownership and validation

Bindings resolve through Django `_meta` and `_base_manager`; Django fields and
lookups remain the source of truth. Each backed relation has exactly one
concrete allowed subject type and carries no wildcard, subject set, caveat, or
expiration. Field paths must terminate at that subject model. Attribute fields
and filters must resolve on that model. Invalid JSON, options, paths, fields,
lookups, or value shapes fail schema validation and Django system checks.

Model identities are queryable scalar Django fields, including virtual fields
whose public value encodes an existing primary key. The identity need not own
a database column. Django owns lookup preparation and projected-value
conversion; REBAC never substitutes a raw primary key for a public graph ID.
Native model-to-model SQL correlations use the underlying columns. Hops that
require a wire-ID conversion unavailable in SQL retain evaluator fallback.

All direct checks, arrows, resource and subject lookup, eager enumeration, and
lazy local queryset scope read the same resolved backing. Reads honor the
queryset database alias. Tuple writes and deletes targeting the live container
raise `SchemaError`; the Django relation or attribute is the only writer.

## Backend limits

This proposal implements live resolution for `LocalBackend`. The directive is
preserved in local schema serialization and omitted from SpiceDB schema text.
Remote SpiceDB projection remains a separate future capability, and its burden
is larger than the forward-FK case: a filtered or set-valued path projects one
edge per distinct `(source, target)` pair whose through rows satisfy the
filters; a dynamic attribute container projects one edge per qualifying subject
row into the container named by its column value; a fixed container projects
one edge per subject whose column matches the declared value. ARCHITECTURE.md
carries the consolidated per-kind table.

Decision caching is declined only for resource types that can reach a live
backing (conservative schema reachability), so unrelated types keep caching.

## Compatibility

Stored relations, const backings, and simple forward-FK field backings are
unchanged. The unimplemented `REBAC_SYNC_DJANGO_GROUPS` setting is removed.
Django-owned memberships use field backing; tuple-owned memberships use the
native membership API.
