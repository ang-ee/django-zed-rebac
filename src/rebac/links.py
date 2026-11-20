"""A policy evaluated without inheritance through links (proposal 0023).

``REBAC_INHERIT_THROUGH_LINKS`` (``True`` by default) chooses the policy the
backend evaluates. ``True`` is the published policy. ``False`` is a narrower
policy derived from it in one place, ``LocalBackend``, which narrows each
composition it evaluates once: the snapshot's own (what ``check_new`` and the
compiled policy's residual evaluator read) and, where there are override rows,
the compiled policy's tagged one. Every surface that decides sees the same
policy; ``LocalBackend.schema()`` and everything public that lists names read
the published one. The published policy, ``rebac sync``,
``build-zed``, schema export and relationship validation are untouched:
flipping the setting needs no sync.

**Kinds of relation.** Every relation an arrow walks is classified on the
definition that declares it (``classify``), from its resolved backing:

- *parent*: a single forward foreign key or one-to-one column of the row
  itself (the multi-table parent link included), with no further hop in its
  path, and with filters, if any, that name only the row's own columns;
- *constant*: backed by ``rebac:const``;
- *same type*: its allowed subject types are the declaring type itself (a
  folder in a folder, a role in a role);
- *membership*: no allowed subject type has a model that stores rows (groups,
  roles and anchors declared only in the policy);
- *link*: everything else: a path of several hops, a filter over another
  table, a reverse, many-to-many or generic relation, an attribute backing, a
  stored relation that names records of another type, a backing that does not
  resolve.

**The rule.** In a granting position an arrow ``link->P`` is evaluated as
``link->lent(P)``: what the linked record grants directly. ``lent(P)`` is
``P`` with the record's own named permissions followed to what they lend, and
every arrow removed except those through a constant relation or a membership
(kept as written, so their target is evaluated as the narrowed policy
evaluates it: the role's narrowed permission whole, its same-type hierarchy,
its constants and their targets' parent chains, with only links narrowed
again) and through the multi-table parent link (followed to what the same
record lends in its parent table). Parent, constant, same-type and
membership arrows of a record's own permissions are kept.

**Granting positions only.** A position is granting when it is under an even
number of right-hand sides of ``-``. Under an odd number nothing is narrowed
and nothing is removed, in a record's own permissions and in what it lends
alike, and what such an operand names is evaluated the same way, so a narrowed
exclusion never takes away less than the published one. A permission used
under an exclusion is a version of its own (``excluded(P)``, and
``excluded(lent(P))`` inside what a record lends) wherever that differs from
its granting form. The invariant: for every actor, action and object, what the
narrowed policy grants is a subset of what the published policy grants.

**Generated names.** ``lent(P)``, ``excluded(P)`` and ``excluded(lent(P))``,
each of a declared name, only where the version differs from the declared
permission, on every subject type an arrow dispatches to. The backend refuses
new input with a name the parser could not produce
(``rebac.schema.parser.name_errors``) at ``set_schema`` and at stored-policy
writes, but a stored policy that already holds one keeps loading
(``rebac.W013``). So the narrowing checks its own input: it refuses a policy
with a name the language cannot spell, which is what keeps a declared name
from holding a parenthesis, colliding with a generated one or starting with
``read__`` or ``write__`` (a field gate); a generated name the type or any
type an arrow reaches declares; and a definition with two permissions of one
name. It also refuses a policy that lets a name be read two ways
(``rebac.schema.ast.name_conflicts``: a built-in's name as a relation, a name
declared twice, which new input may not add either, ``rebac.W014``): the
narrowing asks whether a name is a permission where the compiler reads a
relation first. A built-in's name inside an expression is the actor's state
and is never versioned. The narrowed
policy is therefore not a ``.zed`` schema: it is never stored or exported, and
rendered only to explain it. Every public entry resolves its action against
the published policy (``_published_for``): a generated name is an unknown
action there, as it is with the setting ``True``.

**Overrides.** The narrowing applies to the composed policy, so an override of
``P`` reaches ``lent(P)`` too. The compiler tags override arms and sites by
node identity (``TaggedComposition``): a rebuilt node carries the tags of the
node it replaces, a kept node keeps its identity, and a tagged node is
simplified only where it means the same before its deadline and after it.

**Failure.** The walks are iterative, so no expression the compiler accepts
is too deep to narrow. Every failure is one ``NarrowingError`` (a
``SchemaError``, ``rebac.E025``): every decision raises it (reads and gated
writes fail closed), and a write that decides nothing reads the published
policy only. A refusal about the policy itself (``kept``) is deterministic:
the backend keeps it per composed policy and the system check reports it.
Any other exception is raised as the error but not kept, so the next
operation narrows again.

**The report.** ``narrowed_arrows()`` lists each arrow the setting narrows: the
definition and permission it is in, the relation and why it is a link, the
target types and permission, and what each target still lends.
``manage.py rebac links`` prints it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .conf import app_settings
from .errors import SchemaError
from .schema.ast import (
    AttributeBinding,
    ConstBinding,
    Definition,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermExpr,
    Permission,
    PermNil,
    PermRef,
    Relation,
    Schema,
    is_builtin_reference,
    schema_name_conflicts,
)

if TYPE_CHECKING:
    from .composition import ArmTag, TaggedComposition

__all__ = [
    "CONSTANT",
    "LINK",
    "MEMBERSHIP",
    "PARENT",
    "SAME_TYPE",
    "Classification",
    "NarrowedArrow",
    "NarrowingError",
    "classify",
    "evaluated",
    "generated_name",
    "generated_parts",
    "inherits_through_links",
    "is_generated",
    "narrowed",
    "narrowed_arrows",
    "narrowed_tagged",
]

PARENT = "parent"
CONSTANT = "constant"
SAME_TYPE = "same type"
MEMBERSHIP = "membership"
LINK = "link"

# One version of a declared permission: (lent, under an exclusion, type, name).
type _Version = tuple[bool, bool, str, str]

# Set while a public entry evaluates an action that is a generated name: the
# published policy answers it, as an unknown action.
_published: ContextVar[bool] = ContextVar("rebac_published_policy", default=False)


class NarrowingError(SchemaError):
    """The policy without inheritance through links could not be built (``rebac.E025``).

    ``kept`` is true for a refusal the narrowing makes about the policy itself,
    which the same policy meets again: the backend keeps it, and the system
    check reports it. Any other failure is raised as this error with ``kept``
    false, and the next operation narrows again.
    """

    def __init__(self, message: str, *, kept: bool) -> None:
        super().__init__(message)
        self.kept = kept


def _refusal(message: str) -> NarrowingError:
    return NarrowingError(
        f"The policy evaluated without inheritance through links is refused: {message} "
        "(rebac.E025)",
        kept=True,
    )


def _spelling_errors(schema: Schema) -> list[str]:
    """The names of ``schema`` the schema language cannot spell (``rebac.schema.parser``)."""
    from .schema.parser import name_errors

    return name_errors(schema)


def inherits_through_links() -> bool:
    """Whether the published policy is evaluated: ``REBAC_INHERIT_THROUGH_LINKS``, read at each use.

    Also ``True`` inside ``_published_for`` a generated name. Only ``True`` and ``False``
    mean anything: any other value (the string ``"False"`` an environment variable
    gives, ``0``, ``None``) has no safe reading, so reading it raises
    ``ImproperlyConfigured`` (``rebac.E028``, ``rebac.conf``) and no decision picks
    either policy for a value that means neither. Read as truthy it would evaluate the
    published policy where the deployment asked for the narrower one, which grants
    more; read as falsy it would take access away unannounced.
    """
    value: bool = app_settings.REBAC_INHERIT_THROUGH_LINKS
    return _published.get() or value


def is_generated(name: str) -> bool:
    """Whether ``name`` is a generated permission's: no declared name holds a parenthesis."""
    return "(" in name


@contextmanager
def _published_for(actions: str | Iterable[str]) -> Iterator[None]:
    """Evaluate the block with the published policy if an action is a generated name.

    Every public entry that takes an action wraps its work in this: a
    generated permission is an internal version, not an action, so asking for
    one gets the published policy's answer, an unknown action's, as with the
    setting ``True``. Internal: the block sets a context variable, so it must
    not be held across a ``yield`` or an ``await`` that hands control back.
    """
    names = [actions] if isinstance(actions, str) else list(actions)
    if not any(is_generated(name) for name in names) or _published.get():
        yield
        return
    token = _published.set(True)
    try:
        yield
    finally:
        _published.reset(token)


def generated_name(lent: bool, negative: bool, name: str) -> str:
    """The name of one version of the declared permission ``name``."""
    if lent and negative:
        return f"excluded(lent({name}))"
    if lent:
        return f"lent({name})"
    if negative:
        return f"excluded({name})"
    return name


def generated_parts(name: str) -> tuple[bool, bool, str] | None:
    """``(lent, under an exclusion, declared name)`` of a generated name; ``None`` for a declared one."""
    if name.startswith("excluded(lent(") and name.endswith("))"):
        return True, True, name[len("excluded(lent(") : -2]
    if name.startswith("lent(") and name.endswith(")"):
        return True, False, name[len("lent(") : -1]
    if name.startswith("excluded(") and name.endswith(")"):
        return False, True, name[len("excluded(") : -1]
    return None


# ---------- Kinds of relation ----------


@dataclass(frozen=True, slots=True)
class Classification:
    """What an arrow through a relation is: one of the five kinds, and why."""

    kind: str
    reason: str
    # The multi-table parent link: the same record in its parent table.
    parent_link: bool = False


def classify(definition: Definition, relation: Relation) -> Classification:
    """The kind of ``relation`` on ``definition``, from its resolved backing."""
    from django.db import models

    from .field_backing import resolve_field_backing

    backing = relation.backing
    if isinstance(backing, ConstBinding):
        return Classification(CONSTANT, "a constant relation")
    resolved = (
        resolve_field_backing(definition, relation) if isinstance(backing, FieldBinding) else None
    )
    if (
        isinstance(backing, FieldBinding)
        and resolved is not None
        and resolved.generic is None
        and "__" not in resolved.path
        and isinstance(resolved.field, models.ForeignKey)
        and resolved.field.concrete
        and all(_own_column(resolved.source_model, lookup) for lookup, _value in backing.filters)
    ):
        column = resolved.field
        link = isinstance(column, models.OneToOneField) and bool(column.remote_field.parent_link)
        return Classification(
            PARENT,
            "the multi-table parent link" if link else "a foreign key of the row",
            parent_link=link,
        )
    types = {allowed.type for allowed in relation.allowed_subjects}
    if types == {definition.resource_type}:
        return Classification(SAME_TYPE, "records of its own type")
    if types and not any(_stores_rows(type_) for type_ in types):
        return Classification(MEMBERSHIP, "subjects that store no rows")
    if isinstance(backing, AttributeBinding):
        return Classification(LINK, "an attribute backing")
    if backing is None:
        return Classification(LINK, "a stored relation to records of another type")
    if resolved is None:
        return Classification(LINK, "a backing that does not resolve")
    if resolved.generic is not None or _is_generic_relation(resolved.field):
        return Classification(LINK, "a generic relation")
    if "__" in resolved.path:
        return Classification(LINK, "a path of several hops")
    if resolved.field.many_to_many:
        return Classification(LINK, "a many-to-many relation")
    if not isinstance(resolved.field, models.ForeignKey):
        return Classification(LINK, "a reverse relation")
    return Classification(LINK, "a filter over another table")


def _own_column(model: Any, lookup: str) -> bool:
    """Whether a backing filter names a column of the row: one field, no join."""
    from .field_backing import _relation_path

    try:
        _owner, column, path = _relation_path(model, lookup, lookup=True)
    except ValueError:
        return False
    return "__" not in path and bool(getattr(column, "concrete", False)) and not column.many_to_many


def _stores_rows(type_: str) -> bool:
    from .resources import model_for_subject_type, stores_rows

    found = model_for_subject_type(type_)
    return found is not None and stores_rows(found[0])


def _is_generic_relation(column: Any) -> bool:
    from django.contrib.contenttypes.fields import GenericRelation

    return isinstance(column, GenericRelation)


# ---------- The narrowing ----------


@dataclass(frozen=True, slots=True)
class NarrowedArrow:
    """One arrow the setting narrows, for the report (``narrowed_arrows``).

    ``lent`` holds, for each target type, what it still lends of ``target``:
    the lent expression with the record's own named permissions opened, or
    ``nil`` when it lends nothing. ``reaches`` counts the relations and
    permissions the published policy evaluates from the arrow's targets.
    """

    resource_type: str
    permission: str
    relation: str
    reason: str
    target_types: tuple[str, ...]
    target: str
    lent: tuple[tuple[str, str], ...]
    reaches: int


def _children(expr: PermExpr, negative: bool) -> Iterator[tuple[PermExpr, bool]]:
    """The operands of ``expr`` with their polarity: the right of ``-`` flips it."""
    if isinstance(expr, PermBinOp):
        yield expr.left, negative
        yield expr.right, negative ^ (expr.op == "-")


def _leaves(expr: PermExpr, negative: bool) -> Iterator[tuple[PermExpr, bool]]:
    """Every node of ``expr`` that is not an operator, with its polarity, iteratively."""
    pending = [(expr, negative)]
    while pending:
        node, polarity = pending.pop()
        if isinstance(node, PermBinOp):
            pending.extend(reversed(list(_children(node, polarity))))
        else:
            yield node, polarity


def _nodes(expr: PermExpr) -> Iterator[PermExpr]:
    pending = [expr]
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, PermBinOp):
            pending.append(node.right)
            pending.append(node.left)


class _Narrowing:
    """One narrowing of one composed schema; ``run`` builds the narrowed schema."""

    def __init__(
        self,
        schema: Schema,
        arms: Mapping[int, ArmTag] | None = None,
        sites: Mapping[int, ArmTag] | None = None,
    ) -> None:
        self.schema = schema
        self.arms: Mapping[int, ArmTag] = arms or {}
        self.sites: Mapping[int, ArmTag] = sites or {}
        self.definitions = {d.resource_type: d for d in schema.definitions}
        self.permissions = {
            (d.resource_type, p.name): p for d in schema.definitions for p in d.permissions
        }
        self.relations = {
            (d.resource_type, r.name): r for d in schema.definitions for r in d.relations
        }
        self._kinds: dict[tuple[str, str], Classification] = {}
        self._changed: set[_Version] = set()
        # A generated permission to emit: (type, name) -> its version, or the name it stands for.
        self._needed: dict[tuple[str, str], _Version | str] = {}
        self._pending: list[tuple[str, str]] = []
        self.new_arms: dict[int, ArmTag] = {}
        self.new_sites: dict[int, ArmTag] = {}
        # (type, permission, relation, target) of each narrowed arrow.
        self.report: dict[tuple[str, str, str, str], Classification] = {}

    # ---------- Facts ----------

    def kind(self, type_: str, via: str) -> Classification | None:
        relation = self.relations.get((type_, via))
        if relation is None:
            return None
        found = self._kinds.get((type_, via))
        if found is None:
            found = self._kinds[(type_, via)] = classify(self.definitions[type_], relation)
        return found

    def names_permission(self, type_: str, name: str) -> bool:
        """Whether ``name``, named inside an expression of ``type_``, is one of its permissions.

        Read as the compiler reads it: ``authenticated`` and ``anonymous`` there are the
        actor's state whatever the definition declares under those names
        (``rebac.schema.ast.is_builtin_reference``), so such a name is never versioned.
        """
        return (type_, name) in self.permissions and not is_builtin_reference(name)

    def subject_types(self, type_: str, via: str, target: str) -> list[str]:
        """The subject types an arrow dispatches ``target`` to, where it is declared."""
        relation = self.relations[(type_, via)]
        found = dict.fromkeys(allowed.type for allowed in relation.allowed_subjects)
        return [
            subject
            for subject in sorted(found)
            if (subject, target) in self.permissions or (subject, target) in self.relations
        ]

    @staticmethod
    def follow(lent: bool, negative: bool, kind: Classification) -> bool | None:
        """Whether an arrow's target is read as what it lends; ``None``: the arrow is removed.

        In a record's own permission only a link in a granting position reads
        what its target lends. In what a record lends, the parent link reads
        what the same record lends in its parent table; a constant relation, a
        membership and any arrow under an exclusion read the target's own
        permission; every other arrow in a granting position is removed.
        """
        if not lent:
            return kind.kind == LINK and not negative
        if kind.parent_link:
            return True
        if negative or kind.kind in (CONSTANT, MEMBERSHIP):
            return False
        return None

    # ---------- Which versions differ from the published permission ----------

    def _references(self, version: _Version) -> tuple[bool, list[_Version]]:
        """Whether ``version`` removes an arrow itself, and the versions it reads."""
        lent, negative, type_, name = version
        removed = False
        found: list[_Version] = []
        for node, polarity in _leaves(self.permissions[(type_, name)].expression, negative):
            if isinstance(node, PermRef):
                if self.names_permission(type_, node.name):
                    found.append((lent, polarity, type_, node.name))
            elif isinstance(node, PermArrow):
                kind = self.kind(type_, node.via)
                if kind is None:
                    continue
                types = self.subject_types(type_, node.via, node.target)
                to_lent = self.follow(lent, polarity, kind)
                if to_lent is None:
                    # An arrow whose target is declared nowhere grants nothing already.
                    removed = removed or bool(types)
                    continue
                for subject in types:
                    if (subject, node.target) in self.permissions:
                        found.append((to_lent, polarity, subject, node.target))
                        # Where nothing lent differs, the kept arrow reads the target itself.
                        found.append((False, polarity, subject, node.target))
        return removed, found

    def _settle(self) -> None:
        """The versions that differ from the published permission: a least fixpoint."""
        graph: dict[_Version, list[_Version]] = {}
        pending: list[_Version] = [(False, False, t, n) for t, n in self.permissions]
        changed: list[_Version] = []
        while pending:
            version = pending.pop()
            if version in graph:
                continue
            removed, found = self._references(version)
            graph[version] = found
            if removed:
                changed.append(version)
            pending.extend(found)
        readers: dict[_Version, list[_Version]] = {}
        for version, found in graph.items():
            for target in found:
                readers.setdefault(target, []).append(version)
        self._changed = set(changed)
        while changed:
            for reader in readers.get(changed.pop(), ()):
                if reader not in self._changed:
                    self._changed.add(reader)
                    changed.append(reader)

    def name_of(self, version: _Version) -> str:
        """The name the narrowed policy evaluates ``version`` by, on its own type."""
        lent, negative, type_, name = version
        if (type_, name) not in self.permissions or (not lent and not negative):
            return name
        if version not in self._changed and (False, False, type_, name) not in self._changed:
            return name
        return generated_name(lent, negative, name)

    def _need(self, type_: str, name: str, meaning: _Version | str) -> None:
        """Emit the generated permission ``name`` on ``type_``.

        Never in place of a declared name: a policy that declares one is refused,
        though no schema that spells its names can (``_spelling_errors``).
        """
        if (type_, name) in self.permissions or (type_, name) in self.relations:
            raise _refusal(f"{type_}#{name} is declared and would be generated")
        if (type_, name) not in self._needed:
            self._needed[(type_, name)] = meaning
            self._pending.append((type_, name))

    # ---------- Rewriting ----------

    def _copy(self, old: PermExpr, new: PermExpr) -> PermExpr:
        """``new`` in place of ``old``, with the override tags ``old`` carries."""
        arm = self.arms.get(id(old))
        if arm is not None:
            self.new_arms[id(new)] = arm
        site = self.sites.get(id(old))
        if site is not None:
            self.new_sites[id(new)] = site
        return new

    def _arrow_target(self, type_: str, via: str, target: str, lent: bool, negative: bool) -> str:
        """One name for ``target`` on every subject type the arrow dispatches to."""
        names = {
            subject: self.name_of((lent, negative, subject, target))
            for subject in self.subject_types(type_, via, target)
        }
        chosen = set(names.values())
        name = chosen.pop() if len(chosen) == 1 else generated_name(lent, negative, target)
        if not names or name == target:
            return target
        # The compiler dispatches the arrow to every subject type of ``via``,
        # whether or not it declares ``target``: none may declare the new name.
        for allowed in self.relations[(type_, via)].allowed_subjects:
            if (allowed.type, name) in self.permissions or (allowed.type, name) in self.relations:
                raise _refusal(f"{allowed.type}#{name} is declared and an arrow would reach it")
        for subject, own in names.items():
            # Where the version is the declared member, the generated permission names it.
            # A built-in name named inside an expression is the actor's state: a permission
            # of that name is emitted as the version itself.
            # (A relation never carries a built-in's name: ``run`` refuses such a policy.)
            alias = own != name and not is_builtin_reference(own)
            self._need(subject, name, own if alias else (lent, negative, subject, target))
        return name

    def _leaf(self, node: PermExpr, type_: str, lent: bool, negative: bool, root: str) -> PermExpr:
        if isinstance(node, PermRef):
            if not self.names_permission(type_, node.name):
                return node
            version = (lent, negative, type_, node.name)
            name = self.name_of(version)
            if name == node.name:
                return node
            self._need(type_, name, version)
            return self._copy(node, PermRef(name))
        if isinstance(node, PermArrow):
            kind = self.kind(type_, node.via)
            if kind is None:
                return node
            to_lent = self.follow(lent, negative, kind)
            if to_lent is None:
                return PermNil()
            target = self._arrow_target(type_, node.via, node.target, to_lent, negative)
            if target == node.target:
                return node
            if to_lent and not lent:
                self.report.setdefault((type_, root, node.via, node.target), kind)
            return self._copy(node, PermArrow(node.via, target))
        return node

    def _combine(self, node: PermBinOp, left: PermExpr, right: PermExpr) -> PermExpr:
        if left is node.left and right is node.right:
            return node
        arm = id(node) in self.arms
        site = id(node) in self.sites
        empty_left, empty_right = isinstance(left, PermNil), isinstance(right, PermNil)
        # A tagged node keeps its place unless what replaces it means the same
        # before its deadline and after it. After its deadline a site is its
        # left operand; an arm is admitted only before its deadline.
        if node.op == "+":
            if empty_left and empty_right:
                return PermNil()
            if (empty_left or empty_right) and not arm and not site:
                return right if empty_left else left
        elif node.op == "&":
            if empty_left or (empty_right and not site):
                return PermNil()
        elif node.op == "-":
            if empty_left:
                return PermNil()
            if empty_right and not arm:
                return left
        return self._copy(node, PermBinOp(node.op, left, right))

    def rewrite(
        self, expr: PermExpr, type_: str, lent: bool, negative: bool, root: str
    ) -> PermExpr:
        """``expr`` of ``type_`` as the version ``(lent, negative)`` of ``root`` evaluates it.

        Iterative, so that no depth the compiler accepts is too deep.
        """
        done: dict[tuple[int, bool], PermExpr] = {}
        pending: list[tuple[PermExpr, bool, bool]] = [(expr, negative, False)]
        while pending:
            node, polarity, expanded = pending.pop()
            key = (id(node), polarity)
            if key in done:
                continue
            if not isinstance(node, PermBinOp):
                done[key] = self._leaf(node, type_, lent, polarity, root)
                continue
            right_polarity = polarity ^ (node.op == "-")
            if not expanded:
                pending.append((node, polarity, True))
                pending.append((node.right, right_polarity, False))
                pending.append((node.left, polarity, False))
                continue
            done[key] = self._combine(
                node, done[(id(node.left), polarity)], done[(id(node.right), right_polarity)]
            )
        return done[(id(expr), negative)]

    # ---------- The narrowed schema ----------

    def run(self) -> Schema:
        refused = _spelling_errors(self.schema)
        if refused:
            raise _refusal("; ".join(refused))
        # A name read two ways (a built-in's name as a relation, a name declared twice):
        # the narrowing asks whether a name is a permission where the compiler reads a
        # relation first, so it refuses such a policy rather than version the wrong thing.
        conflicts = schema_name_conflicts(self.schema)
        if conflicts:
            raise _refusal("; ".join(conflicts))
        self._settle()
        permissions: dict[str, dict[str, Permission]] = {
            d.resource_type: {} for d in self.schema.definitions
        }
        for definition in self.schema.definitions:
            type_ = definition.resource_type
            for permission in definition.permissions:
                # Every one is rewritten, not only those that grant less: an exclusion
                # that grants the same names the published version of its operand.
                rewritten = self.rewrite(
                    permission.expression, type_, False, False, permission.name
                )
                permissions[type_][permission.name] = (
                    permission
                    if rewritten is permission.expression
                    else Permission(permission.name, rewritten)
                )
        generated: dict[str, dict[str, Permission]] = {type_: {} for type_ in permissions}
        while self._pending:
            type_, name = self._pending.pop(0)
            meaning = self._needed[(type_, name)]
            if isinstance(meaning, str):
                expression: PermExpr = PermRef(meaning)
            else:
                lent, negative, _type, declared = meaning
                source = self.permissions[(type_, declared)]
                expression = self.rewrite(source.expression, type_, lent, negative, declared)
            generated[type_][name] = Permission(name, expression)
        definitions = []
        for definition in self.schema.definitions:
            type_ = definition.resource_type
            own, extra = permissions[type_], generated[type_]
            if len(own) != len(definition.permissions) or set(own) & set(extra):
                raise _refusal(f"{type_} would hold two permissions of one name")
            if not extra and all(own[p.name] is p for p in definition.permissions):
                definitions.append(definition)
                continue
            definitions.append(
                Definition(
                    type_,
                    definition.relations,
                    (*own.values(), *(extra[name] for name in sorted(extra))),
                )
            )
        return Schema(
            definitions=definitions,
            caveats=list(self.schema.caveats),
            directives=list(self.schema.directives),
            headers=dict(self.schema.headers),
        )

    def tags(self, schema: Schema) -> tuple[dict[int, ArmTag], dict[int, ArmTag]]:
        """The override tags of the nodes of ``schema``, by identity: only nodes it holds."""
        arms: dict[int, ArmTag] = {}
        sites: dict[int, ArmTag] = {}
        for definition in schema.definitions:
            for permission in definition.permissions:
                for node in _nodes(permission.expression):
                    key = id(node)
                    for created, source, target in (
                        (self.new_arms, self.arms, arms),
                        (self.new_sites, self.sites, sites),
                    ):
                        tag = created.get(key, source.get(key))
                        if tag is not None:
                            target[key] = tag
        return arms, sites

    # ---------- What a record lends, for the report ----------

    def lent_text(self, type_: str, name: str) -> str:
        """What ``type_`` lends of ``name``, its own named permissions opened."""
        if (type_, name) not in self.permissions:
            return name
        return _render(self._opened(type_, name, frozenset()))

    def _opened(self, type_: str, name: str, seen: frozenset[str]) -> PermExpr:
        """The lent version of ``name`` with the record's granting references to its own
        permissions replaced by what those lend. The depth is the number of the
        record's permissions; within one expression the work is iterative.
        """
        lent = self.rewrite(self.permissions[(type_, name)].expression, type_, True, False, name)
        seen = seen | {name}
        done: dict[tuple[int, bool], PermExpr] = {}
        pending: list[tuple[PermExpr, bool, bool]] = [(lent, False, False)]
        while pending:
            node, negative, expanded = pending.pop()
            key = (id(node), negative)
            if key in done:
                continue
            if isinstance(node, PermRef) and not negative:
                parts = generated_parts(node.name)
                declared = parts[2] if parts is not None and parts[:2] == (True, False) else None
                if declared is None and self.names_permission(type_, node.name):
                    declared = node.name
                if declared is not None and declared not in seen:
                    done[key] = self._opened(type_, declared, seen)
                else:
                    done[key] = node
                continue
            if not isinstance(node, PermBinOp):
                done[key] = node
                continue
            right_negative = negative ^ (node.op == "-")
            if not expanded:
                pending.append((node, negative, True))
                pending.append((node.right, right_negative, False))
                pending.append((node.left, negative, False))
                continue
            left, right = done[(id(node.left), negative)], done[(id(node.right), right_negative)]
            if node.op == "+" and (isinstance(left, PermNil) or isinstance(right, PermNil)):
                done[key] = right if isinstance(left, PermNil) else left
            elif isinstance(left, PermNil) or (node.op == "&" and isinstance(right, PermNil)):
                done[key] = PermNil()
            elif node.op == "-" and isinstance(right, PermNil):
                done[key] = left
            else:
                done[key] = PermBinOp(node.op, left, right)
        return done[(id(lent), False)]


def _render(expr: PermExpr) -> str:
    """``expr`` as ``rebac.schema.rendering`` writes it, iteratively."""
    done: dict[int, str] = {}
    pending: list[tuple[PermExpr, bool]] = [(expr, False)]
    while pending:
        node, expanded = pending.pop()
        if id(node) in done:
            continue
        if isinstance(node, PermNil):
            done[id(node)] = "nil"
        elif isinstance(node, PermRef):
            done[id(node)] = node.name
        elif isinstance(node, PermArrow):
            done[id(node)] = f"{node.via}->{node.target}"
        elif not expanded:
            assert isinstance(node, PermBinOp)
            pending.extend(((node, True), (node.right, False), (node.left, False)))
        else:
            assert isinstance(node, PermBinOp)
            done[id(node)] = f"({done[id(node.left)]} {node.op} {done[id(node.right)]})"
    return done[id(expr)]


def _unexpected(exc: Exception) -> NarrowingError:
    return NarrowingError(
        f"The policy evaluated without inheritance through links cannot be built: {exc!r} "
        "(rebac.E025)",
        kept=False,
    )


def narrowed(schema: Schema) -> Schema:
    """``schema`` evaluated without inheritance through links."""
    try:
        return _Narrowing(schema).run()
    except NarrowingError:
        raise
    except Exception as exc:
        raise _unexpected(exc) from exc


def narrowed_tagged(tagged: TaggedComposition) -> TaggedComposition:
    """A composition without inheritance through links, its override tags in place."""
    from .composition import TaggedComposition

    try:
        narrowing = _Narrowing(tagged.schema, tagged.arms, tagged.sites)
        schema = narrowing.run()
        arms, sites = narrowing.tags(schema)
    except NarrowingError:
        raise
    except Exception as exc:
        raise _unexpected(exc) from exc
    return TaggedComposition(schema, arms, sites)


def evaluated(schema: Schema) -> Schema:
    """The policy the setting evaluates for the published ``schema``."""
    return schema if inherits_through_links() else narrowed(schema)


# ---------- The report ----------


def narrowed_arrows(schema: Schema | None = None) -> list[NarrowedArrow]:
    """Every arrow ``REBAC_INHERIT_THROUGH_LINKS = False`` narrows in ``schema``.

    ``schema`` defaults to the active backend's published policy, so the report
    is the same whatever the setting. It is sorted by definition, permission,
    relation and target. An arrow is listed under the permission it is written
    in, also where it is narrowed only in that permission's ``excluded(P)``.
    """
    if schema is None:
        from .backends import backend

        schema = backend()._published_schema()
    from .schema.graph import dispatch_graph

    narrowing = _Narrowing(schema)
    narrowing.run()
    dependencies, vias = dispatch_graph(schema)
    found = []
    for (type_, permission, via, target), kind in sorted(narrowing.report.items()):
        types = tuple(narrowing.subject_types(type_, via, target))
        reached: set[tuple[str, str]] = set()
        for subject in types:
            reached |= _reach(dependencies, vias, (subject, target))
        found.append(
            NarrowedArrow(
                type_,
                permission,
                via,
                kind.reason,
                types,
                target,
                tuple((subject, narrowing.lent_text(subject, target)) for subject in types),
                len(reached),
            )
        )
    return found


def _reach(
    dependencies: Mapping[tuple[str, str], Iterable[Any]],
    vias: Mapping[tuple[str, str], Iterable[tuple[str, str]]],
    root: tuple[str, str],
) -> set[tuple[str, str]]:
    """The relations and permissions the compiler evaluates from ``root``, itself included."""
    seen: set[tuple[str, str]] = set()
    pending = [root]
    while pending:
        key = pending.pop()
        if key in seen or key not in dependencies:
            continue
        seen.add(key)
        pending.extend(dependency.target for dependency in dependencies[key])
        pending.extend(vias.get(key, ()))
    return seen
