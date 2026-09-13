from __future__ import annotations

import pytest

from rebac.backends import LocalBackend, reset_backend
from rebac.backends import backend as active_backend
from rebac.checks import check_field_backed_relations
from rebac.schema import parse_zed


@pytest.fixture
def install_schema():
    def install(schema: str) -> None:
        reset_backend()
        backend = active_backend()
        assert isinstance(backend, LocalBackend)
        backend.set_schema(parse_zed(schema))

    try:
        yield install
    finally:
        reset_backend()


def test_default_subject_relation_must_be_a_relation_on_effective_type(db, install_schema):
    install_schema(
        """
        definition blog/subjectcontainer {
            permission member = nil
        }
        """
    )

    issues = check_field_backed_relations()

    assert any(
        issue.id == "rebac.E011"
        and "blog/subjectcontainer" in issue.msg
        and "not a declared relation" in issue.msg
        for issue in issues
    )


def test_default_subject_relation_accepts_declared_relation(db, install_schema):
    install_schema(
        """
        definition auth/user {}
        definition blog/subjectcontainer {
            relation member: auth/user
        }
        """
    )

    issues = check_field_backed_relations()

    assert not any(
        issue.id == "rebac.E011" and "blog/subjectcontainer" in issue.msg for issue in issues
    )


def test_attribute_backing_model_errors_are_reported_as_e009(db, install_schema):
    install_schema(
        """
        definition auth/user {}
        definition sample/kind {
            relation member: auth/user // rebac:attribute={"field":"missing"}
        }
        definition blog/subjectcontainer {
            relation member: auth/user
        }
        """
    )

    issues = check_field_backed_relations()

    assert any(
        issue.id == "rebac.E009" and "missing attribute 'missing'" in issue.msg for issue in issues
    )


def test_case_insensitive_attribute_collation_is_warned(db, install_schema, monkeypatch):
    from django.contrib.auth import get_user_model

    install_schema(
        """
        definition auth/user {}
        definition sample/kind {
            relation member: auth/user // rebac:attribute={"field":"username"}
        }
        """
    )
    field = get_user_model()._meta.get_field("username")
    monkeypatch.setattr(field, "db_collation", "utf8mb4_general_ci")

    issues = check_field_backed_relations()

    assert any(issue.id == "rebac.W009" and "username" in issue.msg for issue in issues)


def test_case_sensitive_attribute_collation_passes(db, install_schema):
    install_schema(
        """
        definition auth/user {}
        definition sample/kind {
            relation member: auth/user // rebac:attribute={"field":"username"}
        }
        """
    )

    assert not any(issue.id == "rebac.W009" for issue in check_field_backed_relations())
