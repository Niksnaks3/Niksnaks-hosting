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

# How often a server we are stopping but did not start is asked whether it has gone.
# Each round is an SSH round trip, so this is slower than the local equivalent.
FOREIGN_STOP_POLL_SECONDS = 2.0

# How much of an existing console.log to replay when picking up a server that was
# already running. A server up for a week has a log nobody wants shown in full.
ADOPT_TAIL_LINES = 500


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
        java_path_provider: Callable[[], str] | None = None,
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
        # Where Java lives on the far machine is one SSH round trip to answer, and it is
        # not needed until this server is actually started. Held as a callable so
        # building the object stays free of the network.
        self._java_path_provider = java_path_provider
        self._service = None
        self._service_lock = threading.Lock()
        self._console_channel = None
        self._console_thread: threading.Thread | None = None
        self._watch_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        # Adoption is asked for from more than one thread -- once when the process is
        # first built, and again by every state sweep while it still looks stopped --
        # and each round is slow enough over SSH that the next can start before it has
        # finished. Two that got through would leave two console readers on one server.
        self._adopt_lock = threading.Lock()
        self._last_heartbeat = 0.0
        # Set when the running process was found rather than started by this class --
        # it did not come from our unit/task, so its liveness has to be polled by
        # re-scanning the host instead of asking the service manager about itself.
        self._foreign = False
        # Set when the machine could not be reached, so the app can say that instead of
        # showing a server as switched off when it has simply lost sight of it.
        self.host_unreachable = False
        self._unreachable_said = ""

    # ------------------------------------------------------------------ identity
    @property
    def service(self):
        """The host's way of keeping this server alive, resolved on first use.

        Which kind it is -- a systemd user unit or a Scheduled Task -- is the host's
        business, not this class's, and working it out can mean asking the machine.
        __init__ runs on the main thread, so it must not be the place that asks.
        """
        with self._service_lock:
            if self._service is None:
                self._service = self.host.ops.service(self.host, self.server_id, str(self.server_dir))
            return self._service

    @property
    def is_remote(self) -> bool:
        return True

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def process(self):
        # There is no local process object; nothing may assume otherwise.
        return None

    @property
    def command_echoes_back(self) -> bool:
        # Only for a server something else started: that is the case where the command
        # goes through the folder's inbox and the app on the far side types it in and
        # says so in the console log this one is reading.
        return self._foreign

    # -------------------------------------------------------------------- launch
    def _build_command(self) -> tuple[list[str], str]:
        ops = self.host.ops
        if self.is_bedrock:
            binary = ops.to_native(self.server_dir / ops.bedrock_binary)
            return ops.bedrock_command(ops.to_native(self.server_dir), binary), (
                f"[Niksnaks-Hosting] Starting {get_edition_display_name(self.edition)} server "
                f"on {self.host.name}...\n"
            )

        if not self.java_path and self._java_path_provider:
            try:
                self.java_path = self._java_path_provider()
            except Exception:
                self.java_path = ""
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
        """Ask the host to start the server. Everything after saying so is off-thread.

        Starting a server over SSH is half a dozen round trips -- reading the loader's
        args file, looking for a process already in the folder, writing the unit,
        starting it -- and this is called straight off a button press. Doing any of it
        here would freeze the window until the far machine answered, which for one that
        has just gone to sleep is the better part of a minute.
        """
        if self.is_running:
            return False

        self._foreign = False
        self.status = ServerStatus.STARTING
        self.player_count = 0
        self._emit_players_changed()
        threading.Thread(target=self._start_on_host, daemon=True).start()
        return True

    def _start_on_host(self) -> None:
        command, start_message = self._build_command()
        if not command:
            if self.java_path or self.is_bedrock:
                self._emit_output("[Niksnaks-Hosting] Error: could not work out how to start this server\n")
            else:
                self._emit_output("[Niksnaks-Hosting] Error: No suitable Java runtime found\n")
            self.status = ServerStatus.STOPPED
            return

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
            if self._status == ServerStatus.STARTING:
                # Adoption did not take -- a state sweep had it -- so the row must not
                # be left claiming this app is starting something it is not.
                self.status = ServerStatus.STOPPED
            return

        # Nothing is running, but another install of this app may still be looking after
        # this folder and about to start it. Said rather than refused: the marker is
        # advisory, and refusing on it would leave a server unstartable after a crash.
        other = self._other_owner()
        if other:
            self._emit_output(
                f"[Niksnaks-Hosting] Heads up: {other.label} is also managing this server. "
                "Starting it in two places at once would damage the world.\n"
            )

        if self.is_bedrock:
            from niksnaks_hosting.shared.backend.bedrock_manager import ensure_bedrock_properties

            try:
                for setting in ensure_bedrock_properties(self.server_dir):
                    self._emit_output(f"[Niksnaks-Hosting] Set {setting} in server.properties\n")
            except Exception:
                pass

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
            return

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

    def stop(self) -> None:
        if not self.is_running:
            return
        self.status = ServerStatus.STOPPING

        if self._foreign:
            threading.Thread(target=self._stop_foreign, daemon=True).start()
            return

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

    def _stop_foreign(self, timeout: float = STOP_GRACE_SECONDS) -> bool:
        """Stop a server on the host that something else started.

        There is no unit of ours to ask -- systemctl would stop a unit that was never
        loaded and cheerfully report success while the JVM carried on holding the world
        open. What there is, is the folder: the server is reading its console from an
        inbox in it, whether a unit written from a third machine is feeding that inbox
        straight down its stdin or the far machine's own copy of this app is watching
        the file. So "stop" goes there, and the pid says when it worked.
        """
        pid = int(self._pid or 0)

        if not self._console_reaches_server():
            self._emit_output(
                f"[Niksnaks-Hosting] Nothing on {self.host.name} is listening to this "
                "server's console, so a stop sent from here would not arrive. It was "
                "started by something that does not read the server folder -- an older "
                "Niksnaks-Hosting, or a script. Stop it over there, update the copy of "
                "this app on that computer, or use Force stop.\n"
            )
            self.status = ServerStatus.RUNNING
            return False

        self._emit_output("[Niksnaks-Hosting] Sending stop command...\n")
        try:
            if not self.is_bedrock:
                self.service.send_console_file("save-all flush")
            self.service.send_console_file("stop")
        except Exception as e:
            self._emit_output(f"[Niksnaks-Hosting] Could not stop the server: {e}\n")
            self.status = ServerStatus.RUNNING
            return False

        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            time.sleep(FOREIGN_STOP_POLL_SECONDS)
            try:
                if pid and not self.service.pid_alive(pid):
                    break
                if not pid and not self.service.foreign_process_pid():
                    break
            except Exception:
                continue
        else:
            self._emit_output("[Niksnaks-Hosting] Server did not stop in time, killing...\n")
            try:
                if pid:
                    self.service.kill_pid(pid)
            except Exception:
                pass
            self._finish_stopped()
            return False

        self._emit_output("[Niksnaks-Hosting] Server stopped.\n")
        self._finish_stopped()
        return True

    def stop_and_wait(
        self,
        timeout: float = STOP_GRACE_SECONDS,
        on_wait: Callable[[float], None] | None = None,
    ) -> bool:
        """Closing this app never stops a server on another machine. Does nothing.

        Signature and contract are the base class's, deliberately: quitting calls this
        without knowing or caring where a server is. What it means, though, is the
        opposite of what it means locally. On this computer, closing the window is the
        last moment a world can be written out, so the base class saves it. On a host it
        is not: the server is run by that machine's service manager, it was up before
        this window opened, it stays up after it closes, and other people are very
        likely playing on it. The whole point of putting a server on a host is that the
        computer you administer it from can be closed, suspended or carried out of the
        building without the server noticing.

        Stopping and backing that server up is the job of the copy of this app on the
        host itself, when *that* one is closed -- see ServerProcess._stop_adopted_and_wait.

        ServerManager.shutdown_all_gracefully already leaves remote servers out of the
        quit entirely; this is here so that a caller that has not learned to would still
        do no harm.
        """
        return True

    def kill(self) -> None:
        def worker() -> None:
            try:
                if self._foreign and self._pid:
                    # Not ours to ask the service manager about, but a pid is a pid.
                    self.service.kill_pid(int(self._pid))
                else:
                    self.service.kill()
            except Exception:
                pass
            self._finish_stopped()

        # Killing is an SSH round trip and this comes straight off a button press.
        threading.Thread(target=worker, daemon=True).start()

    def detach(self) -> None:
        """Stop following this server, and change nothing about it.

        What the app does when it is asked to forget a host: the unit or task on the far
        machine goes on running exactly as it is, its folder is left alone, and its own
        machine's copy of this app still has it. All that ends is the watching -- the
        console channel, the state poller, and the ownership marker claiming this
        install is the one looking after the folder, which stops being true the moment
        there is nobody here to look.
        """
        self._stopping.set()
        self._release_ownership()
        try:
            if self._console_channel is not None:
                self._console_channel.close()
        except Exception:
            pass
        self._console_channel = None

    def send_command(self, command: str) -> None:
        if not self.is_running:
            return

        def worker() -> None:
            try:
                # A server something else started is not behind our unit's FIFO or its
                # console port, but it is reading the folder's inbox, which is the one
                # channel that does not depend on who launched it.
                if self._foreign:
                    self.service.send_console_file(command)
                else:
                    self.service.send_console(command)
            except Exception as e:
                self._emit_output(f"[Niksnaks-Hosting] Could not send the command: {e}\n")

        # One SSH round trip, and this is called straight off the console entry.
        threading.Thread(target=worker, daemon=True).start()

    # -------------------------------------------------------------------- output
    def _start_console_reader(self, tail_lines: int = 0) -> None:
        def reader() -> None:
            try:
                channel = self.service.follow_console(tail_lines=tail_lines)
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
                    # There is no unit/task behind this process to ask about itself, so
                    # the pid is the whole of the evidence. Asking after that one pid is
                    # a great deal cheaper than the scan that first turned it up, which
                    # walks every process on the machine.
                    try:
                        if self._pid:
                            active = self.service.pid_alive(int(self._pid))
                        else:
                            active = bool(self.service.foreign_process_pid())
                    except Exception:
                        continue
                else:
                    try:
                        state = self.service.state()
                    except Exception as e:
                        # Losing the link is not evidence the server went down, so the
                        # status stays as it was -- but it is worth saying, or the row
                        # goes on claiming to know something it stopped being told.
                        self._note_unreachable(e)
                        continue
                    self._clear_unreachable()
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

    def _console_reaches_server(self) -> bool:
        """Whether a line put in this folder's inbox would actually reach the server.

        Two ways it can. A unit of ours -- possibly written by this app on a third
        computer -- holds the inbox open as the process's stdin, in which case it is a
        FIFO and an append goes straight in. Otherwise a copy of this app on the host is
        running the server itself and watching the file, which it says in the folder's
        owner marker. Neither is true for a server started by an older release, and
        sending into that is how a world gets killed at the end of the grace period.
        """
        from niksnaks_hosting.shared.remote.ownership import console_is_watched
        from niksnaks_hosting.shared.remote.remote_service import CONSOLE_IN

        try:
            if (self.server_dir / CONSOLE_IN).is_fifo():
                return True
        except Exception:
            pass
        try:
            return console_is_watched(self.server_dir)
        except Exception:
            # Unreachable is not the same as unwatched, and refusing on a blip would be
            # the wrong way round: let the stop be attempted and time out instead.
            return True

    # ------------------------------------------------------------------ ownership
    def _other_owner(self):
        """Another install of this app that has been looking after this folder lately."""
        try:
            from niksnaks_hosting.shared.remote.ownership import other_owner

            return other_owner(self.server_dir)
        except Exception:
            return None

    def _claim_ownership(self) -> None:
        # A server this app merely found is being looked after by whatever started it.
        # Stamping our own marker on it would tell the other install that the folder
        # had changed hands, when in fact we cannot even reach its console.
        if self._foreign:
            return
        try:
            from niksnaks_hosting.shared.remote.ownership import claim

            claim(self.server_dir, pid=int(self._pid or 0))
            self._last_heartbeat = time.monotonic()
        except Exception:
            pass

    def _release_ownership(self) -> None:
        """Let go of the marker off the main thread; it is one SFTP round trip."""

        def worker() -> None:
            try:
                from niksnaks_hosting.shared.remote.ownership import release

                release(self.server_dir)
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
    def _note_unreachable(self, error: Exception) -> None:
        """Say in this server's own console that its machine could not be reached.

        Said once per distinct reason rather than on every poll: repeated every few
        seconds it would bury the console, and the point is only that an answer is
        waiting there for whoever wonders why the server reads as stopped.
        """
        self.host_unreachable = True
        text = f"[Niksnaks-Hosting] Could not reach {self.host.name}: {error}\n"
        if text == self._unreachable_said:
            return
        self._unreachable_said = text
        self._emit_output(text)

    def _clear_unreachable(self) -> None:
        if self.host_unreachable:
            self.host_unreachable = False
            self._unreachable_said = ""
            self._emit_output(f"[Niksnaks-Hosting] {self.host.name} is reachable again.\n")

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
        if not self._adopt_lock.acquire(blocking=False):
            return
        try:
            self._adopt_running_state(pid)
        finally:
            self._adopt_lock.release()

    def _adopt_running_state(self, pid: int | None = None) -> None:
        if self._status != ServerStatus.STOPPED and pid is None:
            # Another round got there first while this one was waiting on the host.
            return

        self._foreign = pid is not None
        if not self._foreign:
            try:
                state = self.service.state()
            except Exception as e:
                # Staying silent here is what makes an unreachable machine look exactly
                # like a server somebody switched off, which is the harder of the two to
                # work out from the outside.
                self._note_unreachable(e)
                return
            self._clear_unreachable()
            if state.active:
                pid = state.pid
            else:
                # No unit of ours is up, which is not the same as the server being
                # down. The far machine's own copy of this app starts its servers as
                # plain child processes that no service manager knows about, and only
                # looking for them finds those -- without this, a server somebody
                # started over there reads as stopped here for as long as the app runs.
                try:
                    pid = self.service.foreign_process_pid()
                except Exception:
                    pid = 0
                if not pid:
                    return
                self._foreign = True

        self._pid = pid
        self.status = ServerStatus.RUNNING
        self._stopping.clear()
        self._claim_ownership()
        if self._foreign:
            self._emit_output(
                f"[Niksnaks-Hosting] This server is running on {self.host.name} as "
                f"process {pid}, started by something else. Its console is read from "
                "the server folder, so it can still be watched and stopped from here.\n"
            )
        # The follower replays the tail itself rather than the tail being fetched
        # separately and then replayed again by a stream that starts from the top.
        self._start_console_reader(tail_lines=ADOPT_TAIL_LINES)
        self._start_state_watch()

    def refresh_state(self) -> None:
        """Ask the host again whether this server is up, for one that looks stopped.

        A running server has a watcher of its own and needs nothing from here. A stopped
        one has nothing watching it, so a server started on the far machine after this
        app opened would otherwise stay greyed out until the next launch.
        """
        if self._status != ServerStatus.STOPPED:
            return
        self.adopt_running_state()
