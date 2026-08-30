from __future__ import annotations

import getpass
import socket
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, GLib, Gtk

from niksnaks_hosting.gtk_ui.dialogs.host_password import ask_host_password
from niksnaks_hosting.gtk_ui.host_form import (
    add_host_and_verify,
    build_host_form,
    host_draft,
    host_form_error,
)
from niksnaks_hosting.shared.remote.hosts_manager import HostsManager
from niksnaks_hosting.shared.remote.ssh_client import SshError

SSH_PORT = 22


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

    # Sits in the group's header rather than as a row of its own: it acts on the whole
    # list below it, and a row would read as one more host to add.
    clear_all = Gtk.Button(label=_("Clear all…"), valign=Gtk.Align.CENTER)
    clear_all.add_css_class("flat")
    clear_all.set_tooltip_text(
        _("Forget every host here, leaving their servers running on their own machines")
    )
    clear_all.connect(
        "clicked",
        lambda button: _confirm_clear_all(dialog, hosts, manager, rebuild, button),
    )
    group.set_header_suffix(clear_all)

    rows: list[Gtk.Widget] = []

    def rebuild() -> None:
        for row in rows:
            group.remove(row)
        rows.clear()

        clear_all.set_visible(bool(hosts.ssh_hosts()))
        clear_all.set_sensitive(True)
        clear_all.set_label(_("Clear all…"))

        for host in hosts.ssh_hosts():
            row = Adw.ActionRow(title=host.name, subtitle=host.credentials.label)

            status = Gtk.Label(label=_("Connected") if host.is_connected else _("Checking…"))
            status.add_css_class("caption")
            status.add_css_class("dim-label")
            row.add_suffix(status)

            unlock = None
            if hosts.needs_password(host.id):
                # Nothing was written down to reconnect with, so a password may be the
                # only way back in -- unless the app's own key turns out to be on the
                # machine already, which is what the probe below finds out. Built now
                # and shown only if the probe fails, rather than nagging for a password
                # in front of a host that is working.
                unlock = Gtk.Button(label=_("Password…"), valign=Gtk.Align.CENTER)
                unlock.set_visible(False)
                unlock.connect(
                    "clicked",
                    lambda _b, h=host: ask_host_password(
                        dialog, hosts, h.id, lambda _ok, _detail: rebuild()
                    ),
                )
                row.add_suffix(unlock)

            _probe_host(host, status, unlock)

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
            remove.connect(
                "clicked",
                lambda _b, h=host: _confirm_remove(dialog, hosts, h, rebuild, manager),
            )
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
        add_row.connect("activated", lambda _r: _show_add_host(dialog, hosts, rebuild, manager))
        group.add(add_row)
        rows.append(add_row)

    rebuild()
    page.add(_this_computer_group(dialog))
    dialog.add(page)
    dialog.present(parent)


# --------------------------------------------------------------- this computer
def _local_address() -> str:
    """The address another machine on this network would reach this computer at.

    Asking a socket which of this computer's addresses it would leave by is the only
    reliable answer on a machine with several -- a VPN, a docker bridge, two cards.
    Nothing is actually sent; a UDP connect only picks the route.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return ""


def _ssh_is_listening() -> bool:
    """Whether anything answers on this computer's SSH port.

    Flatpak shares the host's network namespace, so 127.0.0.1 in here is the machine
    the app runs on rather than the sandbox, and this says something true about it.
    """
    sock = socket.socket()
    sock.settimeout(1.5)
    try:
        sock.connect(("127.0.0.1", SSH_PORT))
        return True
    except Exception:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _copy_row(dialog: Adw.PreferencesDialog, title: str, value: str, copied: str) -> Adw.ActionRow:
    """One detail to read off or copy, with the copy button doubling as the row action."""
    shown = value or _("Not available")
    row = Adw.ActionRow(title=title, subtitle=shown)

    if not value:
        return row

    def copy(*_args) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            dialog.add_toast(Adw.Toast(title=_("Could not access clipboard")))
            return
        display.get_clipboard().set(value)
        dialog.add_toast(Adw.Toast(title=copied))

    button = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
    button.add_css_class("flat")
    button.set_tooltip_text(_("Copy"))
    button.connect("clicked", copy)
    row.add_suffix(button)
    row.set_activatable(True)
    row.connect("activated", copy)
    return row


def _this_computer_group(dialog: Adw.PreferencesDialog) -> Adw.PreferencesGroup:
    """What to hand somebody who wants to run their servers on this machine.

    The mirror of the group above: those are machines this app reaches out to, this is
    what another copy of the app has to type to reach in.
    """
    group = Adw.PreferencesGroup(
        title=_("This computer"),
        description=_(
            "Somebody else running Niksnaks-Hosting can add this machine with these "
            "details and run their servers on it. They log in as a user account on this "
            "computer, so they need its password once. From outside your network, port "
            "{} also has to be forwarded to this computer."
        ).format(SSH_PORT),
    )

    group.add(_copy_row(dialog, _("Address"), _local_address(), _("Address copied")))
    group.add(_copy_row(dialog, _("Username"), getpass.getuser(), _("Username copied")))

    ssh_row = Adw.ActionRow(title=_("SSH server"), subtitle=_("Checking…"))
    group.add(ssh_row)

    def worker() -> None:
        listening = _ssh_is_listening()

        def finish() -> bool:
            if listening:
                ssh_row.set_subtitle(_("Port {} · accepting connections").format(SSH_PORT))
            else:
                # Without this nothing else on the page is usable to anyone, so it is
                # worth saying plainly rather than leaving them to find out by failing.
                ssh_row.set_subtitle(
                    _(
                        "Nothing is listening on port {}, so nobody can connect to this "
                        "computer yet. Turn on the OpenSSH server to allow it."
                    ).format(SSH_PORT)
                )
                ssh_row.add_css_class("error")
            return False

        GLib.idle_add(finish)

    threading.Thread(target=worker, daemon=True).start()
    return group


def _show_adopt(parent, manager, host) -> None:
    from niksnaks_hosting.gtk_ui.dialogs.adopt_servers import show_adopt_dialog

    show_adopt_dialog(parent, manager, host)


def _probe_host(host, status_label: Gtk.Label, unlock: Gtk.Button | None) -> None:
    """Say whether this machine can be reached, rather than guessing from the saved file.

    Only the login is tried, not check_ready(): opening a dialog must not be able to
    change anything on the far machine, and "can it be reached at all" is the question
    the row is there to answer. The Test button next to it still asks the fuller one.
    """

    def worker() -> None:
        try:
            host.connect()
            ok, detail = True, ""
        except Exception as e:
            ok, detail = False, str(e)

        def finish() -> bool:
            status_label.set_label(_("Connected") if ok else _("Not connected"))
            status_label.set_tooltip_text(detail or None)
            if unlock is not None:
                unlock.set_visible(not ok)
            return False

        GLib.idle_add(finish)

    threading.Thread(target=worker, daemon=True).start()


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


def _confirm_remove(parent, hosts: HostsManager, host, on_done, manager=None) -> None:
    dialog = Adw.AlertDialog()
    dialog.set_heading(_("Remove {}?").format(host.name))
    dialog.set_body(
        _(
            "This app stops managing the host, and its servers drop off the list here. "
            "If it has a login key on the machine, it tries to take that back off too, "
            "though a host that cannot be reached right now will keep its key line until "
            "it is. Servers already on it are left exactly as they are, still running, "
            "the machine's own copy of Niksnaks-Hosting still has them, and nothing on "
            "it is deleted."
        )
    )
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("remove", _("Remove"))
    dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_d, response):
        if response == "remove":
            _remove_host(parent, hosts, host, on_done, manager)

    dialog.connect("response", on_response)
    dialog.present(parent)


def _remove_host(parent, hosts: HostsManager, host, on_done, manager=None) -> None:
    """Take the app's key off the machine before forgetting how to reach it."""
    if manager is not None:
        # Before the host goes, not after: a server whose machine this app no longer
        # knows cannot even say where its folder is, which is enough to stop the whole
        # server list being written to disk. Forgetting it changes nothing on the
        # machine -- see ServerManager.forget_server.
        for info in manager.servers_on_host(host.id):
            manager.forget_server(info.id)

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


def _confirm_clear_all(parent, hosts: HostsManager, manager, on_done, button: Gtk.Button) -> None:
    """Start over with no remote machines, without disturbing any of them.

    Wanted after moving computers, handing the app to somebody else, or simply ending up
    with a list of machines that are not yours any more. It is emphatically not a delete:
    every server named here is on somebody's real machine, very likely with people on it,
    and the only thing that changes is that this install stops keeping track.
    """
    host_list = hosts.ssh_hosts()
    if not host_list:
        return

    server_count = 0
    if manager is not None:
        server_count = sum(len(manager.servers_on_host(host.id)) for host in host_list)

    dialog = Adw.AlertDialog()
    dialog.set_heading(
        _("Clear {} host?").format(len(host_list))
        if len(host_list) == 1
        else _("Clear all {} hosts?").format(len(host_list))
    )

    lines = [
        _(
            "Niksnaks-Hosting forgets every machine listed here: {}."
        ).format(", ".join(host.name for host in host_list))
    ]
    if server_count == 1:
        lines.append(_("The 1 server of theirs in your list disappears from it too."))
    elif server_count > 1:
        lines.append(_("The {} servers of theirs in your list disappear from it too.").format(server_count))
    lines.append(
        _(
            "Nothing on those machines is touched. Servers that are running keep "
            "running, no files are deleted, and each machine's own copy of "
            "Niksnaks-Hosting still has its servers. Only this computer stops keeping "
            "track of them. The app's login key is taken back off every machine it can "
            "still reach, the same as removing one host at a time."
        )
    )
    lines.append(_("Add a machine again later and its servers come back."))
    dialog.set_body("\n\n".join(lines))

    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("clear", _("Clear"))
    dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_d, response):
        if response == "clear":
            _clear_all_hosts(parent, hosts, manager, on_done, button)

    dialog.connect("response", on_response)
    dialog.present(parent)


def _clear_all_hosts(parent, hosts: HostsManager, manager, on_done, button: Gtk.Button) -> None:
    """Forget the servers first, then the machines they were on.

    That order matters. Forgetting a server lets its process object go of the console
    channel and the ownership marker it holds over SSH, which needs the host to still be
    reachable -- and taking the login key back off wants the same. So everything that
    talks to a machine happens while it is still a host, and the list is only emptied
    once none of it is needed again.
    """
    host_list = hosts.ssh_hosts()
    if manager is not None:
        for host in host_list:
            for info in manager.servers_on_host(host.id):
                manager.forget_server(info.id)

    # An unreachable machine holds this open for its whole connect timeout, and there
    # may be several of them, so the button says what is going on rather than looking
    # like a click that did nothing.
    button.set_sensitive(False)
    button.set_label(_("Clearing…"))

    def worker() -> None:
        from niksnaks_hosting.shared.remote.key_auth import KEY_PATH, revoke_key

        kept_keys: list[str] = []
        for host in host_list:
            # Only worth a round trip to a machine this app actually put its key on.
            if host.credentials.key_path != str(KEY_PATH):
                continue
            try:
                revoked = revoke_key(host)
            except Exception:
                revoked = False
            if not revoked:
                kept_keys.append(host.name)

        def finish() -> bool:
            hosts.remove_all()
            on_done()
            parent.add_toast(
                Adw.Toast(
                    title=_("Cleared 1 host")
                    if len(host_list) == 1
                    else _("Cleared {} hosts").format(len(host_list))
                )
            )
            if kept_keys:
                # The dialog promised the keys would come back off; say plainly where
                # they did not, rather than leaving live credentials on machines this
                # app no longer manages without mentioning it.
                _alert(
                    parent,
                    _("Could not remove every login key"),
                    _(
                        "These machines could not be reached, so this app's login key "
                        "is still on them: {}. It is harmless on its own, but if you "
                        "want it gone, remove it by hand or add the machine again "
                        "later just to take it back off."
                    ).format(", ".join(kept_keys)),
                )
            return False

        GLib.idle_add(finish)

    threading.Thread(target=worker, daemon=True).start()


def _show_add_host(parent, hosts: HostsManager, on_done, manager=None) -> None:
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

            if host is not None and manager is not None:
                # A machine is usually added because of what is already on it. Asking it
                # what its own copy of this app is running saves the user finding that
                # out and typing it in, and says nothing at all if there is nothing there.
                from niksnaks_hosting.gtk_ui.dialogs.adopt_servers import offer_adopt_if_any

                offer_adopt_if_any(parent, manager, host)

        add_host_and_verify(hosts, draft, on_step=lambda _text: None, on_done=finished)

    dialog.connect("response", on_response)
    dialog.present(parent)


def _alert(parent, title: str, body: str) -> None:
    dialog = Adw.AlertDialog()
    dialog.set_heading(title)
    dialog.set_body(body)
    dialog.add_response("ok", _("OK"))
    dialog.present(parent)
