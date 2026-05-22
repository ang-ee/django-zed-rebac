"""Backend ABC. All backends present this surface — see ARCHITECTURE.md § Unified check API."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

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


class Backend(ABC):
    """Mirror of `authzed.api.v1` surface, snake_cased.

    The ``at_zookie`` parameter on read methods (added in 0.4 per
    proposal 0002) carries the freshness floor for
    ``Consistency.AT_LEAST_AS_FRESH`` / ``AT_EXACT_SNAPSHOT`` reads.
    Backends MUST validate ``at_zookie.backend == self.kind`` and raise
    a clear error on mismatch — a SpiceDB zookie handed to LocalBackend
    is interpreted as a numeric xid and would silently corrupt results.
    """

    kind: str = ""

    @abstractmethod
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
        """Three-state. Combines model-level and record-level checks."""

    def has_access(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: dict | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> bool:
        """Boolean shorthand. CONDITIONAL collapses to False."""
        return self.check_access(
            subject=subject,
            action=action,
            resource=resource,
            context=context,
            consistency=consistency,
            at_zookie=at_zookie,
        ).allowed

    @abstractmethod
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
        """Set of resource_ids the subject has `action` on."""

    @abstractmethod
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
        """Reverse: who has `action` on this resource?"""

    @abstractmethod
    def write_relationships(self, writes: Iterable[RelationshipTuple]) -> Zookie: ...

    @abstractmethod
    def delete_relationships(self, filter_: RelationshipFilter) -> Zookie: ...

    @abstractmethod
    def delete_relationship(self, tuple_: RelationshipTuple) -> Zookie: ...

    @abstractmethod
    def schema(self) -> Schema:
        """Return the installed schema AST.

        Mirrors SpiceDB's ``ReadSchema`` — the parsed AST is the canonical
        in-process representation. Engine-side semantic checks (notably
        :func:`rebac.preflight.check_new`) require this to walk permission
        expressions before any row exists; ``lookup_subjects`` reverse
        walks will also lean on it once they grow past direct-relation
        rows. SpiceDBBackend will implement by caching the result of
        ``Client.ReadSchema()`` parsed through :func:`rebac.schema.parse_zed`.
        """
