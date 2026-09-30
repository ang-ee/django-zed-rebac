"""A declared caveat parameter supplied as ``None`` is not a supplied value."""

from __future__ import annotations

import pytest

from rebac import ObjectRef, PermissionResult, RelationshipTuple, SubjectRef, backend, sudo
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.backend_setup import install_schema

SCHEMA = """
caveat not_flagged(flagged bool) { flagged != true }
definition auth/user {}
definition blog/post {
    relation viewer: auth/user with not_flagged
    permission read = viewer
}
"""

ALICE = SubjectRef.of("auth/user", "alice")

NONE_IS_SUPPLIED = (
    "caveats.evaluate counts a declared parameter present with value None as supplied and "
    "evaluates the CEL body with null, so a deny-list caveat allows (src/rebac/caveats.py:182)"
)


@pytest.fixture(autouse=True)
def _schema(db):
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA))
    yield
    reset_backend()


def _viewer(post_id: str, caveat_context: dict | None = None) -> RelationshipTuple:
    return RelationshipTuple(
        resource=ObjectRef("blog/post", post_id),
        relation="viewer",
        subject=ALICE,
        caveat_name="not_flagged",
        caveat_context=caveat_context,
    )


@pytest.mark.xfail(strict=True, reason=NONE_IS_SUPPLIED)
def test_check_access_with_none_parameter_is_conditional():
    backend().write_relationships([_viewer("p1")])
    post = ObjectRef("blog/post", "p1")

    def check(context):
        return backend().check_access(subject=ALICE, action="read", resource=post, context=context)

    assert check({"flagged": True}).result is PermissionResult.NO_PERMISSION
    assert check({"flagged": False}).result is PermissionResult.HAS_PERMISSION
    assert check({}).conditional_on == ("flagged",)

    result = check({"flagged": None})
    assert result.result is PermissionResult.CONDITIONAL_PERMISSION
    assert result.conditional_on == ("flagged",)


@pytest.mark.xfail(strict=True, reason=NONE_IS_SUPPLIED)
def test_accessible_with_none_parameter_excludes_row():
    backend().write_relationships([_viewer("p1")])

    def ids(context):
        return set(
            backend().accessible(
                subject=ALICE, action="read", resource_type="blog/post", context=context
            )
        )

    assert ids({"flagged": False}) == {"p1"}
    assert ids({"flagged": True}) == set()
    assert ids({}) == set()
    assert ids({"flagged": None}) == set()


@pytest.mark.xfail(strict=True, reason=NONE_IS_SUPPLIED)
def test_scoped_queryset_with_pinned_none_parameter_excludes_row():
    from tests.testapp.models import Post

    with sudo(reason="security.caveat-none.fixture"):
        allowed = Post.objects.create(title="allowed")
        flagged = Post.objects.create(title="flagged")
        pinned_none = Post.objects.create(title="pinned none")
    backend().write_relationships(
        [
            _viewer(str(allowed.pk), {"flagged": False}),
            _viewer(str(flagged.pk), {"flagged": True}),
            _viewer(str(pinned_none.pk), {"flagged": None}),
        ]
    )

    visible = set(Post.objects.with_actor(ALICE).values_list("pk", flat=True))

    assert allowed.pk in visible
    assert flagged.pk not in visible
    assert pinned_none.pk not in visible
