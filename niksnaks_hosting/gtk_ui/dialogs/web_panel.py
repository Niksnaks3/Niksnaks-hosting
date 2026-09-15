"""The Web panel dialog: switch it on, pick the port, share the address, manage its users."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from niksnaks_hosting.shared.backend.server_manager import ServerManager
from niksnaks_hosting.web.auth import UserStore, WebUser
from niksnaks_hosting.web.panel import WebPanel


def show_web_panel_dialog(parent, app, panel: WebPanel, manager: ServerManager) -> None:
    dialog = Adw.PreferencesDialog()
    dialog.set_title(_("Web panel"))

    page = Adw.PreferencesPage(title=_("Web panel"))
    page.add(_settings_group(dialog, parent, app, panel, manager))
    page.add(_users_group(dialog, panel, manager))
    dialog.add(page)
    dialog.present(parent)


def _settings_group(dialog, parent, app, panel: WebPanel, manager: ServerManager) -> Adw.PreferencesGroup:
    preferences = manager.preferences
    group = Adw.PreferencesGroup(
        title=_("Hosting"),
        description=_(
            "Let other people control servers from a browser, the way a hosting company "
            "would. The page is plain HTTP: share it on your own network, or put a reverse "
            "proxy with HTTPS in front before opening it to the internet."
        ),
    )

    url_row = Adw.ActionRow(title=_("Address"))
    url_row.set_activatable(False)
    copy_button = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
    copy_button.add_css_class("flat")
    copy_button.set_tooltip_text(_("Copy address"))
    url_row.add_suffix(copy_button)

    def refresh_url() -> None:
        if panel.is_running:
            url_row.set_subtitle(panel.url())
            copy_button.set_sensitive(True)
        else:
            url_row.set_subtitle(_("Off"))
            copy_button.set_sensitive(False)

    def apply() -> None:
        problem = app.apply_web_panel_settings()
        if problem:
            dialog.add_toast(Adw.Toast(title=problem))
        refresh_url()

    enable_row = Adw.SwitchRow(title=_("Host the web panel"))
    enable_row.set_active(preferences.web_panel_enabled)

    port_row = Adw.SpinRow.new_with_range(1024, 65535, 1)
    port_row.set_title(_("Port"))
    port_row.set_value(preferences.web_panel_port)

    def on_enable(row, _pspec):
        preferences.web_panel_enabled = row.get_active()
        apply()

    def on_port(row, _pspec):
        preferences.web_panel_port = int(row.get_value())
        if preferences.web_panel_enabled:
            apply()

    enable_row.connect("notify::active", on_enable)
    port_row.connect("notify::value", on_port)

    def on_copy(_button):
        parent.get_clipboard().set(panel.url())
        dialog.add_toast(Adw.Toast(title=_("Address copied")))

    copy_button.connect("clicked", on_copy)

    group.add(enable_row)
    group.add(port_row)
    group.add(url_row)
    refresh_url()
    return group


def _users_group(dialog, panel: WebPanel, manager: ServerManager) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(
        title=_("Users"),
        description=_(
            "Each user logs in with their own name and password and sees only the servers "
            "ticked for them. Anyone with a server can start and stop it, type into its "
            "console, change its mods, its picture and its message of the day, and update "
            "it to a newer Minecraft version -- give it to people you would hand the keys to."
        ),
    )
    rows: list[Gtk.Widget] = []

    def rebuild() -> None:
        for row in rows:
            group.remove(row)
        rows.clear()

        users = panel.users.users()
        for user in users:
            row = Adw.ActionRow(title=user.username, subtitle=_describe(user, manager))
            edit = Gtk.Button(label=_("Edit…"), valign=Gtk.Align.CENTER)
            edit.connect("clicked", lambda _b, u=user: _edit_user(dialog, panel, manager, rebuild, u))
            row.add_suffix(edit)
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.set_tooltip_text(_("Remove this user"))
            remove.connect("clicked", lambda _b, u=user: _confirm_remove(dialog, panel, rebuild, u))
            row.add_suffix(remove)
            group.add(row)
            rows.append(row)

        if not users:
            empty = Adw.ActionRow(
                title=_("No users yet"), subtitle=_("Nobody can log into the web panel until you add one.")
            )
            empty.set_activatable(False)
            group.add(empty)
            rows.append(empty)

        add_row = Adw.ActionRow(title=_("Add a user…"))
        add_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_row.set_activatable(True)
        add_row.connect("activated", lambda _r: _edit_user(dialog, panel, manager, rebuild, None))
        group.add(add_row)
        rows.append(add_row)

    rebuild()
    return group


def _describe(user: WebUser, manager: ServerManager) -> str:
    if user.admin:
        return _("Administrator — every server")
    names = [info.name for info in manager.servers if info.id in user.servers]
    if not names:
        return _("No servers")
    return ", ".join(names)


def _edit_user(parent, panel: WebPanel, manager: ServerManager, on_done, user: WebUser | None) -> None:
    dialog = Adw.AlertDialog()
    dialog.set_heading(_("Edit user") if user else _("Add a user"))

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    form = Gtk.ListBox()
    form.add_css_class("boxed-list")
    form.set_selection_mode(Gtk.SelectionMode.NONE)
    name_row = Adw.EntryRow(title=_("Username"))
    password_row = Adw.PasswordEntryRow(title=_("New password") if user else _("Password"))
    admin_row = Adw.SwitchRow(title=_("Administrator"), subtitle=_("Sees and controls every server"))
    form.append(name_row)
    form.append(password_row)
    form.append(admin_row)
    box.append(form)

    servers_box = Gtk.ListBox()
    servers_box.add_css_class("boxed-list")
    servers_box.set_selection_mode(Gtk.SelectionMode.NONE)
    checks: dict[str, Gtk.CheckButton] = {}
    for info in manager.servers:
        row = Adw.ActionRow(title=info.name, subtitle=info.mc_version)
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        servers_box.append(row)
        checks[info.id] = check
    label = Gtk.Label(label=_("Servers this user may control"), xalign=0)
    label.add_css_class("dim-label")
    box.append(label)
    box.append(servers_box)

    if user:
        name_row.set_text(user.username)
        name_row.set_editable(False)
        admin_row.set_active(user.admin)
        for sid, check in checks.items():
            check.set_active(sid in user.servers)

    def sync_servers(*_a):
        servers_box.set_sensitive(not admin_row.get_active())

    admin_row.connect("notify::active", sync_servers)
    sync_servers()
    dialog.set_extra_child(box)

    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("save", _("Save"))
    dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("save")
    dialog.set_close_response("cancel")

    def on_response(_d, response):
        if response != "save":
            return
        username = name_row.get_text().strip()
        password = password_row.get_text()
        if not UserStore.valid_username(username):
            _alert(parent, _("Bad username"), _("Use 1 to 32 letters, digits, dots, dashes or underscores."))
            return
        if not user and panel.users.get(username):
            _alert(parent, _("Taken"), _("There is already a user called {}.").format(username))
            return
        if not user and len(password) < 8:
            _alert(parent, _("Password too short"), _("Use at least 8 characters."))
            return
        if user and password and len(password) < 8:
            _alert(
                parent,
                _("Password too short"),
                _("Use at least 8 characters, or leave it empty to keep the old one."),
            )
            return
        granted = [sid for sid, check in checks.items() if check.get_active()]
        panel.users.set_user(username, password or None, granted, admin_row.get_active())
        if user:
            # Their sessions are dropped so a narrowed grant or new password takes effect now.
            panel.sessions.logout_user(username)
        on_done()

    dialog.connect("response", on_response)
    dialog.present(parent)


def _confirm_remove(parent, panel: WebPanel, on_done, user: WebUser) -> None:
    dialog = Adw.AlertDialog()
    dialog.set_heading(_("Remove {}?").format(user.username))
    dialog.set_body(_("They will be logged out and can no longer open the web panel."))
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("remove", _("Remove"))
    dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_d, response):
        if response == "remove":
            panel.users.remove(user.username)
            panel.sessions.logout_user(user.username)
            on_done()

    dialog.connect("response", on_response)
    dialog.present(parent)


def _alert(parent, title: str, body: str) -> None:
    dialog = Adw.AlertDialog()
    dialog.set_heading(title)
    dialog.set_body(body)
    dialog.add_response("ok", _("OK"))
    dialog.present(parent)
