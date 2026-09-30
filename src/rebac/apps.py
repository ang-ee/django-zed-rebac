"""App configuration. Static sender registration in ready() — no queries, no I/O."""

from __future__ import annotations

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

        signals.connect_tracked_signals()
