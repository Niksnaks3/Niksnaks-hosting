"""Tell the far machine's own copy of this app about the servers made on it from here.

A server this app creates on a host is a real server on that machine: the folder is
there, the unit is there, and the copy of this app installed over there could run it
perfectly well -- it simply does not know it exists, because the only record of it is in
this app's servers.json. This writes that record into the host's servers.json too, so a
server made from here shows up over there like any other, and either end can start it,
stop it and watch its console.

Only servers this app made on the host are written. One that came the other way, taken
in from the host's own list, already has a record over there that belongs to the far
app; overwriting it with this app's copy would quietly undo things that app owns, such
as whether it starts the server at login.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Any

from niksnaks_hosting.shared.remote.remote_adopt import CONFIG_NAME, remote_data_dir

if TYPE_CHECKING:
    from niksnaks_hosting.shared.remote.host import SshHost

# Where each host keeps its data. Finding it is several round trips, and it does not
# move while the app is open -- so a folder that was found is remembered for good, and
# "there is no install over there" only until the window below has passed. That machine
# may be having this app installed on it right now, and re-asking every few minutes
# costs nothing next to leaving it out of everything for the rest of the session.
_data_dirs: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()

RETRY_MISSING_SECONDS = 300.0


def host_data_dir(host: SshHost) -> str:
    """The folder the far machine's copy of this app keeps its data in, or ''."""
    with _lock:
        found = _data_dirs.get(host.id)
    if found is not None:
        folder, at = found
        if folder or time.monotonic() - at < RETRY_MISSING_SECONDS:
            return folder

    folder = remote_data_dir(host)
    with _lock:
        _data_dirs[host.id] = (folder, time.monotonic())
    return folder


def forget_host_data_dir(host_id: str) -> None:
    with _lock:
        _data_dirs.pop(host_id, None)


def publish_server(host: SshHost, record: dict[str, Any]) -> bool:
    """Put *record* in the host's servers.json, replacing any entry with the same id.

    False when the host has no copy of this app to tell, which is not a failure: plenty
    of hosts are a bare machine with an SSH login and nothing else on them.
    """
    server_id = str(record.get("id", "") or "")
    if not server_id:
        return False
    folder = host_data_dir(host)
    if not folder:
        return False

    config = host.path(f"{folder}/{CONFIG_NAME}")
    data = _read(config)
    servers = [entry for entry in data["servers"] if str(entry.get("id", "")) != server_id]
    servers.append(record)
    data["servers"] = servers
    _write(config, data)
    return True


def withdraw_server(host: SshHost, server_id: str) -> bool:
    """Take a server out of the host's servers.json, for one deleted from here."""
    if not server_id:
        return False
    folder = host_data_dir(host)
    if not folder:
        return False

    config = host.path(f"{folder}/{CONFIG_NAME}")
    data = _read(config)
    kept = [entry for entry in data["servers"] if str(entry.get("id", "")) != server_id]
    if len(kept) == len(data["servers"]):
        return False
    data["servers"] = kept
    _write(config, data)
    return True


def _read(config: Any) -> dict[str, Any]:
    """The host's list as it stands. A file that cannot be read is treated as empty.

    Read afresh every time rather than remembered: the far app rewrites this file
    whenever anybody renames a server over there, and writing back a copy from minutes
    ago would undo it.
    """
    try:
        data = json.loads(config.read_text(errors="replace"))
    except Exception:
        return {"servers": []}
    if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
        return {"servers": []}
    data["servers"] = [entry for entry in data["servers"] if isinstance(entry, dict)]
    return data


def _write(config: Any, data: dict[str, Any]) -> None:
    """Write the list beside the real file and move it into place.

    The far app re-reads this file whenever it changes, and a write it caught halfway
    would read as every server on the machine having been deleted at once.
    """
    text = json.dumps(data, indent=2)
    config.parent.mkdir(parents=True, exist_ok=True)
    temp = config.with_name(CONFIG_NAME + ".new")
    temp.write_text(text)
    temp.rename(config)
