import json
import os
import re
import shutil
import sys
import threading
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from niksnaks_hosting.shared.backend.bedrock_manager import BedrockManager, BedrockVersionOption
from niksnaks_hosting.shared.backend.config_manager import ConfigManager
from niksnaks_hosting.shared.backend.download_manager import DownloadManager
from niksnaks_hosting.shared.backend.java_manager import JavaManager
from niksnaks_hosting.shared.backend.playit_config import load_playit_config
from niksnaks_hosting.shared.backend.playit_manager import PlayitManager
from niksnaks_hosting.shared.backend.preferences_manager import PreferencesManager
from niksnaks_hosting.shared.backend.server_process import ServerProcess, running_server_pids
from niksnaks_hosting.shared.core.events import EventEmitter
from niksnaks_hosting.shared.utils.constants import (
    BEDROCK_BINARY_NAMES,
    BEDROCK_DEFAULT_PORT,
    BEDROCK_SERVER_BINARY,
    BEDROCK_WORLDS_DIR,
    CONFIG_FILE,
    DEFAULT_RAM_MB,
    EDITION_BEDROCK,
    EDITION_JAVA,
    LOADER_FABRIC,
    LOADER_FORGE,
    LOADER_NEOFORGE,
    MAX_SERVER_PORT,
    MIN_SERVER_PORT,
    SERVERS_DIR,
    USER_ICON_FILE,
    default_port_for_edition,
    get_forge_full_version,
    get_required_java_version,
    get_loader_display_name,
    is_bedrock,
    normalize_edition,
    normalize_loader,
    ports_bound_by,
    uses_installer,
)
from niksnaks_hosting.shared.utils.deletion import delete_registered, register_pending_deletion, robust_rmtree
from niksnaks_hosting.shared.utils.migration import BACKUPS_DIR, LEGACY_BACKUPS_DIR
from niksnaks_hosting.shared.utils.storage import free_bytes, human_size, install_space_needed, scratch_dir

# Where servers go on a remote host: under the login user's home, so no privileges
# beyond the account itself are ever needed.
REMOTE_SERVERS_DIR = "niksnaks-hosting/servers"

BACKUP_DIR_NAMES = (BACKUPS_DIR, LEGACY_BACKUPS_DIR)
FULL_BACKUP_PREFIXES = ("niksnaks-hosting-full-backup-", "hosty-full-backup-")
FULL_BACKUP_NAME_RE = r"^(?:niksnaks-hosting|hosty)-full-backup-(.+)-\d{8}-\d{6}\.zip$"

# Bedrock ships worlds as .mcworld; our own exports and backups are plain .zip.
WORLD_ARCHIVE_SUFFIXES = (".mcworld", ".zip")

# What stop_all allows a server when there is no longer a main loop to wait on. The
# unhurried path is shutdown_all_gracefully, which uses the process default instead.
EMERGENCY_STOP_SECONDS = 10.0

def _restore_exec_bits(archive: zipfile.ZipFile, destination: Path) -> None:
    """Re-apply execute permissions, which zipfile.extractall drops.

    A Bedrock server restored from a backup is unlaunchable without this.
    """
    if sys.platform == "win32":
        return
    for entry in archive.infolist():
        if entry.is_dir() or not (entry.external_attr >> 16) & 0o111:
            continue
        target = destination / entry.filename
        try:
            target.chmod(target.stat().st_mode | 0o111)
        except OSError:
            continue

def _safe_extract_zip(
    archive: Path,
    destination: Path,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> None:
    """Extract *archive* into *destination*, refusing entries that escape it.

    With *on_progress* the members come out one at a time so the caller can report
    (done, total, name); extractall is faster but says nothing while it works, and a
    world of a few gigabytes takes long enough that silence reads as a hang.
    """
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive, "r") as zf:
        entries = zf.infolist()
        for entry in entries:
            if not (root / entry.filename).resolve().is_relative_to(root):
                raise ValueError(_("Archive contains invalid paths."))

        if on_progress is None:
            zf.extractall(root)
        else:
            total = len(entries)
            for done, entry in enumerate(entries, start=1):
                zf.extract(entry, root)
                on_progress(done, total, entry.filename)

        _restore_exec_bits(zf, root)

def _copy_tree_with_progress(
    source: Path,
    destination: Path,
    on_file: Callable[[str], None] | None = None,
) -> None:
    """Copy a file or a whole directory, announcing each file as it lands.

    shutil.copytree does the same job without the per-file callback, which is what
    makes a restore look frozen for the minute or two it runs. Descending a level at a
    time keeps every destination built from its own parent, because a staging path that
    mixes separators is not something relative_to can be trusted to take apart.
    """
    if not source.is_dir():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if on_file:
            on_file(source.name)
        return

    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        _copy_tree_with_progress(item, destination / item.name, on_file=on_file)

def _count_files(root: Path) -> int:
    """Files under *root*, for turning a copy loop into a fraction."""
    return sum(len(files) for _parent, _subdirs, files in os.walk(root))

@dataclass(frozen=True)
class FullBackupOption:
    """A full backup on disk that a new server can be built from."""

    path: Path
    server_id: str
    server_name: str
    edition: str
    mc_version: str
    created_at: datetime

    @property
    def label(self) -> str:
        stamp = self.created_at.strftime("%Y-%m-%d %H:%M")
        version = self.mc_version or _("unknown version")
        return f"{self.server_name} · {version} · {stamp}"

class ServerInfo:
    def __init__(self, data: dict):
        self.id: str = data.get("id", str(uuid.uuid4()))
        self.name: str = data.get("name", _("Unnamed Server"))
        self.mc_version: str = data.get("mc_version", "")

        # Servers created before Bedrock support existed have no edition stored.
        self.edition: str = normalize_edition(data.get("edition", EDITION_JAVA))
        self.loader_type: str = normalize_loader(data.get("loader_type", LOADER_FABRIC))
        self.loader_version: str = data.get("loader_version", "")

        stored_ram = int(data.get("ram_mb") or 0)
        self.ram_mb: int = stored_ram if stored_ram > 0 else DEFAULT_RAM_MB
        self.java_version: int = data.get("java_version", 21)
        self.jvm_args: str = data.get("jvm_args", "")
        self.icon_path: str = data.get("icon_path", "")
        self.created_at: str = data.get("created_at", datetime.now().isoformat())
        self.path: str = data.get("path", "")
        self.autostart: bool = data.get("autostart", False)
        # Empty means this computer, which is what every server made before SSH
        # support existed was, so old entries need no migrating.
        self.host_id: str = str(data.get("host_id", "") or "")
        # Set only for a server adopted from a copy of this app already installed on the
        # host, where that id was already taken locally. The unit name and the folder on
        # the host are built from this, so the two apps agree about which server is which.
        self.remote_id: str = str(data.get("remote_id", "") or "")
        # True for a server this app made on a host, which means the copy of this app
        # over there is told about it and kept up to date. False for one that came the
        # other way: that record is the far app's, and writing over it would undo
        # settings it owns.
        self.mirrored: bool = bool(data.get("mirrored", False))

    @property
    def is_remote(self) -> bool:
        return bool(self.host_id)

    @property
    def unit_id(self) -> str:
        """The id the host knows this server by, which is not always the local one."""
        return self.remote_id or self.id

    @property
    def host(self):
        """The machine this server lives on."""
        from niksnaks_hosting.shared.remote.hosts_manager import get_hosts_manager

        return get_hosts_manager().require(self.host_id)

    @property
    def server_dir(self) -> Path:
        """The server's folder, on whichever machine that is.

        For a remote server this is a RemotePath, which answers the same questions a
        Path does but over SFTP. That is what lets the code above this line stay the
        same for both kinds of server.
        """
        if self.is_remote:
            return self.host.path(self.path or f"{self.host.home}/{REMOTE_SERVERS_DIR}/{self.id}")
        if self.path:
            return Path(self.path)
        return SERVERS_DIR / self.id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "mc_version": self.mc_version,
            "edition": self.edition,
            "loader_type": self.loader_type,
            "loader_version": self.loader_version,
            "ram_mb": self.ram_mb,
            "java_version": self.java_version,
            "jvm_args": self.jvm_args,
            "icon_path": self.icon_path,
            "created_at": self.created_at,
            "path": str(self.server_dir),
            "autostart": self.autostart,
            "host_id": self.host_id,
            "remote_id": self.remote_id,
            "mirrored": self.mirrored,
        }

def _config_stamp() -> tuple[int, int]:
    """Enough of servers.json to tell whether anything has written to it."""
    try:
        stat = CONFIG_FILE.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (0, 0)


def _disk_servers() -> list[dict]:
    """The entries in servers.json right now, whoever put them there."""
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        entries = data["servers"]
    except Exception:
        return []
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _adopt_fields(existing: ServerInfo, fresh: ServerInfo) -> None:
    """Take on what another copy of this app changed, in place.

    The object itself is kept rather than replaced: a server's process, its config and
    its row all hold a reference to it, and swapping it out would leave them pointed at
    a record nothing else updates. The id is never copied -- it is what makes these two
    the same server.
    """
    for field in (
        "name",
        "mc_version",
        "edition",
        "loader_type",
        "loader_version",
        "ram_mb",
        "java_version",
        "jvm_args",
        "icon_path",
        "created_at",
        "path",
        "autostart",
        "host_id",
        "remote_id",
        "mirrored",
    ):
        setattr(existing, field, getattr(fresh, field))


def _host_record(info: ServerInfo) -> dict:
    """The entry the machine a server lives on should keep for it in its own list."""
    record = info.to_dict()
    # Over there it is a server on this computer, run by the app that is reading this.
    record["id"] = info.unit_id
    record["host_id"] = ""
    record["remote_id"] = ""
    record.pop("mirrored", None)
    # Starting at boot is the unit's job, and it is already enabled for it. Asking the
    # far app to do it as well would have two things starting one world at once.
    record["autostart"] = False
    return record


class ServerManager(EventEmitter):
    def __init__(self):
        super().__init__()
        self._servers: dict[str, ServerInfo] = {}
        self._processes: dict[str, ServerProcess] = {}
        self._mods_operation_counts: dict[str, int] = {}
        self._remote_java: dict[str, object] = {}
        # A server's picture lives in its folder, which for a remote one is an SFTP read
        # per look. The sidebar looks every time a row is rebuilt, so the answer is kept.
        self._icon_cache: dict[str, bytes] = {}
        # Held for the length of one sweep over the remote servers, so a slow host
        # cannot leave rounds of it running on top of each other.
        self._remote_refresh = threading.Semaphore(1)
        # What the servers.json on disk held when this app last read or wrote it, and
        # which ids it has deliberately deleted. Both are needed to tell a server
        # another copy of this app has just added from one this app has just removed:
        # on disk they look identical, and the two want opposite treatment.
        self._config_stamp: tuple[int, int] = (0, 0)
        self._disk_ids: set[str] = set()
        self._forgotten: set[str] = set()
        # The record each host has been given for the servers made on it, so a change
        # that did not touch anything the host cares about costs no round trips.
        self._mirrored: dict[str, str] = {}
        self._mirror_queue: set[str] = set()
        self._mirror_lock = threading.Lock()
        self._mirror_busy = False
        # get_process is called from the main thread and from the sweep above, and two
        # callers building the same server's process would leave one of them orphaned
        # with a console reader running against a process nothing points at any more.
        self._process_lock = threading.RLock()
        self.java_manager = JavaManager()
        self.download_manager = DownloadManager()
        self.bedrock_manager = BedrockManager()
        self.playit_manager = PlayitManager()
        self.preferences = PreferencesManager()
        self._load()

    def _load(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    data = json.load(f)
                for entry in data.get("servers", []):
                    info = ServerInfo(entry)
                    self._servers[info.id] = info
                self._disk_ids = set(self._servers)
            except Exception as e:
                print(f"Failed to load servers: {e}")
        self._config_stamp = _config_stamp()

    def _save(self):
        """Write the list out, keeping whatever else is in the file.

        This machine's server list stopped being this app's alone the moment a copy on
        another computer could make a server here: that copy adds an entry to this very
        file, and this app may not have read it yet. Rewriting from memory alone would
        delete it, so an entry with an id this app has never had is carried through
        untouched and picked up properly by the next sweep.
        """
        # One snapshot for the whole write: a sweep on another thread can take a server
        # in or out while this runs, and half a list is worse than either version of it.
        listed = list(self._servers.values())
        records = [info.to_dict() for info in listed]
        known = {info.id for info in listed} | self._forgotten
        carried = False
        for entry in _disk_servers():
            entry_id = str(entry.get("id", "") or "")
            if entry_id and entry_id not in known:
                records.append(entry)
                carried = True

        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            # Written beside the real file and moved into place: another copy of this
            # app reads it whenever it changes, and a write caught halfway through
            # would read as every server on this machine having been deleted at once.
            staging = CONFIG_FILE.with_name(CONFIG_FILE.name + ".new")
            with open(staging, "w") as f:
                json.dump({"servers": records}, f, indent=2)
            os.replace(staging, CONFIG_FILE)
            self._disk_ids = {str(r.get("id", "") or "") for r in records}
            # Normally this app has just written what it already knows, and re-reading
            # its own file would be work for nothing. Not when something came through
            # that it has not read yet: leaving the mark unset is what sends the next
            # sweep back to the file to pick that server up properly.
            self._config_stamp = (0, 0) if carried else _config_stamp()
        except Exception as e:
            print(f"Failed to save servers: {e}")

        self._mirror_to_hosts()

    def absorb_external_changes(self) -> bool:
        """Take in whatever another copy of this app has written to the server list.

        A copy on another computer adds an entry here when it makes a server on this
        machine, and takes it out again when the server is deleted. Reading the file
        back whenever it has changed underneath us is what makes a server made from over
        there appear in this window without anybody restarting anything.
        """
        stamp = _config_stamp()
        if stamp == self._config_stamp:
            return False
        self._config_stamp = stamp

        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            entries = data["servers"]
            if not isinstance(entries, list):
                return False
        except Exception:
            # An unreadable file says nothing about what exists. Treating it as an
            # empty list would empty the window.
            return False

        changed = False
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            server_id = str(entry.get("id", "") or "")
            if not server_id or server_id in self._forgotten:
                continue
            seen.add(server_id)
            try:
                fresh = ServerInfo(entry)
            except Exception:
                continue
            existing = self._servers.get(server_id)
            if existing is None:
                # Its own signal: a server that turned up because another copy of this
                # app made one here is not one somebody at this keyboard just asked
                # for, and the two want different treatment on screen.
                self._servers[server_id] = fresh
                self.emit_on_main_thread("server-appeared", server_id)
                changed = True
            elif existing.to_dict() != fresh.to_dict():
                _adopt_fields(existing, fresh)
                self._icon_cache.pop(server_id, None)
                self.emit_on_main_thread("server-changed", server_id)
                changed = True

        # Only entries that were in the file last time count as removed. One this app
        # has just added is missing for the opposite reason -- it has not been written
        # yet -- and dropping it would undo the thing that is in the middle of happening.
        for server_id in sorted(self._disk_ids - seen):
            if server_id not in self._servers:
                continue
            self._servers.pop(server_id, None)
            self._processes.pop(server_id, None)
            self._icon_cache.pop(server_id, None)
            self.emit_on_main_thread("server-removed", server_id)
            changed = True

        self._disk_ids = seen
        return changed

    # ------------------------------------------------------------ the other end
    def _mirror_to_hosts(self) -> None:
        """Tell each host's own copy of this app about the servers made on it from here.

        Off the thread that asked, always: this is an SSH round trip per host, and it
        happens on the back of ordinary things like renaming a server. A host that is
        asleep simply misses this round and is told on the next one.
        """
        ids = [info.id for info in list(self._servers.values()) if info.host_id and info.mirrored]
        if not ids:
            return
        with self._mirror_lock:
            self._mirror_queue.update(ids)
            if self._mirror_busy:
                return
            self._mirror_busy = True
        threading.Thread(target=self._mirror_worker, daemon=True).start()

    def _mirror_worker(self) -> None:
        from niksnaks_hosting.shared.remote.remote_registry import publish_server

        while True:
            with self._mirror_lock:
                if not self._mirror_queue:
                    self._mirror_busy = False
                    return
                server_id = self._mirror_queue.pop()

            info = self._servers.get(server_id)
            if info is None or not (info.host_id and info.mirrored):
                continue
            try:
                record = _host_record(info)
                written = json.dumps(record, sort_keys=True)
                if self._mirrored.get(server_id) == written:
                    continue
                if publish_server(info.host, record):
                    self._mirrored[server_id] = written
            except Exception:
                # Nothing here is worth interrupting the app for. The record is sent
                # again the next time anything changes or the next state sweep runs.
                continue

    def _withdraw_from_host(self, info: ServerInfo) -> None:
        """Take a deleted server out of the list its own machine keeps."""
        try:
            host, unit_id = info.host, info.unit_id
        except Exception:
            # The machine itself has been removed from this app, which takes its
            # servers with it. There is nothing left to tell.
            return

        def worker() -> None:
            from niksnaks_hosting.shared.remote.remote_registry import withdraw_server

            try:
                withdraw_server(host, unit_id)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    @property
    def servers(self) -> list[ServerInfo]:
        return sorted(self._servers.values(), key=lambda s: s.created_at)

    def get_server(self, server_id: str) -> ServerInfo | None:
        return self._servers.get(server_id)

    def add_server(
        self,
        name: str,
        mc_version: str,
        loader_version: str = "",
        ram_mb: int = DEFAULT_RAM_MB,
        java_version: int | None = None,
        loader_type: str = LOADER_FABRIC,
        edition: str = EDITION_JAVA,
        host_id: str = "",
    ) -> ServerInfo:

        server_id = str(uuid.uuid4())
        java_ver = java_version if java_version is not None else get_required_java_version(mc_version)

        if host_id:
            from niksnaks_hosting.shared.remote.hosts_manager import get_hosts_manager
            from niksnaks_hosting.shared.remote.remote_adopt import remote_data_dir

            host = get_hosts_manager().require(host_id)
            # Inside the far app's own data folder when there is one, so the copy of
            # this app over there can reach the server it is about to be told it has.
            # That matters most where it is least obvious: a Flatpak install can only
            # see its own data folder, and a server put in the login user's home would
            # appear in its list as a folder it is not allowed to open.
            try:
                data_dir = remote_data_dir(host)
            except Exception:
                data_dir = ""
            root = f"{data_dir}/servers" if data_dir else f"{host.home}/{REMOTE_SERVERS_DIR}"
            server_path = f"{root}/{server_id}"
        else:
            server_path = str(SERVERS_DIR / server_id)

        info = ServerInfo(
            {
                "id": server_id,
                "name": name,
                "mc_version": mc_version,
                "edition": normalize_edition(edition),
                "loader_type": normalize_loader(loader_type),
                "loader_version": loader_version,
                "ram_mb": ram_mb,
                "java_version": java_ver,
                "path": server_path,
                "host_id": host_id,
                # Made by this app on that machine, so the copy of this app over there
                # is told about it and kept up to date.
                "mirrored": bool(host_id),
            }
        )

        info.server_dir.mkdir(parents=True, exist_ok=True)

        self._servers[server_id] = info
        self._save()
        self.emit_on_main_thread("server-added", server_id)

        return info

    def attach_server(self, info: ServerInfo) -> ServerInfo:
        """Take an existing server folder into this app's list without touching it.

        add_server() makes a new server: it mints an id and creates the directory. This
        is the other case -- the folder is already there, on a host, with a world in it,
        put there by another copy of this app. Creating anything here would be wrong.
        """
        if not info.id:
            raise ValueError("a server record needs an id")
        if info.id in self._servers:
            raise ValueError(f"a server with the id {info.id} is already in this app")

        self._servers[info.id] = info
        self._save()
        self.emit_on_main_thread("server-added", info.id)
        return info

    def rename_server(self, server_id: str, new_name: str):
        info = self._servers.get(server_id)
        if info:
            info.name = new_name
            self._save()
            self.emit_on_main_thread("server-changed", server_id)

    def set_server_icon(self, server_id: str, source_image: str) -> tuple[bool, str]:
        """Give a server its picture, in its own folder, on whichever machine that is.

        The picture belongs with the server rather than in this app's own storage: that
        is what makes it show up in every copy of this app that can see the folder, and
        it is the same place Minecraft looks for the one it puts in the multiplayer list.
        """
        info = self._servers.get(server_id)
        if not info:
            return False, _("Server not found.")

        from niksnaks_hosting.shared.utils.image_utils import png_bytes, write_minecraft_server_icon

        try:
            picture = info.server_dir / USER_ICON_FILE
            picture.parent.mkdir(parents=True, exist_ok=True)
            picture.write_bytes(png_bytes(source_image, 128))
        except Exception as e:
            return False, _("Could not save the picture: {}").format(e)

        # Minecraft's own copy is worth having but not worth failing over: a host that
        # took the first write will take this one, and a pack may overwrite it anyway.
        try:
            write_minecraft_server_icon(source_image, info.server_dir)
        except Exception:
            pass

        info.icon_path = str(picture)
        self._icon_cache.pop(server_id, None)
        self._save()
        self.emit_on_main_thread("server-changed", server_id)
        return True, ""

    def icon_bytes(self, info: ServerInfo) -> bytes:
        """A server's picture as bytes, read from its own folder wherever that is.

        The path written in servers.json is a path on the machine the server lives on,
        which for a server on another computer names a file this one has not got --
        reading the folder instead is the same code for both. Remembered once per
        server, because the sidebar asks again every time a player joins or leaves and
        the answer only changes when this app is the thing that changes it.
        """
        cached = self._icon_cache.get(info.id)
        if cached is not None:
            return cached

        try:
            picture = info.server_dir / USER_ICON_FILE
            data = picture.read_bytes() if picture.is_file() else b""
        except Exception:
            # The machine could not be asked, which is not the same as there being no
            # picture. Remembering "none" here would leave the row blank for the rest
            # of the run even once the computer came back.
            return b""

        if not data and info.icon_path and not info.is_remote:
            # An older release could put the picture outside the server folder.
            try:
                stored = Path(info.icon_path)
                if stored.is_file():
                    data = stored.read_bytes()
            except OSError:
                return b""

        self._icon_cache[info.id] = data
        return data

    def get_autostart_servers(self) -> list[ServerInfo]:
        return [server for server in self._servers.values() if server.autostart]

    def set_server_autostart(self, server_id: str, autostart: bool) -> tuple[bool, str | None]:
        info = self._servers.get(server_id)
        if not info:
            return False, _("Server not found.")

        info.autostart = autostart
        self._save()

        if info.is_remote:
            # For a server on another machine, autostart has to mean something stronger
            # than "when this app opens": the host may boot without anyone opening
            # anything. Its own service manager is asked to bring the server up.
            def apply_remote() -> None:
                try:
                    host = info.host
                    host.ops.service(host, info.unit_id, str(info.server_dir)).enable_autostart(
                        autostart, description=info.name
                    )
                except Exception:
                    # The switch is still saved; a host that was unreachable picks the
                    # setting up the next time its unit is written, which is at start.
                    pass

            threading.Thread(target=apply_remote, daemon=True).start()

        self.emit_on_main_thread("server-changed", server_id)
        return True, None

    def update_server_ram(self, server_id: str, ram_mb: int):
        info = self._servers.get(server_id)
        if info:
            info.ram_mb = ram_mb
            self._save()
            proc = self._processes.get(server_id)
            if proc:
                proc.ram_mb = ram_mb
            self.emit_on_main_thread("server-changed", server_id)

    @staticmethod
    def _clear_loader_runtime(root: Path) -> None:
        """Strip whatever loader is installed so an installer writes onto bare ground.

        Forge and NeoForge unpack hundreds of libraries keyed by their own version, and
        the launcher picks the args file it finds. Leaving an old install behind would
        let a stale one win, so both are cleared along with Fabric's jars.
        """
        from niksnaks_hosting.shared.backend.loader_launch import (
            FABRIC_LAUNCH_JAR,
            LOADER_JAR_GLOBS,
            LOADER_LIBRARY_PATHS,
        )

        for filename in ("server.jar", FABRIC_LAUNCH_JAR, "run.bat", "run.sh", "user_jvm_args.txt"):
            try:
                (root / filename).unlink(missing_ok=True)
            except Exception:
                pass

        for pattern in LOADER_JAR_GLOBS.values():
            for jar in root.glob(pattern):
                if "installer" in jar.name.lower():
                    continue
                try:
                    jar.unlink(missing_ok=True)
                except Exception:
                    pass

        for library_paths in LOADER_LIBRARY_PATHS.values():
            for library_path in library_paths:
                loader_libs = root.joinpath(*library_path)
                if loader_libs.is_dir():
                    shutil.rmtree(loader_libs, ignore_errors=True)

    def update_server_runtime(
        self,
        server_id: str,
        mc_version: str,
        loader_version: str | None = None,
        progress_callback=None,
        compatibility_plan: dict | None = None,
        loader_type: str | None = None,
    ) -> tuple[bool, str]:

        from niksnaks_hosting.shared.utils.constants import get_required_java_version

        info = self._servers.get(server_id)
        if not info:
            return False, _("Server not found")

        if is_bedrock(info.edition):
            return False, _("Use the Bedrock updater for Bedrock Edition servers")

        if info.is_remote:
            # Everything below runs on this computer: it downloads the installer here,
            # runs it with this machine's Java, and writes the result to this disk. Worse,
            # it clears the old loader out of the server folder first -- which for a
            # remote server is the host's folder, and really would be emptied. Refusing
            # up front is the only safe answer until this is routed through the host the
            # way the create-server wizard already routes a first install.
            return False, _(
                "Changing the Minecraft version of a server on {} is not supported yet. "
                "Its mods and worlds would have to be rebuilt on that computer, and this "
                "app can only do that here. Nothing has been changed."
            ).format(info.host.name)

        process = self._processes.get(server_id)
        if process and process.is_running:
            return False, _("Cannot update version while server is running")

        mc_version = str(mc_version or "").strip()
        target_loader = normalize_loader(loader_type) if loader_type else info.loader_type
        if loader_version is not None:
            loader_version = str(loader_version or "").strip()
        elif target_loader != info.loader_type:

            loader_version = ""
        elif uses_installer(target_loader):

            loader_version = ""
        else:
            loader_version = info.loader_version
        if not mc_version:
            return False, _("Minecraft version is required")

        try:
            java_req = get_required_java_version(mc_version)
        except Exception:
            java_req = 21

        root = info.server_dir
        root.mkdir(parents=True, exist_ok=True)

        def progress(frac: float, msg: str) -> None:
            if progress_callback:
                progress_callback(frac, msg)

        progress(0.02, _("Creating full backup"))
        backup_ok, backup_msg = self.create_full_backup(server_id)
        if not backup_ok:
            return False, _("Could not create full backup before updating: {}").format(backup_msg)

        if not self.java_manager.is_java_available(java_req):
            ok, msg = self.java_manager.download_jre_sync(
                java_req,
                progress_callback=lambda f, text: progress(0.05 + f * 0.20, text),
            )
            if not ok:
                return False, _("Failed to download Java {}: {}").format(java_req, msg)

        java_path = self.java_manager.get_java_path(java_req) or self.java_manager.get_java_for_mc(mc_version) or "java"

        if uses_installer(target_loader):
            loader_name = get_loader_display_name(target_loader)

            if target_loader == LOADER_NEOFORGE:
                build = loader_version or self.download_manager.get_neoforge_recommended_version(mc_version)
            else:
                build = loader_version or self.download_manager.get_forge_recommended_build(mc_version)
            if not build:
                return False, _("No {} build available for Minecraft {}").format(loader_name, mc_version)
            loader_version = build

            progress(0.28, _("Downloading {} installer").format(loader_name))
            if target_loader == LOADER_NEOFORGE:
                installer_path = self.download_manager.download_neoforge_installer(
                    build,
                    progress_callback=lambda f, text: progress(0.28 + f * 0.14, text),
                )
            else:
                installer_path = self.download_manager.download_forge_installer(
                    get_forge_full_version(mc_version, build),
                    progress_callback=lambda f, text: progress(0.28 + f * 0.14, text),
                )
            if not installer_path:
                return False, _("Failed to download {} installer").format(loader_name)

            self._clear_loader_runtime(root)

            progress(0.44, _("Installing {} server").format(loader_name))
            if target_loader == LOADER_NEOFORGE:
                ok, msg = self.download_manager.install_neoforge_server(
                    java_path=java_path,
                    installer_jar=installer_path,
                    mc_version=mc_version,
                    server_dir=str(root),
                    progress_callback=lambda f, text: progress(0.44 + f * 0.46, text),
                )
            else:
                ok, msg = self.download_manager.install_forge_server(
                    java_path=java_path,
                    installer_jar=installer_path,
                    mc_version=mc_version,
                    forge_build=build,
                    server_dir=str(root),
                    progress_callback=lambda f, text: progress(0.44 + f * 0.46, text),
                )
            if not ok:
                return False, msg
        else:
            progress(0.28, _("Downloading Fabric installer"))
            installer_path = self.download_manager.download_installer(
                progress_callback=lambda f, text: progress(0.28 + f * 0.12, text),
            )
            if not installer_path:
                return False, _("Failed to download Fabric installer")

            for filename in ("server.jar", "fabric-server-launch.jar"):
                try:
                    (root / filename).unlink(missing_ok=True)
                except Exception:
                    pass

            progress(0.42, _("Downloading Minecraft {} server").format(mc_version))
            ok, msg = self.download_manager.download_server_jar(
                mc_version,
                str(root),
                progress_callback=lambda f, text: progress(0.42 + f * 0.22, text),
            )
            if not ok:
                return False, msg

            progress(0.66, _("Installing Fabric server"))
            ok, msg = self.download_manager.install_fabric_server(
                java_path=java_path,
                installer_jar=installer_path,
                mc_version=mc_version,
                server_dir=str(root),
                loader_version=loader_version or None,
                progress_callback=lambda f, text: progress(0.66 + f * 0.30, text),
            )
            if not ok:
                return False, msg

        progress(0.90, _("Checking installed content compatibility"))
        plan = compatibility_plan or self.scan_update_compatibility(server_id, mc_version, target_loader)

        progress(0.93, _("Updating compatible mods and datapacks"))
        applied, failed = self.apply_compatible_component_updates(server_id, mc_version, plan, target_loader)

        progress(0.97, _("Moving incompatible files aside"))
        disabled = self.isolate_incompatible_components(server_id, mc_version, plan)

        info.mc_version = mc_version

        info.java_version = max(info.java_version, java_req)
        info.loader_type = target_loader
        info.loader_version = loader_version
        self._save()
        existing_process = self._processes.get(server_id)
        if existing_process:
            existing_process.java_path = self.java_manager.get_java_path(info.java_version) or "java"
            existing_process.loader_type = target_loader
        self.emit_on_main_thread("server-changed", server_id)
        progress(1.0, _("Server runtime updated"))

        disabled_count = sum(len(v) for v in disabled.values())
        detail = _("Updated to Minecraft {}.").format(mc_version) + _(" Updated {} compatible file(s).").format(applied)
        if disabled_count:
            detail += _(" Disabled {} incompatible file(s).").format(disabled_count)
        if failed:
            detail += _(" {} compatible update(s) failed.").format(failed)
        return True, detail

    def install_bedrock_server(
        self,
        server_id: str,
        option: BedrockVersionOption,
        progress_callback=None,
        keep_existing_config: bool = False,
    ) -> tuple[bool, str]:

        info = self._servers.get(server_id)
        if not info:
            return False, _("Server not found")

        if info.is_remote:
            # BedrockManager downloads and unpacks on this computer, which is the wrong
            # disk entirely. The host fetches its own zip, for its own platform, and
            # unpacks it itself -- the same path the create-server wizard uses.
            from niksnaks_hosting.shared.remote.remote_install import install_remote_bedrock

            try:
                install_remote_bedrock(
                    info.host,
                    str(info.server_dir),
                    option,
                    progress=progress_callback,
                    keep_existing_config=keep_existing_config,
                )
            except Exception as e:
                return False, _("Could not install the Bedrock server on {}: {}").format(
                    info.host.name, e
                )

            info.mc_version = option.version
            info.edition = EDITION_BEDROCK
            self._save()
            self.emit_on_main_thread("server-changed", server_id)
            return True, _("Bedrock server {} installed on {}.").format(option.version, info.host.name)

        ok, msg = self.bedrock_manager.install(
            option,
            info.server_dir,
            progress_callback=progress_callback,
            keep_existing_config=keep_existing_config,
        )
        if not ok:
            return False, msg

        info.mc_version = option.version
        info.edition = EDITION_BEDROCK
        self._save()
        self.emit_on_main_thread("server-changed", server_id)
        return True, msg

    def update_bedrock_server(
        self,
        server_id: str,
        option: BedrockVersionOption,
        progress_callback=None,
    ) -> tuple[bool, str]:

        info = self._servers.get(server_id)
        if not info:
            return False, _("Server not found")

        if not is_bedrock(info.edition):
            return False, _("This server is not a Bedrock Edition server")

        process = self._processes.get(server_id)
        if process and process.is_running:
            return False, _("Cannot update version while server is running")

        def progress(frac: float, msg: str) -> None:
            if progress_callback:
                progress_callback(frac, msg)

        # Backups are written to this computer's disk from this computer's disk, so
        # there is no backup to take for a server on another machine. Said in the result
        # rather than skipped quietly, and rather than refusing the update: the package
        # leaves worlds/ alone and the settings files are put back after it.
        unbacked = ""
        if info.is_remote:
            unbacked = _(
                " No full backup was taken first: backing up a server on another computer"
                " is not supported yet."
            )
        else:
            progress(0.02, _("Creating full backup"))
            backup_ok, backup_msg = self.create_full_backup(server_id)
            if not backup_ok:
                return False, _("Could not create full backup before updating: {}").format(backup_msg)

        # Worlds live under worlds/ and are untouched by the package; server.properties,
        # allowlist.json and permissions.json are kept so settings survive the update.
        ok, msg = self.install_bedrock_server(
            server_id,
            option,
            progress_callback=lambda frac, text: progress(0.05 + frac * 0.95, text),
            keep_existing_config=True,
        )
        if not ok:
            return False, msg

        progress(1.0, _("Server runtime updated"))
        return True, _("Updated to Bedrock {}.").format(option.version) + unbacked

    def bedrock_versions(self, info: ServerInfo) -> list[BedrockVersionOption]:
        """The Bedrock builds available for the machine this server lives on.

        Not this computer's: the version numbers match across platforms but the
        downloads do not, and handing a Windows host a Linux tarball's URL installs
        nothing it can run.
        """
        platform = ""
        if info.is_remote:
            try:
                platform = info.host.ops.platform
            except Exception:
                platform = ""
        return self.bedrock_manager.fetch_versions(platform)

    def bedrock_binary_name(self, info: ServerInfo) -> str:
        """What the machine this server lives on calls the Bedrock executable.

        Not this computer's answer: a Linux desktop looking after a Windows host is
        asking about a bedrock_server.exe, and the other way round.
        """
        if not info.is_remote:
            return BEDROCK_SERVER_BINARY
        try:
            return info.host.ops.bedrock_binary
        except Exception:
            return BEDROCK_SERVER_BINARY

    def bedrock_runtime_missing(self, server_id: str) -> bool:
        """Whether a Bedrock server has no executable the machine it lives on can run."""
        info = self._servers.get(server_id)
        if not info or not is_bedrock(info.edition):
            return False
        try:
            return not self.bedrock_manager.is_installed(info.server_dir, self.bedrock_binary_name(info))
        except Exception:
            # The machine could not be asked, which is not the same as the answer being
            # no. Reporting the files as missing would offer to reinstall a server that
            # is sitting there intact on a computer that happens to be switched off.
            return False

    def repair_bedrock_runtime(
        self,
        server_id: str,
        option: BedrockVersionOption | None = None,
        progress_callback=None,
    ) -> tuple[bool, str]:
        """Install the Bedrock executable for this computer, keeping worlds and settings.

        No backup is taken first: there is no working runtime here to preserve.
        """
        info = self._servers.get(server_id)
        if not info:
            return False, _("Server not found")

        if not is_bedrock(info.edition):
            return False, _("This server is not a Bedrock Edition server")

        process = self._processes.get(server_id)
        if process and process.is_running:
            return False, _("Cannot reinstall the server files while the server is running")

        if self.bedrock_manager.is_installed(info.server_dir, self.bedrock_binary_name(info)):
            return True, _("The Bedrock server files are already installed.")

        def progress(frac: float, msg: str) -> None:
            if progress_callback:
                progress_callback(frac, msg)

        if option is None:
            progress(0.0, _("Looking up the Bedrock server..."))
            options = self.bedrock_versions(info)
            option = next((entry for entry in options if not entry.preview), options[0] if options else None)
        if not option:
            return False, _("Could not reach the Bedrock server download page")

        foreign = self.bedrock_manager.foreign_binary(info.server_dir, self.bedrock_binary_name(info))

        ok, msg = self.install_bedrock_server(
            server_id,
            option,
            progress_callback=progress,
            keep_existing_config=True,
        )
        if not ok:
            return False, msg

        if foreign:
            foreign.unlink(missing_ok=True)

        where = info.host.name if info.is_remote else _("this computer")
        return True, _("Installed Bedrock server {} for {}.").format(option.version, where)

    def _json_file(self, path: Path) -> dict:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_json_file(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _server_datapacks_dir(self, root: Path) -> Path:
        world_name = self._configured_level_name(root)
        return root / world_name / "datapacks"

    def _unique_disabled_path(self, dest_dir: Path, filename: str) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        candidate = dest_dir / Path(filename).name
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        idx = 2
        while True:
            alt = dest_dir / f"{stem}-{idx}{suffix}"
            if not alt.exists():
                return alt
            idx += 1

    def _move_if_present(self, source_dir: Path, filename: str, dest_dir: Path) -> Path | None:
        name = Path(str(filename or "")).name
        if not name:
            return None
        source = source_dir / name
        if not source.exists():
            for item in source_dir.glob("*"):
                if item.name.casefold() == name.casefold():
                    source = item
                    break
        if not source.exists() or not source.is_file():
            return None
        dest = self._unique_disabled_path(dest_dir, source.name)
        shutil.move(str(source), str(dest))
        return dest

    @staticmethod
    def version_sort_key(value: str) -> tuple:
        text = str(value or "").strip().lower()
        parts: list[object] = []
        for token in re.findall(r"\d+|[a-z]+", text):
            if token.isdigit():
                parts.append((0, int(token)))
            else:
                label_weight = {
                    "snapshot": -4,
                    "pre": -3,
                    "rc": -2,
                    "alpha": -5,
                    "beta": -4,
                }.get(token, -1)
                parts.append((1, label_weight, token))
        return tuple(parts)

    @classmethod
    def is_version_at_least(cls, candidate: str, current: str) -> bool:
        return cls.version_sort_key(candidate) >= cls.version_sort_key(current)

    @classmethod
    def is_version_after(cls, candidate: str, current: str) -> bool:
        return cls.version_sort_key(candidate) > cls.version_sort_key(current)

    def _tracked_mod_state(self, root: Path) -> dict:
        data = self._json_file(root / ".niksnaks-hosting-mod-installs.json")
        return data.get("mods") if isinstance(data.get("mods"), dict) else {}

    def _tracked_modpack_state(self, root: Path) -> dict:
        data = self._json_file(root / ".niksnaks-hosting-modpacks.json")
        return data.get("installed_projects") if isinstance(data.get("installed_projects"), dict) else {}

    def _tracked_datapack_state(self, root: Path) -> dict:
        data = self._json_file(root / ".niksnaks-hosting-datapack-installs.json")
        return data.get("datapacks") if isinstance(data.get("datapacks"), dict) else {}

    def _tracked_mod_dependency_state(self, root: Path) -> dict[str, list[str]]:
        data = self._json_file(root / ".niksnaks-hosting-mod-dependencies.json")
        req = data.get("required_by") if isinstance(data.get("required_by"), dict) else {}
        cleaned: dict[str, list[str]] = {}
        for dep_name, parents in req.items():
            dep_key = Path(str(dep_name or "")).name.casefold()
            if not dep_key or not isinstance(parents, list):
                continue
            parent_keys = sorted(
                {Path(str(parent or "")).name.casefold() for parent in parents if Path(str(parent or "")).name}
            )
            if parent_keys:
                cleaned[dep_key] = parent_keys
        return cleaned

    def _write_mod_dependency_state(self, root: Path, required_by: dict[str, list[str]]) -> None:
        cleaned = {
            Path(str(dep)).name.casefold(): sorted(
                {Path(str(parent)).name.casefold() for parent in parents if Path(str(parent)).name}
            )
            for dep, parents in required_by.items()
            if Path(str(dep)).name and parents
        }
        cleaned = {dep: parents for dep, parents in cleaned.items() if parents}
        self._write_json_file(root / ".niksnaks-hosting-mod-dependencies.json", {"required_by": cleaned})

    def _replace_mod_dependency_parent(
        self,
        root: Path,
        old_parent_filename: str,
        new_parent_filename: str,
        dep_versions: list,
    ) -> tuple[set[str], set[str]]:
        old_parent = Path(str(old_parent_filename or "")).name.casefold()
        new_parent = Path(str(new_parent_filename or "")).name.casefold()
        state = self._tracked_mod_dependency_state(root)
        old_dep_names = {dep_name for dep_name, parents in state.items() if old_parent and old_parent in parents}

        for dep_name, parents in list(state.items()):
            filtered = [parent for parent in parents if parent != old_parent]
            if filtered:
                state[dep_name] = filtered
            else:
                state.pop(dep_name, None)

        new_dep_names: set[str] = set()
        if new_parent:
            for dep in dep_versions:
                dep_name = Path(str(getattr(dep, "filename", "") or "")).name.casefold()
                if not dep_name or dep_name == new_parent:
                    continue
                new_dep_names.add(dep_name)
                parents = set(state.get(dep_name, []))
                parents.add(new_parent)
                state[dep_name] = sorted(parents)

        self._write_mod_dependency_state(root, state)
        return old_dep_names, new_dep_names

    def _remove_orphaned_dependency_files(self, root: Path, dependency_names: set[str]) -> None:
        if not dependency_names:
            return
        mods_dir = root / "mods"
        if not mods_dir.is_dir():
            return
        state = self._tracked_mod_dependency_state(root)
        for dep_name in dependency_names:
            if state.get(dep_name):
                continue
            dep_file = self._find_file_case_insensitive(mods_dir, dep_name)
            if dep_file:
                try:
                    dep_file.unlink(missing_ok=True)
                except Exception:
                    pass

    def _version_entry(self, project_id: str, meta: dict, version) -> dict[str, str]:
        return {
            "title": str((meta or {}).get("title") or project_id),
            "project_id": str(project_id),
            "current_filename": str((meta or {}).get("filename", "")),
            "filename": str(getattr(version, "filename", "") or ""),
            "version_id": str(getattr(version, "version_id", "") or ""),
            "version_number": str(getattr(version, "version_number", "") or ""),
            "download_url": str(getattr(version, "download_url", "") or ""),
        }

    def _incompatible_entry(
        self, project_id: str, meta: dict, target_mc_version: str, kind_label: str
    ) -> dict[str, str]:
        return {
            "title": str((meta or {}).get("title") or project_id),
            "filename": str((meta or {}).get("filename", "")),
            "project_id": str(project_id),
            "reason": _("No Modrinth {} release for Minecraft {}").format(kind_label, target_mc_version),
        }

    def scan_update_compatibility(self, server_id: str, target_mc_version: str, loader_type: str | None = None) -> dict:
        info = self._servers.get(server_id)
        if not info:
            empty = {"mods": [], "modpacks": [], "datapacks": []}
            return {"compatible": empty.copy(), "incompatible": empty.copy(), "unknown": empty.copy()}

        from niksnaks_hosting.shared.backend import modrinth_client

        root = info.server_dir
        loader = normalize_loader(loader_type) if loader_type else info.loader_type
        plan = {
            "compatible": {"mods": [], "modpacks": [], "datapacks": []},
            "incompatible": {"mods": [], "modpacks": [], "datapacks": []},
            "unknown": {"mods": [], "modpacks": [], "datapacks": []},
        }

        def best_version(project_id: str, kind: str):
            try:
                versions = modrinth_client.get_project_versions(project_id)
            except Exception:
                return None
            if not versions:
                return None
            if kind == "datapacks":
                candidates = [v for v in versions if not (v.loaders or [])]
            elif kind == "modpacks":
                loader_candidates = [v for v in versions if loader in [x.lower() for x in (v.loaders or [])]]
                candidates = loader_candidates or versions
            else:
                candidates = [v for v in versions if loader in [x.lower() for x in (v.loaders or [])]]
            exact = [v for v in candidates if target_mc_version in (v.game_versions or [])]
            return exact[0] if exact else False

        for project_id, meta in self._tracked_modpack_state(root).items():
            if not isinstance(meta, dict):
                continue
            version = best_version(str(project_id), "modpacks")
            if version is False:
                entry = self._incompatible_entry(str(project_id), meta, target_mc_version, "modpack")
                entry["filename"] = ", ".join([str(Path(str(f)).name) for f in (meta.get("mods") or [])])
                plan["incompatible"]["modpacks"].append(entry)
            elif version is None:
                plan["unknown"]["modpacks"].append(
                    self._incompatible_entry(str(project_id), meta, target_mc_version, "modpack")
                )
            else:
                entry = self._version_entry(str(project_id), meta, version)
                entry["previous_mods"] = json.dumps([str(Path(str(f)).name) for f in (meta.get("mods") or [])])
                plan["compatible"]["modpacks"].append(entry)

        for project_id, meta in self._tracked_mod_state(root).items():
            if not isinstance(meta, dict):
                continue
            version = best_version(str(project_id), "mods")
            if version is False:
                plan["incompatible"]["mods"].append(
                    self._incompatible_entry(str(project_id), meta, target_mc_version, "mod")
                )
            elif version is None:
                plan["unknown"]["mods"].append(
                    self._incompatible_entry(str(project_id), meta, target_mc_version, "mod")
                )
            else:
                plan["compatible"]["mods"].append(self._version_entry(str(project_id), meta, version))

        for project_id, meta in self._tracked_datapack_state(root).items():
            if not isinstance(meta, dict):
                continue
            version = best_version(str(project_id), "datapacks")
            if version is False:
                plan["incompatible"]["datapacks"].append(
                    self._incompatible_entry(str(project_id), meta, target_mc_version, "datapack")
                )
            elif version is None:
                plan["unknown"]["datapacks"].append(
                    self._incompatible_entry(str(project_id), meta, target_mc_version, "datapack")
                )
            else:
                plan["compatible"]["datapacks"].append(self._version_entry(str(project_id), meta, version))

        return plan

    def _find_file_case_insensitive(self, directory: Path, filename: str) -> Path | None:
        name = Path(str(filename or "")).name
        if not name:
            return None
        direct = directory / name
        if direct.exists():
            return direct
        if not directory.is_dir():
            return None
        for item in directory.iterdir():
            if item.name.casefold() == name.casefold():
                return item
        return None

    def _remove_filename_from_tracked_mods(self, root: Path, filename: str) -> None:
        name = Path(str(filename or "")).name.casefold()
        if not name:
            return

        mods = self._tracked_mod_state(root)
        kept_mods = {
            pid: meta
            for pid, meta in mods.items()
            if Path(str((meta or {}).get("filename", ""))).name.casefold() != name
        }
        if kept_mods != mods:
            self._write_json_file(root / ".niksnaks-hosting-mod-installs.json", {"mods": kept_mods})

        packs = self._tracked_modpack_state(root)
        changed = False
        for meta in packs.values():
            if not isinstance(meta, dict):
                continue
            old_mods = meta.get("mods") or []
            new_mods = [m for m in old_mods if Path(str(m)).name.casefold() != name]
            if new_mods != old_mods:
                meta["mods"] = new_mods
                changed = True
        if changed:
            self._write_json_file(root / ".niksnaks-hosting-modpacks.json", {"installed_projects": packs})

    def apply_compatible_component_updates(
        self, server_id: str, target_mc_version: str, plan: dict | None = None, loader_type: str | None = None
    ) -> tuple[int, int]:

        info = self._servers.get(server_id)
        if not info:
            return 0, 0

        from niksnaks_hosting.shared.backend import modrinth_client

        dep_loader = normalize_loader(loader_type) if loader_type else info.loader_type

        root = info.server_dir
        mods_dir = root / "mods"
        mods_dir.mkdir(parents=True, exist_ok=True)
        dp_dir = self._server_datapacks_dir(root)
        dp_dir.mkdir(parents=True, exist_ok=True)
        plan = plan or self.scan_update_compatibility(server_id, target_mc_version)
        compatible = plan.get("compatible") if isinstance(plan.get("compatible"), dict) else {}
        applied = 0
        failed = 0

        pack_state = self._tracked_modpack_state(root)
        for entry in compatible.get("modpacks", []) or []:
            try:
                project_id = str(entry.get("project_id", "")).strip()
                version_id = str(entry.get("version_id", "")).strip()
                if not project_id or not version_id:
                    continue
                previous_mods = set()
                try:
                    previous_mods = {
                        Path(str(m)).name.casefold()
                        for m in json.loads(str(entry.get("previous_mods") or "[]"))
                        if str(m).strip().lower().endswith(".jar")
                    }
                except Exception:
                    previous_mods = {
                        Path(str(m)).name.casefold()
                        for m in ((pack_state.get(project_id) or {}).get("mods") or [])
                        if str(m).strip().lower().endswith(".jar")
                    }
                result = modrinth_client.install_modpack(version_id, root)
                new_mods = {
                    Path(str(m)).name.casefold()
                    for m in (result.managed_mod_files or [])
                    if str(m).strip().lower().endswith(".jar")
                }
                for removed in previous_mods - new_mods:
                    old = self._find_file_case_insensitive(mods_dir, removed)
                    if old:
                        old.unlink(missing_ok=True)
                    self._remove_filename_from_tracked_mods(root, removed)
                pack_state[project_id] = {
                    "version_id": version_id,
                    "version_number": str(entry.get("version_number", "")),
                    "title": str(entry.get("title", "")),
                    "mods": sorted(new_mods),
                }
                self._write_json_file(root / ".niksnaks-hosting-modpacks.json", {"installed_projects": pack_state})
                applied += 1
            except Exception:
                failed += 1

        managed_mods = set()
        for pack in self._tracked_modpack_state(root).values():
            if isinstance(pack, dict):
                managed_mods.update(Path(str(m)).name.casefold() for m in (pack.get("mods") or []))

        mod_state = self._tracked_mod_state(root)
        for entry in compatible.get("mods", []) or []:
            try:
                project_id = str(entry.get("project_id", "")).strip()
                version_id = str(entry.get("version_id", "")).strip()
                filename = Path(str(entry.get("filename", ""))).name
                download_url = str(entry.get("download_url", "")).strip()
                if not project_id or not version_id or not filename or not download_url:
                    continue

                deps = modrinth_client.resolve_required_dependencies(version_id, target_mc_version, dep_loader)
                for dep in deps:
                    dep_name = Path(str(dep.filename)).name
                    if dep_name.casefold() in managed_mods or dep_name.casefold() == filename.casefold():
                        continue
                    modrinth_client.download_to(dep.download_url, mods_dir / dep_name)
                    dep_project_id = str(getattr(dep, "project_id", "") or "").strip()
                    if dep_project_id:
                        mod_state[dep_project_id] = {
                            "title": str(
                                getattr(dep, "title", "") or getattr(dep, "name", "") or dep_project_id
                            ).strip(),
                            "version_id": str(getattr(dep, "version_id", "") or "").strip(),
                            "version_number": str(getattr(dep, "version_number", "") or "").strip(),
                            "filename": dep_name,
                        }

                modrinth_client.download_to(download_url, mods_dir / filename)
                old_filename = str(entry.get("current_filename", "")).strip()
                old_dep_names, new_dep_names = self._replace_mod_dependency_parent(
                    root,
                    old_filename,
                    filename,
                    [dep for dep in deps if Path(str(dep.filename)).name.casefold() not in managed_mods],
                )
                if old_filename and Path(old_filename).name.casefold() != filename.casefold():
                    old = self._find_file_case_insensitive(mods_dir, old_filename)
                    if old:
                        old.unlink(missing_ok=True)
                    self._remove_filename_from_tracked_mods(root, old_filename)
                self._remove_orphaned_dependency_files(root, old_dep_names - new_dep_names)
                mod_state[project_id] = {
                    "title": str(entry.get("title", "")),
                    "version_id": version_id,
                    "version_number": str(entry.get("version_number", "")),
                    "filename": filename,
                }
                self._write_json_file(root / ".niksnaks-hosting-mod-installs.json", {"mods": mod_state})
                applied += 1
            except Exception:
                failed += 1

        dp_state = self._tracked_datapack_state(root)
        for entry in compatible.get("datapacks", []) or []:
            try:
                project_id = str(entry.get("project_id", "")).strip()
                version_id = str(entry.get("version_id", "")).strip()
                filename = Path(str(entry.get("filename", ""))).name
                download_url = str(entry.get("download_url", "")).strip()
                if not project_id or not version_id or not filename or not download_url:
                    continue
                modrinth_client.download_to(download_url, dp_dir / filename)
                old_filename = str(entry.get("current_filename", "")).strip()
                if old_filename and Path(old_filename).name.casefold() != filename.casefold():
                    old = self._find_file_case_insensitive(dp_dir, old_filename)
                    if old:
                        old.unlink(missing_ok=True)
                dp_state[project_id] = {
                    "title": str(entry.get("title", "")),
                    "version_id": version_id,
                    "version_number": str(entry.get("version_number", "")),
                    "filename": filename,
                }
                self._write_json_file(root / ".niksnaks-hosting-datapack-installs.json", {"datapacks": dp_state})
                applied += 1
            except Exception:
                failed += 1

        return applied, failed

    def isolate_incompatible_components(
        self,
        server_id: str,
        target_mc_version: str,
        plan: dict | None = None,
    ) -> dict[str, list[dict[str, str]]]:

        info = self._servers.get(server_id)
        if not info:
            return {"mods": [], "modpacks": [], "datapacks": []}

        root = info.server_dir
        mods_dir = root / "mods"
        datapacks_dir = self._server_datapacks_dir(root)
        disabled_mods = root / "mods_incompatible"
        disabled_datapacks = root / "datapacks_incompatible"
        plan = plan or self.scan_update_compatibility(server_id, target_mc_version)
        incompatible = plan.get("incompatible") if isinstance(plan.get("incompatible"), dict) else {}
        record: dict[str, list[dict[str, str]]] = {"mods": [], "modpacks": [], "datapacks": []}

        mod_state_path = root / ".niksnaks-hosting-mod-installs.json"
        mods = self._tracked_mod_state(root)
        kept_mods = dict(mods)
        for entry in incompatible.get("mods", []) or []:
            project_id = str(entry.get("project_id", "")).strip()
            meta = mods.get(project_id)
            if not isinstance(meta, dict):
                continue
            moved = self._move_if_present(mods_dir, str(meta.get("filename", "")), disabled_mods)
            if moved:
                kept_mods.pop(project_id, None)
                new_entry = dict(entry)
                new_entry["filename"] = moved.name
                record["mods"].append(new_entry)
        if kept_mods != mods:
            self._write_json_file(mod_state_path, {"mods": kept_mods})

        pack_state_path = root / ".niksnaks-hosting-modpacks.json"
        packs = self._tracked_modpack_state(root)
        kept_packs = dict(packs)
        for entry in incompatible.get("modpacks", []) or []:
            project_id = str(entry.get("project_id", "")).strip()
            meta = packs.get(project_id)
            if not isinstance(meta, dict):
                continue
            moved_any = False
            for filename in meta.get("mods") or []:
                moved = self._move_if_present(mods_dir, str(filename), disabled_mods)
                moved_any = bool(moved) or moved_any
            if moved_any:
                kept_packs.pop(project_id, None)
                record["modpacks"].append(dict(entry))
        if kept_packs != packs:
            self._write_json_file(pack_state_path, {"installed_projects": kept_packs})

        dp_state_path = root / ".niksnaks-hosting-datapack-installs.json"
        datapacks = self._tracked_datapack_state(root)
        kept_datapacks = dict(datapacks)
        for entry in incompatible.get("datapacks", []) or []:
            project_id = str(entry.get("project_id", "")).strip()
            meta = datapacks.get(project_id)
            if not isinstance(meta, dict):
                continue
            moved = self._move_if_present(datapacks_dir, str(meta.get("filename", "")), disabled_datapacks)
            if moved:
                kept_datapacks.pop(project_id, None)
                new_entry = dict(entry)
                new_entry["filename"] = moved.name
                record["datapacks"].append(new_entry)
        if kept_datapacks != datapacks:
            self._write_json_file(dp_state_path, {"datapacks": kept_datapacks})

        if any(record.values()):
            previous = self.get_incompatible_components(server_id)
            merged = {key: [*previous.get(key, []), *record.get(key, [])] for key in ("mods", "modpacks", "datapacks")}
            self._write_json_file(root / ".niksnaks-hosting-incompatible-components.json", merged)
        return record

    def get_incompatible_components(self, server_id: str) -> dict[str, list[dict[str, str]]]:
        info = self._servers.get(server_id)
        if not info:
            return {"mods": [], "modpacks": [], "datapacks": []}
        data = self._json_file(info.server_dir / ".niksnaks-hosting-incompatible-components.json")
        out: dict[str, list[dict[str, str]]] = {}
        for key in ("mods", "modpacks", "datapacks"):
            values = data.get(key) if isinstance(data.get(key), list) else []
            out[key] = [v for v in values if isinstance(v, dict)]
        return out

    def delete_incompatible_component(
        self,
        server_id: str,
        kind: str,
        project_id: str = "",
        filename: str = "",
    ) -> tuple[bool, str]:

        info = self._servers.get(server_id)
        if not info:
            return False, _("Server not found")

        key = str(kind or "").strip().lower()
        aliases = {
            "mod": "mods",
            "mods": "mods",
            "modpack": "modpacks",
            "modpacks": "modpacks",
            "datapack": "datapacks",
            "datapacks": "datapacks",
        }
        key = aliases.get(key, key)
        if key not in {"mods", "modpacks", "datapacks"}:
            return False, _("Unknown disabled item type")

        root = info.server_dir
        disabled_dir = root / ("datapacks_incompatible" if key == "datapacks" else "mods_incompatible")
        data_path = root / ".niksnaks-hosting-incompatible-components.json"
        data = self.get_incompatible_components(server_id)
        records = data.get(key, [])
        project_id = str(project_id or "").strip()
        filename = str(filename or "").strip()

        removed_records: list[dict[str, str]] = []
        kept: list[dict[str, str]] = []
        for record in records:
            rec_project = str(record.get("project_id") or "").strip()
            rec_filename = str(record.get("filename") or "").strip()
            project_matches = bool(project_id) and rec_project == project_id
            filename_matches = bool(filename) and rec_filename == filename
            if project_matches or filename_matches:
                removed_records.append(record)
            else:
                kept.append(record)

        if not removed_records:
            return False, _("Disabled item not found")

        deleted_files = 0
        for record in removed_records:
            names = [filename] if filename else []
            rec_filename = str(record.get("filename") or "").strip()
            if rec_filename:
                names.extend([part.strip() for part in rec_filename.split(",") if part.strip()])
            for name in {Path(n).name for n in names if n}:
                target = self._find_file_case_insensitive(disabled_dir, name)
                if target and target.exists():
                    target.unlink(missing_ok=True)
                    deleted_files += 1

        data[key] = kept
        self._write_json_file(data_path, data)
        self.emit_on_main_thread("server-changed", server_id)
        if deleted_files:
            return True, _("Deleted {} disabled file(s).").format(deleted_files)
        return True, _("Removed disabled item record.")

    @staticmethod
    def backup_game_version(zip_path: Path) -> str:
        name = Path(zip_path).name
        match = re.match(FULL_BACKUP_NAME_RE, name)
        return match.group(1) if match else ""

    @staticmethod
    def is_version_older(candidate: str, current: str) -> bool:
        def parse(value: str) -> tuple[int, ...] | None:
            parts = re.findall(r"\d+", str(value or ""))
            if not parts:
                return None
            return tuple(int(p) for p in parts[:4])

        a = parse(candidate)
        b = parse(current)
        if not a or not b:
            return False
        max_len = max(len(a), len(b))
        return a + (0,) * (max_len - len(a)) < b + (0,) * (max_len - len(b))

    def restore_server(self, server_data: dict) -> bool:
        try:
            info = ServerInfo(server_data)
        except Exception:
            return False

        if not info.id or info.id in self._servers:
            return False

        self._servers[info.id] = info
        self._save()
        self.emit_on_main_thread("server-added", info.id)
        return True

    def delete_server(self, server_id: str, delete_files: bool = True):
        info = self._servers.get(server_id)
        if not info:
            return

        if self.playit_manager.is_running_for(server_id):
            self.playit_manager.stop_server(server_id)

        process = self._processes.get(server_id)
        if process and process.is_running:
            process.kill()

        if server_id in self._processes:
            del self._processes[server_id]

        if info.is_remote:
            # The unit or task outlives the folder, so it has to be withdrawn explicitly
            # or the host is left with a service pointing at nothing.
            try:
                host = info.host
                host.ops.service(host, info.unit_id, str(info.server_dir)).remove_unit()
            except Exception:
                pass
            if delete_files:
                try:
                    info.host.ops.remove_tree(info.host, str(info.server_dir))
                except Exception:
                    pass
        elif delete_files and info.server_dir.exists():
            # Journaled first: if a locked file wins this round, or the app dies
            # mid-delete, the next launch finishes the job.
            register_pending_deletion(info.server_dir)
            delete_registered(info.server_dir)

        del self._servers[server_id]
        # Remembered so the entry still on disk, or one written by another copy of this
        # app a moment ago, is not read back in as a server that has come back.
        self._forgotten.add(server_id)
        self._mirrored.pop(server_id, None)
        if info.is_remote and info.mirrored:
            self._withdraw_from_host(info)
        self._save()
        self.emit_on_main_thread("server-removed", server_id)

    def get_process(self, server_id: str) -> ServerProcess | None:
        info = self._servers.get(server_id)
        if not info:
            return None

        with self._process_lock:
            return self._make_process(server_id, info)

    def _make_process(self, server_id: str, info: ServerInfo) -> ServerProcess:
        if server_id not in self._processes:
            server_is_bedrock = is_bedrock(info.edition)
            default_max_players = 10 if server_is_bedrock else 20

            if info.is_remote:
                from niksnaks_hosting.shared.remote.remote_process import RemoteServerProcess

                # Nothing here may talk to the far machine. This runs on the main thread
                # -- the window asks for a process while it is still building its first
                # server row -- so a host that is asleep would freeze the app for the
                # whole connect timeout before its window ever appeared. Everything that
                # needs an answer from the host happens on the thread started below.
                process = RemoteServerProcess(
                    host=info.host,
                    server_id=info.unit_id,
                    server_dir=str(info.server_dir),
                    java_path="",
                    java_path_provider=lambda i=info: self.remote_java_path(i),
                    ram_mb=info.ram_mb,
                    max_players=default_max_players,
                    jvm_args=info.jvm_args,
                    loader_type=info.loader_type,
                    edition=info.edition,
                    description=info.name,
                )
                self._processes[server_id] = process
                threading.Thread(
                    target=self._prepare_remote_process,
                    args=(server_id, process),
                    daemon=True,
                ).start()
            else:
                java_path = ""
                if not server_is_bedrock:
                    java_path = (
                        self.java_manager.get_java_path(info.java_version) or shutil.which("java") or "java"
                    )

                config = self.get_config(server_id)
                max_players = default_max_players
                if config:
                    config.load()
                    max_players = config.get_int("max-players", default_max_players)

                process = ServerProcess(
                    server_dir=str(info.server_dir),
                    java_path=java_path,
                    ram_mb=info.ram_mb,
                    max_players=max_players,
                    jvm_args=info.jvm_args,
                    loader_type=info.loader_type,
                    edition=info.edition,
                )
                self._processes[server_id] = process
                # This folder may already have a server running in it that this app did
                # not start -- another copy of it here, or a unit a copy on another
                # computer wrote onto this machine. Scanning the process table is not
                # main-thread work, and the row can catch up when the answer lands.
                threading.Thread(target=process.adopt_running_state, daemon=True).start()

        return self._processes[server_id]

    def _prepare_remote_process(self, server_id: str, process) -> None:
        """Everything about a remote server that only the far machine can answer.

        Whether it is running comes first: that is what the server list is waiting to
        find out, and a machine that has gone away then says so after one connect
        timeout instead of sitting through a second one for a player limit.
        """
        process.adopt_running_state()
        if process.host_unreachable:
            return

        config = self.get_config(server_id)
        if config:
            try:
                config.load()
                process.set_max_players(config.get_int("max-players", process.max_players))
            except Exception:
                # The player limit is a label on a status dot, not a reason to give up
                # on a server whose state has already been established.
                pass

    def refresh_server_states(self) -> None:
        """Ask every machine which of its servers are up, without holding up the app.

        A server's state is never something this app can simply remember. One on another
        computer keeps running while the app is closed and can be started from over
        there while it is open; one on this computer can just as easily have been started
        by another copy of this app, or by a unit that a copy on another computer wrote
        onto this machine over SSH. Either way the answer is asked for, and asked for
        again.

        One worker for the whole sweep, and never two at once: a host that has gone away
        holds each of its servers up for a connect timeout, and rounds that overlapped
        would pile threads up behind it for as long as it stayed away.
        """
        # Before asking what is running: a server another copy of this app has just
        # made on this machine is one this app has to know about before it can ask.
        self.absorb_external_changes()
        # And a host that was asleep when something changed here is told now.
        self._mirror_to_hosts()

        if not self._remote_refresh.acquire(blocking=False):
            return

        server_ids = [info.id for info in self.servers]
        if not server_ids:
            self._remote_refresh.release()
            return

        def worker() -> None:
            try:
                # One walk of this machine's processes for the whole sweep, rather than
                # one per local server; every refresh_state below reads the same answer.
                try:
                    running_server_pids(fresh=True)
                except Exception:
                    pass
                for server_id in server_ids:
                    process = self.get_existing_process(server_id)
                    try:
                        if process is None:
                            # Making the process is itself what asks, so there is
                            # nothing more to do for one that was not there yet.
                            self.get_process(server_id)
                        else:
                            process.refresh_state()
                    except Exception:
                        continue
            finally:
                self._remote_refresh.release()

        threading.Thread(target=worker, daemon=True).start()

    def remote_java(self, info: ServerInfo):
        """The Java manager for the host a server lives on."""
        from niksnaks_hosting.shared.remote.remote_java import RemoteJavaManager

        manager = self._remote_java.get(info.host_id)
        if manager is None:
            manager = RemoteJavaManager(info.host)
            self._remote_java[info.host_id] = manager
        return manager

    def remote_java_path(self, info: ServerInfo) -> str:
        try:
            return self.remote_java(info).java_path(info.java_version)
        except Exception:
            return ""

    def get_existing_process(self, server_id: str) -> ServerProcess | None:
        return self._processes.get(server_id)

    def get_config(self, server_id: str) -> ConfigManager | None:
        info = self._servers.get(server_id)
        if not info:
            return None
        # The path object, not its text: for a remote server the object is what knows
        # which machine the file is on.
        return ConfigManager(info.server_dir)

    def is_any_server_running(self) -> bool:
        return any(p.is_running for p in self._processes.values())

    def get_running_server_ids(self) -> list[str]:
        return [sid for sid, p in self._processes.items() if p.is_running]

    def get_running_local_server_ids(self) -> list[str]:
        """Running servers whose world is on this computer.

        The list closing the app works from. A server on a host is deliberately not in
        it: that machine keeps it running on its own, and the copy of this app over
        there is what saves and backs it up when the host is finished with it. See
        shutdown_all_gracefully.
        """
        ids = []
        for server_id, process in self._processes.items():
            if not process.is_running:
                continue
            info = self._servers.get(server_id)
            if info is None or info.is_remote:
                continue
            ids.append(server_id)
        return ids

    def is_any_local_server_running(self) -> bool:
        return bool(self.get_running_local_server_ids())

    def server_bound_ports(self, info: ServerInfo) -> set[int]:
        """The ports *info* claims once it starts, read from its own server.properties."""
        default = default_port_for_edition(info.edition)
        try:
            cfg = ConfigManager(info.server_dir)
            cfg.load()
            port = cfg.get_int("server-port", default)
            # Bedrock's IPv6 port is configured separately, so read it rather than assume.
            ports = {port, cfg.get_int("server-portv6", port + 1)} if is_bedrock(info.edition) else {port}
        except Exception:
            return set()
        return {p for p in ports if MIN_SERVER_PORT <= p <= MAX_SERVER_PORT}

    def ports_in_use(self, exclude_server_id: str = "") -> dict[int, str]:
        """Ports the existing servers already claim, mapped to the server claiming each."""
        owners: dict[int, str] = {}
        for sid, info in self._servers.items():
            if sid == exclude_server_id:
                continue
            for port in self.server_bound_ports(info):
                owners.setdefault(port, info.name)
        return owners

    def find_port_conflict(
        self, port: int, edition: str = EDITION_JAVA, exclude_server_id: str = ""
    ) -> str | None:
        """Name of the server already holding *port* (or its Bedrock IPv6 partner), if any."""
        owners = self.ports_in_use(exclude_server_id)
        for candidate in sorted(ports_bound_by(port, edition)):
            if candidate in owners:
                return owners[candidate]
        return None

    def get_next_available_port(self, edition: str = EDITION_JAVA, exclude_server_id: str = "") -> int:
        """The first port from this edition's default upwards that no server has claimed.

        Bedrock steps in twos because it takes the next port for IPv6 as well.
        """
        owners = self.ports_in_use(exclude_server_id)
        step = 2 if is_bedrock(edition) else 1
        port = default_port_for_edition(edition)
        while port + step - 1 <= MAX_SERVER_PORT and not ports_bound_by(port, edition).isdisjoint(owners):
            port += step
        return port

    def set_server_port(self, server_id: str, port: int) -> None:
        info = self._servers.get(server_id)
        if not info:
            return
        cfg = self.get_config(server_id)
        if cfg:
            cfg.load()
            cfg.set_value("server-port", port)
            # Bedrock binds IPv6 separately; leaving it behind would collide with whichever
            # server the old pair belonged to.
            if is_bedrock(info.edition):
                cfg.set_value("server-portv6", port + 1)
            cfg.save()

    def get_bedrock_port(self, server_id: str) -> int:
        info = self._servers.get(server_id)
        if not info:
            return BEDROCK_DEFAULT_PORT
        # A Bedrock Edition server listens on this port itself; on a Java server the
        # Bedrock traffic goes to Geyser, which uses its own configured port.
        if is_bedrock(info.edition):
            try:
                server_cfg = self.get_config(server_id)
                if server_cfg:
                    server_cfg.load()
                    return server_cfg.get_int("server-port", BEDROCK_DEFAULT_PORT)
            except Exception:
                pass
            return BEDROCK_DEFAULT_PORT
        try:
            cfg = load_playit_config(info.server_dir)
            return int(cfg.get("bedrock_port", BEDROCK_DEFAULT_PORT))
        except Exception:
            return BEDROCK_DEFAULT_PORT

    def get_voicechat_port(self, server_id: str) -> int:
        info = self._servers.get(server_id)
        if not info:
            return 24454
        try:
            cfg = load_playit_config(info.server_dir)
            return int(cfg.get("voicechat_port", 24454))
        except Exception:
            return 24454

    def set_bedrock_port(self, server_id: str, port: int) -> None:
        info = self._servers.get(server_id)
        if not info:
            return
        from niksnaks_hosting.shared.backend.playit_config import save_playit_config

        cfg = load_playit_config(info.server_dir)
        cfg["bedrock_port"] = port
        save_playit_config(info.server_dir, cfg)

    def set_voicechat_port(self, server_id: str, port: int) -> None:
        info = self._servers.get(server_id)
        if not info:
            return
        from niksnaks_hosting.shared.backend.playit_config import save_playit_config

        cfg = load_playit_config(info.server_dir)
        cfg["voicechat_port"] = port
        save_playit_config(info.server_dir, cfg)

    def check_bedrock_port_conflict(self, server_id: str) -> int | None:
        port = self.get_bedrock_port(server_id)
        if not self.has_bedrock_tunnel(server_id):
            for sid in self._servers:
                if sid != server_id and self.has_bedrock_tunnel(sid):
                    break
            else:
                return None
        for sid, info in self._servers.items():
            if sid == server_id:
                continue
            proc = self._processes.get(sid)
            if not proc or not proc.is_running:
                continue
            if self.get_bedrock_port(sid) == port:
                return port
        return None

    def check_voicechat_port_conflict(self, server_id: str) -> int | None:
        port = self.get_voicechat_port(server_id)
        if not self.has_voicechat_tunnel(server_id):
            for sid in self._servers:
                if sid != server_id and self.has_voicechat_tunnel(sid):
                    break
            else:
                return None
        for sid, info in self._servers.items():
            if sid == server_id:
                continue
            proc = self._processes.get(sid)
            if not proc or not proc.is_running:
                continue
            if self.get_voicechat_port(sid) == port:
                return port
        return None

    def has_bedrock_tunnel(self, server_id: str) -> bool:
        info = self._servers.get(server_id)
        if not info:
            return False
        try:
            cfg = load_playit_config(info.server_dir)
            return bool(str(cfg.get("bedrock_endpoint", "")).strip())
        except Exception:
            return False

    def has_voicechat_tunnel(self, server_id: str) -> bool:
        info = self._servers.get(server_id)
        if not info:
            return False
        try:
            cfg = load_playit_config(info.server_dir)
            return bool(str(cfg.get("voicechat_endpoint", "")).strip())
        except Exception:
            return False

    def check_port_conflict(self, server_id: str) -> int | None:
        """The port stopping this server from starting, because a running one holds it."""
        info = self._servers.get(server_id)
        if not info:
            return None
        mine = self.server_bound_ports(info)
        if not mine:
            return None
        for sid, other in self._servers.items():
            if sid == server_id:
                continue
            proc = self._processes.get(sid)
            if not proc or not proc.is_running:
                continue
            clash = mine & self.server_bound_ports(other)
            if clash:
                return min(clash)
        return None

    def begin_mod_operation(self, server_id: str) -> None:
        if not server_id:
            return
        count = int(self._mods_operation_counts.get(server_id, 0)) + 1
        self._mods_operation_counts[server_id] = count
        self.emit_on_main_thread("mods-operation-changed", server_id, True, count)

    def end_mod_operation(self, server_id: str) -> None:
        if not server_id:
            return
        count = int(self._mods_operation_counts.get(server_id, 0)) - 1
        if count <= 0:
            self._mods_operation_counts.pop(server_id, None)
            count = 0
            active = False
        else:
            self._mods_operation_counts[server_id] = count
            active = True
        self.emit_on_main_thread("mods-operation-changed", server_id, active, count)

    def is_mod_operation_active(self, server_id: str) -> bool:
        if not server_id:
            return False
        return int(self._mods_operation_counts.get(server_id, 0)) > 0

    def stop_all(self):
        """Last-resort stop for teardown that cannot afford to wait.

        shutdown_all_gracefully is the one that gives a world as long as it needs;
        this runs from do_shutdown, where the main loop is already going away and
        nothing is left to report progress to, so the grace period is short.

        Servers on a host are left out of it, exactly as they are there: this app going
        away is not a thing that happened to another computer.
        """
        self.playit_manager.stop()
        for server_id in self.get_running_local_server_ids():
            process = self._processes.get(server_id)
            if process:
                process.stop_and_wait(timeout=EMERGENCY_STOP_SECONDS)

    def shutdown_all_gracefully(self, progress_callback: Callable[[float, str, str], None] | None = None) -> None:
        """Stop every server running *on this computer* properly, and back its world up.

        This is what quitting the app runs in place of stop_all. Each server is asked to
        save and exit and is only killed if it ignores that, and the auto backup a normal
        stop would trigger is taken here too -- the poll loop that normally takes those is
        tied to the window, which is on its way out by the time this matters.

        Which servers those are is the whole point of the list below. A server on a host
        is not one of them: it is kept alive by that machine's own service manager and
        was already running before this window opened, so closing a window here has no
        more business shutting it down than closing a browser tab has. It keeps running,
        and the copy of this app on the host stops and backs it up when that machine's
        app is closed -- including servers somebody started on it from over here, which
        are, from the host's point of view, servers running on the host with nobody
        local watching them.

        Blocking, and slow by design: callers run it on a worker thread and keep a
        progress dialog up, reporting (fraction, title, detail).
        """
        running = self.get_running_local_server_ids()
        self.playit_manager.stop()
        if not running:
            return

        want_backups = self.preferences.auto_backup_on_stop
        # Stopping is one step per server, backing up another, so the bar spends half its
        # length on each phase instead of jumping.
        steps = len(running) * (2 if want_backups else 1)
        step = 0

        def report(fraction: float, title: str, detail: str) -> None:
            if progress_callback:
                progress_callback(max(0.0, min(1.0, fraction)), title, detail)

        def name_of(server_id: str) -> str:
            info = self.get_server(server_id)
            return info.name if info else server_id

        # Everything comes down first and is only then backed up: if the machine is being
        # shut down behind us, the servers being off is the part that has to happen early.
        # A server that would not come down is remembered, because backing a world up
        # while it is still being written to is how a backup ends up unrestorable.
        down: list[str] = []
        for server_id in running:
            name = name_of(server_id)
            base = step / steps
            report(base, _("Stopping servers"), _("Saving and stopping {}...").format(name))

            # Nothing here says how far into its save a server is, so the elapsed seconds
            # are the only honest sign of life while the bar sits on one step.
            def on_wait(elapsed: float) -> None:
                report(base, "", _("Saving and stopping {}... {}s").format(name, int(elapsed)))

            process = self._processes.get(server_id)
            if process:
                process.stop_and_wait(on_wait=on_wait)
                if not process.is_running:
                    down.append(server_id)
            step += 1

        if want_backups:
            for server_id in down:
                name = name_of(server_id)
                base = step / steps

                def on_backup_progress(fraction: float, message: str) -> None:
                    report(base + fraction / steps, "", _("{}: {}").format(name, message))

                report(base, _("Backing up worlds"), _("Backing up {}...").format(name))
                # A world with no folder yet just reports back that there was nothing to
                # take; the save above already did the part that matters.
                self.create_world_backup(server_id, auto=True, progress_callback=on_backup_progress)
                step += 1

        report(1.0, _("Closing"), "")

    def _configured_level_name(self, server_root: Path) -> str:
        try:
            cfg = ConfigManager(server_root)
            cfg.load()
            name = cfg.get("level-name", "world").strip()
            return name or "world"
        except Exception:
            return "world"

    def _is_world_dir(self, item: Path, level_name: str) -> bool:
        if not item.is_dir():
            return False

        if (item / "level.dat").exists():
            return True

        if item.name.casefold() == level_name.casefold():
            return True

        markers = (
            "region",
            "data",
            "playerdata",
            "poi",
            "entities",
            "stats",
            "advancements",
            "dimensions",
            "DIM-1",
            "DIM1",
            "session.lock",
            "uid.dat",
        )
        return any((item / marker).exists() for marker in markers)

    def _is_importable_world_dir(self, item: Path) -> bool:
        if not item.is_dir():
            return False
        if not (item / "level.dat").is_file():
            return False

        markers = (
            "db",  # Bedrock keeps every dimension in one LevelDB
            "region",
            "data",
            "playerdata",
            "poi",
            "entities",
            "stats",
            "advancements",
            "dimensions",
            "DIM-1",
            "DIM1",
            "session.lock",
            "uid.dat",
        )
        return any((item / marker).exists() for marker in markers)

    def _pick_world_dir(self, container: Path, level_name: str, preferred_name: str) -> list[Path]:
        preferred = container / preferred_name
        if self._is_world_dir(preferred, level_name):
            return [preferred]

        try:
            worlds = [item for item in container.iterdir() if self._is_world_dir(item, level_name)]
        except OSError:
            return []
        if not worlds:
            return []

        return [sorted(worlds, key=lambda p: p.name.lower())[0]]

    def _iter_world_dirs(self, server_root: Path) -> list[Path]:
        if not server_root.is_dir():
            return []

        level_name = self._configured_level_name(server_root)

        # Bedrock keeps its worlds one level down, in worlds/<level-name>/.
        bedrock_root = server_root / BEDROCK_WORLDS_DIR
        if bedrock_root.is_dir():
            found = self._pick_world_dir(bedrock_root, level_name, level_name)
            if found:
                return found

        return self._pick_world_dir(server_root, level_name, "world")

    def create_world_folder(self, server_id: str, name: str, seed: str = "", level_type: str = "") -> tuple[bool, str]:
        info = self.get_server(server_id)
        if not info:
            return False, _("Server not found")

        process = self._processes.get(server_id)
        if process and process.is_running:
            return False, _("Server is running")

        root = info.server_dir
        bedrock = is_bedrock(info.edition)
        level_name = self._configured_level_name(root)
        # Bedrock regenerates worlds/<level-name>/ on next boot and keeps its own name.
        container = root / BEDROCK_WORLDS_DIR if bedrock else root
        world_dir = container / (level_name or "Bedrock level") if bedrock else root / "world"

        try:
            container.mkdir(parents=True, exist_ok=True)
            for item in container.iterdir():
                if not self._is_world_dir(item, level_name):
                    continue
                if item.resolve() == world_dir.resolve():
                    continue
                if item.exists():
                    robust_rmtree(item)

            if world_dir.exists():
                robust_rmtree(world_dir)

            world_dir.mkdir(parents=True, exist_ok=True)
            cfg = ConfigManager(root)
            cfg.load()
            if not bedrock:
                cfg.set_value("level-name", "world")
            cfg.set_value("level-seed", seed.strip())
            if level_type and not bedrock:
                cfg.set_value("level-type", level_type)
            cfg.save()
        except Exception as e:
            return False, str(e)

        self.emit_on_main_thread("server-changed", server_id)
        return True, world_dir.name

    def _find_world_root(self, extracted: Path) -> Path | None:
        """Locate the world inside an extracted archive.

        A .mcworld puts level.dat straight at the archive root, while our own world
        zips nest it one level down under the world folder's name.
        """
        if self._is_importable_world_dir(extracted):
            return extracted

        try:
            children = sorted((p for p in extracted.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
        except OSError:
            return None

        return next((child for child in children if self._is_importable_world_dir(child)), None)

    def import_world_folder(self, server_id: str, source: str | Path) -> tuple[bool, str]:
        """Import a world from a folder or from a .mcworld/.zip archive."""
        info = self.get_server(server_id)
        if not info:
            return False, _("Server not found")

        process = self._processes.get(server_id)
        if process and process.is_running:
            return False, _("Server is running")

        src = Path(source).expanduser()

        if src.is_file():
            if src.suffix.lower() not in WORLD_ARCHIVE_SUFFIXES:
                return False, _("Select a .mcworld or .zip world archive")
            with scratch_dir("niksnaks-hosting-world-") as td:
                staged = td / "extracted"
                try:
                    _safe_extract_zip(src, staged)
                except (OSError, ValueError, zipfile.BadZipFile) as e:
                    return False, str(e)

                world = self._find_world_root(staged)
                if not world:
                    return False, _("Archive does not contain a Minecraft world")
                return self._replace_server_world(info, world)

        if not src.is_dir():
            return False, _("Selected world folder does not exist")
        if not self._is_importable_world_dir(src):
            return False, _("Selected folder does not look like a Minecraft world")

        return self._replace_server_world(info, src)

    def _replace_server_world(self, info: ServerInfo, src: Path) -> tuple[bool, str]:
        root = info.server_dir
        bedrock = is_bedrock(info.edition)
        level_name = self._configured_level_name(root)
        container = root / BEDROCK_WORLDS_DIR if bedrock else root
        dst = container / (level_name or "Bedrock level") if bedrock else root / "world"
        try:
            container.mkdir(parents=True, exist_ok=True)
            for item in container.iterdir():
                if not self._is_world_dir(item, level_name):
                    continue
                if item.resolve() == dst.resolve():
                    continue
                if item.exists():
                    # A remnant the delete skips over would merge into the imported
                    # world, so this one is not allowed to fail quietly.
                    robust_rmtree(item)

            if dst.exists():
                robust_rmtree(dst)

            # Bedrock worlds carry their settings inside the level, not in server.properties,
            # and their level.dat is not the Java NBT format this reader understands.
            seed, wtype = ("", "")
            if not bedrock:
                from niksnaks_hosting.shared.utils.nbt_utils import get_world_info

                seed, wtype = get_world_info(src)

            shutil.copytree(src, dst)
            cfg = ConfigManager(root)
            cfg.load()
            if not bedrock:
                cfg.set_value("level-name", "world")
                cfg.set_value("level-seed", seed or "")
                if wtype:
                    cfg.set_value("level-type", wtype)
            cfg.save()
        except Exception as e:
            if dst.exists():
                robust_rmtree(dst)
            return False, str(e)

        self.emit_on_main_thread("server-changed", info.id)
        return True, dst.name

    def export_world_zip(self, server_id: str, world: str | Path, destination: str | Path) -> tuple[bool, str]:
        info = self.get_server(server_id)
        if not info:
            return False, _("Server not found")

        world_path = Path(world)
        if not world_path.is_absolute():
            world_path = info.server_dir / world_path
        if not world_path.is_dir():
            return False, _("World folder does not exist")

        try:
            world_path.resolve().relative_to(info.server_dir.resolve())
        except ValueError:
            return False, _("World folder is outside this server")

        dest = Path(destination).expanduser()
        if dest.suffix.lower() not in WORLD_ARCHIVE_SUFFIXES:
            dest = dest.with_name(dest.name + ".zip")
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Minecraft only recognises a .mcworld whose contents sit at the archive
        # root; our own .zip exports keep the wrapping world folder.
        flat = dest.suffix.lower() == ".mcworld"

        try:
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                for item in world_path.rglob("*"):
                    if not item.is_file():
                        continue
                    relative = item.relative_to(world_path)
                    arc = relative if flat else Path(world_path.name) / relative
                    zf.write(item, arcname=str(arc).replace("\\", "/"))
        except Exception as e:
            return False, str(e)

        return True, str(dest)

    def create_world_backup(self, server_id: str, auto: bool = False, progress_callback=None) -> tuple[bool, str]:
        info = self.get_server(server_id)
        if not info:
            return False, _("Server not found")

        process = self._processes.get(server_id)
        if process and process.is_running:
            return False, _("Server is running")

        root = info.server_dir
        if not root.exists():
            return False, _("Server directory does not exist")

        worlds = self._iter_world_dirs(root)
        if not worlds:
            return False, _("No world folder found")

        backups_dir = root / BACKUPS_DIR
        backups_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        prefix = "niksnaks-hosting-auto-backup" if auto else "niksnaks-hosting-backup"
        backup_path = backups_dir / f"{prefix}-{stamp}.zip"

        try:
            with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
                world_dir = worlds[0]
                # Listing first costs a second walk but is what turns the zip loop into a
                # fraction; without it a backup taken while the app is quitting looks hung.
                files = [item for item in world_dir.rglob("*") if item.is_file()]
                total = len(files)
                for done, item in enumerate(files, start=1):
                    arc = item.relative_to(root)
                    zf.write(item, arcname=str(arc).replace("\\", "/"))
                    if progress_callback:
                        progress_callback(
                            done / total if total else 1.0,
                            _("Backing up world... {}/{}").format(done, total),
                        )
        except Exception as e:
            return False, str(e)

        self._cleanup_old_backups(server_id)
        return True, backup_path.name

    def create_full_backup(self, server_id: str) -> tuple[bool, str]:
        info = self.get_server(server_id)
        if not info:
            return False, _("Server not found")

        process = self._processes.get(server_id)
        if process and process.is_running:
            return False, _("Server is running")

        root = info.server_dir
        if not root.exists():
            return False, _("Server directory does not exist")

        backups_dir = root / BACKUPS_DIR
        backups_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        version = info.mc_version if info.mc_version else "unknown"
        backup_path = backups_dir / f"niksnaks-hosting-full-backup-{version}-{stamp}.zip"

        try:
            with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for item in root.rglob("*"):
                    if not item.is_file():
                        continue

                    if hasattr(item, "is_relative_to") and item.is_relative_to(backups_dir):
                        continue
                    elif str(item).startswith(str(backups_dir)):
                        continue

                    arc = item.relative_to(root)
                    zf.write(item, arcname=str(arc).replace("\\", "/"))
        except Exception as e:
            return False, str(e)

        self._cleanup_old_backups(server_id)
        return True, backup_path.name

    def list_full_backups(self) -> list[FullBackupOption]:
        """Every full backup across all servers, newest first."""
        options: list[FullBackupOption] = []

        for info in self._servers.values():
            for dir_name in BACKUP_DIR_NAMES:
                backups_dir = info.server_dir / dir_name
                if not backups_dir.is_dir():
                    continue
                try:
                    entries = list(backups_dir.iterdir())
                except OSError:
                    continue

                for path in entries:
                    if path.suffix.lower() != ".zip" or not path.name.startswith(FULL_BACKUP_PREFIXES):
                        continue
                    if not path.is_file():
                        continue
                    try:
                        created = datetime.fromtimestamp(path.stat().st_mtime)
                    except OSError:
                        continue
                    options.append(
                        FullBackupOption(
                            path=path,
                            server_id=info.id,
                            server_name=info.name,
                            edition=info.edition,
                            mc_version=self.backup_game_version(path) or info.mc_version,
                            created_at=created,
                        )
                    )

        return sorted(options, key=lambda option: option.created_at, reverse=True)

    @staticmethod
    def peek_backup_edition(archive: Path) -> str:
        """Read a backup's archive index to see which edition it holds, without extracting."""
        try:
            with zipfile.ZipFile(archive, "r") as zf:
                names = zf.namelist()
        except (OSError, zipfile.BadZipFile):
            return EDITION_JAVA

        roots = {name.split("/", 1)[0] for name in names}
        return EDITION_BEDROCK if roots.intersection(BEDROCK_BINARY_NAMES) else EDITION_JAVA

    @staticmethod
    def _detect_backup_layout(staged: Path, source: ServerInfo | None) -> tuple[str, str]:
        """Work out (edition, loader) from the files a backup actually contains.

        The archive is the only reliable witness for a backup browsed off disk; a
        backup taken from a known server just confirms what that server already says.
        """
        from niksnaks_hosting.shared.backend.loader_launch import is_loader_installed

        if any((staged / name).is_file() for name in BEDROCK_BINARY_NAMES):
            return EDITION_BEDROCK, LOADER_FABRIC
        if source and is_bedrock(source.edition):
            return EDITION_BEDROCK, LOADER_FABRIC

        for loader in (LOADER_NEOFORGE, LOADER_FORGE, LOADER_FABRIC):
            if is_loader_installed(staged, loader):
                return EDITION_JAVA, loader

        return EDITION_JAVA, source.loader_type if source else LOADER_FABRIC

    def create_server_from_backup(
        self,
        name: str,
        archive: str | Path,
        ram_mb: int = DEFAULT_RAM_MB,
        source_server_id: str = "",
        progress_callback=None,
        port: int | None = None,
    ) -> tuple[bool, str, ServerInfo | None]:
        """Build a brand-new server out of a full backup, with no download needed."""

        def report(fraction: float, message: str) -> None:
            if progress_callback:
                progress_callback(fraction, message)

        src = Path(archive).expanduser()
        if not src.is_file():
            return False, _("Backup file not found"), None
        if src.suffix.lower() != ".zip":
            return False, _("Select a full backup .zip archive"), None

        source = self.get_server(source_server_id) if source_server_id else None

        # The archive is staged before it is copied into place, so the restore needs room
        # for both copies at once.
        needed = install_space_needed(src.stat().st_size)
        available = free_bytes(SERVERS_DIR)
        if available < needed:
            return False, _("Not enough free space: about {needed} is needed and {free} is free.").format(
                needed=human_size(needed), free=human_size(available)
            ), None

        with scratch_dir("niksnaks-hosting-clone-") as td:
            staged = td / "server"
            report(0.05, _("Reading backup..."))
            try:
                _safe_extract_zip(src, staged)
            except (OSError, ValueError, zipfile.BadZipFile) as e:
                return False, str(e), None

            if not any(staged.iterdir()):
                return False, _("Backup archive is empty"), None

            report(0.45, _("Detecting server type..."))
            edition, loader_type = self._detect_backup_layout(staged, source)
            mc_version = self.backup_game_version(src) or (source.mc_version if source else "")

            info = self.add_server(
                name=name,
                mc_version=mc_version,
                loader_version=source.loader_version if source else "",
                ram_mb=ram_mb,
                java_version=source.java_version if source else None,
                loader_type=loader_type,
                edition=edition,
            )

            report(0.55, _("Copying server files..."))
            try:
                for item in staged.iterdir():
                    # The new server starts its own backup history rather than
                    # inheriting the source server's archives.
                    if item.name in BACKUP_DIR_NAMES:
                        continue
                    destination = info.server_dir / item.name
                    if item.is_dir():
                        shutil.copytree(item, destination, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, destination)
            except Exception as e:
                self.delete_server(info.id, delete_files=True)
                return False, str(e), None

        report(0.92, _("Applying server settings..."))
        try:
            cfg = ConfigManager(info.server_dir)
            cfg.load()
            # A clone arrives on its source server's port, so a port of its own is what
            # lets the original and the copy run side by side.
            chosen_port = port or self.get_next_available_port(edition, exclude_server_id=info.id)
            cfg.set_value("server-port", chosen_port)
            if is_bedrock(edition):
                cfg.set_value("server-portv6", chosen_port + 1)
                cfg.set_value("server-name", name)
            cfg.save()
            if not is_bedrock(edition):
                cfg.set_eula(True)
        except Exception as e:
            self.delete_server(info.id, delete_files=True)
            return False, str(e), None

        if self.bedrock_runtime_missing(info.id):
            report(0.95, _("Downloading Bedrock server files..."))
            self.repair_bedrock_runtime(
                info.id,
                progress_callback=lambda frac, text: report(0.95 + frac * 0.05, text),
            )

        self.emit_on_main_thread("server-changed", info.id)
        return True, info.id, info

    def _cleanup_old_backups(self, server_id: str) -> None:
        if not self.preferences.auto_delete_old_backups:
            return
        info = self.get_server(server_id)
        if not info:
            return
        backups_dir = info.server_dir / BACKUPS_DIR
        if not backups_dir.exists():
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for p in backups_dir.iterdir():
            if p.suffix != ".zip":
                continue
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    p.unlink()
            except OSError:
                continue

    def restore_world_backup(self, server_id: str, zip_path: Path, progress_callback=None) -> tuple[bool, str]:
        """Put a backup back on disk, reporting (fraction, message) as it goes.

        The work splits into reading the archive out to a staging folder and copying that
        into the server, and both halves are slow enough to be worth showing separately.
        """

        def report(fraction: float, message: str) -> None:
            if progress_callback:
                progress_callback(max(0.0, min(1.0, fraction)), message)

        info = self.get_server(server_id)
        if not info:
            return False, _("Server not found")

        process = self._processes.get(server_id)
        if process and process.is_running:
            return False, _("Server is running")

        root = info.server_dir
        if not root.exists():
            return False, _("Server directory does not exist")

        if not zip_path.exists():
            return False, _("Backup file not found")

        is_full = zip_path.name.startswith(FULL_BACKUP_PREFIXES)

        try:
            with scratch_dir("niksnaks-hosting-restore-") as td:
                tmp_root = td.resolve()

                report(0.01, _("Reading backup..."))
                _safe_extract_zip(
                    zip_path,
                    tmp_root,
                    on_progress=lambda done, total, name: report(
                        0.02 + 0.48 * (done / total if total else 1.0),
                        _("Reading backup... {}/{}").format(done, total),
                    ),
                )

                # Copying is the other half of the work, so it needs its own file count
                # to divide by. The tree was just written, so this walk is cheap.
                copy_total = 0
                copied = 0

                def on_copied(_name: str) -> None:
                    nonlocal copied
                    copied += 1
                    report(
                        0.55 + 0.42 * (copied / copy_total if copy_total else 1.0),
                        _("Restoring files... {}/{}").format(copied, copy_total),
                    )

                if is_full:
                    report(0.51, _("Clearing old server files..."))
                    for item in root.iterdir():
                        if item.name in BACKUP_DIR_NAMES:
                            continue
                        # Anything that survives here ends up mixed into the restored
                        # server, so locked and read-only files must go too.
                        robust_rmtree(item)

                    copy_total = _count_files(tmp_root)
                    report(0.55, _("Restoring files..."))
                    for item in tmp_root.iterdir():
                        _copy_tree_with_progress(item, root / item.name, on_file=on_copied)
                else:

                    extracted_worlds = self._iter_world_dirs(tmp_root)
                    if not extracted_worlds:
                        return False, _("This backup does not contain any world data.")

                    bedrock = is_bedrock(info.edition)
                    container = root / BEDROCK_WORLDS_DIR if bedrock else root
                    container.mkdir(parents=True, exist_ok=True)

                    copy_total = sum(_count_files(world) for world in extracted_worlds)
                    report(0.51, _("Clearing the old world..."))

                    level_name = "world"
                    for item in container.iterdir():
                        if not item.is_dir():
                            continue
                        if (
                            (item / "level.dat").exists()
                            or item.name.casefold() == level_name.casefold()
                            or any(
                                (item / marker).exists()
                                for marker in (
                                    "region",
                                    "data",
                                    "playerdata",
                                    "poi",
                                    "entities",
                                    "stats",
                                    "advancements",
                                    "dimensions",
                                    "DIM-1",
                                    "DIM1",
                                    "session.lock",
                                    "uid.dat",
                                )
                            )
                        ):
                            robust_rmtree(item)

                    report(0.55, _("Restoring world..."))
                    for item in extracted_worlds:
                        # Bedrock worlds keep their own folder name; Java's is always "world".
                        dst = container / item.name if bedrock else root / "world"
                        if dst.is_dir():
                            robust_rmtree(dst)
                        _copy_tree_with_progress(item, dst, on_file=on_copied)

            if not is_full or not self.bedrock_runtime_missing(server_id):
                report(1.0, _("Restored."))
                return True, _("Restored.")

            # The runtime download reports 0..1 of its own, so it lands in the tail here
            # rather than sending the bar back to the start.
            report(0.97, _("Downloading Bedrock server files..."))
            ok, detail = self.repair_bedrock_runtime(
                server_id,
                progress_callback=lambda frac, text: report(0.97 + frac * 0.03, text),
            )
            if ok:
                return True, _("Restored. {}").format(detail)
            return True, _("Restored, but the Bedrock server files could not be installed: {}").format(detail)
        except Exception as e:
            return False, str(e)
