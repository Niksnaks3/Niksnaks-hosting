"""
Resolve how an installed server is launched, per mod loader.

Fabric always produces a single launch jar. Forge produces either a generated
argument file (1.17+) or a launch jar (1.16.5 and older), so the layout has to be
probed on disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

from niksnaks_hosting.shared.utils.constants import LOADER_FORGE, normalize_loader

FABRIC_LAUNCH_JAR = "fabric-server-launch.jar"

FORGE_LIBRARY_PATH = ("libraries", "net", "minecraftforge", "forge")


def forge_args_file(server_dir: Path) -> Path | None:
    """Return the Forge generated args file (1.17+ layout), newest install wins."""
    forge_libs = Path(server_dir).joinpath(*FORGE_LIBRARY_PATH)
    if not forge_libs.is_dir():
        return None

    preferred = "win_args.txt" if sys.platform == "win32" else "unix_args.txt"
    candidates: list[Path] = []
    for version_dir in forge_libs.iterdir():
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


def forge_launch_jar(server_dir: Path) -> Path | None:
    """Return the Forge launch jar (1.16.5 and older layout)."""
    root = Path(server_dir)
    if not root.is_dir():
        return None

    jars = [
        p
        for p in root.glob("forge-*.jar")
        if p.is_file() and "installer" not in p.name.lower() and "shim" not in p.name.lower()
    ]
    if not jars:
        return None
    return max(jars, key=lambda p: p.stat().st_mtime)


def is_loader_installed(server_dir: Path | str, loader_type: str) -> bool:
    """True when the given server directory contains a launchable install for the loader."""
    root = Path(server_dir)
    if normalize_loader(loader_type) == LOADER_FORGE:
        return forge_args_file(root) is not None or forge_launch_jar(root) is not None
    return (root / FABRIC_LAUNCH_JAR).is_file()


def resolve_launch_args(server_dir: Path | str, loader_type: str) -> tuple[list[str], str]:
    """
    Build the java arguments that follow the JVM options for a server install.

    Returns (args, error_message). ``args`` is empty when an error is returned.
    All paths are relative to the server directory, which is the process cwd.
    """
    root = Path(server_dir)
    loader = normalize_loader(loader_type)

    if loader == LOADER_FORGE:
        args_file = forge_args_file(root)
        if args_file:
            relative = args_file.relative_to(root).as_posix()
            return [f"@{relative}", "nogui"], ""

        launch_jar = forge_launch_jar(root)
        if launch_jar:
            return ["-jar", launch_jar.name, "nogui"], ""

        return [], "Forge server files not found (run the Forge installer again)"

    launch_jar = root / FABRIC_LAUNCH_JAR
    if not launch_jar.is_file():
        return [], f"{FABRIC_LAUNCH_JAR} not found"
    return ["-jar", FABRIC_LAUNCH_JAR, "nogui"], ""
