import os
import re
import stat
import sys
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from niksnaks_hosting.shared.utils.constants import (
    BEDROCK_BINARY_NAMES,
    BEDROCK_DOWNLOAD_LINKS_URL,
    BEDROCK_SERVER_BINARY,
    CACHE_DIR,
    get_bedrock_download_types,
)

# www.minecraft.net rejects requests without a browser user agent.
BEDROCK_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Niksnaks-Hosting"

BEDROCK_VERSION_RE = re.compile(r"bedrock-server-([0-9][0-9.]*)\.zip")

# Mojang ships these with the package; they hold the user's own settings once a
# server exists, so an update must not overwrite them.
BEDROCK_PRESERVED_FILES = ("server.properties", "allowlist.json", "permissions.json", "whitelist.json")

@dataclass(frozen=True)
class BedrockVersionOption:

    version: str
    url: str
    preview: bool = False

    @property
    def label(self) -> str:
        if self.preview:
            return _("{} (preview)").format(self.version)
        return _("{} (latest release)").format(self.version)

class BedrockManager:

    def __init__(self):
        self._links: dict[str, str] | None = None

    def fetch_download_links(self, force: bool = False) -> dict[str, str]:
        if self._links is not None and not force:
            return dict(self._links)

        links: dict[str, str] = {}
        try:
            resp = requests.get(
                BEDROCK_DOWNLOAD_LINKS_URL,
                timeout=20,
                headers={"User-Agent": BEDROCK_USER_AGENT},
            )
            resp.raise_for_status()
            payload = resp.json() or {}
            entries = (payload.get("result") or {}).get("links") or payload.get("links") or []
            for entry in entries:
                download_type = str(entry.get("downloadType") or "").strip()
                url = str(entry.get("downloadUrl") or "").strip()
                if download_type and url:
                    links[download_type] = url
        except Exception as e:
            print(f"Failed to fetch Bedrock download links: {e}")
            return {}

        self._links = links
        return dict(links)

    def fetch_versions(self) -> list[BedrockVersionOption]:

        links = self.fetch_download_links()
        if not links:
            return []

        release_type, preview_type = get_bedrock_download_types()
        options: list[BedrockVersionOption] = []
        for download_type, preview in ((release_type, False), (preview_type, True)):
            url = links.get(download_type)
            if not url:
                continue
            match = BEDROCK_VERSION_RE.search(url)
            if not match:
                continue
            options.append(BedrockVersionOption(match.group(1), url, preview=preview))

        return options

    def fetch_versions_async(self, callback: Callable[[list[BedrockVersionOption]], None]):
        def _fetch():
            callback(self.fetch_versions())

        thread = threading.Thread(target=_fetch, daemon=True)
        thread.start()
        return thread

    def download_package(
        self,
        option: BedrockVersionOption,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> str | None:

        cached_zip = CACHE_DIR / f"bedrock-server-{option.version}.zip"
        if cached_zip.exists() and cached_zip.stat().st_size > 1024 * 1024:
            if progress_callback:
                progress_callback(1.0, _("Using cached Bedrock server"))
            return str(cached_zip)

        try:
            if progress_callback:
                progress_callback(0.0, _("Downloading Bedrock server..."))

            resp = requests.get(
                option.url,
                stream=True,
                timeout=300,
                headers={"User-Agent": BEDROCK_USER_AGENT},
            )
            resp.raise_for_status()

            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(cached_zip, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0 and progress_callback:
                        progress_callback(
                            downloaded / total,
                            _("Downloading Bedrock server... {:.1f}/{:.1f} MB").format(
                                downloaded / (1024 * 1024), total / (1024 * 1024)
                            ),
                        )

            if progress_callback:
                progress_callback(1.0, _("Bedrock server downloaded"))

            return str(cached_zip)

        except Exception as e:
            print(f"Failed to download Bedrock server: {e}")
            cached_zip.unlink(missing_ok=True)
            return None

    def install(
        self,
        option: BedrockVersionOption,
        server_dir: str | Path,
        progress_callback: Callable[[float, str], None] | None = None,
        keep_existing_config: bool = False,
    ) -> tuple[bool, str]:

        package = self.download_package(
            option,
            progress_callback=lambda frac, msg: (progress_callback(frac * 0.7, msg) if progress_callback else None),
        )
        if not package:
            return False, _("Failed to download Bedrock server {}").format(option.version)

        root = Path(server_dir)
        root.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(package) as archive:
                members = archive.namelist()
                total = max(1, len(members))
                for index, member in enumerate(members, start=1):
                    if keep_existing_config and member in BEDROCK_PRESERVED_FILES and (root / member).exists():
                        continue
                    archive.extract(member, root)
                    if progress_callback and index % 250 == 0:
                        progress_callback(
                            0.7 + (index / total) * 0.3,
                            _("Extracting Bedrock server... {}/{}").format(index, total),
                        )
        except Exception as e:
            return False, _("Failed to extract Bedrock server: {}").format(e)

        binary = self.server_binary(root)
        if not binary:
            return False, _("Bedrock server executable not found after extraction")

        if sys.platform != "win32":
            try:
                binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except Exception:
                pass

        if progress_callback:
            progress_callback(1.0, _("Bedrock server {} installed").format(option.version))

        return True, _("Bedrock server {} installed").format(option.version)

    def server_binary(self, server_dir: str | Path) -> Path | None:
        binary = Path(server_dir) / BEDROCK_SERVER_BINARY
        return binary if binary.is_file() else None

    def is_installed(self, server_dir: str | Path) -> bool:
        return self.server_binary(server_dir) is not None

    def foreign_binary(self, server_dir: str | Path) -> Path | None:
        """The other operating system's server executable, left behind by a backup from there."""
        root = Path(server_dir)
        for name in BEDROCK_BINARY_NAMES:
            if name == BEDROCK_SERVER_BINARY:
                continue
            candidate = root / name
            if candidate.is_file():
                return candidate
        return None

    def launch_env(self, server_dir: str | Path) -> dict[str, str]:

        env = dict(os.environ)
        if sys.platform != "win32":
            existing = env.get("LD_LIBRARY_PATH", "")
            root = str(Path(server_dir))
            env["LD_LIBRARY_PATH"] = f"{root}{os.pathsep}{existing}" if existing else root
        return env
