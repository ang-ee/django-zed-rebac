"""Ordered model declaration binding belongs to one native lowering operation."""

from unittest.mock import Mock, patch

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import F
from django.test import override_settings

from rebac import LocalBackend, SubjectRef, backend, resources
from rebac.backends import reset_backend
from rebac.compile import At, Compiler, read
from rebac.field_backing import _attribute_backing, _const_backing, _field_backing
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.testapp.models import Folder, Post


def test_ordered_alias_index_preserves_first_winner_and_exact_prefix(monkeypatch):
    monkeypatch.setattr(Folder._meta, "rebac_resource_type", "unconventional/Alias")
    monkeypatch.setattr(Post._meta, "rebac_resource_type", "unconventional/Alias")
    models = Mock(return_value=[Folder, Post])
    monkeypatch.setattr(apps, "get_models", models)
    with override_settings(REBAC_TYPE_PREFIX="tenantA/"):
        index = resources._ModelIndex.capture()
        assert index.resource("tenantA/unconventional/Alias") is Folder
        assert index.resource("tenantA/unconventional/alias") is None
        assert index.resource("unconventional/Alias") is None
        assert index.resource("missing/type") is None
        assert index.subject("tenantA/unconventional/Alias") == (Folder, "pk")
        models.assert_called_once_with()
        assert resources.model_for_resource_type("tenantA/unconventional/Alias") is Folder
        models.return_value = [Post, Folder]
        assert resources.model_for_resource_type("tenantA/unconventional/Alias") is Post
        assert index.resource("tenantA/unconventional/Alias") is Folder
        reset_backend()
        assert resources._ModelIndex.capture().resource("tenantA/unconventional/Alias") is Post


def test_subject_index_retains_resource_precedence_and_captured_fallbacks(monkeypatch):
    monkeypatch.setattr(Folder._meta, "rebac_resource_type", "people/user")
    with override_settings(
        REBAC_TYPE_PREFIX="a/",
        REBAC_USER_TYPE="people/user",
        REBAC_GROUP_TYPE="people/group",
        REBAC_USER_ID_ATTR="username",
    ):
        index = resources._ModelIndex.capture()
        assert index.subject("a/people/user") == (Folder, "pk")
        assert index.subject("a/people/group") == (Group, "username")
        assert index.subject("people/group") is None
        assert index.subject("unknown") is None
    with override_settings(
        REBAC_TYPE_PREFIX="b/",
        REBAC_USER_TYPE="people/user",
        REBAC_GROUP_TYPE="people/group",
        REBAC_USER_ID_ATTR="username",
    ):
        reset_backend()
        assert index.subject("a/people/user") == (Folder, "pk")
        assert index.subject("a/people/group") == (Group, "username")
        assert resources.model_for_subject_type("a/people/user") is None
        assert resources.model_for_subject_type("b/people/user") == (Folder, "pk")
        assert resources._ModelIndex.capture().subject("b/people/group") == (Group, "username")


def test_index_construction_does_not_require_auth_models():
    with patch(
        "django.contrib.auth.get_user_model", side_effect=AssertionError("eager auth lookup")
    ):
        index = resources._ModelIndex.capture()
        assert index.resource("blog/folder") is Folder
        assert index.subject("blog/folder") == (Folder, "pk")
        assert index.subject("not/an/auth/type") is None


def test_index_user_fallback_is_the_native_user_identity():
    with override_settings(REBAC_USER_ID_ATTR="username"):
        index = resources._ModelIndex.capture()
        assert index.subject("auth/user") == (get_user_model(), "username")
        assert resources.model_for_subject_type("auth/user") == index.subject("auth/user")


@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_operation_compilers_and_backings_share_one_ordered_index(monkeypatch, storage):
    schema = parse_zed("""
        definition auth/user {}
        definition site/role {
            relation member: auth/user // rebac:attribute={"field":"is_staff","resource":"staff","value":true}
            permission read = member
        }
        definition blog/folder {
            relation role: site/role // rebac:const=staff
            relation viewer: auth/user
            permission read = viewer + role->read
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->read
        }
    """)
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        reset_backend()
        local = backend()
        assert isinstance(local, LocalBackend)
        install_schema(local, schema)
        policy = read._policy(local, "default")
        enumerate_models = Mock(wraps=resources._model_types)
        monkeypatch.setattr(resources, "_model_types", enumerate_models)
        actor = SubjectRef.of("auth/user", "7")
        operation = read._Operation.begin(local, actor, "default", policy=policy)
        verdicts = operation.verdicts(("blog/post", "read"))
        first = operation.sets_compiler(verdicts)
        second = operation.compiler(verdicts, parametric=False)
        field = schema.get_definition("blog/post").relations[0]
        resolved = _field_backing(schema.get_definition("blog/post"), field, operation.model_index)
        assert (
            resolved is not None
            and resolved.source_model is Post
            and resolved.target_model is Folder
        )
        folder = schema.get_definition("blog/folder")
        role = next(relation for relation in folder.relations if relation.name == "role")
        assert _const_backing(folder, role, operation.model_index).source_model is Folder
        roles = schema.get_definition("site/role")
        assert (
            _attribute_backing(roles, roles.relations[0], operation.model_index).target_model
            is get_user_model()
        )
        assert first.model_index is second.model_index is operation.model_index
        # Building real predicates performs no further app-model enumeration.
        condition = second.holds(
            ("blog/post", "read"), At("blog/post", F("pk"), Post._meta.pk, True)
        )
        assert condition is not None
        enumerate_models.assert_called_once_with("")
        Compiler(schema, actor, "default")
        assert enumerate_models.call_count == 2
        reset_backend()
        read._Operation.begin(local, actor, "default")
        assert enumerate_models.call_count >= 3
