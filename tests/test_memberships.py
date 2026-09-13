"""Direct membership API behavior shared by roles, groups, and kind anchors."""

import pytest

from rebac.memberships import containers_of, grant, members_of, revoke
from rebac.models import Relationship
from rebac.types import ObjectRef, SubjectRef


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
