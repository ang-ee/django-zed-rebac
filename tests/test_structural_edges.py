"""``structural.edges()`` lists exactly what ``structural.delivers`` delivers.

For every installed model with a resource type and every foreign key or one-to-one field it
exposes (its own, one inherited through multi-table inheritance, one seen through a proxy, a
``to_field`` key, a self-referential key, a parent link), and every permission of the field's
target type, a child query bound to one parent is offered to ``delivers`` inside a delivery that
admitted that parent under the permission. The set delivered equals the set ``edges()`` lists,
for the children's read action and for two others.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.apps import apps
from django.db import models

from rebac import SubjectRef, structural
from rebac.resources import model_resource_type
from rebac.testing import install_schema

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
MEMBER = SubjectRef.of("auth/user", "member")

PARENT = """
    relation viewer: auth/user
    relation editor: auth/user
    permission read = viewer
    permission browse = viewer
    permission other = editor
"""


def child(relations: dict[str, tuple[str, str]]) -> str:
    """Relations backed by the named fields, with ``read`` and ``browse`` through each."""
    lines = [
        f"    relation {name}: {target} // rebac:field={field}"
        for name, (field, target) in relations.items()
    ]
    reads = " + ".join(f"{name}->read" for name in relations)
    browses = " + ".join(f"{name}->browse" for name in relations)
    return (
        "\n".join(lines)
        + "\n    relation viewer: auth/user\n    relation editor: auth/user\n"
        + f"    permission read = {reads} + viewer\n"
        + f"    permission browse = {browses} + viewer\n"
        + "    permission other = editor\n"
    )


PTR = "nativeparentlinkedresource_ptr"
SCHEMA = f"""
definition auth/user {{}}
definition scope/thread {{{PARENT}}}
definition scope/message {{
{child({"thread": ("thread", "scope/thread")})}}}
definition scope/part {{
{child({"message": ("message", "scope/message")})}}}
definition scope/profile {{
{child({"owner": ("owner", "scope/message")})}}}
definition blog/folder {{
{child({"parent": ("parent", "blog/folder")})}}}
definition blog/post {{
{child({"folder": ("folder", "blog/folder")})}}}
definition test/virtualfolder {{
{child({"parent": ("parent", "blog/folder")})}}}
definition test/virtualpost {{
{child({"folder": ("folder", "blog/folder")})}}}
definition blog/sluggedpost {{{PARENT}}}
definition test/slugreference {{
{child({"target": ("target", "blog/sluggedpost")})}}}
definition test/nativeparentlinkedresource {{{PARENT}}}
definition test/nativeparentlinkedchild {{
{child({"ptr": (PTR, "test/nativeparentlinkedresource")})}}}
definition test/nativeparentlinkedrecord {{
{child({"child": ("child", "test/nativeparentlinkedchild")})}}}
definition test/nativeparentlinkedbranch {{
{child({"parent": ("parent", "test/nativeparentlinkedbranch"), "ptr": (PTR, "test/nativeparentlinkedresource")})}}}
definition test/nativeparentlinkednote {{
{child({"branch": ("branch", "test/nativeparentlinkedbranch")})}}}
"""


def delivered(child_action: str | None) -> set[tuple[Any, ...]]:
    """Every (child, key, parent, child action, parent action) a running delivery delivers."""
    policy = structural.current_policy()
    found: set[tuple[Any, ...]] = set()
    for model in apps.get_models():
        if not model_resource_type(model):
            continue
        action = child_action or str(getattr(model._meta, "rebac_default_action", "read"))
        for field in model._meta.get_fields():
            if not isinstance(field, models.ForeignKey):
                continue
            parent = field.related_model
            if not isinstance(parent, type):
                continue
            parent_type = model_resource_type(parent)
            definition = policy.schema.get_definition(parent_type) if parent_type else None
            names = [p.name for p in definition.permissions] if definition else ["read"]
            query = model._base_manager.filter(**{f"{field.name}__in": [1]}).query
            for name in names:
                row = parent(pk=1) if field.target_field.primary_key else parent()
                row._state.db = "default"
                with structural.delivering(MEMBER, parent, name, [row]):
                    if structural.delivers(query, model, MEMBER, action, using="default"):
                        found.add((model, field.name, parent, action, name))
    return found


@pytest.mark.parametrize("child_action", [None, "browse", "other"])
def test_the_edges_listed_are_exactly_the_edges_delivered(child_action):
    install_schema(SCHEMA)
    listed = {
        (edge.child_model, edge.fk_name, edge.parent_model, edge.child_action, name)
        for edge in structural.edges(child_action=child_action)
        for name in edge.parent_actions
    }
    assert delivered(child_action) == listed
    keys = {(child_model.__name__, key) for child_model, key, *_ in listed}
    if child_action != "other":
        # Not vacuous: forward keys, a parent link and a key below one. A self-referential key
        # (``Folder.parent``) is an edge inside a recursion, delivered nowhere, listed nowhere.
        assert {
            ("ScopeMessage", "thread"),
            ("Post", "folder"),
            ("NativeParentLinkedChild", PTR),
            ("NativeParentLinkedRecord", "child"),
        } <= keys, keys
        assert ("Folder", "parent") not in keys


def test_edges_keeps_no_proof_and_reads_the_kept_ones():
    """It proves every pair afresh and keeps none: the proofs running deliveries keep are not
    pushed out of the 4,096-entry cache by it."""
    install_schema(SCHEMA)
    structural._PROOFS.clear()
    first = structural.edges()
    assert structural._PROOFS == {}
    assert first and structural.edges() == first
