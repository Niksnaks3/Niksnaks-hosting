from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from niksnaks_hosting.shared.remote.remote_service import (
    CONSOLE_IN,
    CONSOLE_LOG,
    STOP_GRACE_SECONDS,
    UNIT_PREFIX,
    UnitState,
    as_int,
    unit_name,
)
from niksnaks_hosting.shared.remote.ssh_client import CommandResult

if TYPE_CHECKING:
    from niksnaks_hosting.shared.remote.host import SshHost

# Re-exported so nothing outside had to learn that these moved.
__all__ = [
    "CONSOLE_LOG",
    "STDIN_FIFO",
    "STOP_GRACE_SECONDS",
    "UNIT_PREFIX",
    "SystemdService",
    "UnitState",
    "ensure_user_manager",
    "unit_name",
]

# Minecraft answers "stop" on stdin, and systemd cannot type it. The unit keeps a FIFO
# open as the process's stdin so the app can push console commands into it, and stopping
# writes "stop" there rather than sending a signal, so the world is saved on the way out.
# The same name a server started by a copy of this app uses for its inbox, so appending
# to it reaches the server either way -- straight down its stdin when a unit of ours is
# holding the FIFO, and via the app that started it when it is a plain file.
STDIN_FIFO = CONSOLE_IN


class SystemdService:
    """Runs one server as a systemd user service on a remote host.

    A user service is what lets a server outlive the SSH session that started it: the
    app connects, asks systemd to start the unit, and can disconnect entirely while the
    server keeps running. Lingering has to be enabled for the account, or systemd tears
    the user's services down at logout.
    """

    def __init__(self, host: SshHost, server_id: str, server_dir: str) -> None:
        self.host = host
        self.server_id = server_id
        self.server_dir = str(server_dir)
        self.unit = unit_name(server_id)

    # ------------------------------------------------------------------- plumbing
    @property
    def unit_path(self) -> str:
        return f"{self.host.home}/.config/systemd/user/{self.unit}.service"

    @property
    def fifo_path(self) -> str:
        return f"{self.server_dir}/{STDIN_FIFO}"

    @property
    def log_path(self) -> str:
        return f"{self.server_dir}/{CONSOLE_LOG}"

    def _systemctl(self, *args: str, timeout: float = 60.0) -> CommandResult:
        return self.host.run(["systemctl", "--user", *args], timeout=timeout)

    # --------------------------------------------------------------- installation
    def write_unit(self, description: str, exec_args: list[str], ram_mb: int) -> None:
        """Write (or rewrite) the unit file and reload systemd so it takes effect."""
        command = shlex.join(exec_args)

        # sh -c holds the FIFO open for writing on a spare descriptor. Without that,
        # the server would see end-of-file the moment the last console writer left and
        # would treat it as the console closing.
        wrapped = (
            f"exec 3<> {shlex.quote(self.fifo_path)}; "
            f"exec {command} <&3 >> {shlex.quote(self.log_path)} 2>&1"
        )

        unit_text = _escape_specifiers(f"""[Unit]
Description={description}
After=network-online.target

[Service]
Type=simple
WorkingDirectory={self.server_dir}
ExecStartPre=/bin/sh -c 'rm -f {shlex.quote(self.fifo_path)}; mkfifo -m 600 {shlex.quote(self.fifo_path)}'
ExecStart=/bin/sh -c {shlex.quote(wrapped)}
ExecStop=/bin/sh -c {shlex.quote(f'echo stop > {shlex.quote(self.fifo_path)}')}
TimeoutStopSec={STOP_GRACE_SECONDS}
KillMode=mixed
Restart=no
MemoryMax={max(256, int(ram_mb) + 512)}M

[Install]
WantedBy=default.target
""")
        unit_file = self.host.path(self.unit_path)
        unit_file.parent.mkdir(parents=True, exist_ok=True)
        unit_file.write_text(unit_text)
        self._systemctl("daemon-reload").check(f"Could not reload systemd on {self.host.name}")

    def remove_unit(self) -> None:
        self._systemctl("disable", "--now", f"{self.unit}.service", timeout=STOP_GRACE_SECONDS + 30)
        self.host.path(self.unit_path).unlink(missing_ok=True)
        self._systemctl("daemon-reload")

    def enable_autostart(self, enabled: bool, description: str = "") -> None:
        # The unit file already has its description; enabling a unit never rewrites it.
        self._systemctl("enable" if enabled else "disable", f"{self.unit}.service")

    # -------------------------------------------------------------------- control
    def start(self) -> None:
        self.host.path(self.log_path).write_text("")
        self._systemctl("start", f"{self.unit}.service").check(
            f"Could not start {self.unit} on {self.host.name}"
        )

    def stop(self) -> None:
        # ExecStop types "stop" into the console, so systemd waits for a clean save
        # rather than killing a server mid-write.
        self._systemctl("stop", f"{self.unit}.service", timeout=STOP_GRACE_SECONDS + 30)

    def kill(self) -> None:
        self._systemctl("kill", "--signal=SIGKILL", f"{self.unit}.service")

    def kill_pid(self, pid: int) -> None:
        self.host.run(["kill", "-9", str(int(pid))], timeout=30)

    def take_warnings(self) -> list[str]:
        # Writing a unit needs nothing this account might lack, so there is never
        # anything to report here.
        return []

    def send_console(self, line: str) -> None:
        """Type a line into the running server's console.

        Appended rather than written over: for the FIFO one of our own units holds open
        the two are the same thing, but for the plain file a copy of this app uses when
        it runs the server itself, overwriting would drop a command sent a moment ago
        that had not been picked up yet.
        """
        text = line.rstrip("\n") + "\n"
        self.host.run_shell(
            f"printf %s {shlex.quote(text)} >> {shlex.quote(self.fifo_path)}",
            timeout=20,
        )

    def send_console_file(self, line: str) -> None:
        # One inbox, whoever is running the server: there is nothing to do differently.
        self.send_console(line)

    def pid_alive(self, pid: int) -> bool:
        """Whether a process on the host is still doing anything.

        A zombie is deliberately not alive, which rules out `kill -0`: a server started
        by the far machine's own copy of this app is that app's child and stays in the
        process table until it is reaped, so a stop would go on waiting long after the
        world had been written out and the process had exited.
        """
        script = (
            f"state=$(ps -o stat= -p {int(pid)} 2>/dev/null | head -n1); "
            '[ -n "$state" ] || exit 1; '
            'case "$state" in Z*) exit 1 ;; esac; '
            "exit 0"
        )
        try:
            return self.host.run_shell(script, timeout=20).ok
        except Exception:
            # No answer is not evidence the process has gone, and saying it has would
            # have the app report a running server as stopped every time the link blips.
            return True

    # ---------------------------------------------------------------------- state
    def state(self) -> UnitState:
        result = self._systemctl(
            "show",
            f"{self.unit}.service",
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--property=MemoryCurrent",
            "--property=CPUUsageNSec",
            timeout=30,
        )
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, _sep, value = line.partition("=")
            values[key.strip()] = value.strip()

        active = values.get("ActiveState", "") == "active"
        return UnitState(
            active=active,
            status=values.get("SubState", values.get("ActiveState", "unknown")),
            pid=as_int(values.get("MainPID")),
            memory_bytes=as_int(values.get("MemoryCurrent")),
            cpu_seconds=as_int(values.get("CPUUsageNSec")) / 1_000_000_000,
        )

    def follow_console(self, tail_lines: int = 0):
        """A channel streaming the console log as it is written.

        With no *tail_lines* this starts from the top of the file, which is right after
        a start: the log is truncated there, so the whole file is this run. Picking up a
        server that was already going asks for a screenful instead, because that file
        can be days long and none of it has been shown yet.
        """
        where = f"-n {int(tail_lines)}" if tail_lines else "-n +1"
        return self.host.connection.open_stream(
            f"tail {where} -F {shlex.quote(self.log_path)} 2>/dev/null"
        )

    def read_console_tail(self, lines: int = 1000) -> str:
        result = self.host.run(["tail", "-n", str(int(lines)), self.log_path], timeout=30)
        return result.stdout

    def foreign_process_pid(self) -> int:
        """A JVM already running out of this folder that this unit did not start.

        The far machine may have its own copy of this app, which starts servers as plain
        child processes systemd knows nothing about. Matching on the working directory
        rather than the command line catches those too, since the folder is the thing
        that must not be opened twice.
        """
        mine = self.state().pid
        script = (
            f"dir=$(readlink -f {shlex.quote(self.server_dir)} 2>/dev/null); "
            '[ -n "$dir" ] || exit 0; '
            'for p in /proc/[0-9]*; do '
            'pid=${p#/proc/}; '
            '[ -r "$p/cwd" ] || continue; '
            'case "$(readlink "$p/cwd" 2>/dev/null)" in '
            '"$dir") ;; *) continue ;; esac; '
            'case "$(tr "\\0" " " < "$p/cmdline" 2>/dev/null)" in '
            '*java*|*bedrock_server*) printf "%s\\n" "$pid" ;; '
            'esac; '
            "done"
        )
        try:
            result = self.host.run_shell(script, timeout=30)
        except Exception:
            return 0
        for token in result.stdout.split():
            pid = as_int(token)
            if pid and pid != mine:
                return pid
        return 0


def ensure_user_manager(host: SshHost) -> tuple[bool, str]:
    """Check the account can run user services that survive logout.

    Without lingering, systemd stops every user service the moment the SSH session
    ends, which would kill a server the instant the app disconnects.
    """
    if not host.which("systemctl"):
        return False, _("{} has no systemd, so servers cannot be kept running there.").format(host.name)

    probe = host.run(["systemctl", "--user", "is-system-running"], timeout=30)
    if probe.exit_status == 127 or "Failed to connect" in probe.output:
        return False, _(
            "{} has systemd but no user session for this account. Ask an administrator to run "
            "'loginctl enable-linger {}' on it."
        ).format(host.name, host.credentials.username or "youruser")

    linger = host.run(["loginctl", "show-user", host.credentials.username or "", "--property=Linger"], timeout=30)
    if "Linger=no" in linger.stdout:
        enabled = host.run(["loginctl", "enable-linger"], timeout=30)
        if not enabled.ok:
            return False, _(
                "Servers on {} would stop as soon as you disconnect. Run "
                "'loginctl enable-linger {}' on it, or ask an administrator to."
            ).format(host.name, host.credentials.username or "youruser")
    return True, ""


def _escape_specifiers(unit_text: str) -> str:
    """systemd expands %h, %i and friends in unit files, so a literal % has to be doubled.

    A server folder or Java path is free to contain a percent sign, and an unescaped one
    would silently turn into something else entirely -- or refuse to load the unit.
    """
    body = unit_text.replace("%", "%%")
    # Only values are escaped; there are no specifiers in the section headers or keys.
    return body
