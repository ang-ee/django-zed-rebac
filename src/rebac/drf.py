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
    "create": "create",
    "update": "write",
    "partial_update": "write",
    "destroy": "delete",
}


class RebacPermission(BasePermission):  # type: ignore[misc]  # untyped third-party base
    """Routes per-action permission through `backend().has_access`.

    Override `action_map` to customise:
        class MyPerm(RebacPermission):
            action_map = {**RebacPermission.action_map, "publish": "publish"}
    """

    action_map = _DEFAULT_ACTION_MAP

    def has_permission(self, request: Any, view: Any) -> bool:
        action_name = getattr(view, "action", None) or request.method.lower()
        rebac_action = self.action_map.get(action_name)
        if rebac_action is None:
            return True

        subject = _subject_from_request(request)
        if subject is None:
            return False

        model_cls = getattr(getattr(view, "queryset", None), "model", None)
        rebac_type = model_resource_type(model_cls) if model_cls is not None else None
        if not rebac_type:
            return True

        # Model-level check (empty resource_id) for create/list.
        return backend().has_access(
            subject=subject,
            action=rebac_action,
            resource=ObjectRef(rebac_type, ""),
        )

    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        action_name = getattr(view, "action", None) or request.method.lower()
        rebac_action = self.action_map.get(action_name)
        if rebac_action is None:
            return True

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
