from __future__ import annotations

import socket

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gtk

from niksnaks_hosting.shared.utils.constants import is_bedrock

PLAYIT_DASHBOARD_URL = "https://playit.gg/account/tunnels"

from ..utils import *

class LocalIpMixin:
    def _make_local_network_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("Local Network"),
            description=_("Share your LAN address for local multiplayer"),
        )
        row = Adw.ActionRow(title=_("Local device IP"), subtitle=_("Detecting..."))
        row.set_activatable(True)
        row.connect("activated", self._on_copy_local_ip)
        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
        copy_btn.add_css_class("flat")
        copy_btn.set_tooltip_text(_("Copy local IP"))
        copy_btn.connect("clicked", self._on_copy_local_ip)
        row.add_suffix(copy_btn)
        group.add(row)
        self._local_ip_rows.append(row)
        return group

    def _get_local_ip(self) -> str:
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
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

        try:
            ip = socket.gethostbyname(socket.gethostname())
            if ip:
                return ip
        except Exception:
            pass

        return _("Not available")

    def _local_connect_port(self) -> int | None:
        """Port LAN players must type in, or None when the client's default is right.

        Bedrock clients always ask for a port, and the Bedrock default (19132) differs
        from the Java one, so spell it out rather than leaving players to guess.
        """
        server_info = getattr(self, "_server_info", None)
        manager = getattr(self, "_server_manager", None)
        if not server_info or not manager or not is_bedrock(server_info.edition):
            return None
        return manager.playit_manager._read_server_port(str(server_info.server_dir))

    def _refresh_local_ip_row(self):
        ip = self._get_local_ip()
        self._local_ip_value = ip
        port = self._local_connect_port()
        subtitle = _("{} · port {}").format(ip, port) if port else ip
        for row in self._local_ip_rows:
            row.set_subtitle(subtitle)

    def _on_copy_local_ip(self, *_args):
        ip = self._local_ip_value.strip()
        not_available = _("Not available")
        if not ip or ip == not_available:
            self._toast(_("Local IP not available"))
            return
        try:
            display = Gdk.Display.get_default()
            if display:
                clipboard = display.get_clipboard()
                clipboard.set(ip)
                self._toast(_("Local IP copied"))
                return
        except Exception:
            pass
        self._toast(_("Could not access clipboard"))
