from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango


class ProgressDialog(Adw.Dialog):
    """A determinate progress bar for work slow enough that people wonder if it hung.

    Worker threads call update() straight from the thread they are on: the dialog
    marshals onto the main loop itself and keeps only the newest value, so a download
    reporting every quarter of a megabyte cannot flood the loop with frames nobody
    could read anyway.

    The dialog holds the screen while it is up, because a half-written mods folder or a
    half-restored world is worse than waiting. That makes finish() mandatory on every
    path out of the work, including the failing ones.
    """

    def __init__(
        self,
        title: str,
        detail: str = "",
        icon_name: str = "folder-download-symbolic",
    ) -> None:
        super().__init__()
        self.set_can_close(False)
        self.set_content_width(420)
        self.set_title(title)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(28)
        box.set_margin_bottom(28)
        box.set_margin_start(24)
        box.set_margin_end(24)

        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(48)
        icon.add_css_class("dim-label")
        box.append(icon)

        self._title_label = Gtk.Label(label=title)
        self._title_label.add_css_class("title-3")
        self._title_label.set_wrap(True)
        self._title_label.set_justify(Gtk.Justification.CENTER)
        box.append(self._title_label)

        self._bar = Gtk.ProgressBar()
        self._bar.set_show_text(True)
        self._bar.add_css_class("niksnaks-hosting-progress")
        box.append(self._bar)

        # File names and pack paths are long and their tails are the informative part,
        # so squeeze the middle rather than wrapping to an ever-taller dialog.
        self._detail_label = Gtk.Label(label=detail)
        self._detail_label.add_css_class("dim-label")
        self._detail_label.add_css_class("caption")
        self._detail_label.set_max_width_chars(46)
        self._detail_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        box.append(self._detail_label)

        self.set_child(box)

        self._pending: tuple[float | None, str, str] | None = None
        self._flush_queued = False
        self._finished = False

    def update(self, fraction: float | None, title: str = "", detail: str = "") -> None:
        """Report progress from any thread. A fraction of None pulses instead of filling.

        Empty title or detail leaves whatever is on screen alone, so a caller reporting
        only bytes does not have to keep repeating the heading it already set.
        """
        if self._finished:
            return
        self._pending = (fraction, title, detail)
        if not self._flush_queued:
            self._flush_queued = True
            GLib.idle_add(self._flush)

    def _flush(self) -> bool:
        self._flush_queued = False
        pending = self._pending
        self._pending = None
        if pending is None or self._finished:
            return False

        fraction, title, detail = pending
        if fraction is None:
            self._bar.pulse()
        else:
            self._bar.set_fraction(max(0.0, min(1.0, fraction)))
        if title:
            self._title_label.set_label(title)
        if detail:
            self._detail_label.set_label(detail)
        return False

    def finish(self) -> None:
        """Release and close the dialog. Safe from any thread, and safe to call twice."""
        if self._finished:
            return
        self._finished = True

        def do_close() -> bool:
            self.set_can_close(True)
            self.close()
            return False

        GLib.idle_add(do_close)
