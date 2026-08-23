from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from niksnaks_hosting.shared.backend.loader_launch import resolve_launch_args
from niksnaks_hosting.shared.backend.server_process import ServerProcess
from niksnaks_hosting.shared.remote.systemd_service import SystemdService
from niksnaks_hosting.shared.utils.constants import (
    EDITION_JAVA,
    LOADER_FABRIC,
    ServerStatus,
    get_edition_display_name,
    get_loader_display_name,
)

if TYPE_CHECKING:
    from niksnaks_hosting.shared.remote.host import SshHost

# How often the unit is asked whether it is still up. Console output arrives on its own
# stream, so this only has to notice a server that died without saying anything.
STATE_POLL_SECONDS = 5.0


class RemoteServerProcess(ServerProcess):
    """A server running on another machine, under systemd, presented like a local one.

    Everything above this class -- the console view, the player counter, the sidebar
    status dot -- is the same code that drives a local server. Only the three things
    that genuinely differ are overridden: how the process is started and stopped, how a
    console command reaches it, and where its output is read from.
    """

    def __init__(
        self,
        host: SshHost,
        server_id: str,
        server_dir: str,
        java_path: str,
        ram_mb: int = 2048,
        max_players: int = 20,
        jvm_args: str = "",
        loader_type: str = LOADER_FABRIC,
        edition: str = EDITION_JAVA,
        description: str = "",
    ) -> None:
        super().__init__(
            server_dir=".",
            java_path=java_path,
            ram_mb=ram_mb,
            max_players=max_players,
            jvm_args=jvm_args,
            loader_type=loader_type,
            edition=edition,
        )
        self.host = host
        self.server_id = server_id
        # Overwrite what the base class made of the path: it is a folder on the host.
        self.server_dir = host.path(server_dir)
        self.description = description or f"Minecraft server {server_id}"
        self.service = SystemdService(host, server_id, str(self.server_dir))
        self._console_channel = None
        self._console_thread: threading.Thread | None = None
        self._watch_thread: threading.Thread | None = None
        self._stopping = threading.Event()

    # ------------------------------------------------------------------ identity
    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def process(self):
        # There is no local process object; nothing may assume otherwise.
        return None

    # -------------------------------------------------------------------- launch
    def _build_command(self) -> tuple[list[str], str]:
        if self.is_bedrock:
            binary = str(self.server_dir / "bedrock_server")
            return [binary], (
                f"[Niksnaks-Hosting] Starting {get_edition_display_name(self.edition)} server "
                f"on {self.host.name}...\n"
            )

        if not self.java_path:
            return [], ""

        loader_args, launch_error = resolve_launch_args(self.server_dir, self.loader_type)
        if launch_error:
            self._emit_output(f"[Niksnaks-Hosting] Error: {launch_error}\n")
            return [], ""

        command = [self.java_path, f"-Xmx{self.ram_mb}M", f"-Xms{self.ram_mb}M"]
        if self.jvm_args:
            command.extend(self.jvm_args.split())
        command.extend(loader_args)
        loader_name = get_loader_display_name(self.loader_type)
        return command, (
            f"[Niksnaks-Hosting] Starting {loader_name} server on {self.host.name} "
            f"with {self.ram_mb}MB RAM...\n"
        )

    def start(self) -> bool:
        if self.is_running:
            return False

        command, start_message = self._build_command()
        if not command:
            if self.java_path or self.is_bedrock:
                self._emit_output("[Niksnaks-Hosting] Error: could not work out how to start this server\n")
            else:
                self._emit_output("[Niksnaks-Hosting] Error: No suitable Java runtime found\n")
            return False

        self.status = ServerStatus.STARTING
        self.player_count = 0
        self._emit_players_changed()
        self._emit_output(start_message)
        self._emit_output(f"[Niksnaks-Hosting] Command: {' '.join(command)}\n")

        try:
            self.service.write_unit(self.description, command, self.ram_mb)
            self.service.start()
        except Exception as e:
            self._emit_output(f"[Niksnaks-Hosting] Failed to start server: {e}\n")
            self.status = ServerStatus.STOPPED
            return False

        self._stopping.clear()
        # Ask once up front rather than leaving the pid blank until the first poll:
        # the performance view wants it as soon as the server appears to be running.
        try:
            self._pid = self.service.state().pid or None
        except Exception:
            self._pid = None
        self._start_console_reader()
        self._start_state_watch()
        return True

    def stop(self) -> None:
        if not self.is_running:
            return
        self.status = ServerStatus.STOPPING

        def worker() -> None:
            try:
                # The unit's ExecStop types "stop" into the console, so systemd waits
                # for the world to be written out rather than killing the process.
                self.service.stop()
            except Exception as e:
                self._emit_output(f"[Niksnaks-Hosting] Could not stop the server: {e}\n")
            finally:
                self._finish_stopped()

        threading.Thread(target=worker, daemon=True).start()

    def stop_and_wait(self, timeout: float = 180.0, on_progress=None) -> bool:
        if not self.is_running:
            return True
        self.status = ServerStatus.STOPPING
        if on_progress:
            on_progress(_("Stopping {}").format(self.description))
        try:
            self.service.stop()
        except Exception as e:
            self._emit_output(f"[Niksnaks-Hosting] Could not stop the server: {e}\n")
            return False
        finally:
            self._finish_stopped()
        return True

    def kill(self) -> None:
        try:
            self.service.kill()
        except Exception:
            pass
        self._finish_stopped()

    def send_command(self, command: str) -> None:
        if not self.is_running:
            return
        try:
            self.service.send_console(command)
        except Exception as e:
            self._emit_output(f"[Niksnaks-Hosting] Could not send the command: {e}\n")

    # -------------------------------------------------------------------- output
    def _start_console_reader(self) -> None:
        def reader() -> None:
            try:
                channel = self.service.follow_console()
            except Exception as e:
                self._emit_output(f"[Niksnaks-Hosting] Lost the console: {e}\n")
                return

            self._console_channel = channel
            pending = ""
            try:
                while not self._stopping.is_set():
                    if channel.recv_ready():
                        chunk = channel.recv(65536).decode("utf-8", errors="replace")
                        if not chunk:
                            break
                        pending += chunk
                        # Hold back a partial last line so a split write never shows
                        # up as two broken console lines.
                        *lines, pending = pending.split("\n")
                        for line in lines:
                            self._handle_console_line(line + "\n")
                    elif channel.exit_status_ready():
                        break
                    else:
                        time.sleep(0.1)
            except Exception:
                pass
            finally:
                if pending.strip():
                    self._handle_console_line(pending)
                try:
                    channel.close()
                except Exception:
                    pass

        self._console_thread = threading.Thread(target=reader, daemon=True)
        self._console_thread.start()

    def _handle_console_line(self, line: str) -> None:
        # Identical treatment to a local server: the text is the same either way.
        if self._status == ServerStatus.STARTING and self._is_ready_line(line):
            self.status = ServerStatus.RUNNING
        self._update_player_count_from_output(line)
        self._emit_output(line)

    def _start_state_watch(self) -> None:
        """Notice a server that stopped without the app asking it to."""

        def watcher() -> None:
            while not self._stopping.is_set():
                time.sleep(STATE_POLL_SECONDS)
                if self._stopping.is_set() or self._status == ServerStatus.STOPPED:
                    return
                try:
                    state = self.service.state()
                except Exception:
                    continue
                if state.active:
                    self._pid = state.pid or self._pid
                    continue
                if self._status != ServerStatus.STOPPING:
                    self._emit_output("[Niksnaks-Hosting] The server stopped on its own.\n")
                self._finish_stopped()
                return

        self._watch_thread = threading.Thread(target=watcher, daemon=True)
        self._watch_thread.start()

    def _finish_stopped(self) -> None:
        if self._status == ServerStatus.STOPPED:
            return
        self._stopping.set()
        self._pid = None
        self.status = ServerStatus.STOPPED
        self.player_count = 0
        self._emit_players_changed()
        try:
            if self._console_channel is not None:
                self._console_channel.close()
        except Exception:
            pass
        self._console_channel = None

    # --------------------------------------------------------------------- state
    def adopt_running_state(self) -> None:
        """Pick up a server that was already running when the app started.

        Servers on a host outlive the app entirely, which is the point of using
        systemd. On launch each one is asked whether it is up, and the ones that are
        get their console reattached as though the app had started them.
        """
        try:
            state = self.service.state()
        except Exception:
            return
        if not state.active:
            return

        self._pid = state.pid
        self.status = ServerStatus.RUNNING
        self._stopping.clear()
        try:
            self._emit_output(self.service.read_console_tail())
        except Exception:
            pass
        self._start_console_reader()
        self._start_state_watch()
