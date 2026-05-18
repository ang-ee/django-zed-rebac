"""RebacBackend — Django auth backend that routes ``has_perm`` through REBAC.

Two distinct call shapes the backend has to answer:

- **Object-level** (``has_perm("auth.change_user", obj)``) — admin row
  edit, DRF object permission, anywhere code can name a specific row.
  Resolves ``obj`` to an :class:`ObjectRef` and calls
  ``backend.has_access(subject, action, resource)``.

- **Model-level** (``has_perm("auth.change_user")`` with no ``obj``) —
  admin changelist, "Add" button, app index. The Django convention
  here is "does this user have *any* row of this type to act on?".
  We translate ``"<app>.<verb>_<model>"`` into a target resource type
  via ``apps.get_model(...)._meta.rebac_resource_type`` and call
  ``backend.has_access`` with an empty ``resource_id`` —
  :class:`LocalBackend` already treats that as a non-empty
  ``accessible(...)`` probe (see ``backends/local.py § check_access``).

``has_module_perms`` walks the app's models and short-circuits on the
first non-empty ``accessible()`` result, matching the Django
convention "any permission in this app at all".

Add to :setting:`AUTHENTICATION_BACKENDS` *before* ``ModelBackend`` —
``has_perm`` returns ``True`` short-circuits the chain, ``False`` lets
the next backend (typically ``ModelBackend``) try.

**Defer-vs-deny note.** Django's auth backend protocol can't tell
"I deny this perm" from "I have no opinion on this perm" — both are
``False``. This backend uses ``False`` for both, matching how
``ModelBackend`` behaves. Distinguishing requires the next backend in
``AUTHENTICATION_BACKENDS`` to give a different answer.
"""

from __future__ import annotations

from typing import Any

from django.apps import apps as django_apps

from ..codenames import codename_to_action
from ..conf import app_settings
from ..errors import PermissionDepthExceeded
from ..types import ObjectRef


class RebacBackend:
    """Adds REBAC-routed permission checks to the Django auth chain.

    ``authenticate()`` returns ``None`` so this backend never claims
    user identity — it composes with whatever auth backend (model,
    OAuth, SAML, …) the project uses for sign-in.
    """

    def authenticate(self, request: Any, **credentials: Any) -> None:
        return None

    def get_user(self, user_id: int) -> Any:
        # Identity resolution belongs to whichever backend handled
        # ``authenticate``; returning ``None`` lets the auth pipeline
        # ignore us when reconstituting a session user.
        return None

    # ---------- Permission resolution ----------

    def has_perm(self, user_obj: Any, perm: str, obj: Any = None) -> bool:
        if not getattr(user_obj, "is_active", False):
            return False
        if app_settings.REBAC_SUPERUSER_BYPASS and getattr(user_obj, "is_superuser", False):
            return True

        action = codename_to_action(perm)
        if action is None:
            return False

        from ..actors import to_subject_ref
        from ..errors import NoActorResolvedError
        from ..resources import to_object_ref
        from . import backend

        try:
            subject = to_subject_ref(user_obj)
        except NoActorResolvedError:
            return False

        if obj is not None:
            try:
                resource = to_object_ref(obj)
            except TypeError:
                return False
        else:
            resource = _model_level_resource_for_perm(perm)
            if resource is None:
                return False

        # ``PermissionDepthExceeded`` from a misconfigured (cyclic)
        # schema would otherwise crash admin / DRF render. Translate
        # to a deny — the engine couldn't answer the question, treat
        # that as "no access" rather than 500.
        try:
            return backend().has_access(subject=subject, action=action, resource=resource)
        except PermissionDepthExceeded:
            return False

    def has_module_perms(self, user_obj: Any, app_label: str) -> bool:
        if not getattr(user_obj, "is_active", False):
            return False
        if app_settings.REBAC_SUPERUSER_BYPASS and getattr(user_obj, "is_superuser", False):
            return True

        from ..actors import to_subject_ref
        from ..errors import NoActorResolvedError

        try:
            subject = to_subject_ref(user_obj)
        except NoActorResolvedError:
            return False

        try:
            cfg = django_apps.get_app_config(app_label)
        except LookupError:
            return False

        rebac_types = [
            getattr(model._meta, "rebac_resource_type", None)
            for model in cfg.get_models(include_auto_created=False)
        ]
        rebac_types = [t for t in rebac_types if t]
        if not rebac_types:
            return False

        # Cheap "any access?" probe: the user has module perms iff
        # at least one Relationship row names them as subject for one
        # of this app's resource types. Skips schema walking entirely.
        # Trade-off: overshoots when a stale grant references a type
        # whose schema no longer authorises ``read`` — for the
        # admin-sidebar use case overshoot is fine (the per-row
        # ``has_perm`` still gates the actual page render).
        from ..models import active_relationship_model

        return (
            active_relationship_model()
            .objects.filter(
                subject_type=subject.subject_type,
                subject_id=subject.subject_id,
                resource_type__in=rebac_types,
            )
            .exists()
        )


def _model_level_resource_for_perm(perm: str) -> ObjectRef | None:
    """Translate ``"<app>.<verb>_<model>"`` to an empty-id ObjectRef.

    Returns ``None`` when the perm string can't be parsed, the model
    isn't registered, or the model lacks a ``rebac_resource_type`` —
    in any of those cases the backend defers to the next entry in
    ``AUTHENTICATION_BACKENDS`` rather than answering authoritatively.

    The empty ``resource_id`` is the contract documented in
    :meth:`LocalBackend.check_access`: "model-level check (any row of
    this type the subject has the action on)".
    """
    if "." not in perm:
        return None
    app_label, codename = perm.split(".", 1)
    if "_" not in codename:
        return None
    _, model_name = codename.split("_", 1)
    try:
        model = django_apps.get_model(app_label, model_name)
    except LookupError:
        return None
    except ValueError:
        return None
    rebac_type = _resource_type_for_model(model)
    if rebac_type is None:
        return None
    return ObjectRef(rebac_type, "")


def _resource_type_for_model(model: type) -> str | None:
    """Return the model's ``Meta.rebac_resource_type`` if declared.

    Falls back to ``None`` (caller defers to other backends) rather
    than synthesising a default — convention-driven defaults would
    break silently against a schema that uses different names.
    """
    return getattr(model._meta, "rebac_resource_type", None) or None
