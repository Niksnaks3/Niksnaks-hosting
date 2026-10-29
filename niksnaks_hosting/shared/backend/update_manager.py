from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from niksnaks_hosting.shared.utils.constants import APP_VERSION
from niksnaks_hosting.shared.utils.subprocess_utils import hidden_subprocess_kwargs

LATEST_RELEASE_URL = "https://api.github.com/repos/Niksnaks3/Niksnaks-hosting/releases/latest"
USER_AGENT = f"Niksnaks-Hosting/{APP_VERSION}"
INSTALLER_ARGS = ("/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCANCEL")


@dataclass
class ReleaseInfo:
    version: str
    installer_name: str
    installer_url: str
    installer_size: int


def is_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID")) or os.path.exists("/.flatpak-info")


def is_windows_install() -> bool:
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def version_from_tag(tag: str) -> str:
    match = re.search(r"\d+(?:\.\d+)*", tag)
    return match.group(0) if match else ""


def parse_version(text: str) -> tuple[int, ...]:
    version = version_from_tag(text)
    return tuple(int(part) for part in version.split(".")) if version else ()


def is_newer(candidate: str, current: str = APP_VERSION) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def fetch_latest_release() -> ReleaseInfo | None:
    resp = requests.get(
        LATEST_RELEASE_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    version = version_from_tag(data.get("tag_name") or "")
    if not version:
        return None
    for asset in data.get("assets") or []:
        name = asset.get("name") or ""
        if name.lower().endswith("-setup.exe"):
            return ReleaseInfo(
                version=version,
                installer_name=name,
                installer_url=asset.get("browser_download_url") or "",
                installer_size=int(asset.get("size") or 0),
            )
    return None


def check_for_windows_update() -> ReleaseInfo | None:
    release = fetch_latest_release()
    if release and release.installer_url and is_newer(release.version):
        return release
    return None


def download_installer(
    release: ReleaseInfo,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    target_dir = Path(tempfile.gettempdir()) / "Niksnaks-Hosting-update"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / release.installer_name
    partial = target.with_name(target.name + ".part")

    with requests.get(
        release.installer_url,
        headers={"User-Agent": USER_AGENT},
        stream=True,
        timeout=30,
    ) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or release.installer_size or 0)
        done = 0
        with open(partial, "wb") as handle:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)

    if total and done != total:
        partial.unlink(missing_ok=True)
        raise OSError(f"Download stopped at {done} of {total} bytes")

    os.replace(partial, target)
    return target


def launch_installer_after_exit(installer: Path) -> None:
    path = str(installer).replace("'", "''")
    arguments = ",".join(f"'{arg}'" for arg in INSTALLER_ARGS)
    script = (
        f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; "
        f"Start-Process -FilePath '{path}' -ArgumentList {arguments}"
    )
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **hidden_subprocess_kwargs(),
    )
