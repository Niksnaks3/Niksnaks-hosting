"""Who is currently driving a server, written down where the other app can see it.

Adoption makes it normal for two installs of this app to be pointed at the same server
folder: the one on the host that created it, and this one. That is fine -- the service
manager owns the process, and starts and stops are idempotent -- right up until both of
them start it at once, which puts two JVMs on one world directory and corrupts it.

RemoteService.foreign_process_pid() catches the collision that has already happened. This
catches the one that is about to: a marker in the server folder, refreshed while the
server runs, saying which install is looking after it. A marker nobody has touched for a
few minutes is left over from an app that was closed, and means nothing.

The marker answers a second question too. A server started by another copy of this app is
reachable only through the inbox in its folder, and only because that copy is watching
the inbox and typing what arrives into the process. Whether anyone is doing that is not
something the folder shows on its own -- an unread inbox looks exactly like one that has
not been read *yet* -- so the marker says it, and a stop that would otherwise sit there
unanswered until the world got killed can say plainly that nothing is listening.

It is advisory on purpose. Nothing here can stop a determined second app, and a lock file
that outlived a crash would be worse than the problem -- a server that cannot be started
and no obvious way to say it is fine.
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any

from niksnaks_hosting.shared.utils.constants import DATA_DIR

OWNER_FILE = "niksnaks-owner.json"

# Identifies this installation, not this run: closing and reopening the app has to look
# like the same owner, or every restart would look like a second app appearing.
INSTALL_ID_FILE = DATA_DIR / "install-id"

# How often a running server's marker is touched, and how long a marker stays meaningful
# after that. The gap between them is generous because a laptop that slept through a
# refresh has not stopped owning anything.
HEARTBEAT_SECONDS = 30.0
STALE_AFTER_SECONDS = 300.0

_cached_id: str = ""


@dataclass
class Owner:
    """What the marker in a server folder says."""

    install_id: str
    label: str
    at: float
    # Whether this owner is watching the folder's inbox and typing what arrives into
    # the server. Everything that writes a marker does, so this is only ever false for
    # a marker left by a release that did not have the console channel at all.
    console: bool = False
    pid: int = 0

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.at)

    @property
    def fresh(self) -> bool:
        return self.age < STALE_AFTER_SECONDS

    @property
    def is_us(self) -> bool:
        return self.install_id == install_id()


def install_id() -> str:
    """A stable id for this installation, made once and kept."""
    global _cached_id
    if _cached_id:
        return _cached_id
    try:
        if INSTALL_ID_FILE.is_file():
            text = INSTALL_ID_FILE.read_text(encoding="ascii").strip()
            if text:
                _cached_id = text
                return _cached_id
    except Exception:
        pass

    _cached_id = uuid.uuid4().hex
    try:
        INSTALL_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        INSTALL_ID_FILE.write_text(_cached_id, encoding="ascii")
    except Exception:
        # An id that cannot be saved still works for this run; the only cost is that the
        # next run looks like a different app, which is the safe direction to be wrong in.
        pass
    return _cached_id


def install_label() -> str:
    """Something a person reading the warning can act on: which machine is the other one."""
    try:
        machine = socket.gethostname() or ""
    except Exception:
        machine = ""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if machine and user:
        return f"{user} on {machine}"
    return machine or user or _("another computer")


def read_owner(server_dir: Any) -> Owner | None:
    """Whatever the marker says, or None if there is not one that can be read.

    *server_dir* is the folder itself -- a Path for a server on this machine, a
    RemotePath for one on a host. Both answer read_text the same way, which is what
    lets one copy of this file serve a local server and a remote one.
    """
    try:
        data = json.loads((server_dir / OWNER_FILE).read_text(errors="replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Owner(
            install_id=str(data.get("install_id", "") or ""),
            label=str(data.get("label", "") or ""),
            at=float(data.get("at") or 0.0),
            console=bool(data.get("console", False)),
            pid=int(data.get("pid") or 0),
        )
    except (TypeError, ValueError):
        return None


def other_owner(server_dir: Any) -> Owner | None:
    """A different install that has been looking after this server recently."""
    owner = read_owner(server_dir)
    if owner is None or owner.is_us or not owner.fresh or not owner.install_id:
        return None
    return owner


def console_is_watched(server_dir: Any) -> bool:
    """Whether some copy of this app is currently reading this folder's inbox.

    The question a stop has to ask before it sends one: a line appended to the inbox of
    a server nobody is watching sits there unread, and waiting out the grace period on
    that only ends in a server being killed with its world half written.
    """
    owner = read_owner(server_dir)
    return bool(owner and owner.fresh and owner.console)


def claim(server_dir: Any, pid: int = 0) -> None:
    """Say this app is looking after the server now, and stamp the time."""
    payload = {
        "install_id": install_id(),
        "label": install_label(),
        "at": time.time(),
        # Everything that writes a marker is watching the inbox by the time it does.
        "console": True,
        "pid": int(pid or 0),
    }
    try:
        (server_dir / OWNER_FILE).write_text(json.dumps(payload, indent=2))
    except Exception:
        # Not being able to write the marker is not a reason to refuse to run a server.
        pass


def release(server_dir: Any) -> None:
    """Stop claiming it, so another app sees the folder as free straight away.

    Only this app's own marker is removed. Deleting someone else's would be the one way
    this file could make the collision it exists to prevent more likely.
    """
    try:
        owner = read_owner(server_dir)
        if owner is not None and not owner.is_us:
            return
        (server_dir / OWNER_FILE).unlink(missing_ok=True)
    except Exception:
        pass
