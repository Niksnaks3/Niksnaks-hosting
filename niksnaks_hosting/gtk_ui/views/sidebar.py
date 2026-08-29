import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, Gio, GObject, Gtk

from niksnaks_hosting.gtk_ui.background import read_then_show
from niksnaks_hosting.shared.backend.server_manager import ServerInfo, ServerManager
from niksnaks_hosting.shared.utils.constants import ServerStatus
from niksnaks_hosting.shared.utils.image_utils import load_texture

class ServerRow(Adw.ActionRow):
    def __init__(self, server_info: ServerInfo, server_manager: ServerManager):
        super().__init__()
        self.server_info = server_info
        self._server_manager = server_manager
        self._process = None
        self._status_handler_id = None
        self._players_handler_id = None

        self.set_title(server_info.name)
        self.set_subtitle(self._subtitle_text())
        self.set_tooltip_text(server_info.mc_version)
        self.set_activatable(True)

        self._avatar = Adw.Avatar(size=42, text=server_info.name, show_initials=True)
        self._avatar.add_css_class("server-avatar")
        self._update_icon()
        self.add_prefix(self._avatar)

        self._status_dot = Gtk.Label()
        self._status_dot.set_size_request(10, 10)
        self._status_dot.add_css_class("status-dot")
        self._status_dot.add_css_class("stopped")
        self._status_dot.set_valign(Gtk.Align.CENTER)
        self.add_suffix(self._status_dot)

        self.attach_existing_process()

    def _subtitle_text(self) -> str:
        parts = [self.server_info.mc_version, self._where_text()]
        if self._process and self._process.is_running:
            parts.append(f"{self._process.player_count}/{self._process.max_players}")
        return " · ".join(part for part in parts if part)

    def _where_text(self) -> str:
        """Which computer this server is on, for one that is not on this computer.

        Read from the saved host list and never from the machine itself: this runs on
        the main thread every time a player joins or leaves.
        """
        if not self.server_info.is_remote:
            return ""

        from niksnaks_hosting.shared.remote.hosts_manager import get_hosts_manager

        host = get_hosts_manager().get(self.server_info.host_id)
        name = host.name if host else _("unknown computer")
        if getattr(self._process, "host_unreachable", False):
            # Not the same thing as the server being off, and the difference is the
            # whole question when a row has gone quiet.
            return _("{} · not reachable").format(name)
        return name

    def _set_subtitle_text(self) -> None:
        text = self._subtitle_text()
        if text != self.get_subtitle():
            self.set_subtitle(text)

    def _update_icon(self):
        """Put this server's picture on the row, fetching it from its host if need be.

        A picture on another computer is an SFTP read away, which is not something the
        main loop can wait on -- so the row draws its initials and the picture replaces
        them when it arrives, if the row is still showing the same server by then.
        """
        info = self.server_info
        read_then_show(
            remote=info.is_remote,
            read=lambda: self._server_manager.icon_bytes(info),
            show=self._show_icon,
            fallback=b"",
            still_wanted=lambda: self.server_info.id == info.id,
        )

    def _show_icon(self, data: bytes) -> None:
        texture = load_texture(data)
        if texture is not None:
            self._avatar.set_custom_image(texture)

    def _on_status_changed(self, process, status):
        self._update_status(status)
        self._set_subtitle_text()

    def _on_players_changed(self, process, player_count, max_players):
        self._set_subtitle_text()

    def _update_status(self, status: str):
        for cls in ["running", "starting", "stopping", "stopped"]:
            self._status_dot.remove_css_class(cls)
        self._status_dot.add_css_class(status)
        if getattr(self._process, "host_unreachable", False) and status == ServerStatus.STOPPED:
            # A hollow dot for "nobody has been able to ask": filling it in grey would
            # be this row claiming to know the server is off, which is what it cannot do
            # while its machine is unreachable.
            self._status_dot.set_markup('<span foreground="#e5a50a">○</span>')
            return
        color = {
            ServerStatus.RUNNING: "#2ec27e",
            ServerStatus.STARTING: "#e5a50a",
            ServerStatus.STOPPING: "#e5a50a",
            ServerStatus.STOPPED: "#8e8e8e",
        }.get(status, "#8e8e8e")
        self._status_dot.set_markup(f'<span foreground="{color}">●</span>')

    def _disconnect_process_handlers(self):
        if self._process and self._status_handler_id:
            try:
                self._process.disconnect(self._status_handler_id)
            except Exception:
                pass
        if self._process and self._players_handler_id:
            try:
                self._process.disconnect(self._players_handler_id)
            except Exception:
                pass
        self._status_handler_id = None
        self._players_handler_id = None

    def attach_existing_process(self):
        process = self._server_manager.get_existing_process(self.server_info.id)
        if process is self._process and process is not None:
            return

        self._disconnect_process_handlers()
        self._process = process
        if process:
            self._status_handler_id = process.connect("status-changed", self._on_status_changed)
            self._players_handler_id = process.connect("players-changed", self._on_players_changed)
            self._update_status(process.status)
        else:
            self._update_status(ServerStatus.STOPPED)
        self._set_subtitle_text()

    def ensure_process_attached(self):
        if self._process:
            return
        process = self._server_manager.get_process(self.server_info.id)
        if process:
            self.attach_existing_process()

    def cleanup(self):
        self._disconnect_process_handlers()
        self._process = None

    def update_info(self, server_info: ServerInfo):
        self.server_info = server_info
        self.set_title(server_info.name)
        self._set_subtitle_text()
        self.set_tooltip_text(server_info.mc_version)
        self._update_icon()

    def refresh(self):
        """Catch up with a process that appeared, or an answer that arrived, since.

        A server on another computer is asked about on a thread, so the row is built
        long before there is anything to say about it -- and a machine can drop off the
        network while the row just sits there.
        """
        self.attach_existing_process()
        self._update_status(self._process.status if self._process else ServerStatus.STOPPED)
        self._set_subtitle_text()

class Sidebar(Gtk.Box):
    __gsignals__ = {
        "server-selected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, server_manager: ServerManager):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._server_manager = server_manager

        self._toolbar_view = Adw.ToolbarView()
        self._toolbar_view.set_hexpand(True)
        self._toolbar_view.set_vexpand(True)
        self.append(self._toolbar_view)
        self._rows: dict[str, ServerRow] = {}
        self.add_css_class("server-sidebar")

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        add_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        add_btn.set_tooltip_text(_("Create new server"))
        add_btn.add_css_class("flat")
        add_btn.set_action_name("app.new-server")
        add_btn.set_focusable(True)
        self._add_btn = add_btn
        header.pack_start(add_btn)

        title = Gtk.Label(label=_("Servers"))
        title.add_css_class("sidebar-header-title")
        header.set_title_widget(title)

        menu_btn = Gtk.MenuButton(valign=Gtk.Align.CENTER)
        menu_btn.set_icon_name("open-menu-symbolic")
        menu_btn.set_tooltip_text(_("Main menu"))
        menu_btn.add_css_class("flat")
        menu_btn.set_focusable(True)
        menu = Gio.Menu()
        menu.append(_("Hosts…"), "app.hosts")
        menu.append(_("Preferences"), "app.preferences")
        menu.append(_("Keyboard Shortcuts"), "app.shortcuts")
        menu.append(_("About Niksnaks-Hosting"), "app.about")
        menu_btn.set_menu_model(menu)
        self._menu_btn = menu_btn
        header.pack_end(menu_btn)

        self._toolbar_view.add_top_bar(header)

        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_vexpand(True)
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._listbox.add_css_class("navigation-sidebar")
        self._listbox.set_focusable(True)
        self._listbox.connect("row-selected", self._on_row_selected)

        self._scrolled.set_child(self._listbox)
        self._toolbar_view.set_content(self._scrolled)

        self._keyboard_controller = Gtk.EventControllerKey()
        self._keyboard_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._keyboard_controller.connect("key-pressed", self._on_key_pressed)
        self._listbox.add_controller(self._keyboard_controller)

        self._header_controller = Gtk.EventControllerKey()
        self._header_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._header_controller.connect("key-pressed", self._on_header_key_pressed)
        add_btn.add_controller(self._header_controller)

        self._menu_header_controller = Gtk.EventControllerKey()
        self._menu_header_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._menu_header_controller.connect("key-pressed", self._on_header_key_pressed)
        menu_btn.add_controller(self._menu_header_controller)

        server_manager.connect("server-added", self._on_server_added)
        server_manager.connect("server-removed", self._on_server_removed)
        server_manager.connect("server-changed", self._on_server_changed)

        self._populate()

        self._setup_context_menu()

    def _populate(self):
        for server in self._server_manager.servers:
            self._add_row(server)

    def _add_row(self, server_info: ServerInfo):
        row = ServerRow(server_info, self._server_manager)
        self._rows[server_info.id] = row
        self._listbox.append(row)

    def _on_row_selected(self, listbox, row):
        if row and isinstance(row, ServerRow):
            row.ensure_process_attached()
            self.emit("server-selected", row.server_info.id)

    def _on_server_added(self, manager, server_id):
        info = manager.get_server(server_id)
        if info:
            self._add_row(info)

            row = self._rows.get(server_id)
            if row:
                self._listbox.select_row(row)

    def _on_server_removed(self, manager, server_id):
        row = self._rows.pop(server_id, None)
        was_selected = row is not None and self._listbox.get_selected_row() is row
        if row:
            row.cleanup()
            self._listbox.remove(row)

        if was_selected:
            self._listbox.select_row(None)
            self.emit("server-selected", "")
            return

        if not self._rows:
            self.emit("server-selected", "")

    def _on_server_changed(self, manager, server_id):
        row = self._rows.get(server_id)
        info = manager.get_server(server_id)
        if row and info:
            row.update_info(info)

    def sync_rows(self):
        """Bring every row up to date with what its server turned out to be doing."""
        for row in self._rows.values():
            row.refresh()

    def select_server(self, server_id: str):
        row = self._rows.get(server_id)
        if row:
            self._listbox.select_row(row)

    def popup_main_menu(self):
        if hasattr(self, "_menu_btn") and self._menu_btn:
            self._menu_btn.grab_focus()
            self._menu_btn.popup()

    def focus_create_server(self):
        if hasattr(self, "_add_btn") and self._add_btn:
            self._add_btn.grab_focus()

    def focus_main_menu(self):
        if hasattr(self, "_menu_btn") and self._menu_btn:
            self._menu_btn.grab_focus()

    def focus_selected_server(self):
        row = self._listbox.get_selected_row()
        if row:
            row.grab_focus()

    def popup_selected_server_menu(self):
        row = self._listbox.get_selected_row()
        if not row or not isinstance(row, ServerRow):
            return

        row.grab_focus()

        menu = Gio.Menu()
        menu.append(_("Change Icon"), f"app.change-icon::{row.server_info.id}")
        menu.append(_("Rename…"), f"app.rename-server::{row.server_info.id}")
        menu.append(_("Delete"), f"app.delete-server::{row.server_info.id}")

        popover = Gtk.PopoverMenu(menu_model=menu)
        popover.set_parent(row)
        popover.connect("closed", lambda *_: row.grab_focus())
        popover.popup()

    def _on_key_pressed(self, controller, keyval, keycode, state):
        selected = self._listbox.get_selected_row()
        first_row = self._listbox.get_row_at_index(0)

        if keyval == Gdk.KEY_Up and selected and selected is first_row:
            self.focus_create_server()
            return True

        if keyval in (Gdk.KEY_Menu, Gdk.KEY_F10) and (state & Gdk.ModifierType.SHIFT_MASK or keyval == Gdk.KEY_Menu):
            self.popup_selected_server_menu()
            return True
        return False

    def _on_header_key_pressed(self, controller, keyval, keycode, state):
        widget = controller.get_widget()
        if keyval == Gdk.KEY_Right and widget is getattr(self, "_add_btn", None):
            self.focus_main_menu()
            return True
        if keyval == Gdk.KEY_Left and widget is getattr(self, "_menu_btn", None):
            self.focus_create_server()
            return True
        if keyval == Gdk.KEY_Down:
            self.focus_selected_server()
            return True
        return False

    def _setup_context_menu(self):
        gesture = Gtk.GestureClick()
        gesture.set_button(3)

        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect("pressed", self._on_right_click_pressed)
        gesture.connect("released", self._on_right_click_released)
        gesture.connect("cancel", self._on_right_click_cancel)
        gesture.connect("unpaired-release", self._on_right_click_cancel)
        self._listbox.add_controller(gesture)

        self._active_context_row = None

    def _on_right_click_pressed(self, gesture, n_press, x, y):
        if n_press != 1:
            return
        row = self._listbox.get_row_at_y(int(y))
        if not row or not isinstance(row, ServerRow):
            return

        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        row.add_css_class("context-active")
        self._active_context_row = row

    def _on_right_click_cancel(self, gesture, *args):
        if hasattr(self, "_active_context_row") and self._active_context_row:
            self._active_context_row.remove_css_class("context-active")
            self._active_context_row = None

    def _on_right_click_released(self, gesture, n_press, x, y):
        if hasattr(self, "_active_context_row") and self._active_context_row:
            row = self._active_context_row
            row.remove_css_class("context-active")
            self._active_context_row = None
        else:
            row = self._listbox.get_row_at_y(int(y))

        if not row or not isinstance(row, ServerRow):
            return

        server_info = row.server_info

        menu = Gio.Menu()
        menu.append(_("Change Icon"), f"app.change-icon::{server_info.id}")
        menu.append(_("Rename…"), f"app.rename-server::{server_info.id}")
        menu.append(_("Delete"), f"app.delete-server::{server_info.id}")

        popover = Gtk.PopoverMenu(menu_model=menu)

        popover.set_parent(self._listbox)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)

        row.add_css_class("context-open")

        def on_closed(p):
            row.remove_css_class("context-open")

        popover.connect("closed", on_closed)

        popover.popup()
