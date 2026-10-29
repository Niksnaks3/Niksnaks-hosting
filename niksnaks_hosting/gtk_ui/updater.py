from __future__ import annotations

import os
import threading
import uuid

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib

from niksnaks_hosting.gtk_ui.dialogs.progress_dialog import ProgressDialog
from niksnaks_hosting.shared.backend import update_manager

FLATPAK_PORTAL = "org.freedesktop.portal.Flatpak"
FLATPAK_PORTAL_PATH = "/org/freedesktop/portal/Flatpak"
FLATPAK_INTERFACE = "org.freedesktop.portal.Flatpak"
UPDATE_MONITOR_INTERFACE = "org.freedesktop.portal.Flatpak.UpdateMonitor"
SPAWN_LATEST_VERSION = 2
PROGRESS_EMPTY = 1
PROGRESS_DONE = 2
PROGRESS_FAILED = 3
WINDOWS_FIRST_CHECK_SECONDS = 10
WINDOWS_CHECK_INTERVAL_SECONDS = 6 * 60 * 60


class AppUpdater:
    def __init__(self, application, get_window):
        self._app = application
        self._get_window = get_window
        self._toast = None
        self._offered = ""
        self._busy = False
        self._bus = None
        self._monitor_token = f"niksnaks_hosting_{uuid.uuid4().hex}"
        self._monitor_path = ""

    def start(self) -> None:
        if update_manager.is_windows_install():
            GLib.timeout_add_seconds(WINDOWS_FIRST_CHECK_SECONDS, self._check_windows_once)
            GLib.timeout_add_seconds(WINDOWS_CHECK_INTERVAL_SECONDS, self._check_windows_repeating)
        elif update_manager.is_flatpak():
            self._start_flatpak_monitor()

    def _show(self, message: str, button_label: str | None = None, on_button=None) -> None:
        window = self._get_window()
        if not window:
            return
        if self._toast:
            self._toast.dismiss()
        self._toast = window.show_toast(message, button_label=button_label, on_button=on_button, timeout=0)

    def _hide(self) -> None:
        if self._toast:
            self._toast.dismiss()
            self._toast = None

    def _failed(self, message: str) -> None:
        self._busy = False
        self._offered = ""
        self._hide()
        window = self._get_window()
        if window:
            window.show_toast(_("Could not update: {}").format(message or _("unknown error")), timeout=8)

    def _check_windows_once(self) -> bool:
        self._check_windows()
        return False

    def _check_windows_repeating(self) -> bool:
        self._check_windows()
        return True

    def _check_windows(self) -> None:
        if self._busy:
            return

        def worker() -> None:
            try:
                release = update_manager.check_for_windows_update()
            except Exception:
                release = None
            if release:
                GLib.idle_add(self._offer_windows_update, release)

        threading.Thread(target=worker, daemon=True).start()

    def _offer_windows_update(self, release) -> bool:
        if self._busy or self._offered == release.version:
            return False
        self._offered = release.version
        self._show(
            _("Version {} is available").format(release.version),
            _("Update"),
            lambda: self._confirm_windows_update(release),
        )
        return False

    def _confirm_windows_update(self, release) -> None:
        window = self._get_window()
        if not window or self._busy:
            return

        dialog = Adw.AlertDialog()
        dialog.set_heading(_("Install Update?"))
        dialog.set_body(
            _(
                "Niksnaks-Hosting {} will be downloaded and installed. The app closes while it "
                "installs and opens again when it is done. Servers on this computer are stopped "
                "and saved first."
            ).format(release.version)
        )
        dialog.add_response("later", _("Later"))
        dialog.add_response("install", _("Install"))
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("install")
        dialog.set_close_response("later")

        def on_response(_dialog, response):
            if response == "install":
                self._download_windows_update(release)
            else:
                self._offered = ""

        dialog.connect("response", on_response)
        dialog.present(window)

    def _download_windows_update(self, release) -> None:
        window = self._get_window()
        if not window:
            return
        self._busy = True
        self._hide()

        progress = ProgressDialog(_("Downloading update"), detail=release.installer_name)
        progress.present(window)

        def report(done: int, total: int) -> None:
            fraction = done / total if total else None
            progress.update(
                fraction,
                detail=_("{} of {} MB").format(round(done / 1048576), round(total / 1048576)),
            )

        def worker() -> None:
            try:
                installer = update_manager.download_installer(release, report)
                error = ""
            except Exception as exc:
                installer, error = None, str(exc)

            def finish() -> bool:
                progress.finish()
                if installer is None:
                    self._failed(error)
                    return False
                self._app.run_after_shutdown(lambda: update_manager.launch_installer_after_exit(installer))
                self._app.activate_action("quit", None)
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    def _start_flatpak_monitor(self) -> None:
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return

        for signal_name, handler in (
            ("UpdateAvailable", self._on_update_available),
            ("Progress", self._on_update_progress),
        ):
            self._bus.signal_subscribe(
                FLATPAK_PORTAL,
                UPDATE_MONITOR_INTERFACE,
                signal_name,
                None,
                None,
                Gio.DBusSignalFlags.NONE,
                handler,
            )

        options = {"handle_token": GLib.Variant("s", self._monitor_token)}
        self._bus.call(
            FLATPAK_PORTAL,
            FLATPAK_PORTAL_PATH,
            FLATPAK_INTERFACE,
            "CreateUpdateMonitor",
            GLib.Variant("(a{sv})", (options,)),
            GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_monitor_created,
        )

    def _on_monitor_created(self, bus, result) -> None:
        try:
            self._monitor_path = bus.call_finish(result).unpack()[0]
        except GLib.Error:
            self._monitor_path = ""

    def _is_ours(self, object_path: str) -> bool:
        if self._monitor_path:
            return object_path == self._monitor_path
        return object_path.endswith("/" + self._monitor_token)

    def _on_update_available(self, _connection, _sender, object_path, _interface, _signal, parameters, *_args):
        if not self._is_ours(object_path) or self._busy:
            return
        info = parameters.unpack()[0]
        running = info.get("running-commit", "")
        local = info.get("local-commit", "")
        remote = info.get("remote-commit", "")

        if remote and remote != local:
            if self._offered != remote:
                self._offered = remote
                self._show(_("A new version is available"), _("Update"), self._install_flatpak_update)
        elif local and local != running:
            if self._offered != local:
                self._offered = local
                self._show(_("Update installed"), _("Restart"), self._restart)

    def _install_flatpak_update(self) -> None:
        if self._busy or not self._monitor_path:
            return
        self._busy = True
        self._show(_("Downloading update…"))
        self._bus.call(
            FLATPAK_PORTAL,
            self._monitor_path,
            UPDATE_MONITOR_INTERFACE,
            "Update",
            GLib.Variant("(sa{sv})", ("", {})),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_update_called,
        )

    def _on_update_called(self, bus, result) -> None:
        try:
            bus.call_finish(result)
        except GLib.Error as exc:
            self._failed(exc.message)

    def _on_update_progress(self, _connection, _sender, object_path, _interface, _signal, parameters, *_args):
        if not self._is_ours(object_path) or not self._busy:
            return
        info = parameters.unpack()[0]
        status = info.get("status", 0)

        if status == PROGRESS_DONE:
            self._busy = False
            self._show(_("Update installed"), _("Restart"), self._restart)
        elif status == PROGRESS_EMPTY:
            self._busy = False
            self._hide()
        elif status == PROGRESS_FAILED:
            self._failed(info.get("error_message") or info.get("error") or "")

    def _restart(self) -> None:
        self._app.run_after_shutdown(self._spawn_latest_version)
        self._app.activate_action("quit", None)

    def _spawn_latest_version(self) -> None:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        cwd = os.path.expanduser("~").encode() + b"\0"
        argv = [b"sh\0", b"-c\0", b"sleep 2; exec niksnaks-hosting\0"]
        bus.call_sync(
            FLATPAK_PORTAL,
            FLATPAK_PORTAL_PATH,
            FLATPAK_INTERFACE,
            "Spawn",
            GLib.Variant("(ayaaya{uh}a{ss}ua{sv})", (cwd, argv, {}, {}, SPAWN_LATEST_VERSION, {})),
            GLib.VariantType("(u)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
