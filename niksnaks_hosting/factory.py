"""Application factory for Niksnaks-Hosting's GTK frontend."""

from __future__ import annotations

from typing import Protocol

import niksnaks_hosting.i18n  # noqa: F401 — installs _() into builtins early


class HostyApp(Protocol):
    """Common interface for app frontends."""

    def run(self, argv: list[str]) -> int: ...


def create_application() -> HostyApp:
    """Create the GTK frontend."""
    from niksnaks_hosting.gtk_ui.application import HostyApplication

    return HostyApplication()
