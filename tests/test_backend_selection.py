"""``rebac.backend()`` selection per ARCHITECTURE.md § Settings catalog (``REBAC_BACKEND``).

``"spicedb"`` is reserved for the roadmap adapter and raises today; an unknown
value is rejected. A failed construction is never cached.
"""

from __future__ import annotations

import importlib.util

import pytest
from django.test import override_settings

import rebac.backends
from rebac import LocalBackend, ObjectRef, RelationshipTuple, SpiceDBBackend, SubjectRef, backend
from rebac.types import RelationshipFilter

_real_find_spec = importlib.util.find_spec


def _pretend_authzed_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def find_spec(name: str, *args, **kwargs):
        if name == "authzed":
            return object()
        return _real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr("rebac.backends.spicedb.importlib.util.find_spec", find_spec)


def test_local_backend_is_the_default_and_is_cached() -> None:
    first = backend()
    assert isinstance(first, LocalBackend)
    assert backend() is first


@pytest.mark.skipif(_real_find_spec("authzed") is not None, reason="authzed is installed")
def test_spicedb_without_client_names_the_extra() -> None:
    with (
        override_settings(REBAC_BACKEND="spicedb", REBAC_SPICEDB_ENDPOINT="localhost:50051"),
        pytest.raises(ImportError, match=r"django-zed-rebac\[spicedb\]"),
    ):
        backend()
    assert rebac.backends._backend is None


def test_spicedb_without_endpoint_names_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    _pretend_authzed_is_installed(monkeypatch)
    with (
        override_settings(REBAC_BACKEND="spicedb", REBAC_SPICEDB_ENDPOINT=None),
        pytest.raises(RuntimeError, match="REBAC_SPICEDB_ENDPOINT"),
    ):
        backend()
    assert rebac.backends._backend is None


def test_configured_spicedb_raises_until_the_adapter_ships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pretend_authzed_is_installed(monkeypatch)
    with (
        override_settings(
            REBAC_BACKEND="spicedb",
            REBAC_SPICEDB_ENDPOINT="localhost:50051",
            REBAC_SPICEDB_TOKEN="token",
        ),
        pytest.raises(NotImplementedError),
    ):
        backend()
    assert rebac.backends._backend is None


def test_unknown_backend_value_is_rejected() -> None:
    with (
        override_settings(REBAC_BACKEND="postgres"),
        pytest.raises(ValueError, match="'postgres'"),
    ):
        backend()
    assert rebac.backends._backend is None
    assert isinstance(backend(), LocalBackend)


_SUBJECT = SubjectRef.of("auth/user", "1")
_RESOURCE = ObjectRef("blog/post", "1")


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("check_access", {"subject": _SUBJECT, "action": "read", "resource": _RESOURCE}),
        ("accessible", {"subject": _SUBJECT, "action": "read", "resource_type": "blog/post"}),
        (
            "lookup_subjects",
            {"resource": _RESOURCE, "action": "read", "subject_type": "auth/user"},
        ),
        ("write_relationships", {"writes": []}),
        ("delete_relationships", {"filter_": RelationshipFilter(resource_type="blog/post")}),
        (
            "delete_relationship",
            {"tuple_": RelationshipTuple(resource=_RESOURCE, relation="owner", subject=_SUBJECT)},
        ),
        ("schema", {}),
    ],
)
def test_spicedb_stub_never_answers(method: str, kwargs: dict[str, object]) -> None:
    """The unimplemented adapter fails closed instead of returning a default."""
    stub = object.__new__(SpiceDBBackend)
    with pytest.raises(NotImplementedError):
        getattr(stub, method)(**kwargs)
