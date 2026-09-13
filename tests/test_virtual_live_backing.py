"""Live backing remains queryable when public IDs are virtual field values."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import override_settings

from rebac import (
    LocalBackend,
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    backend,
    sudo,
    to_object_ref,
    to_subject_ref,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.testapp.models import (
    AuthoredPost,
    PrimarySluggedPost,
    SluggedPost,
    SlugReference,
    VirtualFolder,
    VirtualPost,
)

pytestmark = pytest.mark.django_db(transaction=True)

SCHEMA = """
definition auth/user {}

definition test/virtualfolder {
    relation owner: auth/user
    relation roster: auth/user // rebac:field={"path":"authored_posts__author","filters":{"authored_posts__role":"editor","authored_posts__confirmed":true,"authored_posts__dismissed":false}}
    permission read = owner + roster
}

definition test/virtualpost {
    relation folder: test/virtualfolder // rebac:field=folder
    relation collections: test/virtualfolder // rebac:field=collections
    relation shared: auth/user
    relation blocked: auth/user
    permission read = (shared + folder->read) - blocked
    permission collaborate = shared & folder->read
    permission collected_read = collections->read
}

definition test/kind {
    relation member: test/virtualfolder // rebac:attribute={"field":"kind"}
    permission inspect = member
}

definition test/adminrole {
    relation member: test/virtualfolder // rebac:attribute={"field":"kind","resource":"admin","value":"admin","filters":{"is_active":true}}
    permission inspect = member
}

definition blog/sluggedpost {
    relation reader: auth/user
    permission read = reader
}

definition test/primarysluggedpost {
    relation reader: auth/user
    permission read = reader
}

definition test/slugreference {
    relation slug_target: blog/sluggedpost // rebac:field=target
    relation pk_target: test/primarysluggedpost // rebac:field=target
    permission via_slug = slug_target->read
    permission via_pk = pk_target->read
}
"""


@pytest.fixture(params=["denormalized", "registry"])
def active(request):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        reset_backend()
        local = backend()
        assert isinstance(local, LocalBackend)
        local.set_schema(parse_zed(SCHEMA))
        try:
            yield local
        finally:
            reset_backend()


@pytest.fixture
def actors(active, django_user_model):
    return (
        django_user_model.objects.create_user(username="alice"),
        django_user_model.objects.create_user(username="bob"),
    )


def _grant(row, relation, actor):
    return RelationshipTuple(to_object_ref(row), relation, to_subject_ref(actor))


def test_virtual_source_and_fk_target_ids_use_field_conversion_everywhere(active, actors):
    alice, _bob = actors
    with sudo(reason="virtual live backing fixtures"):
        folder = VirtualFolder.objects.create(name="team")
        post = VirtualPost.objects.create(title="plan", folder=folder)
    active.write_relationships([_grant(folder, "owner", alice)])

    assert folder.virtual_id == f"item-{folder.pk}"
    assert post.virtual_id == f"item-{post.pk}"
    assert list(
        VirtualPost.objects.sudo(reason="inspect virtual identity")
        .filter(virtual_id=post.virtual_id)
        .values_list("virtual_id", flat=True)
    ) == [post.virtual_id]
    assert active.has_access(
        subject=SubjectRef.of("test/virtualfolder", folder.virtual_id),
        action="folder",
        resource=to_object_ref(post),
    )
    assert active.has_access(
        subject=to_subject_ref(alice), action="read", resource=to_object_ref(post)
    )
    assert list(
        active.lookup_subjects(
            resource=to_object_ref(post), action="folder", subject_type="test/virtualfolder"
        )
    ) == [SubjectRef.of("test/virtualfolder", folder.virtual_id)]
    assert set(
        active.accessible(
            subject=to_subject_ref(alice), action="read", resource_type="test/virtualpost"
        )
    ) == {post.virtual_id}
    assert list(VirtualPost.objects.with_actor(alice)) == [post]
    assert list(VirtualPost.objects.with_actor(alice).scoped()) == [post]


def test_virtual_scope_keeps_union_intersection_exclusion_and_revocation_lazy(active, actors):
    alice, bob = actors
    with sudo(reason="virtual permission expression fixtures"):
        folder = VirtualFolder.objects.create(name="team")
        both = VirtualPost.objects.create(title="both", folder=folder)
        inherited = VirtualPost.objects.create(title="inherited", folder=folder)
        manual = VirtualPost.objects.create(title="manual")
        blocked = VirtualPost.objects.create(title="blocked", folder=folder)
    with sudo(reason="virtual roster fixture"):
        roster = AuthoredPost.objects.create(
            title="membership",
            folder=folder,
            author=alice,
            role="editor",
            confirmed=True,
            dismissed=False,
        )
    active.write_relationships(
        [
            _grant(both, "shared", alice),
            _grant(manual, "shared", alice),
            _grant(blocked, "blocked", alice),
            _grant(manual, "shared", bob),
        ]
    )

    expected = {both.pk, inherited.pk, manual.pk}
    lazy = VirtualPost.objects.with_actor(alice)
    eager = VirtualPost.objects.with_actor(alice).scoped()
    assert set(lazy.values_list("pk", flat=True)) == expected
    assert set(eager.values_list("pk", flat=True)) == expected
    assert list(
        VirtualPost.objects.with_actor(alice)
        .with_action("collaborate")
        .values_list("pk", flat=True)
    ) == [both.pk]

    pending = VirtualPost.objects.with_actor(alice).order_by("pk")
    with sudo(reason="revoke virtual roster fixture"):
        roster.delete()
    assert list(pending.values_list("pk", flat=True)) == [manual.pk]
    assert list(eager.values_list("pk", flat=True)) == [manual.pk]
    assert active.has_access(
        subject=to_subject_ref(bob), action="read", resource=to_object_ref(manual)
    )


def test_filtered_reverse_roster_requires_one_matching_membership_row(active, actors):
    alice, _bob = actors
    with sudo(reason="virtual reverse roster fixtures"):
        folder = VirtualFolder.objects.create(name="team")
        post = VirtualPost.objects.create(title="plan", folder=folder)
        AuthoredPost.objects.create(
            title="wrong role",
            folder=folder,
            author=alice,
            role="viewer",
            confirmed=True,
            dismissed=False,
        )
        AuthoredPost.objects.create(
            title="dismissed editor",
            folder=folder,
            author=alice,
            role="editor",
            confirmed=False,
            dismissed=True,
        )
    assert list(VirtualPost.objects.with_actor(alice)) == []

    with sudo(reason="virtual current roster fixture"):
        membership = AuthoredPost.objects.create(
            title="current editor",
            folder=folder,
            author=alice,
            role="editor",
            confirmed=True,
            dismissed=False,
        )
    pending = VirtualPost.objects.with_actor(alice)
    assert active.has_access(
        subject=to_subject_ref(alice), action="read", resource=to_object_ref(post)
    )
    with sudo(reason="dismiss virtual roster fixture"):
        membership.dismissed = True
        membership.save(update_fields=["dismissed"])
    assert list(pending) == []


def test_attribute_kind_uses_virtual_subject_identity_for_checks_and_lookups(active, actors):
    _alice, _bob = actors
    with sudo(reason="virtual attribute identity fixtures"):
        admin = VirtualFolder.objects.create(name="admin", kind="admin")
        VirtualFolder.objects.create(name="other", kind="viewer")
    subject = to_subject_ref(admin)
    resource = ObjectRef("test/kind", "admin")

    assert subject == SubjectRef.of("test/virtualfolder", admin.virtual_id)
    assert active.has_access(subject=subject, action="inspect", resource=resource)
    assert list(
        active.lookup_subjects(
            resource=resource, action="member", subject_type="test/virtualfolder"
        )
    ) == [subject]
    assert set(active.accessible(subject=subject, action="inspect", resource_type="test/kind")) == {
        "admin"
    }


def test_virtual_m2m_arrow_and_lookup_revoke_before_queryset_evaluation(active, actors):
    alice, _bob = actors
    with sudo(reason="virtual m2m fixtures"):
        folder = VirtualFolder.objects.create(name="collection")
        post = VirtualPost.objects.create(title="collected")
        post.collections.add(folder)
    active.write_relationships([_grant(folder, "owner", alice)])
    folder_subject = SubjectRef.of("test/virtualfolder", folder.virtual_id)

    assert active.has_access(
        subject=folder_subject, action="collections", resource=to_object_ref(post)
    )
    assert list(
        active.lookup_subjects(
            resource=to_object_ref(post),
            action="collections",
            subject_type="test/virtualfolder",
        )
    ) == [folder_subject]
    assert active.has_access(
        subject=to_subject_ref(alice), action="collected_read", resource=to_object_ref(post)
    )
    assert list(VirtualPost.objects.with_actor(alice).with_action("collected_read")) == [post]

    pending = VirtualPost.objects.with_actor(alice).with_action("collected_read")
    with sudo(reason="revoke virtual m2m fixture"):
        post.collections.remove(folder)
    assert list(pending) == []
    assert not active.has_access(
        subject=folder_subject, action="collections", resource=to_object_ref(post)
    )


def test_fixed_attribute_admin_membership_uses_virtual_identity_and_active_filter(active, actors):
    _alice, _bob = actors
    with sudo(reason="fixed virtual attribute fixtures"):
        admin = VirtualFolder.objects.create(name="admin", kind="admin", is_active=True)
        VirtualFolder.objects.create(name="inactive", kind="admin", is_active=False)
    subject = to_subject_ref(admin)
    resource = ObjectRef("test/adminrole", "admin")

    assert active.has_access(subject=subject, action="inspect", resource=resource)
    assert list(
        active.lookup_subjects(
            resource=resource, action="member", subject_type="test/virtualfolder"
        )
    ) == [subject]
    assert set(
        active.accessible(subject=subject, action="inspect", resource_type="test/adminrole")
    ) == {"admin"}

    with sudo(reason="deactivate fixed virtual attribute fixture"):
        admin.is_active = False
        admin.save(update_fields=["is_active"])
    assert (
        list(
            active.lookup_subjects(
                resource=resource, action="member", subject_type="test/virtualfolder"
            )
        )
        == []
    )


def test_to_field_fk_projects_target_slug_or_pk_for_each_declared_identity(active, actors):
    alice, _bob = actors
    with sudo(reason="to-field virtual identity fixtures"):
        slug_target = SluggedPost.objects.create(slug="release", title="Release")
        pk_target = PrimarySluggedPost.objects.get(pk=slug_target.pk)
        reference = SlugReference.objects.create(target=slug_target)
    active.write_relationships(
        [_grant(slug_target, "reader", alice), _grant(pk_target, "reader", alice)]
    )
    slug_subject = SubjectRef.of("blog/sluggedpost", slug_target.slug)
    pk_subject = SubjectRef.of("test/primarysluggedpost", str(pk_target.pk))

    assert active.has_access(
        subject=slug_subject, action="slug_target", resource=to_object_ref(reference)
    )
    assert active.has_access(
        subject=pk_subject, action="pk_target", resource=to_object_ref(reference)
    )
    assert list(
        active.lookup_subjects(
            resource=to_object_ref(reference),
            action="slug_target",
            subject_type="blog/sluggedpost",
        )
    ) == [slug_subject]
    assert list(
        active.lookup_subjects(
            resource=to_object_ref(reference),
            action="pk_target",
            subject_type="test/primarysluggedpost",
        )
    ) == [pk_subject]
    for action in ("via_slug", "via_pk"):
        assert set(
            active.accessible(
                subject=to_subject_ref(alice),
                action=action,
                resource_type="test/slugreference",
            )
        ) == {reference.virtual_id}

    with patch.object(active, "accessible", side_effect=AssertionError("enumerated FK scope")):
        for action in ("via_slug", "via_pk"):
            assert list(
                SlugReference.objects.with_actor(alice)
                .with_action(action)
                .values_list("virtual_id", flat=True)
            ) == [reference.virtual_id]


def test_virtual_owned_corpus_is_not_enumerated_to_scope_sparse_grants(active, actors):
    alice, _bob = actors
    with sudo(reason="virtual corpus fixtures"):
        folder = VirtualFolder.objects.create(name="owned")
        granted = VirtualPost.objects.create(title="granted")
        VirtualPost.objects.bulk_create(
            [VirtualPost(title=f"row {index}", folder=folder) for index in range(100)]
        )
    active.write_relationships([_grant(granted, "shared", alice)])

    with patch.object(active, "accessible", side_effect=AssertionError("enumerated corpus")):
        assert list(VirtualPost.objects.with_actor(alice).values_list("virtual_id", flat=True)) == [
            granted.virtual_id
        ]


@pytest.mark.parametrize(
    "target_type",
    [
        "test/propertyfolder",
        "test/columnlessfolder",
        "test/nonexpressionfolder",
        "test/missinglookupfolder",
    ],
)
def test_live_backing_rejects_nonqueryable_subject_identity(active, target_type):
    from rebac.checks import check_field_backed_relations

    active.set_schema(
        parse_zed(
            f"""
            definition {target_type} {{}}
            definition test/virtualpost {{
                relation folder: {target_type} // rebac:field=folder
            }}
            """
        )
    )

    issues = check_field_backed_relations()
    assert any(issue.id == "rebac.E009" and "identity" in issue.msg for issue in issues)
