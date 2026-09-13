from __future__ import annotations

import os
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from niksnaks_hosting.gtk_ui.dialogs.progress_dialog import ProgressDialog
from niksnaks_hosting.i18n import LANGUAGES
from niksnaks_hosting.i18n import set_language as set_app_language
from niksnaks_hosting.shared.backend.preferences_manager import PreferencesManager
from niksnaks_hosting.shared.backend.server_manager import ServerManager
from niksnaks_hosting.shared.utils.constants import DATA_DIR
from niksnaks_hosting.shared.utils.storage import free_bytes, human_size

def _open_data_folder() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        os.startfile(str(DATA_DIR))
        return

    Gio.AppInfo.launch_default_for_uri(DATA_DIR.as_uri())

def show_preferences_window(
    parent: Gtk.Window, preferences: PreferencesManager, server_manager: ServerManager | None = None
):
    win = Adw.PreferencesDialog()

    page = Adw.PreferencesPage(title=_("General"))
    group = Adw.PreferencesGroup(
        title=_("Application"),
    )
    data_row = Adw.ActionRow(
        title=_("Data folder"),
        subtitle=_("{path}\n{free} free for servers and modpacks").format(
            path=DATA_DIR, free=human_size(free_bytes(DATA_DIR))
        ),
    )
    data_button = Gtk.Button(valign=Gtk.Align.CENTER)
    data_image = Gtk.Image.new_from_icon_name("folder-open-symbolic")
    data_button.set_child(data_image)
    data_button.connect("clicked", lambda _: _open_data_folder())
    data_row.add_suffix(data_button)
    group.add(data_row)

    bg_row = Adw.SwitchRow(
        title=_("Run in background"),
    )
    bg_row.set_active(preferences.run_in_background_on_close)

    startup_row = Adw.SwitchRow(
        title=_("Open Niksnaks-Hosting on startup"),
    )
    startup_row.set_active(preferences.open_on_startup)
    startup_row.set_sensitive(preferences.run_in_background_on_close)

    def on_bg_toggled(row, _pspec):
        active = row.get_active()
        preferences.run_in_background_on_close = active
        startup_row.set_sensitive(active)

        if not active and startup_row.get_active():
            startup_row.set_active(False)

        if active:

            from niksnaks_hosting.shared.utils.portal import request_background

            def on_bg_response(success, bg, auto, err):
                if not success or not bg:
                    GLib.idle_add(row.set_active, False)
                    GLib.idle_add(preferences.__setattr__, "run_in_background_on_close", False)

            request_background(False, on_bg_response)

    bg_row.connect("notify::active", on_bg_toggled)

    def on_startup_toggled(row, _pspec):
        active = row.get_active()
        preferences.open_on_startup = active

        from niksnaks_hosting.shared.utils.portal import request_background

        def on_start_response(success, bg, auto, err):
            if active and (not success or not auto):
                GLib.idle_add(row.set_active, False)
                GLib.idle_add(preferences.__setattr__, "open_on_startup", False)

        request_background(active, on_start_response)

    startup_row.connect("notify::active", on_startup_toggled)

    group.add(bg_row)
    group.add(startup_row)

    autobackup_row = Adw.SwitchRow(
        title=_("Auto backup world on stop"),
    )
    autobackup_row.set_active(preferences.auto_backup_on_stop)

    def on_autobackup_toggled(row, _pspec):
        preferences.auto_backup_on_stop = row.get_active()

    autobackup_row.connect("notify::active", on_autobackup_toggled)
    group.add(autobackup_row)

    autodelete_row = Adw.SwitchRow(
        title=_("Auto-delete backups older than 30 days"),
    )
    autodelete_row.set_active(preferences.auto_delete_old_backups)

    def on_autodelete_toggled(row, _pspec):
        preferences.auto_delete_old_backups = row.get_active()

    autodelete_row.connect("notify::active", on_autodelete_toggled)
    group.add(autodelete_row)

    dep_row = Adw.SwitchRow(
        title=_("Auto resolve mod dependencies"),
    )
    dep_row.set_active(preferences.auto_resolve_mod_dependencies)

    def on_dep_toggled(row, _pspec):
        preferences.auto_resolve_mod_dependencies = row.get_active()

    dep_row.connect("notify::active", on_dep_toggled)
    group.add(dep_row)

    lang_keys = list(LANGUAGES.keys())
    lang_names = list(LANGUAGES.values())
    language_model = Gtk.StringList.new(lang_names)

    lang_row = Adw.ComboRow(
        title=_("Language"),
        subtitle=_("Requires restart for changes to take effect"),
        model=language_model,
    )
    current_lang = preferences.language
    lang_row.set_selected(lang_keys.index(current_lang) if current_lang in lang_keys else 0)

    def on_language_changed(row, _pspec):
        selected = row.get_selected()
        lang_code = lang_keys[selected]
        preferences.language = lang_code
        set_app_language(lang_code)

    lang_row.connect("notify::selected", on_language_changed)
    group.add(lang_row)

    page.add(group)

    # Without a manager there is nothing that could delete a server properly, and a
    # half-working "delete everything" is worse than not offering it.
    if server_manager is not None:
        page.add(_reset_group(win, parent, preferences, server_manager))

    win.add(page)

    win.present(parent)

def _reset_group(
    win: Adw.PreferencesDialog,
    parent: Gtk.Window,
    preferences: PreferencesManager,
    server_manager: ServerManager,
) -> Adw.PreferencesGroup:
    """The way back to a copy of the app that has never been used.

    A button rather than a switch row, because it is not a setting and there is no
    flipping it back, and the red line under it is the whole point of the group:
    everything else in this window changes how the app behaves, and this one throws
    away what it has.
    """
    group = Adw.PreferencesGroup(
        title=_("Reset"),
        description=_(
            "Start over with a copy of Niksnaks-Hosting that has never been used. "
            "Servers you run on another machine over SSH are left alone: they keep "
            "running over there, and this app simply stops keeping track of them."
        ),
    )

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.set_margin_top(4)

    button = Gtk.Button(label=_("Reset Niksnaks-Hosting…"), halign=Gtk.Align.CENTER)
    button.add_css_class("destructive-action")
    button.add_css_class("pill")
    button.connect("clicked", lambda _b: _confirm_reset(win, parent, preferences, server_manager))
    box.append(button)

    warning = Gtk.Label(
        label=_(
            "This permanently deletes everything on this computer: every server, with "
            "its world, its mods and all of its backups, and every setting you have "
            "changed. It cannot be undone."
        )
    )
    warning.add_css_class("error")
    warning.add_css_class("caption")
    warning.set_wrap(True)
    warning.set_justify(Gtk.Justification.CENTER)
    warning.set_max_width_chars(48)
    box.append(warning)

    group.add(box)
    return group

def _ssh_hosts_manager():
    """The registry of hosts, or None on an install that cannot talk SSH at all.

    Opening Settings must not be the thing that first drags in the SSH stack, so it is
    asked for only once a reset is actually on the table -- and a build that shipped
    without paramiko is no reason a reset should not still clear this computer.
    """
    try:
        from niksnaks_hosting.shared.remote.hosts_manager import get_hosts_manager

        return get_hosts_manager()
    except Exception:
        return None

def _confirm_reset(
    win: Adw.PreferencesDialog,
    parent: Gtk.Window,
    preferences: PreferencesManager,
    manager: ServerManager,
) -> None:
    """Say exactly what is about to go, and what is not, before any of it does.

    Deleting one server puts an undo toast up for six seconds. Nothing here can do that
    -- the worlds, the backups and the settings all go at once -- so this dialog is the
    only chance anybody gets, and it is worth spending words on.
    """
    hosts = _ssh_hosts_manager()
    local = [info for info in manager.servers if not info.is_remote]
    remote = [info for info in manager.servers if info.is_remote]
    host_list = hosts.ssh_hosts() if hosts is not None else []

    dialog = Adw.AlertDialog()
    dialog.set_heading(_("Delete everything and start over?"))

    lines = []
    if len(local) == 1:
        lines.append(
            _(
                "The 1 server on this computer is deleted for good, with its world, its "
                "mods and every backup of it."
            )
        )
    elif local:
        lines.append(
            _(
                "All {} servers on this computer are deleted for good, with their "
                "worlds, their mods and every backup of them."
            ).format(len(local))
        )
    else:
        lines.append(_("There are no servers on this computer to delete."))

    lines.append(
        _(
            "Every setting, every Java runtime the app downloaded and everything in its "
            "cache goes with them. None of it can be undone, and there is nothing left "
            "behind to restore from."
        )
    )

    if len(remote) == 1:
        lines.append(
            _(
                "The 1 server you run on a machine over SSH is not deleted. It keeps "
                "running exactly as it is, nothing on that machine is touched, and that "
                "machine's own copy of Niksnaks-Hosting still has it."
            )
        )
    elif remote:
        lines.append(
            _(
                "The {} servers you run on machines over SSH are not deleted. They keep "
                "running exactly as they are, nothing on those machines is touched, and "
                "each machine's own copy of Niksnaks-Hosting still has them."
            ).format(len(remote))
        )

    if len(host_list) == 1:
        lines.append(
            _(
                "The host listed here is forgotten too, and this app's login key is "
                "taken back off it if it can still be reached. Add the machine again "
                "later and its servers come back."
            )
        )
    elif host_list:
        lines.append(
            _(
                "All {} hosts listed here are forgotten too, and this app's login key is "
                "taken back off every machine it can still reach. Add a machine again "
                "later and its servers come back."
            ).format(len(host_list))
        )

    lines.append(
        _("Niksnaks-Hosting closes when it is done. Open it again and it is the app you installed.")
    )
    dialog.set_body("\n\n".join(lines))

    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("reset", _("Delete everything"))
    dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def start() -> bool:
        _run_reset(win, parent, preferences, manager, hosts)
        return False

    def on_response(_d, response):
        if response == "reset":
            # Queued rather than run here, so this dialog is finished dismissing before
            # the settings page it was presented on is closed underneath it.
            GLib.idle_add(start)

    dialog.connect("response", on_response)
    dialog.present(win)

def _run_reset(
    win: Adw.PreferencesDialog,
    parent: Gtk.Window,
    preferences: PreferencesManager,
    manager: ServerManager,
    hosts,
) -> None:
    """Take the app apart, with something on screen saying so the whole time."""
    from niksnaks_hosting.shared.utils import factory_reset

    # This page is showing settings that are about to stop existing, and what replaces
    # it belongs to the window rather than to a page being dismantled underneath it.
    win.close()

    # The window's polls have to stop before anything is killed. Left running, the
    # runtime poll would watch every server on this computer go down at once and start
    # an auto backup of each one -- into the folders the worker is about to delete.
    parent.shutdown_background()

    progress = ProgressDialog(
        _("Resetting Niksnaks-Hosting"),
        detail=_("Deleting servers, settings and downloads..."),
        icon_name="user-trash-symbolic",
    )
    progress.present(parent)

    def worker(plan) -> None:
        report = factory_reset.run(plan, hosts, progress=progress.update)

        def done() -> bool:
            progress.finish()
            _finish_reset(parent, report)
            return False

        GLib.idle_add(done)

    def begin() -> bool:
        # Queued so the dialog above is on screen first. The records go on this thread,
        # where dropping the rows and killing the processes is quick, and it is what
        # stops anything else reading a server whose folder is about to be deleted.
        plan = factory_reset.prepare(manager, preferences)
        threading.Thread(target=worker, args=(plan,), daemon=True).start()
        return False

    GLib.idle_add(begin)

def _finish_reset(parent: Gtk.Window, report) -> None:
    """Say what the reset could not get rid of, and close the app either way.

    Closing is not tidiness. Every manager in this process is still holding servers,
    settings and hosts that no longer exist anywhere on disk, and the only honest way
    back from that is the launch that reads it all in again.
    """

    def quit_app(*_args) -> None:
        app = parent.get_application()
        if app is not None:
            app.activate_action("quit", None)

    if report.clean:
        quit_app()
        return

    from niksnaks_hosting.shared.utils.factory_reset import describe_leftovers

    lines = []
    if report.kept_keys:
        # The dialog promised the keys would come back off; say plainly where they did
        # not, rather than leaving a live credential on a machine this app has just
        # finished forgetting without mentioning it.
        lines.append(
            _(
                "These machines could not be reached, so this app's login key is still "
                "on them: {}. It is harmless on its own, but if you want it gone, "
                "remove it by hand."
            ).format(", ".join(report.kept_keys))
        )
    if report.leftovers:
        lines.append(
            _(
                "These were still in use and would not delete: {}. Niksnaks-Hosting "
                "clears them the next time it opens."
            ).format(describe_leftovers(report.leftovers))
        )

    dialog = Adw.AlertDialog()
    dialog.set_heading(_("Reset, with a couple of leftovers"))
    dialog.set_body("\n\n".join(lines))
    dialog.add_response("ok", _("Close the app"))
    dialog.connect("response", quit_app)
    dialog.present(parent)
