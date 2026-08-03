from __future__ import annotations

from typing import Protocol

import niksnaks_hosting.i18n

class NiksnaksHostingApp(Protocol):
    def run(self, argv: list[str]) -> int: ...

def create_application() -> NiksnaksHostingApp:
    from niksnaks_hosting.gtk_ui.application import NiksnaksHostingApplication

    return NiksnaksHostingApplication()
