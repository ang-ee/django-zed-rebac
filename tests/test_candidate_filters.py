"""Candidate filters keep native SQL lookup semantics without speculative facts."""

from __future__ import annotations

import pytest
from django.db import connection, models
from django.db.models import Value
from django.db.models.expressions import DatabaseDefault
from django.test import override_settings
from django.test.utils import isolate_apps

from rebac import sudo
from rebac._candidate_filters import _filter_candidate_targets
from tests.testapp.models import AuthoredPost, Folder, Post


@pytest.fixture
def folder(db):
    with sudo(reason="test.candidate-filter.fixture"):
        return Folder.objects.create(name="target", is_active=True)


def _filter(candidate, field_name, target, filters):
    return _filter_candidate_targets(
        candidate,
        candidate._meta.get_field(field_name),
        type(target)._base_manager.using("default").filter(pk=target.pk),
        filters,
        using="default",
    )


@pytest.mark.parametrize(
    ("filters", "matches"),
    [
        ({"title__icontains": "VIEW", "folder__is_active": True}, True),
        ({"title__startswith": "other", "folder__is_active": True}, False),
        ({"title__isnull": True}, False),
        ({"folder__is_active": False}, False),
        ({"folder__isnull": False}, True),
    ],
)
def test_candidate_scalar_and_target_filters_use_native_sql(folder, filters, matches):
    candidate = Post(title="Review me", folder=folder)
    targets = _filter(candidate, "folder", folder, filters)
    assert targets is not None
    assert targets.exists() is matches


@pytest.mark.parametrize(("pattern", "matches"), [("^priority$", True), ("^PrIoRiTy$", False)])
def test_candidate_normalized_scalar_filter_matches_persisted_column(folder, pattern, matches):
    candidate = Folder(name="candidate", kind="PrIoRiTy", parent=folder)
    # Regex leaves the RHS untouched, exposing whether the candidate's LHS
    # receives the same LowercaseCharField preparation as the stored column.
    filters = {"kind__regex": pattern}
    targets = _filter(candidate, "parent", folder, filters)
    assert targets is not None
    assert targets.exists() is matches

    with sudo(reason="test.candidate-filter.normalized-scalar"):
        candidate.save()
    assert Folder._base_manager.filter(pk=candidate.pk, **filters).exists() is matches
    assert Folder._base_manager.get(pk=candidate.pk).kind == "priority"


def test_candidate_fk_attname_filter_uses_candidate_value(folder):
    targets = _filter(Post(folder=folder), "folder", folder, {"folder_id": folder.pk})
    assert targets is not None
    assert targets.exists()


@pytest.mark.parametrize("is_null", [False, True])
@pytest.mark.parametrize("lookup", ["folder__isnull", "folder__name__isnull"])
def test_known_null_candidate_fk_filters_keep_sql_null_semantics(
    folder, django_user_model, is_null, lookup
):
    author = django_user_model.objects.create(username="author")
    candidate = AuthoredPost(author=author, folder=None)
    targets = _filter(candidate, "author", author, {lookup: is_null})
    assert targets is not None
    assert targets.exists() is is_null


@pytest.mark.parametrize("active", [False, True])
def test_other_candidate_forward_fk_filters_use_persisted_target(folder, django_user_model, active):
    author = django_user_model.objects.create(username="author", is_active=active)
    candidate = AuthoredPost(author=author, folder=folder)
    targets = _filter(
        candidate,
        "folder",
        folder,
        {"author__is_active": True, "author__username__iexact": "AUTHOR"},
    )
    assert targets is not None
    assert targets.exists() is active


class _ReadReplicaRouter:
    def db_for_read(self, model, **hints):
        return "unavailable-read-replica"


def test_other_candidate_fk_filter_uses_write_alias(folder, django_user_model):
    author = django_user_model.objects.create(username="author", is_active=True)
    with override_settings(DATABASE_ROUTERS=[_ReadReplicaRouter()]):
        targets = _filter(
            AuthoredPost(author=author, folder=folder),
            "folder",
            folder,
            {"author__is_active": True},
        )
        assert targets is not None
        assert targets.exists()


@pytest.mark.parametrize(
    "filters",
    [
        {"collections__name": "new collection"},
        {"folder__posts__title": "new post"},
        {"folder__collected_posts__title": "new post"},
    ],
)
def test_filters_with_insert_dependent_relation_paths_are_unknown(folder, filters):
    assert _filter(Post(folder=folder), "folder", folder, filters) is None


@pytest.mark.parametrize("value", [Value("title"), DatabaseDefault(Value("title"))])
def test_candidate_expression_scalar_filter_is_unknown(folder, value):
    assert _filter(Post(title=value, folder=folder), "folder", folder, {"title": "title"}) is None


@pytest.mark.parametrize("field_attribute", ["generated", "auto_now", "auto_now_add"])
def test_candidate_insert_populated_scalar_filter_is_unknown(folder, monkeypatch, field_attribute):
    monkeypatch.setattr(Post._meta.get_field("title"), field_attribute, True, raising=False)
    assert _filter(Post(title="title", folder=folder), "folder", folder, {"title": "title"}) is None


def test_candidate_database_assigned_pk_filter_is_unknown(folder):
    assert _filter(Post(folder=folder), "folder", folder, {"pk__isnull": False}) is None


def test_candidate_missing_other_fk_target_still_raises(folder):
    with pytest.raises(ValueError, match="proposed related object is unavailable"):
        _filter(
            AuthoredPost(author_id=999_999, folder=folder),
            "folder",
            folder,
            {"author__is_active": True},
        )


def test_candidate_non_scalar_prepared_other_fk_still_raises(folder, monkeypatch):
    field = AuthoredPost._meta.get_field("author")
    monkeypatch.setattr(field.target_field, "get_prep_value", lambda value: Value(value))
    with pytest.raises(ValueError, match="foreign-key identity did not resolve to a scalar"):
        _filter(
            AuthoredPost(author_id=1, folder=folder),
            "folder",
            folder,
            {"author__is_active": True},
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("path", ["metadata", "other__metadata"])
@pytest.mark.parametrize(
    ("suffix", "expected", "matches"), [("", None, False), ("__isnull", True, True)]
)
@isolate_apps("tests.testapp")
def test_candidate_json_none_filters_match_persisted_sql_null(path, suffix, expected, matches):
    class JsonTarget(models.Model):
        metadata = models.JSONField(null=True)

        class Meta:
            app_label = "testapp"

    class JsonCandidate(models.Model):
        target = models.ForeignKey(JsonTarget, on_delete=models.CASCADE, related_name="+")
        other = models.ForeignKey(JsonTarget, on_delete=models.CASCADE, null=True, related_name="+")
        metadata = models.JSONField(null=True)

        class Meta:
            app_label = "testapp"

    with connection.schema_editor() as editor:
        editor.create_model(JsonTarget)
        editor.create_model(JsonCandidate)
    try:
        target = JsonTarget.objects.create(metadata={"existing": True})
        candidate = JsonCandidate(target=target, other=None, metadata=None)
        filters = {f"{path}{suffix}": expected}
        projected = _filter(candidate, "target", target, filters)
        assert projected is not None
        assert projected.exists() is matches

        candidate.save()
        assert JsonCandidate.objects.filter(pk=candidate.pk, **filters).exists() is matches
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(JsonCandidate)
            editor.delete_model(JsonTarget)
