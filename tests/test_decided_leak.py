"""A recursive field-chain alias never reuses another actor's decided rows."""

import pytest

from rebac import RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.compile import read
from rebac.testing import install_schema
from tests.testapp.models import Folder

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation reader: auth/user
    relation parent: blog/folder // rebac:field=parent
    permission read = reader + parent->read
    permission alias = read
}
"""


@pytest.mark.parametrize("scope", ["ordinary", "evaluator"])
def test_alias_of_recursive_field_chain(scope):
    local = install_schema(SCHEMA)
    with sudo(reason="cross-actor isolation fixture"):
        a_root = Folder.objects.create(name="a-root")
        a_child = Folder.objects.create(name="a-child", parent=a_root)
        b_root = Folder.objects.create(name="b-root")
        b_child = Folder.objects.create(name="b-child", parent=b_root)
    alice, bob = SubjectRef.of("auth/user", "alice"), SubjectRef.of("auth/user", "bob")
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(a_root), "reader", alice),
            RelationshipTuple(to_object_ref(b_root), "reader", bob),
        ]
    )

    def alias(actor):
        if scope == "evaluator":
            with evaluator_scope():
                return sorted(
                    Folder.objects.with_actor(actor)
                    .with_action("alias")
                    .values_list("pk", flat=True)
                )
        return sorted(
            Folder.objects.with_actor(actor).with_action("alias").values_list("pk", flat=True)
        )

    read.reset()
    seen = {"alice": alias(alice), "bob_after_alice": alias(bob)}
    read.reset()
    seen["bob_cold"] = alias(bob)
    seen["alice_rows"] = sorted([a_root.pk, a_child.pk])
    seen["bob_rows"] = sorted([b_root.pk, b_child.pk])
    assert seen["bob_cold"] == seen["bob_rows"]
    assert seen["bob_after_alice"] == seen["bob_rows"], seen
