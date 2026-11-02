"""A hierarchy over a multi-table child: its key is the link to its parent's table."""

import pytest

from rebac import CheckItem, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.compile import read
from rebac.testing import install_schema
from tests.testapp.models import NativeParentLinkedBranch, NativeParentLinkedNote

pytestmark = pytest.mark.django_db

SCHEMA = """
definition auth/user {}
definition test/nativeparentlinkedresource {}
definition test/nativeparentlinkedbranch {
    relation parent: test/nativeparentlinkedbranch // rebac:field=parent
    relation manager: auth/user
    permission admin = manager + parent->admin
    permission write = admin
}
definition test/nativeparentlinkednote {
    relation branch: test/nativeparentlinkedbranch // rebac:field=branch
    permission write = branch->write
}
"""
ALICE = SubjectRef.of("auth/user", "alice")


@pytest.fixture
def branches():
    local = install_schema(SCHEMA)
    with sudo(reason="test.fixture"):
        head = NativeParentLinkedBranch.objects.create(name="head")
        region = NativeParentLinkedBranch.objects.create(name="region", parent=head)
        office = NativeParentLinkedBranch.objects.create(name="office", parent=region)
        other = NativeParentLinkedBranch.objects.create(name="other")
        notes = {
            branch.name: NativeParentLinkedNote.objects.create(branch=branch)
            for branch in (head, region, office, other)
        }
    local.write_relationships([RelationshipTuple(to_object_ref(region), "manager", ALICE)])
    return local, {"head": head, "region": region, "office": office, "other": other}, notes


def writable(model):
    return set(model.objects.with_actor(ALICE).with_action("write").values_list("pk", flat=True))


def test_the_key_of_the_hierarchy_is_a_parent_link():
    parent = NativeParentLinkedBranch._meta.get_field("parent")
    # The column a child names its parent by is a one-to-one relation field,
    # for which Django's ``__in`` lookup takes plain values only.
    assert parent.target_field.is_relation
    assert parent.target_field is NativeParentLinkedBranch._meta.pk


@pytest.mark.parametrize("name", ["head", "region", "office", "other"])
def test_a_point_check_inherits_through_the_self_foreign_key(branches, name):
    local, rows, notes = branches
    held = name in ("region", "office")
    for resource in (rows[name], notes[name]):
        answer = local.check_access(subject=ALICE, action="write", resource=to_object_ref(resource))
        assert answer.allowed is held


def test_a_scope_inherits_through_the_self_foreign_key(branches):
    _local, rows, notes = branches
    assert writable(NativeParentLinkedBranch) == {rows["region"].pk, rows["office"].pk}
    assert writable(NativeParentLinkedNote) == {notes["region"].pk, notes["office"].pk}


def test_bulk_checks_agree(branches):
    local, rows, notes = branches
    items = [
        CheckItem(ALICE, "write", to_object_ref(resource))
        for resource in (*rows.values(), *notes.values())
    ]
    answers = local.check_bulk_permissions(items)
    assert [answer.allowed for answer in answers] == [False, True, True, False] * 2


@pytest.mark.pg_delta
@pytest.mark.parametrize("selector", ["exact", "in"])
def test_parent_link_identity_uses_only_proven_lookup_shapes(branches, monkeypatch, selector):
    _local, rows, _notes = branches
    original = read._point_statement
    compiled = []

    def point(operation, key, *args, **kwargs):
        compiled.append(key)
        return original(operation, key, *args, **kwargs)

    monkeypatch.setattr(read, "_point_statement", point)
    for name in ("region", "other"):
        row = rows[name]
        query = NativeParentLinkedBranch.objects.with_actor(ALICE).with_action("write")
        query = query.filter(pk=row.pk) if selector == "exact" else query.filter(pk__in=[row.pk])
        assert list(query.values_list("pk", flat=True)) == ([row.pk] if name == "region" else [])
    # Native RelatedExact is admitted; RelatedIn is deliberately not native In.
    assert (("test/nativeparentlinkedbranch", "write") in compiled) is (selector == "exact")
