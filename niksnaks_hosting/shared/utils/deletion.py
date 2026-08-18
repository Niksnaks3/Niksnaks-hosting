"""Deletion that actually deletes.

shutil.rmtree(ignore_errors=True) reports nothing and, on Windows, leaves things
behind routinely: a file carrying the read-only attribute refuses deletion outright,
and a file still held open -- by the Java process that only just died, by the
antivirus scanning it, by the search indexer -- refuses it for a moment longer than
the one attempt it gets. robust_rmtree strips the read-only bit and retries through
those windows.

The other way files survive a delete is the app quitting first: the undo toast holds
the real deletion back for a few seconds, and quitting inside that window used to
orphan the files forever. Paths waiting to be deleted are therefore journaled, and
the journal is replayed on the next launch (and flushed once more at quit).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from niksnaks_hosting.shared.utils.constants import DATA_DIR

PENDING_DELETIONS_FILE = DATA_DIR / "pending-deletions.json"

# Where the Files view parks a deleted file for the length of its undo toast. Content
# only lives here during that window, so whatever a launch finds inside is an
# interrupted deletion and gets finished.
TRASH_DIR_NAME = ".niksnaks-hosting-trash"

# Pauses between attempts: long enough in total (~4s) to outlive a virus scanner or
# indexer holding a file, short enough not to feel like a hang.
_RETRY_DELAYS = (0.2, 0.5, 1.0, 2.3)

_journal_lock = threading.Lock()


def _os_path(path: Path | str) -> str:
    """*path* as the OS calls want it; on Windows in extended-length form.

    Modpacks nest libraries deep enough that a server directory can hold paths past
    MAX_PATH, which the plain form cannot delete; the \\\\?\\ form is exempt.
    """
    p = os.path.abspath(str(path))
    if sys.platform == "win32" and not p.startswith("\\\\?\\"):
        p = "\\\\?\\UNC" + p[1:] if p.startswith("\\\\") else "\\\\?\\" + p
    return p


def _canonical(path: Path | str) -> str:
    """One spelling per path, so journal entries can be compared."""
    return os.path.normcase(os.path.abspath(str(path)))


def _make_writable_and_retry(func, failed_path, exc) -> None:
    """rmtree onexc handler. Read-only entries refuse deletion on Windows, and an
    unwritable parent directory refuses it everywhere; take ownership of both and
    try the failed operation once more. Failures are left for the retry loop."""
    if isinstance(exc, FileNotFoundError):
        return
    for target in (failed_path, os.path.dirname(failed_path)):
        try:
            # chmod follows symlinks, and a link's target may live outside the tree
            # being deleted; a failing link is always the parent's problem anyway.
            if os.path.islink(target):
                continue
            os.chmod(target, stat.S_IRWXU)
        except OSError:
            continue
        try:
            func(failed_path)
            return
        except OSError:
            continue


def _delete_single(target: str) -> None:
    try:
        os.unlink(target)
    except FileNotFoundError:
        pass
    except OSError:
        _make_writable_and_retry(os.unlink, target, None)


def robust_rmtree(path: Path | str, max_attempts: int = 1 + len(_RETRY_DELAYS)) -> bool:
    """Remove a file or a whole tree and report whether it is really gone.

    Never raises. A symlink is unlinked, not followed, so a link inside a trash
    folder cannot reach out and delete what it points at.
    """
    target = _os_path(path)
    for attempt in range(max(1, max_attempts)):
        if attempt:
            time.sleep(_RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)])
        try:
            if os.path.islink(target) or not os.path.isdir(target):
                _delete_single(target)
            else:
                shutil.rmtree(target, onexc=_make_writable_and_retry)
        except OSError:
            pass
        if not os.path.lexists(target):
            return True
    return not os.path.lexists(target)


def _load_journal() -> list[str]:
    try:
        with open(PENDING_DELETIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, str) and entry.strip()]


def _store_journal(entries: list[str]) -> None:
    try:
        if entries:
            with open(PENDING_DELETIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2)
        else:
            PENDING_DELETIONS_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def register_pending_deletion(path: Path | str) -> None:
    """Promise that *path* will be deleted, surviving a quit or crash in between."""
    entry = os.path.abspath(str(path))
    with _journal_lock:
        entries = _load_journal()
        if not any(_canonical(e) == _canonical(entry) for e in entries):
            entries.append(entry)
            _store_journal(entries)


def unregister_pending_deletion(path: Path | str) -> None:
    """Take the promise back -- the deletion was undone."""
    canonical = _canonical(path)
    with _journal_lock:
        entries = _load_journal()
        remaining = [e for e in entries if _canonical(e) != canonical]
        if len(remaining) != len(entries):
            _store_journal(remaining)


def delete_registered(path: Path | str, max_attempts: int = 1 + len(_RETRY_DELAYS)) -> bool:
    """Delete *path* and, once it is really gone, clear its journal entry.

    On failure the entry stays, and the next launch tries again. Blocking; call
    from a worker thread.
    """
    if robust_rmtree(path, max_attempts=max_attempts):
        unregister_pending_deletion(path)
        return True
    return False


def purge_pending_deletions(live_server_dirs: Iterable[Path | str], max_attempts: int = 1 + len(_RETRY_DELAYS)) -> None:
    """Finish every deletion a previous run promised but never carried out.

    A journal entry that is -- or contains -- a live server's directory is dropped
    without deleting anything: the only way that happens is a delete that was
    undone. The trash folder inside each live server is emptied too, since nothing
    in there can still be undone. Blocking; call from a worker thread.
    """
    live = [_canonical(d) for d in live_server_dirs]

    # The trash folders are listed before the slow work starts: at launch that is
    # before the user can soft-delete anything, so a delete made later in the
    # session -- whose undo toast may still be up -- can never land in this sweep.
    trash_leftovers: list[Path] = []
    for server_dir in live_server_dirs:
        trash = Path(server_dir) / TRASH_DIR_NAME
        try:
            trash_leftovers.extend(trash.iterdir())
        except OSError:
            continue

    with _journal_lock:
        entries = _load_journal()

    settled: set[str] = set()
    for entry in entries:
        canonical = _canonical(entry)
        if any(canonical == d or d.startswith(canonical + os.sep) for d in live):
            settled.add(canonical)
        elif robust_rmtree(entry, max_attempts=max_attempts):
            settled.add(canonical)

    if settled:
        # Deletions registered while this ran must survive, so re-read and remove
        # only what was actually dealt with.
        with _journal_lock:
            current = _load_journal()
            _store_journal([e for e in current if _canonical(e) not in settled])

    for item in trash_leftovers:
        robust_rmtree(item, max_attempts=max_attempts)
