"""Role-as-namespace helpers — the GCP-style role-grant convention.

This module is a **convention layer** on top of :mod:`rebac.relationships`.
It does not introduce a new storage type, change the engine, or add
schema syntax. It packages the "role-as-resource" pattern — a standard
SpiceDB recipe — into ergonomic helpers so every consumer doesn't
hand-roll the same four CRUD wrappers around :class:`Relationship`.

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

…and every ``storage/file`` then evaluates ``read`` against the new
grant. No per-file Relationship rows needed.

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
from typing import TYPE_CHECKING

from .actors import ActorLike, to_subject_ref
from .types import ObjectRef, SubjectRef

if TYPE_CHECKING:  # pragma: no cover
    from .models import Relationship


ROLE_RELATION = "member"
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
        relation member:   auth/user | auth/group#member | angee/role:admin#member
        relation includes: <namespace>/role#effective_member

        permission effective_member = member + includes
    }

…and resources reference ``<namespace>/role:<name>#effective_member``
instead of ``#member``. :func:`imply` then writes the ``includes`` tuple
that wires one role's effective_member into another's.

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


def grant(*, actor: ActorLike, role: str | ObjectRef) -> Relationship:
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
    from .models import Relationship

    actor_ref = to_subject_ref(actor)
    role_ref = _parse_role(role)
    row, _ = Relationship.objects.get_or_create(
        resource_type=role_ref.resource_type,
        resource_id=role_ref.resource_id,
        relation=ROLE_RELATION,
        subject_type=actor_ref.subject_type,
        subject_id=actor_ref.subject_id,
        optional_subject_relation=actor_ref.optional_relation,
        caveat_name="",
    )
    return row


def revoke(*, actor: ActorLike, role: str | ObjectRef) -> int:
    """Revoke ``actor``'s membership in ``role``.

    Returns the number of rows deleted (0 if no membership existed, 1
    otherwise — the unique constraint on :class:`Relationship` guarantees
    at most one matching row).
    """
    from .models import Relationship

    actor_ref = to_subject_ref(actor)
    role_ref = _parse_role(role)
    deleted, _ = Relationship.objects.filter(
        resource_type=role_ref.resource_type,
        resource_id=role_ref.resource_id,
        relation=ROLE_RELATION,
        subject_type=actor_ref.subject_type,
        subject_id=actor_ref.subject_id,
        optional_subject_relation=actor_ref.optional_relation,
    ).delete()
    return deleted


def roles_of(actor: ActorLike) -> Iterator[ObjectRef]:
    """Yield the role objects ``actor`` is a **direct** member of.

    Detects role objects by the ``<namespace>/role`` resource-type
    convention. Does NOT walk role hierarchy — for transitive membership,
    use the engine (``has_access`` / ``accessible``), which traverses
    ``role:editor#member`` subject-sets at check time.
    """
    from .models import Relationship

    actor_ref = to_subject_ref(actor)
    rows = Relationship.objects.filter(
        relation=ROLE_RELATION,
        subject_type=actor_ref.subject_type,
        subject_id=actor_ref.subject_id,
        optional_subject_relation=actor_ref.optional_relation,
        resource_type__endswith="/role",
    ).values_list("resource_type", "resource_id")
    for rt, rid in rows:
        yield ObjectRef(rt, rid)


def members_of(role: str | ObjectRef) -> Iterator[SubjectRef]:
    """Yield the subjects directly granted ``role``.

    Direct grants only; does NOT walk role hierarchy or subject-set
    traversal. For "who *effectively* holds this role" (including
    transitive members via the ``#member`` subject-set chain),
    enumerate ``accessible()`` on a resource that references the role
    in its permission expression.
    """
    from .models import Relationship

    role_ref = _parse_role(role)
    rows = Relationship.objects.filter(
        resource_type=role_ref.resource_type,
        resource_id=role_ref.resource_id,
        relation=ROLE_RELATION,
    ).values_list("subject_type", "subject_id", "optional_subject_relation")
    for st, sid, sr in rows:
        yield SubjectRef.of(st, sid, sr)


def imply(*, parent: str | ObjectRef, child: str | ObjectRef) -> Relationship:
    """Make ``child`` role's effective members also count as ``parent`` role's members.

    Requires both role definitions to use the ``includes`` /
    ``effective_member`` pattern (see :data:`ROLE_INCLUDES_RELATION`).
    Resources that reference ``parent#effective_member`` will then resolve
    grants of ``child`` as if they were ``parent`` grants.

    The membership row written is::

        Relationship(
            resource_type=<parent.resource_type>,
            resource_id=<parent.resource_id>,
            relation="includes",
            subject_type=<child.resource_type>,
            subject_id=<child.resource_id>,
            optional_subject_relation="effective_member",
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
    from .models import Relationship

    parent_ref = _parse_role(parent)
    child_ref = _parse_role(child)
    row, _ = Relationship.objects.get_or_create(
        resource_type=parent_ref.resource_type,
        resource_id=parent_ref.resource_id,
        relation=ROLE_INCLUDES_RELATION,
        subject_type=child_ref.resource_type,
        subject_id=child_ref.resource_id,
        optional_subject_relation=ROLE_EFFECTIVE_MEMBER,
        caveat_name="",
    )
    return row


def unimply(*, parent: str | ObjectRef, child: str | ObjectRef) -> int:
    """Remove the implication ``child#effective_member → parent``.

    Returns the number of rows deleted (0 or 1).
    """
    from .models import Relationship

    parent_ref = _parse_role(parent)
    child_ref = _parse_role(child)
    deleted, _ = Relationship.objects.filter(
        resource_type=parent_ref.resource_type,
        resource_id=parent_ref.resource_id,
        relation=ROLE_INCLUDES_RELATION,
        subject_type=child_ref.resource_type,
        subject_id=child_ref.resource_id,
        optional_subject_relation=ROLE_EFFECTIVE_MEMBER,
    ).delete()
    return deleted


def implies_of(role: str | ObjectRef) -> Iterator[ObjectRef]:
    """Yield roles that ``role`` directly implies (one hop).

    "X implies Y" means members of X are also effective members of Y.
    Looks up rows where ``role`` is the *child* and yields the *parents*.

    Direct edges only; the engine handles transitive closure at check
    time via the ``effective_member`` permission expression.
    """
    from .models import Relationship

    role_ref = _parse_role(role)
    rows = Relationship.objects.filter(
        relation=ROLE_INCLUDES_RELATION,
        subject_type=role_ref.resource_type,
        subject_id=role_ref.resource_id,
        optional_subject_relation=ROLE_EFFECTIVE_MEMBER,
    ).values_list("resource_type", "resource_id")
    for rt, rid in rows:
        yield ObjectRef(rt, rid)


def implied_by_of(role: str | ObjectRef) -> Iterator[ObjectRef]:
    """Yield roles that directly imply ``role`` (one hop).

    Inverse of :func:`implies_of`. Looks up rows where ``role`` is the
    *parent* and yields the *children*.
    """
    from .models import Relationship

    role_ref = _parse_role(role)
    rows = Relationship.objects.filter(
        resource_type=role_ref.resource_type,
        resource_id=role_ref.resource_id,
        relation=ROLE_INCLUDES_RELATION,
        optional_subject_relation=ROLE_EFFECTIVE_MEMBER,
    ).values_list("subject_type", "subject_id")
    for st, sid in rows:
        yield ObjectRef(st, sid)


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
    "unimply",
]
