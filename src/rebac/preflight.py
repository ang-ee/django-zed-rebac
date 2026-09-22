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

* Caveats on the **top-level virtual tuples** are not supported — known
  relations in the ``relationships`` overlay are ``SubjectRef`` sequences with no
  caveat name or pinned context. These tuples must match an explicitly
  uncaveated schema alternative; required-caveat alternatives fail closed.
  Caveat-conditional ``create`` permissions remain
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
from typing import TYPE_CHECKING, Any

from .conf import app_settings
from .errors import PermissionDepthExceeded, SchemaError
from .schema.ast import ConstBinding
from .schema.introspection import relation_dependencies
from .schema.walker import (
    WalkContext,
    _UnknownRelation,
    eval_expr,
    find_permission,
    find_relation,
    subject_allowed_by_relation,
)
from .types import CheckResult, ObjectRef, PermissionResult, SubjectRef

if TYPE_CHECKING:  # pragma: no cover
    from .backends.base import Backend
    from .schema.ast import Definition, Schema


def _check_new_model(
    instance: Any,
    *,
    subject: SubjectRef,
    using: str | None = None,
    backend: Backend | None = None,
) -> CheckResult:
    """Evaluate one constructed Django candidate through :func:`check_new`."""

    from .backends import backend as _current_backend
    from .field_backing import _proposed_forward_relationships
    from .resources import model_resource_type

    active_backend = backend if backend is not None else _current_backend()
    resource_type = model_resource_type(type(instance))
    if resource_type is None:
        return CheckResult.has()
    try:
        schema = active_backend.schema()
        definition = schema.get_definition(resource_type)
    except NotImplementedError:
        # Preserve check_new's backend-specific fail-closed diagnostic.
        return check_new(
            subject=subject,
            action="create",
            resource_type=resource_type,
            backend=active_backend,
        )
    relationships = (
        _proposed_forward_relationships(
            instance,
            definition,
            required_relations=relation_dependencies(schema, resource_type, "create"),
            using=using,
        )
        if definition is not None
        else {}
    )
    return check_new(
        subject=subject,
        action="create",
        resource_type=resource_type,
        relationships=relationships,
        backend=active_backend,
    )


# The new row doesn't exist yet — eval_expr threads a resource_id through
# its dispatcher but the preflight callbacks never query it (relations are
# virtual; arrows route through the backend on the real target's id).
_VIRTUAL_RESOURCE_ID = ""


def check_new(
    *,
    subject: SubjectRef,
    action: str,
    resource_type: str,
    relationships: Mapping[str, Sequence[SubjectRef] | None] | None = None,
    backend: Backend | None = None,
    context: dict[str, object] | None = None,
) -> CheckResult:
    """Check whether ``subject`` may perform ``action`` on a not-yet-persisted
    resource of ``resource_type``, given the relations it would carry.

    ``relationships`` maps relation name → the subjects the new row would
    point at via that relation. Empty / missing relation names are treated
    as "no row" — exactly the persisted semantics. A ``None`` value marks a
    relation whose subjects cannot be resolved before insertion. Any arm
    referencing that relation denies, including through intersections,
    exclusions, arrows, or sub-permissions; an independently granted union
    arm can still authorize the action. This is distinct from a caveat's
    conditional result and does not produce ``CONDITIONAL_PERMISSION``.

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

    rels: Mapping[str, Sequence[SubjectRef] | None] = relationships or {}
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
        has_unknown_relations=any(
            rels.get(name, ()) is None
            for name in relation_dependencies(schema, resource_type, action)
        ),
    )

    try:
        if permission is not None:
            verdict = eval_expr(
                permission.expression,
                definition=definition,
                resource_id=_VIRTUAL_RESOURCE_ID,
                depth=0,
                ctx=ctx,
            )
        else:
            # Direct-relation fallback shares the walker's relation callback.
            verdict = ctx.resolve_relation(ctx, definition, _VIRTUAL_RESOURCE_ID, action, 0)
    except _UnknownRelation as exc:
        return CheckResult.no(reason=f"unknown proposed relation: {exc}")

    if verdict is True:
        return CheckResult.has()
    if verdict is None:
        return CheckResult.conditional(missing=tuple(sorted(missing)))
    return CheckResult.no()


def _merge_const_backed_relationships(
    definition: Definition,
    relationships: Mapping[str, Sequence[SubjectRef] | None],
) -> Mapping[str, Sequence[SubjectRef] | None]:
    """Inject schema-owned const relation subjects into the virtual overlay."""
    merged: dict[str, tuple[SubjectRef, ...] | None] = {
        name: tuple(subjects) if subjects is not None else None
        for name, subjects in relationships.items()
    }
    for relation in definition.relations:
        backing = relation.backing
        if not isinstance(backing, ConstBinding):
            continue
        supplied = merged.get(relation.name, ())
        if supplied is None or supplied:
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
    relationships: Mapping[str, Sequence[SubjectRef] | None],
    has_unknown_relations: bool,
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
        proposed = relationships.get(relation, ())
        if proposed is None:
            raise _UnknownRelation(relation)
        candidates = [
            candidate
            for candidate in proposed
            if subject_allowed_by_relation(relation_def, candidate, caveat_name="")
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
        proposed = relationships.get(via, ())
        if proposed is None:
            raise _UnknownRelation(via)
        candidates = [
            candidate
            for candidate in proposed
            if subject_allowed_by_relation(via_relation, candidate, caveat_name="")
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
        has_unknown_relations=has_unknown_relations,
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
    target_definition = ctx.schema.get_definition(candidate.subject_type)
    if (
        target_definition is None
        or find_relation(target_definition, candidate.optional_relation) is None
    ):
        return False
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
