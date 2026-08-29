"""Keeping a server alive on a Windows host, where nothing else will.

A process started over SSH on Windows dies with the session: OpenSSH puts the whole
session in a job object and tears it down on disconnect. A Scheduled Task does not --
`schtasks /Run` hands the work to the Task Scheduler service, which is outside that job
object entirely, and an AtStartup trigger is the same switch as systemd's `enable`.

The three things that differ from the systemd side, and why:

* **No FIFO.** Console commands for a Java server go over RCON instead, tunnelled
  through the SSH connection that is already open (see rcon.py), which is better than a
  pipe anyway because the server answers back. Bedrock has no RCON, so for those the
  launcher watches console.in and feeds it to the process's stdin -- a poll where Linux
  gets a real FIFO, but the same idea and the same file name.
* **No ExecStop.** A task can be ended, but nothing runs on the way down, so a clean
  save has to be asked for over RCON first and only then enforced. The gap that leaves:
  a Windows *shutdown* kills the JVM without a save, and Minecraft's own autosave is the
  only thing standing between that and lost minutes.
* **No memory cap.** MemoryMax has no schtasks equivalent. -Xmx is the real limit
  anyway; it is simply not backed up by the operating system here.
"""

from __future__ import annotations

import ntpath
import secrets
import time
import zlib
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape as xml_escape

from niksnaks_hosting.shared.remote.host_ops import ps_quote
from niksnaks_hosting.shared.remote.remote_service import (
    CONSOLE_LOG,
    STOP_GRACE_SECONDS,
    UnitState,
    as_int,
    unit_name,
)
from niksnaks_hosting.shared.remote.ssh_client import SshError
from niksnaks_hosting.shared.utils.constants import LAUNCHER_PID_FILE

if TYPE_CHECKING:
    from niksnaks_hosting.shared.remote.host import SshHost

# A folder keeps `schtasks /Query` readable and makes this app's tasks removable as a set.
TASK_FOLDER = "Niksnaks-Hosting"

# Written by the launcher, holding the wrapper's pid and the JVM's. Its absence is how
# everything here knows the server is not running. Named alongside console.in and
# console.log, because a copy of this app on the host reads it out of the same folder to
# tell whether the launcher is still forwarding the inbox.
PID_FILE = LAUNCHER_PID_FILE
LAUNCHER = "niksnaks-run.ps1"

# The stand-in for a FIFO. The launcher watches it and forwards new lines into the
# running process, which is the only way to reach a Bedrock server's console on Windows.
STDIN_FILE = "console.in"
AUTOSTART_MARKER = "niksnaks-autostart"

# RCON is bound to loopback on the host and reached down the SSH connection, so the port
# only has to be free on that machine. It is derived from the server's own port, which is
# already unique per machine, and needs no bookkeeping of its own.
RCON_PORT_OFFSET = 10000
RCON_PORT_BASE = 25580
RCON_PORT_SPAN = 900

# How long to wait for the launcher to say which process it started.
START_TIMEOUT = 60.0


def win_quote(arg: str) -> str:
    """Quote one argument the way the Microsoft C runtime parses them back.

    Java's own argument parsing follows these rules, so this is what makes a modpack in
    "C:\\Program Files\\..." start rather than fail with a file-not-found on "C:\\Program".
    """
    text = str(arg)
    if text and not any(c in text for c in ' \t\n\v"'):
        return text

    out = ['"']
    backslashes = 0
    for char in text:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            # Backslashes are only special immediately before a quote, where each pair
            # becomes one and an odd one escapes the quote itself.
            out.append("\\" * (backslashes * 2 + 1))
            out.append('"')
        else:
            out.append("\\" * backslashes)
            out.append(char)
        backslashes = 0
    out.append("\\" * (backslashes * 2))
    out.append('"')
    return "".join(out)


class WindowsTaskService:
    """Runs one server as a Scheduled Task on a Windows host."""

    def __init__(self, host: SshHost, server_id: str, server_dir: str) -> None:
        self.host = host
        self.server_id = str(server_id)
        self.server_dir = str(server_dir)
        self.unit = unit_name(server_id)
        self.task = f"\\{TASK_FOLDER}\\{self.unit}"
        self._pids: tuple[int, int, str] = (0, 0, "")
        self._bedrock: bool | None = None
        self._warnings: list[str] = []

    # ------------------------------------------------------------------- plumbing
    @property
    def ops(self) -> Any:
        return self.host.ops

    @property
    def log_path(self) -> str:
        return f"{self.server_dir}/{CONSOLE_LOG}"

    @property
    def pid_path(self) -> str:
        return f"{self.server_dir}/{PID_FILE}"

    @property
    def launcher_path(self) -> str:
        return f"{self.server_dir}/{LAUNCHER}"

    @property
    def stdin_path(self) -> str:
        return f"{self.server_dir}/{STDIN_FILE}"

    @property
    def autostart_path(self) -> str:
        return f"{self.server_dir}/{AUTOSTART_MARKER}"

    @property
    def rcon_port(self) -> int:
        """A console port that no other server on this host will pick.

        Derived from the server's own port, which the app already keeps unique per
        machine -- two servers sharing one would not both start anyway. A hash of the
        server id looks tidier and is wrong: fifty servers in a thousand-port window
        collide about three quarters of the time, and a collision here is a console that
        silently stops working on whichever server came up second.
        """
        server_port = self._server_port()
        if server_port:
            offset = server_port + RCON_PORT_OFFSET
            return offset if offset < 65536 else server_port - RCON_PORT_OFFSET
        # No properties file yet: fall back to something stable for this server.
        return RCON_PORT_BASE + zlib.crc32(self.server_id.encode("utf-8")) % RCON_PORT_SPAN

    def _server_port(self) -> int:
        from niksnaks_hosting.shared.backend.config_manager import ConfigManager

        try:
            config = ConfigManager(self.host.path(self.server_dir))
            config.load()
            return config.get_int("server-port", 0)
        except Exception:
            return 0

    def _schtasks(self, *args: str, timeout: float = 60.0):
        return self.host.run(["schtasks", *args], timeout=timeout)

    # --------------------------------------------------------------- installation
    def write_unit(self, description: str, exec_args: list[str], ram_mb: int) -> None:
        """Describe the server to the Task Scheduler, replacing whatever was there.

        ram_mb is deliberately unused: -Xmx is already in exec_args, and a Scheduled Task
        has nothing corresponding to systemd's MemoryMax. Capping it here would mean
        lying about a limit that is not being enforced.
        """
        self._bedrock = None
        self._ensure_rcon()
        self._write_launcher(exec_args)

        at_startup = self.host.path(self.autostart_path).exists()
        xml = self._task_xml(description or self.unit, at_startup)
        xml_path = f"{self.ops.script_dir(self.host)}/{self.unit}.xml"
        self.host.path(xml_path).parent.mkdir(parents=True, exist_ok=True)
        # schtasks reads a task definition as UTF-16, and answers a UTF-8 one with a
        # parse error that names nothing useful.
        self.host.path(xml_path).write_bytes(b"\xff\xfe" + xml.encode("utf-16-le"))

        try:
            result = self._schtasks("/Create", "/TN", self.task, "/XML", self.ops.to_native(xml_path), "/F")
            if not result.ok:
                # S4U lets a task start without anyone being logged in, but registering
                # one needs a privilege some accounts do not have. Falling back to an
                # interactive token still works, just only while the user is signed in.
                xml = self._task_xml(description or self.unit, at_startup, interactive=True)
                self.host.path(xml_path).write_bytes(b"\xff\xfe" + xml.encode("utf-16-le"))
                result = self._schtasks(
                    "/Create", "/TN", self.task, "/XML", self.ops.to_native(xml_path), "/F"
                )
            if not result.ok:
                raise SshError(
                    _("Could not set up the server task on {}: {}").format(
                        self.host.name, result.output or _("exit status {}").format(result.exit_status)
                    )
                )
        finally:
            try:
                self.host.path(xml_path).unlink(missing_ok=True)
            except Exception:
                pass

    def remove_unit(self) -> None:
        try:
            self._schtasks("/End", "/TN", self.task, timeout=STOP_GRACE_SECONDS + 30)
        except Exception:
            pass
        try:
            self._schtasks("/Delete", "/TN", self.task, "/F")
        except Exception:
            pass

    def enable_autostart(self, enabled: bool, description: str = "") -> None:
        marker = self.host.path(self.autostart_path)
        if enabled:
            marker.write_text("1")
        else:
            marker.unlink(missing_ok=True)
        # The trigger lives in the task definition, so the task has to be written again
        # in full -- and without the caller's description, that rewrite would overwrite
        # whatever write_unit last put there with this.unit's raw id.
        if self.host.path(self.launcher_path).is_file():
            xml = self._task_xml(description or self.unit, enabled)
            xml_path = f"{self.ops.script_dir(self.host)}/{self.unit}.xml"
            self.host.path(xml_path).parent.mkdir(parents=True, exist_ok=True)
            self.host.path(xml_path).write_bytes(b"\xff\xfe" + xml.encode("utf-16-le"))
            self._schtasks("/Create", "/TN", self.task, "/XML", self.ops.to_native(xml_path), "/F")
            self.host.path(xml_path).unlink(missing_ok=True)

    # -------------------------------------------------------------------- control
    def start(self) -> None:
        # The caller's own idea of whether this is running can be wrong -- a missed
        # poll, an app restart that never got to adopt it -- and MultipleInstancesPolicy
        # means schtasks /Run silently does nothing for a task that is already going. If
        # both of those happen together, wiping the pid file first is exactly what turns
        # a live server invisible: nothing rewrites it, and the JVM is still running with
        # no evidence left pointing at it. Trust the task itself before touching anything.
        if self.state().active:
            return
        self.host.path(self.log_path).write_text("")
        self.host.path(self.stdin_path).write_text("")
        self.host.path(self.pid_path).unlink(missing_ok=True)
        self._pids = (0, 0, "")
        self._schtasks("/Run", "/TN", self.task).check(
            f"Could not start {self.unit} on {self.host.name}"
        )

        # The task starts the launcher, which starts the JVM and only then writes down
        # what it started. Nothing above should have to know that took a moment.
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self._read_pids()[1]:
                return
            time.sleep(0.5)

    def stop(self) -> None:
        """Ask for a clean shutdown first, and only then insist.

        There is no ExecStop here, so the save has to be requested over the console and
        waited for. Ending the task straight away would be the same as pulling the plug.
        """
        asked = False
        try:
            self.send_console("stop")
            asked = True
        except Exception:
            pass

        if asked:
            deadline = time.monotonic() + STOP_GRACE_SECONDS
            while time.monotonic() < deadline:
                if not self.state().active:
                    self._end_task()
                    return
                time.sleep(2.0)

        self.kill()

    def kill(self) -> None:
        self._end_task()
        wrapper, java, _name = self._read_pids()
        for pid in (java, wrapper):
            if pid:
                try:
                    self.host.run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=60)
                except Exception:
                    pass
        try:
            self.host.path(self.pid_path).unlink(missing_ok=True)
        except Exception:
            pass
        self._pids = (0, 0, "")

    def kill_pid(self, pid: int) -> None:
        self.host.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], timeout=60)

    def take_warnings(self) -> list[str]:
        warnings, self._warnings = self._warnings, []
        return warnings

    def _end_task(self) -> None:
        try:
            self._schtasks("/End", "/TN", self.task, timeout=60)
        except Exception:
            pass

    def send_console(self, line: str) -> None:
        """Type a line into the server, whichever way this one listens.

        RCON is preferred for a Java server: it is immediate and it answers back. Bedrock
        does not speak it at all, so for those -- and whenever RCON is not up yet, which
        includes an adopted server on its first run -- the line goes into the file the
        launcher is feeding to the process's stdin.
        """
        text = line.strip()
        if not text:
            return

        if not self._is_bedrock():
            from niksnaks_hosting.shared.remote.rcon import send_rcon

            port, password = self._rcon_settings()
            if password:
                try:
                    send_rcon(self.host, port, password, text)
                    return
                except Exception:
                    # Fall through: the file works even when the console port does not.
                    pass

        self._append_stdin(text)

    def _append_stdin(self, text: str) -> None:
        """Add a line to the file the launcher forwards into the running process.

        Appending rather than rewriting: the launcher tracks how far it has read, so a
        second command sent before the first was picked up must not overwrite it.
        """
        path = self.host.path(self.stdin_path)
        try:
            existing = path.read_bytes()
        except Exception:
            existing = b""
        if existing and not existing.endswith(b"\n"):
            existing += b"\n"
        path.write_bytes(existing + text.encode("utf-8") + b"\n")

    def send_console_file(self, line: str) -> None:
        """Type into a server in this folder that this task did not start.

        Straight to the inbox, never RCON: the console port belongs to a server this
        task launched and configured, and a process something else started is not
        listening on it -- but it is watching this file, because it is the same file
        this app's launcher would be watching.
        """
        text = line.strip()
        if text:
            self._append_stdin(text)

    def pid_alive(self, pid: int) -> bool:
        try:
            return self._tasklist(int(pid)) is not None
        except Exception:
            # Losing the link is not evidence the process has gone.
            return True

    def _is_bedrock(self) -> bool:
        """Whether this folder holds a Bedrock server, which has no RCON to speak to."""
        if self._bedrock is None:
            try:
                self._bedrock = self.host.path(f"{self.server_dir}/bedrock_server.exe").is_file()
            except Exception:
                return False
        return bool(self._bedrock)

    # ---------------------------------------------------------------------- state
    def state(self) -> UnitState:
        wrapper, java, expected = self._read_pids()
        if not java:
            return UnitState(active=False, status="dead")

        row = self._tasklist(java)
        if row is None:
            # The pid file is stale: the JVM is gone and the launcher never got to
            # clean up, which is what a crash looks like.
            return UnitState(active=False, status="dead", pid=0)

        name, memory, cpu = row
        if not self._is_ours(name, expected):
            # A recycled pid must not be reported as the server. "Notepad is using 4GB"
            # is a confusing way to find out a server died.
            return UnitState(active=False, status="dead", pid=0)

        return UnitState(
            active=True,
            status="running",
            pid=java or wrapper,
            memory_bytes=memory,
            cpu_seconds=cpu,
        )

    def follow_console(self, tail_lines: int = 0) -> Any:
        """Stream the console log, replaying what is already in it first.

        Get-Content -Wait prints the file and then keeps pace with it, which is what
        `tail -F` does on the other side. With no *tail_lines* the whole file goes out,
        which is right after a start because the log is truncated there; picking up a
        server that was already running asks for a screenful of a file that may be days
        long instead.
        """
        self.ops.sweep_scripts(self.host)
        window = f" -Tail {int(tail_lines)}" if tail_lines else ""
        body = f"""$log = {ps_quote(self.ops.to_native(self.log_path))}
for ($i = 0; $i -lt 60; $i++) {{
    if (Test-Path -LiteralPath $log) {{ break }}
    Start-Sleep -Milliseconds 500
}}
Get-Content -LiteralPath $log -Encoding utf8{window} -Wait"""
        return self.ops.open_script_stream(self.host, body)

    def read_console_tail(self, lines: int = 1000) -> str:
        # SystemdService does this with `tail -n`, on the host; pulling the whole log
        # over SFTP just to throw away everything but the last N lines locally is the
        # same round trip paying for every byte a long-running server ever printed.
        body = f"""$log = {ps_quote(self.ops.to_native(self.log_path))}
if (Test-Path -LiteralPath $log) {{
    Get-Content -LiteralPath $log -Encoding utf8 -Tail {int(lines)}
}}"""
        try:
            result = self.ops.run_script(self.host, body, timeout=30)
        except Exception:
            return ""
        return result.stdout

    def foreign_process_pid(self) -> int:
        """A JVM in this folder that this task did not start.

        Windows does not expose a process's working directory to another process, so this
        matches on the command line instead -- which catches the far machine's own copy
        of this app when it names the folder, and misses one that started the server with
        purely relative arguments. It is a guard, not a guarantee.
        """
        _wrapper, mine, _name = self._read_pids()
        needle = self.ops.to_native(self.server_dir)
        body = f"""$needle = {ps_quote(needle)}
$rows = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {{ $_.Name -match '^(java|javaw|bedrock_server)' }} |
    Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains($needle) }})
foreach ($row in $rows) {{ Write-Output $row.ProcessId }}"""
        try:
            result = self.ops.run_script(self.host, body, timeout=45)
        except Exception:
            return 0
        for token in result.stdout.split():
            pid = as_int(token)
            if pid and pid != mine:
                return pid
        return 0

    # ------------------------------------------------------------------- internals
    def _read_pids(self) -> tuple[int, int, str]:
        """What the launcher wrote down: the wrapper, the JVM, and what the JVM is called."""
        try:
            raw = self.host.path(self.pid_path).read_text(errors="replace")
        except Exception:
            return (0, 0, "")
        parts = raw.split()
        wrapper = as_int(parts[0]) if parts else 0
        java = as_int(parts[1]) if len(parts) > 1 else 0
        name = parts[2] if len(parts) > 2 else ""
        self._pids = (wrapper, java, name)
        return self._pids

    @staticmethod
    def _is_ours(running: str, expected: str) -> bool:
        """Whether the process holding that pid is still the one that was started."""
        if expected:
            return running.strip().lower() == expected.strip().lower()
        # Written by an older launcher, before the name was recorded: fall back to what
        # a Minecraft server is normally called.
        return any(running.lower().startswith(known) for known in ("java", "bedrock_server"))

    def _tasklist(self, pid: int) -> tuple[str, int, float] | None:
        """One process, asked for without spending a PowerShell start-up on it.

        state() is polled every few seconds per running server, so this path stays a
        plain command line. Only digits are interpolated into it.
        """
        result = self.host.connection.run(
            f'cmd /c tasklist /FI "PID eq {int(pid)}" /FO CSV /NH /V', timeout=30
        )
        for line in result.stdout.splitlines():
            row = _split_csv(line.strip())
            if len(row) < 8 or as_int(row[1]) != int(pid):
                continue
            # Thousands separators are whatever the host's locale uses, so only the
            # digits are trustworthy.
            memory_kb = as_int("".join(c for c in row[4] if c.isdigit()))
            return row[0], memory_kb * 1024, _hms_seconds(row[7])
        return None

    def _rcon_settings(self) -> tuple[int, str]:
        from niksnaks_hosting.shared.backend.config_manager import ConfigManager

        config = ConfigManager(self.host.path(self.server_dir))
        config.load()
        port = config.get_int("rcon.port", self.rcon_port)
        return port or self.rcon_port, config.get("rcon.password", "")

    def _ensure_rcon(self) -> None:
        """Turn the console on, once, before the server is next started.

        An adopted server was created without this, so it only gains a working console
        after one restart -- which is the moment this runs.
        """
        from niksnaks_hosting.shared.backend.config_manager import ConfigManager

        if self._is_bedrock():
            return

        try:
            config = ConfigManager(self.host.path(self.server_dir))
            config.load()
            changed = False
            if not config.get_bool("enable-rcon", False):
                config.set_value("enable-rcon", "true")
                changed = True
            if not config.get_int("rcon.port", 0):
                config.set_value("rcon.port", str(self.rcon_port))
                changed = True
            if not config.get("rcon.password", ""):
                config.set_value("rcon.password", secrets.token_hex(16))
                changed = True
            if not config.get("broadcast-rcon-to-ops", ""):
                # Otherwise every command this app sends is announced to everyone
                # playing, which is startling rather than useful.
                config.set_value("broadcast-rcon-to-ops", "false")
                changed = True
            if changed:
                config.save()
            self._restrict_rcon_to_loopback(config.get_int("rcon.port", 0) or self.rcon_port)
        except Exception:
            # A server whose properties file does not exist yet is written by the first
            # run; the console simply starts working the time after that.
            pass

    def _restrict_rcon_to_loopback(self, port: int) -> None:
        """Block the RCON port from anything that is not this machine.

        Turning RCON on opens a console port. This app only ever reaches it through the
        SSH connection, which arrives at the server as a connection from 127.0.0.1, but
        that says nothing about who else can reach it: RCON is served on whatever
        address the server itself is bound to, and that has to stay open or players
        could not connect. So the bind address cannot be narrowed without taking the
        server off the network, and a firewall rule is the lever that is left. Windows
        does not evaluate loopback traffic against firewall rules at all, so a rule
        blocking the port from every remote address shuts out the network while leaving
        the SSH tunnel's own connection untouched.

        Adding a rule needs Administrator, which the account this app logs in as often
        is not. That failure is recorded rather than swallowed: the password guarding
        the port is 128 bits of randomness, so an exposed port is not an emergency, but
        it is not something to find out about by accident either.
        """
        if not port:
            return
        name = f"Niksnaks-Hosting-RCON-{self.server_id}"
        # -ErrorAction Stop, so a denied rule raises rather than being reported as
        # success by a cmdlet that writes a non-terminating error and carries on.
        script = f"""$name = {ps_quote(name)}
Remove-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Out-Null
New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Block -Protocol TCP `
    -LocalPort {int(port)} -RemoteAddress Any -ErrorAction Stop | Out-Null"""
        try:
            ok = self.ops.run_script(self.host, script, timeout=30).ok
        except Exception:
            ok = False
        if not ok:
            self._warnings.append(
                _(
                    "The remote console port {port} on {host} could not be closed to the "
                    "network -- that needs an administrator account. It is guarded by a "
                    "long random password, but if {host} is reachable from the internet, "
                    "consider blocking port {port} on it yourself."
                ).format(port=int(port), host=self.host.name)
            )

    def _write_launcher(self, exec_args: list[str]) -> None:
        """The script the task actually runs.

        cmd does the redirection rather than .NET, which is not laziness: two async
        stream handlers appending to one file interleave stdout and stderr in whatever
        order the callbacks happen to fire, and a stack trace shuffled into the log
        around the lines it belongs to is worse than useless.

        The script also stands in for the FIFO the Linux side gets from systemd: it
        watches console.in and feeds new lines to the process's stdin. Java servers are
        normally driven over RCON instead, but Bedrock has no RCON at all, and this is
        the only way to say "stop" to one on Windows and have the world saved.
        """
        native_dir = self.ops.to_native(self.server_dir)
        native_log = self.ops.to_native(self.log_path)
        native_pid = self.ops.to_native(self.pid_path)
        native_stdin = self.ops.to_native(self.stdin_path)

        inner = " ".join(win_quote(a) for a in exec_args) + f" >> {win_quote(native_log)} 2>&1"

        script = f"""$ErrorActionPreference = 'Continue'
$dir = {ps_quote(native_dir)}
$pidFile = {ps_quote(native_pid)}
$inFile = {ps_quote(native_stdin)}
Set-Location -LiteralPath $dir

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $env:ComSpec
# /s /c makes cmd strip exactly the outer pair of quotes, which is the only form that
# survives a command line where the program path is itself quoted.
$psi.Arguments = '/s /c "' + {ps_quote(inner)} + '"'
$psi.WorkingDirectory = $dir
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardInput = $true

$proc = [System.Diagnostics.Process]::Start($psi)

$wanted = {ps_quote(ntpath.basename(self.ops.to_native(exec_args[0])))}
$javaPid = 0
$javaName = ''
for ($i = 0; $i -lt 150; $i++) {{
    if ($proc.HasExited) {{ break }}
    try {{
        $kids = @(Get-CimInstance Win32_Process -Filter ("ParentProcessId=" + $proc.Id) -ErrorAction SilentlyContinue)
    }} catch {{
        $kids = @()
    }}
    # Not simply the first child: cmd starts conhost.exe alongside a console program,
    # and taking whichever turned up first would have this app watching the console
    # window's host process instead of the server -- intermittently, which is worse.
    $match = @($kids | Where-Object {{ $_.Name -ieq $wanted }})
    if ($match.Count -eq 0) {{
        $match = @($kids | Where-Object {{ $_.Name -ine 'conhost.exe' }})
    }}
    if ($match.Count -gt 0) {{
        $javaPid = $match[0].ProcessId
        $javaName = $match[0].Name
        break
    }}
    Start-Sleep -Milliseconds 200
}}

# The image name goes in the file too. Windows hands pid numbers back out quickly, and
# without something to compare against, a recycled pid would have this app reporting
# whatever program inherited it as the server. Recording what was actually started beats
# assuming it must have been called java.
$record = $proc.Id.ToString() + ' ' + $javaPid.ToString() + ' ' + $javaName
Set-Content -LiteralPath $pidFile -Value $record -Encoding ascii -Force

# Instead of WaitForExit: watch console.in and forward whatever appears to the process.
# The file is opened with FileShare.ReadWrite every pass so the app can keep appending
# to it while this holds it open, and $offset is how much has already been sent -- a
# command must not be delivered twice because the file still contains it.
$offset = 0
while (-not $proc.HasExited) {{
    try {{
        $share = [IO.FileShare]::ReadWrite
        $stream = [IO.File]::Open($inFile, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::Read, $share)
        try {{
            # start() empties the file, so a shorter file means a new run, not a rewind.
            if ($stream.Length -lt $offset) {{ $offset = 0 }}
            if ($stream.Length -gt $offset) {{
                $stream.Position = $offset
                $reader = New-Object IO.StreamReader($stream)
                $text = $reader.ReadToEnd()
                $offset = $stream.Length
                foreach ($line in ($text -split "`n")) {{
                    $typed = $line.TrimEnd("`r")
                    if ($typed -ne '') {{ $proc.StandardInput.WriteLine($typed) }}
                }}
                $proc.StandardInput.Flush()
            }}
        }} finally {{
            $stream.Dispose()
        }}
    }} catch {{
        # A locked or missing file is a reason to try again in a moment, never a reason
        # to stop watching: the server itself is perfectly healthy.
    }}
    Start-Sleep -Milliseconds 250
}}

$proc.WaitForExit()
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
exit $proc.ExitCode
"""
        self.host.path(self.launcher_path).write_bytes(b"\xef\xbb\xbf" + script.encode("utf-8"))

    def _task_xml(self, description: str, at_startup: bool, interactive: bool = False) -> str:
        account = self.ops.account(self.host)
        arguments = (
            "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "
            f'"{self.ops.to_native(self.launcher_path)}"'
        )
        trigger = "<BootTrigger><Enabled>true</Enabled></BootTrigger>" if at_startup else ""
        logon = "InteractiveToken" if interactive else "S4U"

        # ExecutionTimeLimit PT0S means "no limit"; the default is three days, after
        # which the Task Scheduler would quietly kill a server that had been up too long.
        return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>Niksnaks-Hosting</Author>
    <Description>{xml_escape(description)}</Description>
  </RegistrationInfo>
  <Triggers>{trigger}</Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{xml_escape(account)}</UserId>
      <LogonType>{logon}</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>{xml_escape(arguments)}</Arguments>
      <WorkingDirectory>{xml_escape(self.ops.to_native(self.server_dir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _split_csv(line: str) -> list[str]:
    """tasklist /FO CSV, which quotes every field and escapes nothing else."""
    fields: list[str] = []
    current: list[str] = []
    in_quotes = False
    for char in line:
        if char == '"':
            in_quotes = not in_quotes
        elif char == "," and not in_quotes:
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def _hms_seconds(value: str) -> float:
    """tasklist reports CPU time as 0:01:23, and as N/A when it does not know."""
    parts = value.strip().split(":")
    if len(parts) != 3:
        return 0.0
    try:
        hours, minutes, seconds = (float(p) for p in parts)
    except ValueError:
        return 0.0
    return hours * 3600 + minutes * 60 + seconds
