from __future__ import annotations

import shlex
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from niksnaks_hosting.shared.remote.paths import RemotePath
from niksnaks_hosting.shared.remote.sftp_fs import SftpFs
from niksnaks_hosting.shared.remote.ssh_client import CommandResult, SshConnection, SshCredentials, SshError
from niksnaks_hosting.shared.utils.subprocess_utils import hidden_subprocess_kwargs

LOCAL_HOST_ID = ""

ProgressFn = Callable[[float, str], None]

# How often a host-side download is asked how far it has got. Often enough for a bar
# that moves, rare enough that a long download is not a stream of round trips.
DOWNLOAD_POLL_SECONDS = 0.5


class Host(ABC):
    """Where a server lives: this machine, or one reached over SSH.

    Everything the app does to a server folder goes through a Host, so the same
    manager code drives a local install and a remote one. Paths handed out by path()
    know which machine they belong to.
    """

    id: str = LOCAL_HOST_ID
    name: str = "This computer"

    @property
    @abstractmethod
    def is_local(self) -> bool: ...

    @abstractmethod
    def path(self, value: Any) -> Any:
        """A path object rooted on this host."""

    @abstractmethod
    def run(self, args: list[str], timeout: float = 120.0, cwd: Any = None) -> CommandResult: ...

    @abstractmethod
    def fetch_url(
        self,
        url: str,
        dest: Any,
        expected_size: int = 0,
        progress: ProgressFn | None = None,
        timeout: float = 900.0,
        user_agent: str = "",
    ) -> None:
        """Put the contents of *url* at *dest* on this host, without routing the bytes
        through the app when the host can fetch them itself."""

    @abstractmethod
    def check_ready(self) -> tuple[bool, str]:
        """Whether this host can actually run a server, and what is missing if not."""

    def which(self, program: str) -> str:
        result = self.run(["sh", "-lc", f"command -v {shlex.quote(program)} || true"], timeout=20)
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""

    def __str__(self) -> str:
        return self.name


class LocalHost(Host):
    """This machine. Every call is the plain local operation it always was."""

    id = LOCAL_HOST_ID

    def __init__(self) -> None:
        self.name = _("This computer")

    @property
    def is_local(self) -> bool:
        return True

    def path(self, value: Any) -> Path:
        return Path(value)

    def run(self, args: list[str], timeout: float = 120.0, cwd: Any = None) -> CommandResult:
        try:
            done = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd) if cwd else None,
                **hidden_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", f"{args[0]} timed out")
        except OSError as e:
            return CommandResult(127, "", str(e))
        return CommandResult(done.returncode, done.stdout or "", done.stderr or "")

    def fetch_url(
        self,
        url: str,
        dest: Any,
        expected_size: int = 0,
        progress: ProgressFn | None = None,
        timeout: float = 900.0,
        user_agent: str = "",
    ) -> None:
        import requests

        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": user_agent} if user_agent else None
        response = requests.get(url, stream=True, timeout=60, headers=headers)
        response.raise_for_status()
        total = int(response.headers.get("content-length", expected_size) or 0)
        done = 0
        with open(target, "wb") as handle:
            for chunk in response.iter_content(chunk_size=262144):
                handle.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total, target.name)

    def check_ready(self) -> tuple[bool, str]:
        return True, ""


class SshHost(Host):
    """A machine reached over SSH, with its files served over SFTP.

    The far machine may be Linux or Windows, and which one is not known when this object
    is built: hosts are loaded from hosts.json at startup, long before anything connects.
    So everything that is spelled differently on the two lives behind self.ops, which is
    chosen on first use and remembered in hosts.json so the next launch already knows.
    """

    def __init__(
        self,
        host_id: str,
        name: str,
        credentials: SshCredentials,
        platform: str = "",
        arch: str = "",
    ) -> None:
        self.id = host_id
        self.name = name or credentials.label
        self.credentials = credentials
        self.connection = SshConnection(credentials)
        self.fs = SftpFs(self.connection)
        self._home: str = ""
        self._lock = threading.RLock()
        self.platform = platform
        self.arch = arch
        self._ops: Any = None
        # Set by HostsManager so a platform learned the hard way is written down once.
        self.on_platform_detected: Callable[[SshHost], None] | None = None

    @property
    def is_local(self) -> bool:
        return False

    # ------------------------------------------------------------------ platform
    @property
    def ops(self) -> Any:
        """How this host spells things. Detected once, then remembered.

        Detection asks the connection directly and never self.run(), which would route
        back through here and wait forever on the answer it is trying to produce.
        """
        from niksnaks_hosting.shared.remote.host_ops import detect_platform, make_ops

        with self._lock:
            if self._ops is not None:
                return self._ops
            if self.platform:
                self._ops = make_ops(self.platform)
                return self._ops

        # Outside the lock: detection talks to the host, and holding the lock across a
        # round trip would stall every other caller behind it.
        try:
            platform, arch = detect_platform(self)
        except Exception:
            # The host was unreachable, not identified as POSIX. Falling back silently
            # and writing that guess down would strand a Windows host on the wrong ops
            # forever, since a truthy self.platform short-circuits detection above on
            # every later call -- including once the host is back and answering.
            return make_ops("posix")

        with self._lock:
            if self._ops is None:
                self.platform, self.arch = platform, arch
                self._ops = make_ops(platform)
            notify = self.on_platform_detected
        if notify:
            try:
                notify(self)
            except Exception:
                pass
        return self._ops

    def to_native(self, path: Any) -> str:
        """A path in the spelling this host's own command lines want."""
        return self.ops.to_native(path)

    def which(self, program: str) -> str:
        return self.ops.which(self, program)

    def connect(self) -> None:
        self.connection.connect()

    def close(self) -> None:
        self.connection.close()

    @property
    def is_connected(self) -> bool:
        return self.connection.is_connected

    def path(self, value: Any) -> RemotePath:
        if isinstance(value, RemotePath):
            return value
        text = str(value).replace("\\", "/")
        if text.startswith("~"):
            text = self.home + text[1:]
        return self.fs.path(text)

    @property
    def home(self) -> str:
        """The login user's home directory, asked for once and remembered.

        Always a POSIX path, even on Windows, where SFTP answers /C:/Users/nik. This
        string is interpolated into server paths that get written to servers.json, so its
        spelling must not drift between releases.
        """
        # Resolve ops first: asking for it inside the lock would deadlock against
        # detection, which itself wants nothing from here.
        ops = self.ops
        with self._lock:
            if not self._home:
                self._home = ops.home(self)
            return self._home

    def run(self, args: list[str], timeout: float = 120.0, cwd: Any = None) -> CommandResult:
        return self.ops.run(self, args, timeout=timeout, cwd=cwd)

    def run_shell(self, script: str, timeout: float = 120.0) -> CommandResult:
        """Run a POSIX shell script verbatim. Only meaningful on a POSIX host."""
        return self.connection.run(script, timeout=timeout)

    def fetch_url(
        self,
        url: str,
        dest: Any,
        expected_size: int = 0,
        progress: ProgressFn | None = None,
        timeout: float = 900.0,
        user_agent: str = "",
    ) -> None:
        """Have the host download it. A 300MB modpack should never cross the app twice."""
        target = self.path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        remote = str(target)
        partial = remote + ".part"

        channel = self.connection.open_stream(
            self.ops.download_command(self, url, partial, user_agent=user_agent)
        )
        started = time.monotonic()
        try:
            while not channel.exit_status_ready():
                if time.monotonic() - started > timeout:
                    channel.close()
                    raise SshError(f"Downloading {url} on {self.name} took too long.")
                if progress and expected_size:
                    # Only worth an SFTP round trip when there is a total to divide by --
                    # otherwise it is a stat every half second whose answer can never be
                    # shown as anything but a bar stuck at zero.
                    info = self.fs.stat(partial)
                    done = info.st_size if info else 0
                    progress(min(1.0, done / expected_size), target.name)
                time.sleep(DOWNLOAD_POLL_SECONDS)
            status = channel.recv_exit_status()
            detail = channel.recv_stderr(65536).decode("utf-8", errors="replace").strip()
        finally:
            try:
                channel.close()
            except Exception:
                pass

        if status != 0:
            self.fs.remove(partial, missing_ok=True)
            raise SshError(f"{self.name} could not download {url}: {detail or f'exit status {status}'}")

        self.fs.rename(partial, remote)
        if progress:
            progress(1.0, target.name)

    def upload(self, local_path: Any, remote_path: Any, progress: ProgressFn | None = None) -> None:
        target = self.path(remote_path)
        self.fs.upload(
            local_path,
            str(target),
            on_bytes=(lambda d, t: progress(d / t if t else 0.0, target.name)) if progress else None,
        )

    def download(self, remote_path: Any, local_path: Any, progress: ProgressFn | None = None) -> None:
        source = self.path(remote_path)
        self.fs.download(
            str(source),
            local_path,
            on_bytes=(lambda d, t: progress(d / t if t else 0.0, source.name)) if progress else None,
        )

    def check_ready(self) -> tuple[bool, str]:
        """A host needs a way to download and a way to keep a server running.

        What that means depends on the machine, so the list comes from ops -- which is
        also the point at which the platform gets worked out and written down.
        """
        try:
            self.connect()
        except SshError as e:
            return False, str(e)

        missing = self.ops.requirements(self)
        if missing:
            return False, _("{} is missing: {}").format(self.name, ", ".join(missing))
        return True, ""
