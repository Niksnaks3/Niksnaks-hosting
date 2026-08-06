from __future__ import annotations

import contextlib
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from niksnaks_hosting.shared.utils.constants import TEMP_DIR

# Flatpak hands the sandbox its own /tmp as a tmpfs sized at about half of RAM, so a
# modpack download can run out of room while the disk is nearly empty. Scratch space
# therefore lives beside the servers instead, where the only limit is the real disk.

MIN_INSTALL_FREE_BYTES = 1024**3

# An archive costs its own size to download, roughly its size again once unpacked, and
# a little more for the mods and libraries the install pulls in afterwards.
INSTALL_SIZE_FACTOR = 4

STALE_SCRATCH_SECONDS = 24 * 60 * 60


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def free_bytes(path: Path | str) -> int:
    """Free space on the filesystem holding *path*, or its nearest existing parent."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def install_space_needed(archive_bytes: int) -> int:
    return max(MIN_INSTALL_FREE_BYTES, archive_bytes * INSTALL_SIZE_FACTOR)


def _prune_stale_scratch() -> None:
    """Drop leftovers from a run that was killed before its temp dir was cleaned up."""
    cutoff = time.time() - STALE_SCRATCH_SECONDS
    try:
        entries = list(TEMP_DIR.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.stat().st_mtime > cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


@contextlib.contextmanager
def scratch_dir(prefix: str) -> Iterator[Path]:
    """A temporary directory on the app's own storage rather than the sandbox tmpfs.

    Keeping it on the same filesystem as the servers also turns the final move into a
    rename instead of a copy.
    """
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    _prune_stale_scratch()
    with tempfile.TemporaryDirectory(prefix=prefix, dir=TEMP_DIR) as td:
        yield Path(td)
