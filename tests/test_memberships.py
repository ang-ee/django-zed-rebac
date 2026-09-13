"""Direct membership API behavior shared by roles, groups, and kind anchors."""

import pytest

from rebac import backend
from rebac.backends import reset_backend
from rebac.memberships import containers_of, grant, members_of, revoke
from rebac.models import Relationship
from rebac.schema import parse_zed
from rebac.types import ObjectRef, SubjectRef

SCHEMA_TEXT = """
caveat during_hours(timezone string) {
    timezone == "UTC"
}

definition auth/user {}

definition auth/group {
    relation member: auth/user
}

definition iam/kind {
    relation member: auth/user | auth/group#member
}

definition access/group {
    relation member: auth/user | auth/user with during_hours
}

definition storage/role {
    relation member: auth/user
}
"""


@pytest.fixture(autouse=True)
def _membership_schema(request):
    if request.node.get_closest_marker("django_db") is None:
        yield
        return
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


@pytest.mark.django_db
def test_membership_round_trip_is_generic_and_idempotent():
    subject = SubjectRef.of("auth/group", "reviewers", "member")
    container = ObjectRef("iam/kind", "person")

    first = grant(subject=subject, container=container)
    second = grant(subject=subject, container=container)

    assert first.pk == second.pk
    assert list(members_of(container)) == [subject]
    assert list(containers_of(subject)) == [container]
    assert revoke(subject=subject, container=container) == 1
    assert revoke(subject=subject, container=container) == 0


@pytest.mark.django_db
def test_caveated_revoke_removes_only_the_named_membership():
    subject = SubjectRef.of("auth/user", "42")
    container = ObjectRef("access/group", "reviewers")
    grant(subject=subject, container=container)
    grant(
        subject=subject,
        container=container,
        caveat_name="during_hours",
        caveat_context={"timezone": "UTC"},
    )

    assert revoke(subject=subject, container=container, caveat_name="during_hours") == 1
    assert Relationship.objects.filter(caveat_name="").count() == 1
    assert not Relationship.objects.filter(caveat_name="during_hours").exists()


@pytest.mark.django_db
def test_containers_of_applies_container_lookups_in_sql():
    subject = SubjectRef.of("auth/user", "7")
    grant(subject=subject, container=ObjectRef("storage/role", "viewer"))
    grant(subject=subject, container=ObjectRef("iam/kind", "person"))

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        roles = list(containers_of(subject, resource_type__endswith="/role"))

    assert roles == [ObjectRef("storage/role", "viewer")]
    assert len(captured) == 1
    assert "/role" in captured[0]["sql"] or "%/role" in str(captured[0]["params"])
