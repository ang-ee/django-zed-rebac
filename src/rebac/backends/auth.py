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

``has_module_perms`` walks the app's REBAC models and asks the same model-level
question for each model's default action, short-circuiting on the first grant.

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

from asgiref.sync import sync_to_async
from django.apps import apps as django_apps
from django.contrib.auth.base_user import AbstractBaseUser

from ..codenames import codename_to_action
from ..conf import app_settings
from ..errors import PermissionDepthExceeded
from ..resources import model_resource_type
from ..types import ObjectRef


class RebacBackend:
    """Adds REBAC-routed permission checks to the Django auth chain.

    ``authenticate()`` returns ``None`` so this backend never claims
    user identity — it composes with whatever auth backend (model,
    OAuth, SAML, …) the project uses for sign-in.
    """

    def authenticate(self, request: Any, **credentials: Any) -> AbstractBaseUser | None:
        # This backend never claims identity — it only resolves
        # permissions. Returning ``None`` defers sign-in to whatever
        # other backend the project configures. The Django-conformant
        # ``AbstractBaseUser | None`` return type (not bare ``None``)
        # keeps callers that inspect the result type-correct.
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

        resource: ObjectRef | None
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

    async def ahas_perm(self, user_obj: Any, perm: str, obj: Any = None) -> bool:
        return await sync_to_async(self.has_perm, thread_sensitive=True)(user_obj, perm, obj)

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

        from . import backend

        for model in cfg.get_models(include_auto_created=False):
            resource_type = model_resource_type(model)
            if resource_type is None:
                continue
            action = getattr(model._meta, "rebac_default_action", "read")
            try:
                if backend().has_access(
                    subject=subject, action=action, resource=ObjectRef(resource_type, "")
                ):
                    return True
            except PermissionDepthExceeded:
                continue
        return False

    async def ahas_module_perms(self, user_obj: Any, app_label: str) -> bool:
        return await sync_to_async(self.has_module_perms, thread_sensitive=True)(
            user_obj, app_label
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
    rebac_type = model_resource_type(model)
    if rebac_type is None:
        return None
    return ObjectRef(rebac_type, "")
