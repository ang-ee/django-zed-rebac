"""Resource / subject id-attribute resolution.

Single source of truth for *which* attribute the engine reads when
building a resource_id (signals + manager) or a subject_id (the
``to_subject_ref`` Django-User / Group branches).

Every helper accepts a model class **or** a model instance and reads only
``._meta``. Pass the instance when you hold one: ``instance._meta`` resolves
through Django's ``SimpleLazyObject`` (``request.user``) to the wrapped
model's metadata, whereas ``type(instance)`` is the wrapper class and has no
``_meta``. Class-level callers (signal ``sender``, ``queryset.model``) pass
the class.

Resolution order, narrowest to broadest:

1. Per-model ``Meta.rebac_id_attr`` — recognised by
   :class:`RebacModelBase` and re-attached onto ``cls._meta``.
2. The corresponding global setting (``REBAC_RESOURCE_ID_ATTR`` for
   resources and registered model subjects, ``REBAC_USER_ID_ATTR`` for legacy
   User/Group subjects without resource metadata).
3. ``"pk"`` — the historical default; kept so existing consumers
   behave identically without opt-in.
"""

from __future__ import annotations

from typing import Any

from .conf import app_settings


def type_with_prefix(rebac_type: str) -> str:
    """Return the configured wire type for an unprefixed declaration."""

    prefix = str(app_settings.REBAC_TYPE_PREFIX or "")
    return f"{prefix}{rebac_type}" if prefix else rebac_type


def resource_id_attr(model_or_instance: Any) -> str:
    """Return the attribute name used to source a resource's id.

    Accepts a model class or instance; see the module docstring.
    """
    attr = getattr(model_or_instance._meta, "rebac_id_attr", None)
    return str(attr or app_settings.REBAC_RESOURCE_ID_ATTR)


def subject_id_attr(model_or_instance: Any) -> str:
    """Return the attribute name used to source a subject's id.

    Accepts a model class or instance; see the module docstring.
    A model declaring ``Meta.rebac_resource_type`` has one object identity, so
    it follows :func:`resource_id_attr`, including its resource-setting
    fallback. Legacy User/Group models without resource metadata retain the
    actor-side ``REBAC_USER_ID_ATTR`` fallback.
    """
    attr = getattr(model_or_instance._meta, "rebac_id_attr", None)
    if attr:
        return str(attr)
    if getattr(model_or_instance._meta, "rebac_resource_type", None):
        return resource_id_attr(model_or_instance)
    return str(app_settings.REBAC_USER_ID_ATTR)


def subject_relation(model_or_instance: Any) -> str:
    """Return the optional subject-set relation declared by a Django model.

    Accepts a model class or instance; see the module docstring.
    """

    relation = getattr(model_or_instance._meta, "rebac_subject_relation", "")
    return str(relation or "")


__all__ = ["resource_id_attr", "subject_id_attr", "subject_relation", "type_with_prefix"]
