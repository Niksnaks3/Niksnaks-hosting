import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from niksnaks_hosting.shared.backend.loader_launch import resolve_launch_args
from niksnaks_hosting.shared.core.events import EventEmitter
from niksnaks_hosting.shared.remote.ownership import (
    HEARTBEAT_SECONDS,
    other_owner,
    owner_on_this_machine,
)
from niksnaks_hosting.shared.utils.constants import (
    CONSOLE_IN_FILE,
    CONSOLE_LOG_FILE,
    EDITION_JAVA,
    LAUNCHER_PID_FILE,
    LOADER_FABRIC,
    ServerStatus,
    get_edition_display_name,
    get_loader_display_name,
    is_bedrock,
    normalize_edition,
    normalize_loader,
)
from niksnaks_hosting.shared.utils import memory_limit
from niksnaks_hosting.shared.utils.subprocess_utils import hidden_subprocess_kwargs

LOG_HISTORY_LINES = 1000

# How often the folder's inbox is checked for a command typed from somewhere else, and
# how often an adopted server's log and process are looked at. Half a second is below
# what a person notices and costs one stat of a small file.
INBOX_POLL_SECONDS = 0.5
ADOPT_POLL_SECONDS = 2.0

# How much of an existing console.log to show when picking up a server that was already
# running. A server up for a week has a log nobody wants replayed in full.
ADOPT_TAIL_BYTES = 64 * 1024

# The names a Minecraft server process goes by. Bedrock is its own binary; Java Edition
# is whatever JVM launched it, which is java on every platform and javaw on Windows.
SERVER_PROCESS_NAMES = ("java", "bedrock_server")

# One scan of the machine answers for every server, and the answer is held briefly: a
# sweep asks about each server in turn, and walking every process once per server is the
# same work repeated as many times as there are servers.
PID_SCAN_CACHE_SECONDS = 3.0

_pid_scan: tuple[float, dict[str, int]] = (0.0, {})
_pid_scan_lock = threading.Lock()


def running_server_pids(fresh: bool = False) -> dict[str, int]:
    """Which folders on this machine have a Minecraft server running in them.

    Keyed by the folder's real path, so a server started by anything at all -- another
    copy of this app, or a systemd unit a copy on another computer wrote -- is found.
    The working directory is what is matched on, because the folder is the thing that
    must not be opened twice and the command line is not something this app controls.
    """
    global _pid_scan
    with _pid_scan_lock:
        stamp, found = _pid_scan
        if not fresh and time.monotonic() - stamp < PID_SCAN_CACHE_SECONDS:
            return found

    import psutil

    mine = os.getpid()
    found = {}
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if process.pid == mine:
                continue
            name = (process.info.get("name") or "").lower()
            if not name.startswith(SERVER_PROCESS_NAMES):
                continue
            cwd = process.cwd()
            if cwd:
                found.setdefault(os.path.realpath(cwd), process.pid)
        except Exception:
            # A process that went away mid-scan, or one this account may not look at.
            continue

    with _pid_scan_lock:
        _pid_scan = (time.monotonic(), found)
    return found


def forget_running_server_pids() -> None:
    """Drop the memoised scan, so the next question is answered by looking again.

    Called when this app has just changed what is running. A scan taken a moment before
    a server was launched is not merely stale, it is the one answer that would have the
    app conclude the server it just started is not there.
    """
    global _pid_scan
    with _pid_scan_lock:
        _pid_scan = (0.0, {})


def server_pid_in(server_dir: Path | str, fresh: bool = False) -> int:
    """The pid of a server running out of *server_dir*, or 0 if the folder is free."""
    try:
        root = os.path.realpath(str(server_dir))
    except OSError:
        return 0
    return running_server_pids(fresh=fresh).get(root, 0)


def console_has_reader(inbox: Path) -> bool:
    """Whether something is holding this folder's inbox open, waiting to be typed into.

    A unit written onto this machine from another computer keeps console.in open as the
    server's stdin for as long as the server runs. Opening a FIFO for writing succeeds
    only while somebody is reading it, and fails at once with ENXIO otherwise, so this
    is a direct answer to "is that server up", asked of the folder rather than of the
    process table -- which matters because this app usually cannot see that process at
    all. Under Flatpak it runs in a process namespace of its own, where a server systemd
    started is not merely hidden but absent.
    """
    try:
        if not inbox.is_fifo():
            # A plain file is the inbox of a server some copy of this app is running as
            # a child process; opening it says nothing about whether anyone is reading.
            return False
        handle = os.open(str(inbox), os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    os.close(handle)
    return True


def console_is_live(server_dir: Path) -> str:
    """What is running a server in this folder that this app cannot see, if anything.

    Returns a phrase naming it, for the console to say, or an empty string. Three things
    leave evidence in the folder. A unit started from another computer holds the inbox
    open, which console_has_reader asks about directly; a Scheduled Task started the same
    way onto a Windows machine leaves a live launcher pid instead. Another copy of this
    app running the server as its own child leaves the owner marker -- that one is a
    claim rather than a measurement, so it is only believed once the two direct answers
    have come up empty.
    """
    try:
        inbox = server_dir / CONSOLE_IN_FILE
        if inbox.is_fifo():
            return "started from another computer" if console_has_reader(inbox) else ""
    except OSError:
        return ""

    if launcher_is_watching(server_dir):
        return "started from another computer"

    owner = other_owner(server_dir)
    if owner is not None and owner.console:
        return f"started by {owner.label}"
    return ""


def launcher_is_watching(server_dir: Path) -> bool:
    """Whether a Windows launcher in this folder is forwarding the inbox to the server.

    Windows has no FIFO, so console_has_reader has nothing to ask. What a server started
    on a Windows machine from another computer does have is the launcher its Scheduled
    Task runs: it watches console.in for as long as the server is up and records its own
    pid in the folder while it does. That pid being alive is the same answer the FIFO
    gives on Linux -- a line put in the inbox will reach the server.
    """
    try:
        record = (server_dir / LAUNCHER_PID_FILE).read_text(errors="replace").split()
    except (OSError, ValueError):
        return False
    if not record:
        return False
    try:
        wrapper = int(record[0])
    except ValueError:
        return False
    return pid_alive(wrapper)


def pid_alive(pid: int | None) -> bool:
    """Whether a process is still doing anything.

    A zombie is deliberately not alive. A server started by another copy of this app is
    that app's child, and it stays in the process table as a zombie until that app
    reaps it -- so pid_exists() alone would have a server that has finished saving and
    exited still read as running, which is exactly the answer a stop is waiting on.
    """
    if not pid:
        return False
    try:
        import psutil

        return psutil.Process(int(pid)).status() != psutil.STATUS_ZOMBIE
    except Exception:
        # NoSuchProcess is the common case and means gone; anything else -- a process
        # belonging to another account, say -- is not evidence either way, and the
        # caller only ever asks about one it has already seen.
        return False


def kill_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        import psutil

        psutil.Process(int(pid)).kill()
    except Exception:
        pass

# How long a server gets to write its world out before we stop being polite. A vanilla
# world saves in a second or two; a big modded one with a few hundred loaded chunks can
# spend the better part of a minute in "Saving chunks for level".
STOP_GRACE_SECONDS = 120.0

class ServerProcess(EventEmitter):
    def __init__(
        self,
        server_dir: str,
        java_path: str,
        ram_mb: int = 2048,
        max_players: int = 20,
        jvm_args: str = "",
        loader_type: str = LOADER_FABRIC,
        edition: str = EDITION_JAVA,
    ):
        super().__init__()
        self.server_dir = Path(server_dir)
        self.java_path = java_path
        self.ram_mb = ram_mb
        self.max_players = max(1, int(max_players))
        self.jvm_args = jvm_args
        self.loader_type = normalize_loader(loader_type)
        self.edition = normalize_edition(edition)
        self.player_count = 0
        self._process: subprocess.Popen | None = None
        self._status = ServerStatus.STOPPED
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._pid: int | None = None
        self._memory_limit_job = None
        # Bounded by the deque itself: dropping the oldest line is then O(1) rather than
        # shifting a thousand entries on every line the server prints.
        self.log_history: deque[str] = deque(maxlen=LOG_HISTORY_LINES)
        # This run's transcript in the server's own folder, so anything that can see the
        # folder -- including this app running on another computer -- can read along.
        self._console_log = None
        self._console_lock = threading.Lock()
        self._console_share_open = False
        # Set when the running server was found rather than started here: another copy
        # of this app, or a unit a copy on another computer wrote. There is no pipe to
        # it, so its console and its stop both go through the folder instead.
        self._adopted = False
        self._adopt_stop = threading.Event()
        # Adoption is asked for from more than one thread -- when the process is first
        # built, and by every state sweep while it still looks stopped. Two that got
        # through at once would leave two log followers on one server.
        self._adopt_lock = threading.Lock()

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str):
        if self._status != value:
            self._status = value
            if value == ServerStatus.STOPPED:
                self._release_memory_limit()
                self._close_console_share()
            self.emit_on_main_thread("status-changed", value)

    def _release_memory_limit(self) -> None:
        if self._memory_limit_job is not None:
            memory_limit.release(self._memory_limit_job)
            self._memory_limit_job = None

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def process(self) -> subprocess.Popen | None:
        return self._process

    @property
    def is_running(self) -> bool:
        return self._status in (ServerStatus.RUNNING, ServerStatus.STARTING)

    @property
    def is_bedrock(self) -> bool:
        return is_bedrock(self.edition)

    @property
    def command_echoes_back(self) -> bool:
        """Whether a command sent from here will come back in this server's output.

        A command typed at the app that is actually running the server goes down a pipe
        and is never seen again -- so the console view has to show it. One that goes
        through the folder's inbox is read by that other app, which types it in and says
        so in the console both apps are reading, and echoing it here as well would show
        it twice.
        """
        return self._adopted

    def _build_bedrock_command(self) -> tuple[list[str], str]:
        from niksnaks_hosting.shared.backend.bedrock_manager import BedrockManager

        manager = BedrockManager()
        binary = manager.server_binary(self.server_dir)
        if binary:
            return [str(binary)], ""

        if manager.foreign_binary(self.server_dir):
            return [], (
                "This server's files were made for a different operating system. "
                "Open Properties and use the version button to install the Bedrock server files for this computer."
            )
        return [], "Bedrock server executable not found (reinstall the server files)"

    def start(self) -> bool:
        if self.is_running:
            return False

        # Two JVMs on one world directory corrupt it, and this folder is not
        # necessarily driven from here alone: another copy of this app, or a unit that
        # a copy on another computer wrote onto this machine, may already be running
        # it. Attaching to that is the only answer that does not damage the world.
        # The folder is asked as well as the process table, because a server started
        # through a unit is invisible to this app when it runs inside a sandbox.
        if server_pid_in(self.server_dir, fresh=True) or console_is_live(self.server_dir):
            self.adopt_running_state()
            return False

        self._adopted = False
        env = None

        if self.is_bedrock:
            cmd, launch_error = self._build_bedrock_command()
            if launch_error:
                self._emit_output(f"[Niksnaks-Hosting] Error: {launch_error}\n")
                return False

            from niksnaks_hosting.shared.backend.bedrock_manager import BedrockManager

            env = BedrockManager().launch_env(self.server_dir)

            # Bedrock has no -Xmx, so the ceiling comes from the OS instead.
            cmd = memory_limit.wrap_command(cmd, self.ram_mb)
            start_message = (
                f"[Niksnaks-Hosting] Starting {get_edition_display_name(self.edition)} server "
                f"with a {self.ram_mb}MB memory limit...\n"
            )
        else:
            if not self.java_path:
                self._emit_output("[Niksnaks-Hosting] Error: No suitable Java runtime found\n")
                return False

            loader_args, launch_error = resolve_launch_args(self.server_dir, self.loader_type)
            if launch_error:
                self._emit_output(f"[Niksnaks-Hosting] Error: {launch_error}\n")
                return False

            cmd = [
                self.java_path,
                f"-Xmx{self.ram_mb}M",
                f"-Xms{self.ram_mb}M",
            ]
            if self.jvm_args:
                cmd.extend(self.jvm_args.split())
            cmd.extend(loader_args)

            loader_name = get_loader_display_name(self.loader_type)
            start_message = f"[Niksnaks-Hosting] Starting {loader_name} server with {self.ram_mb}MB RAM...\n"

        self.status = ServerStatus.STARTING
        self.player_count = 0
        self._emit_players_changed()
        self._emit_output(start_message)
        self._emit_output(f"[Niksnaks-Hosting] Command: {' '.join(cmd)}\n")

        try:
            popen_kwargs = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "cwd": str(self.server_dir),
                "text": True,
                "bufsize": 1,
            }
            if env is not None:
                popen_kwargs["env"] = env
            popen_kwargs.update(hidden_subprocess_kwargs())

            self._process = subprocess.Popen(cmd, **popen_kwargs)
            self._pid = self._process.pid
            forget_running_server_pids()
            self._open_console_share()

            if self.is_bedrock:
                self._memory_limit_job = memory_limit.apply_to_process(self._pid, self.ram_mb)

            self._stdout_thread = threading.Thread(target=self._read_output, daemon=True)
            self._stdout_thread.start()

            return True

        except Exception as e:
            self._emit_output(f"[Niksnaks-Hosting] Failed to start server: {e}\n")
            self.status = ServerStatus.STOPPED
            return False

    def stop(self):
        if self._adopted:
            self._stop_adopted()
            return
        if not self.is_running or not self._process:
            return

        self.status = ServerStatus.STOPPING
        self._emit_output("[Niksnaks-Hosting] Sending stop command...\n")
        self.send_command("stop")

        def _wait_stop():
            try:
                self._process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._emit_output("[Niksnaks-Hosting] Server did not stop gracefully, killing...\n")
                self.kill()

            self._pid = None
            self.status = ServerStatus.STOPPED
            self._emit_output("[Niksnaks-Hosting] Server stopped.\n")

        threading.Thread(target=_wait_stop, daemon=True).start()

    def stop_and_wait(
        self,
        timeout: float = STOP_GRACE_SECONDS,
        on_wait: Callable[[float], None] | None = None,
    ) -> bool:
        """Stop the server and block until it has finished saving and exited.

        stop() hands the waiting to a thread and returns immediately, which is right for
        a button press and wrong for anything that has to happen *after* the world is on
        disk -- taking a backup, or quitting the app. Call this from a worker thread, not
        the main loop: it is allowed to take minutes.

        *on_wait* is called every half second with the seconds spent waiting so far, so a
        caller can show that something is still happening. Returns False if the server had
        to be killed because it ignored the stop command for *timeout* seconds.
        """
        if self._adopted:
            return self._stop_adopted_and_wait(timeout=timeout, on_wait=on_wait)

        process = self._process
        if not self.is_running or not process:
            return True

        self.status = ServerStatus.STOPPING
        self._emit_output("[Niksnaks-Hosting] Saving world and stopping...\n")

        # Bedrock has no save-all; its own stop flushes the world before it exits.
        if not self.is_bedrock:
            self.send_command("save-all flush")
        self.send_command("stop")

        started = time.monotonic()
        deadline = started + max(1.0, float(timeout))
        while process.poll() is None and time.monotonic() < deadline:
            if on_wait:
                on_wait(time.monotonic() - started)
            time.sleep(0.5)

        stopped_on_its_own = process.poll() is not None
        if not stopped_on_its_own:
            self._emit_output("[Niksnaks-Hosting] Server did not stop in time, killing...\n")
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass

        self._pid = None
        self.status = ServerStatus.STOPPED
        self.player_count = 0
        self._emit_players_changed()
        self._emit_output("[Niksnaks-Hosting] Server stopped.\n")
        return stopped_on_its_own

    def kill(self):
        if self._adopted:
            if not self._pid:
                # Nothing to send a signal to: the process belongs to a unit outside
                # this app's sandbox. Saying so and stopping it properly is better than
                # a force stop that quietly does nothing and marks the server off.
                self._emit_output(
                    "[Niksnaks-Hosting] This server was started from another computer, "
                    "so there is no process here to force. Sending a normal stop.\n"
                )
                self._stop_adopted()
                return
            self._emit_output("[Niksnaks-Hosting] Server killed.\n")
            kill_pid(self._pid)
            self._finish_adopted()
            return
        if self._process:
            try:
                self._process.kill()
                self._process.wait(timeout=5)
            except Exception:
                pass
            self._pid = None
            self.status = ServerStatus.STOPPED
            self.player_count = 0
            self._emit_players_changed()
            self._emit_output("[Niksnaks-Hosting] Server killed.\n")

    def detach(self) -> None:
        """Let go of a server without stopping it.

        Nothing to do for a server on this computer: forgetting one of these is not
        something the app offers, and the process object is dropped along with the
        record. Overridden for a server on another machine, where threads of ours are
        following it over SSH and have to be told to stop looking.
        """
        return

    def send_command(self, command: str):
        cmd = command.strip()
        if cmd.startswith("/"):
            cmd = cmd[1:]
        if not cmd:
            return

        if self._adopted:
            self._write_inbox(cmd)
            return

        if not self._process or not self._process.stdin:
            return

        try:
            self._process.stdin.write(cmd + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _read_output(self):
        try:
            for line in iter(self._process.stdout.readline, ""):
                if not line:
                    break

                if self._status == ServerStatus.STARTING and self._is_ready_line(line):
                    self.status = ServerStatus.RUNNING

                self._update_player_count_from_output(line)

                self._emit_output(line)

        except Exception:
            pass
        finally:
            # Reaped here rather than left for whoever notices: the output ending means
            # the process has gone, and an unreaped child stays in the process table
            # looking for all the world like a server that is still running -- to this
            # app's own scan, and to a copy of it on another computer.
            try:
                if self._process is not None:
                    self._process.wait(timeout=10)
            except Exception:
                pass
            forget_running_server_pids()

            if self._status != ServerStatus.STOPPED:
                self._pid = None
                self.status = ServerStatus.STOPPED
                self.player_count = 0
                self._emit_players_changed()
                self._emit_output("[Niksnaks-Hosting] Server process ended.\n")

    def _emit_output(self, text: str):
        self.log_history.append(text)
        self._write_console_log(text)
        self.emit_on_main_thread("output-received", text)

    def _emit_players_changed(self):
        self.emit_on_main_thread("players-changed", self.player_count, self.max_players)

    # ------------------------------------------------------- the folder's console
    # A server folder carries its own console: console.log is what the server has said
    # this run, console.in is what anyone wants to say back. Keeping both on disk is
    # what lets a copy of this app on another computer read the console of a server
    # started here, and type into it -- the same two files a remote host's unit uses,
    # so neither side has to know which of them started the server.

    @property
    def _console_log_path(self) -> Path:
        return self.server_dir / CONSOLE_LOG_FILE

    @property
    def _console_in_path(self) -> Path:
        return self.server_dir / CONSOLE_IN_FILE

    def _open_console_share(self) -> None:
        """Start this run's transcript and open the folder's inbox for commands."""
        try:
            self.server_dir.mkdir(parents=True, exist_ok=True)
            # Truncated per run, exactly as the remote unit does it, so a reader that
            # attaches later sees this run rather than every run there has ever been.
            self._console_log = self._console_log_path.open(
                "w", encoding="utf-8", errors="replace", buffering=1
            )
        except OSError:
            self._console_log = None

        inbox = self._console_in_path
        try:
            # A FIFO left behind by a unit that used to run this folder would block the
            # plain open below forever, so whatever is there goes and a file replaces it.
            inbox.unlink(missing_ok=True)
            inbox.touch()
        except OSError:
            self._console_share_open = self._console_log is not None
            return

        self._console_share_open = True
        self._adopt_stop.clear()
        self._claim_folder()
        threading.Thread(target=self._read_inbox, daemon=True).start()

    def _close_console_share(self) -> None:
        self._adopt_stop.set()
        if not self._console_share_open:
            # Never opened one, so there is nothing here that is ours to close or
            # remove. An adopted server's files belong to whatever is running it.
            return
        self._console_share_open = False
        with self._console_lock:
            handle, self._console_log = self._console_log, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        try:
            self._console_in_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._release_folder()

    def _write_console_log(self, text: str) -> None:
        with self._console_lock:
            handle = self._console_log
            if handle is None:
                return
            try:
                handle.write(text)
            except (OSError, ValueError):
                # A full disk must not take the server down with it; the console on
                # screen carries on, only the copy other computers read stops.
                self._console_log = None

    def _read_inbox(self) -> None:
        """Type anything appended to console.in into the running server.

        This is the far side of the same channel a remote host offers: another computer
        appends a line to the file over SFTP, and it arrives on the server's stdin here.
        """
        path = self._console_in_path
        offset = 0
        pending = ""
        last_claim = time.monotonic()
        while not self._adopt_stop.wait(INBOX_POLL_SECONDS):
            # This thread is already awake on a timer, so keeping the folder's marker
            # fresh rides along on it rather than costing a thread of its own.
            if time.monotonic() - last_claim >= HEARTBEAT_SECONDS:
                last_claim = time.monotonic()
                self._claim_folder()
            try:
                size = path.stat().st_size
            except OSError:
                # Somebody removed it, so nothing more can arrive through it.
                return
            if size < offset:
                # A writer used > rather than >>; start again from the top.
                offset = 0
            if size == offset:
                continue
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
            except OSError:
                continue
            offset += len(chunk)
            pending += chunk.decode("utf-8", errors="replace")
            # Hold back a partial last line: an append caught mid-write is not a command.
            *lines, pending = pending.split("\n")
            for line in lines:
                text = line.strip()
                if not text:
                    continue
                # Echoed so this computer's console shows what the other one typed,
                # rather than only the server's answer to a command nobody here sent.
                self._emit_output(f"> {text}\n")
                self.send_command(text)

    def _write_inbox(self, line: str) -> None:
        """Hand a command to whatever is actually running the server in this folder.

        A unit written from another computer holds console.in open as the process's
        stdin, so an append lands straight in the server's console. A copy of this app
        running it as a child process is watching the same file and types it in.
        """
        path = self._console_in_path
        text = line.rstrip("\n") + "\n"
        try:
            if path.is_fifo():
                # Nothing is draining a FIFO with no reader, and a blocking open would
                # hang this thread for the rest of the run rather than fail.
                handle = os.open(str(path), os.O_WRONLY | os.O_NONBLOCK)
                try:
                    os.write(handle, text.encode("utf-8"))
                finally:
                    os.close(handle)
            else:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(text)
        except OSError as e:
            self._emit_output(f"[Niksnaks-Hosting] Could not send the command: {e}\n")

    def _claim_folder(self) -> None:
        """Say in the folder that this app is running the server and reading its inbox.

        Written where another computer can see it, because that is the only way one can
        tell an inbox nobody is watching from one that has simply not been read yet --
        and a stop sent into the first of those ends with a world being killed.
        """
        try:
            from niksnaks_hosting.shared.remote.ownership import claim

            claim(self.server_dir, pid=int(self._pid or 0))
        except Exception:
            pass

    def _release_folder(self) -> None:
        try:
            from niksnaks_hosting.shared.remote.ownership import release

            release(self.server_dir)
        except Exception:
            pass

    def _console_reaches_server(self) -> bool:
        """Whether a line put in this folder's inbox would actually reach the server.

        Three ways it can. A systemd unit written onto this machine from another computer
        holds the inbox open as the process's stdin, in which case it is a FIFO and an
        append goes straight in. A Scheduled Task written onto a Windows machine the same
        way runs a launcher that watches the file instead, and says so by keeping its pid
        in the folder. Otherwise a copy of this app is running the server itself and
        watching the file, which it says in the folder's owner marker.

        The last of those is the only one that goes stale, which is why it is asked last:
        an app that was closed leaves its marker behind for a few minutes, and taking
        that as an answer would have a stop wait out the whole grace period on a console
        nobody was reading and then kill the world.
        """
        try:
            if self._console_in_path.is_fifo():
                # A FIFO nobody is reading is one a stopped unit left behind, and a line
                # written into it would not go anywhere.
                return console_has_reader(self._console_in_path)
        except OSError:
            pass
        if launcher_is_watching(self.server_dir):
            return True
        try:
            from niksnaks_hosting.shared.remote.ownership import console_is_watched

            return console_is_watched(self.server_dir)
        except Exception:
            return False

    # ------------------------------------------------------------------- adoption
    def adopt_running_state(self) -> None:
        """Pick up a server already running in this folder that this app did not start.

        One folder can be driven from more than one place: another copy of this app on
        this machine, or a copy on another computer that started it here through a
        systemd unit. The process is on this machine either way and its console is in
        the folder, so there is no reason for this app to show it as switched off.
        """
        if not self._adopt_lock.acquire(blocking=False):
            return
        try:
            self._adopt_running_state()
        finally:
            self._adopt_lock.release()

    def _adopt_running_state(self) -> None:
        if self.is_running:
            return
        if self._process is not None and self._process.poll() is None:
            return
        pid = server_pid_in(self.server_dir)
        # A server this app can see the process of, or one it can only see the traces
        # of. The second is the ordinary case for a server started from another
        # computer: the unit runs it outside this app's sandbox, so there is no process
        # here to find and the folder is the only place the answer is written down.
        runner = "" if pid else console_is_live(self.server_dir)
        if not pid and not runner:
            return

        self._adopted = True
        self._pid = pid or None
        self._adopt_stop.clear()
        self.status = ServerStatus.RUNNING
        if pid:
            self._emit_output(
                f"[Niksnaks-Hosting] This server is already running here as process {pid}, "
                "started by something else. Reading its console from the server folder.\n"
            )
        else:
            self._emit_output(
                f"[Niksnaks-Hosting] This server is already running here, {runner}. "
                "Reading its console from the server folder, and it can be stopped "
                "from here.\n"
            )
        threading.Thread(target=self._follow_console_log, daemon=True).start()
        threading.Thread(target=self._watch_adopted_server, daemon=True).start()

    def refresh_state(self) -> None:
        """Ask this machine again whether something has started this server behind us.

        A running server is already being watched. A stopped one has nothing watching
        it, so without this a server started from another computer stays greyed out
        here until the app is next opened.
        """
        if self._status != ServerStatus.STOPPED:
            return
        self.adopt_running_state()

    def _follow_console_log(self) -> None:
        """Read the folder's transcript as it is written, for a server we did not start."""
        path = self._console_log_path
        try:
            offset = max(0, path.stat().st_size - ADOPT_TAIL_BYTES)
        except OSError:
            offset = 0
        skip_partial = offset > 0
        pending = ""
        while not self._adopt_stop.is_set():
            try:
                size = path.stat().st_size
            except OSError:
                # No transcript yet -- a Bedrock server started by an older build, say.
                # Keep looking: the folder is shared, and one may appear at any time.
                if self._adopt_stop.wait(INBOX_POLL_SECONDS):
                    return
                continue
            if size < offset:
                offset = 0
            if size > offset:
                try:
                    with path.open("rb") as handle:
                        handle.seek(offset)
                        chunk = handle.read()
                except OSError:
                    chunk = b""
                offset += len(chunk)
                text = chunk.decode("utf-8", errors="replace")
                if skip_partial:
                    # Starting at a byte offset lands mid-line; that half line is not
                    # worth showing as though the server had printed it.
                    skip_partial = False
                    _before, _sep, text = text.partition("\n")
                pending += text
                *lines, pending = pending.split("\n")
                for line in lines:
                    self._handle_adopted_line(line + "\n")
            if self._adopt_stop.wait(INBOX_POLL_SECONDS):
                return

    def _handle_adopted_line(self, line: str) -> None:
        self._update_player_count_from_output(line)
        self._emit_output(line)

    def _watch_adopted_server(self) -> None:
        """Notice an adopted server going away; there is no process object to wait on."""
        while not self._adopt_stop.wait(ADOPT_POLL_SECONDS):
            if not self._adopted_alive():
                self._emit_output("[Niksnaks-Hosting] The server stopped.\n")
                self._finish_adopted()
                return

    def _adopted_alive(self) -> bool:
        """Whether the server this app adopted is still up, however it was found."""
        if self._pid:
            return pid_alive(self._pid)
        return bool(console_is_live(self.server_dir))

    def _stop_adopted(self) -> None:
        """Stop a server this app did not start, by typing into the folder's console."""
        if not self._begin_adopted_stop():
            return
        threading.Thread(target=self._await_adopted_stop, daemon=True).start()

    def _begin_adopted_stop(self) -> bool:
        """Check the stop can even be delivered, and say it is on its way. False if not."""
        if not self._console_reaches_server():
            self._emit_output(
                "[Niksnaks-Hosting] Nothing is listening to this server's console, so a "
                "stop sent from here would not arrive. It was started by something that "
                "does not read the server folder -- an older Niksnaks-Hosting, or a "
                "script. Stop it where it was started, or use Force stop.\n"
            )
            return False

        self.status = ServerStatus.STOPPING
        self._emit_output("[Niksnaks-Hosting] Sending stop command...\n")
        return True

    def _await_adopted_stop(
        self,
        timeout: float = STOP_GRACE_SECONDS,
        on_wait: Callable[[float], None] | None = None,
    ) -> bool:
        """Send the stop and block until the world is on disk. Runs on a worker thread.

        Split out of _stop_adopted so that quitting, which has to know when the saving
        has actually finished before it takes a backup, can wait on the same sequence a
        button press fires and forgets.
        """
        pid = self._pid
        if not self.is_bedrock:
            self.send_command("save-all flush")
        self.send_command("stop")

        started = time.monotonic()
        deadline = started + max(1.0, float(timeout))
        while time.monotonic() < deadline and self._adopted_alive():
            if on_wait:
                on_wait(time.monotonic() - started)
            time.sleep(0.5)

        stopped_on_its_own = not self._adopted_alive()
        if not stopped_on_its_own:
            if pid:
                self._emit_output("[Niksnaks-Hosting] Server did not stop in time, killing...\n")
                kill_pid(pid)
            else:
                # Still up, and nothing here can force it. Saying so and putting the
                # row back to running is the honest end: marking it stopped would be
                # a lie, and leaving it stopping for good gives no way to try again.
                self._emit_output(
                    "[Niksnaks-Hosting] The server has not stopped, and there is no "
                    "process here to force it. Stop it on the computer it was "
                    "started from.\n"
                )
                self.status = ServerStatus.RUNNING
                return False
        self._emit_output("[Niksnaks-Hosting] Server stopped.\n")
        self._finish_adopted()
        return stopped_on_its_own

    def _stop_adopted_and_wait(
        self,
        timeout: float = STOP_GRACE_SECONDS,
        on_wait: Callable[[float], None] | None = None,
    ) -> bool:
        """What closing the app does about a server running here that it did not start.

        Which of two things depends on who is looking after it. A second copy of this
        app *on this computer* will save and back the server up when its own window
        closes, so this one leaves it alone. A server started from another computer over
        SSH has nobody here watching it -- the app at the far end leaves it running on
        purpose when it closes, because pulling a server down over a window closing on a
        laptop is not something anybody asked for -- and the world is on this machine, so
        this app is the last one that can write it out. That one is stopped and saved.
        """
        keeper = owner_on_this_machine(self.server_dir)
        if keeper is not None:
            self._emit_output(
                "[Niksnaks-Hosting] Leaving this server running: another copy of "
                f"Niksnaks-Hosting on this computer ({keeper.label}) is running it and "
                "will stop it when it closes.\n"
            )
            return True

        self._emit_output(
            "[Niksnaks-Hosting] This server was started from another computer, but it "
            "is running on this one -- saving and stopping it before closing.\n"
        )
        if not self._begin_adopted_stop():
            return False
        return self._await_adopted_stop(timeout=timeout, on_wait=on_wait)

    def _finish_adopted(self) -> None:
        self._adopt_stop.set()
        self._adopted = False
        self._pid = None
        self.player_count = 0
        self.status = ServerStatus.STOPPED
        self._emit_players_changed()


    def set_max_players(self, max_players: int):
        self.max_players = max(1, int(max_players))
        if self.player_count > self.max_players:
            self.player_count = self.max_players
        self._emit_players_changed()

    def _is_ready_line(self, line: str) -> bool:
        if self.is_bedrock:
            return "Server started." in line
        return "Done" in line and "For help" in line

    def _update_player_count_from_output(self, line: str):
        if self.is_bedrock:
            self._update_bedrock_player_count(line)
            return

        list_match = re.search(r"There are\s+(\d+)\s+of a max of\s+(\d+)\s+players online", line)
        if list_match:
            self.player_count = int(list_match.group(1))
            self.max_players = max(1, int(list_match.group(2)))
            self._emit_players_changed()
            return

        if " joined the game" in line:
            self.player_count = min(self.max_players, self.player_count + 1)
            self._emit_players_changed()
            return

        if " left the game" in line:
            self.player_count = max(0, self.player_count - 1)
            self._emit_players_changed()

    def _update_bedrock_player_count(self, line: str):
        list_match = re.search(r"There are\s+(\d+)/(\d+)\s+players online", line)
        if list_match:
            self.player_count = int(list_match.group(1))
            self.max_players = max(1, int(list_match.group(2)))
            self._emit_players_changed()
            return

        if "Player connected:" in line:
            self.player_count = min(self.max_players, self.player_count + 1)
            self._emit_players_changed()
            return

        if "Player disconnected:" in line:
            self.player_count = max(0, self.player_count - 1)
            self._emit_players_changed()
