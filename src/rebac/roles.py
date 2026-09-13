"""Role-as-namespace helpers — the GCP-style role-grant convention.

This module is a **convention layer** composed on :mod:`rebac.memberships`
(direct ``member`` tuples) and :mod:`rebac.relationships`. It does not
introduce a new storage type, change the engine, or add schema syntax. It
packages the "role-as-resource" pattern — a standard SpiceDB recipe — into
ergonomic helpers that add role-spec parsing and role hierarchy on top of the
generic membership operations.

The convention
==============

Predefined roles per package / addon live as objects in a
``<namespace>/role`` resource type, where:

- ``namespace`` is the package or addon name (``storage``, ``knowledge``,
  ``agents``, …).
- ``object_id`` is the role name (``object_viewer``, ``object_admin``,
  ``vault_editor``, …).
- The single relation used for membership is ``member`` (constant
  :data:`ROLE_RELATION`).

Schema
------

Every addon ships a single ``definition <namespace>/role`` block in its
``rebac.zed``::

    definition storage/role {
        relation member: auth/user | auth/group#member
    }

Resources reference role memberships via the ``#member`` subject-set::

    definition storage/file {
        relation viewer: auth/user
                       | auth/group#member
                       | storage/role:object_viewer#member
                       | storage/role:object_admin#member

        permission read = viewer
    }

Granting Alice the ``object_viewer`` role is one row::

    >>> from rebac.roles import grant
    >>> grant(actor=alice, role="storage/role:object_viewer")

A pinned-id ``#member`` allowed subject is a *grantable* subject, not an
implicit grant: the role grant opens ``read`` only on files that carry a
per-file ``viewer @ storage/role:object_viewer#member`` tuple linking them
to the role. Once that linking tuple exists, membership changes reach every
linked file with no further per-file rows — but the role grant alone opens
nothing, and the local backend never synthesises the linking tuple. For a
role that must cover **every** row of a type with no per-resource tuple,
declare a const-backed relation
(``relation admin: <ns>/role // rebac:const=<id>`` + ``admin->member``);
that is the tuple-free canon for role reach.

Role hierarchy
==============

Two stock-SpiceDB recipes, both supported without extra machinery:

**Permission composition** (per-resource) — wider roles appear in the
narrower role's permission expression::

    permission read   = viewer + editor + admin
    permission write  = editor + admin
    permission delete = admin

**Relation traversal** (per-role) — wider roles are members of the
narrower role via the ``#member`` subject-set::

    definition storage/role {
        relation member: auth/user
                       | auth/group#member
                       | storage/role:object_admin#member   // admin includes viewer
    }

Pick one style per addon. Composition is more explicit; traversal is
DRYer for deep hierarchies.

System / framework roles
========================

Bypass paths for framework jobs (migrations, asset seed loaders) use
:func:`rebac.actors.sudo` — they are not modelled as roles. The role
helpers here are exclusively for **actor-grantable** roles (the GCP
``roles/<service>.<role>`` shape).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, cast

from .actors import ActorLike
from .memberships import MEMBER_RELATION
from .types import ObjectRef, RelationshipTuple, SubjectRef

if TYPE_CHECKING:  # pragma: no cover
    from .models import RelationshipRow
    from .schema.ast import Schema


ROLE_RELATION = MEMBER_RELATION
"""The single relation used for role membership. Convention, not configurable.

If a consumer needs a different relation name for some bespoke role-shape,
they should call :class:`Relationship` CRUD directly rather than this
module — the helpers exist specifically to enforce the one-relation rule
across the ecosystem.
"""

ROLE_INCLUDES_RELATION = "includes"
"""The relation used by :func:`imply` to wire one role's effective members
into another role's effective_member permission.

Convention paired with this relation: addons that want runtime-editable role
hierarchy declare their roles as::

    definition <namespace>/role {
        relation member:   auth/user | auth/group#member | platform/role:admin#member
        relation includes: <namespace>/role

        permission effective_member = member + includes->effective_member
    }

…and resources hold a direct role relation, then arrow to
``effective_member``. :func:`imply` writes the ``includes`` tuple that wires
one role object's computed membership into another's without placing a
permission name in a relationship subject.

Addons that don't need runtime-editable hierarchy can skip the
``includes`` relation entirely and use per-resource permission composition
(``permission read = viewer + editor + admin``) instead.
"""

ROLE_EFFECTIVE_MEMBER = "effective_member"
"""The permission name produced by the ``includes`` pattern. See
:data:`ROLE_INCLUDES_RELATION` for the schema convention.
"""


def _parse_role(role: str | ObjectRef) -> ObjectRef:
    """Coerce ``role`` to an :class:`ObjectRef`.

    Accepted forms:

    - :class:`ObjectRef` instance (passed through).
    - ``"<namespace>/role:<name>"`` — full role spec, e.g.
      ``"storage/role:object_viewer"``.

    Raises :class:`ValueError` for any other shape so misspellings fail
    fast at the grant site rather than producing orphan rows.
    """
    if isinstance(role, ObjectRef):
        return role
    if ":" not in role:
        raise ValueError(
            f"Invalid role spec {role!r}; expected "
            f"'<namespace>/role:<role_name>' (e.g. 'storage/role:object_viewer')"
        )
    rtype, rid = role.split(":", 1)
    if not rtype or not rid:
        raise ValueError(
            f"Invalid role spec {role!r}; both '<namespace>/role' and "
            f"'<role_name>' must be non-empty"
        )
    return ObjectRef(rtype, rid)


def grant(*, actor: ActorLike, role: str | ObjectRef) -> RelationshipRow:
    """Grant ``actor`` membership in ``role``.

    ``role`` is either an :class:`ObjectRef` or a
    ``"<namespace>/role:<name>"`` string. Idempotent — re-granting an
    existing membership returns the existing row.

    The membership row is::

        Relationship(
            resource_type=<role.resource_type>,
            resource_id=<role.resource_id>,
            relation="member",
            subject_type=<actor.subject_type>,
            subject_id=<actor.subject_id>,
            optional_subject_relation=<actor.optional_relation>,
        )

    Returns the :class:`Relationship` row (newly created or pre-existing).
    """
    from .memberships import grant as grant_membership

    return grant_membership(subject=actor, container=_parse_role(role))


def revoke(*, actor: ActorLike, role: str | ObjectRef) -> int:
    """Revoke ``actor``'s membership in ``role``.

    Returns the number of rows deleted (0 if no membership existed, 1
    otherwise — the unique constraint on :class:`Relationship` guarantees
    at most one matching row).
    """
    from .memberships import revoke as revoke_membership

    return revoke_membership(subject=actor, container=_parse_role(role))


def roles_of(actor: ActorLike) -> Iterator[ObjectRef]:
    """Yield the role objects ``actor`` is a **direct** member of.

    Detects role objects by the ``<namespace>/role`` resource-type
    convention. Does NOT walk role hierarchy — for transitive membership,
    use the engine (``has_access`` / ``accessible``), which traverses
    ``role:editor#member`` subject-sets at check time.
    """
    from .memberships import containers_of

    # The convention filter stays in SQL; ``containers_of`` owns the row query.
    yield from containers_of(actor, resource_type__endswith="/role")


def members_of(role: str | ObjectRef) -> Iterator[SubjectRef]:
    """Yield the subjects directly granted ``role``.

    Direct grants only; does NOT walk role hierarchy or subject-set
    traversal. For "who *effectively* holds this role" (including
    transitive members via the ``#member`` subject-set chain),
    enumerate ``accessible()`` on a resource that references the role
    in its permission expression.
    """
    from .memberships import members_of as membership_members_of

    yield from membership_members_of(_parse_role(role))


def imply(*, parent: str | ObjectRef, child: str | ObjectRef) -> RelationshipRow:
    """Make ``child`` role's effective members also count as ``parent`` role's members.

    Requires both role definitions to use the ``includes`` /
    ``effective_member`` pattern (see :data:`ROLE_INCLUDES_RELATION`).
    Resources whose permissions arrow through ``parent`` to
    ``effective_member`` will then resolve grants of ``child`` as if they were
    ``parent`` grants.

    The membership row written is::

        Relationship(
            resource_type=<parent.resource_type>,
            resource_id=<parent.resource_id>,
            relation="includes",
            subject_type=<child.resource_type>,
            subject_id=<child.resource_id>,
            optional_subject_relation="",
        )

    Idempotent — re-implying an existing edge returns the existing row.
    Returns the :class:`Relationship` row (newly created or pre-existing).

    Example::

        from rebac.roles import imply
        imply(
            parent="storage/role:object_editor",
            child="storage/role:object_admin",
        )
        # Now any member of storage/role:object_admin is also an
        # effective member of storage/role:object_editor.
    """
    from django.db import transaction

    from .models import active_relationship_model
    from .relationships import write_relationships

    Relationship = active_relationship_model()

    parent_ref = _parse_role(parent)
    child_ref = _parse_role(child)
    tuple_ = RelationshipTuple(
        resource=parent_ref,
        relation=ROLE_INCLUDES_RELATION,
        subject=SubjectRef(child_ref),
    )
    # Wrap write + read-back: same DoesNotExist race as ``grant``.
    with transaction.atomic():
        write_relationships([tuple_])
        row = Relationship.objects.get(
            resource_type=parent_ref.resource_type,
            resource_id=parent_ref.resource_id,
            relation=ROLE_INCLUDES_RELATION,
            subject_type=child_ref.resource_type,
            subject_id=child_ref.resource_id,
            optional_subject_relation="",
            caveat_name="",
        )
        return cast("RelationshipRow", row)


def unimply(*, parent: str | ObjectRef, child: str | ObjectRef) -> int:
    """Remove the direct ``child → parent#includes`` implication edge.

    Returns the number of rows deleted (0 or 1).
    """
    from django.db import transaction

    from .models import active_relationship_model
    from .relationships import delete_relationship

    Relationship = active_relationship_model()

    parent_ref = _parse_role(parent)
    child_ref = _parse_role(child)
    tuple_ = RelationshipTuple(
        resource=parent_ref,
        relation=ROLE_INCLUDES_RELATION,
        subject=SubjectRef(child_ref),
    )
    # Wrap presence-check + delete: same TOCTOU as ``revoke``.
    with transaction.atomic():
        exists = Relationship.objects.filter(
            resource_type=parent_ref.resource_type,
            resource_id=parent_ref.resource_id,
            relation=ROLE_INCLUDES_RELATION,
            subject_type=child_ref.resource_type,
            subject_id=child_ref.resource_id,
            optional_subject_relation="",
            caveat_name="",
        ).exists()
        delete_relationship(tuple_)
    return 1 if exists else 0


def implies_of(role: str | ObjectRef) -> Iterator[ObjectRef]:
    """Yield roles that ``role`` directly implies (one hop).

    "X implies Y" means members of X are also effective members of Y.
    Looks up rows where ``role`` is the *child* and yields the *parents*.

    Direct edges only; the engine handles transitive closure at check
    time via the ``effective_member`` permission expression.
    """
    from .models import active_relationship_model

    Relationship = active_relationship_model()

    role_ref = _parse_role(role)
    rows = Relationship.objects.filter(
        relation=ROLE_INCLUDES_RELATION,
        subject_type=role_ref.resource_type,
        subject_id=role_ref.resource_id,
        optional_subject_relation="",
    )
    for row in rows:
        yield ObjectRef(row.resource_type, row.resource_id)


def implied_by_of(role: str | ObjectRef) -> Iterator[ObjectRef]:
    """Yield roles that directly imply ``role`` (one hop).

    Inverse of :func:`implies_of`. Looks up rows where ``role`` is the
    *parent* and yields the *children*.
    """
    from .models import active_relationship_model

    Relationship = active_relationship_model()

    role_ref = _parse_role(role)
    rows = Relationship.objects.filter(
        resource_type=role_ref.resource_type,
        resource_id=role_ref.resource_id,
        relation=ROLE_INCLUDES_RELATION,
        optional_subject_relation="",
    )
    for row in rows:
        yield ObjectRef(row.subject_type, row.subject_id)


def roles_reaching(
    resource_type: str,
    permission: str,
    *,
    role_resource_type: str,
    schema: Schema | None = None,
) -> frozenset[ObjectRef]:
    """Return statically named role objects that can reach a permission.

    The helper reads the effective schema through ``backend().schema()`` when
    ``schema`` is omitted. It only returns roles with a concrete object id from
    the schema, of two shapes:

    - **const-backed role relations** — ``relation admin: storage/role //
      rebac:const=admin`` reached through ``admin->member``: tuple-free role
      reach, the canon; and
    - **specific-id allowed subjects** — ``storage/role:object_viewer#member``:
      *declared* reachers that resolve only once a per-resource linking tuple is
      written (the local backend never synthesises one).

    So the result names the roles a permission *can* be reached by, not the
    roles that reach it tuple-free.
    """
    from .backends import backend
    from .schema.introspection import permission_object_sources

    return permission_object_sources(
        schema if schema is not None else backend().schema(),
        resource_type,
        permission,
        object_type=role_resource_type,
    )


__all__ = [
    "ROLE_EFFECTIVE_MEMBER",
    "ROLE_INCLUDES_RELATION",
    "ROLE_RELATION",
    "grant",
    "implied_by_of",
    "implies_of",
    "imply",
    "members_of",
    "revoke",
    "roles_of",
    "roles_reaching",
    "unimply",
]
