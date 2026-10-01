"""Autocommit lifecycle gap deferred to proposal 0012."""

import warnings

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models.signals import pre_save

from rebac import ObjectRef, RelationshipTuple, SubjectRef, backend, to_subject_ref
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.index_harness import assert_no_drift


@pytest.mark.django_db(transaction=True)
@pytest.mark.xfail(
    strict=True, reason="proposal 0012: a nested tuple owner reaps autocommit User old-state work"
)
def test_autocommit_user_attribute_move_with_consumer_pre_save_tuple_write():
    reset_backend()
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
        user = get_user_model().objects.create_user(username="bucket-user", last_name="a")
    subject = to_subject_ref(user)
    old = ObjectRef("test/bucket", "a")
    new = ObjectRef("test/bucket", "b")
    assert backend().has_access(subject=subject, action="read", resource=old)

    def nested(sender, instance, **kwargs):
        backend().write_relationships(
            [
                RelationshipTuple(
                    ObjectRef("blog/post", "consumer"),
                    "viewer",
                    SubjectRef.of("auth/user", "other"),
                )
            ]
        )

    pre_save.connect(nested, sender=get_user_model(), weak=False, dispatch_uid="proposal0012")
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            user.last_name = "b"
            user.save()
        assert any("D2" in str(w.message) for w in caught)
        assert backend().has_access(subject=subject, action="read", resource=new)
        assert not backend().has_access(subject=subject, action="read", resource=old)
        assert_no_drift()
    finally:
        pre_save.disconnect(sender=get_user_model(), dispatch_uid="proposal0012")
        reset_backend()
