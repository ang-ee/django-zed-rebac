"""The delete collector checks each collected resource model as one batch."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import PermissionDenied, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.testing import install_schema
from tests.testapp.models import Folder

pytestmark = pytest.mark.django_db
ACTOR = SubjectRef.of("auth/user", "collector")
SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation owner: auth/user
    permission read = owner
    permission write = owner
    permission delete = owner
}
"""


def _tree(local, children, *, denied=0):
    with sudo(reason="collector fixture"):
        root = Folder.objects.create(name="root")
        kids = [Folder.objects.create(name=f"kid{index}", parent=root) for index in range(children)]
    owned = [root, *kids[denied:]]
    local.write_relationships(
        [RelationshipTuple(to_object_ref(row), "owner", ACTOR) for row in owned]
    )
    return root, kids


def _gate_statements(queries):
    return sum("rebac_relationship" in query["sql"] for query in queries.captured_queries)


@pytest.mark.parametrize("delete", ["instance", "queryset"])
def test_cascade_is_checked_once_per_model_whatever_its_size(delete):
    local = install_schema(SCHEMA)
    costs = []
    for children in (5, 12):
        root, _kids = _tree(local, children)
        with CaptureQueriesContext(connection) as queries:
            if delete == "instance":
                Folder.objects.with_actor(ACTOR).get(pk=root.pk).delete()
            else:
                Folder.objects.with_actor(ACTOR).filter(pk=root.pk).delete()
        assert not Folder._base_manager.filter(pk=root.pk).exists()
        costs.append(_gate_statements(queries))
    assert costs[0] == costs[1]


def test_one_denied_cascaded_row_refuses_the_whole_delete():
    local = install_schema(SCHEMA)
    root, kids = _tree(local, 3, denied=1)
    with pytest.raises(PermissionDenied):
        Folder.objects.with_actor(ACTOR).filter(pk=root.pk).delete()
    assert Folder._base_manager.filter(pk__in=[root.pk, *(kid.pk for kid in kids)]).count() == 4
