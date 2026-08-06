import re
import subprocess
import threading
from pathlib import Path

from niksnaks_hosting.shared.backend.loader_launch import resolve_launch_args
from niksnaks_hosting.shared.core.events import EventEmitter
from niksnaks_hosting.shared.utils.constants import (
    EDITION_JAVA,
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
        self.log_history: list[str] = []

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str):
        if self._status != value:
            self._status = value
            if value == ServerStatus.STOPPED:
                self._release_memory_limit()
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

    def kill(self):
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

    def send_command(self, command: str):
        if not self._process or not self._process.stdin:
            return

        cmd = command.strip()
        if cmd.startswith("/"):
            cmd = cmd[1:]

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

            if self._status != ServerStatus.STOPPED:
                self._pid = None
                self.status = ServerStatus.STOPPED
                self.player_count = 0
                self._emit_players_changed()
                self._emit_output("[Niksnaks-Hosting] Server process ended.\n")

    def _emit_output(self, text: str):
        self.log_history.append(text)
        if len(self.log_history) > 1000:
            self.log_history.pop(0)
        self.emit_on_main_thread("output-received", text)

    def _emit_players_changed(self):
        self.emit_on_main_thread("players-changed", self.player_count, self.max_players)

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
