from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from niksnaks_hosting.shared.remote.ssh_client import SshError
from niksnaks_hosting.shared.utils.constants import get_adoptium_jre_url_for

if TYPE_CHECKING:
    from niksnaks_hosting.shared.remote.host import SshHost

# Runtimes live beside the servers, under the login user's home, so installing one
# never needs root on the host -- or Administrator, on a Windows one.
REMOTE_JRES_DIR = "niksnaks-hosting/jres"

ProgressFn = Callable[[float, str], None]


class RemoteJavaManager:
    """Puts a Java runtime on a host and finds it again.

    The host's own platform and architecture decide which build is fetched -- a Windows
    desktop managing a Linux box needs the Linux build, and the other way round -- and
    the download runs on the host, so a runtime never travels through this machine.
    """

    def __init__(self, host: SshHost) -> None:
        self.host = host
        self._arch: str = ""

    @property
    def jres_dir(self) -> str:
        return f"{self.host.home}/{REMOTE_JRES_DIR}"

    def machine(self) -> str:
        """The host's architecture, in the spelling Adoptium's arch map understands."""
        if self._arch:
            return self._arch
        # Detection already asked, as part of working out which kind of machine this is.
        if self.host.arch:
            self._arch = self.host.arch
            return self._arch
        result = self.host.run(["uname", "-m"], timeout=20)
        self._arch = result.stdout.strip() or "x86_64"
        return self._arch

    def java_path(self, java_version: int) -> str:
        """The managed runtime for this version, or the host's own java, or nothing."""
        managed = self._find_managed(java_version)
        if managed:
            return managed
        system = self.host.which(self.host.ops.java_binary)
        if system and self._version_of(system) >= java_version:
            return system
        return ""

    def jre_roots(self, java_version: int) -> list[str]:
        """Where a runtime of this version could already be, best first.

        The second entry is the one that matters for an adopted server: a copy of this
        app installed on that machine has its own runtimes, in exactly this layout, and
        downloading a second identical JRE beside them would be a few hundred megabytes
        spent to end up where we already were.
        """
        version = int(java_version)
        roots = [f"{self.jres_dir}/jre-{version}"]
        try:
            far_data = self.host.ops.default_data_dir(self.host)
        except Exception:
            far_data = ""
        if far_data:
            roots.append(f"{far_data}/jres/jre-{version}")
        return roots

    def _find_managed(self, java_version: int) -> str:
        """Look for the binary rather than guessing the folder name.

        The archive unpacks to a single versioned directory -- jdk-21.0.5+11-jre --
        whose name is not known ahead of time. Walking it over SFTP rather than with a
        shell glob means one fewer command dialect to get right, and works the same on
        both kinds of host.
        """
        binary = self.host.ops.java_binary
        for root_path in self.jre_roots(java_version):
            root = self.host.path(root_path)
            try:
                entries = sorted((entry for entry in root.iterdir()), key=lambda p: p.name)
            except Exception:
                continue
            for entry in entries:
                candidate = entry / "bin" / binary
                try:
                    if candidate.is_file():
                        return self.host.to_native(str(candidate))
                except Exception:
                    continue
        return ""

    def _version_of(self, java_binary: str) -> int:
        result = self.host.run([java_binary, "-version"], timeout=30)
        match = re.search(r'version "(\d+)(?:\.(\d+))?', result.output)
        if not match:
            return 0
        major = int(match.group(1))
        if major == 1 and match.group(2):
            return int(match.group(2))
        return major

    def ensure_java(self, java_version: int, progress: ProgressFn | None = None) -> str:
        """The path to a runtime of at least this version, installing one if needed."""
        existing = self.java_path(java_version)
        if existing:
            return existing

        java_version = int(java_version)
        ops = self.host.ops
        url, archive_type = get_adoptium_jre_url_for(java_version, ops.java_os_name, self.machine())
        root = f"{self.jres_dir}/jre-{java_version}"
        archive = f"{root}/download.{archive_type}"

        self.host.path(root).mkdir(parents=True, exist_ok=True)
        if progress:
            progress(0.0, _("Downloading Java {} on {}").format(java_version, self.host.name))

        # Adoptium answers with a redirect to the real file; every downloader used here
        # follows it.
        self.host.fetch_url(
            url,
            archive,
            progress=(lambda f, n: progress(f * 0.8, _("Downloading Java {}").format(java_version)))
            if progress
            else None,
            timeout=900,
        )

        if progress:
            progress(0.85, _("Unpacking Java {}").format(java_version))
        unpack = ops.extract_archive(self.host, archive, root)
        if not unpack.ok:
            raise SshError(
                _("Could not unpack Java {} on {}: {}").format(java_version, self.host.name, unpack.output)
            )
        self.host.path(archive).unlink(missing_ok=True)

        path = self._find_managed(java_version)
        if not path:
            raise SshError(
                _("Java {} was unpacked on {} but no java binary was found in it.").format(
                    java_version, self.host.name
                )
            )
        ops.make_executable(self.host, path)
        if progress:
            progress(1.0, _("Java {} ready").format(java_version))
        return path
