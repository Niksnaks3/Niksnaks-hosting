from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from niksnaks_hosting.shared.remote.ssh_client import SshError
from niksnaks_hosting.shared.utils.constants import (
    LOADER_FABRIC,
    LOADER_NEOFORGE,
    get_forge_full_version,
    get_forge_installer_url,
    get_loader_display_name,
    get_neoforge_installer_url,
    normalize_loader,
    uses_installer,
)

if TYPE_CHECKING:
    from niksnaks_hosting.shared.remote.host import SshHost

ProgressFn = Callable[[float, str], None]

# The installers are Java programs, so they run on the host beside the server they are
# building. Nothing is fetched to this machine and pushed across.
INSTALL_TIMEOUT = 1800


class RemoteLoaderInstaller:
    """Installs a Minecraft server and its mod loader on a host.

    This mirrors what DownloadManager does locally, with two differences that matter:
    the downloads happen on the host, and the installer runs there too. A modpack
    server is several hundred megabytes of libraries, and none of it should make a
    round trip through the app.
    """

    def __init__(self, host: SshHost) -> None:
        self.host = host

    # ------------------------------------------------------------------- vanilla
    def install_server_jar(
        self, mc_version: str, server_dir: str, progress: ProgressFn | None = None
    ) -> None:
        """Fabric needs the vanilla server.jar beside it; Forge and NeoForge fetch their own."""
        from niksnaks_hosting.shared.backend.download_manager import DownloadManager

        dest = self.host.path(server_dir) / "server.jar"
        if dest.is_file() and dest.stat().st_size > 1000:
            return

        manager = DownloadManager()
        version_url = manager._get_version_json_url(mc_version)
        if not version_url:
            raise SshError(_("Minecraft {} is not in Mojang's version list.").format(mc_version))

        import requests

        data = requests.get(version_url, timeout=20).json()
        server = (data.get("downloads") or {}).get("server") or {}
        url = server.get("url")
        if not url:
            raise SshError(_("Minecraft {} has no server download.").format(mc_version))

        self.host.fetch_url(
            url, str(dest), expected_size=int(server.get("size") or 0), progress=progress,
            timeout=INSTALL_TIMEOUT,
        )

    # -------------------------------------------------------------------- loaders
    def install_loader(
        self,
        loader_type: str,
        mc_version: str,
        loader_version: str,
        server_dir: str,
        java_path: str,
        progress: ProgressFn | None = None,
    ) -> None:
        loader = normalize_loader(loader_type)
        name = get_loader_display_name(loader)
        root = self.host.path(server_dir)
        root.mkdir(parents=True, exist_ok=True)

        if uses_installer(loader):
            if loader == LOADER_NEOFORGE:
                url = get_neoforge_installer_url(loader_version)
            else:
                url = get_forge_installer_url(get_forge_full_version(mc_version, loader_version))
            installer = root / "installer.jar"

            if progress:
                progress(0.05, _("Downloading the {} installer on {}").format(name, self.host.name))
            self.host.fetch_url(
                url,
                str(installer),
                progress=(lambda f, n: progress(0.05 + f * 0.35, n)) if progress else None,
                timeout=INSTALL_TIMEOUT,
            )

            if progress:
                progress(0.45, _("Installing {} on {}").format(name, self.host.name))
            result = self.host.run(
                [java_path, "-jar", str(installer), "--installServer"],
                timeout=INSTALL_TIMEOUT,
                cwd=str(root),
            )
            self._cleanup_installer(root, installer)
            if not result.ok:
                raise SshError(
                    _("The {} installer failed on {}: {}").format(
                        name, self.host.name, _describe_failure(result)
                    )
                )
        else:
            if progress:
                progress(0.05, _("Downloading Minecraft {} on {}").format(mc_version, self.host.name))
            self.install_server_jar(
                mc_version,
                server_dir,
                progress=(lambda f, n: progress(0.05 + f * 0.30, n)) if progress else None,
            )

            installer_url, _version = self._fabric_installer_url()
            installer = root / "installer.jar"
            if progress:
                progress(0.40, _("Downloading the Fabric installer on {}").format(self.host.name))
            self.host.fetch_url(
                installer_url, str(installer), progress=None, timeout=INSTALL_TIMEOUT
            )

            command = [java_path, "-jar", str(installer), "server", "-mcversion", mc_version, "-dir", str(root)]
            if loader_version:
                command += ["-loader", loader_version]
            if progress:
                progress(0.55, _("Installing Fabric on {}").format(self.host.name))
            result = self.host.run(command, timeout=INSTALL_TIMEOUT, cwd=str(root))
            self._cleanup_installer(root, installer)
            if not result.ok:
                raise SshError(
                    _("The Fabric installer failed on {}: {}").format(
                        self.host.name, _describe_failure(result)
                    )
                )

        from niksnaks_hosting.shared.backend.loader_launch import is_loader_installed

        if not is_loader_installed(root, loader):
            raise SshError(
                _("{} finished on {} but its server files are not there.").format(name, self.host.name)
            )
        if progress:
            progress(1.0, _("{} ready").format(name))

    def _cleanup_installer(self, root, installer) -> None:
        try:
            installer.unlink(missing_ok=True)
        except Exception:
            pass
        # Forge-family installers drop a log beside themselves.
        for pattern in ("*installer*.jar.log", "installer.jar.log"):
            try:
                for leftover in root.glob(pattern):
                    leftover.unlink(missing_ok=True)
            except Exception:
                pass

    def _fabric_installer_url(self) -> tuple[str, str]:
        import requests

        from niksnaks_hosting.shared.utils.constants import FABRIC_INSTALLER_VERSIONS_URL

        response = requests.get(FABRIC_INSTALLER_VERSIONS_URL, timeout=20)
        response.raise_for_status()
        installers = response.json() or []
        if not installers:
            raise SshError(_("Could not find a Fabric installer to download."))
        latest = installers[0]
        return str(latest.get("url") or ""), str(latest.get("version") or "")


def _describe_failure(result) -> str:
    """Installers that fail silently still have to say something useful."""
    text = result.output.strip()
    if text:
        return text[-600:]
    return _("it exited with status {} and said nothing.").format(result.exit_status)


def install_remote_server(
    host: SshHost,
    server_dir: str,
    mc_version: str,
    loader_type: str,
    loader_version: str,
    java_version: int,
    progress: ProgressFn | None = None,
) -> str:
    """Everything a fresh remote server needs: a runtime, then the loader. Returns the java path."""
    from niksnaks_hosting.shared.remote.remote_java import RemoteJavaManager

    java = RemoteJavaManager(host).ensure_java(
        java_version,
        progress=(lambda f, text: progress(f * 0.35, text)) if progress else None,
    )
    RemoteLoaderInstaller(host).install_loader(
        loader_type or LOADER_FABRIC,
        mc_version,
        loader_version,
        server_dir,
        java,
        progress=(lambda f, text: progress(0.35 + f * 0.65, text)) if progress else None,
    )
    return java
