"""What the app knows about the mods in a server folder, and how they get there.

The server folder carries four small JSON files next to ``mods/`` that remember
where each jar came from -- which Modrinth project and version, which modpack put it
there, and which other mod asked for it as a dependency. The Files tab and the web
panel both read and write them through here, so a mod installed from one shows up
with its name and version in the other, and neither leaves a dependency behind that
nothing needs any more.

Every path goes through the server directory it is given (a Path here, a RemotePath
for a server on another machine), never through open(), so it all works over SFTP.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from niksnaks_hosting.shared.backend import modrinth_client
from niksnaks_hosting.shared.backend.modrinth_client import ModrinthVersion

MOD_STATE_FILE = ".niksnaks-hosting-mod-installs.json"
MODPACK_STATE_FILE = ".niksnaks-hosting-modpacks.json"
DATAPACK_STATE_FILE = ".niksnaks-hosting-datapack-installs.json"
DEPENDENCY_STATE_FILE = ".niksnaks-hosting-mod-dependencies.json"


def _read_json(path: Any) -> dict:
    try:
        if path is None or not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _write_json(path: Any, data: dict) -> bool:
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _clean_entry(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    filename = str(item.get("filename", "")).strip()
    if not filename:
        return None
    return {
        "title": str(item.get("title", "")).strip(),
        "version_id": str(item.get("version_id", "")).strip(),
        "version_number": str(item.get("version_number", "")).strip(),
        "filename": filename,
    }


def _jar_names(names: Any) -> list[str]:
    if not isinstance(names, list):
        return []
    return sorted({Path(str(m)).name.strip().lower() for m in names if str(m).strip().lower().endswith(".jar")})


class ModState:
    """The four bookkeeping files of one server folder."""

    def __init__(self, root: Any):
        self.root = root

    def _file(self, name: str) -> Any:
        # No server folder yet (the tab is still loading) reads as empty and writes nowhere.
        return None if self.root is None else self.root / name

    # -- individually installed mods -------------------------------------------------

    def read_mods(self) -> dict:
        raw = _read_json(self._file(MOD_STATE_FILE)).get("mods")
        cleaned = {}
        for project_id, item in (raw or {}).items() if isinstance(raw, dict) else []:
            pid = str(project_id).strip()
            entry = _clean_entry(item)
            if pid and entry:
                cleaned[pid] = entry
        return {"mods": cleaned}

    def write_mods(self, state: dict) -> bool:
        return _write_json(self._file(MOD_STATE_FILE), state)

    def record_mod(self, project_id: str, title: str, version_id: str, filename: str, version_number: str = "") -> None:
        pid = str(project_id).strip()
        if not pid:
            return
        state = self.read_mods()
        state["mods"][pid] = {
            "title": str(title or "").strip(),
            "version_id": str(version_id or "").strip(),
            "version_number": str(version_number or "").strip(),
            "filename": str(filename or "").strip(),
        }
        self.write_mods(state)

    # -- datapacks ---------------------------------------------------------------------

    def read_datapacks(self) -> dict:
        raw = _read_json(self._file(DATAPACK_STATE_FILE)).get("datapacks")
        cleaned = {}
        for project_id, item in (raw or {}).items() if isinstance(raw, dict) else []:
            pid = str(project_id).strip()
            entry = _clean_entry(item)
            if pid and entry:
                cleaned[pid] = entry
        return {"datapacks": cleaned}

    def write_datapacks(self, state: dict) -> bool:
        return _write_json(self._file(DATAPACK_STATE_FILE), state)

    def record_datapack(
        self, project_id: str, title: str, version_id: str, filename: str, version_number: str = ""
    ) -> None:
        pid = str(project_id).strip()
        if not pid:
            return
        state = self.read_datapacks()
        state["datapacks"][pid] = {
            "title": str(title or "").strip(),
            "version_id": str(version_id or "").strip(),
            "version_number": str(version_number or "").strip(),
            "filename": str(filename or "").strip(),
        }
        self.write_datapacks(state)

    # -- modpacks ----------------------------------------------------------------------

    def read_modpacks(self) -> dict:
        projects = _read_json(self._file(MODPACK_STATE_FILE)).get("installed_projects")
        normalized: dict[str, dict[str, Any]] = {}
        for project_id, item in (projects or {}).items() if isinstance(projects, dict) else []:
            pid = str(project_id).strip()
            if not pid:
                continue
            # An early release wrote the entry as a repr() string; read that back too.
            if isinstance(item, str):
                text = item.strip()
                if text.startswith("{") and text.endswith("}"):
                    try:
                        recovered = ast.literal_eval(text)
                        if isinstance(recovered, dict):
                            item = recovered
                    except Exception:
                        pass
            if isinstance(item, dict):
                normalized[pid] = {
                    "version_id": str(item.get("version_id", "")).strip(),
                    "version_number": str(item.get("version_number", "")).strip(),
                    "title": str(item.get("title", "")).strip(),
                    "mods": _jar_names(item.get("mods")),
                }
            else:
                normalized[pid] = {"version_id": str(item).strip(), "version_number": "", "title": "", "mods": []}
        return {"installed_projects": normalized}

    def write_modpacks(self, state: dict) -> bool:
        return _write_json(self._file(MODPACK_STATE_FILE), state)

    def record_modpack(
        self, project_id: str, version_id: str, version_number: str = "", title: str = "", mod_files: list | None = None
    ) -> None:
        pid = str(project_id).strip()
        if not pid:
            return
        state = self.read_modpacks()
        state["installed_projects"][pid] = {
            "version_id": str(version_id).strip(),
            "version_number": str(version_number or "").strip(),
            "title": str(title or "").strip(),
            "mods": _jar_names(mod_files or []),
        }
        self.write_modpacks(state)

    def modpack_managed_mods(self) -> dict[str, list[str]]:
        """Lower-cased jar name -> the modpack titles that put it there."""
        managed: dict[str, list[str]] = {}
        for project_id, entry in self.read_modpacks()["installed_projects"].items():
            label = str(entry.get("title", "")).strip() or project_id
            for mod_name in entry.get("mods", []):
                names = managed.setdefault(mod_name, [])
                if label not in names:
                    names.append(label)
        return managed

    # -- dependencies ------------------------------------------------------------------

    def read_dependencies(self) -> dict:
        req = _read_json(self._file(DEPENDENCY_STATE_FILE)).get("required_by")
        cleaned: dict[str, list[str]] = {}
        for dep_name, parents in (req or {}).items() if isinstance(req, dict) else []:
            dep_key = str(dep_name).strip().lower()
            if not dep_key or not isinstance(parents, list):
                continue
            parent_keys = sorted({str(p).strip().lower() for p in parents if str(p).strip()})
            if parent_keys:
                cleaned[dep_key] = parent_keys
        return {"required_by": cleaned}

    def write_dependencies(self, state: dict) -> bool:
        return _write_json(self._file(DEPENDENCY_STATE_FILE), state)

    def record_dependencies(self, parent_filename: str, dep_versions: list) -> None:
        """Remember that *parent_filename* pulled in each of *dep_versions*, and list them as mods."""
        parent_key = str(parent_filename).strip().lower()
        if not parent_key or not dep_versions:
            return
        state = self.read_dependencies()
        req = state["required_by"]
        for dep in dep_versions:
            dep_key = str(getattr(dep, "filename", "")).strip().lower()
            if not dep_key or dep_key == parent_key:
                continue
            req[dep_key] = sorted(set(req.get(dep_key, [])) | {parent_key})

            dep_project_id = str(getattr(dep, "project_id", "")).strip()
            dep_filename = str(getattr(dep, "filename", "")).strip()
            if dep_project_id and dep_filename:
                dep_title = str(
                    getattr(dep, "title", "") or getattr(dep, "name", "") or dep_project_id or dep_key
                ).strip()
                self.record_mod(
                    dep_project_id,
                    dep_title or dep_project_id,
                    str(getattr(dep, "version_id", "")).strip(),
                    dep_filename,
                    version_number=str(getattr(dep, "version_number", "")).strip(),
                )
        self.write_dependencies(state)

    def dependents_of(self, filename: str) -> list[str]:
        """Installed jars that asked for *filename* as a dependency."""
        key = str(filename).strip().lower()
        if not key:
            return []
        parents = list(self.read_dependencies()["required_by"].get(key, []))
        installed = {p.lower() for p in installed_jar_names(self.root)}
        return [p for p in parents if p in installed]

    def forget_mod(self, removed_filename: str) -> None:
        """Take a jar out of every bookkeeping file once it is gone from mods/."""
        key = str(removed_filename).strip().lower()
        if not key:
            return

        req = self.read_dependencies()["required_by"]
        req.pop(key, None)
        new_req = {}
        for dep_key, parents in req.items():
            filtered = [p for p in parents if p != key]
            if filtered:
                new_req[dep_key] = filtered
        self.write_dependencies({"required_by": new_req})

        mods = self.read_mods()["mods"]
        kept = {pid: meta for pid, meta in mods.items() if str(meta.get("filename", "")).strip().lower() != key}
        self.write_mods({"mods": kept})

        projects = self.read_modpacks()["installed_projects"]
        for entry in projects.values():
            entry["mods"] = [m for m in entry.get("mods", []) if m != key]
        self.write_modpacks({"installed_projects": projects})

    def orphaned_dependencies(self, removed_filename: str) -> list[str]:
        """Jars that only *removed_filename* needed. Ask before forget_mod, which erases the record."""
        removed_key = str(removed_filename).strip().lower()
        if not removed_key:
            return []
        req = self.read_dependencies()["required_by"]
        return [dep for dep, parents in req.items() if removed_key in parents and parents == [removed_key]]


# -- the mods folder ---------------------------------------------------------------------


def mods_dir(root: Any) -> Any:
    return root / "mods"


def installed_jar_names(root: Any) -> list[str]:
    if root is None:
        return []
    folder = mods_dir(root)
    if not folder.is_dir():
        return []
    return sorted((p.name for p in folder.glob("*.jar")), key=str.lower)


def find_jar(root: Any, filename: str) -> Any | None:
    """The jar called *filename* in mods/, matched without regard to case."""
    name = str(filename).strip()
    if root is None or not name or "/" in name or "\\" in name or name != Path(name).name:
        return None
    folder = mods_dir(root)
    direct = folder / name
    if direct.exists():
        return direct
    name_l = name.lower()
    for jar in folder.glob("*.jar") if folder.is_dir() else []:
        if jar.name.lower() == name_l:
            return jar
    return None


@dataclass
class InstalledMod:
    filename: str
    size: int
    project_id: str = ""
    title: str = ""
    version_id: str = ""
    version_number: str = ""
    dependents: list[str] = field(default_factory=list)
    modpacks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "size": self.size,
            "project_id": self.project_id,
            "title": self.title,
            "version_id": self.version_id,
            "version_number": self.version_number,
            "dependents": list(self.dependents),
            "modpacks": list(self.modpacks),
        }


def list_installed(root: Any) -> list[InstalledMod]:
    state = ModState(root)
    by_filename = {meta["filename"].lower(): (pid, meta) for pid, meta in state.read_mods()["mods"].items()}
    managed = state.modpack_managed_mods()
    out = []
    for name in installed_jar_names(root):
        pid, meta = by_filename.get(name.lower(), ("", {}))
        try:
            size = int((mods_dir(root) / name).stat().st_size)
        except Exception:
            size = 0
        out.append(
            InstalledMod(
                filename=name,
                size=size,
                project_id=pid,
                title=meta.get("title", ""),
                version_id=meta.get("version_id", ""),
                version_number=meta.get("version_number", ""),
                dependents=state.dependents_of(name),
                modpacks=managed.get(name.lower(), []),
            )
        )
    return out


def plan_dependencies(
    root: Any, version: ModrinthVersion, mc_version: str, loader: str
) -> tuple[list[ModrinthVersion], list[ModrinthVersion]]:
    """(dependencies still missing from mods/, every required dependency). Talks to Modrinth."""
    installed = {n.lower() for n in installed_jar_names(root)}
    deps = modrinth_client.resolve_required_dependencies(version.version_id, mc_version, loader=loader)
    own = version.filename.lower()
    missing = [d for d in deps if d.filename.lower() not in installed and d.filename.lower() != own]
    return missing, deps


ProgressCallback = Callable[[float, str], None]


def download_mod(
    root: Any,
    project_id: str,
    title: str,
    version: ModrinthVersion,
    deps_to_install: list[ModrinthVersion],
    all_required_deps: list[ModrinthVersion],
    progress: ProgressCallback | None = None,
) -> int:
    """Put *version* and its missing dependencies into mods/ and write them down.

    Returns how many dependencies were downloaded. An older file of the same project
    is removed first, so updating never leaves two versions side by side. Raises on a
    failed download; the caller decides how to tell the user.
    """
    state = ModState(root)
    folder = mods_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    installed = {n.lower() for n in installed_jar_names(root)}

    # Each file gets an equal slice of the bar and fills its own slice by bytes,
    # so a lone jar still moves instead of sitting at 0% and jumping to done.
    total_files = len(deps_to_install) + 1

    def report_file(index: int, name: str):
        def on_bytes(done: int, size: int) -> None:
            if not progress:
                return
            share = (done / size) if size else 0.0
            detail = f"{name} — {index + 1}/{total_files}" if total_files > 1 else name
            progress((index + share) / total_files, detail)

        return on_bytes

    old = state.read_mods()["mods"].get(project_id)
    if old:
        old_filename = old.get("filename", "")
        if old_filename and old_filename.lower() != version.filename.lower():
            old_path = find_jar(root, old_filename)
            if old_path:
                old_path.unlink(missing_ok=True)
            installed.discard(old_filename.lower())

    downloaded = 0
    for position, dep in enumerate(deps_to_install):
        dep_name = dep.filename.lower()
        if dep_name in installed or dep_name == version.filename.lower():
            continue
        modrinth_client.download_to(
            dep.download_url, folder / dep.filename, on_bytes=report_file(position, dep.filename)
        )
        installed.add(dep_name)
        downloaded += 1

    modrinth_client.download_to(
        version.download_url, folder / version.filename, on_bytes=report_file(len(deps_to_install), version.filename)
    )
    state.record_dependencies(version.filename, all_required_deps)
    state.record_mod(project_id, title, version.version_id, version.filename, version_number=version.version_number)
    return downloaded


def configure_known_mod(manager: Any, info: Any, slug: str = "", title: str = "", project_id: str = "") -> None:
    """Geyser, Floodgate and Simple Voice Chat need a config written to suit the tunnels."""
    identifiers = {
        str(slug or "").strip().lower(),
        str(title or "").strip().lower().replace(" ", "-"),
        str(project_id or "").strip().lower(),
    }
    playit = manager.playit_manager
    server_dir = str(info.server_dir)
    if "geyser" in identifiers:
        playit.configure_geyser_mod(server_dir)
    elif "floodgate" in identifiers:
        playit.configure_floodgate_mod(server_dir)
    elif "simple-voice-chat" in identifiers or "voice-chat" in identifiers:
        try:
            from niksnaks_hosting.shared.backend.playit_config import load_playit_config

            cfg = load_playit_config(info.server_dir)
            endpoint = str(cfg.get("voicechat_endpoint", "")).strip()
            port = int(cfg.get("voicechat_port", 24454))
        except Exception:
            endpoint, port = "", 24454
        playit.configure_voicechat_mod(server_dir, info.id, endpoint=endpoint, voicechat_port=port)


def remove_mod(root: Any, filename: str, remove_orphaned_dependencies: bool = True) -> tuple[bool, str]:
    """Delete a jar from mods/ for good, and with it whatever only it needed."""
    jar = find_jar(root, filename)
    if jar is None:
        return False, _("There is no mod called {}.").format(filename)
    state = ModState(root)
    orphans = state.orphaned_dependencies(jar.name) if remove_orphaned_dependencies else []
    try:
        jar.unlink(missing_ok=True)
    except Exception as exc:
        return False, str(exc)
    state.forget_mod(jar.name)
    for orphan in orphans:
        orphan_jar = find_jar(root, orphan)
        if orphan_jar:
            try:
                orphan_jar.unlink(missing_ok=True)
            except Exception:
                continue
            state.forget_mod(orphan_jar.name)
    return True, jar.name
