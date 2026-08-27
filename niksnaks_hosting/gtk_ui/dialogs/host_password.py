"""Ask for a host's password when there is no key to fall back on.

Passwords are not written to disk, so a host that never got a key installed has nothing
to log in with after a restart. Rather than leave it broken, it is asked for once -- and
the answer is spent on installing the key, so the question does not come back.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from niksnaks_hosting.shared.remote.hosts_manager import HostsManager


def ask_host_password(parent, hosts: HostsManager, host_id: str, on_done=None) -> None:
    """Ask for *host_id*'s password, then try once more to set up key login with it."""
    host = hosts.get(host_id)
    if host is None or host.is_local:
        return

    dialog = Adw.AlertDialog()
    dialog.set_heading(_("Password for {}").format(host.name))
    dialog.set_body(
        _(
            "This app has no key on {} yet, so it needs the password once more. It will "
            "be used to set up a key, so this should be the last time."
        ).format(host.credentials.label)
    )

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    listbox = Gtk.ListBox()
    listbox.add_css_class("boxed-list")
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    password_row = Adw.PasswordEntryRow(title=_("Password"))
    listbox.append(password_row)
    box.append(listbox)

    status = Gtk.Label()
    status.add_css_class("caption")
    status.set_wrap(True)
    status.set_xalign(0.0)
    status.set_visible(False)
    box.append(status)

    dialog.set_extra_child(box)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("connect", _("Connect"))
    dialog.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("connect")
    dialog.set_close_response("cancel")

    def on_response(_d, response):
        if response != "connect":
            return

        password = password_row.get_text()
        if not password:
            return

        # Not held before it is tried: storing a wrong password would make
        # needs_password() report this host as having something to log in with, and
        # the "Password…" button that offers a retry would disappear for good.
        def worker() -> None:
            from niksnaks_hosting.shared.remote.key_auth import ensure_key_auth

            try:
                result = ensure_key_auth(host, password)
                installed, detail = result.installed, result.detail
            except Exception as e:
                installed, detail = False, str(e)

            # The key not taking and the password being wrong are different failures.
            # A password that logs in is worth keeping even with no key -- the host
            # works until the app closes, which is the whole reason set_password
            # exists -- while a wrong one has to go, or it masks the next attempt.
            password_works = False
            if not installed:
                try:
                    host.credentials.password = password
                    host.close()
                    host.connect()
                    password_works = True
                except Exception:
                    password_works = False

            def finish() -> bool:
                if installed:
                    hosts.set_key_path(host_id, host.credentials.key_path)
                else:
                    hosts.set_password(host_id, password if password_works else "")
                if on_done:
                    on_done(installed, detail)
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    dialog.connect("response", on_response)
    dialog.present(parent)
