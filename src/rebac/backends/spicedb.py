"""SpiceDBBackend — adapter for the official `authzed-py` client.

Stub for v0.1. Real implementation lands in 0.5 per the roadmap. The class
exists so `REBAC_BACKEND = "spicedb"` raises a clear ImportError today,
not a generic AttributeError.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..conf import app_settings
from ..schema.ast import Schema
from ..types import (
    CheckResult,
    Consistency,
    ObjectRef,
    RelationshipFilter,
    RelationshipTuple,
    SubjectRef,
    Zookie,
)
from .base import Backend


class SpiceDBBackend(Backend):
    """Wraps `authzed.api.v1.Client`. Not yet implemented in 0.1.

    To prepare a project for the eventual swap, write your code against the
    `Backend` ABC and let `LocalBackend` serve runtime; flip the setting and
    point at a SpiceDB cluster when 0.5 lands.
    """

    kind = "spicedb"

    def __init__(self) -> None:
        try:
            import authzed  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "REBAC_BACKEND='spicedb' requires the `authzed` package. "
                "Install with: pip install django-zed-rebac[spicedb]"
            ) from exc
        if not app_settings.REBAC_SPICEDB_ENDPOINT:
            raise RuntimeError("REBAC_SPICEDB_ENDPOINT must be set when REBAC_BACKEND='spicedb'")
        # Real client wiring is deferred to v0.5.
        raise NotImplementedError(
            "SpiceDBBackend is not yet implemented in 0.1. "
            "See ARCHITECTURE.md § Roadmap — phase 0.5."
        )

    def check_access(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: dict | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> CheckResult:
        raise NotImplementedError

    def accessible(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource_type: str,
        context: dict | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> Iterable[str]:
        raise NotImplementedError

    def lookup_subjects(
        self,
        *,
        resource: ObjectRef,
        action: str,
        subject_type: str,
        context: dict | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> Iterable[SubjectRef]:
        raise NotImplementedError

    def write_relationships(self, writes: Iterable[RelationshipTuple]) -> Zookie:
        raise NotImplementedError

    def delete_relationships(self, filter_: RelationshipFilter) -> Zookie:
        raise NotImplementedError

    def delete_relationship(self, tuple_: RelationshipTuple) -> Zookie:
        raise NotImplementedError

    def schema(self) -> Schema:
        raise NotImplementedError
