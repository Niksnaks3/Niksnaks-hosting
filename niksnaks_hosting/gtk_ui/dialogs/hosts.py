from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from niksnaks_hosting.shared.remote.hosts_manager import HostsManager
from niksnaks_hosting.shared.remote.ssh_client import SshError


def show_hosts_dialog(parent: Gtk.Window, hosts: HostsManager) -> None:
    dialog = Adw.PreferencesDialog()
    dialog.set_title(_("Hosts"))

    page = Adw.PreferencesPage(title=_("Hosts"))
    group = Adw.PreferencesGroup(
        title=_("Remote hosts"),
        description=_(
            "Servers can run on another machine over SSH. It needs systemd, Java is "
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

            status = Gtk.Label(label=_("Connected") if host.is_connected else _("Not connected"))
            status.add_css_class("caption")
            status.add_css_class("dim-label")
            row.add_suffix(status)

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
            "This app stops managing the host. Servers already on it are left exactly as "
            "they are, still running, and nothing on the machine is deleted."
        )
    )
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("remove", _("Remove"))
    dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_d, response):
        if response == "remove":
            hosts.remove(host.id)
            on_done()

    dialog.connect("response", on_response)
    dialog.present(parent)


def _show_add_host(parent, hosts: HostsManager, on_done) -> None:
    dialog = Adw.AlertDialog()
    dialog.set_heading(_("Add a host"))
    dialog.set_body(_("Niksnaks-Hosting connects over SSH and installs what it needs."))

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    listbox = Gtk.ListBox()
    listbox.add_css_class("boxed-list")
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)

    name_row = Adw.EntryRow(title=_("Name"))
    host_row = Adw.EntryRow(title=_("Address"))
    port_row = Adw.EntryRow(title=_("Port"))
    port_row.set_text("22")
    user_row = Adw.EntryRow(title=_("Username"))
    password_row = Adw.PasswordEntryRow(title=_("Password"))
    key_row = Adw.EntryRow(title=_("Private key file (optional)"))

    for row in (name_row, host_row, port_row, user_row, password_row, key_row):
        listbox.append(row)
    box.append(listbox)

    hint = Gtk.Label(
        label=_(
            "Leave the password empty to use a key or your SSH agent. A password is kept "
            "for this session only and is never written to disk."
        )
    )
    hint.add_css_class("caption")
    hint.add_css_class("dim-label")
    hint.set_wrap(True)
    hint.set_xalign(0.0)
    hint.set_margin_top(6)
    box.append(hint)

    dialog.set_extra_child(box)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("add", _("Add and check"))
    dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("add")
    dialog.set_close_response("cancel")

    def on_response(_d, response):
        if response != "add":
            return

        hostname = host_row.get_text().strip()
        username = user_row.get_text().strip()
        if not hostname or not username:
            _alert(parent, _("Missing details"), _("A host needs at least an address and a username."))
            return

        try:
            port = int(port_row.get_text().strip() or "22")
        except ValueError:
            port = 22

        host = hosts.add(
            name=name_row.get_text().strip(),
            hostname=hostname,
            username=username,
            port=port,
            password=password_row.get_text(),
            key_path=key_row.get_text().strip(),
        )

        def worker() -> None:
            try:
                ok, detail = host.check_ready()
            except Exception as e:
                ok, detail = False, str(e)

            def finish() -> bool:
                if ok:
                    # The key it answered with is trusted from here on.
                    hosts.trust_current_key(host.id)
                else:
                    _alert(parent, _("Could not use {}").format(host.name), detail)
                on_done()
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    dialog.connect("response", on_response)
    dialog.present(parent)


def _alert(parent, title: str, body: str) -> None:
    dialog = Adw.AlertDialog()
    dialog.set_heading(title)
    dialog.set_body(body)
    dialog.add_response("ok", _("OK"))
    dialog.present(parent)
