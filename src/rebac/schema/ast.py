"""AST nodes for the .zed schema language.

The expression operators bind in this order (per SpiceDB):

    +   union          ← binds tightest
    &   intersection
    -   exclusion      ← binds loosest

The compiler always emits explicit parentheses for compound expressions —
single-line schema fragments without parens are a footgun even when they parse.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TypeGuard, cast

BUILTIN_ACTOR_TYPES = frozenset({"anonymous", "authenticated"})


@dataclass(frozen=True, slots=True)
class AllowedSubject:
    """One subject term in a relation's type union.

    Four shapes are representable, in order of specificity:

    - ``auth/user`` → ``type="auth/user"`` (any user)
    - ``auth/user:*`` → ``type="auth/user", wildcard=True`` (the wildcard subject)
    - ``auth/group#member`` → ``type="auth/group", relation="member"`` (any group's member)
    - ``platform/role:admin#member`` → ``type="platform/role", id="admin", relation="member"``
      (members of one specific resource id — the canonical pattern for
      universal admin roles)
    """

    type: str
    relation: str = ""  # subject set, e.g. group#member
    wildcard: bool = False  # `auth/user:*`
    with_caveat: str = ""  # caveat the subject is bound by
    # Specific resource id, e.g. `platform/role:admin#member`. Constrained at
    # parse time to identifier shape — `[A-Za-z_][A-Za-z0-9_]*` — even though
    # the runtime `Relationship.resource_id` column accepts the broader
    # SpiceDB object-id grammar. The schema-side restriction matches the
    # universal-admin pattern's actual usage (`role:admin`,
    # `role:object_viewer`) and keeps the tokenizer simple. Relationship rows
    # may still carry numeric or hyphenated ids — the schema just can't
    # name-check them.
    id: str = ""


@dataclass(frozen=True, slots=True)
class FieldBinding:
    """A Django relation path with source-model predicates.

    ``path`` is a Django lookup path from the declaring model to the single
    allowed subject model (``folder``, ``roster__user``); ``filters`` are
    source-model lookups applied in the same join.
    """

    path: str
    filters: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class AttributeBinding:
    """Live container membership from a scalar field on the subject model.

    With resource/value, only that fixed container is derived; other IDs keep
    stored tuples. Without them the subject field determines the container ID.
    """

    field: str
    resource: str | None = None
    value: Any = None
    filters: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ConstBinding:
    """A synthetic relation that resolves to one fixed object id for every row.

    Declared with ``// rebac:const=<id>`` on a single-subject relation: every
    object of the declaring type behaves as if it held
    ``<relation>@<subject_type>:<id>``, with no stored tuple and no model field.
    It is the schema-level "static relationship" SpiceDB never shipped (issue
    #346 / #1266); the local backend synthesises the edge at evaluation time, so
    a tuple-only backend would have to materialise it instead.
    """

    target_id: str


@dataclass(frozen=True, slots=True)
class Relation:
    name: str
    allowed_subjects: tuple[AllowedSubject, ...]
    with_expiration: bool = False
    backing: FieldBinding | ConstBinding | AttributeBinding | None = None

    def has_backing(self, resource_id: str | None = None) -> bool:
        """Whether the relation is live-backed for this resource scope.

        An omitted ID is an unqualified scope and therefore includes a fixed
        attribute's derived anchor. Concrete non-anchor IDs retain stored
        tuples.
        """

        if self.backing is None:
            return False
        if (
            resource_id is not None
            and isinstance(self.backing, AttributeBinding)
            and self.backing.resource is not None
        ):
            return resource_id == self.backing.resource
        return True


def _is_lookup_path(value: object) -> TypeGuard[str]:
    """Whether ``value`` is a Django lookup path: identifier segments joined by ``__``.

    Segments follow Python's identifier rule (Django field names are Python
    identifiers, Unicode included); an empty segment (``a____b``) is rejected
    because Django would raise ``FieldError`` on it.
    """
    return isinstance(value, str) and all(part.isidentifier() for part in value.split("__"))


def _binding_filters(value: Any) -> tuple[tuple[str, Any], ...]:
    """Validate immutable, deterministic scalar ORM predicates."""

    if not isinstance(value, dict):
        raise ValueError("backing filters must be a JSON object")
    for key, item in value.items():
        if not _is_lookup_path(key):
            raise ValueError("backing filters require ORM lookup names")
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError("backing filter values must be JSON scalars")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("backing filter values must be finite")
    return tuple(sorted(value.items()))


def backing_from_dict(
    value: dict[str, Any] | None,
) -> FieldBinding | ConstBinding | AttributeBinding | None:
    """Decode and validate the native persisted relation-backing contract."""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("relation backing must be a JSON object")
    kind = value.get("kind", "fk")
    if kind == "const":
        if set(value) - {"kind", "target_id"}:
            raise ValueError("unknown constant backing fields")
        target = value.get("target_id")
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9/_|=+-]{1,1024}", target):
            raise ValueError("constant backing requires a concrete object ID")
        return ConstBinding(target_id=target)
    if kind == "fk":
        if set(value) - {"kind", "path", "filters"}:
            raise ValueError("unknown field backing fields")
        path = value.get("path")
        if not _is_lookup_path(path):
            raise ValueError("field backing requires a Django relation path")
        return FieldBinding(path=path, filters=_binding_filters(value.get("filters", {})))
    if kind == "attribute":
        if set(value) - {"kind", "field", "resource", "value", "filters"}:
            raise ValueError("unknown attribute backing fields")
        name = value.get("field")
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError("attribute backing requires a scalar field name")
        if ("resource" in value) != ("value" in value):
            raise ValueError("attribute backing resource and value must appear together")
        resource = value.get("resource")
        if "resource" in value:
            if not isinstance(resource, str) or not re.fullmatch(
                r"[A-Za-z0-9/_|=+-]{1,1024}", resource
            ):
                raise ValueError("attribute backing requires a concrete resource ID")
            _binding_filters({"value": value["value"]})
        return AttributeBinding(
            field=name,
            resource=cast(str | None, resource),
            value=value.get("value"),
            filters=_binding_filters(value.get("filters", {})),
        )
    raise ValueError(f"unsupported relation backing kind {kind!r}")


def backing_to_dict(
    backing: FieldBinding | ConstBinding | AttributeBinding | None,
) -> dict[str, Any] | None:
    """Encode one backing without losing filters, fixed IDs, or false/null values.

    The ``kind`` key is the persisted discriminator; the AST discriminates by
    class. Consumers of ``SchemaRelation.backing`` read this shape.
    """

    if backing is None:
        return None
    if isinstance(backing, ConstBinding):
        return {"kind": "const", "target_id": backing.target_id}
    if isinstance(backing, FieldBinding):
        result: dict[str, Any] = {"kind": "fk", "path": backing.path}
    elif isinstance(backing, AttributeBinding):
        result = {"kind": "attribute", "field": backing.field}
        if backing.resource is not None:
            result.update(resource=backing.resource, value=backing.value)
    else:
        raise TypeError(f"unsupported backing {type(backing).__name__}")
    if backing.filters:
        result["filters"] = dict(backing.filters)
    return result


# ---------- Permission expression AST ----------


@dataclass(frozen=True, slots=True)
class PermRef:
    """Reference to a relation or another permission on the same definition."""

    name: str


@dataclass(frozen=True, slots=True)
class PermArrow:
    """`relation->permission` — walk through `relation`, evaluate `permission` there."""

    via: str  # the relation to walk
    target: str  # the permission to check on the target


@dataclass(frozen=True, slots=True)
class PermBinOp:
    """`+` (union), `&` (intersection), `-` (exclusion)."""

    op: str  # "+" | "&" | "-"
    left: PermExpr
    right: PermExpr


@dataclass(frozen=True, slots=True)
class PermNil:
    """The `nil` literal — never satisfied."""


PermExpr = PermRef | PermArrow | PermBinOp | PermNil


@dataclass(frozen=True, slots=True)
class Permission:
    name: str
    expression: PermExpr
    raw_text: str = ""


# ---------- Caveat AST ----------


@dataclass(frozen=True, slots=True)
class CaveatParam:
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class Caveat:
    name: str
    params: tuple[CaveatParam, ...]
    expression: str  # raw CEL — evaluation is the backend's job


# ---------- Definition / Schema ----------


@dataclass(frozen=True, slots=True)
class Definition:
    resource_type: str
    relations: tuple[Relation, ...]
    permissions: tuple[Permission, ...]

    def extend(
        self,
        *,
        relations: Sequence[Relation] = (),
        permission_arms: Sequence[Permission] = (),
    ) -> Definition:
        """Add relations and union permission arms without mutating this definition.

        Existing declaration names cannot be reused; every permission arm must
        target an existing permission. Operand order follows contribution order.
        """
        all_relations = {relation.name: relation for relation in self.relations}
        permissions = {permission.name: permission for permission in self.permissions}
        for relation in relations:
            if relation.name in all_relations or relation.name in permissions:
                raise ValueError(
                    f"relation {self.resource_type}#{relation.name!r} already declared"
                )
            all_relations[relation.name] = relation
        for arm in permission_arms:
            current = permissions.get(arm.name)
            if current is None:
                raise ValueError(f"permission {self.resource_type}#{arm.name!r} is not declared")
            permissions[arm.name] = Permission(
                arm.name, PermBinOp("+", current.expression, arm.expression)
            )
        return Definition(
            self.resource_type,
            tuple(sorted(all_relations.values(), key=lambda relation: relation.name)),
            tuple(sorted(permissions.values(), key=lambda permission: permission.name)),
        )


@dataclass
class Schema:
    """Top-level parsed schema."""

    definitions: list[Definition] = field(default_factory=list)
    caveats: list[Caveat] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    def get_definition(self, resource_type: str) -> Definition | None:
        for d in self.definitions:
            if d.resource_type == resource_type:
                return d
        return None

    def all_relations(self, resource_type: str) -> Sequence[Relation]:
        d = self.get_definition(resource_type)
        return d.relations if d else ()

    def get_permission(self, resource_type: str, name: str) -> Permission | None:
        d = self.get_definition(resource_type)
        if not d:
            return None
        for p in d.permissions:
            if p.name == name:
                return p
        return None

    def get_caveat(self, name: str) -> Caveat | None:
        for c in self.caveats:
            if c.name == name:
                return c
        return None
