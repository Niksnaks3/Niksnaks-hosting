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
    ) -> None:
        import requests

        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(url, stream=True, timeout=60)
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
    """A machine reached over SSH, with its files served over SFTP."""

    def __init__(self, host_id: str, name: str, credentials: SshCredentials) -> None:
        self.id = host_id
        self.name = name or credentials.label
        self.credentials = credentials
        self.connection = SshConnection(credentials)
        self.fs = SftpFs(self.connection)
        self._home: str = ""
        self._lock = threading.RLock()

    @property
    def is_local(self) -> bool:
        return False

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
        """The login user's home directory, asked for once and remembered."""
        with self._lock:
            if not self._home:
                result = self.connection.run("printf %s \"$HOME\"", timeout=20)
                self._home = result.stdout.strip() or f"/home/{self.credentials.username}"
            return self._home

    def run(self, args: list[str], timeout: float = 120.0, cwd: Any = None) -> CommandResult:
        command = shlex.join(args)
        if cwd:
            command = f"cd {shlex.quote(str(cwd))} && {command}"
        return self.connection.run(command, timeout=timeout)

    def run_shell(self, script: str, timeout: float = 120.0) -> CommandResult:
        return self.connection.run(script, timeout=timeout)

    def fetch_url(
        self,
        url: str,
        dest: Any,
        expected_size: int = 0,
        progress: ProgressFn | None = None,
        timeout: float = 900.0,
    ) -> None:
        """Have the host download it. A 300MB modpack should never cross the app twice."""
        target = self.path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        remote = str(target)
        partial = remote + ".part"

        downloader = self._downloader()
        if downloader == "curl":
            command = f"curl -fsSL --retry 2 -o {shlex.quote(partial)} {shlex.quote(url)}"
        else:
            command = f"wget -q -O {shlex.quote(partial)} {shlex.quote(url)}"

        channel = self.connection.open_stream(command)
        started = time.monotonic()
        try:
            while not channel.exit_status_ready():
                if time.monotonic() - started > timeout:
                    channel.close()
                    raise SshError(f"Downloading {url} on {self.name} took too long.")
                if progress:
                    info = self.fs.stat(partial)
                    done = info.st_size if info else 0
                    if expected_size:
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

    def _downloader(self) -> str:
        with self._lock:
            cached = getattr(self, "_downloader_name", "")
            if cached:
                return cached
            name = "curl" if self.which("curl") else ("wget" if self.which("wget") else "")
            if not name:
                raise SshError(
                    f"{self.name} has neither curl nor wget installed, so it cannot fetch "
                    "server files. Install one of them and try again."
                )
            self._downloader_name = name
            return name

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
        """A host needs a shell, a way to download, and systemd to keep servers running."""
        try:
            self.connect()
        except SshError as e:
            return False, str(e)

        missing: list[str] = []
        if not self.which("curl") and not self.which("wget"):
            missing.append(_("curl or wget, to download server files"))
        if not self.which("systemctl"):
            missing.append(_("systemd, to keep servers running after you disconnect"))
        if not self.which("tar"):
            missing.append(_("tar, to unpack the Java runtime"))
        if not self.which("unzip"):
            missing.append(_("unzip, to unpack modpacks"))

        if missing:
            return False, _("{} is missing: {}").format(self.name, ", ".join(missing))
        return True, ""
