from __future__ import annotations

import stat as stat_module
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from niksnaks_hosting.shared.remote.sftp_fs import SftpFs


class RemotePath:
    """A path on another machine, shaped like pathlib.Path so callers need not care.

    Only the slice of the Path API this app actually uses is here. Everything that
    touches the filesystem goes through the SFTP connection the path carries; the
    name-only parts are PurePosixPath's, because a remote host is always POSIX even
    when the app is driving it from Windows.

    There is deliberately no __fspath__. Handing one of these to open(), shutil or
    zipfile would otherwise silently read and write the *local* disk at the same
    path, which is the worst possible failure: quiet, and wrong. Without it those
    calls raise TypeError and name the line that has to be ported instead.
    """

    __slots__ = ("_fs", "_pure")

    def __init__(self, fs: SftpFs, path: str | PurePosixPath) -> None:
        self._fs = fs
        pure = PurePosixPath(path)
        # Server folders are always addressed absolutely; a relative one would be
        # resolved against whatever directory the SFTP session happens to sit in.
        if not pure.is_absolute():
            pure = PurePosixPath("/") / pure
        self._pure = pure

    # ------------------------------------------------------------- pure path parts
    @property
    def fs(self) -> SftpFs:
        return self._fs

    @property
    def pure(self) -> PurePosixPath:
        return self._pure

    @property
    def name(self) -> str:
        return self._pure.name

    @property
    def suffix(self) -> str:
        return self._pure.suffix

    @property
    def stem(self) -> str:
        return self._pure.stem

    @property
    def parts(self) -> tuple[str, ...]:
        return self._pure.parts

    @property
    def parent(self) -> RemotePath:
        return self._wrap(self._pure.parent)

    @property
    def parents(self) -> list[RemotePath]:
        return [self._wrap(p) for p in self._pure.parents]

    def _wrap(self, pure: PurePosixPath) -> RemotePath:
        return RemotePath(self._fs, pure)

    def joinpath(self, *other: str) -> RemotePath:
        return self._wrap(self._pure.joinpath(*other))

    def __truediv__(self, other: str) -> RemotePath:
        return self._wrap(self._pure / other)

    def with_suffix(self, suffix: str) -> RemotePath:
        return self._wrap(self._pure.with_suffix(suffix))

    def with_name(self, name: str) -> RemotePath:
        return self._wrap(self._pure.with_name(name))

    def relative_to(self, other: Any) -> PurePosixPath:
        return self._pure.relative_to(_pure_of(other))

    def is_relative_to(self, other: Any) -> bool:
        return self._pure.is_relative_to(_pure_of(other))

    def as_posix(self) -> str:
        return self._pure.as_posix()

    def is_absolute(self) -> bool:
        return True

    def __str__(self) -> str:
        return self._pure.as_posix()

    def __repr__(self) -> str:
        return f"RemotePath({self._fs.label!r}, {self._pure.as_posix()!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RemotePath):
            return NotImplemented
        return self._pure == other._pure and self._fs is other._fs

    def __hash__(self) -> int:
        return hash((id(self._fs), self._pure))

    # -------------------------------------------------------------- filesystem
    def resolve(self) -> RemotePath:
        return self._wrap(PurePosixPath(self._fs.normalize(str(self))))

    def exists(self) -> bool:
        return self._fs.lstat(str(self)) is not None

    def is_file(self) -> bool:
        info = self._fs.stat(str(self))
        return info is not None and stat_module.S_ISREG(info.st_mode)

    def is_dir(self) -> bool:
        info = self._fs.stat(str(self))
        return info is not None and stat_module.S_ISDIR(info.st_mode)

    def is_symlink(self) -> bool:
        info = self._fs.lstat(str(self))
        return info is not None and stat_module.S_ISLNK(info.st_mode)

    def is_fifo(self) -> bool:
        info = self._fs.lstat(str(self))
        return info is not None and stat_module.S_ISFIFO(info.st_mode)

    def stat(self) -> Any:
        info = self._fs.stat(str(self))
        if info is None:
            raise FileNotFoundError(str(self))
        return info

    def mkdir(self, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        self._fs.mkdir(str(self), mode=mode, parents=parents, exist_ok=exist_ok)

    def rmdir(self) -> None:
        self._fs.rmdir(str(self))

    def unlink(self, missing_ok: bool = False) -> None:
        self._fs.remove(str(self), missing_ok=missing_ok)

    def touch(self, exist_ok: bool = True) -> None:
        self._fs.touch(str(self), exist_ok=exist_ok)

    def rename(self, target: Any) -> RemotePath:
        self._fs.rename(str(self), str(target))
        return self._wrap(_pure_of(target))

    def replace(self, target: Any) -> RemotePath:
        return self.rename(target)

    def iterdir(self) -> Iterator[RemotePath]:
        for entry in self._fs.listdir(str(self)):
            yield self / entry

    def glob(self, pattern: str) -> Iterator[RemotePath]:
        yield from self._fs.glob(self, pattern, recursive=False)

    def rglob(self, pattern: str) -> Iterator[RemotePath]:
        yield from self._fs.glob(self, pattern, recursive=True)

    def read_bytes(self) -> bytes:
        return self._fs.read_bytes(str(self))

    def write_bytes(self, data: bytes) -> int:
        return self._fs.write_bytes(str(self), data)

    def read_text(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        return self.read_bytes().decode(encoding, errors)

    def write_text(self, data: str, encoding: str = "utf-8", errors: str = "strict") -> int:
        return self.write_bytes(data.encode(encoding, errors))

    def open(self, mode: str = "r", encoding: str | None = None, **_kwargs: Any) -> Any:
        return self._fs.open(str(self), mode, encoding=encoding)


def _pure_of(value: Any) -> PurePosixPath:
    if isinstance(value, RemotePath):
        return value.pure
    return PurePosixPath(str(value))


def is_remote(path: Any) -> bool:
    return isinstance(path, RemotePath)
