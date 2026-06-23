"""Field-level read enforcement.

``read__<field>`` is a normal permission name in the schema. These tests pin
the model-layer redaction behavior that consumes those permissions for every
transport, not just GraphQL.
"""

from __future__ import annotations

import pickle
from typing import Any, cast

import pytest
from django.test import override_settings

from rebac import (
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    backend,
    sudo,
    write_relationships,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed

SCHEMA_TEXT = """
definition auth/user {}

definition blog/post {
    relation owner: auth/user
    relation editor: auth/user
    relation viewer: auth/user

    permission read = owner + editor + viewer
    permission write = owner + editor
    permission read__title = owner
}
"""


NO_READ_GATE_SCHEMA_TEXT = """
definition auth/user {}

definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user

    permission read = owner + viewer
    permission write = owner
}
"""


CAVEAT_SCHEMA_TEXT = """
caveat link_not_expired(expires_at timestamp, now timestamp) {
    now < expires_at
}

definition auth/user {}

definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user
    relation gated_reader: auth/user with link_not_expired

    permission read = owner + viewer + gated_reader
    permission write = owner
    permission read__title = gated_reader
}
"""


WRITE_GATE_WITH_REDACTED_BODY_SCHEMA_TEXT = """
definition auth/user {}

definition blog/folder {
    relation owner: auth/user
    permission read = owner
    permission write = owner
}

definition blog/post {
    relation owner: auth/user
    relation editor: auth/user
    relation viewer: auth/user

    permission read = owner + editor + viewer
    permission write = owner + editor
    permission write__title = owner
    permission read__body = owner
}
"""


SLUGGED_SCHEMA_TEXT = """
definition auth/user {}

definition blog/sluggedpost {
    relation owner: auth/user
    relation editor: auth/user
    relation viewer: auth/user

    permission read = owner + editor + viewer
    permission write = owner + editor
    permission read__slug = owner
}
"""


PAST = "1999-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


@pytest.fixture
def alice(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username="alice", is_active=True)


@pytest.fixture
def bob(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username="bob", is_active=True)


def _post(*, title: str, body: str = ""):
    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        return Post.objects.create(title=title, body=body)


def _folder(*, name: str):
    from tests.testapp.models import Folder

    with sudo(reason="test.fixture"):
        return Folder.objects.create(name=name)


def _slugged_post(*, slug: str, title: str):
    from tests.testapp.models import SluggedPost

    with sudo(reason="test.fixture"):
        return SluggedPost.objects.create(slug=slug, title=title)


def _grant(
    post_pk: int,
    user: Any,
    relation: str,
    *,
    caveat_name: str = "",
    caveat_context: dict[str, Any] | None = None,
) -> None:
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(post_pk)),
                relation=relation,
                subject=SubjectRef.of("auth/user", str(user.pk)),
                caveat_name=caveat_name,
                caveat_context=caveat_context or {},
            ),
        ]
    )


def _grant_ref(resource_type: str, resource_id: str, user: Any, relation: str) -> None:
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef(resource_type, resource_id),
                relation=relation,
                subject=SubjectRef.of("auth/user", str(user.pk)),
            ),
        ]
    )


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_redaction_is_per_row_not_a_blanket_defer(alice, bob):
    from tests.testapp.models import Post

    alice_post = _post(title="alice title")
    bob_post = _post(title="bob title")
    _grant(alice_post.pk, alice, "owner")
    _grant(alice_post.pk, bob, "viewer")
    _grant(bob_post.pk, bob, "owner")
    _grant(bob_post.pk, alice, "viewer")

    rows = list(Post.objects.as_user(alice).order_by("pk"))

    assert [row.pk for row in rows] == [alice_post.pk, bob_post.pk]
    assert rows[0].title == "alice title"
    assert getattr(rows[0], "_rebac_redacted_fields", frozenset()) == frozenset()
    assert rows[1].title is None
    assert rows[1]._rebac_redacted_fields == frozenset({"title"})


def test_on_field_deny_omit_overrides_global_allow_and_survives_clones(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="not for alice")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    row = Post.objects.as_user(alice).on_field_deny("omit").filter(pk=post.pk).get()

    assert row.title is None
    assert row._rebac_redacted_fields == frozenset({"title"})
    assert row._rebac_omitted_fields == frozenset({"title"})


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_for_write_keeps_row_scope_but_disables_field_redaction(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="secret title")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "editor")  # can write, but read__title=owner redacts on a normal read

    # A normal read redacts the gated field for the non-owner editor.
    redacted = Post.objects.as_user(alice).get(pk=post.pk)
    assert redacted.title is None
    assert redacted._rebac_redacted_fields == frozenset({"title"})

    # for_write() = on_field_deny("allow"): redaction off so the write target carries
    # every column, while row scope still applies.
    writable = Post.objects.as_user(alice).for_write().get(pk=post.pk)
    assert writable.title == "secret title"
    assert getattr(writable, "_rebac_redacted_fields", frozenset()) == frozenset()

    # Row scope is preserved: a row the actor cannot access is still not returned.
    unrelated = _post(title="bob only")
    _grant(unrelated.pk, bob, "owner")
    assert Post.objects.as_user(alice).for_write().filter(pk=unrelated.pk).first() is None


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_values_projection_of_gated_field_fails_closed(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="projected secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    with pytest.raises(PermissionDenied) as excinfo:
        list(Post.objects.as_user(alice).values("title"))
    assert "read__" in str(excinfo.value)
    assert "title" in str(excinfo.value)


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_values_list_projection_of_gated_field_fails_closed(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="projected secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    with pytest.raises(PermissionDenied):
        list(Post.objects.as_user(alice).values_list("title", flat=True))


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_values_without_field_list_fails_closed_when_model_has_read_gates(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="projected secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    with pytest.raises(PermissionDenied):
        list(Post.objects.as_user(alice).values())


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_iterator_materialises_model_instances_with_field_redaction(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="streamed secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    row = next(Post.objects.as_user(alice).filter(pk=post.pk).iterator())

    assert row.title is None
    assert row._rebac_redacted_fields == frozenset({"title"})


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_values_iterator_of_gated_field_fails_closed(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="streamed projection secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    with pytest.raises(PermissionDenied):
        next(Post.objects.as_user(alice).values_list("title", flat=True).iterator())


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_pk_values_projection_remains_allowed_with_read_gates(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="pk projection")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    assert list(Post.objects.as_user(alice).values_list("pk", flat=True)) == [post.pk]


def test_no_read_gates_do_not_add_field_accessible_calls(alice, monkeypatch):
    from tests.testapp.models import Post

    backend().set_schema(parse_zed(NO_READ_GATE_SCHEMA_TEXT))
    post = _post(title="plain")
    _grant(post.pk, alice, "owner")
    active_backend = backend()
    actions: list[str] = []
    original_accessible = active_backend.accessible

    def counting_accessible(**kwargs: Any):
        actions.append(kwargs["action"])
        return original_accessible(**kwargs)

    monkeypatch.setattr(active_backend, "accessible", counting_accessible)

    with override_settings(REBAC_FIELD_READ_MODE="redact"):
        assert list(Post.objects.as_user(alice)) != []

    assert actions == ["read"]


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_redaction_scrubs_loaded_value_snapshot(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="snapshot secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    instance = Post.objects.as_user(alice).get(pk=post.pk)

    assert instance.title is None
    assert "title" not in instance._rebac_loaded_values


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_full_save_excludes_redacted_fields_from_the_update(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="stored secret", body="original")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance.title is None
    instance.body = "safe update"
    instance.save()

    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert fresh.title == "stored secret"
    assert fresh.body == "safe update"


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_full_save_uses_dirty_fields_minus_redacted_fields(alice, bob):
    from tests.testapp.models import Post

    backend().set_schema(parse_zed(WRITE_GATE_WITH_REDACTED_BODY_SCHEMA_TEXT))
    post = _post(title="visible title", body="stored secret")
    folder = _folder(name="new folder")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance.body is None
    instance.folder = folder
    instance.save()

    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert fresh.title == "visible title"
    assert fresh.body == "stored secret"
    assert fresh.folder_id == folder.pk


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_bulk_update_still_works_when_read_gates_are_enabled(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="bulk title", body="original")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    count = Post.objects.as_user(alice).filter(pk=post.pk).update(body="bulk safe")

    assert count == 1
    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert fresh.title == "bulk title"
    assert fresh.body == "bulk safe"


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_full_save_skips_deferred_fields_when_redaction_narrows_update_fields(alice, bob):
    from tests.testapp.models import Post

    backend().set_schema(parse_zed(WRITE_GATE_WITH_REDACTED_BODY_SCHEMA_TEXT))
    post = _post(title="deferred title", body="stored secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    instance = Post.objects.as_user(alice).only("id", "body", "folder").get(pk=post.pk)
    assert instance.body is None
    instance.folder = _folder(name="changed folder")
    instance.save()

    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert fresh.title == "deferred title"
    assert fresh.body == "stored secret"
    assert fresh.folder_id == instance.folder_id


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_django_6_rejects_positional_save_before_redacted_fields_can_write(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="stored secret", body="original")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance.title is None
    instance.body = "safe update"
    with pytest.raises(TypeError):
        instance.save(False, False, None, None)

    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert fresh.title == "stored secret"
    assert fresh.body == "original"


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_pickled_redacted_instance_preserves_write_safety_metadata(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="stored secret", body="original")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance.title is None
    restored = pickle.loads(pickle.dumps(instance)).with_actor(alice)
    restored.body = "after pickle"
    restored.save()

    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert fresh.title == "stored secret"
    assert fresh.body == "after pickle"


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_redacted_resource_id_attr_still_authorizes_writes_against_loaded_id(alice, bob):
    from tests.testapp.models import SluggedPost

    backend().set_schema(parse_zed(SLUGGED_SCHEMA_TEXT))
    post = _slugged_post(slug="visible-id", title="old title")
    _grant_ref("blog/sluggedpost", "visible-id", bob, "owner")
    _grant_ref("blog/sluggedpost", "visible-id", alice, "viewer")
    _grant_ref("blog/sluggedpost", "visible-id", alice, "editor")

    instance = SluggedPost.objects.as_user(alice).get(pk=post.pk)
    assert instance.slug is None
    instance.title = "new title"
    instance.save()

    with sudo(reason="test.verify"):
        fresh = SluggedPost.objects.get(pk=post.pk)
    assert fresh.slug == "visible-id"
    assert fresh.title == "new title"


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_explicit_save_of_redacted_field_fails_closed(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="stored secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance.title is None
    instance.title = "overwrite"

    with pytest.raises(PermissionDenied) as excinfo:
        instance.save(update_fields=["title"])
    assert "redacted" in str(excinfo.value)
    assert "title" in str(excinfo.value)


def test_instance_denied_read_fields_honours_caveat_context(alice):
    backend().set_schema(parse_zed(CAVEAT_SCHEMA_TEXT))
    post = _post(title="conditional title")
    _grant(post.pk, alice, "viewer")
    _grant(
        post.pk,
        alice,
        "gated_reader",
        caveat_name="link_not_expired",
        caveat_context={"expires_at": FUTURE},
    )

    assert post.with_actor(alice).denied_read_fields(context={"now": PAST}) == frozenset()
    assert post.with_actor(alice).denied_read_fields(context={"now": FUTURE}) == frozenset(
        {"title"}
    )
    assert post.with_actor(alice).denied_read_fields() == frozenset({"title"})


def test_bulk_conditional_field_reads_fail_closed_by_default_and_can_flip(alice):
    from tests.testapp.models import Post

    backend().set_schema(parse_zed(CAVEAT_SCHEMA_TEXT))
    post = _post(title="conditional title")
    _grant(post.pk, alice, "viewer")
    _grant(
        post.pk,
        alice,
        "gated_reader",
        caveat_name="link_not_expired",
        caveat_context={"expires_at": FUTURE},
    )

    with override_settings(REBAC_FIELD_READ_MODE="redact"):
        assert Post.objects.as_user(alice).get(pk=post.pk).title is None

    with override_settings(
        REBAC_FIELD_READ_MODE="redact",
        REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL=False,
    ):
        assert Post.objects.as_user(alice).get(pk=post.pk).title == "conditional title"


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_sudo_queryset_skips_field_redaction(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="sudo visible")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    row = Post.objects.sudo(reason="test").on_field_deny("redact").get(pk=post.pk)

    assert row.title == "sudo visible"
    assert getattr(row, "_rebac_redacted_fields", frozenset()) == frozenset()


@override_settings(REBAC_FIELD_READ_MODE="raise")
def test_raise_mode_degrades_to_redact_until_descriptor_tier_lands(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="redacted through raise")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    row = Post.objects.as_user(alice).get(pk=post.pk)

    assert row.title is None
    assert row._rebac_redacted_fields == frozenset({"title"})


def test_on_field_deny_rejects_unknown_modes():
    from tests.testapp.models import Post

    with pytest.raises(ValueError):
        Post.objects.on_field_deny(cast(Any, "explode"))


def test_on_field_deny_raise_surfaces_runtime_w008():
    from tests.testapp.models import Post

    with pytest.warns(RuntimeWarning, match="rebac.W008"):
        Post.objects.on_field_deny("raise")
