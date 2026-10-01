"""Tuple and schema writes nested inside another write commit or roll back with it."""

from contextlib import contextmanager

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save

from rebac import (
    Consistency,
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    backend,
    schema_changes,
    sudo,
    to_subject_ref,
)
from rebac.backends import reset_backend
from rebac.models import (
    Relationship,
    SchemaDefinition,
    SchemaOverride,
    SchemaPermission,
    SchemaRelation,
)
from rebac.schema import parse_zed
from rebac.types import RelationshipFilter
from tests.backend_setup import install_schema
from tests.testapp.models import BackingEntry, BackingRound

SCHEMA = """
definition auth/user {}
definition blog/post {
    relation viewer: auth/user
    permission read = viewer
}
definition test/backinground {
    relation owner: auth/user
    relation responders: auth/user // rebac:field=entries__responder
    permission read = owner + responders
    permission write = owner
}
"""
CAROL = SubjectRef.of("auth/user", "carol")
DAVE = SubjectRef.of("auth/user", "dave")
ALICE = SubjectRef.of("auth/user", "alice")


@pytest.fixture(autouse=True)
def installed(db):
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA))
    yield
    reset_backend()


@contextmanager
def connected(signal, sender, handler):
    uid = f"{__name__}.{handler.__name__}"
    signal.connect(handler, sender=sender, weak=False, dispatch_uid=uid)
    try:
        yield
    finally:
        signal.disconnect(sender=sender, dispatch_uid=uid)


def fixture_rows():
    with sudo(reason="fixture"), transaction.atomic():
        first = BackingRound.objects.create()
        second = BackingRound.objects.create()
        user = get_user_model().objects.create_user(username="nested responder")
        entry = BackingEntry.objects.create(round=first, responder=user)
    responder = to_subject_ref(user)
    assert reads_round(responder, first)
    assert not reads_round(responder, second)
    return first, second, entry, responder


def reads_round(subject, round_):
    return backend().has_access(
        subject=subject, action="read", resource=ObjectRef("test/backinground", str(round_.pk))
    )


@pytest.fixture
def persisted(db):
    """A stored policy with one grant; returns its ``read`` permission row."""
    reset_backend()
    with schema_changes():
        SchemaDefinition.objects.create(resource_type="auth/user")
        definition = SchemaDefinition.objects.create(resource_type="test/policy")
        SchemaRelation.objects.create(
            definition=definition, name="viewer", allowed_subjects=[{"type": "auth/user"}]
        )
        permission = SchemaPermission.objects.create(
            definition=definition, name="read", expression="viewer"
        )
        SchemaPermission.objects.create(definition=definition, name="dependent", expression="read")
    backend().write_relationships(
        [RelationshipTuple(ObjectRef("test/policy", "one"), "viewer", ALICE)]
    )
    return permission


@pytest.mark.parametrize("event", ["pre_save", "pre_delete"])
def test_nested_tuple_write_does_not_hide_outer_backing_change(event):
    first, second, entry, responder = fixture_rows()

    def nested_write(**kwargs):
        backend().write_relationships(
            [RelationshipTuple(ObjectRef("blog/post", "1"), "viewer", CAROL)]
        )

    signal = pre_save if event == "pre_save" else pre_delete
    with connected(signal, BackingEntry, nested_write):
        with sudo(reason="outer write"), transaction.atomic():
            if event == "pre_save":
                entry.round = second
                entry.save()
            else:
                entry.delete()
    assert not backend().has_access(
        subject=responder, action="read", resource=ObjectRef("test/backinground", str(first.pk))
    )
    assert reads_round(responder, second) is (event == "pre_save")
    assert backend().has_access(subject=CAROL, action="read", resource=ObjectRef("blog/post", "1"))


def test_relationship_signal_orm_delete_revokes_access():
    backend().write_relationships([RelationshipTuple(ObjectRef("blog/post", "2"), "viewer", DAVE)])

    def revoke(sender, instance, created, **kwargs):
        if instance.resource_id == "3":
            Relationship.objects.filter(resource_type="blog/post", resource_id="2").delete()

    with connected(post_save, Relationship, revoke):
        backend().write_relationships(
            [RelationshipTuple(ObjectRef("blog/post", "3"), "viewer", CAROL)]
        )
    assert not backend().has_access(
        subject=DAVE, action="read", resource=ObjectRef("blog/post", "2")
    )


@pytest.mark.parametrize("event", ["pre_save", "post_save", "pre_delete", "post_delete"])
@pytest.mark.parametrize("kind", ["grant", "revoke"])
def test_nested_tuple_write_in_each_model_signal_applies_both_changes(event, kind):
    first, second, entry, responder = fixture_rows()
    target = ObjectRef("blog/post", "signal")
    if kind == "revoke":
        backend().write_relationships([RelationshipTuple(target, "viewer", DAVE)])

    def nested_write(**kwargs):
        if kind == "grant":
            backend().write_relationships([RelationshipTuple(target, "viewer", CAROL)])
        else:
            zookie = backend().delete_relationships(
                RelationshipFilter(resource_type="blog/post", resource_id="signal")
            )
            assert (
                not backend()
                .check_access(
                    subject=DAVE,
                    action="read",
                    resource=target,
                    consistency=Consistency.AT_LEAST_AS_FRESH,
                    at_zookie=zookie,
                )
                .allowed
            )

    signal = {
        "pre_save": pre_save,
        "post_save": post_save,
        "pre_delete": pre_delete,
        "post_delete": post_delete,
    }[event]
    with connected(signal, BackingEntry, nested_write):
        with sudo(reason="outer write"), transaction.atomic():
            if event.endswith("save"):
                entry.round = second
                entry.save()
            else:
                entry.delete()
    assert not backend().has_access(
        subject=responder, action="read", resource=ObjectRef("test/backinground", str(first.pk))
    )
    assert reads_round(responder, second) is event.endswith("save")
    assert backend().has_access(
        subject=CAROL if kind == "grant" else DAVE, action="read", resource=target
    ) is (kind == "grant")


@pytest.mark.parametrize("event", ["pre_save", "post_save"])
def test_caught_nested_savepoint_exception_preserves_outer_write(event):
    first, second, entry, responder = fixture_rows()

    class Rollback(Exception):
        pass

    def nested_write(**kwargs):
        try:
            with transaction.atomic():
                backend().write_relationships(
                    [RelationshipTuple(ObjectRef("blog/post", "rolled"), "viewer", CAROL)]
                )
                raise Rollback
        except Rollback:
            pass

    with connected(pre_save if event == "pre_save" else post_save, BackingEntry, nested_write):
        with sudo(reason="outer write"), transaction.atomic():
            entry.round = second
            entry.save()
    assert not backend().has_access(
        subject=CAROL, action="read", resource=ObjectRef("blog/post", "rolled")
    )
    assert not backend().has_access(
        subject=responder, action="read", resource=ObjectRef("test/backinground", str(first.pk))
    )
    assert reads_round(responder, second)


def test_savepoint_rollback_then_second_nested_tuple_write():
    first, second, entry, responder = fixture_rows()

    def nested_write(**kwargs):
        savepoint = transaction.savepoint()
        backend().write_relationships(
            [RelationshipTuple(ObjectRef("blog/post", "rolled"), "viewer", CAROL)]
        )
        transaction.savepoint_rollback(savepoint)
        backend().write_relationships(
            [RelationshipTuple(ObjectRef("blog/post", "kept"), "viewer", DAVE)]
        )

    with connected(pre_save, BackingEntry, nested_write):
        with sudo(reason="outer write"), transaction.atomic():
            entry.round = second
            entry.save()
    assert not backend().has_access(
        subject=CAROL, action="read", resource=ObjectRef("blog/post", "rolled")
    )
    assert backend().has_access(
        subject=DAVE, action="read", resource=ObjectRef("blog/post", "kept")
    )
    assert not backend().has_access(
        subject=responder, action="read", resource=ObjectRef("test/backinground", str(first.pk))
    )
    assert reads_round(responder, second)


def test_outer_rollback_reverts_nested_tuple_and_source_write():
    first, second, entry, responder = fixture_rows()

    class Rollback(Exception):
        pass

    def nested_write(**kwargs):
        backend().write_relationships(
            [RelationshipTuple(ObjectRef("blog/post", "outer-rolled"), "viewer", CAROL)]
        )

    with connected(post_save, BackingEntry, nested_write), pytest.raises(Rollback):
        with sudo(reason="outer write"), transaction.atomic():
            entry.round = second
            entry.save()
            raise Rollback
    assert not backend().has_access(
        subject=CAROL, action="read", resource=ObjectRef("blog/post", "outer-rolled")
    )
    assert backend().has_access(
        subject=responder, action="read", resource=ObjectRef("test/backinground", str(first.pk))
    )
    assert not reads_round(responder, second)


def test_third_party_tracked_user_pre_save_tuple_write_inside_atomic():
    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user
            permission read = viewer
        }
        definition test/bucket {
            relation member: auth/user // rebac:attribute={"field":"last_name"}
            permission read = member
        }
        """),
    )
    with transaction.atomic():
        user = get_user_model().objects.create_user(username="tracked-nested", last_name="a")
    subject = to_subject_ref(user)
    assert backend().has_access(
        subject=subject, action="read", resource=ObjectRef("test/bucket", "a")
    )

    def nested_write(**kwargs):
        backend().write_relationships(
            [RelationshipTuple(ObjectRef("blog/post", "tracked"), "viewer", CAROL)]
        )

    with connected(pre_save, get_user_model(), nested_write), transaction.atomic():
        user.last_name = "b"
        user.save(update_fields=["last_name"])
    assert not backend().has_access(
        subject=subject, action="read", resource=ObjectRef("test/bucket", "a")
    )
    assert backend().has_access(
        subject=subject, action="read", resource=ObjectRef("test/bucket", "b")
    )
    assert backend().has_access(
        subject=CAROL, action="read", resource=ObjectRef("blog/post", "tracked")
    )


def test_tuple_writes_in_schema_override_post_save_handler(persisted):
    assert backend().has_access(
        subject=ALICE, action="read", resource=ObjectRef("test/policy", "one")
    )

    def nested_write(sender, instance, created, **kwargs):
        backend().write_relationships(
            [RelationshipTuple(ObjectRef("test/policy", "two"), "viewer", DAVE)]
        )
        backend().delete_relationships(
            RelationshipFilter(resource_type="test/policy", resource_id="one")
        )

    with connected(post_save, SchemaOverride, nested_write):
        SchemaOverride.objects.create(
            kind=SchemaOverride.KIND_EXTEND,
            target_ct=ContentType.objects.get_for_model(SchemaPermission),
            target_pk=persisted.pk,
            expression="viewer",
            reason="nested handler",
        )
    assert not backend().has_access(
        subject=ALICE, action="read", resource=ObjectRef("test/policy", "one")
    )
    assert backend().has_access(
        subject=DAVE, action="read", resource=ObjectRef("test/policy", "two")
    )


@pytest.mark.pg_delta
def test_revoke_after_nested_schema_publish_is_denied_inside_and_after_the_transaction(persisted):
    resource = ObjectRef("test/policy", "one")
    assert backend().has_access(subject=ALICE, action="read", resource=resource)
    with transaction.atomic():
        SchemaPermission.objects.filter(pk=persisted.pk).update(expression="viewer")
        zookie = backend().delete_relationships(
            RelationshipFilter(resource_type="test/policy", resource_id="one")
        )
        assert (
            not backend()
            .check_access(
                subject=ALICE,
                action="read",
                resource=resource,
                consistency=Consistency.AT_LEAST_AS_FRESH,
                at_zookie=zookie,
            )
            .allowed
        )
    assert not backend().has_access(subject=ALICE, action="read", resource=resource)
