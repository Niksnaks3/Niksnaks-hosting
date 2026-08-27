from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from niksnaks_hosting.gtk_ui.dialogs.host_password import ask_host_password
from niksnaks_hosting.gtk_ui.host_form import (
    add_host_and_verify,
    build_host_form,
    host_draft,
    host_form_error,
)
from niksnaks_hosting.shared.remote.hosts_manager import HostsManager
from niksnaks_hosting.shared.remote.ssh_client import SshError


def show_hosts_dialog(parent: Gtk.Window, hosts: HostsManager, manager=None) -> None:
    dialog = Adw.PreferencesDialog()
    dialog.set_title(_("Hosts"))

    page = Adw.PreferencesPage(title=_("Hosts"))
    group = Adw.PreferencesGroup(
        title=_("Remote hosts"),
        description=_(
            "Servers can run on another machine over SSH, Windows or Linux. Java is "
            "installed for you, and servers keep running there after you close this app."
        ),
    )
    page.add(group)

    rows: list[Gtk.Widget] = []

    def rebuild() -> None:
        for row in rows:
            group.remove(row)
        rows.clear()

        for host in hosts.ssh_hosts():
            row = Adw.ActionRow(title=host.name, subtitle=host.credentials.label)

            if host.is_connected:
                state = _("Connected")
            elif hosts.needs_password(host.id):
                state = _("Needs password")
            else:
                state = _("Not connected")
            status = Gtk.Label(label=state)
            status.add_css_class("caption")
            status.add_css_class("dim-label")
            row.add_suffix(status)

            if hosts.needs_password(host.id):
                # No key was ever installed, so there is nothing to reconnect with until
                # someone types the password again.
                unlock = Gtk.Button(label=_("Password…"), valign=Gtk.Align.CENTER)
                unlock.connect(
                    "clicked",
                    lambda _b, h=host: ask_host_password(
                        dialog, hosts, h.id, lambda _ok, _detail: rebuild()
                    ),
                )
                row.add_suffix(unlock)

            if manager is not None:
                # The whole point of the far machine having this app installed too: its
                # servers can be taken on from here rather than recreated.
                adopt = Gtk.Button(label=_("Its servers…"), valign=Gtk.Align.CENTER)
                adopt.set_tooltip_text(
                    _("Control servers a Niksnaks-Hosting install on this machine already manages")
                )
                adopt.connect(
                    "clicked",
                    lambda _b, h=host: _show_adopt(dialog, manager, h),
                )
                row.add_suffix(adopt)

            test = Gtk.Button(label=_("Test"), valign=Gtk.Align.CENTER)
            test.connect("clicked", lambda _b, h=host, s=status: _test_host(dialog, h, s))
            row.add_suffix(test)

            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.set_tooltip_text(_("Remove this host"))
            remove.connect("clicked", lambda _b, h=host: _confirm_remove(dialog, hosts, h, rebuild))
            row.add_suffix(remove)

            group.add(row)
            rows.append(row)

        if not hosts.ssh_hosts():
            empty = Adw.ActionRow(
                title=_("No remote hosts yet"),
                subtitle=_("Add one to run servers on another machine."),
            )
            empty.set_activatable(False)
            group.add(empty)
            rows.append(empty)

        add_row = Adw.ActionRow(title=_("Add a host…"))
        add_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_row.set_activatable(True)
        add_row.connect("activated", lambda _r: _show_add_host(dialog, hosts, rebuild))
        group.add(add_row)
        rows.append(add_row)

    rebuild()
    dialog.add(page)
    dialog.present(parent)


def _show_adopt(parent, manager, host) -> None:
    from niksnaks_hosting.gtk_ui.dialogs.adopt_servers import show_adopt_dialog

    show_adopt_dialog(parent, manager, host)


def _test_host(parent, host, status_label: Gtk.Label) -> None:
    status_label.set_label(_("Checking…"))

    def worker() -> None:
        try:
            ok, detail = host.check_ready()
        except SshError as e:
            ok, detail = False, str(e)
        except Exception as e:  # a broken key file or a bad address lands here
            ok, detail = False, str(e)

        def finish() -> bool:
            status_label.set_label(_("Ready") if ok else _("Not ready"))
            if not ok:
                _alert(parent, _("Cannot use this host"), detail)
            return False

        GLib.idle_add(finish)

    threading.Thread(target=worker, daemon=True).start()


def _confirm_remove(parent, hosts: HostsManager, host, on_done) -> None:
    dialog = Adw.AlertDialog()
    dialog.set_heading(_("Remove {}?").format(host.name))
    dialog.set_body(
        _(
            "This app stops managing the host. If it has a login key on the machine, it "
            "tries to take that back off too, though a host that cannot be reached right "
            "now will keep its key line until it is. Servers already on it are left "
            "exactly as they are, still running, and nothing else on the machine is "
            "deleted."
        )
    )
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("remove", _("Remove"))
    dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_d, response):
        if response == "remove":
            _remove_host(parent, hosts, host, on_done)

    dialog.connect("response", on_response)
    dialog.present(parent)


def _remove_host(parent, hosts: HostsManager, host, on_done) -> None:
    """Take the app's key off the machine before forgetting how to reach it."""

    def worker() -> None:
        from niksnaks_hosting.shared.remote.key_auth import KEY_PATH, revoke_key

        # Only worth a round trip if this app ever put its key there; otherwise an
        # unreachable host would hold the removal open for the connect timeout.
        had_key = host.credentials.key_path == str(KEY_PATH)
        revoked = not had_key
        if had_key:
            try:
                revoked = revoke_key(host)
            except Exception:
                revoked = False

        def finish() -> bool:
            hosts.remove(host.id)
            if had_key and not revoked:
                # The dialog promised the key would come back off; say so when it did
                # not, rather than quietly leaving a live credential on a machine this
                # app no longer manages.
                _alert(
                    parent,
                    _("Could not remove the login key"),
                    _(
                        "{} could not be reached, so its login key for this app is "
                        "still on the machine. It is harmless on its own, but if you "
                        "want it gone, remove it by hand or add the host again later "
                        "just to take it back off."
                    ).format(host.name),
                )
            on_done()
            return False

        GLib.idle_add(finish)

    threading.Thread(target=worker, daemon=True).start()


def _show_add_host(parent, hosts: HostsManager, on_done) -> None:
    dialog = Adw.AlertDialog()
    dialog.set_heading(_("Add a host"))
    dialog.set_body(_("Niksnaks-Hosting connects over SSH and installs what it needs."))

    form = build_host_form()
    dialog.set_extra_child(form.box)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("add", _("Add and check"))
    dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("add")
    dialog.set_close_response("cancel")

    def on_response(_d, response):
        if response != "add":
            return

        draft = host_draft(form)
        if draft is None:
            _alert(parent, _("Missing details"), host_form_error(form))
            return

        def finished(host, detail: str) -> None:
            label = draft.name or f"{draft.username}@{draft.hostname}"
            if host is None:
                _alert(parent, _("Could not use {}").format(label), detail)
            elif detail:
                # It works, so the host is kept -- but say what will not survive a restart.
                _alert(parent, _("Added {}").format(label), detail)
            on_done()

        add_host_and_verify(hosts, draft, on_step=lambda _text: None, on_done=finished)

    dialog.connect("response", on_response)
    dialog.present(parent)


def _alert(parent, title: str, body: str) -> None:
    dialog = Adw.AlertDialog()
    dialog.set_heading(title)
    dialog.set_body(body)
    dialog.add_response("ok", _("OK"))
    dialog.present(parent)
