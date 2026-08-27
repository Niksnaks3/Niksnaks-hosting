"""What a host understands, so nothing above has to know which kind it is.

SshHost objects are built at startup from hosts.json, before any connection exists --
the app has to boot with hosts that are switched off. So the far machine's operating
system cannot be known at construction time, which rules out subclassing SshHost. A
strategy object is swapped in on first connect instead, and every place that used to
build a POSIX command inline asks it instead.

The rule for paths: RemotePath is always POSIX, because SFTP is POSIX even on Windows --
OpenSSH's SFTP server answers /C:/Users/nik for a home directory. to_native() is applied
only where a path enters a command line, which is a small and enumerable set.
"""

from __future__ import annotations

import re
import shlex
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from niksnaks_hosting.shared.remote.remote_service import RemoteService
from niksnaks_hosting.shared.remote.ssh_client import CommandResult, SshError

if TYPE_CHECKING:
    from niksnaks_hosting.shared.remote.host import SshHost

PLATFORM_POSIX = "posix"
PLATFORM_WINDOWS = "windows"

# Where a Windows host keeps the throwaway scripts this app runs, relative to the login
# user's home. Under the account itself, so nothing here ever needs privileges.
WINDOWS_SCRIPT_DIR = "niksnaks-hosting/tmp"

# Bedrock is published for x64 only -- there is no ARM build for either platform.
BEDROCK_ARCHES = {"x86_64", "AMD64", "amd64", "x64"}


class HostOps(ABC):
    """Everything that is spelled differently on one machine than another."""

    platform: str = PLATFORM_POSIX
    # The name Adoptium knows this platform by, and what its java binary is called.
    java_os_name: str = "linux"
    java_binary: str = "java"
    bedrock_binary: str = "bedrock_server"

    # ------------------------------------------------------------------- commands
    @abstractmethod
    def run(
        self, host: SshHost, args: list[str], timeout: float = 120.0, cwd: Any = None
    ) -> CommandResult: ...

    @abstractmethod
    def which(self, host: SshHost, program: str) -> str:
        """Where a program is, in the spelling a command line wants. Empty if absent."""

    @abstractmethod
    def home(self, host: SshHost) -> str:
        """The login user's home directory, as a POSIX path."""

    # ---------------------------------------------------------------------- paths
    def to_native(self, path: Any) -> str:
        """A POSIX path in the spelling this host's own command line expects."""
        return str(path)

    def to_posix(self, path: Any) -> str:
        """A path in this host's spelling, as the POSIX path SFTP and RemotePath use."""
        return str(path)

    # ------------------------------------------------------------------- transfer
    @abstractmethod
    def download_command(self, host: SshHost, url: str, dest: str, user_agent: str = "") -> str:
        """A command that fetches *url* to the POSIX path *dest*, ready to stream."""

    def bedrock_command(self, server_dir: str, binary: str) -> list[str]:
        """How to launch the Bedrock server, which is not simply its own path everywhere."""
        return [binary]

    @abstractmethod
    def extract_archive(self, host: SshHost, archive: str, dest: str) -> CommandResult:
        """Unpack an archive into a folder, both given as POSIX paths."""

    def make_executable(self, host: SshHost, path: str) -> None:
        """Windows has no execute bit, so this is a no-op there."""

    @abstractmethod
    def remove_tree(self, host: SshHost, path: str) -> None: ...

    # --------------------------------------------------------------- capabilities
    @abstractmethod
    def requirements(self, host: SshHost) -> list[str]:
        """What this host is missing before it can run a server. Empty means ready."""

    @abstractmethod
    def ensure_service_manager(self, host: SshHost) -> tuple[bool, str]:
        """Whether servers here will outlive the SSH session, and why not if they will not."""

    @abstractmethod
    def service(self, host: SshHost, server_id: str, server_dir: str) -> RemoteService: ...

    def supports_bedrock(self, arch: str) -> bool:
        return arch in BEDROCK_ARCHES

    @abstractmethod
    def default_data_dir(self, host: SshHost) -> str:
        """Where a copy of this app installed on that machine keeps its own data."""

    # ------------------------------------------------------------------ key login
    @abstractmethod
    def append_authorized_key(self, host: SshHost, public_line: str) -> None:
        """Add one key to the login user's authorized_keys, exactly once."""

    def append_admin_authorized_key(self, host: SshHost, public_line: str) -> bool:
        """The second place a key may have to go. Only Windows has one; False elsewhere."""
        return False

    @abstractmethod
    def revoke_authorized_key(self, host: SshHost, public_line: str) -> bool:
        """Take this app's key back off, wherever it was put."""


# ===========================================================================  POSIX
class PosixOps(HostOps):
    """Linux and the BSDs: systemd user units, a FIFO for stdin, POSIX paths throughout.

    Every command builder here is deliberately byte-identical to what SshHost used to
    build inline. Any drift would quietly change paths already written into servers.json.
    """

    platform = PLATFORM_POSIX
    java_os_name = "linux"
    java_binary = "java"
    bedrock_binary = "bedrock_server"

    def run(
        self, host: SshHost, args: list[str], timeout: float = 120.0, cwd: Any = None
    ) -> CommandResult:
        command = shlex.join(args)
        if cwd:
            command = f"cd {shlex.quote(str(cwd))} && {command}"
        return host.connection.run(command, timeout=timeout)

    def which(self, host: SshHost, program: str) -> str:
        result = host.connection.run(
            f"sh -lc {shlex.quote(f'command -v {shlex.quote(program)} || true')}", timeout=20
        )
        text = result.stdout.strip()
        return text.splitlines()[0] if text else ""

    def home(self, host: SshHost) -> str:
        result = host.connection.run('printf %s "$HOME"', timeout=20)
        return result.stdout.strip() or f"/home/{host.credentials.username}"

    # ------------------------------------------------------------------- transfer
    def download_command(self, host: SshHost, url: str, dest: str, user_agent: str = "") -> str:
        if self.which(host, "curl"):
            agent = f"-A {shlex.quote(user_agent)} " if user_agent else ""
            return f"curl -fsSL --retry 2 {agent}-o {shlex.quote(dest)} {shlex.quote(url)}"
        if self.which(host, "wget"):
            agent = f"-U {shlex.quote(user_agent)} " if user_agent else ""
            return f"wget -q {agent}-O {shlex.quote(dest)} {shlex.quote(url)}"
        raise SshError(
            _(
                "{} has neither curl nor wget installed, so it cannot fetch server files. "
                "Install one of them and try again."
            ).format(host.name)
        )

    def extract_archive(self, host: SshHost, archive: str, dest: str) -> CommandResult:
        if archive.endswith(".zip"):
            return host.run(["unzip", "-oq", archive, "-d", dest], timeout=600)
        return host.run(["tar", "-xzf", archive, "-C", dest, "--no-same-owner"], timeout=600)

    def make_executable(self, host: SshHost, path: str) -> None:
        host.run(["chmod", "+x", path], timeout=20)

    def bedrock_command(self, server_dir: str, binary: str) -> list[str]:
        """Bedrock ships its own shared libraries and looks for them beside itself.

        Without LD_LIBRARY_PATH the server exits immediately with a linker error about
        libCrypto, which reads like a broken download rather than a missing variable.
        """
        return ["env", f"LD_LIBRARY_PATH={server_dir}", binary]

    def remove_tree(self, host: SshHost, path: str) -> None:
        host.run(["rm", "-rf", "--", path], timeout=300)

    # --------------------------------------------------------------- capabilities
    def requirements(self, host: SshHost) -> list[str]:
        missing: list[str] = []
        if not self.which(host, "curl") and not self.which(host, "wget"):
            missing.append(_("curl or wget, to download server files"))
        if not self.which(host, "systemctl"):
            missing.append(_("systemd, to keep servers running after you disconnect"))
        if not self.which(host, "tar"):
            missing.append(_("tar, to unpack the Java runtime"))
        if not self.which(host, "unzip"):
            missing.append(_("unzip, to unpack modpacks"))
        return missing

    def ensure_service_manager(self, host: SshHost) -> tuple[bool, str]:
        from niksnaks_hosting.shared.remote.systemd_service import ensure_user_manager

        return ensure_user_manager(host)

    def service(self, host: SshHost, server_id: str, server_dir: str) -> RemoteService:
        from niksnaks_hosting.shared.remote.systemd_service import SystemdService

        return SystemdService(host, server_id, server_dir)

    def default_data_dir(self, host: SshHost) -> str:
        return f"{host.home}/.local/share/niksnaks-hosting"

    # ------------------------------------------------------------------ key login
    def append_authorized_key(self, host: SshHost, public_line: str) -> None:
        from niksnaks_hosting.shared.remote.key_auth import authorized_key_script

        result = host.run_shell(authorized_key_script(public_line), timeout=60)
        if not result.ok:
            raise SshError(
                _("Could not add the key to {}: {}").format(
                    host.name, result.output or _("exit status {}").format(result.exit_status)
                )
            )

    def revoke_authorized_key(self, host: SshHost, public_line: str) -> bool:
        from niksnaks_hosting.shared.remote.key_auth import revoke_key_script

        return host.run_shell(revoke_key_script(public_line), timeout=60).ok


# =========================================================================  Windows
class WindowsOps(HostOps):
    """Windows: Scheduled Tasks for supervision, RCON for console input, PowerShell for
    anything carrying user data.

    Two rules earn their keep here. Never rely on the host's default shell -- OpenSSH for
    Windows runs the command through whatever DefaultShell is set to, which may be
    PowerShell, where %USERPROFILE% is a literal string -- so every command line starts
    with `cmd /c`. And never build a command line out of user data: write a PowerShell
    script over SFTP and run the file, which removes an entire class of quoting bug.
    """

    platform = PLATFORM_WINDOWS
    java_os_name = "windows"
    java_binary = "java.exe"
    bedrock_binary = "bedrock_server.exe"

    # ---------------------------------------------------------------------- paths
    def to_native(self, path: Any) -> str:
        text = str(path)
        # SFTP spells a drive as /C:/Users/nik; a command line wants C:\Users\nik.
        if re.match(r"^/[A-Za-z]:", text):
            text = text[1:]
        return text.replace("/", "\\")

    def to_posix(self, path: Any) -> str:
        text = str(path).replace("\\", "/")
        if re.match(r"^[A-Za-z]:", text):
            text = "/" + text
        return text

    # ------------------------------------------------------------------- commands
    def run(
        self, host: SshHost, args: list[str], timeout: float = 120.0, cwd: Any = None
    ) -> CommandResult:
        """Run a program with exactly the arguments given, and collect what it said.

        Deliberately not `& 'prog' 'arg'`: PowerShell builds the native command line
        itself there, and 5.1 passes an embedded double quote through unescaped, so an
        argument containing one arrives at the program with the quotes eaten. Which
        version does what changed again in 7.3, so there is no spelling that is correct
        everywhere. Handing .NET a command line quoted the way the C runtime parses it
        removes PowerShell from the question entirely.
        """
        if not args:
            return CommandResult(0, "", "")

        from niksnaks_hosting.shared.remote.windows_task_service import win_quote

        command_line = " ".join(win_quote(a) for a in args[1:])
        location = f"$psi.WorkingDirectory = {ps_quote(self.to_native(cwd))}\n" if cwd else ""

        body = f"""$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = {ps_quote(self.to_native(args[0]))}
$psi.Arguments = {ps_quote(command_line)}
{location}$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true
$proc = [System.Diagnostics.Process]::Start($psi)
# Both streams are read at once. Waiting on one and then the other deadlocks the moment
# a program fills the pipe it is not being read from, which a Forge installer does.
$outTask = $proc.StandardOutput.ReadToEndAsync()
$errTask = $proc.StandardError.ReadToEndAsync()
$proc.WaitForExit()
[Console]::Out.Write($outTask.GetAwaiter().GetResult())
[Console]::Error.Write($errTask.GetAwaiter().GetResult())
exit $proc.ExitCode"""
        return self.run_script(host, body, timeout=timeout)

    def which(self, host: SshHost, program: str) -> str:
        # Only ever asked about names this app chose, so no user data reaches the line.
        safe = re.sub(r"[^A-Za-z0-9_.-]", "", program)
        if not safe:
            return ""
        result = host.connection.run(f"cmd /c where {safe} 2>NUL", timeout=20)
        for line in result.stdout.splitlines():
            found = line.strip()
            if found:
                return found
        return ""

    def home(self, host: SshHost) -> str:
        # The SFTP server's own answer, rather than a shell's: it is the spelling every
        # RemotePath on this host will be built from, so it has to come from the same
        # place. OpenSSH answers /C:/Users/nik.
        try:
            return host.fs.normalize(".")
        except Exception:
            return f"/C:/Users/{host.credentials.username}"

    def account(self, host: SshHost) -> str:
        """The logged-in account as Windows spells it, MACHINE\\user, asked for once.

        A Scheduled Task has to name the account it runs as, and the bare username is not
        enough -- a local account and a domain account of the same name are different
        principals, and registering the wrong one fails with nothing useful to say.
        """
        cached = getattr(host, "_windows_account", "")
        if cached:
            return str(cached)
        try:
            result = host.connection.run("cmd /c whoami", timeout=20)
            name = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
        except Exception:
            name = ""
        name = name or host.credentials.username
        host._windows_account = name
        return name

    # ------------------------------------------------------------- script running
    def script_dir(self, host: SshHost) -> str:
        return f"{host.home}/{WINDOWS_SCRIPT_DIR}"

    def run_script(self, host: SshHost, body: str, timeout: float = 120.0) -> CommandResult:
        """Write a PowerShell script over SFTP and run it, then take it away again.

        Going through a file rather than a command line is what makes paths with spaces,
        ampersands and percent signs simply work. The preamble turns a missing program
        into exit status 127, matching what a POSIX shell would have said, so callers
        that check for it keep working on both platforms.
        """
        folder = self.script_dir(host)
        host.path(folder).mkdir(parents=True, exist_ok=True)
        remote = f"{folder}/run-{uuid.uuid4().hex[:12]}.ps1"

        script = _POWERSHELL_PREAMBLE + body + "\n" + _POWERSHELL_EPILOGUE
        # UTF-8 with a BOM: PowerShell 5.1 reads a BOM-less file as the system codepage,
        # which mangles any non-ASCII path in it.
        host.path(remote).write_bytes(b"\xef\xbb\xbf" + script.encode("utf-8"))

        command = (
            "cmd /c powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
            f'-File "{self.to_native(remote)}"'
        )
        try:
            return host.connection.run(command, timeout=timeout)
        finally:
            try:
                host.path(remote).unlink(missing_ok=True)
            except Exception:
                pass

    def open_script_stream(self, host: SshHost, body: str) -> Any:
        """The same, for output that arrives over time. The script is left to be reaped.

        A streamed script outlives this call by definition, so it cannot be deleted here.
        They are small, they live in one folder, and the next start sweeps it.
        """
        folder = self.script_dir(host)
        host.path(folder).mkdir(parents=True, exist_ok=True)
        remote = f"{folder}/stream-{uuid.uuid4().hex[:12]}.ps1"
        script = _POWERSHELL_PREAMBLE + body + "\n" + _POWERSHELL_EPILOGUE
        host.path(remote).write_bytes(b"\xef\xbb\xbf" + script.encode("utf-8"))
        return host.connection.open_stream(
            "cmd /c powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
            f'-File "{self.to_native(remote)}"'
        )

    def sweep_scripts(self, host: SshHost, keep: int = 40) -> None:
        """Throw away the outlived scripts that piled up, newest of each kind kept.

        Covers every script this class leaves behind on purpose: a "stream-" script
        outlives the call that wrote it because a console or a download is still
        reading from the channel it started, and a "get-" download script outlives it
        the same way -- download_command hands back a command line, not something
        with a lifetime this class controls, so there is nowhere else to delete it
        once the transfer finishes.
        """
        try:
            folder = host.path(self.script_dir(host))
            all_entries = list(folder.iterdir())
            for prefix in ("stream-", "get-"):
                entries = sorted(
                    (p for p in all_entries if p.name.startswith(prefix)),
                    key=lambda p: p.name,
                )
                for stale in entries[:-keep] if len(entries) > keep else []:
                    stale.unlink(missing_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------- transfer
    def download_command(self, host: SshHost, url: str, dest: str, user_agent: str = "") -> str:
        """curl.exe ships with Windows 10 1803 and later; older ones fall back to .NET."""
        # This script outlives the call that writes it -- the returned command line is
        # run by the caller's own channel, so this is the only moment it will see
        # before that download starts, and the only chance to clear out old ones.
        self.sweep_scripts(host)
        folder = self.script_dir(host)
        host.path(folder).mkdir(parents=True, exist_ok=True)
        remote = f"{folder}/get-{uuid.uuid4().hex[:12]}.ps1"

        body = f"""$url = {ps_quote(url)}
$dest = {ps_quote(self.to_native(dest))}
$agent = {ps_quote(user_agent)}
$code = 1
try {{
    if ($agent) {{
        & curl.exe -fsSL --retry 2 -A $agent -o $dest $url
    }} else {{
        & curl.exe -fsSL --retry 2 -o $dest $url
    }}
    $code = $LASTEXITCODE
}} catch {{
    $code = 1
}}
if ($code -ne 0) {{
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    if ($agent) {{
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -UserAgent $agent
    }} else {{
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    }}
    # The fallback throws on failure, so reaching here means it worked -- but
    # $LASTEXITCODE still holds the failed attempt's status, and the epilogue exits
    # with it. Without this a successful download reports failure.
    $global:LASTEXITCODE = 0
}}"""
        host.path(remote).write_bytes(
            b"\xef\xbb\xbf" + (_POWERSHELL_PREAMBLE + body + "\n" + _POWERSHELL_EPILOGUE).encode("utf-8")
        )
        return (
            "cmd /c powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
            f'-File "{self.to_native(remote)}"'
        )

    def extract_archive(self, host: SshHost, archive: str, dest: str) -> CommandResult:
        """bsdtar reads zip as well as tar.gz, and ships with Windows 10 1803 and later."""
        body = f"""$archive = {ps_quote(self.to_native(archive))}
$dest = {ps_quote(self.to_native(dest))}
$code = 1
try {{
    & tar.exe -xf $archive -C $dest
    $code = $LASTEXITCODE
}} catch {{
    $code = 1
}}
if ($code -ne 0) {{
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $dest)
    # See download_command: a successful fallback has to clear the status the failed
    # first attempt left behind, or the epilogue reports the archive as unreadable.
    $global:LASTEXITCODE = 0
}}"""
        return self.run_script(host, body, timeout=900)

    def remove_tree(self, host: SshHost, path: str) -> None:
        body = (
            f"$target = {ps_quote(self.to_native(path))}\n"
            "if (Test-Path -LiteralPath $target) { "
            "Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue }"
        )
        self.run_script(host, body, timeout=300)

    # --------------------------------------------------------------- capabilities
    def requirements(self, host: SshHost) -> list[str]:
        missing: list[str] = []
        if not self.which(host, "powershell"):
            missing.append(_("Windows PowerShell, which this app uses to run everything else"))
        if not self.which(host, "schtasks"):
            missing.append(_("the Task Scheduler, to keep servers running after you disconnect"))
        # No separate curl requirement: download_command falls back to
        # Invoke-WebRequest when curl.exe is missing, so PowerShell being present is
        # already sufficient and is checked above.
        return missing

    def ensure_service_manager(self, host: SshHost) -> tuple[bool, str]:
        if not self.which(host, "schtasks"):
            return False, _(
                "{} has no Task Scheduler, so servers could not be kept running there."
            ).format(host.name)
        return True, ""

    def service(self, host: SshHost, server_id: str, server_dir: str) -> RemoteService:
        from niksnaks_hosting.shared.remote.windows_task_service import WindowsTaskService

        return WindowsTaskService(host, server_id, server_dir)

    def default_data_dir(self, host: SshHost) -> str:
        return f"{host.home}/AppData/Local/Niksnaks-Hosting"

    # ------------------------------------------------------------------ key login
    def append_authorized_key(self, host: SshHost, public_line: str) -> None:
        result = self.run_script(host, _authorized_key_body(public_line, admin=False), timeout=60)
        if not result.ok:
            raise SshError(
                _("Could not add the key to {}: {}").format(
                    host.name, result.output or _("exit status {}").format(result.exit_status)
                )
            )

    def append_admin_authorized_key(self, host: SshHost, public_line: str) -> bool:
        """OpenSSH's stock config sends administrators somewhere else entirely.

        The shipped sshd_config ends with a Match Group administrators block pointing at
        C:\\ProgramData\\ssh\\administrators_authorized_keys, and for those accounts the
        profile file is ignored outright. That file needs its ACLs set by SID -- the
        Administrators group is renamed on localised Windows, so matching by name fails
        on exactly the machines this is meant to help.
        """
        check = self.run_script(host, _ADMIN_CHECK_BODY, timeout=30)
        if "admin" not in check.stdout.lower():
            return False
        return self.run_script(host, _authorized_key_body(public_line, admin=True), timeout=60).ok

    def revoke_authorized_key(self, host: SshHost, public_line: str) -> bool:
        return self.run_script(host, _revoke_key_body(public_line), timeout=60).ok


# ==================================================================  PowerShell bits
# Native commands must not be turned into terminating errors by PowerShell 7.3's
# preference, or a server that writes a warning to stderr would look like a failure. A
# genuinely missing program still throws, and is reported as 127 to match a POSIX shell.
_POWERSHELL_PREAMBLE = """$ErrorActionPreference = 'Continue'
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$global:LASTEXITCODE = 0
try {
"""

_POWERSHELL_EPILOGUE = """}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 127
}
if ($null -eq $LASTEXITCODE) { exit 0 }
exit $LASTEXITCODE
"""


def ps_quote(value: Any) -> str:
    """A PowerShell single-quoted literal. Nothing inside one is expanded but a quote."""
    return "'" + str(value).replace("'", "''") + "'"


_ADMIN_CHECK_BODY = """$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($id)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Output 'admin'
} elseif ($id.Groups.Value -contains 'S-1-5-32-544') {
    Write-Output 'admin'
} else {
    Write-Output 'user'
}"""


_UTF8_NO_BOM = "(New-Object System.Text.UTF8Encoding($false))"


def _authorized_key_body(public_line: str, admin: bool) -> str:
    """Add one key, exactly once, in a file sshd will actually read.

    Every write goes through [System.IO.File] with an explicit no-BOM UTF-8 encoding,
    on purpose. PowerShell 5.1's > and Out-File default to UTF-16LE and Add-Content to
    the system codepage; sshd silently ignores an authorized_keys file that starts with
    a BOM, and the resulting auth failure explains nothing at all. -Encoding ascii
    avoids the BOM too, but Get-Content substitutes '?' for anything outside ASCII, so
    reading a file back and rewriting it -- which this does, to append idempotently --
    would permanently mangle a pre-existing key whose comment has a non-ASCII byte in
    it. UTF-8 without a BOM satisfies sshd and round-trips everything else untouched.
    Read-modify-write happens in one invocation so the duplicate check cannot race the
    append.
    """
    if admin:
        target = "($env:ProgramData + '\\ssh\\administrators_authorized_keys')"
        acl = """& icacls "$file" /inheritance:r /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F" | Out-Null"""
    else:
        target = "($env:USERPROFILE + '\\.ssh\\authorized_keys')"
        acl = ""

    return f"""$key = {ps_quote(public_line)}
$file = {target}
$dir = Split-Path -Parent $file
if (-not (Test-Path -LiteralPath $dir)) {{ New-Item -ItemType Directory -Path $dir -Force | Out-Null }}
$lines = @()
if (Test-Path -LiteralPath $file) {{
    $lines = @([IO.File]::ReadAllLines($file))
}}
if ($lines -notcontains $key) {{
    $lines = @($lines | Where-Object {{ $_ -ne '' }}) + $key
    [IO.File]::WriteAllLines($file, $lines, {_UTF8_NO_BOM})
}}
{acl}"""


def _revoke_key_body(public_line: str) -> str:
    """Take the key out of both files it could be in.

    Each element of the list is parenthesized because PowerShell binds the comma tighter
    than +: without them, "a" + $x, $y + "b" is one concatenated string rather than two
    paths, and the loop runs once over a filename that cannot exist -- silently, since a
    path that is not there is exactly what this is expected to find sometimes.

    Read and written the same no-BOM UTF-8 way as _authorized_key_body, so revoking a
    key never mangles a sibling entry's non-ASCII comment either.
    """
    return f"""$key = {ps_quote(public_line)}
$files = @(($env:USERPROFILE + '\\.ssh\\authorized_keys'), ($env:ProgramData + '\\ssh\\administrators_authorized_keys'))
foreach ($file in $files) {{
    if (Test-Path -LiteralPath $file) {{
        $lines = @([IO.File]::ReadAllLines($file))
        if ($lines -contains $key) {{
            $kept = @($lines | Where-Object {{ $_ -ne $key }})
            [IO.File]::WriteAllLines($file, $kept, {_UTF8_NO_BOM})
        }}
    }}
}}"""


# ======================================================================  detection
_POSIX_OPS = PosixOps()
_WINDOWS_OPS = WindowsOps()


def make_ops(platform: str) -> HostOps:
    return _WINDOWS_OPS if platform == PLATFORM_WINDOWS else _POSIX_OPS


def detect_platform(host: SshHost) -> tuple[str, str]:
    """Work out what the far machine is, cheapest question first.

    Everything here goes through host.connection directly, never host.run: host.run
    routes through ops, ops is what this is being asked to choose, and the recursion is
    only obvious once it has already hung.
    """
    banner = host.connection.server_banner.lower()
    if "windows" in banner:
        found = _probe_windows(host)
        if found:
            return found
        return PLATFORM_WINDOWS, "AMD64"

    try:
        result = host.connection.run("uname -sm", timeout=20)
    except Exception:
        result = CommandResult(1, "", "")

    text = result.stdout.strip()
    if result.ok and text:
        system, _sep, machine = text.partition(" ")
        # Git for Windows and Cygwin both answer uname while being Windows underneath,
        # where there is no systemd and none of this would work.
        if any(marker in system.upper() for marker in ("MINGW", "MSYS", "CYGWIN")):
            return PLATFORM_WINDOWS, machine.strip() or "AMD64"
        return PLATFORM_POSIX, machine.strip() or "x86_64"

    found = _probe_windows(host)
    if found:
        return found
    return PLATFORM_POSIX, "x86_64"


def _probe_windows(host: SshHost) -> tuple[str, str] | None:
    try:
        result = host.connection.run('cmd /c "ver & echo %PROCESSOR_ARCHITECTURE%"', timeout=20)
    except Exception:
        return None
    text = result.stdout
    if "windows" not in text.lower():
        return None
    arch = ""
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if candidate and "%" not in candidate and " " not in candidate:
            arch = candidate
            break
    return PLATFORM_WINDOWS, arch or "AMD64"
