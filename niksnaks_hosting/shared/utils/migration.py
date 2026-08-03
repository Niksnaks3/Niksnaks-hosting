from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

LEGACY_STATE_FILES = {
    ".hosty-mod-installs.json": ".niksnaks-hosting-mod-installs.json",
    ".hosty-modpacks.json": ".niksnaks-hosting-modpacks.json",
    ".hosty-datapack-installs.json": ".niksnaks-hosting-datapack-installs.json",
    ".hosty-mod-dependencies.json": ".niksnaks-hosting-mod-dependencies.json",
    ".hosty-incompatible-components.json": ".niksnaks-hosting-incompatible-components.json",
    ".hosty-playit.json": ".niksnaks-hosting-playit.json",
}

LEGACY_BACKUPS_DIR = "hosty-backups"
BACKUPS_DIR = "niksnaks-hosting-backups"

LEGACY_BACKUP_PREFIXES = (
    ("hosty-auto-backup-", "niksnaks-hosting-auto-backup-"),
    ("hosty-full-backup-", "niksnaks-hosting-full-backup-"),
    ("hosty-backup-", "niksnaks-hosting-backup-"),
)

def legacy_data_dir() -> Path:
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Hosty"
        return Path.home() / "AppData" / "Local" / "Hosty"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Hosty"

    return Path.home() / ".local" / "share" / "hosty"

def migrate_server_dir(root: Path) -> None:
    root = Path(root)
    if not root.is_dir():
        return

    for legacy_name, new_name in LEGACY_STATE_FILES.items():
        legacy = root / legacy_name
        if legacy.is_file() and not (root / new_name).exists():
            try:
                legacy.rename(root / new_name)
            except OSError:
                pass

    legacy_backups = root / LEGACY_BACKUPS_DIR
    backups = root / BACKUPS_DIR
    if legacy_backups.is_dir():
        if backups.exists():
            _move_contents(legacy_backups, backups)
        else:
            try:
                legacy_backups.rename(backups)
            except OSError:
                pass

    if backups.is_dir():
        for archive in backups.glob("hosty-*.zip"):
            for legacy_prefix, new_prefix in LEGACY_BACKUP_PREFIXES:
                if archive.name.startswith(legacy_prefix):
                    target = backups / (new_prefix + archive.name[len(legacy_prefix) :])
                    if not target.exists():
                        try:
                            archive.rename(target)
                        except OSError:
                            pass
                    break

def _move_contents(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for entry in list(source.iterdir()):
        destination = target / entry.name
        if destination.exists():
            continue
        try:
            shutil.move(str(entry), str(destination))
        except (OSError, shutil.Error):
            pass

def _rewrite_path(value: str, legacy_root: Path, new_root: Path) -> str:
    if not value:
        return value
    try:
        candidate = Path(value)
    except (TypeError, ValueError):
        return value

    try:
        relative = candidate.resolve().relative_to(legacy_root.resolve())
    except (OSError, ValueError):
        return value

    return str(new_root / relative)

def _migrate_config(legacy_config: Path, new_config: Path, legacy_root: Path, new_root: Path) -> None:
    try:
        data = json.loads(legacy_config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return

    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, list):
        return

    for server in servers:
        if not isinstance(server, dict):
            continue
        for key in ("path", "icon_path"):
            if isinstance(server.get(key), str):
                server[key] = _rewrite_path(server[key], legacy_root, new_root)

    try:
        new_config.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        return

    try:
        legacy_config.rename(legacy_config.with_suffix(".json.migrated"))
    except OSError:
        pass

def migrate_legacy_data(data_dir: Path) -> bool:
    new_root = Path(data_dir)
    legacy_root = legacy_data_dir()

    if new_root.resolve() == legacy_root.resolve():
        return False

    new_config = new_root / "servers.json"
    legacy_config = legacy_root / "servers.json"
    if new_config.exists() or not legacy_config.is_file():
        return False

    new_root.mkdir(parents=True, exist_ok=True)
    for name in ("servers", "jres", "cache"):
        legacy_sub = legacy_root / name
        if legacy_sub.is_dir():
            _move_contents(legacy_sub, new_root / name)

    _migrate_config(legacy_config, new_config, legacy_root, new_root)

    roots: list[Path] = []
    servers_dir = new_root / "servers"
    if servers_dir.is_dir():
        roots.extend(entry for entry in servers_dir.iterdir() if entry.is_dir())
    roots.extend(_configured_server_dirs(new_config))

    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        migrate_server_dir(root)

    return True

def _configured_server_dirs(config_path: Path) -> list[Path]:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, list):
        return []

    out: list[Path] = []
    for server in servers:
        if isinstance(server, dict) and isinstance(server.get("path"), str) and server["path"]:
            out.append(Path(server["path"]))
    return out
