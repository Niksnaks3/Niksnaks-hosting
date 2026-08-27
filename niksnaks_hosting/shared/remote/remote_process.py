from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from niksnaks_hosting.shared.backend.loader_launch import resolve_launch_args
from niksnaks_hosting.shared.backend.server_process import ServerProcess
from niksnaks_hosting.shared.remote.ownership import HEARTBEAT_SECONDS
from niksnaks_hosting.shared.remote.remote_service import STOP_GRACE_SECONDS
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
    """A server running on another machine, presented exactly like a local one.

    Everything above this class -- the console view, the player counter, the sidebar
    status dot -- is the same code that drives a local server. Only the three things
    that genuinely differ are overridden: how the process is started and stopped, how a
    console command reaches it, and where its output is read from. Whether the machine
    keeps it alive with systemd or the Task Scheduler is settled below, by host.ops.
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
        # Which kind of service this is -- a systemd user unit or a Scheduled Task --
        # is the host's business, not this class's. Everything below asks the same
        # questions either way.
        self.service = host.ops.service(host, server_id, str(self.server_dir))
        self._console_channel = None
        self._console_thread: threading.Thread | None = None
        self._watch_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._last_heartbeat = 0.0
        # Set when the running process was found rather than started by this class --
        # it did not come from our unit/task, so its liveness has to be polled by
        # re-scanning the host instead of asking the service manager about itself.
        self._foreign = False

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
        ops = self.host.ops
        if self.is_bedrock:
            binary = ops.to_native(self.server_dir / ops.bedrock_binary)
            return ops.bedrock_command(ops.to_native(self.server_dir), binary), (
                f"[Niksnaks-Hosting] Starting {get_edition_display_name(self.edition)} server "
                f"on {self.host.name}...\n"
            )

        if not self.java_path:
            return [], ""

        # A Windows host needs win_args.txt: its classpath separator is ';', and a
        # unix_args.txt handed to it would silently produce one enormous broken path.
        loader_args, launch_error = resolve_launch_args(
            self.server_dir, self.loader_type, prefer_windows=ops.platform == "windows"
        )
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

        # Two JVMs on one world directory corrupt it, and the far machine may well have
        # its own copy of this app pointed at the same folder. The service manager only
        # knows about its own unit, so this asks the machine itself.
        foreign = 0
        try:
            foreign = self.service.foreign_process_pid()
        except Exception:
            foreign = 0
        if foreign:
            self._emit_output(
                "[Niksnaks-Hosting] This server is already running on "
                f"{self.host.name} as process {foreign}, started by something else. "
                "Attaching to it instead of starting a second copy.\n"
            )
            self.adopt_running_state(pid=foreign)
            return False

        # Nothing is running, but another install of this app may still be looking after
        # this folder and about to start it. Said rather than refused: the marker is
        # advisory, and refusing on it would leave a server unstartable after a crash.
        other = self._other_owner()
        if other:
            self._emit_output(
                f"[Niksnaks-Hosting] Heads up: {other.label} is also managing this server. "
                "Starting it in two places at once would damage the world.\n"
            )

        self._foreign = False
        self.status = ServerStatus.STARTING
        self.player_count = 0
        self._emit_players_changed()
        self._emit_output(start_message)
        self._emit_output(f"[Niksnaks-Hosting] Command: {' '.join(command)}\n")

        try:
            self.service.write_unit(self.description, command, self.ram_mb)
            # Anything set-up could not do that was worth attempting but not worth
            # refusing to start over. Said here because this is the only place with a
            # console to say it in.
            for warning in self.service.take_warnings():
                self._emit_output(f"[Niksnaks-Hosting] {warning}\n")
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
        self._claim_ownership()
        self._start_console_reader()
        self._start_state_watch()
        return True

    # A process this app did not start is not behind our unit or task, so every one of
    # these would be addressed to the wrong thing: systemctl would stop a unit that was
    # never loaded and report success, and _finish_stopped would then grey the server
    # out while the JVM carried on holding the world open. Force stop is the exception --
    # a pid can be killed whoever started it -- so that one stays available.
    _FOREIGN_REFUSAL = (
        "[Niksnaks-Hosting] This server was started by something else on {host}, so this "
        "app cannot reach its console. Stop it where it was started, or use Force stop.\n"
    )

    def stop(self) -> None:
        if not self.is_running:
            return
        if self._foreign:
            self._emit_output(self._FOREIGN_REFUSAL.format(host=self.host.name))
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

    def stop_and_wait(
        self,
        timeout: float = STOP_GRACE_SECONDS,
        on_wait: Callable[[float], None] | None = None,
    ) -> bool:
        """Stop the server and block until the host says it has finished saving.

        Signature and contract are the base class's, deliberately: quitting the app
        calls this as stop_and_wait(on_wait=...) without knowing or caring whether the
        server is local, and on_wait is called with the seconds spent waiting so far.
        """
        if not self.is_running:
            return True
        if self._foreign:
            # Said, not silently skipped: this runs when the app is closing, and a
            # server it never started is one it has no business claiming to have shut
            # down cleanly.
            self._emit_output(self._FOREIGN_REFUSAL.format(host=self.host.name))
            return False
        self.status = ServerStatus.STOPPING

        # service.stop() blocks until the host has written the world out, so reporting
        # elapsed time while that happens needs it on a thread of its own.
        failure: list[str] = []

        def worker() -> None:
            try:
                self.service.stop()
            except Exception as e:
                failure.append(str(e))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        started = time.monotonic()
        deadline = started + max(1.0, float(timeout))
        while thread.is_alive() and time.monotonic() < deadline:
            if on_wait:
                on_wait(time.monotonic() - started)
            time.sleep(0.5)

        stopped_in_time = not thread.is_alive()
        if failure:
            self._emit_output(f"[Niksnaks-Hosting] Could not stop the server: {failure[0]}\n")
        elif not stopped_in_time:
            self._emit_output("[Niksnaks-Hosting] Server did not stop in time, killing...\n")
            try:
                self.service.kill()
            except Exception:
                pass

        self._finish_stopped()
        return stopped_in_time and not failure

    def kill(self) -> None:
        try:
            if self._foreign and self._pid:
                # Not ours to ask the service manager about, but a pid is a pid.
                self.service.kill_pid(int(self._pid))
            else:
                self.service.kill()
        except Exception:
            pass
        self._finish_stopped()

    def send_command(self, command: str) -> None:
        if not self.is_running:
            return
        if self._foreign:
            self._emit_output(self._FOREIGN_REFUSAL.format(host=self.host.name))
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
                if self._foreign:
                    # There is no unit/task behind this process, so the only way to
                    # know it is still there is to look for it again the same way it
                    # was first found.
                    try:
                        active = bool(self.service.foreign_process_pid())
                    except Exception:
                        continue
                else:
                    try:
                        state = self.service.state()
                    except Exception:
                        continue
                    active = state.active
                    if active:
                        self._pid = state.pid or self._pid
                if active:
                    # The watcher is already awake on a timer, so the marker rides along
                    # rather than costing a thread of its own.
                    if time.monotonic() - self._last_heartbeat >= HEARTBEAT_SECONDS:
                        self._claim_ownership()
                    continue
                if self._status != ServerStatus.STOPPING:
                    self._emit_output("[Niksnaks-Hosting] The server stopped on its own.\n")
                self._finish_stopped()
                return

        self._watch_thread = threading.Thread(target=watcher, daemon=True)
        self._watch_thread.start()

    # ------------------------------------------------------------------ ownership
    def _other_owner(self):
        """Another install of this app that has been looking after this folder lately."""
        try:
            from niksnaks_hosting.shared.remote.ownership import other_owner

            return other_owner(self.host, self.server_dir)
        except Exception:
            return None

    def _claim_ownership(self) -> None:
        try:
            from niksnaks_hosting.shared.remote.ownership import claim

            claim(self.host, self.server_dir)
            self._last_heartbeat = time.monotonic()
        except Exception:
            pass

    def _release_ownership(self) -> None:
        """Let go of the marker off the main thread; it is one SFTP round trip."""

        def worker() -> None:
            try:
                from niksnaks_hosting.shared.remote.ownership import release

                release(self.host, self.server_dir)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _finish_stopped(self) -> None:
        if self._status == ServerStatus.STOPPED:
            return
        self._release_ownership()
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
    def adopt_running_state(self, pid: int | None = None) -> None:
        """Pick up a server that is already running rather than starting it.

        With no *pid*, this is the app-launch case: servers on a host outlive the app
        entirely, so each one is asked whether it is up, and the ones that are get
        their console reattached as though the app had started them.

        With *pid*, the process was found by foreign_process_pid() -- something else
        started it, so it is not behind our unit/task and service.state() will never
        report it as active. The pid itself is the only evidence there is, and the
        watcher below is told to keep re-scanning for it rather than asking the
        service manager about a unit that was never involved.
        """
        self._foreign = pid is not None
        if not self._foreign:
            try:
                state = self.service.state()
            except Exception:
                return
            if not state.active:
                return
            pid = state.pid

        self._pid = pid
        self.status = ServerStatus.RUNNING
        self._stopping.clear()
        self._claim_ownership()
        try:
            self._emit_output(self.service.read_console_tail())
        except Exception:
            pass
        self._start_console_reader()
        self._start_state_watch()
