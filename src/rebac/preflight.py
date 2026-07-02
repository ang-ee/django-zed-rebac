"""Permission preflight against not-yet-persisted resources.

Auto-CRUD create mutations need to authorise a row *before* it exists.
The permission expression on the resource type may reference relations
that the new row would have once created — e.g.::

    definition blog/post {
        relation vault: blog/vault
        permission create = vault->write
    }

There are no ``Relationship`` rows on ``blog/post:<id>`` yet, so the
normal :meth:`Backend.check_access` short-circuits to deny. Instead,
the caller supplies the relations the row *would* carry, and
:func:`check_new` evaluates the permission expression against that
in-memory overlay using the shared
:func:`rebac.schema.walker.eval_expr` walker. Arrow hops cross into
the (real) target resources via :meth:`Backend.check_access` — so all
post-hop evaluation reuses the canonical backend semantics; only the
top-level lookups are virtual.

The walker is tri-state, so caveat-conditional results on real arrow
targets propagate cleanly:

* ``CheckResult.conditional(missing=...)`` on a hop's target is
  surfaced through the AST and emerges as a top-level
  ``CONDITIONAL_PERMISSION`` with the union of missing parameter
  names, matching SpiceDB's contract.

Limitations (v0.4):

* Caveats on the **top-level virtual tuples** are not supported — the
  ``relationships`` overlay is a bare ``SubjectRef`` sequence with no
  caveat context. Caveat-conditional ``create`` permissions remain
  evaluated through :meth:`Backend.check_access` for the *post-hop*
  targets only.
* SpiceDB-style backends don't ship a "check with proposed tuples"
  RPC. The cleanest production strategy when 0.5 SpiceDB support
  lands is: open a sub-transaction, ``WriteRelationships`` for the
  proposed tuples, ``CheckPermission`` on the (now-real) row, then
  roll back. Until that ships, :func:`check_new` raises ``RuntimeError``
  if the active backend's :meth:`schema` raises ``NotImplementedError``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from .conf import app_settings
from .errors import PermissionDepthExceeded, SchemaError
from .schema.ast import ConstBinding
from .schema.walker import (
    WalkContext,
    eval_expr,
    find_permission,
    find_relation,
    subject_allowed_by_relation,
)
from .types import CheckResult, ObjectRef, PermissionResult, SubjectRef

if TYPE_CHECKING:  # pragma: no cover
    from .backends.base import Backend
    from .schema.ast import Definition, Schema

# The new row doesn't exist yet — eval_expr threads a resource_id through
# its dispatcher but the preflight callbacks never query it (relations are
# virtual; arrows route through the backend on the real target's id).
_VIRTUAL_RESOURCE_ID = ""


def check_new(
    *,
    subject: SubjectRef,
    action: str,
    resource_type: str,
    relationships: Mapping[str, Sequence[SubjectRef]] | None = None,
    backend: Backend | None = None,
    context: dict[str, object] | None = None,
) -> CheckResult:
    """Check whether ``subject`` may perform ``action`` on a not-yet-persisted
    resource of ``resource_type``, given the relations it would carry.

    ``relationships`` maps relation name → the subjects the new row would
    point at via that relation. Empty / missing relation names are treated
    as "no row" — exactly the persisted semantics.

    Returns a three-state :class:`CheckResult`:

    * ``HAS_PERMISSION`` — every required path resolved True.
    * ``NO_PERMISSION`` — every path resolved False (or the row carries
      no qualifying relations / built-in actor terms).
    * ``CONDITIONAL_PERMISSION`` — at least one path (typically an arrow
      hop into a caveat-bound real row) is conditional on caveat
      parameters not yet supplied. ``conditional_on`` carries the union
      of missing parameter names.

    ``action`` may name either a declared permission or — for direct
    membership checks — a declared relation. An unknown name yields a
    diagnostic ``NO_PERMISSION``.
    """
    from .backends import backend as _current_backend

    rels: Mapping[str, Sequence[SubjectRef]] = relationships or {}
    active_backend = backend if backend is not None else _current_backend()

    try:
        schema = active_backend.schema()
    except NotImplementedError as exc:
        raise RuntimeError(
            f"check_new requires a backend that implements schema() "
            f"(got {type(active_backend).__name__}). SpiceDB-style backends "
            "will need a 'write-then-rollback' strategy or a server-side "
            "preflight RPC; see rebac/preflight.py module docstring."
        ) from exc

    definition = schema.get_definition(resource_type)
    if definition is None:
        return CheckResult.no(reason=f"unknown resource type: {resource_type}")

    permission = find_permission(definition, action)
    relation = find_relation(definition, action)
    if permission is None and relation is None:
        return CheckResult.no(reason=f"unknown action: {resource_type}#{action}")

    rels = _merge_const_backed_relationships(definition, rels)

    missing: set[str] = set()
    ctx = _build_ctx(
        backend=active_backend,
        schema=schema,
        subject=subject,
        context=context,
        missing=missing,
        relationships=rels,
    )

    if permission is not None:
        verdict = eval_expr(
            permission.expression,
            definition=definition,
            resource_id=_VIRTUAL_RESOURCE_ID,
            depth=0,
            ctx=ctx,
        )
    else:
        # Direct-relation fallback: ``action`` names a relation rather than a
        # permission. The check is "is the subject one of the candidates the
        # new row would carry via that relation?". Same dispatch the walker
        # uses internally for PermRef-of-relation, just no enclosing expr.
        verdict = ctx.resolve_relation(ctx, definition, _VIRTUAL_RESOURCE_ID, action, 0)

    if verdict is True:
        return CheckResult.has()
    if verdict is None:
        return CheckResult.conditional(missing=tuple(sorted(missing)))
    return CheckResult.no()


def _merge_const_backed_relationships(
    definition: Definition,
    relationships: Mapping[str, Sequence[SubjectRef]],
) -> Mapping[str, Sequence[SubjectRef]]:
    """Inject schema-owned const relation subjects into the virtual overlay."""
    merged: dict[str, tuple[SubjectRef, ...]] = {
        name: tuple(subjects) for name, subjects in relationships.items()
    }
    for relation in definition.relations:
        backing = relation.backing
        if not isinstance(backing, ConstBinding):
            continue
        supplied = merged.get(relation.name, ())
        if supplied:
            raise SchemaError(
                f"{definition.resource_type}#{relation.name} is const-backed; "
                "check_new callers must not supply virtual tuples for synthetic relations"
            )
        if len(relation.allowed_subjects) != 1:
            continue
        allowed = relation.allowed_subjects[0]
        merged[relation.name] = (SubjectRef.of(allowed.type, backing.target_id),)
    return merged


def _build_ctx(
    *,
    backend: Backend,
    schema: Schema,
    subject: SubjectRef,
    context: dict[str, object] | None,
    missing: set[str],
    relationships: Mapping[str, Sequence[SubjectRef]],
) -> WalkContext:
    """Construct a :class:`WalkContext` whose callbacks resolve against the
    caller-supplied virtual relationships and the active backend.

    The closures capture ``relationships`` and ``backend`` so the walker
    doesn't have to know about either.
    """

    def resolve_relation(
        ctx: WalkContext,
        definition: Definition,
        resource_id: str,
        relation: str,
        depth: int,
    ) -> bool | None:
        del resource_id  # virtual — relation lookup is dict-only
        relation_def = find_relation(definition, relation)
        if relation_def is None:
            return False
        candidates = [
            candidate
            for candidate in relationships.get(relation, ())
            if subject_allowed_by_relation(relation_def, candidate)
        ]
        return _virtual_membership(
            ctx=ctx,
            backend=backend,
            candidates=candidates,
            depth=depth,
        )

    def resolve_arrow(
        ctx: WalkContext,
        definition: Definition,
        resource_id: str,
        via: str,
        target: str,
        depth: int,
    ) -> bool | None:
        del resource_id  # virtual — arrow walks the dict, not rows
        via_relation = find_relation(definition, via)
        if via_relation is None:
            return False
        candidates = [
            candidate
            for candidate in relationships.get(via, ())
            if subject_allowed_by_relation(via_relation, candidate)
        ]
        if not candidates:
            return False
        # Each arrow hop is a dispatch into another (real) resource, so
        # increments depth — mirrors LocalBackend's `_walk_resolve_arrow`.
        new_depth = depth + 1
        if new_depth > ctx.depth_limit:
            raise PermissionDepthExceeded(f"Depth limit {ctx.depth_limit} exceeded")
        saw_conditional = False
        for target_subject in candidates:
            result = backend.check_access(
                subject=ctx.subject,
                action=target,
                resource=ObjectRef(target_subject.subject_type, target_subject.subject_id),
                context=ctx.context,
            )
            if result.result is PermissionResult.HAS_PERMISSION:
                return True
            if result.result is PermissionResult.CONDITIONAL_PERMISSION:
                ctx.missing.update(result.conditional_on)
                saw_conditional = True
        if saw_conditional:
            return None
        return False

    return WalkContext(
        schema=schema,
        subject=subject,
        context=context,
        missing=missing,
        depth_limit=app_settings.REBAC_DEPTH_LIMIT,
        resolve_relation=resolve_relation,
        resolve_arrow=resolve_arrow,
    )


def _virtual_membership(
    *,
    ctx: WalkContext,
    backend: Backend,
    candidates: Sequence[SubjectRef],
    depth: int,
) -> bool | None:
    """Tri-state membership lookup against a virtual list of subjects.

    A candidate matches when:

    * it equals the actor exactly, or
    * it is a ``<type>:*`` wildcard for the actor's type (only valid for
      direct, non-subject-set actors — mirrors
      ``LocalBackend._has_direct_relation`` wildcard handling), or
    * it is a subject-set ref like ``auth/group:eng#member`` and the
      actor has ``member`` on ``auth/group:eng`` per the active backend.

    The subject-set hop costs one dispatch level. Anything beyond the
    depth limit raises :class:`PermissionDepthExceeded`.
    """
    saw_conditional = False
    for candidate in candidates:
        verdict = _candidate_matches(ctx=ctx, backend=backend, candidate=candidate, depth=depth)
        if verdict is True:
            return True
        if verdict is None:
            saw_conditional = True
    if saw_conditional:
        return None
    return False


def _candidate_matches(
    *,
    ctx: WalkContext,
    backend: Backend,
    candidate: SubjectRef,
    depth: int,
) -> bool | None:
    subject = ctx.subject
    if (
        candidate.subject_type == subject.subject_type
        and candidate.subject_id == subject.subject_id
        and candidate.optional_relation == subject.optional_relation
    ):
        return True
    if (
        not subject.optional_relation
        and not candidate.optional_relation
        and candidate.subject_type == subject.subject_type
        and candidate.subject_id == "*"
    ):
        return True
    if not candidate.optional_relation:
        return False
    # Subject-set candidate: walk via the backend on the (real) target row.
    new_depth = depth + 1
    if new_depth > ctx.depth_limit:
        raise PermissionDepthExceeded(f"Depth limit {ctx.depth_limit} exceeded")
    result = backend.check_access(
        subject=subject,
        action=candidate.optional_relation,
        resource=ObjectRef(candidate.subject_type, candidate.subject_id),
        context=ctx.context,
    )
    if result.result is PermissionResult.HAS_PERMISSION:
        return True
    if result.result is PermissionResult.CONDITIONAL_PERMISSION:
        ctx.missing.update(result.conditional_on)
        return None
    return False


__all__ = ["check_new"]
