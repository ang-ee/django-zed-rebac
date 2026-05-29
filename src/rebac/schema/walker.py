"""Tri-state permission-expression walker — shared by every evaluator.

The walker dispatches on the :class:`PermExpr` AST and combines branch
results through ``+`` / ``&`` / ``-`` with caveat-aware tri-state
semantics. The two side-effectful resolution steps —

* "does the subject have this *direct relation* on
  ``(definition, resource_id)``?"
* "for each row of relation ``via``, does the subject have ``target`` on
  the row's subject?"

— are delegated to caller-supplied callbacks on :class:`WalkContext`.
Today two evaluators share the walker:

* :class:`rebac.backends.local.LocalBackend` resolves both callbacks
  against the in-process ``Relationship`` table.
* :func:`rebac.preflight.check_new` resolves direct relations against a
  caller-supplied virtual ``relation -> subjects`` mapping (the row
  doesn't exist yet) and resolves arrow hops via the active backend's
  :meth:`Backend.check_access` on the (real) target rows.

The walker is the single source of truth for operator precedence,
sub-permission cycle detection, depth bookkeeping, the tri-state
``OR/AND/MINUS`` combinators, and the ``anonymous`` / ``authenticated``
built-in actor terms. Backends do not re-implement any of these.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..errors import PermissionDepthExceeded
from ..types import SubjectRef
from .ast import (
    BUILTIN_ACTOR_TYPES,
    AllowedSubject,
    Definition,
    PermArrow,
    PermBinOp,
    PermExpr,
    Permission,
    PermNil,
    PermRef,
    Relation,
    Schema,
)


class ResolveRelation(Protocol):
    """Resolve ``subject ∈ <relation> on (definition, resource_id)``?"""

    def __call__(
        self,
        ctx: WalkContext,
        definition: Definition,
        resource_id: str,
        relation: str,
        depth: int,
    ) -> bool | None: ...


class ResolveArrow(Protocol):
    """Resolve ``∃ row of <via> whose subject has <target> permission``?"""

    def __call__(
        self,
        ctx: WalkContext,
        definition: Definition,
        resource_id: str,
        via: str,
        target: str,
        depth: int,
    ) -> bool | None: ...


@dataclass(slots=True)
class WalkContext:
    """State threaded through the recursive walker.

    ``missing`` accumulates the union of caveat-parameter names callers
    failed to supply (populated whenever a branch returns ``None``).
    Callers surface ``CheckResult.conditional(missing=...)`` once the
    top-level walk settles.
    """

    schema: Schema
    subject: SubjectRef
    context: dict[str, Any] | None
    missing: set[str]
    depth_limit: int
    resolve_relation: ResolveRelation
    resolve_arrow: ResolveArrow


def eval_expr(
    expr: PermExpr,
    *,
    definition: Definition,
    resource_id: str,
    depth: int,
    ctx: WalkContext,
    seen: frozenset[tuple[str, str]] = frozenset(),
) -> bool | None:
    """Tri-state evaluation of ``expr`` on ``(definition, resource_id)``.

    Returns ``True`` for unconditional allow, ``False`` for unconditional
    deny, and ``None`` when at least one branch is conditional on caveat
    parameters not yet supplied (the union of required names is
    accumulated in ``ctx.missing``).

    Depth is checked on entry against ``ctx.depth_limit``. Binary
    operators don't increment depth — they're tree shape, not dispatch
    hops. Arrow walks and subject-set traversals do; the callbacks
    typically pass ``depth + 1`` when they recurse into another type.
    """
    if depth > ctx.depth_limit:
        raise PermissionDepthExceeded(f"Depth limit {ctx.depth_limit} exceeded")
    if isinstance(expr, PermNil):
        return False
    if isinstance(expr, PermRef):
        if expr.name in BUILTIN_ACTOR_TYPES:
            return builtin_actor_matches(expr.name, ctx.subject)
        if find_relation(definition, expr.name) is not None:
            return ctx.resolve_relation(ctx, definition, resource_id, expr.name, depth)
        # Sub-permission reference on the same definition. Guard against
        # mutually-recursive permission refs (`permission a = b; permission b = a`).
        key = (definition.resource_type, expr.name)
        if key in seen:
            return False
        sub_perm = find_permission(definition, expr.name)
        if sub_perm is None:
            return False
        return eval_expr(
            sub_perm.expression,
            definition=definition,
            resource_id=resource_id,
            depth=depth,
            ctx=ctx,
            seen=seen | {key},
        )
    if isinstance(expr, PermArrow):
        if find_relation(definition, expr.via) is None:
            return False
        return ctx.resolve_arrow(ctx, definition, resource_id, expr.via, expr.target, depth)
    if isinstance(expr, PermBinOp):
        left = eval_expr(
            expr.left,
            definition=definition,
            resource_id=resource_id,
            depth=depth,
            ctx=ctx,
            seen=seen,
        )
        if expr.op == "+":
            if left is True:
                return True
            right = eval_expr(
                expr.right,
                definition=definition,
                resource_id=resource_id,
                depth=depth,
                ctx=ctx,
                seen=seen,
            )
            return tri_or(left, right)
        if expr.op == "&":
            if left is False:
                return False
            right = eval_expr(
                expr.right,
                definition=definition,
                resource_id=resource_id,
                depth=depth,
                ctx=ctx,
                seen=seen,
            )
            return tri_and(left, right)
        if expr.op == "-":
            if left is False:
                return False
            right = eval_expr(
                expr.right,
                definition=definition,
                resource_id=resource_id,
                depth=depth,
                ctx=ctx,
                seen=seen,
            )
            return tri_minus(left, right)
        raise ValueError(f"unknown operator: {expr.op}")
    raise TypeError(f"unknown PermExpr: {expr!r}")


# ---------- AST queries ----------


def find_relation(definition: Definition, name: str) -> Relation | None:
    for r in definition.relations:
        if r.name == name:
            return r
    return None


def find_permission(definition: Definition, name: str) -> Permission | None:
    for p in definition.permissions:
        if p.name == name:
            return p
    return None


def field_gated_actions(definition: Definition, verb: str) -> frozenset[str]:
    """Return permission names of the form ``<verb>__<field>``.

    ``write`` callers use the full action names to preserve the historical
    ``write__title`` comparison, while read callers strip the same prefix back
    to model field names at the projection boundary.
    """
    prefix = f"{verb}__"
    return frozenset(p.name for p in definition.permissions if p.name.startswith(prefix))


def subject_allowed_by_relation(relation: Relation, subject: SubjectRef) -> bool:
    """Return whether ``subject`` matches a relation's declared type union."""
    return any(_subject_matches_allowed(allowed, subject) for allowed in relation.allowed_subjects)


def relationship_row_allowed_by_relation(relation: Relation, row: object) -> bool:
    subject = SubjectRef.of(
        getattr(row, "subject_type", ""),
        getattr(row, "subject_id", ""),
        getattr(row, "optional_subject_relation", ""),
    )
    return subject_allowed_by_relation(relation, subject)


def _subject_matches_allowed(allowed: AllowedSubject, subject: SubjectRef) -> bool:
    if subject.subject_type != allowed.type:
        return False
    if allowed.wildcard:
        return subject.subject_id == "*" and subject.optional_relation == ""
    if subject.subject_id == "*":
        return False
    if allowed.id and subject.subject_id != allowed.id:
        return False
    if allowed.relation:
        return subject.optional_relation == allowed.relation
    return subject.optional_relation == ""


def builtin_actor_matches(name: str, subject: SubjectRef) -> bool:
    """Match the bare schema keywords ``anonymous`` / ``authenticated``.

    Delegates the anonymous shape to :func:`rebac.actors.is_anonymous_actor`
    so the actor layer and the engine cannot desynchronize if a future
    change tightens what "anonymous" means.
    """
    from ..actors import is_anonymous_actor

    anonymous = is_anonymous_actor(subject)
    if name == "anonymous":
        return anonymous
    if name == "authenticated":
        # Anything that isn't the anonymous singleton — including
        # subject-set rows (``auth/group:eng#member``) and other wildcard
        # subjects — counts as authenticated. The id check guards against
        # degenerate empty-string ids.
        return not anonymous and subject.subject_id != ""
    return False


# ---------- Tri-state operators ----------
#
# ``None`` means "conditional on caveat params not yet supplied" —
# short-circuit where possible (``True`` absorbs OR, ``False`` absorbs AND)
# and propagate ``None`` up to the caller otherwise. Mirrors SpiceDB's
# caveat semantics.


def tri_or(left: bool | None, right: bool | None) -> bool | None:
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


def tri_and(left: bool | None, right: bool | None) -> bool | None:
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def tri_minus(left: bool | None, right: bool | None) -> bool | None:
    # `a - b` ≡ `a AND NOT b`. None on the left absorbs through AND when the
    # right side denies; otherwise we don't know the answer.
    if left is False:
        return False
    if left is None and right is True:
        return False  # whatever 'left' resolves to, '- True' kills it.
    if left is None:
        return None
    # left is True
    if right is True:
        return False
    if right is False:
        return True
    return None


__all__ = [
    "ResolveArrow",
    "ResolveRelation",
    "WalkContext",
    "builtin_actor_matches",
    "eval_expr",
    "find_permission",
    "find_relation",
    "relationship_row_allowed_by_relation",
    "subject_allowed_by_relation",
    "tri_and",
    "tri_minus",
    "tri_or",
]
