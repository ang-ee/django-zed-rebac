# Membership and identity lifecycle

Status: accepted for implementation.

## Problem

Relationship-backed membership applies to roles, groups, kinds, and any other
schema container whose canonical relation is `member`. The library currently
names its generic membership operations after roles, and denormalized storage
does not remove tuples when a registered Django object disappears as a subject.
The Django permissions mixin also duplicates the superuser bypass already owned
by `RebacBackend`, defeating `REBAC_SUPERUSER_BYPASS=False`.

## Contract

`rebac.memberships` owns direct `member` tuple creation, exact revocation, and
direct enumeration. It delegates persistence to the existing relationship API
and creates no catalogue or membership store. Caveated revocation identifies
the exact tuple by caveat name; it never broad-deletes other memberships between
the same subject and container. `rebac.roles` retains its historical API as a
narrow compatibility layer and continues to own role parsing and hierarchy.

Deleting a registered Django resource removes every tuple in which its object
reference appears, as resource or subject. Registry storage keeps using its
native `RebacResource` foreign-key cascade. Denormalized storage performs the
equivalent two scoped relationship deletions from the existing post-delete
lifecycle.

Schema introspection exposes all object references named literally by schema:
const-backed relation targets and fixed-id allowed subjects. It never derives
runtime ids or claims that a named object is effective for a permission.

`RebacPermissionsMixin` always follows Django's configured backend chain.
`RebacBackend` remains the single owner of the optional permission-level
superuser bypass; `ActorMiddleware` owns the queryset bypass. Consumer login
backends and authoritative-denial policy remain consumer concerns.
