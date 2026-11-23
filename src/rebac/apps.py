"""App configuration. Static sender registration in ready() — no queries, no I/O."""

from __future__ import annotations

import logging

from django.apps import AppConfig


class RebacConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "rebac"
    verbose_name = "REBAC"
    label = "rebac"
    default = True

    def ready(self) -> None:
        # Connect signal handlers + register system checks. No DB queries here.
        from . import (
            checks,  # noqa: F401  — side-effect: register checks
            signals,
        )

        # Before the receivers: a value REBAC_TRACKED_MODELS does not accept raises there.
        log_wrongly_typed_settings()
        signals.connect_tracked_signals()
        from .conf import app_settings

        if not app_settings.REBAC_ENABLED:
            # A server that never runs the system checks still says so once.
            logging.getLogger("rebac").warning(
                "REBAC_ENABLED is False: no permission is checked. "
                "Every caller reads and writes every row."
            )


def log_wrongly_typed_settings() -> None:
    """Say once, on the ``rebac`` logger, what each setting the library does not accept is read as.

    A web or worker process runs no system checks, so ``rebac.E028`` and the settings' own
    checks never reach it; this does. A setting with no safe reading is logged as an error,
    one read closed or passed to readers that refuse it as a warning.
    """
    from .conf import _wrongly_typed_settings

    logger = logging.getLogger("rebac")
    for entry in _wrongly_typed_settings():
        log = logger.error if entry.raises else logger.warning
        log("%s: %s (%s).", entry.problem(), entry.reading(), entry.report)
