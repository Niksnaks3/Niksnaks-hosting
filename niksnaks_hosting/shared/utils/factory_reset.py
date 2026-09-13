"""Put this install back the way it came, without reaching onto anybody else's machine.

A reset deletes the servers on this computer -- their worlds, their mods, every backup
of them -- along with the settings, the cached downloads and the Java runtimes the app
has collected since it was installed. It is the one thing here with no undo toast behind
it, which is why the button that starts it says so in red and asks twice.

What it deliberately does not delete is anything on a host. Those servers are on a real
machine somewhere, very likely with people on it, and the folder belongs to that machine
rather than to this app. They are forgotten exactly the way removing a host forgets them:
the unit or task keeping each one alive goes on running, the folder is left alone, the
host's own copy of Niksnaks-Hosting still has it, and adding the machine again later
brings the lot back. The app's login key is handed back on the way out, because an
install that has been wiped has no business still holding a credential on somebody's
server.

The work is split the same way deleting one server is. Emptying the app's own records is
quick and happens on the main thread; deleting a modded server folder is not, and would
freeze the window for as long as it took, so that waits for a worker.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from niksnaks_hosting.shared.utils.constants import CACHE_DIR, DATA_DIR, JRES_DIR, SERVERS_DIR, TEMP_DIR
from niksnaks_hosting.shared.utils.deletion import (
    delete_registered,
    pending_deletions,
    register_pending_deletion,
    robust_rmtree,
)

# Reporting (fraction, title, detail), the shape ProgressDialog.update already takes.
ProgressFn = Callable[[float, str, str], None]

# The folders a first launch creates. They are emptied rather than removed: the app is
# running with constants.py imported long ago, so putting them back is what makes the
# data folder look like one the app has just made for itself -- and a leftover inside
# one can be named and promised again, where a whole folder that would not go cannot.
_SKELETON = (SERVERS_DIR, JRES_DIR, CACHE_DIR, TEMP_DIR)

# Enough leftovers named in a message for somebody to go and look; past that the list
# stops being something anyone reads.
_MAX_NAMED_LEFTOVERS = 3


@dataclass
class ResetPlan:
    """What the records phase dealt with, and the folders the worker still has to delete."""

    server_dirs: list[Path] = field(default_factory=list)
    local_servers: int = 0
    remote_servers: int = 0


@dataclass
class ResetReport:
    """What survived a reset that was meant to leave nothing behind."""

    kept_keys: list[str] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.kept_keys and not self.leftovers


def prepare(manager, preferences) -> ResetPlan:
    """Empty the app's own records, and promise the folders that follow.

    Runs on the main thread, and everything in it is quick. Servers are killed rather
    than asked to save first: the world is about to be deleted, so there is nothing left
    worth waiting for. Their folders are only journaled here -- the deleting itself is
    run()'s job -- which also means a crash between the two still leaves the promise
    behind for the next launch to finish.

    Settings are put back to their defaults first rather than last, so that anything
    still holding the preferences object while the wipe runs can only ever write a fresh
    install's values back out.
    """
    preferences.reset_to_defaults()

    # One blanket stop rather than one per server: a tunnel is a process on this
    # computer, and nothing is about to be left for it to point at.
    manager.playit_manager.stop()

    plan = ResetPlan()
    for info in list(manager.servers):
        if info.is_remote:
            # Not this app's to delete. forget_server leaves the unit or task, the
            # folder, and the host's own record of it all exactly as they are.
            manager.forget_server(info.id)
            plan.remote_servers += 1
            continue

        server_dir = info.server_dir
        # Journaled before the record goes, because the record is the only thing that
        # knows where a server kept outside the data folder actually is. Once it is
        # gone, the promise is all that would ever finish the job.
        register_pending_deletion(server_dir)
        manager.delete_server(info.id, delete_files=False)
        plan.server_dirs.append(server_dir)
        plan.local_servers += 1

    return plan


def run(plan: ResetPlan, hosts, progress: ProgressFn | None = None) -> ResetReport:
    """Delete the files, hand the login keys back, and empty the data folder.

    Blocking, and slow by design; call it from a worker thread with a progress dialog
    up. The order is the one clearing the host list already uses: every round trip to a
    machine happens while it is still a host, and the list is emptied only once nothing
    needs it again.
    """
    report = ResetReport()
    host_list = hosts.ssh_hosts() if hosts is not None else []
    steps = max(1, len(plan.server_dirs) + len(host_list) + 1)
    step = 0

    def say(title: str, detail: str) -> None:
        if progress is not None:
            progress(min(1.0, step / steps), title, detail)

    for server_dir in plan.server_dirs:
        say(_("Deleting servers"), _("Removing {}...").format(server_dir.name))
        if not delete_registered(server_dir):
            report.leftovers.append(str(server_dir))
        step += 1

    if host_list:
        # Imported here rather than at the top: a machine that never added a host has no
        # reason to load the SSH stack at all, and a build that shipped without paramiko
        # is no reason a reset should not still clear this computer.
        from niksnaks_hosting.shared.remote.key_auth import KEY_PATH, revoke_key

        for host in host_list:
            say(_("Forgetting hosts"), _("Taking this app's key back off {}...").format(host.name))
            # Only worth a round trip to a machine this app actually put its key on;
            # otherwise an unreachable host holds the reset open for a connect timeout
            # to revoke something that was never there.
            if host.credentials.key_path == str(KEY_PATH):
                try:
                    revoked = revoke_key(host)
                except Exception:
                    revoked = False
                if not revoked:
                    report.kept_keys.append(host.name)
            step += 1

    if hosts is not None:
        # Safe from here: the registry takes its own lock and emits on the main thread,
        # and nothing above needs a host to be listed any more.
        hosts.remove_all()

    say(_("Clearing settings and downloads"), str(DATA_DIR))
    report.leftovers.extend(wipe_data_dir())
    step += 1

    clear_autostart()

    # One path named once, however many passes tripped over it.
    report.leftovers = list(dict.fromkeys(report.leftovers))

    # Promised again now that the journal has been rebuilt. Whatever was locked a moment
    # ago -- the Java process that only just died, a scanner part way through a jar --
    # gets swept on the next launch, which is the same second chance deleting a single
    # server has always had.
    for path in report.leftovers:
        if os.path.lexists(path):
            register_pending_deletion(path)

    say(_("Finishing"), "")
    return report


def wipe_data_dir() -> list[str]:
    """Empty the app's data folder, leaving the skeleton a first launch would create.

    Returns whatever would not go, which is not an error so much as a Windows fact of
    life: a file somebody still has open survives, and gets promised to the next launch
    by the caller.
    """
    # Read before the sweep and put back after it. A promise to delete something outside
    # this folder -- a server somebody kept on another drive, still locked a moment ago
    # -- is the one thing in here worth surviving a wipe, because the journal is the
    # only record that it was ever meant to go.
    outstanding = [path for path in pending_deletions() if not _inside_data_dir(path)]

    leftovers: list[str] = []
    for entry in _entries(DATA_DIR):
        targets = _entries(entry) if entry in _SKELETON else [entry]
        for target in targets:
            if not robust_rmtree(target):
                leftovers.append(str(target))

    for folder in _SKELETON:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    for path in outstanding:
        if os.path.lexists(path):
            register_pending_deletion(path)

    return leftovers


def clear_autostart() -> None:
    """Take the app back out of Windows' list of things to start at login.

    The one piece of a reset that does not live in the data folder. Settings going back
    to their defaults say the app should not open on startup, but the registry value was
    written on some earlier launch and would sit there until another one happened --
    which a reset does not get to assume will ever come. On Linux the same setting is
    the portal's, and asking it anything here would put a permission dialog in the
    middle of a wipe, so that one is left to the next launch to put right.
    """
    if sys.platform != "win32":
        return
    try:
        from niksnaks_hosting.shared.utils.portal import request_background

        request_background(False, lambda *_args: None)
    except Exception:
        pass


def describe_leftovers(paths: list[str]) -> str:
    """The leftover paths as something worth reading, however many of them there are."""
    named = ", ".join(paths[:_MAX_NAMED_LEFTOVERS])
    extra = len(paths) - _MAX_NAMED_LEFTOVERS
    if extra <= 0:
        return named
    if extra == 1:
        return _("{}, and 1 more").format(named)
    return _("{}, and {} more").format(named, extra)


def _entries(folder: Path) -> list[Path]:
    try:
        return list(folder.iterdir())
    except OSError:
        return []


def _inside_data_dir(path: Path | str) -> bool:
    """Whether the wipe below is already going to take care of *path*."""
    root = os.path.normcase(os.path.abspath(str(DATA_DIR)))
    candidate = os.path.normcase(os.path.abspath(str(path)))
    return candidate == root or candidate.startswith(root + os.sep)
