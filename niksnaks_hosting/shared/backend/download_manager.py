import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from niksnaks_hosting.shared.utils.constants import (
    CACHE_DIR,
    FABRIC_GAME_VERSIONS_URL,
    FABRIC_INSTALLER_VERSIONS_URL,
    FABRIC_LOADER_VERSIONS_URL,
    FORGE_PROMOTIONS_URL,
    FORGE_VERSIONS_URL,
    LOADER_FORGE,
    LOADER_NEOFORGE,
    NEOFORGE_LEGACY_VERSIONS_URL,
    NEOFORGE_VERSIONS_URL,
    TEMP_DIR,
    get_fabric_loader_versions_url,
    get_forge_installer_url,
    get_loader_display_name,
    get_neoforge_installer_url,
    is_legacy_neoforge_version,
    is_neoforge_beta,
    neoforge_mc_version,
    normalize_loader,
    parse_mc_version,
)
from niksnaks_hosting.shared.backend.loader_launch import is_loader_installed
from niksnaks_hosting.shared.utils.subprocess_utils import hidden_subprocess_kwargs

MOJANG_VERSION_MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"

FORGE_MIN_MC_VERSION = (1, 12, 0)

# The game version list is asked for every time the create dialog opens, the loader or
# edition is switched, and a server is selected in Properties — but it only changes when
# Mojang ships a version. Hold it briefly so those are not all separate round trips,
# while a session left open for hours still picks a new release up.
GAME_VERSIONS_TTL_SECONDS = 600

@dataclass(frozen=True)
class LoaderVersionOption:

    version: str
    recommended: bool = False
    latest: bool = False

    @property
    def label(self) -> str:
        if self.recommended:
            return _("{} (recommended)").format(self.version)
        if self.latest:
            return _("{} (latest)").format(self.version)
        return self.version

def default_loader_option_index(options: list[LoaderVersionOption]) -> int:
    for idx, option in enumerate(options):
        if option.recommended:
            return idx
    for idx, option in enumerate(options):
        if option.latest:
            return idx
    return 0

def _forge_build_sort_key(build: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", str(build or "")))

def _maven_versions(url: str, label: str) -> list[str]:
    """Every version listed in a maven-metadata.xml, in the order the repository gives them."""
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        import xml.etree.ElementTree as ET

        root = ET.fromstring(resp.content)
        return [v.text.strip() for v in root.findall("./versioning/versions/version") if v.text]
    except Exception as e:
        print(f"Failed to fetch {label} metadata: {e}")
        return []

class DownloadManager:
    def __init__(self):
        self._game_versions: list[dict] = []
        self._game_versions_fetched_at: float = 0.0
        self._loader_versions: list[dict] = []
        self._installer_url: str | None = None
        self._installer_version: str | None = None
        self._mojang_manifest: dict | None = None
        self._forge_metadata: dict[str, list[str]] | None = None
        self._forge_promotions: dict[str, str] | None = None
        self._neoforge_metadata: dict[str, list[str]] | None = None
        self._fabric_loaders_by_game: dict[str, list[tuple[str, bool]]] = {}

    def fetch_game_versions(self, include_snapshots: bool = False) -> list[str]:
        entries = self._fetch_game_version_entries()
        return [v["version"] for v in entries if include_snapshots or v.get("stable", False)]

    def _fetch_game_version_entries(self) -> list[dict]:
        now = time.monotonic()
        if self._game_versions and now - self._game_versions_fetched_at < GAME_VERSIONS_TTL_SECONDS:
            return self._game_versions

        try:
            resp = requests.get(FABRIC_GAME_VERSIONS_URL, timeout=15)
            resp.raise_for_status()
            self._game_versions = resp.json()
            self._game_versions_fetched_at = now
        except Exception as e:
            print(f"Failed to fetch game versions: {e}")
            # Serve the list we already hold rather than nothing: a dropped connection
            # should not empty a version picker that was populated a minute ago.
            return self._game_versions

        return self._game_versions

    def fetch_loader_versions(self) -> list[str]:
        try:
            resp = requests.get(FABRIC_LOADER_VERSIONS_URL, timeout=15)
            resp.raise_for_status()
            self._loader_versions = resp.json()
            return [v["version"] for v in self._loader_versions]
        except Exception as e:
            print(f"Failed to fetch loader versions: {e}")
            return []

    def fetch_fabric_loader_versions(self, mc_version: str) -> list[tuple[str, bool]]:

        mc_version = str(mc_version or "").strip()
        if not mc_version:
            return []

        cached = self._fabric_loaders_by_game.get(mc_version)
        if cached is not None:
            return list(cached)

        try:
            resp = requests.get(get_fabric_loader_versions_url(mc_version), timeout=15)
            resp.raise_for_status()
            entries = []
            for entry in resp.json() or []:
                loader = entry.get("loader") or {}
                version = str(loader.get("version") or "").strip()
                if version:
                    entries.append((version, bool(loader.get("stable"))))
        except Exception as e:
            print(f"Failed to fetch Fabric loader versions for MC {mc_version}: {e}")
            return [(version, False) for version in self.fetch_loader_versions()]

        self._fabric_loaders_by_game[mc_version] = entries
        return list(entries)

    def fetch_installer_info(self) -> tuple[str | None, str | None]:
        try:
            resp = requests.get(FABRIC_INSTALLER_VERSIONS_URL, timeout=15)
            resp.raise_for_status()
            installers = resp.json()

            if installers:
                latest = installers[0]
                self._installer_url = latest.get("url")
                self._installer_version = latest.get("version")
                return self._installer_url, self._installer_version
        except Exception as e:
            print(f"Failed to fetch installer info: {e}")

        return None, None

    def download_installer(self, progress_callback: Callable[[float, str], None] | None = None) -> str | None:
        url, version = self.fetch_installer_info()
        if not url:
            return None

        cached_jar = CACHE_DIR / f"fabric-installer-{version}.jar"
        if cached_jar.exists():
            if progress_callback:
                progress_callback(1.0, _("Using cached installer"))
            return str(cached_jar)

        try:
            if progress_callback:
                progress_callback(0.0, _("Downloading Fabric installer..."))

            resp = requests.get(url, stream=True, timeout=60)
            resp.raise_for_status()

            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(cached_jar, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0 and progress_callback:
                        frac = downloaded / total
                        progress_callback(frac, _("Downloading installer... {:.0f} KB").format(downloaded / 1024))

            if progress_callback:
                progress_callback(1.0, _("Installer downloaded"))

            return str(cached_jar)

        except Exception as e:
            print(f"Failed to download installer: {e}")
            cached_jar.unlink(missing_ok=True)
            return None

    def _fetch_mojang_manifest(self) -> dict | None:
        if self._mojang_manifest:
            return self._mojang_manifest
        try:
            resp = requests.get(MOJANG_VERSION_MANIFEST, timeout=15)
            resp.raise_for_status()
            self._mojang_manifest = resp.json()
            return self._mojang_manifest
        except Exception as e:
            print(f"Failed to fetch Mojang manifest: {e}")
            return None

    def _get_version_json_url(self, mc_version: str) -> str | None:
        manifest = self._fetch_mojang_manifest()
        if not manifest:
            return None
        for entry in manifest.get("versions", []):
            if entry.get("id") == mc_version:
                return entry.get("url")
        return None

    def download_server_jar(
        self, mc_version: str, server_dir: str, progress_callback: Callable[[float, str], None] | None = None
    ) -> tuple[bool, str]:

        dest = Path(server_dir) / "server.jar"

        if dest.exists() and dest.stat().st_size > 1000:
            if progress_callback:
                progress_callback(1.0, _("server.jar already present"))
            return True, _("server.jar already present")

        try:

            if progress_callback:
                progress_callback(0.05, _("Fetching MC {} metadata...").format(mc_version))

            version_url = self._get_version_json_url(mc_version)
            if not version_url:
                return False, _("Minecraft version {} not found in Mojang manifest").format(mc_version)

            if progress_callback:
                progress_callback(0.1, _("Reading version details..."))

            resp = requests.get(version_url, timeout=15)
            resp.raise_for_status()
            version_data = resp.json()

            downloads = version_data.get("downloads", {})
            server_info = downloads.get("server")
            if not server_info:
                return False, _("No server download available for MC {}").format(mc_version)

            jar_url = server_info.get("url")
            jar_size = server_info.get("size", 0)

            if not jar_url:
                return False, _("server.jar URL not found in version metadata")

            if progress_callback:
                progress_callback(0.15, _("Downloading server.jar..."))

            resp = requests.get(jar_url, stream=True, timeout=120)
            resp.raise_for_status()

            total = int(resp.headers.get("content-length", jar_size))
            downloaded = 0

            Path(server_dir).mkdir(parents=True, exist_ok=True)

            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0 and progress_callback:
                        frac = 0.15 + (downloaded / total) * 0.85
                        size_mb = downloaded / (1024 * 1024)
                        total_mb = total / (1024 * 1024)
                        progress_callback(
                            frac, _("Downloading server.jar... {:.1f}/{:.1f} MB").format(size_mb, total_mb)
                        )

            if progress_callback:
                progress_callback(1.0, _("server.jar downloaded"))

            return True, _("server.jar downloaded successfully")

        except Exception as e:

            dest.unlink(missing_ok=True)
            return False, _("Failed to download server.jar: {}").format(e)

    def install_fabric_server(
        self,
        java_path: str,
        installer_jar: str,
        mc_version: str,
        server_dir: str,
        loader_version: str | None = None,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> tuple[bool, str]:

        import subprocess

        Path(server_dir).mkdir(parents=True, exist_ok=True)

        cmd = [
            java_path,
            f"-Djava.io.tmpdir={TEMP_DIR}",
            "-jar",
            installer_jar,
            "server",
            "-mcversion",
            mc_version,
            "-dir",
            server_dir,
        ]

        if loader_version:
            cmd.extend(["-loader", loader_version])

        if progress_callback:
            progress_callback(0.5, _("Installing Fabric server for MC {}...").format(mc_version))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=server_dir,
                **hidden_subprocess_kwargs(),
            )

            if result.returncode == 0:

                launch_jar = Path(server_dir) / "fabric-server-launch.jar"
                if launch_jar.exists():
                    if progress_callback:
                        progress_callback(1.0, _("Fabric server installed successfully"))
                    return True, _("Installation successful")
                else:
                    return False, _("Installation completed but fabric-server-launch.jar not found")
            else:
                error_msg = result.stderr or result.stdout or _("Unknown error")
                return False, _("Installation failed: {}").format(error_msg)

        except subprocess.TimeoutExpired:
            return False, _("Installation timed out (5 minutes)")
        except Exception as e:
            return False, _("Installation error: {}").format(e)

    def _fetch_forge_metadata(self) -> dict[str, list[str]]:
        if self._forge_metadata is not None:
            return self._forge_metadata

        raw_versions = _maven_versions(FORGE_VERSIONS_URL, "Forge")
        if not raw_versions:
            self._forge_metadata = {}
            return self._forge_metadata

        grouped: dict[str, list[str]] = {}
        for full in raw_versions:
            if "-" not in full:
                continue
            mc, _sep, build = full.partition("-")
            if not mc or not build:
                continue
            parsed = parse_mc_version(mc)
            if not parsed or parsed < FORGE_MIN_MC_VERSION:
                continue
            grouped.setdefault(mc, []).append(build)

        for builds in grouped.values():
            builds.sort(key=_forge_build_sort_key, reverse=True)

        self._forge_metadata = grouped
        return grouped

    def fetch_forge_game_versions(self) -> list[str]:
        grouped = self._fetch_forge_metadata()
        versions = list(grouped.keys())
        versions.sort(key=lambda v: parse_mc_version(v) or (0, 0, 0), reverse=True)
        return versions

    def fetch_forge_builds(self, mc_version: str) -> list[str]:
        grouped = self._fetch_forge_metadata()
        return list(grouped.get(mc_version, []))

    def _fetch_neoforge_metadata(self) -> dict[str, list[str]]:
        """Every NeoForge build, grouped by the Minecraft version it targets, newest first."""
        if self._neoforge_metadata is not None:
            return self._neoforge_metadata

        raw_versions = _maven_versions(NEOFORGE_VERSIONS_URL, "NeoForge")
        # The 1.20.1 artifact prefixes its versions with the game version. One build was
        # published without that prefix, and it would read as a Minecraft that never
        # existed, so only properly named entries are taken.
        raw_versions += [
            version
            for version in _maven_versions(NEOFORGE_LEGACY_VERSIONS_URL, "NeoForge 1.20.1")
            if is_legacy_neoforge_version(version)
        ]
        if not raw_versions:
            self._neoforge_metadata = {}
            return self._neoforge_metadata

        grouped: dict[str, list[str]] = {}
        for version in raw_versions:
            # Builds for Minecraft snapshots name no released game version and are skipped.
            mc_version = neoforge_mc_version(version)
            if mc_version:
                grouped.setdefault(mc_version, []).append(version)

        for versions in grouped.values():
            versions.sort(key=_forge_build_sort_key, reverse=True)

        self._neoforge_metadata = grouped
        return grouped

    def fetch_neoforge_game_versions(self) -> list[str]:
        grouped = self._fetch_neoforge_metadata()
        versions = list(grouped.keys())
        versions.sort(key=lambda v: parse_mc_version(v) or (0, 0, 0), reverse=True)
        return versions

    def fetch_neoforge_versions(self, mc_version: str) -> list[str]:
        return list(self._fetch_neoforge_metadata().get(mc_version, []))

    def get_neoforge_recommended_version(self, mc_version: str) -> str | None:
        """NeoForge runs no promotions file, so the newest stable build is the safe pick."""
        versions = self.fetch_neoforge_versions(mc_version)
        if not versions:
            return None
        stable = next((v for v in versions if not is_neoforge_beta(v)), None)
        return stable or versions[0]

    def download_neoforge_installer(
        self,
        version: str,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> str | None:
        return self._download_installer_jar(
            get_neoforge_installer_url(version),
            CACHE_DIR / f"neoforge-installer-{version}.jar",
            get_loader_display_name(LOADER_NEOFORGE),
            progress_callback,
        )

    def install_neoforge_server(
        self,
        java_path: str,
        installer_jar: str,
        mc_version: str,
        server_dir: str,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> tuple[bool, str]:
        return self._install_loader_server(
            LOADER_NEOFORGE, java_path, installer_jar, mc_version, server_dir, progress_callback
        )

    def _fetch_forge_promotions(self) -> dict[str, str]:
        if self._forge_promotions is not None:
            return self._forge_promotions
        try:
            resp = requests.get(FORGE_PROMOTIONS_URL, timeout=15)
            resp.raise_for_status()
            promos = (resp.json() or {}).get("promos", {}) or {}
            self._forge_promotions = {str(k): str(v) for k, v in promos.items()}
        except Exception as e:
            print(f"Failed to fetch Forge promotions: {e}")
            self._forge_promotions = {}
        return self._forge_promotions

    def get_forge_recommended_build(self, mc_version: str) -> str | None:
        promos = self._fetch_forge_promotions()
        build = promos.get(f"{mc_version}-recommended") or promos.get(f"{mc_version}-latest")
        if build:
            return build
        builds = self.fetch_forge_builds(mc_version)
        return builds[0] if builds else None

    def fetch_compatible_loader_versions(self, loader_type: str, mc_version: str) -> list[LoaderVersionOption]:

        mc_version = str(mc_version or "").strip()
        if not mc_version:
            return []

        loader = normalize_loader(loader_type)

        if loader == LOADER_FORGE:
            builds = self.fetch_forge_builds(mc_version)
            if not builds:
                return []
            promos = self._fetch_forge_promotions()
            recommended = promos.get(f"{mc_version}-recommended", "")
            latest = promos.get(f"{mc_version}-latest", "")
            return [
                LoaderVersionOption(build, recommended=build == recommended, latest=build == latest)
                for build in builds
            ]

        if loader == LOADER_NEOFORGE:
            versions = self.fetch_neoforge_versions(mc_version)
            if not versions:
                return []
            recommended = self.get_neoforge_recommended_version(mc_version) or ""
            return [
                LoaderVersionOption(
                    version,
                    recommended=version == recommended,
                    latest=index == 0 and version != recommended,
                )
                for index, version in enumerate(versions)
            ]

        entries = self.fetch_fabric_loader_versions(mc_version)
        return [
            LoaderVersionOption(version, recommended=stable, latest=idx == 0)
            for idx, (version, stable) in enumerate(entries)
        ]

    def fetch_compatible_loader_versions_async(
        self,
        loader_type: str,
        mc_version: str,
        callback: Callable[[list[LoaderVersionOption]], None],
    ):

        def _fetch():
            callback(self.fetch_compatible_loader_versions(loader_type, mc_version))

        thread = threading.Thread(target=_fetch, daemon=True)
        thread.start()
        return thread

    def download_forge_installer(
        self,
        full_version: str,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> str | None:
        return self._download_installer_jar(
            get_forge_installer_url(full_version),
            CACHE_DIR / f"forge-installer-{full_version}.jar",
            get_loader_display_name(LOADER_FORGE),
            progress_callback,
        )

    def _download_installer_jar(
        self,
        url: str,
        cached_jar: Path,
        loader_name: str,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> str | None:

        if cached_jar.exists():
            if progress_callback:
                progress_callback(1.0, _("Using cached installer"))
            return str(cached_jar)

        try:
            if progress_callback:
                progress_callback(0.0, _("Downloading {} installer...").format(loader_name))

            resp = requests.get(url, stream=True, timeout=60)
            resp.raise_for_status()

            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(cached_jar, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0 and progress_callback:
                        frac = downloaded / total
                        progress_callback(frac, _("Downloading installer... {:.0f} KB").format(downloaded / 1024))

            if progress_callback:
                progress_callback(1.0, _("Installer downloaded"))

            return str(cached_jar)

        except Exception as e:
            print(f"Failed to download {loader_name} installer: {e}")
            cached_jar.unlink(missing_ok=True)
            return None

    def install_forge_server(
        self,
        java_path: str,
        installer_jar: str,
        mc_version: str,
        forge_build: str,
        server_dir: str,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> tuple[bool, str]:
        return self._install_loader_server(
            LOADER_FORGE, java_path, installer_jar, mc_version, server_dir, progress_callback
        )

    def _install_loader_server(
        self,
        loader_type: str,
        java_path: str,
        installer_jar: str,
        mc_version: str,
        server_dir: str,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> tuple[bool, str]:
        """Run a Forge or NeoForge installer, which build a server folder the same way."""

        import subprocess

        loader_name = get_loader_display_name(loader_type)
        Path(server_dir).mkdir(parents=True, exist_ok=True)

        if progress_callback:
            progress_callback(0.3, _("Installing {} server for MC {}...").format(loader_name, mc_version))

        cmd = [java_path, f"-Djava.io.tmpdir={TEMP_DIR}", "-jar", installer_jar, "--installServer"]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
                cwd=server_dir,
                **hidden_subprocess_kwargs(),
            )

            for pattern in ("forge*installer*.jar.log", "neoforge*installer*.jar.log"):
                for log in Path(server_dir).glob(pattern):
                    try:
                        log.unlink(missing_ok=True)
                    except Exception:
                        pass

            if result.returncode == 0 and is_loader_installed(server_dir, loader_type):
                if progress_callback:
                    progress_callback(1.0, _("{} server installed successfully").format(loader_name))
                return True, _("Installation successful")
            else:
                error_msg = result.stderr or result.stdout or _("Unknown error")
                if result.returncode == 0:
                    return False, _("Installation completed but {} server files were not found").format(loader_name)
                return False, _("Installation failed: {}").format(error_msg)

        except subprocess.TimeoutExpired:
            return False, _("Installation timed out (15 minutes)")
        except Exception as e:
            return False, _("Installation error: {}").format(e)

    def fetch_game_versions_for_loader(self, loader_type: str) -> list[str]:
        loader = normalize_loader(loader_type)
        if loader == LOADER_FORGE:
            return self.fetch_forge_game_versions()
        if loader == LOADER_NEOFORGE:
            return self.fetch_neoforge_game_versions()
        return self.fetch_game_versions()

    def fetch_game_versions_for_loader_async(
        self,
        loader_type: str,
        callback: Callable[[list[str]], None],
    ):

        def _fetch():
            callback(self.fetch_game_versions_for_loader(loader_type))

        thread = threading.Thread(target=_fetch, daemon=True)
        thread.start()
        return thread
