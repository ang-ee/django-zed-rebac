"""Executable proofs for generic, reverse, inherited and filtered ORM paths."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.utils import timezone

from rebac import PermissionDenied, sudo, to_object_ref, to_subject_ref
from rebac.backends import backend as active_backend
from rebac.backends import reset_backend
from rebac.checks import check_field_backed_relations
from rebac.field_backing import _proposed_forward_relationships
from rebac.preflight import _check_new_model
from rebac.schema import Permission, PermRef, parse_zed, validate_schema
from rebac.schema.introspection import relation_dependencies
from tests.backend_setup import atomic_source_write, install_schema

from .testapp.models import (
    BackingBinding,
    BackingDocument,
    BackingEntry,
    BackingOtherDocument,
    BackingProject,
    BackingQueue,
    BackingRound,
    BackingStage,
    BackingTask,
    NativeParentLinkedChild,
    NativeParentLinkedResource,
)

SCHEMA = """
definition auth/user {}
definition test/backingdocument {
    relation owner: auth/user // rebac:field=owner
    relation reader: auth/user // rebac:field=bindings__reader
    permission read = reader
    permission manage = owner
    permission create = reader
}
definition test/backingbinding {
    relation document: test/backingdocument // rebac:field=document
    permission read = document->manage
    permission create = document->manage
}
definition test/backingqueue {
    permission withheld = nil
    permission read = authenticated - withheld
    permission create = authenticated - withheld
    permission write = authenticated
}
definition test/backinground {
    relation responder: auth/user // rebac:field={"path":"entries__responder","filters":{"entries__retired_at__isnull":true}}
    relation listed: auth/user // rebac:field={"path":"entries__responder","filters":{"listing":"names","entries__retired_at__isnull":true}}
    permission read = responder
    permission names = listed
    permission create = listed
}
definition test/backingtask {
    relation inherited_queue: test/backingqueue // rebac:field={"path":"queue","filters":{"visibility":"inherited","stage__hidden":false}}
    relation active_asker: auth/user // rebac:field={"path":"asker","filters":{"surrendered":false}}
    relation shared_round: test/backinground // rebac:field={"path":"shared_round","filters":{"visibility":"inherited"}}
    permission read = inherited_queue->read
    permission asker = active_asker
    permission shared = shared_round->read
    permission create = inherited_queue->read
}
definition test/nativeparentlinkedresource {
    relation owner: auth/user // rebac:field=nativeparentlinkedchild__owner
    permission read = owner
    permission create = owner
}
"""
FRAGMENT = """
definition test/backingqueue {
    relation engaged: auth/user // rebac:field={"path":"tasks__promoted__rounds__entries__responder","filters":{"tasks__promoted__rounds__entries__retired_at__isnull":true}}
}
"""


@pytest.fixture
def backend(db):
    reset_backend()
    result = active_backend()
    schema = parse_zed(SCHEMA)
    base = schema.get_definition("test/backingqueue")
    fragment = parse_zed(FRAGMENT).get_definition("test/backingqueue")
    merged = base.extend(
        relations=fragment.relations, permission_arms=[Permission("withheld", PermRef("engaged"))]
    )
    schema.definitions[schema.definitions.index(base)] = merged
    validate_schema(schema)
    install_schema(result, schema)
    assert not [issue for issue in check_field_backed_relations() if issue.id == "rebac.E009"]
    yield result
    reset_backend()


@pytest.fixture
def actors(django_user_model):
    return [
        atomic_source_write(django_user_model.objects.create_user, username=name)
        for name in ("alice", "bob")
    ]


def assert_read(backend, model, actor, rows, *, action="read"):
    expected = {row.pk for row in rows}
    for row in model._base_manager.all():
        assert backend.check_access(
            subject=to_subject_ref(actor), action=action, resource=to_object_ref(row)
        ).allowed is (row.pk in expected)
    # A passing proof must compile; enumeration would hide an unsupported shape.
    with patch.object(backend, "accessible", side_effect=AssertionError("unexpected enumeration")):
        scoped = model.objects.with_actor(actor).with_action(action)
        assert set(scoped.values_list("pk", flat=True)) == expected


def assert_empty_preflight(backend, candidate, actor, relation):
    resource_type = candidate._meta.rebac_resource_type
    schema = backend.schema()
    definition = schema.get_definition(resource_type)
    projected = _proposed_forward_relationships(
        candidate,
        definition,
        required_relations=relation_dependencies(schema, resource_type, "create"),
    )
    assert projected[relation] == ()
    assert not _check_new_model(candidate, subject=to_subject_ref(actor), backend=backend).allowed
    with pytest.raises(PermissionDenied):
        candidate.with_actor(actor).save()


def test_generic_reverse_collection_preserves_content_type_and_create_boundary(backend, actors):
    alice, bob = actors
    with sudo(reason="generic collection fixtures"):
        visible = BackingDocument.objects.create(owner=alice)
        hidden = BackingDocument.objects.create(owner=bob)
        other = BackingOtherDocument.objects.create(pk=hidden.pk)
        BackingBinding.objects.create(content_object=visible, reader=alice)
        BackingBinding.objects.create(content_object=hidden, reader=bob)
        # Same object id, different content type must never grant the hidden document.
        BackingBinding.objects.create(content_object=other, reader=alice)
    assert_read(backend, BackingDocument, alice, [visible])
    assert_read(backend, BackingDocument, bob, [hidden])
    assert_empty_preflight(backend, BackingDocument(owner=alice), alice, "reader")


def test_related_query_name_reverse_path_resolves_from_binding_side(backend, actors):
    alice, bob = actors
    with sudo(reason="related query name fixtures"):
        first = BackingDocument.objects.create(owner=alice)
        second = BackingDocument.objects.create(owner=bob)
        other = BackingOtherDocument.objects.create(pk=first.pk)
        visible = BackingBinding.objects.create(content_object=first, reader=bob)
        BackingBinding.objects.create(content_object=second, reader=alice)
        BackingBinding.objects.create(content_object=other, reader=alice)
    assert_read(backend, BackingBinding, alice, [visible])
    # GenericRelation's related_query_name is a reverse first hop even if the
    # candidate already has a generic FK value. It is empty at create time.
    assert_empty_preflight(
        backend, BackingBinding(content_object=first, reader=alice), alice, "document"
    )


def test_four_hop_filtered_path_and_nil_fragment_exclusion_use_same_entry(backend, actors):
    alice, bob = actors
    with sudo(reason="deep reverse path fixtures"):
        queue = BackingQueue.objects.create()
        empty = BackingQueue.objects.create()
        task = BackingTask.objects.create(queue=queue)
        project = BackingProject.objects.create(task=task)
        round_ = BackingRound.objects.create(project=project)
        retired = BackingEntry.objects.create(
            round=round_, responder=alice, retired_at=timezone.now()
        )
        active = BackingEntry.objects.create(round=round_, responder=bob)
    assert_read(backend, BackingQueue, alice, [queue, empty])
    assert_read(backend, BackingQueue, bob, [empty])
    assert_read(backend, BackingQueue, alice, [], action="withheld")
    assert_read(backend, BackingQueue, bob, [queue], action="withheld")
    # Revocation changes an already constructed SQL scope, with no tuple writes.
    pending = BackingQueue.objects.with_actor(bob)
    active.retired_at = timezone.now()
    atomic_source_write(active.save, update_fields=["retired_at"])
    assert set(pending.values_list("pk", flat=True)) == {queue.pk, empty.pk}
    retired.retired_at = None
    atomic_source_write(retired.save, update_fields=["retired_at"])
    assert_read(backend, BackingQueue, alice, [empty])
    candidate = BackingQueue()
    projected = _proposed_forward_relationships(
        candidate,
        backend.schema().get_definition("test/backingqueue"),
        required_relations=frozenset({"engaged"}),
    )
    assert projected == {"engaged": ()}
    # Empty reverse relations make exclusion true for a genuinely new queue.
    assert _check_new_model(candidate, subject=to_subject_ref(alice), backend=backend).allowed
    BackingQueue.objects.with_actor(alice).insert(candidate)


@pytest.mark.parametrize(
    "hidden,visibility,expected",
    [
        (False, "inherited", True),
        (True, "inherited", False),
        (None, "inherited", False),
        ("missing", "inherited", False),
        (False, "restricted", False),
    ],
)
def test_related_column_and_root_filter_with_null_parity(
    backend, actors, hidden, visibility, expected
):
    alice, _ = actors
    with sudo(reason="related filter fixtures"):
        queue = BackingQueue.objects.create()
        stage = None if hidden == "missing" else BackingStage.objects.create(hidden=hidden)
        row = BackingTask.objects.create(queue=queue, stage=stage, visibility=visibility)
    assert_read(backend, BackingTask, alice, [row] if expected else [])
    candidate = BackingTask(queue=queue, stage=stage, visibility=visibility)
    assert (
        _check_new_model(candidate, subject=to_subject_ref(alice), backend=backend).allowed
        is expected
    )
    if expected:
        BackingTask.objects.with_actor(alice).insert(candidate)
    else:
        with pytest.raises(PermissionDenied):
            BackingTask.objects.with_actor(alice).insert(candidate)


def test_reverse_parent_link_then_owner_column(backend, actors):
    alice, bob = actors
    with sudo(reason="parent-link fixtures"):
        visible = NativeParentLinkedChild.objects.create(name="alice", owner=alice)
        NativeParentLinkedChild.objects.create(name="bob", owner=bob)
        NativeParentLinkedResource.objects.create(name="no child")
    assert_read(backend, NativeParentLinkedResource, alice, [visible])
    assert_empty_preflight(backend, NativeParentLinkedResource(name="new parent"), alice, "owner")


@pytest.mark.parametrize("action", ["asker", "shared"])
def test_donor_column_paths_use_root_row_filters_including_preflight(backend, actors, action):
    alice, bob = actors
    with sudo(reason="root filter fixtures"):
        queue = BackingQueue.objects.create()
        round_ = BackingRound.objects.create()
        BackingEntry.objects.create(round=round_, responder=alice)
        included = BackingTask.objects.create(queue=queue, asker=alice, shared_round=round_)
        BackingTask.objects.create(
            queue=queue,
            asker=alice,
            shared_round=round_,
            surrendered=True,
            visibility="restricted",
        )
        BackingTask.objects.create(queue=queue, asker=bob)
    assert_read(backend, BackingTask, alice, [included], action=action)
    schema = backend.schema()
    definition = schema.get_definition("test/backingtask")
    # Switch only this fixture's create gate to exercise the same donor branch.
    from dataclasses import replace

    schema.definitions[schema.definitions.index(definition)] = replace(
        definition,
        permissions=tuple(
            Permission("create", PermRef(action)) if permission.name == "create" else permission
            for permission in definition.permissions
        ),
    )
    install_schema(backend, schema)
    for allowed in (True, False):
        candidate = BackingTask(
            queue=queue,
            asker=alice,
            shared_round=round_,
            surrendered=not allowed,
            visibility="inherited" if allowed else "restricted",
        )
        assert (
            _check_new_model(candidate, subject=to_subject_ref(alice), backend=backend).allowed
            is allowed
        )
        if allowed:
            BackingTask.objects.with_actor(alice).insert(candidate)
        else:
            with pytest.raises(PermissionDenied):
                BackingTask.objects.with_actor(alice).insert(candidate)


def test_root_listed_filter_and_terminal_retirement_filter_share_join(backend, actors):
    alice, bob = actors
    with sudo(reason="listed fixtures"):
        visible = BackingRound.objects.create(listing="names")
        hidden = BackingRound.objects.create(listing="hidden")
        BackingEntry.objects.create(round=visible, responder=alice, retired_at=timezone.now())
        BackingEntry.objects.create(round=visible, responder=bob)
        BackingEntry.objects.create(round=hidden, responder=bob)
    assert_read(backend, BackingRound, alice, [], action="names")
    assert_read(backend, BackingRound, bob, [visible], action="names")
    assert_empty_preflight(backend, BackingRound(listing="names"), bob, "listed")
