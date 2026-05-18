"""AST nodes for the .zed schema language.

The expression operators bind in this order (per SpiceDB):

    +   union          ← binds tightest
    &   intersection
    -   exclusion      ← binds loosest

The compiler always emits explicit parentheses for compound expressions —
single-line schema fragments without parens are a footgun even when they parse.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

BUILTIN_ACTOR_TYPES = frozenset({"anonymous", "authenticated"})


@dataclass(frozen=True, slots=True)
class AllowedSubject:
    """One subject term in a relation's type union.

    Four shapes are representable, in order of specificity:

    - ``auth/user`` → ``type="auth/user"`` (any user)
    - ``auth/user:*`` → ``type="auth/user", wildcard=True`` (the wildcard subject)
    - ``auth/group#member`` → ``type="auth/group", relation="member"`` (any group's member)
    - ``angee/role:admin#member`` → ``type="angee/role", id="admin", relation="member"``
      (members of one specific resource id — the canonical pattern for
      universal admin roles)
    """

    type: str
    relation: str = ""  # subject set, e.g. group#member
    wildcard: bool = False  # `auth/user:*`
    with_caveat: str = ""  # caveat the subject is bound by
    # Specific resource id, e.g. `angee/role:admin#member`. Constrained at
    # parse time to identifier shape — `[A-Za-z_][A-Za-z0-9_]*` — even though
    # the runtime `Relationship.resource_id` column accepts the broader
    # SpiceDB object-id grammar. The schema-side restriction matches the
    # universal-admin pattern's actual usage (`role:admin`,
    # `role:object_viewer`) and keeps the tokenizer simple. Relationship rows
    # may still carry numeric or hyphenated ids — the schema just can't
    # name-check them.
    id: str = ""


@dataclass(frozen=True, slots=True)
class Relation:
    name: str
    allowed_subjects: tuple[AllowedSubject, ...]
    with_expiration: bool = False


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
