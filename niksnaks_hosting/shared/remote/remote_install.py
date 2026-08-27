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
            # A path stops being POSIX the moment it enters a command line: on a Windows
            # host the installer would otherwise be handed /C:/Users/... , which Java
            # reads as a folder called C: at the root of the drive.
            result = self.host.run(
                [java_path, "-jar", self.host.to_native(installer), "--installServer"],
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

            command = [
                java_path, "-jar", self.host.to_native(installer),
                "server", "-mcversion", mc_version, "-dir", self.host.to_native(root),
            ]
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


def install_remote_bedrock(
    host: SshHost,
    server_dir: str,
    option,
    progress: ProgressFn | None = None,
) -> None:
    """Put a Bedrock server on a host. No Java, no loader -- one zip and a binary.

    The zip is fetched by the host, like everything else here: it is a few hundred
    megabytes, and there is no reason for it to travel to this machine first. Which zip
    depends on the host's own platform, not this one's, which is why the caller has to
    have asked BedrockManager for that platform's build.
    """
    from niksnaks_hosting.shared.backend.bedrock_manager import BEDROCK_USER_AGENT

    ops = host.ops
    root = host.path(server_dir)
    root.mkdir(parents=True, exist_ok=True)
    archive = f"{server_dir}/bedrock-server.zip"

    if progress:
        progress(0.0, _("Downloading Bedrock {} on {}").format(option.version, host.name))
    # Mojang's download refuses a request with no user agent, and a plain curl one is
    # exactly the shape it refuses.
    host.fetch_url(
        option.url,
        archive,
        progress=(lambda f, n: progress(f * 0.7, _("Downloading Bedrock {}").format(option.version)))
        if progress
        else None,
        timeout=INSTALL_TIMEOUT,
        user_agent=BEDROCK_USER_AGENT,
    )

    if progress:
        progress(0.75, _("Unpacking Bedrock {} on {}").format(option.version, host.name))
    unpack = ops.extract_archive(host, archive, server_dir)
    if not unpack.ok:
        raise SshError(
            _("Could not unpack the Bedrock server on {}: {}").format(host.name, unpack.output)
        )
    host.path(archive).unlink(missing_ok=True)

    binary = root / ops.bedrock_binary
    if not binary.is_file():
        raise SshError(
            _("The Bedrock server was unpacked on {} but its program is not in it.").format(host.name)
        )
    ops.make_executable(host, host.to_native(binary))
    if progress:
        progress(1.0, _("Bedrock {} ready").format(option.version))


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
