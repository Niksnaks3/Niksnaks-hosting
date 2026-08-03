"""
DownloadManager - Handle Fabric/Forge installer and Minecraft server.jar downloads.
Uses the Fabric Meta API and the Forge maven metadata for versions and installers,
and the Mojang version manifest API for the vanilla server.jar.
"""

import re
import threading
from collections.abc import Callable
from pathlib import Path

import requests

from niksnaks_hosting.shared.utils.constants import (
    CACHE_DIR,
    FABRIC_GAME_VERSIONS_URL,
    FABRIC_INSTALLER_VERSIONS_URL,
    FABRIC_LOADER_VERSIONS_URL,
    FORGE_PROMOTIONS_URL,
    FORGE_VERSIONS_URL,
    LOADER_FABRIC,
    LOADER_FORGE,
    get_forge_full_version,
    get_forge_installer_url,
    normalize_loader,
    parse_mc_version,
)
from niksnaks_hosting.shared.backend.loader_launch import is_loader_installed
from niksnaks_hosting.shared.utils.subprocess_utils import hidden_subprocess_kwargs

MOJANG_VERSION_MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"

# Forge builds older than this are not shipped as installable dedicated servers by Niksnaks-Hosting.
FORGE_MIN_MC_VERSION = (1, 12, 0)


class DownloadManager:
    """Manages downloads of Fabric/Forge components and vanilla server JARs."""

    def __init__(self):
        self._game_versions: list[dict] = []
        self._loader_versions: list[dict] = []
        self._installer_url: str | None = None
        self._installer_version: str | None = None
        self._mojang_manifest: dict | None = None
        self._forge_metadata: dict[str, list[str]] | None = None
        self._forge_promotions: dict[str, str] | None = None

    def fetch_game_versions(self, include_snapshots: bool = False) -> list[str]:
        """
        Fetch available Minecraft game versions from Fabric Meta.
        Returns list of version strings, newest first.
        """
        try:
            resp = requests.get(FABRIC_GAME_VERSIONS_URL, timeout=15)
            resp.raise_for_status()
            self._game_versions = resp.json()

            versions = []
            for v in self._game_versions:
                if include_snapshots or v.get("stable", False):
                    versions.append(v["version"])

            return versions
        except Exception as e:
            print(f"Failed to fetch game versions: {e}")
            return []

    def fetch_loader_versions(self) -> list[str]:
        """Fetch available Fabric loader versions."""
        try:
            resp = requests.get(FABRIC_LOADER_VERSIONS_URL, timeout=15)
            resp.raise_for_status()
            self._loader_versions = resp.json()
            return [v["version"] for v in self._loader_versions]
        except Exception as e:
            print(f"Failed to fetch loader versions: {e}")
            return []

    def fetch_installer_info(self) -> tuple[str | None, str | None]:
        """
        Fetch the latest Fabric installer URL and version.
        Returns (url, version) tuple.
        """
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
        """
        Download the Fabric installer JAR. Returns path to the downloaded file.
        Uses cache if already downloaded.
        """
        url, version = self.fetch_installer_info()
        if not url:
            return None

        # Check cache
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

    # ----- Mojang vanilla server.jar download -----

    def _fetch_mojang_manifest(self) -> dict | None:
        """Fetch the Mojang version manifest (cached per session)."""
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
        """Get the URL for a specific MC version's metadata JSON."""
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
        """
        Download the vanilla Minecraft server.jar from Mojang into server_dir.

        This is required because the Fabric installer only installs the loader;
        it expects server.jar to already be present.

        Args:
            mc_version: Minecraft version string (e.g. "1.21.4", "26.1.1")
            server_dir: Path to the server directory
            progress_callback: Optional (fraction, message) callback

        Returns:
            (success, message) tuple
        """
        dest = Path(server_dir) / "server.jar"

        # Skip if already present
        if dest.exists() and dest.stat().st_size > 1000:
            if progress_callback:
                progress_callback(1.0, _("server.jar already present"))
            return True, _("server.jar already present")

        try:
            # Step 1: Get version JSON URL from manifest
            if progress_callback:
                progress_callback(0.05, _("Fetching MC {} metadata...").format(mc_version))

            version_url = self._get_version_json_url(mc_version)
            if not version_url:
                return False, _("Minecraft version {} not found in Mojang manifest").format(mc_version)

            # Step 2: Fetch version JSON
            if progress_callback:
                progress_callback(0.1, _("Reading version details..."))

            resp = requests.get(version_url, timeout=15)
            resp.raise_for_status()
            version_data = resp.json()

            # Step 3: Extract server download URL
            downloads = version_data.get("downloads", {})
            server_info = downloads.get("server")
            if not server_info:
                return False, _("No server download available for MC {}").format(mc_version)

            jar_url = server_info.get("url")
            jar_size = server_info.get("size", 0)

            if not jar_url:
                return False, _("server.jar URL not found in version metadata")

            # Step 4: Download server.jar
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
            # Clean up partial download
            dest.unlink(missing_ok=True)
            return False, _("Failed to download server.jar: {}").format(e)

    # ----- Fabric installation -----

    def install_fabric_server(
        self,
        java_path: str,
        installer_jar: str,
        mc_version: str,
        server_dir: str,
        loader_version: str | None = None,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> tuple[bool, str]:
        """
        Run the Fabric installer to set up a server.

        Args:
            java_path: Path to the java binary.
            installer_jar: Path to the Fabric installer JAR.
            mc_version: Minecraft version string.
            server_dir: Directory to install the server into.
            loader_version: Optional specific loader version.
            progress_callback: Progress callback.

        Returns:
            (success, message) tuple.
        """
        import subprocess

        Path(server_dir).mkdir(parents=True, exist_ok=True)

        cmd = [
            java_path,
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
                # Verify the launch jar exists
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

    def fetch_all_versions_async(self, callback: Callable[[list[str], list[str]], None]):
        """
        Fetch game and loader versions in a background thread.
        Calls callback(game_versions, loader_versions) when done.
        """

        def _fetch():
            games = self.fetch_game_versions()
            loaders = self.fetch_loader_versions()
            callback(games, loaders)

        thread = threading.Thread(target=_fetch, daemon=True)
        thread.start()
        return thread

    # ----- Forge metadata + installation -----

    def _fetch_forge_metadata(self) -> dict[str, list[str]]:
        """Fetch and cache the Forge maven metadata, grouped by MC version.

        Returns a dict mapping each MC version (>= FORGE_MIN_MC_VERSION) to its
        list of Forge builds, newest-first (the maven listing order).
        """
        if self._forge_metadata is not None:
            return self._forge_metadata

        try:
            resp = requests.get(FORGE_VERSIONS_URL, timeout=20)
            resp.raise_for_status()
            import xml.etree.ElementTree as ET

            root = ET.fromstring(resp.content)
            raw_versions = [v.text.strip() for v in root.findall("./versioning/versions/version") if v.text]
        except Exception as e:
            print(f"Failed to fetch Forge metadata: {e}")
            self._forge_metadata = {}
            return self._forge_metadata

        grouped: dict[str, list[str]] = {}
        for full in raw_versions:
            if "-" not in full:
                continue
            mc, _, build = full.partition("-")
            if not mc or not build:
                continue
            parsed = parse_mc_version(mc)
            if not parsed or parsed < FORGE_MIN_MC_VERSION:
                continue
            grouped.setdefault(mc, []).append(build)

        self._forge_metadata = grouped
        return grouped

    def fetch_forge_game_versions(self) -> list[str]:
        """Return Forge-supported Minecraft versions, newest first."""
        grouped = self._fetch_forge_metadata()
        versions = list(grouped.keys())
        versions.sort(key=lambda v: parse_mc_version(v) or (0, 0, 0), reverse=True)
        return versions

    def fetch_forge_builds(self, mc_version: str) -> list[str]:
        """Return available Forge builds for a Minecraft version, newest first."""
        grouped = self._fetch_forge_metadata()
        return list(grouped.get(mc_version, []))

    def _fetch_forge_promotions(self) -> dict[str, str]:
        """Fetch and cache the Forge promotions map (e.g. '1.21.4-recommended' -> build)."""
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
        """Recommended Forge build for an MC version, falling back to latest/newest available."""
        promos = self._fetch_forge_promotions()
        build = promos.get(f"{mc_version}-recommended") or promos.get(f"{mc_version}-latest")
        if build:
            return build
        builds = self.fetch_forge_builds(mc_version)
        return builds[0] if builds else None

    def get_forge_latest_build(self, mc_version: str) -> str | None:
        """Latest Forge build for an MC version, falling back to recommended/newest available."""
        promos = self._fetch_forge_promotions()
        build = promos.get(f"{mc_version}-latest") or promos.get(f"{mc_version}-recommended")
        if build:
            return build
        builds = self.fetch_forge_builds(mc_version)
        return builds[0] if builds else None

    def download_forge_installer(
        self,
        full_version: str,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> str | None:
        """Download the Forge installer JAR for a full '<mc>-<build>' version. Uses cache."""
        url = get_forge_installer_url(full_version)
        cached_jar = CACHE_DIR / f"forge-installer-{full_version}.jar"
        if cached_jar.exists():
            if progress_callback:
                progress_callback(1.0, _("Using cached installer"))
            return str(cached_jar)

        try:
            if progress_callback:
                progress_callback(0.0, _("Downloading Forge installer..."))

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
            print(f"Failed to download Forge installer: {e}")
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
        """
        Run the Forge installer to set up a server via ``--installServer``.

        The Forge installer fetches the vanilla server.jar itself, so the Mojang
        server.jar download must be skipped for Forge servers.

        Args:
            java_path: Path to the java binary.
            installer_jar: Path to the Forge installer JAR.
            mc_version: Minecraft version string.
            forge_build: Forge build string (e.g. "54.1.14").
            server_dir: Directory to install the server into.
            progress_callback: Progress callback.

        Returns:
            (success, message) tuple.
        """
        import subprocess

        Path(server_dir).mkdir(parents=True, exist_ok=True)

        if progress_callback:
            progress_callback(0.3, _("Installing Forge server for MC {}...").format(mc_version))

        cmd = [java_path, "-jar", installer_jar, "--installServer"]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
                cwd=server_dir,
                **hidden_subprocess_kwargs(),
            )

            # The installer leaves a log file in the server dir named after the
            # running jar. Niksnaks-Hosting caches the installer under a "forge-installer-"
            # prefixed name, so match both that and Forge's stock naming.
            for log in Path(server_dir).glob("forge*installer*.jar.log"):
                try:
                    log.unlink(missing_ok=True)
                except Exception:
                    pass

            if result.returncode == 0 and is_loader_installed(server_dir, LOADER_FORGE):
                if progress_callback:
                    progress_callback(1.0, _("Forge server installed successfully"))
                return True, _("Installation successful")
            else:
                error_msg = result.stderr or result.stdout or _("Unknown error")
                if result.returncode == 0 and not is_loader_installed(server_dir, LOADER_FORGE):
                    return False, _("Installation completed but Forge server files were not found")
                return False, _("Installation failed: {}").format(error_msg)

        except subprocess.TimeoutExpired:
            return False, _("Installation timed out (15 minutes)")
        except Exception as e:
            return False, _("Installation error: {}").format(e)

    def fetch_versions_for_loader_async(
        self,
        loader_type: str,
        callback: Callable[[list[str], list[str]], None],
    ):
        """
        Fetch game (and loader, where applicable) versions for a loader, in a background thread.

        For Fabric the callback receives (game_versions, loader_versions). For Forge
        loader_versions is empty -- the build for a chosen MC version is resolved on demand.
        """

        def _fetch():
            if normalize_loader(loader_type) == LOADER_FORGE:
                games = self.fetch_forge_game_versions()
                callback(games, [])
            else:
                games = self.fetch_game_versions()
                loaders = self.fetch_loader_versions()
                callback(games, loaders)

        thread = threading.Thread(target=_fetch, daemon=True)
        thread.start()
        return thread
