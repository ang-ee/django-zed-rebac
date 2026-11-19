"""Stand-in Django models for an exported policy skeleton (``rebac.skeleton``).

A stand-in has the original's table name, key, relations (with the names a
lookup path uses) and the columns the policy's filters read, and nothing else.
The policy resolves against it exactly as against the original.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.db import models

from rebac import RebacMixin

POLICIES = Path(__file__).resolve().parent / "policies"
CONTENT_TYPE = "contenttypes.ContentType"


def load(name: str) -> tuple[dict[str, Any], str]:
    """``(skeleton, policy text)`` of the exported policy ``name``."""
    directory = POLICIES / name
    return json.loads((directory / "skeleton.json").read_text()), (
        directory / "policy.zed"
    ).read_text()


def class_name(label: str) -> str:
    app, model = label.split(".", 1)
    return "".join(part.capitalize() for part in app.split("_")) + model.replace("_", "")


def build(
    skeleton: dict[str, Any], *, app_label: str, module: str
) -> dict[str, type[models.Model]]:
    """Create one model per skeleton entry; returns them by the original's label."""
    described: dict[str, Any] = skeleton["models"]
    built: dict[str, type[models.Model]] = {}

    def reference(label: str, source: str) -> str:
        if label == CONTENT_TYPE:
            return label
        return "self" if label == source else f"{app_label}.{class_name(label)}"

    def key_of(label: str) -> str | None:
        return next((f["name"] for f in described[label]["fields"] if f.get("pk")), None)

    def reverse(field: dict[str, Any], model_name: str, single: bool) -> dict[str, Any]:
        """The names the original gives the reverse side, whatever this class is called."""
        name, query = field.get("related_name"), field.get("related_query_name")
        if name and name.endswith("+"):
            # No reverse accessor; a lookup may still name the relation.
            hidden = {"related_name": name}
            return hidden if query in (None, "+", name) else {**hidden, "related_query_name": query}
        if name:
            named = {"related_name": name}
            return named if query in (None, name) else {**named, "related_query_name": query}
        if query in (None, "+"):
            return {"related_name": "+"}
        if single:
            return {"related_name": query}
        return {"related_name": f"{model_name}_set", "related_query_name": query}

    pending = [
        label
        for label, entry in described.items()
        if not entry["auto_created"] and label != CONTENT_TYPE
    ]
    while pending:
        ready = [
            label
            for label in pending
            if all(parent in built for parent in described[label]["parents"])
        ]
        if not ready:
            raise ValueError(f"Unresolvable model parents: {pending}")
        for label in ready:
            pending.remove(label)
            entry = described[label]
            model_name = label.split(".", 1)[1].lower()
            attrs: dict[str, Any] = {"__module__": module}
            meta: dict[str, Any] = {
                "app_label": app_label,
                "db_table": entry["db_table"],
                "managed": entry["managed"],
            }
            if entry["resource_type"]:
                meta["rebac_resource_type"] = entry["resource_type"]
                if entry["id_attr"] != "pk":
                    meta["rebac_id_attr"] = entry["id_attr"]
                for option in ("default_action", "subject_relation"):
                    if entry[option]:
                        meta[f"rebac_{option}"] = entry[option]
            inherited = {
                field["name"]
                for parent in entry["parents"]
                for field in described[parent]["fields"]
            }
            for field in entry["fields"]:
                kind, name = field["kind"], field["name"]
                if name in inherited:
                    continue  # the multi-table parent declares it
                if kind in ("foreign_key", "one_to_one"):
                    single = kind == "one_to_one"
                    options: dict[str, Any] = {
                        "on_delete": models.DO_NOTHING,
                        "null": field["null"],
                        "db_column": field["column"],
                        "db_constraint": field["db_constraint"]
                        and described.get(field["to"], {"managed": True})["managed"],
                        **reverse(field, model_name, single),
                    }
                    if field["pk"]:
                        options["primary_key"] = True
                    if field["parent_link"]:
                        options["parent_link"] = True
                    if field["unique"] and not single:
                        options["unique"] = True
                    if field["to"] != CONTENT_TYPE and field["to_field"] != key_of(field["to"]):
                        options["to_field"] = field["to_field"]
                    relation = models.OneToOneField if single else models.ForeignKey
                    attrs[name] = relation(reference(field["to"], label), **options)
                elif kind == "generic_key":
                    attrs[name] = GenericForeignKey(field["ct_field"], field["fk_field"])
                elif kind == "generic_relation":
                    query = field["related_query_name"]
                    attrs[name] = GenericRelation(
                        reference(field["to"], label),
                        content_type_field=field["content_type_field"],
                        object_id_field=field["object_id_field"],
                        related_query_name=None if query in (None, "+") else query,
                    )
                elif kind == "many_to_many":
                    options = reverse(field, model_name, False)
                    if field["through"]:
                        options["through"] = reference(field["through"], label)
                    else:
                        options["db_table"] = field["db_table"]
                    attrs[name] = models.ManyToManyField(reference(field["to"], label), **options)
                else:
                    attrs[name] = _column(field)
            parents = tuple(built[parent] for parent in entry["parents"])
            bases = parents or (
                (RebacMixin, models.Model) if entry["resource_type"] else (models.Model,)
            )
            attrs["Meta"] = type("Meta", (), meta)
            built[label] = type(class_name(label), bases, attrs)
    return built


def _column(field: dict[str, Any]) -> models.Field[Any, Any]:
    kind = field["kind"]
    common: dict[str, Any] = {"db_column": field["column"], "null": field["null"]}
    if field["unique"]:
        common["unique"] = True
    if kind == "int":
        if field["pk"]:
            return models.BigAutoField(primary_key=True, db_column=field["column"])
        return models.BigIntegerField(**common)
    if kind == "text":
        if field["pk"]:
            return models.CharField(
                primary_key=True, max_length=field["max_length"] or 64, db_column=field["column"]
            )
        if field["max_length"]:
            return models.CharField(max_length=max(field["max_length"], 32), default="", **common)
        return models.TextField(default="", **common)
    if kind == "bool":
        return models.BooleanField(default=False, **common)
    if kind == "uuid":
        return models.UUIDField(primary_key=field["pk"], **({} if field["pk"] else common))
    common["null"] = True
    if kind == "datetime":
        return models.DateTimeField(**common)
    if kind == "date":
        return models.DateField(**common)
    if kind == "json":
        return models.JSONField(**common)
    if kind == "decimal":
        return models.DecimalField(max_digits=30, decimal_places=10, **common)
    if kind == "float":
        return models.FloatField(**common)
    return models.TextField(**common)
