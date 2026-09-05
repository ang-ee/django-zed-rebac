"""DRF integration: RebacPermission + RebacFilterBackend.

Requires ``djangorestframework`` (the ``[drf]`` extra). This module is
never imported by ``rebac/__init__.py`` — only consumers wiring the DRF
integration import it, and they are expected to have DRF installed. A
missing dependency therefore surfaces as a plain ``ImportError`` naming
``rest_framework`` at the point of import, which is the correct
fail-fast behaviour.
"""

from __future__ import annotations

from typing import Any

from rest_framework.filters import BaseFilterBackend
from rest_framework.permissions import BasePermission

from .actors import current_actor, to_subject_ref
from .backends import backend
from .errors import NoActorResolvedError
from .resources import model_resource_type, to_object_ref
from .types import ObjectRef, SubjectRef

_DEFAULT_ACTION_MAP = {
    "list": "read",
    "retrieve": "read",
    "metadata": "read",
    "create": "create",
    "update": "write",
    "partial_update": "write",
    "destroy": "delete",
    "get": "read",
    "head": "read",
    "options": "read",
    "post": "create",
    "put": "write",
    "patch": "write",
    "delete": "delete",
}


class RebacPermission(BasePermission):  # type: ignore[misc]  # untyped third-party base
    """Routes per-action permission through `backend().has_access`.

    Override `action_map` to customise:
        class MyPerm(RebacPermission):
            action_map = {**RebacPermission.action_map, "publish": "publish"}
    """

    action_map = _DEFAULT_ACTION_MAP

    def _action_for_view(self, request: Any, view: Any) -> str | None:
        action_name = getattr(view, "action", None) or request.method.lower()
        action_map = {**self.action_map, **getattr(view, "rebac_action_map", {})}
        action: str | None = action_map.get(action_name)
        return action

    # pyright infers BasePermission's `return True` body as `Literal[True]`;
    # widening to `bool` is the correct override, not an incompatibility.
    def has_permission(self, request: Any, view: Any) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride]
        rebac_action = self._action_for_view(request, view)
        if rebac_action is None:
            return False

        subject = _subject_from_request(request)
        if subject is None:
            return False

        # Read admission is separate from row authorization: empty lists are
        # valid, and DRF checks concrete detail objects after queryset scoping.
        if rebac_action == "read":
            return True

        model_cls = getattr(getattr(view, "queryset", None), "model", None)
        rebac_type = model_resource_type(model_cls) if model_cls is not None else None
        if not rebac_type:
            return True

        # Non-read collection actions require the model-level permission.
        return backend().has_access(
            subject=subject,
            action=rebac_action,
            resource=ObjectRef(rebac_type, ""),
        )

    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride]
        rebac_action = self._action_for_view(request, view)
        if rebac_action is None:
            return False

        subject = _subject_from_request(request)
        if subject is None:
            return False

        try:
            resource = to_object_ref(obj)
        except TypeError:
            return True
        return backend().has_access(subject=subject, action=rebac_action, resource=resource)


class RebacFilterBackend(BaseFilterBackend):  # type: ignore[misc]  # untyped third-party base
    """Scopes a viewset's queryset to the actor."""

    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> Any:
        if not hasattr(queryset, "as_user"):
            return queryset
        subject = _subject_from_request(request)
        if subject is None:
            return queryset.none()
        return queryset.with_actor(subject)


def _subject_from_request(request: Any) -> SubjectRef | None:
    subject = current_actor()
    if subject is not None:
        return subject
    user = getattr(request, "user", None)
    try:
        return to_subject_ref(user) if user is not None else None
    except NoActorResolvedError:
        return None
