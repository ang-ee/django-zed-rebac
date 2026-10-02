"""Portable expiry bounds, in the project's datetime convention."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils import timezone

if TYPE_CHECKING:
    TIME_MIN: datetime
    TIME_MAX: datetime


def time_min() -> datetime:
    """Callable model default; also respects runtime USE_TZ overrides."""
    return datetime(1000, 1, 2, tzinfo=UTC if settings.USE_TZ else None)


def time_max() -> datetime:
    """Leave a day's margin for vendor connection-timezone conversion."""
    return datetime(9999, 12, 30, tzinfo=UTC if settings.USE_TZ else None)


def __getattr__(name: str) -> datetime:
    # Do not freeze settings into model defaults or require configured settings
    # merely to import a migration. These attributes are actual datetimes.
    if name == "TIME_MIN":
        return time_min()
    if name == "TIME_MAX":
        return time_max()
    raise AttributeError(name)


def application_now() -> datetime:
    """Application clock, bound as SQL parameters; never the database clock."""
    return timezone.now()
