"""The watch map: which model columns the backings of a schema read.

The map is resolved from model metadata alone; none of these tests reads rows.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model

from rebac.backends.local import LocalBackend
from rebac.schema.ast import (
    AllowedSubject,
    AttributeBinding,
    ConstBinding,
    Definition,
    FieldBinding,
    Relation,
    Schema,
)
from rebac.schema.parser import parse_zed
from rebac.watch import codec_fields, gate_policy, model_is_watched, watched_for

from .testapp.models import (
    AuthoredPost,
    BackingEntry,
    BackingProject,
    BackingQueue,
    BackingRound,
    BackingStage,
    BackingTask,
    Folder,
    NativeParentLinkedChild,
    NativeParentLinkedResource,
    Post,
)


def _backing_schema(type_, target, backing, *, name="member"):
    return Schema(
        definitions=[
            Definition(type_, (Relation(name, (AllowedSubject(target),), backing=backing),), ())
        ]
    )


def _assert_watch(watched, model, fields, resource_type):
    watch = watched[model._meta.label_lower]
    assert fields <= watch.fields
    assert resource_type in watch.resource_types
    assert watch.model_label == model._meta.label_lower
    assert watch.model is model
    return watch


def _policy(schema):
    active = LocalBackend()
    active.set_schema(schema)
    return gate_policy("default", active)


def test_multi_hop_and_filter_watches_include_intermediate_plain_models():
    schema = _backing_schema(
        "test/backingqueue",
        "auth/user",
        FieldBinding(
            "tasks__promoted__rounds__entries__responder",
            (
                ("tasks__stage__hidden__isnull", True),
                ("tasks__promoted__rounds__entries__retired_at__isnull", True),
            ),
        ),
    )
    watched = watched_for(schema)
    type_ = "test/backingqueue"
    _assert_watch(watched, BackingQueue, {"id"}, type_)
    _assert_watch(watched, BackingTask, {"queue", "queue_id", "stage", "stage_id"}, type_)
    assert not _assert_watch(watched, BackingStage, {"hidden"}, type_).is_mixin
    assert _assert_watch(watched, BackingProject, {"task_id"}, type_).is_mixin
    _assert_watch(watched, BackingRound, {"project_id"}, type_)
    assert _assert_watch(
        watched, BackingEntry, {"round_id", "responder_id", "retired_at"}, type_
    ).is_mixin
    _assert_watch(watched, get_user_model(), {"id"}, type_)


@pytest.mark.parametrize("reverse", [False, True])
def test_m2m_watches_include_both_through_columns(reverse):
    source, target, path = (
        ("blog/folder", "blog/post", "collected_posts")
        if reverse
        else ("blog/post", "blog/folder", "collections")
    )
    watched = watched_for(_backing_schema(source, target, FieldBinding(path)))
    through = Post._meta.get_field("collections").remote_field.through
    _assert_watch(watched, through, {"post_id", "folder_id"}, source)
    _assert_watch(watched, Post, {"id"}, source)
    _assert_watch(watched, Folder, {"id"}, source)


def test_mti_watches_parent_identity_and_parent_link():
    schema = _backing_schema(
        "test/nativeparentlinkedrecord", "test/nativeparentlinkedchild", FieldBinding("child")
    )
    watched = watched_for(schema)
    type_ = "test/nativeparentlinkedrecord"
    _assert_watch(watched, NativeParentLinkedChild, {"nativeparentlinkedresource_ptr_id"}, type_)
    _assert_watch(watched, NativeParentLinkedResource, {"id"}, type_)


def test_attribute_watches_identity_attribute_and_filter_columns():
    schema = _backing_schema(
        "kind", "auth/user", AttributeBinding("username", filters=(("is_active", True),))
    )
    watched = watched_for(schema)
    assert not _assert_watch(
        watched, get_user_model(), {"id", "username", "is_active"}, "kind"
    ).is_mixin
    fields = {(m._meta.label_lower, attr) for m, attr in codec_fields(schema)}
    assert (get_user_model()._meta.label_lower, "username") in fields


def test_fixed_boolean_attribute_is_watched_but_requires_no_attribute_codec():
    schema = _backing_schema(
        "flags", "auth/user", AttributeBinding("is_staff", resource="staff", value=True)
    )
    watched = watched_for(schema)
    _assert_watch(watched, get_user_model(), {"is_staff"}, "flags")
    assert all(attr != "is_staff" for _model, attr in codec_fields(schema))


def test_const_watches_local_filters_and_source_identity():
    schema = _backing_schema(
        "blog/authoredpost",
        "role",
        ConstBinding("public", (("confirmed", True), ("role__exact", "x"))),
    )
    watched = watched_for(schema)
    assert _assert_watch(
        watched, AuthoredPost, {"id", "confirmed", "role"}, "blog/authoredpost"
    ).is_mixin


def test_watched_map_merges_fields_and_resource_types():
    one = _backing_schema("kind", "auth/user", AttributeBinding("username"))
    two = _backing_schema("email", "auth/user", AttributeBinding("email"))
    watched = watched_for(Schema(definitions=one.definitions + two.definitions))
    watch = watched[get_user_model()._meta.label_lower]
    assert watch.resource_types == frozenset({"kind", "email"})
    assert {"id", "username", "email"} <= watch.fields


def test_watch_mapping_and_specs_are_immutable():
    watched = watched_for(_backing_schema("kind", "auth/user", AttributeBinding("username")))
    label = get_user_model()._meta.label_lower
    with pytest.raises(TypeError):
        watched["anything"] = None
    with pytest.raises(FrozenInstanceError):
        watched[label].is_mixin = True
    policy = _policy(parse_zed("definition doc { permission read = authenticated }"))
    with pytest.raises(TypeError):
        policy.watched["anything"] = None
    with pytest.raises(FrozenInstanceError):
        policy.watched = {}


def test_gate_policy_names_relations_and_permissions_of_the_schema():
    policy = _policy(
        parse_zed("""
        definition auth/user {}
        definition doc {
            relation reader: auth/user
            permission read = reader
        }
        definition note { relation reader: auth/user }
    """)
    )
    # A relation is a name of the policy even when no permission uses it.
    assert policy.has_node("note", "reader")
    assert policy.has_node("doc", "reader") and policy.has_node("doc", "read")
    assert not policy.has_node("doc", "write")
    assert not policy.has_node("missing", "read")
    assert dict(policy.watched) == {}


def test_model_is_watched_by_field_name_or_column_and_along_its_lineage():
    watched = watched_for(
        _backing_schema(
            "test/backingqueue",
            "auth/user",
            FieldBinding("tasks__asker", (("tasks__stage__hidden__isnull", True),)),
        )
    )
    assert model_is_watched(watched, BackingTask)
    assert model_is_watched(watched, BackingTask, ["stage"])
    assert model_is_watched(watched, BackingTask, ["stage_id"])
    assert model_is_watched(watched, BackingTask, ["asker_id", "visibility"])
    assert not model_is_watched(watched, BackingTask, ["visibility", "surrendered"])
    assert not model_is_watched(watched, BackingRound)
    # A write to a multi-table child also writes its concrete parent's row.
    parent = watched_for(
        _backing_schema(
            "test/nativeparentlinkedresource", "role", ConstBinding("public", (("name", "x"),))
        )
    )
    assert NativeParentLinkedChild._meta.label_lower not in parent
    assert model_is_watched(parent, NativeParentLinkedChild)
    assert model_is_watched(parent, NativeParentLinkedChild, ["name"])
    assert not model_is_watched(parent, NativeParentLinkedChild, ["owner"])


def test_resolving_the_gate_policy_never_reconnects_signals(monkeypatch):
    from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete, pre_save

    connections = []
    for signal in (m2m_changed, pre_save, post_save, pre_delete, post_delete):
        connect = Mock(side_effect=AssertionError("resolving the policy reconnected a signal"))
        monkeypatch.setattr(signal, "connect", connect)
        connections.append(connect)
    policy = _policy(_backing_schema("kind", "auth/user", AttributeBinding("username")))
    assert get_user_model()._meta.label_lower in policy.watched
    for connect in connections:
        connect.assert_not_called()
