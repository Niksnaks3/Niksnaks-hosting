from __future__ import annotations

import errno
import fnmatch
import posixpath
import stat as stat_module
import threading
from collections.abc import Iterator
from typing import Any

from niksnaks_hosting.shared.remote.paths import RemotePath
from niksnaks_hosting.shared.remote.ssh_client import SshConnection, SshError

# Reading a whole modpack a few kilobytes at a time spends the entire transfer waiting
# on round trips. SFTP tops out well above this, but it is a sane per-read chunk.
TRANSFER_CHUNK = 262144


class SftpFs:
    """Filesystem operations on a host, over one SFTP session.

    Every RemotePath ends up here. Missing files come back as None from stat rather
    than as exceptions, because most callers are asking a question, not demanding a
    file; the ones that do demand raise from RemotePath instead.
    """

    def __init__(self, connection: SshConnection) -> None:
        self._connection = connection
        self._lock = threading.RLock()

    @property
    def label(self) -> str:
        return self._connection.credentials.label

    @property
    def connection(self) -> SshConnection:
        return self._connection

    def path(self, value: str) -> RemotePath:
        return RemotePath(self, value)

    def _sftp(self) -> Any:
        return self._connection.sftp()

    # ------------------------------------------------------------------- queries
    def stat(self, path: str) -> Any | None:
        try:
            with self._lock:
                return self._sftp().stat(path)
        except FileNotFoundError:
            return None
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ENOTDIR):
                return None
            raise SshError(f"Could not read {path} on {self.label}: {e}") from e

    def lstat(self, path: str) -> Any | None:
        try:
            with self._lock:
                return self._sftp().lstat(path)
        except FileNotFoundError:
            return None
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ENOTDIR):
                return None
            raise SshError(f"Could not read {path} on {self.label}: {e}") from e

    def normalize(self, path: str) -> str:
        try:
            with self._lock:
                return self._sftp().normalize(path)
        except Exception:
            # normalize needs the path to exist; for one that does not, collapsing the
            # dots ourselves is the same answer Path.resolve would give.
            return posixpath.normpath(path)

    def listdir(self, path: str) -> list[str]:
        try:
            with self._lock:
                return list(self._sftp().listdir(path))
        except FileNotFoundError:
            return []
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ENOTDIR):
                return []
            raise SshError(f"Could not list {path} on {self.label}: {e}") from e

    def listdir_attr(self, path: str) -> list[Any]:
        try:
            with self._lock:
                return list(self._sftp().listdir_attr(path))
        except FileNotFoundError:
            return []
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ENOTDIR):
                return []
            raise SshError(f"Could not list {path} on {self.label}: {e}") from e

    # ------------------------------------------------------------------ mutation
    def mkdir(self, path: str, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        info = self.stat(path)
        if info is not None:
            if exist_ok and stat_module.S_ISDIR(info.st_mode):
                return
            raise FileExistsError(path)

        if parents:
            parent = posixpath.dirname(path.rstrip("/"))
            if parent and parent != path and self.stat(parent) is None:
                self.mkdir(parent, mode=mode, parents=True, exist_ok=True)

        try:
            with self._lock:
                self._sftp().mkdir(path, mode)
        except OSError as e:
            # Something else may have created it between the check and the call.
            if exist_ok and self.stat(path) is not None:
                return
            raise SshError(f"Could not create {path} on {self.label}: {e}") from e

    def rmdir(self, path: str) -> None:
        try:
            with self._lock:
                self._sftp().rmdir(path)
        except OSError as e:
            raise SshError(f"Could not remove the folder {path} on {self.label}: {e}") from e

    def remove(self, path: str, missing_ok: bool = False) -> None:
        try:
            with self._lock:
                self._sftp().remove(path)
        except FileNotFoundError:
            if not missing_ok:
                raise
        except OSError as e:
            if e.errno == errno.ENOENT:
                if not missing_ok:
                    raise FileNotFoundError(path) from e
                return
            raise SshError(f"Could not delete {path} on {self.label}: {e}") from e

    def rename(self, source: str, target: str) -> None:
        try:
            with self._lock:
                sftp = self._sftp()
                # posix_rename replaces an existing target the way os.replace does;
                # plain SFTP rename fails instead. Not every server offers it.
                try:
                    sftp.posix_rename(source, target)
                except (AttributeError, OSError):
                    self.remove(target, missing_ok=True)
                    sftp.rename(source, target)
        except OSError as e:
            raise SshError(f"Could not move {source} to {target} on {self.label}: {e}") from e

    def touch(self, path: str, exist_ok: bool = True) -> None:
        if self.stat(path) is not None:
            if not exist_ok:
                raise FileExistsError(path)
            return
        self.write_bytes(path, b"")

    def chmod(self, path: str, mode: int) -> None:
        try:
            with self._lock:
                self._sftp().chmod(path, mode)
        except OSError as e:
            raise SshError(f"Could not set permissions on {path} at {self.label}: {e}") from e

    # --------------------------------------------------------------------- bytes
    def read_bytes(self, path: str) -> bytes:
        try:
            with self._lock:
                with self._sftp().open(path, "rb") as handle:
                    handle.prefetch()
                    return handle.read()
        except FileNotFoundError:
            raise
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise FileNotFoundError(path) from e
            raise SshError(f"Could not read {path} on {self.label}: {e}") from e

    def write_bytes(self, path: str, data: bytes) -> int:
        try:
            with self._lock:
                with self._sftp().open(path, "wb") as handle:
                    handle.write(data)
            return len(data)
        except OSError as e:
            raise SshError(f"Could not write {path} on {self.label}: {e}") from e

    def open(self, path: str, mode: str = "r", encoding: str | None = None) -> Any:
        """A file handle on the host. Text modes are decoded here, as pathlib does."""
        binary = "b" in mode
        sftp_mode = mode.replace("b", "").replace("t", "") or "r"
        try:
            with self._lock:
                handle = self._sftp().open(path, sftp_mode + "b" if binary else sftp_mode)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise FileNotFoundError(path) from e
            raise SshError(f"Could not open {path} on {self.label}: {e}") from e
        if binary or encoding is None:
            return handle
        return _TextHandle(handle, encoding)

    # ---------------------------------------------------------------- transfers
    def download(self, remote: str, local_path: Any, on_bytes: Any = None) -> None:
        from pathlib import Path

        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock:
                with self._sftp().open(remote, "rb") as src, target.open("wb") as dst:
                    src.prefetch()
                    total = self.stat(remote).st_size if self.stat(remote) else 0
                    done = 0
                    while True:
                        chunk = src.read(TRANSFER_CHUNK)
                        if not chunk:
                            break
                        dst.write(chunk)
                        done += len(chunk)
                        if on_bytes:
                            on_bytes(done, total)
        except OSError as e:
            raise SshError(f"Could not download {remote} from {self.label}: {e}") from e

    def upload(self, local_path: Any, remote: str, on_bytes: Any = None) -> None:
        from pathlib import Path

        source = Path(local_path)
        total = source.stat().st_size
        self.mkdir(posixpath.dirname(remote), parents=True, exist_ok=True)
        try:
            with self._lock:
                with source.open("rb") as src, self._sftp().open(remote, "wb") as dst:
                    dst.set_pipelined(True)
                    done = 0
                    while True:
                        chunk = src.read(TRANSFER_CHUNK)
                        if not chunk:
                            break
                        dst.write(chunk)
                        done += len(chunk)
                        if on_bytes:
                            on_bytes(done, total)
        except OSError as e:
            raise SshError(f"Could not upload to {remote} on {self.label}: {e}") from e

    # ------------------------------------------------------------------ globbing
    def glob(self, base: RemotePath, pattern: str, recursive: bool) -> Iterator[RemotePath]:
        """Path.glob and Path.rglob, done with listings rather than a shell.

        Letting the remote shell expand a pattern would mean quoting user-supplied
        names into a command line, so matching happens here instead.
        """
        pattern = pattern.replace("\\", "/").strip("/")
        if not pattern:
            return

        if pattern.startswith("**/"):
            recursive = True
            pattern = pattern[3:]

        if recursive:
            for path, is_dir in self._walk(base):
                if fnmatch.fnmatch(path.name, pattern):
                    yield path
                del is_dir
            return

        head, _, tail = pattern.partition("/")
        for entry in sorted(self.listdir(str(base))):
            if not fnmatch.fnmatch(entry, head):
                continue
            child = base / entry
            if not tail:
                yield child
            elif child.is_dir():
                yield from self.glob(child, tail, recursive=False)

    def _walk(self, base: RemotePath) -> Iterator[tuple[RemotePath, bool]]:
        stack = [base]
        while stack:
            current = stack.pop()
            for attr in sorted(self.listdir_attr(str(current)), key=lambda a: a.filename):
                child = current / attr.filename
                is_dir = stat_module.S_ISDIR(attr.st_mode)
                yield child, is_dir
                if is_dir:
                    stack.append(child)


class _TextHandle:
    """Decodes an SFTP handle's bytes, so read_text-style callers see str."""

    def __init__(self, handle: Any, encoding: str) -> None:
        self._handle = handle
        self._encoding = encoding

    def read(self, size: int = -1) -> str:
        return self._handle.read(size).decode(self._encoding, errors="replace")

    def write(self, data: str) -> int:
        return self._handle.write(data.encode(self._encoding))

    def __iter__(self) -> Iterator[str]:
        for line in self._handle:
            yield line.decode(self._encoding, errors="replace")

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> _TextHandle:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
