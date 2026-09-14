import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from niksnaks_hosting.gtk_ui.views.connect import ConnectView
from niksnaks_hosting.gtk_ui.views.console_view import ConsoleView
from niksnaks_hosting.gtk_ui.views.files import FilesView
from niksnaks_hosting.gtk_ui.views.performance_view import PerformanceView
from niksnaks_hosting.gtk_ui.views.properties_view import PropertiesView
from niksnaks_hosting.shared.backend.server_manager import ServerInfo, ServerManager, StartRefusal
from niksnaks_hosting.shared.backend.server_process import ServerProcess
from niksnaks_hosting.shared.utils.constants import ServerStatus

class ServerDetailView(Gtk.Box):
    def __init__(self, server_manager: ServerManager, toast_overlay=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._server_manager = server_manager
        self._toast_overlay = toast_overlay
        self._current_server: ServerInfo | None = None
        self._selected_process: ServerProcess | None = None
        self._selected_status_handler_id = None
        self._mods_operation_handler_id = None
        self._general_status_connected: set[int] = set()

        self._tab_hosts: dict[str, Gtk.Box] = {}
        self._console_views: dict[str, ConsoleView] = {}
        self._console_stack: Gtk.Stack = Gtk.Stack()
        self._console_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._console_stack.set_transition_duration(150)
        self._connect_view: ConnectView | None = None
        self._perf_view: PerformanceView | None = None
        self._props_view: PropertiesView | None = None
        self._files_view: FilesView | None = None
        # Which server the Files tab was last pointed at. Reading a server's folder is
        # the expensive part of that tab -- a run of SFTP calls for one on another
        # computer -- so it is not done for a tab nobody is looking at.
        self._files_view_server_id = ""

        self._toolbar_view = Adw.ToolbarView()
        self._toolbar_view.set_hexpand(True)
        self._toolbar_view.set_vexpand(True)

        self._outer_nav = Adw.NavigationView()
        self._outer_nav.set_hexpand(True)
        self._outer_nav.set_vexpand(True)
        self._outer_nav_root = Adw.NavigationPage(title=_("Server Detail"), child=self._toolbar_view)
        try:
            self._outer_nav_root.set_tag("niksnaks-hosting-detail-root")
        except Exception:
            pass
        self._outer_nav.push(self._outer_nav_root)
        self.append(self._outer_nav)

        self._header = Adw.HeaderBar()
        self._header.set_show_start_title_buttons(False)

        self._view_switcher_title = Adw.ViewSwitcherTitle()
        self._view_switcher_title.set_title(_("Server"))
        self._header.set_title_widget(self._view_switcher_title)

        self._toggle_btn = Gtk.Button(label=_("Start"))
        self._toggle_btn.add_css_class("suggested-action")
        self._toggle_btn.connect("clicked", self._on_toggle_clicked)
        self._header.pack_end(self._toggle_btn)

        self._toolbar_view.add_top_bar(self._header)

        self._view_stack = Adw.ViewStack()
        self._view_stack.set_vexpand(True)
        self._view_stack.connect("notify::visible-child-name", self._on_tab_changed)
        self._view_switcher_title.set_stack(self._view_stack)

        self._add_lazy_tab("console", _("Console"), "utilities-terminal-symbolic")
        self._tab_hosts["console"].append(self._console_stack)
        self._add_lazy_tab("connect", _("Connect"), "network-workgroup-symbolic")
        self._add_lazy_tab("performance", _("Performance"), "power-profile-performance-symbolic")
        self._add_lazy_tab("properties", _("Properties"), "emblem-system-symbolic")
        self._add_lazy_tab("files", _("Files"), "folder-symbolic")
        self._view_stack.set_visible_child_name("connect")
        self._ensure_connect_view()

        self._detail_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._detail_content.set_hexpand(True)
        self._detail_content.set_vexpand(True)
        self._detail_content.append(self._view_stack)
        self._toolbar_view.set_content(self._detail_content)

        self._switcher_bar = Adw.ViewSwitcherBar()
        self._switcher_bar.set_stack(self._view_stack)
        self._switcher_bar.set_reveal(False)
        self._view_switcher_title.connect("notify::title-visible", self._on_switcher_title_visible_changed)
        self._toolbar_view.add_bottom_bar(self._switcher_bar)
        GLib.idle_add(self._sync_switcher_bar_reveal)

        self._mods_operation_handler_id = self._server_manager.connect(
            "mods-operation-changed", self._on_mods_operation_changed
        )
        self._server_manager.connect("server-removed", self._on_server_removed)

    def _add_lazy_tab(self, name: str, title: str, icon: str):
        host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        host.set_hexpand(True)
        host.set_vexpand(True)
        self._tab_hosts[name] = host
        self._view_stack.add_titled_with_icon(host, name, title, icon)

    def _ensure_console_view(self, server_id: str) -> ConsoleView:
        if server_id not in self._console_views:
            cv = ConsoleView()
            proc = self._server_manager.get_process(server_id)
            cv.set_process(proc)
            self._console_stack.add_named(cv, server_id)
            self._console_views[server_id] = cv
        return self._console_views[server_id]

    def _ensure_connect_view(self) -> ConnectView:
        if self._connect_view is None:
            self._connect_view = ConnectView()
            self._tab_hosts["connect"].append(self._connect_view)
            if self._current_server:
                self._connect_view.set_server(self._current_server, self._server_manager)
        return self._connect_view

    def _ensure_perf_view(self) -> PerformanceView:
        if self._perf_view is None:
            self._perf_view = PerformanceView()
            self._tab_hosts["performance"].append(self._perf_view)
            if self._current_server:
                self._perf_view.set_stats(self._server_manager.get_stats(self._current_server.id))
                self._sync_perf_with_io_process()
        return self._perf_view

    def _ensure_props_view(self) -> PropertiesView:
        if self._props_view is None:
            self._props_view = PropertiesView(toast_overlay=self._toast_overlay)
            self._tab_hosts["properties"].append(self._props_view)
            if self._current_server:
                config = self._server_manager.get_config(self._current_server.id)
                self._props_view.set_config(config, self._server_manager, self._current_server)
        return self._props_view

    def _ensure_files_view(self) -> FilesView:
        if self._files_view is None:
            self._files_view = FilesView()
            self._files_view._push_fullscreen_page_cb = self._push_fullscreen_page
            self._tab_hosts["files"].append(self._files_view)
            if self._current_server:
                self._files_view.set_server(self._current_server, self._server_manager)
                self._files_view_server_id = self._current_server.id
        return self._files_view

    def _push_fullscreen_page(self, page):
        self._outer_nav.push(page)

    def _on_switcher_title_visible_changed(self, *_args):
        self._sync_switcher_bar_reveal()

    def _sync_switcher_bar_reveal(self):
        self._switcher_bar.set_reveal(self._view_switcher_title.get_title_visible())
        return False

    def load_server(self, server_info: ServerInfo):
        try:
            self._outer_nav.pop_to_tag("niksnaks-hosting-detail-root")
        except Exception:
            pass

        self._current_server = server_info

        if not server_info:
            return

        self._view_switcher_title.set_title(f"{server_info.name} · {server_info.mc_version}")
        self._view_switcher_title.set_subtitle("")

        selected = self._server_manager.get_process(server_info.id)
        self._set_selected_process(selected)

        self._ensure_console_view(server_info.id)
        self._console_stack.set_visible_child_name(server_info.id)

        self._connect_general_status_handlers()

        if self._perf_view:
            self._perf_view.set_stats(self._server_manager.get_stats(server_info.id))
            self._sync_perf_with_io_process()

        self._ensure_connect_view().set_server(server_info, self._server_manager)

        if self._props_view:
            config = self._server_manager.get_config(server_info.id)
            self._props_view.set_config(config, self._server_manager, server_info)
        if self._files_view:
            if self._view_stack.get_visible_child_name() == "files":
                self._files_view.set_server(server_info, self._server_manager)
                self._files_view_server_id = server_info.id
            else:
                # Left for _on_tab_changed to do when somebody actually opens the tab.
                self._files_view_server_id = ""

        self._update_toggle_for_selected(selected.status if selected else ServerStatus.STOPPED)

    def _set_selected_process(self, process: ServerProcess):
        if self._selected_process and self._selected_status_handler_id:
            try:
                self._selected_process.disconnect(self._selected_status_handler_id)
            except Exception:
                pass
        self._selected_status_handler_id = None
        self._selected_process = process
        if process:
            self._selected_status_handler_id = process.connect("status-changed", self._on_selected_status_changed)

    def _sync_perf_with_io_process(self):
        if not self._perf_view:
            return
        p = self._selected_process
        if p and p.is_running:
            self._perf_view.start_monitoring()
        else:
            self._perf_view.stop_monitoring()
            self._perf_view.reset()

    def _connect_general_status_handlers(self):
        for sid in self._server_manager.get_running_server_ids():
            self._ensure_console_view(sid)
            proc = self._server_manager.get_process(sid)
            pid = id(proc)
            if pid not in self._general_status_connected:
                proc.connect("status-changed", self._on_process_status_changed)
                self._general_status_connected.add(pid)

    def _on_process_status_changed(self, process, status):
        if status == ServerStatus.RUNNING or status == ServerStatus.STARTING:
            for sid, p in self._server_manager._processes.items():
                if p is process:
                    self._ensure_console_view(sid)
                    break

    def _on_selected_status_changed(self, process, status):
        self._update_toggle_for_selected(status)
        self._sync_perf_with_io_process()

    def _update_toggle_for_selected(self, status: str):
        self._toggle_btn.remove_css_class("niksnaks-hosting-starting-button")
        selected_id = self._current_server.id if self._current_server else ""
        mods_busy = bool(selected_id) and self._server_manager.is_mod_operation_active(selected_id)

        if status == ServerStatus.STARTING:
            self._toggle_btn.set_label(_("Starting"))
            self._toggle_btn.remove_css_class("suggested-action")
            self._toggle_btn.remove_css_class("destructive-action")
            self._toggle_btn.add_css_class("niksnaks-hosting-starting-button")
            self._toggle_btn.set_sensitive(False)
            self._toggle_btn.set_tooltip_text(_("Wait for the server to finish starting"))
            return

        if status == ServerStatus.RUNNING:
            self._toggle_btn.set_label(_("Stop"))
            self._toggle_btn.remove_css_class("suggested-action")
            self._toggle_btn.add_css_class("destructive-action")
            self._toggle_btn.set_sensitive(True)
            self._toggle_btn.set_tooltip_text(None)
        else:
            self._toggle_btn.set_label(_("Start"))
            self._toggle_btn.remove_css_class("destructive-action")
            self._toggle_btn.add_css_class("suggested-action")
            self._toggle_btn.set_sensitive(not mods_busy)
            if mods_busy:
                self._toggle_btn.set_tooltip_text(_("Mods are currently installing/updating"))
            else:
                self._toggle_btn.set_tooltip_text(None)

    def _on_server_removed(self, _manager, server_id: str):
        cv = self._console_views.pop(server_id, None)
        if cv is not None:
            try:
                self._console_stack.remove(cv)
            except Exception:
                pass

    def _on_mods_operation_changed(self, _manager, server_id: str, _active: bool, _count: int):
        if not self._current_server:
            return
        if self._current_server.id != server_id:
            return
        status = self._selected_process.status if self._selected_process else ServerStatus.STOPPED
        self._update_toggle_for_selected(status)

    def _on_tab_changed(self, stack, _pspec):
        tab_name = stack.get_visible_child_name()
        if tab_name == "console":
            if self._current_server:
                self._ensure_console_view(self._current_server.id)
                self._console_stack.set_visible_child_name(self._current_server.id)
        elif tab_name == "connect":
            self._ensure_connect_view()
        elif tab_name == "performance":
            perf = self._ensure_perf_view()
            perf.scroll_to_top()
        elif tab_name == "properties":
            props = self._ensure_props_view()
            props.reload_from_disk()
            GLib.idle_add(props.focus_save_button)
        elif tab_name == "files":
            files = self._ensure_files_view()
            if self._current_server and self._files_view_server_id != self._current_server.id:
                files.set_server(self._current_server, self._server_manager)
                self._files_view_server_id = self._current_server.id
            files.refresh_worlds_if_changed(force=True)

    def poll_runtime_state(self) -> None:
        if self._view_stack.get_visible_child_name() == "files":
            self._ensure_files_view().refresh_worlds_if_changed()

    def _show_start_refusal(self, refusal: StartRefusal):
        if refusal.kind == "port":
            title = _("{} Port In Use").format(refusal.port_type)
        elif refusal.kind == "mods":
            title = _("Cannot Start Server")
        else:
            return
        dialog = Adw.AlertDialog.new(title, refusal.message)
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self.get_root())

    def _on_toggle_clicked(self, button):
        if not self._selected_process or not self._current_server:
            return

        if self._selected_process.status == ServerStatus.STARTING:
            return

        if self._selected_process.is_running:
            self._selected_process.stop()
            return

        refusal = self._server_manager.start_server(self._current_server.id)
        if refusal:
            self._show_start_refusal(refusal)

    @property
    def header(self) -> Adw.HeaderBar:
        return self._header
