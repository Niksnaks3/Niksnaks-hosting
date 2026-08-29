"""Reading something that may live on another computer, without stopping the window.

A server on this machine is read straight through: it is a disk read, and sending it
round a thread would only make pages flicker on their way to the same answer. A server on
another machine is read on a thread, because the answer can be a whole connect timeout
away -- a computer that is switched off does not say so quickly -- and the main loop is
what draws the window and runs every other server's status.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib


def read_then_show(
    remote: bool,
    read: Callable[[], Any],
    show: Callable[[Any], None],
    fallback: Any,
    still_wanted: Callable[[], bool] | None = None,
) -> None:
    """Run *read* where it belongs, then hand its answer to *show* on the main thread.

    *show* only ever runs on the main thread, so it is free to build widgets. A machine
    that cannot be reached produces *fallback* rather than an exception: the page it was
    for is still on screen, and taking the rest of the tab down with it helps nobody.

    *still_wanted* drops an answer that arrived after the user moved on, which for a
    slow host is the difference between a page and somebody else's page.
    """
    if not remote:
        try:
            show(read())
        except Exception:
            show(fallback)
        return

    def worker() -> None:
        try:
            answer = read()
        except Exception:
            answer = fallback

        def finish() -> bool:
            if still_wanted is None or still_wanted():
                show(answer)
            return False

        GLib.idle_add(finish)

    threading.Thread(target=worker, daemon=True).start()
