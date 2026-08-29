"""Take over the servers a copy of this app on the far machine is already managing.

If Niksnaks-Hosting is installed over there, it has a servers.json listing everything it
looks after. Reading that is enough to control those servers from here: the folders are
already on the host, the unit or task is written on every start, and nothing has to be
moved or converted.

Nothing is written to the far machine by discovery. Importing writes only to this app's
own servers.json -- the host's copy is left exactly as it was, still listing the same
servers, still able to start them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from niksnaks_hosting.shared.utils.constants import APP_ID

if TYPE_CHECKING:
    from niksnaks_hosting.shared.backend.server_manager import ServerManager
    from niksnaks_hosting.shared.remote.host import SshHost

CONFIG_NAME = "servers.json"

# Where a Niksnaks-Hosting install keeps its data, per platform, relative to the login
# user's home. NIKSNAKS_HOSTING_DATA_DIR cannot be consulted: an environment variable set
# for the desktop session is not visible to a non-interactive SSH session, so a host that
# uses one has to be pointed at the folder by hand.
#
# The Flatpak comes first because it is how this app is normally installed on Linux, and
# it is the one case where the app's own home directory is not the login user's: Flatpak
# redirects HOME into ~/.var/app/<id>, so the folder XDG puts the data in is nowhere near
# where an SSH session looking at ~/.local/share would find it.
DATA_DIR_CANDIDATES = {
    "posix": (
        f".var/app/{APP_ID}/data/niksnaks-hosting",
        ".local/share/niksnaks-hosting",
        # A Mac reports itself as POSIX here, and keeps its data somewhere else again.
        "Library/Application Support/Niksnaks-Hosting",
    ),
    "windows": ("AppData/Local/Niksnaks-Hosting",),
}


@dataclass
class RemoteServerCandidate:
    """One server the far machine's copy of this app is managing."""

    remote_id: str
    name: str
    path: str
    mc_version: str = ""
    edition: str = ""
    loader_type: str = ""
    loader_version: str = ""
    ram_mb: int = 0
    java_version: int = 0
    jvm_args: str = ""
    autostart: bool = False
    created_at: str = ""

    # Worked out here rather than by the dialog, so the rules live in one place.
    folder_exists: bool = True
    already_imported: bool = False
    refused: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def importable(self) -> bool:
        return not self.refused and not self.already_imported

    @property
    def summary(self) -> str:
        bits = [b for b in (self.mc_version, self.loader_type) if b]
        return " · ".join(bits) if bits else self.path


def remote_data_dirs(host: SshHost) -> list[str]:
    """Where to look for the far app's data, most likely first."""
    platform = getattr(host.ops, "platform", "posix")
    home = host.home
    return [f"{home}/{tail}" for tail in DATA_DIR_CANDIDATES.get(platform, DATA_DIR_CANDIDATES["posix"])]


def remote_data_dir(host: SshHost) -> str:
    """The folder the far machine's copy of this app keeps its data in, or ''.

    Two passes, because "installed" is a stronger claim than "there is a folder with the
    right name". An install that has been run has a servers.json, even an empty one; a
    bare directory is what a half-removed Flatpak leaves behind, and picking that one
    would have new servers written where the app that is actually in use never looks.
    """
    folders = remote_data_dirs(host)
    for folder in folders:
        try:
            if host.path(f"{folder}/{CONFIG_NAME}").is_file():
                return folder
        except Exception:
            continue
    for folder in folders:
        try:
            if host.path(folder).is_dir():
                return folder
        except Exception:
            continue
    return ""


def discover_remote_servers(
    manager: ServerManager, host: SshHost, data_dir: str = ""
) -> tuple[list[RemoteServerCandidate], str]:
    """Read the far app's servers.json. Returns what was found and where it was found.

    An empty list with a folder named back means the app is installed there but manages
    nothing; an empty list with an empty folder means it was not found at all, and the
    caller should offer to be pointed at it.
    """
    candidates = [data_dir] if data_dir else remote_data_dirs(host)

    # A machine can have more than one of these -- a Flatpak tried once and a native
    # install kept, say -- and the one that matters is the one with servers in it. The
    # first that could be read is remembered so "installed but managing nothing" is
    # still told apart from "not installed at all".
    empty_folder = ""
    for folder in candidates:
        config = host.path(f"{folder}/{CONFIG_NAME}")
        try:
            if not config.is_file():
                continue
            data = json.loads(config.read_text(errors="replace"))
        except Exception:
            continue

        entries = data.get("servers", []) if isinstance(data, dict) else []
        if not entries:
            empty_folder = empty_folder or folder
            continue

        candidates_out: list[RemoteServerCandidate] = []
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            try:
                candidates_out.append(_classify(manager, host, raw))
            except Exception:
                # One malformed entry -- a non-numeric ram_mb, for instance -- must not
                # take the rest of the far app's server list down with it.
                remote_id = str(raw.get("id", "") or "")
                bad = RemoteServerCandidate(
                    remote_id=remote_id,
                    name=str(raw.get("name", "") or remote_id or _("Unnamed server")),
                    path=str(raw.get("path", "") or ""),
                    raw=raw,
                )
                bad.refused = _("Its entry on {} could not be read.").format(host.name)
                candidates_out.append(bad)
        return candidates_out, folder

    return [], empty_folder


def _classify(manager: ServerManager, host: SshHost, raw: dict[str, Any]) -> RemoteServerCandidate:
    remote_id = str(raw.get("id", "") or "")
    path = str(raw.get("path", "") or "")

    candidate = RemoteServerCandidate(
        remote_id=remote_id,
        name=str(raw.get("name", "") or remote_id or _("Unnamed server")),
        path=path,
        mc_version=str(raw.get("mc_version", "") or ""),
        edition=str(raw.get("edition", "") or ""),
        loader_type=str(raw.get("loader_type", "") or ""),
        loader_version=str(raw.get("loader_version", "") or ""),
        ram_mb=int(raw.get("ram_mb") or 0),
        java_version=int(raw.get("java_version") or 0),
        jvm_args=str(raw.get("jvm_args", "") or ""),
        autostart=bool(raw.get("autostart", False)),
        created_at=str(raw.get("created_at", "") or ""),
        raw=raw,
    )

    if not remote_id or not path:
        candidate.refused = _("Its entry on {} is incomplete.").format(host.name)
        return candidate

    # A record that already points somewhere else means the far app is itself reaching a
    # third machine. Importing it would hand this app a path on a computer it holds no
    # credentials for and cannot reach.
    if str(raw.get("host_id", "") or ""):
        candidate.refused = _(
            "{} is managing this server on a third computer, which this app cannot reach."
        ).format(host.name)
        return candidate

    try:
        candidate.folder_exists = host.path(path).is_dir()
    except Exception:
        candidate.folder_exists = False
    if not candidate.folder_exists:
        candidate.refused = _("Its folder is not on {} any more.").format(host.name)
        return candidate

    for existing in manager.servers:
        if existing.host_id == host.id and existing.unit_id == remote_id:
            candidate.already_imported = True
            break

    return candidate


def import_remote_servers(
    manager: ServerManager, host: SshHost, chosen: list[RemoteServerCandidate]
) -> tuple[list[str], list[str]]:
    """Add the chosen servers to this app. Returns (imported ids, problems).

    The id is kept as it is wherever it can be: the unit name and the folder on the host
    are both built from it, so keeping it means this app and the far one agree about
    which server is which. Only a genuine collision with a different local server forces
    a new id, and then the original is written down as remote_id so everything sent to
    the host still uses the name the host knows.
    """
    from niksnaks_hosting.shared.backend.server_manager import ServerInfo

    imported: list[str] = []
    problems: list[str] = []

    for candidate in chosen:
        if not candidate.importable:
            continue

        taken = manager.get_server(candidate.remote_id) is not None
        local_id = _fresh_id(manager) if taken else candidate.remote_id

        record = dict(candidate.raw)
        record["id"] = local_id
        record["host_id"] = host.id
        record["remote_id"] = candidate.remote_id if taken else ""
        record["path"] = candidate.path
        # Autostart on the host is the far app's business until this one is asked for it.
        record["autostart"] = False

        try:
            info = ServerInfo(record)
            manager.attach_server(info)
        except Exception as e:
            problems.append(_("{}: {}").format(candidate.name, e))
            continue

        imported.append(local_id)

    return imported, problems


def _fresh_id(manager: ServerManager) -> str:
    import uuid

    while True:
        candidate = str(uuid.uuid4())
        if manager.get_server(candidate) is None:
            return candidate
