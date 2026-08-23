from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from niksnaks_hosting.shared.remote.paths import is_remote
from niksnaks_hosting.shared.utils.constants import (
    LOADER_DISPLAY_NAMES,
    LOADER_FORGE,
    LOADER_NEOFORGE,
    normalize_loader,
    uses_installer,
)

FABRIC_LAUNCH_JAR = "fabric-server-launch.jar"

def _as_path(value: Any) -> Any:
    """Path() for a local folder, and the remote path untouched for one on a host."""
    return value if is_remote(value) else Path(value)

# Where each installer-based loader parks the args file that starts the server. NeoForge
# has two: 1.20.1 kept the net/neoforged/forge coordinates it inherited from Forge.
LOADER_LIBRARY_PATHS = {
    LOADER_FORGE: [("libraries", "net", "minecraftforge", "forge")],
    LOADER_NEOFORGE: [
        ("libraries", "net", "neoforged", "neoforge"),
        ("libraries", "net", "neoforged", "forge"),
    ],
}

# The jar a pre-1.17 installer leaves behind, for loaders old enough not to use an args file.
LOADER_JAR_GLOBS = {
    LOADER_FORGE: "forge-*.jar",
    LOADER_NEOFORGE: "neoforge-*.jar",
}

def loader_args_file(server_dir: Path, loader_type: str) -> Path | None:
    # Which args file to prefer follows the machine the server runs on, not the one
    # running this app: a Windows desktop managing a Linux host must still pick
    # unix_args.txt, or the server is handed classpaths with the wrong separators.
    remote = is_remote(server_dir)
    preferred = "win_args.txt" if (sys.platform == "win32" and not remote) else "unix_args.txt"
    candidates: list[Path] = []

    for library_path in LOADER_LIBRARY_PATHS.get(normalize_loader(loader_type), []):
        loader_libs = _as_path(server_dir).joinpath(*library_path)
        if not loader_libs.is_dir():
            continue
        for version_dir in loader_libs.iterdir():
            if not version_dir.is_dir():
                continue
            for name in (preferred, "unix_args.txt", "win_args.txt"):
                args_file = version_dir / name
                if args_file.is_file():
                    candidates.append(args_file)
                    break

    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)

def loader_launch_jar(server_dir: Path, loader_type: str) -> Path | None:
    root = _as_path(server_dir)
    pattern = LOADER_JAR_GLOBS.get(normalize_loader(loader_type))
    if not pattern or not root.is_dir():
        return None

    jars = [
        p
        for p in root.glob(pattern)
        if p.is_file() and "installer" not in p.name.lower() and "shim" not in p.name.lower()
    ]
    if not jars:
        return None
    return max(jars, key=lambda p: p.stat().st_mtime)

def is_loader_installed(server_dir: Path | str, loader_type: str) -> bool:
    root = _as_path(server_dir)
    loader = normalize_loader(loader_type)
    if uses_installer(loader):
        return loader_args_file(root, loader) is not None or loader_launch_jar(root, loader) is not None
    return (root / FABRIC_LAUNCH_JAR).is_file()

def resolve_launch_args(server_dir: Path | str, loader_type: str) -> tuple[list[str], str]:
    root = _as_path(server_dir)
    loader = normalize_loader(loader_type)

    if uses_installer(loader):
        args_file = loader_args_file(root, loader)
        if args_file:
            relative = args_file.relative_to(root).as_posix()
            return [f"@{relative}", "nogui"], ""

        launch_jar = loader_launch_jar(root, loader)
        if launch_jar:
            return ["-jar", launch_jar.name, "nogui"], ""

        name = LOADER_DISPLAY_NAMES[loader]
        return [], f"{name} server files not found (run the {name} installer again)"

    launch_jar = root / FABRIC_LAUNCH_JAR
    if not launch_jar.is_file():
        return [], f"{FABRIC_LAUNCH_JAR} not found"
    return ["-jar", FABRIC_LAUNCH_JAR, "nogui"], ""
