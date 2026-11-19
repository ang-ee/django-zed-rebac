"""Export a project's policy and the skeleton of the models it refers to.

A permission's cost depends on the policy and on how much data there is, not
on what the data says. So a project's policy can be measured without its data:
this module writes the effective policy (``policy.zed``) and, for every model
the policy touches, only what the policy reads of it (``skeleton.json``): its
table and key, its relations to the other such models, and the columns a
backing's filters name. No row is read.

From the two files a benchmark rebuilds stand-in models, generates rows to any
size and runs the real policy over them (the library's own ``tests/bench``).

Run it inside the project, with Django set up and the policy published::

    python manage.py shell -c "from rebac import skeleton; skeleton.export('out')"
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.apps import apps
from django.conf import settings
from django.db import models

from ._id import resource_id_attr
from .field_backing import resolve_attribute_backing, resolve_const_backing, resolve_field_backing
from .resources import model_for_resource_type, model_for_subject_type, model_resource_type
from .schema.ast import AttributeBinding, ConstBinding, FieldBinding, Schema
from .schema.rendering import render_zed

# What a stand-in needs to know of a column: its family, not its exact class.
_KINDS = {
    "AutoField": "int",
    "BigAutoField": "int",
    "SmallAutoField": "int",
    "IntegerField": "int",
    "BigIntegerField": "int",
    "SmallIntegerField": "int",
    "PositiveIntegerField": "int",
    "PositiveBigIntegerField": "int",
    "PositiveSmallIntegerField": "int",
    "CharField": "text",
    "TextField": "text",
    "SlugField": "text",
    "EmailField": "text",
    "URLField": "text",
    "FileField": "text",
    "GenericIPAddressField": "text",
    "BooleanField": "bool",
    "UUIDField": "uuid",
    "DateTimeField": "datetime",
    "DateField": "date",
    "TimeField": "time",
    "DurationField": "duration",
    "JSONField": "json",
    "DecimalField": "decimal",
    "FloatField": "float",
    "BinaryField": "binary",
}


def export(directory: str | Path, *, source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write ``policy.zed`` and ``skeleton.json`` for the active policy into ``directory``.

    ``source`` is recorded in the skeleton as given: the repository and commit
    the policy came from, for whoever reads the fixture later.
    """
    from .backends import backend

    schema = backend().schema()
    found = skeleton(schema)
    found["source"] = dict(source or {})
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    (target / "skeleton.json").write_text(json.dumps(found, indent=1, sort_keys=True) + "\n")
    (target / "policy.zed").write_text(render_zed(schema))
    return found


def skeleton(schema: Schema) -> dict[str, Any]:
    """The models ``schema`` touches, each with the fields the policy reads or follows."""
    from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation

    wanted: dict[type[models.Model], set[str]] = {}
    notes: list[str] = []

    def want(model: type[models.Model], field_name: str | None = None) -> None:
        concrete = model._meta.concrete_model if model._meta.proxy else model
        assert concrete is not None
        names = wanted.setdefault(concrete, set())
        if field_name:
            names.add(field_name)

    def walk(model: type[models.Model], lookup: str) -> None:
        """Mark every field a lookup path touches."""
        current: type[models.Model] | None = model
        for part in lookup.split("__"):
            if current is None:
                return
            try:
                field: Any = current._meta.get_field(part)
            except Exception:
                return  # a lookup suffix or a key inside a JSON column
            want(current, getattr(field, "name", part))
            if isinstance(field, GenericForeignKey):
                want(current, field.ct_field)
                want(current, field.fk_field)
                return
            related = getattr(field, "related_model", None)
            if not isinstance(related, type):
                return
            forward = getattr(field, "field", None)
            if getattr(field, "auto_created", False) and not field.concrete and forward is not None:
                # A reverse relation: the forward field lives on the related model.
                want(related, forward.name)
            through = getattr(getattr(field, "remote_field", None), "through", None)
            if isinstance(through, type):
                want(through)
            want(related)
            current = related

    for definition in schema.definitions:
        model = model_for_resource_type(definition.resource_type)
        if model is None:
            subject = model_for_subject_type(definition.resource_type)
            model = subject[0] if subject else None
        if model is None:
            continue
        want(model)
        for relation in definition.relations:
            backing = relation.backing
            where = f"{definition.resource_type}#{relation.name}"
            try:
                if isinstance(backing, FieldBinding):
                    resolved = resolve_field_backing(definition, relation)
                    if resolved is None:
                        notes.append(f"{where}: field backing unresolved")
                        continue
                    walk(resolved.source_model, backing.path)
                    want(resolved.target_model)
                    for lookup in resolved.filters:
                        walk(resolved.source_model, lookup)
                elif isinstance(backing, ConstBinding):
                    constant = resolve_const_backing(definition, relation)
                    if constant is None:
                        notes.append(f"{where}: constant backing unresolved")
                        continue
                    for lookup in constant.filters:
                        walk(constant.source_model, lookup)
                elif isinstance(backing, AttributeBinding):
                    attribute = resolve_attribute_backing(definition, relation)
                    if attribute is None:
                        notes.append(f"{where}: attribute backing unresolved")
                        continue
                    want(attribute.target_model, attribute.field.name)
                    for lookup in attribute.filters:
                        walk(attribute.target_model, lookup)
            except Exception as error:
                notes.append(f"{where}: {type(error).__name__}: {error}")

    # The parents of a multi-table child, so its key resolves.
    pending = list(wanted)
    while pending:
        model = pending.pop()
        for parent, parent_link in model._meta.parents.items():
            if parent not in wanted:
                want(parent)
                pending.append(parent)
            if parent_link is not None:
                wanted[model].add(parent_link.name)
    # A foreign key to a column that is not its target's key needs that column.
    for model in list(wanted):
        for local in model._meta.local_concrete_fields:
            target = local.related_model if local.is_relation else None
            if isinstance(target, type) and target in wanted:
                column: Any = getattr(local, "target_field", None)
                if column is not None and not column.primary_key:
                    wanted[target].add(column.name)

    described: dict[str, Any] = {}
    for model in sorted(wanted, key=lambda item: item._meta.label):
        meta = model._meta
        identity = resource_id_attr(model)
        keep = set(wanted[model])
        if meta.pk is not None:
            keep.add(meta.pk.name)
        if identity != "pk":
            keep.add(identity)
        fields: list[dict[str, Any]] = []
        inherited = {
            private.name for parent in meta.parents for private in parent._meta.private_fields
        }
        for private in meta.private_fields:
            if private.name in inherited:
                continue  # a multi-table parent declares it, and the child has it from there
            if isinstance(private, GenericForeignKey):
                keep.update({private.ct_field, private.fk_field})
                fields.append(
                    {
                        "name": private.name,
                        "kind": "generic_key",
                        "ct_field": private.ct_field,
                        "fk_field": private.fk_field,
                    }
                )
            elif isinstance(private, GenericRelation) and private.related_model in wanted:
                reverse: Any = private.remote_field
                named = reverse.related_query_name or reverse.related_name
                fields.append(
                    {
                        "name": private.name,
                        "kind": "generic_relation",
                        "to": private.related_model._meta.label,
                        "content_type_field": private.content_type_field_name,
                        "object_id_field": private.object_id_field_name,
                        "related_query_name": private.related_query_name() if named else None,
                    }
                )
        for many in meta.local_many_to_many:
            target = many.related_model
            if not isinstance(target, type) or target not in wanted:
                continue
            through: Any = many.remote_field.through
            fields.append(
                {
                    "name": many.name,
                    "kind": "many_to_many",
                    "to": target._meta.label,
                    "through": None if through._meta.auto_created else through._meta.label,
                    "db_table": through._meta.db_table,
                    "related_name": many.remote_field.related_name,
                    "related_query_name": many.related_query_name(),
                }
            )
        field: Any
        for field in meta.local_concrete_fields:
            if field.is_relation:
                related: Any = field.related_model
                related = related._meta.concrete_model if related._meta.proxy else related
                if related not in wanted:
                    if field.name in keep:
                        notes.append(
                            f"{meta.label}.{field.name}: target {related._meta.label} "
                            "is outside the skeleton"
                        )
                    continue
                remote = field.remote_field
                fields.append(
                    {
                        "name": field.name,
                        "kind": "one_to_one" if field.one_to_one else "foreign_key",
                        "to": related._meta.label,
                        "null": field.null,
                        "pk": field.primary_key,
                        "parent_link": bool(getattr(remote, "parent_link", False)),
                        "related_name": remote.related_name,
                        "related_query_name": field.related_query_name(),
                        "to_field": field.target_field.name,
                        "db_constraint": bool(getattr(field, "db_constraint", True)),
                        "unique": field.unique and not field.primary_key,
                        "column": field.column,
                    }
                )
                continue
            if field.name not in keep:
                continue
            kind = _KINDS.get(field.get_internal_type())
            if kind is None:
                notes.append(f"{meta.label}.{field.name}: column type {field.get_internal_type()}")
                kind = "text"
            fields.append(
                {
                    "name": field.name,
                    "kind": kind,
                    "null": field.null,
                    "pk": field.primary_key,
                    "unique": field.unique and not field.primary_key,
                    "max_length": getattr(field, "max_length", None),
                    "column": field.column,
                }
            )
        described[meta.label] = {
            "db_table": meta.db_table,
            "resource_type": model_resource_type(model),
            "id_attr": identity,
            "default_action": getattr(meta, "rebac_default_action", None),
            "subject_relation": getattr(meta, "rebac_subject_relation", None),
            "managed": bool(meta.managed),
            "auto_created": bool(meta.auto_created),
            "is_user": meta.label == settings.AUTH_USER_MODEL,
            "parents": [parent._meta.label for parent in meta.parents],
            "fields": fields,
        }
    proxies = {
        model._meta.label: model._meta.concrete_model._meta.label
        for model in apps.get_models()
        if model._meta.proxy
        and model._meta.concrete_model is not None
        and model_resource_type(model)
    }
    return {"models": described, "proxies": proxies, "notes": notes}
