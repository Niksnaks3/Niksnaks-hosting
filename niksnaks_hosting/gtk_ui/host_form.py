"""The "which computer" form, shared by the Hosts dialog and the create-server wizard.

Both places ask for the same six things and then have to do the same careful dance --
add the host, check it can actually run a server, and undo the add when it cannot --
so both the rows and that dance live here rather than being written twice.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from niksnaks_hosting.shared.remote.hosts_manager import HostsManager

DEFAULT_SSH_PORT = 22


@dataclass
class HostDraft:
    """What the user typed, once it is complete enough to try."""

    name: str
    hostname: str
    port: int
    username: str
    password: str
    key_path: str


@dataclass
class HostForm:
    """The rows of one host form, kept together so callers can read them back."""

    box: Gtk.Box
    name: Adw.EntryRow
    hostname: Adw.EntryRow
    port: Adw.EntryRow
    username: Adw.EntryRow
    password: Adw.PasswordEntryRow
    key_path: Adw.EntryRow


def build_host_form(show_name: bool = True) -> HostForm:
    """The address/username/password rows, plus the note about where a password goes."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

    listbox = Gtk.ListBox()
    listbox.add_css_class("boxed-list")
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)

    name_row = Adw.EntryRow(title=_("Name"))
    hostname_row = Adw.EntryRow(title=_("Address"))
    port_row = Adw.EntryRow(title=_("Port"))
    port_row.set_text(str(DEFAULT_SSH_PORT))
    username_row = Adw.EntryRow(title=_("Username"))
    password_row = Adw.PasswordEntryRow(title=_("Password"))
    key_row = Adw.EntryRow(title=_("Private key file (optional)"))

    rows = [name_row] if show_name else []
    rows += [hostname_row, port_row, username_row, password_row, key_row]
    for row in rows:
        listbox.append(row)
    box.append(listbox)

    hint = Gtk.Label(
        label=_(
            "Leave the password empty to use a key or your SSH agent. A password is used "
            "once to set up a key for this app, and is never written to disk."
        )
    )
    hint.add_css_class("caption")
    hint.add_css_class("dim-label")
    hint.set_wrap(True)
    hint.set_xalign(0.0)
    hint.set_margin_top(6)
    box.append(hint)

    return HostForm(
        box=box,
        name=name_row,
        hostname=hostname_row,
        port=port_row,
        username=username_row,
        password=password_row,
        key_path=key_row,
    )


def host_form_rows(form: HostForm) -> list[Gtk.Widget]:
    return [form.name, form.hostname, form.port, form.username, form.password, form.key_path]


def connect_host_form_changed(form: HostForm, callback: Callable[[], None]) -> None:
    """Call *callback* whenever any field changes, so a caller can re-validate."""
    for row in host_form_rows(form):
        row.connect("changed", lambda _row: callback())


def set_host_form_editable(form: HostForm, editable: bool) -> None:
    """Freeze the form while a connection attempt is in flight."""
    for row in host_form_rows(form):
        row.set_sensitive(editable)


def host_draft(form: HostForm) -> HostDraft | None:
    """What was typed, or None when it is not yet enough to try connecting."""
    hostname = form.hostname.get_text().strip()
    username = form.username.get_text().strip()
    if not hostname or not username:
        return None

    try:
        port = int(form.port.get_text().strip() or str(DEFAULT_SSH_PORT))
    except ValueError:
        port = DEFAULT_SSH_PORT
    if not 1 <= port <= 65535:
        port = DEFAULT_SSH_PORT

    return HostDraft(
        name=form.name.get_text().strip(),
        hostname=hostname,
        port=port,
        username=username,
        password=form.password.get_text(),
        key_path=form.key_path.get_text().strip(),
    )


def host_form_error(form: HostForm) -> str:
    """Why host_draft() came back empty, phrased for the person who typed it."""
    if not form.hostname.get_text().strip():
        return _("Enter the address of the computer to connect to.")
    if not form.username.get_text().strip():
        return _("Enter the username to log in as.")
    return ""


def add_host_and_verify(
    hosts: HostsManager,
    draft: HostDraft,
    on_step: Callable[[str], None],
    on_done: Callable[[object, str], None],
    still_wanted: Callable[[], bool] | None = None,
) -> None:
    """Add the host, check it can run a server, and set it up to reconnect on its own.

    Returns straight away; the work happens on a daemon thread and both callbacks are
    handed back on the main loop. *still_wanted* lets a caller that has moved on -- the
    wizard user who pressed Back -- drop a late answer. The bookkeeping is deliberately
    not gated on it: a host that failed must be removed whether or not anyone is still
    looking, or it is stranded in hosts.json with no password and no way back.

    on_done receives (host, detail). A None host means it could not be used and detail
    says why. A host with a non-empty detail worked, but something non-fatal is worth
    saying -- in practice, that key login could not be set up.
    """

    def wanted() -> bool:
        return still_wanted() if still_wanted else True

    def to_main(fn: Callable[[], None]) -> None:
        def once() -> bool:
            if wanted():
                fn()
            return False

        GLib.idle_add(once)

    def report(text: str) -> None:
        to_main(lambda: on_step(text))

    def worker() -> None:
        report(_("Connecting to {}…").format(draft.hostname))

        host = hosts.add(
            name=draft.name,
            hostname=draft.hostname,
            username=draft.username,
            port=draft.port,
            password=draft.password,
            key_path=draft.key_path,
        )

        try:
            ok, detail = host.check_ready()
        except Exception as e:  # a broken key file or a bad address lands here
            ok, detail = False, str(e)

        if not ok:
            hosts.remove(host.id)
            to_main(lambda: on_done(None, detail))
            return

        # The key it answered with is trusted from here on.
        hosts.trust_current_key(host.id)
        warning = _install_key(hosts, host, draft, report)
        to_main(lambda: on_done(host, warning))

    threading.Thread(target=worker, daemon=True).start()


def _install_key(hosts: HostsManager, host, draft: HostDraft, report: Callable[[str], None]) -> str:
    """Spend the typed password on a key, so no later launch has to ask for one.

    Returns "" when there is nothing to say, or a sentence explaining why this host will
    only work until the app closes.
    """
    from niksnaks_hosting.shared.remote.key_auth import ensure_key_auth

    # Nothing to convert. A host given a key file already reconnects on its own, and one
    # given neither is leaning on an SSH agent that will still be there next time.
    if draft.key_path or not draft.password:
        return ""

    try:
        result = ensure_key_auth(host, draft.password, progress=report)
    except Exception as e:
        installed, reason = False, str(e)
    else:
        installed, reason = result.installed, result.detail

    if installed:
        hosts.set_key_path(host.id, host.credentials.key_path)
        return ""

    # It still works right now -- it just cannot be reconnected to without being asked
    # again, so the password is held for this session rather than thrown away.
    hosts.set_password(host.id, draft.password)
    return _(
        "Servers on {} will work until you close the app. It could not be set up to "
        "reconnect on its own: {}"
    ).format(host.name, reason)
