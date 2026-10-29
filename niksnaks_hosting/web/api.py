"""The HTTP side of the web panel: a handful of JSON routes, one event stream, static files.

Every route under /api/servers/<id>/ first checks that the logged-in user was given
that server. A server the user may not see answers 404, the same as one that does
not exist, so the ids of other people's servers cannot be probed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import mimetypes
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit

from niksnaks_hosting.shared.backend import mod_installer, modrinth_client
from niksnaks_hosting.shared.backend.config_manager import ConfigManager
from niksnaks_hosting.shared.backend.server_manager import DAILY_BACKUP_KINDS, ServerManager
from niksnaks_hosting.shared.backend.stats import host_sample
from niksnaks_hosting.shared.utils.constants import (
    APP_VERSION,
    MAX_RAM_MB,
    MIN_RAM_MB,
    ServerStatus,
    is_bedrock,
    normalize_loader,
    uses_installer,
)

if TYPE_CHECKING:
    from niksnaks_hosting.web.panel import WebPanel

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "nh_session"
MAX_BODY = 64 * 1024
MAX_ICON_BYTES = 4 * 1024 * 1024  # a server picture is 128x128; anything this big is a mistake
JOB_TTL_S = 600

# The line players see in the server list: Java Edition calls it the MOTD, Bedrock
# keeps the same thing under server-name, and the GTK app edits whichever one it is.
MOTD_KEY_JAVA = "motd"
MOTD_KEY_BEDROCK = "server-name"


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Job:
    """One background mod install or version update, polled by the browser until it is done."""

    def __init__(self) -> None:
        self.id = uuid.uuid4().hex
        self.state = "running"
        self.fraction = 0.0
        self.message = ""
        self.finished_at = 0.0

    def to_dict(self) -> dict:
        return {"id": self.id, "state": self.state, "fraction": self.fraction, "message": self.message}


Route = tuple[str, re.Pattern, Callable]


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "NiksnaksHosting"
    protocol_version = "HTTP/1.1"

    @property
    def panel(self) -> WebPanel:
        return self.server.panel  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler's name
        return  # the terminal is not a web server log

    # -- plumbing ------------------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        path = urlsplit(self.path).path
        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    raise HttpError(405, "Method not allowed")
                self._serve_static(path)
                return
            for route_method, pattern, handler in ROUTES:
                match = pattern.fullmatch(path)
                if match and route_method == method:
                    handler(self, *[unquote(g) for g in match.groups()])
                    return
            raise HttpError(404, "Not found")
        except HttpError as exc:
            self._json({"error": exc.message}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # a bug must not take the whole panel down
            log.exception("web panel: %s %s failed", method, path)
            self._json({"error": "Something went wrong inside the app; see its log"}, 500)

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HttpError(413, "Request too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise HttpError(400, "Malformed JSON") from None
        return data if isinstance(data, dict) else {}

    def _json(self, payload: Any, status: int = 200, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The page compares this with the version it was served as, and reloads itself
        # once the app underneath it has been updated rather than calling routes that
        # have changed shape with an old copy of app.js.
        self.send_header("X-App-Version", APP_VERSION)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()
        if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
            raise HttpError(404, "Not found")
        data = target.read_bytes()
        if target.name == "index.html":
            # A browser or a proxy in front of the panel may hold on to the old script
            # and stylesheet after an update; a new URL per version cannot be stale.
            data = data.replace(b"__APP_VERSION__", APP_VERSION.encode())
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # -- who is asking -------------------------------------------------------------------

    def _session_token(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _require_user(self):
        user = self.panel.sessions.user_for(self._session_token())
        if not user:
            raise HttpError(401, "Not logged in")
        return user

    def _require_mutation(self) -> None:
        # With a SameSite cookie, a request another site tricked the browser into sending
        # cannot carry a custom header; the fetch() calls in app.js always do.
        if self.headers.get("X-Requested-With") != "fetch":
            raise HttpError(403, "Missing X-Requested-With header")

    def _require_admin(self, user) -> None:
        if not user.admin:
            raise HttpError(403, "Only an admin can do that")

    def _server_for(self, user, server_id: str):
        info = self.panel.manager.get_server(server_id)
        if not info or not user.may_access(server_id):
            raise HttpError(404, "No such server")
        return info

    def _client_ip(self) -> str:
        return str(self.client_address[0])

    # -- routes ----------------------------------------------------------------------------

    def r_login(self) -> None:
        self._require_mutation()
        body = self._body()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        token = self.panel.sessions.login(username, password, self._client_ip())
        if not token:
            raise HttpError(401, "Wrong username or password")
        user = self.panel.users.get(username)
        self._json(
            user.to_public(),
            extra_headers={
                "Set-Cookie": f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={30 * 24 * 3600}"
            },
        )

    def r_logout(self) -> None:
        self._require_mutation()
        self.panel.sessions.logout(self._session_token())
        self._json({"ok": True}, extra_headers={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0"})

    def r_me(self) -> None:
        self._json(self._require_user().to_public())

    def _server_dict(self, info) -> dict:
        process = self.panel.manager.get_existing_process(info.id)
        return {
            "id": info.id,
            "name": info.name,
            "mc_version": info.mc_version,
            "edition": info.edition,
            "loader_type": info.loader_type,
            "loader_version": info.loader_version,
            "ram_mb": info.ram_mb,
            "port": self._server_port(info),
            "is_remote": info.is_remote,
            "has_mods": not is_bedrock(info.edition),
            "status": process.status if process else ServerStatus.STOPPED,
            "players": process.player_count if process else 0,
            "max_players": process.max_players if process else 0,
        }

    def _server_port(self, info) -> int | None:
        """The game port from server.properties, or None while it cannot be read."""
        ports = self.panel.manager.server_bound_ports(info)
        return min(ports) if ports else None

    def r_servers(self) -> None:
        user = self._require_user()
        self._json([self._server_dict(i) for i in self.panel.manager.servers if user.may_access(i.id)])

    def r_server(self, server_id: str) -> None:
        info = self._server_for(self._require_user(), server_id)
        self._json(self._server_dict(info))

    def r_power(self, server_id: str, action: str) -> None:
        self._require_mutation()
        info = self._server_for(self._require_user(), server_id)
        manager = self.panel.manager
        if action == "start":
            refusal = manager.start_server(info.id)
            if refusal:
                raise HttpError(409, refusal.message)
        elif action == "stop":
            process = manager.get_process(info.id)
            if process and process.is_running:
                process.stop()
        elif action == "restart":
            manager.restart_server(info.id)
        self._json({"ok": True})

    def r_console(self, server_id: str) -> None:
        info = self._server_for(self._require_user(), server_id)
        process = self.panel.manager.get_process(info.id)
        lines = list(process.log_history) if process else []
        try:
            count = int(self._query().get("lines", "200"))
        except ValueError:
            count = 200
        self._json({"lines": lines[-max(1, count) :]})

    def r_command(self, server_id: str) -> None:
        self._require_mutation()
        info = self._server_for(self._require_user(), server_id)
        command = str(self._body().get("command", "")).strip()
        if not command:
            raise HttpError(400, "Empty command")
        process = self.panel.manager.get_process(info.id)
        if not process or not process.is_running:
            raise HttpError(409, "The server is not running")
        process.send_command(command)
        self._json({"ok": True, "echoed": process.command_echoes_back})

    def r_events(self, server_id: str) -> None:
        token = self._session_token()
        info = self._server_for(self._require_user(), server_id)
        # The first frame is produced only after the hub has subscribed this connection,
        # so by the time the browser sees the response headers (and asks for the console
        # history) nothing printed from here on can slip between the two.
        frames = self.panel.hub.stream(info.id, self.panel.stopping)
        first = next(frames)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(first)
            self.wfile.flush()
            for frame in frames:
                # A login the admin has since removed, or a server taken away from
                # this user, stops the stream at the next frame or keepalive.
                user = self.panel.sessions.user_for(token)
                if not user or not user.may_access(info.id):
                    break
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            frames.close()
            self.close_connection = True

    def r_stats(self, server_id: str) -> None:
        info = self._server_for(self._require_user(), server_id)
        stats = self.panel.manager.get_stats(info.id)
        self._json(stats.sample().to_dict() if stats else {})

    def r_all_stats(self) -> None:
        """Every granted server's sample plus the host's own load, for the dashboard."""
        user = self._require_user()
        manager = self.panel.manager
        servers: dict[str, dict] = {}
        for info in manager.servers:
            if not user.may_access(info.id):
                continue
            stats = manager.get_stats(info.id)
            if stats:
                servers[info.id] = stats.sample().to_dict()
        self._json({"host": host_sample(), "servers": servers})

    def _java_server(self, user, server_id: str):
        """A server the user may touch, refused when it is a Bedrock one: mods are Java only."""
        info = self._server_for(user, server_id)
        if is_bedrock(info.edition):
            raise HttpError(409, "Bedrock servers have no mods")
        return info

    def r_mods(self, server_id: str) -> None:
        info = self._java_server(self._require_user(), server_id)
        self._json([m.to_dict() for m in mod_installer.list_installed(info.server_dir)])

    def r_mod_delete(self, server_id: str, filename: str) -> None:
        self._require_mutation()
        info = self._java_server(self._require_user(), server_id)
        process = self.panel.manager.get_existing_process(info.id)
        if process and process.is_running:
            raise HttpError(409, "Stop the server before removing mods")
        if not filename.lower().endswith(".jar"):
            raise HttpError(400, "Not a mod file")
        ok, message = mod_installer.remove_mod(info.server_dir, filename)
        if not ok:
            raise HttpError(404, message)
        self._json({"ok": True, "removed": message})

    def r_search(self, server_id: str) -> None:
        info = self._java_server(self._require_user(), server_id)
        q = self._query()
        try:
            offset = max(0, int(q.get("offset", "0")))
            limit = max(1, min(50, int(q.get("limit", "20"))))
        except ValueError:
            offset, limit = 0, 20
        hits, total = modrinth_client.search_mods(
            q.get("q", ""),
            limit=limit,
            offset=offset,
            game_version=info.mc_version,
            loader=normalize_loader(info.loader_type),
        )
        self._json(
            {
                "total": total,
                "hits": [
                    {
                        "project_id": h.project_id,
                        "slug": h.slug,
                        "title": h.title,
                        "description": h.description,
                        "icon_url": h.icon_url,
                        "downloads": h.downloads,
                        "author": h.author,
                    }
                    for h in hits
                ],
            }
        )

    def r_versions(self, server_id: str, project_id: str) -> None:
        info = self._java_server(self._require_user(), server_id)
        versions = modrinth_client.find_compatible_versions(
            project_id, info.mc_version, normalize_loader(info.loader_type), limit=12
        )
        self._json(
            [
                {
                    "version_id": v.version_id,
                    "name": v.name,
                    "version_number": v.version_number,
                    "filename": v.filename,
                    "published": v.published,
                }
                for v in versions
            ]
        )

    def r_install(self, server_id: str) -> None:
        self._require_mutation()
        info = self._java_server(self._require_user(), server_id)
        body = self._body()
        project_id = str(body.get("project_id", "")).strip()
        version_id = str(body.get("version_id", "")).strip()
        if not project_id or not version_id:
            raise HttpError(400, "project_id and version_id are required")
        job = self.panel.new_job()
        manager = self.panel.manager
        manager.begin_mod_operation(info.id)

        def worker() -> None:
            try:
                raw = modrinth_client.get_version(version_id)
                version = modrinth_client._version_to_model(raw) if raw else None
                if not version or version.project_id != project_id:
                    raise RuntimeError("That version does not exist on Modrinth")
                project = modrinth_client.get_project(project_id) or {}
                title = str(project.get("title") or version.title or project_id)
                loader = normalize_loader(info.loader_type)
                missing, required = mod_installer.plan_dependencies(info.server_dir, version, info.mc_version, loader)
                if not manager.preferences.auto_resolve_mod_dependencies:
                    missing = []

                def progress(fraction: float, detail: str) -> None:
                    job.fraction, job.message = fraction, detail

                mod_installer.download_mod(info.server_dir, project_id, title, version, missing, required, progress)
                mod_installer.configure_known_mod(
                    manager, info, slug=str(project.get("slug", "")), title=title, project_id=project_id
                )
                job.state, job.fraction, job.message = "done", 1.0, version.filename
            except Exception as exc:
                job.state, job.message = "failed", str(exc)
            finally:
                job.finished_at = time.time()
                manager.end_mod_operation(info.id)

        threading.Thread(target=worker, daemon=True, name=f"web-install-{job.id[:8]}").start()
        self._json({"job_id": job.id}, 202)

    # -- settings: the picture, the MOTD and the version updater ---------------------------

    @staticmethod
    def _motd_key(info) -> str:
        return MOTD_KEY_BEDROCK if is_bedrock(info.edition) else MOTD_KEY_JAVA

    def r_settings(self, server_id: str) -> None:
        user = self._require_user()
        info = self._server_for(user, server_id)
        bedrock = is_bedrock(info.edition)
        config = ConfigManager(info.server_dir)
        readable = config.try_load()
        admin = {}
        if user.admin:
            admin = {
                "ram_mb": info.ram_mb,
                "min_ram_mb": MIN_RAM_MB,
                "max_ram_mb": MAX_RAM_MB,
                "daily_restart": info.daily_restart,
                "daily_backup": info.daily_backup,
                "daily_time": info.daily_time,
            }
        self._json(
            {
                "admin": user.admin,
                **admin,
                "edition": info.edition,
                "motd": config.get(self._motd_key(info), "") if readable else "",
                "properties_readable": readable,
                "has_icon": bool(self.panel.manager.icon_bytes(info)),
                "mc_version": info.mc_version,
                "loader_type": info.loader_type,
                "loader_version": info.loader_version,
                # Updating a Java server downloads and installs on the machine the app
                # runs on, which for a server on another computer is the wrong disk; the
                # GTK app refuses the same way. A Bedrock host fetches its own package,
                # so that one can be updated wherever it lives.
                "can_update": bedrock or not info.is_remote,
                # Backups are only taken of folders on this computer.
                "backs_up": not info.is_remote,
            }
        )

    def r_set_motd(self, server_id: str) -> None:
        self._require_mutation()
        info = self._server_for(self._require_user(), server_id)
        motd = str(self._body().get("motd", ""))
        if len(motd) > 200:
            raise HttpError(400, "That message of the day is too long")
        if "\n" in motd or "\r" in motd:
            raise HttpError(400, "A message of the day is a single line")
        config = ConfigManager(info.server_dir)
        if not config.try_load():
            raise HttpError(409, "Could not read this server's server.properties")
        config.set_value(self._motd_key(info), motd)
        try:
            config.save()
        except Exception as exc:
            raise HttpError(409, f"Could not save server.properties: {exc}") from None
        self._json({"ok": True, "motd": motd})

    def r_set_ram(self, server_id: str) -> None:
        self._require_mutation()
        user = self._require_user()
        info = self._server_for(user, server_id)
        self._require_admin(user)
        try:
            ram_mb = int(self._body().get("ram_mb"))
        except (TypeError, ValueError):
            raise HttpError(400, "ram_mb has to be a number") from None
        if not MIN_RAM_MB <= ram_mb <= MAX_RAM_MB:
            raise HttpError(400, f"Memory has to be between {MIN_RAM_MB} and {MAX_RAM_MB} MB")
        self.panel.manager.update_server_ram(info.id, ram_mb)
        self._json({"ok": True, "ram_mb": ram_mb})

    def r_set_schedule(self, server_id: str) -> None:
        self._require_mutation()
        user = self._require_user()
        info = self._server_for(user, server_id)
        self._require_admin(user)
        body = self._body()
        backup = str(body.get("daily_backup", "off"))
        if info.is_remote and backup != "off":
            raise HttpError(409, "Backups are only made of servers on this computer")
        ok, message = self.panel.manager.set_server_schedule(
            info.id, bool(body.get("daily_restart")), backup, str(body.get("daily_time", ""))
        )
        if not ok:
            raise HttpError(400, message)
        self._json(
            {
                "ok": True,
                "daily_restart": info.daily_restart,
                "daily_backup": info.daily_backup,
                "daily_time": info.daily_time,
            }
        )

    def r_backup(self, server_id: str) -> None:
        self._require_mutation()
        user = self._require_user()
        info = self._server_for(user, server_id)
        self._require_admin(user)
        kind = str(self._body().get("kind", ""))
        if kind not in DAILY_BACKUP_KINDS or kind == "off":
            raise HttpError(400, "kind has to be world or full")
        if info.is_remote:
            raise HttpError(409, "Backups are only made of servers on this computer")
        manager = self.panel.manager
        process = manager.get_existing_process(info.id)
        if process and process.is_running:
            raise HttpError(409, "Stop the server before backing it up")
        if manager.is_mod_operation_active(info.id):
            raise HttpError(409, "Something else is running on this server; try again when it is done")
        job = self.panel.new_job()
        manager.begin_mod_operation(info.id)

        def progress(fraction: float, detail: str) -> None:
            job.fraction, job.message = float(fraction), str(detail)

        def worker() -> None:
            try:
                ok, message = manager.create_backup(info.id, kind, progress_callback=progress)
                job.state = "done" if ok else "failed"
                job.message = message
                if ok:
                    job.fraction = 1.0
            except Exception as exc:
                job.state, job.message = "failed", str(exc)
            finally:
                job.finished_at = time.time()
                manager.end_mod_operation(info.id)

        threading.Thread(target=worker, daemon=True, name=f"web-backup-{job.id[:8]}").start()
        self._json({"job_id": job.id}, 202)

    def r_icon(self, server_id: str) -> None:
        info = self._server_for(self._require_user(), server_id)
        data = self.panel.manager.icon_bytes(info)
        if not data:
            raise HttpError(404, "This server has no picture")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def r_set_icon(self, server_id: str) -> None:
        self._require_mutation()
        info = self._server_for(self._require_user(), server_id)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_ICON_BYTES:
            raise HttpError(413, "That picture is too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            raise HttpError(400, "No picture was sent")
        # set_server_icon reads a file, because that is what the GTK file chooser hands
        # it; the browser hands over bytes instead, so they go through a temporary file.
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            ok, message = self.panel.manager.set_server_icon(info.id, tmp_path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        if not ok:
            raise HttpError(400, message or "That file is not a picture this app can use")
        self._json({"ok": True})

    def r_update_versions(self, server_id: str) -> None:
        """The Minecraft versions this server could move up to, newest first."""
        info = self._server_for(self._require_user(), server_id)
        manager = self.panel.manager
        if is_bedrock(info.edition):
            # Mojang offers one release and one preview build; the preview is only on
            # offer when it is ahead of what the server has, as in the GTK dialog.
            options = [o for o in manager.bedrock_versions(info) if ServerManager.is_version_after(o.version, info.mc_version)]
            self._json(
                {
                    "current": info.mc_version,
                    "loader_type": None,
                    "mc_versions": [o.version for o in options],
                    "labels": {o.version: o.label for o in options},
                }
            )
            return
        versions = manager.download_manager.fetch_game_versions_for_loader(info.loader_type)
        newer = [v for v in versions if ServerManager.is_version_after(v, info.mc_version)]
        self._json({"current": info.mc_version, "loader_type": info.loader_type, "mc_versions": newer})

    def r_loader_versions(self, server_id: str) -> None:
        info = self._server_for(self._require_user(), server_id)
        if is_bedrock(info.edition):
            raise HttpError(409, "A Bedrock server has no loader")
        mc_version = self._query().get("mc_version", "").strip()
        if not mc_version:
            raise HttpError(400, "mc_version is required")
        manager = self.panel.manager
        options = manager.download_manager.fetch_compatible_loader_versions(info.loader_type, mc_version)
        # Forge and NeoForge builds are rebuilt per Minecraft version, so "newer than what
        # is installed" means nothing for them and every build stays on offer.
        if not uses_installer(info.loader_type) and info.loader_version:
            newer = [o for o in options if ServerManager.is_version_at_least(o.version, info.loader_version)]
            options = newer or options
        self._json([{"version": o.version, "label": o.label} for o in options])

    def r_update(self, server_id: str) -> None:
        self._require_mutation()
        info = self._server_for(self._require_user(), server_id)
        bedrock = is_bedrock(info.edition)
        body = self._body()
        mc_version = str(body.get("mc_version", "")).strip()
        loader_version = str(body.get("loader_version", "")).strip()
        if not mc_version:
            raise HttpError(400, "mc_version is required")
        if info.is_remote and not bedrock:
            # The install would download and unpack on this computer, into what is the
            # host's folder; the GTK app refuses for the same reason.
            raise HttpError(409, f"A server on {info.host.name} has to be updated from the app on that machine")
        process = self.panel.manager.get_existing_process(info.id)
        if process and process.is_running:
            raise HttpError(409, "Stop the server before updating it")
        manager = self.panel.manager
        bedrock_option = None
        if bedrock:
            bedrock_option = next((o for o in manager.bedrock_versions(info) if o.version == mc_version), None)
            if not bedrock_option:
                raise HttpError(409, f"Bedrock {mc_version} is not on offer from Mojang any more")
        job = self.panel.new_job()
        manager.begin_mod_operation(info.id)

        def progress(fraction: float, detail: str) -> None:
            job.fraction, job.message = float(fraction), str(detail)

        def worker() -> None:
            try:
                if bedrock_option:
                    ok, message = manager.update_bedrock_server(info.id, bedrock_option, progress_callback=progress)
                else:
                    ok, message = manager.update_server_runtime(
                        info.id,
                        mc_version,
                        loader_version or None,
                        progress_callback=progress,
                        loader_type=info.loader_type,
                    )
                job.state = "done" if ok else "failed"
                job.message = message
                if ok:
                    job.fraction = 1.0
            except Exception as exc:
                job.state, job.message = "failed", str(exc)
            finally:
                job.finished_at = time.time()
                manager.end_mod_operation(info.id)

        threading.Thread(target=worker, daemon=True, name=f"web-update-{job.id[:8]}").start()
        self._json({"job_id": job.id}, 202)

    def r_job(self, job_id: str) -> None:
        self._require_user()
        job = self.panel.job(job_id)
        if not job:
            raise HttpError(404, "No such job")
        self._json(job.to_dict())


def _r(method: str, pattern: str, handler: Callable) -> Route:
    return method, re.compile(pattern), handler


SID = r"([A-Za-z0-9_\-]+)"
ROUTES: list[Route] = [
    _r("POST", r"/api/login", PanelHandler.r_login),
    _r("POST", r"/api/logout", PanelHandler.r_logout),
    _r("GET", r"/api/me", PanelHandler.r_me),
    _r("GET", r"/api/servers", PanelHandler.r_servers),
    _r("GET", r"/api/stats", PanelHandler.r_all_stats),
    _r("GET", rf"/api/servers/{SID}", PanelHandler.r_server),
    _r("POST", rf"/api/servers/{SID}/(start|stop|restart)", PanelHandler.r_power),
    _r("GET", rf"/api/servers/{SID}/console", PanelHandler.r_console),
    _r("POST", rf"/api/servers/{SID}/console", PanelHandler.r_command),
    _r("GET", rf"/api/servers/{SID}/events", PanelHandler.r_events),
    _r("GET", rf"/api/servers/{SID}/stats", PanelHandler.r_stats),
    _r("GET", rf"/api/servers/{SID}/mods", PanelHandler.r_mods),
    _r("DELETE", rf"/api/servers/{SID}/mods/([^/]+)", PanelHandler.r_mod_delete),
    _r("GET", rf"/api/servers/{SID}/modrinth/search", PanelHandler.r_search),
    _r("GET", rf"/api/servers/{SID}/modrinth/([A-Za-z0-9]+)/versions", PanelHandler.r_versions),
    _r("POST", rf"/api/servers/{SID}/mods/install", PanelHandler.r_install),
    _r("GET", rf"/api/servers/{SID}/settings", PanelHandler.r_settings),
    _r("POST", rf"/api/servers/{SID}/settings/motd", PanelHandler.r_set_motd),
    _r("POST", rf"/api/servers/{SID}/settings/ram", PanelHandler.r_set_ram),
    _r("POST", rf"/api/servers/{SID}/settings/schedule", PanelHandler.r_set_schedule),
    _r("POST", rf"/api/servers/{SID}/backup", PanelHandler.r_backup),
    _r("GET", rf"/api/servers/{SID}/icon", PanelHandler.r_icon),
    _r("POST", rf"/api/servers/{SID}/icon", PanelHandler.r_set_icon),
    _r("GET", rf"/api/servers/{SID}/update/versions", PanelHandler.r_update_versions),
    _r("GET", rf"/api/servers/{SID}/update/loader-versions", PanelHandler.r_loader_versions),
    _r("POST", rf"/api/servers/{SID}/update", PanelHandler.r_update),
    _r("GET", r"/api/jobs/([a-f0-9]+)", PanelHandler.r_job),
]
