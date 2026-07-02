"""Public exception hierarchy."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied


class RebacError(Exception):
    """Base for all rebac errors."""


class PermissionDenied(DjangoPermissionDenied, RebacError):
    """Raised when an actor is denied access to a resource.

    Subclasses Django's `PermissionDenied` so view-layer 403 handlers continue
    to work without rebac-specific wiring.
    """


class MissingActorError(RebacError):
    """Raised when a queryset materialises with no actor and strict mode is on.

    Strict-by-default: REBAC_STRICT_MODE = True (production default).
    """


class NoActorResolvedError(RebacError):
    """Resolution chain produced no SubjectRef for the request."""


class CaveatUnsupportedError(RebacError):
    """Caveat references a CEL feature the active backend cannot evaluate.

    For example, the `ipaddress` CEL type is not supported by `cel-python`;
    `LocalBackend` raises this when checking such a caveat. `SpiceDBBackend`
    handles it natively.
    """


class PermissionDepthExceeded(RebacError):
    """Recursive permission walk hit `REBAC_DEPTH_LIMIT`."""


class SchemaError(RebacError):
    """Schema-level error (parse failure, undefined reference, etc.)."""


class RelationshipReadError(RebacError):
    """Raised for relationship-store reads that cannot be made storage-agnostic."""


class SudoNotAllowedError(RebacError):
    """`sudo()` called when REBAC_ALLOW_SUDO is False."""


class SudoReasonRequiredError(RebacError):
    """`sudo()` called without `reason=` and REBAC_REQUIRE_SUDO_REASON is True."""
