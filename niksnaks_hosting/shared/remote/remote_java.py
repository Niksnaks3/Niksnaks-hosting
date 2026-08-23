from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING

from niksnaks_hosting.shared.remote.ssh_client import SshError
from niksnaks_hosting.shared.utils.constants import get_adoptium_jre_url_for

if TYPE_CHECKING:
    from niksnaks_hosting.shared.remote.host import SshHost

# Runtimes live beside the servers, under the login user's home, so installing one
# never needs root on the host.
REMOTE_JRES_DIR = "niksnaks-hosting/jres"

ProgressFn = Callable[[float, str], None]


class RemoteJavaManager:
    """Puts a Java runtime on a host and finds it again.

    The host's own architecture decides which build is fetched, and the download runs
    on the host, so a runtime never travels through this machine.
    """

    def __init__(self, host: SshHost) -> None:
        self.host = host
        self._arch: str = ""

    @property
    def jres_dir(self) -> str:
        return f"{self.host.home}/{REMOTE_JRES_DIR}"

    def machine(self) -> str:
        if not self._arch:
            result = self.host.run(["uname", "-m"], timeout=20)
            self._arch = result.stdout.strip() or "x86_64"
        return self._arch

    def java_path(self, java_version: int) -> str:
        """The managed runtime for this version, or the host's own java, or nothing."""
        managed = self._find_managed(java_version)
        if managed:
            return managed
        system = self.host.which("java")
        if system and self._version_of(system) >= java_version:
            return system
        return ""

    def _find_managed(self, java_version: int) -> str:
        root = f"{self.jres_dir}/jre-{int(java_version)}"
        # The archive unpacks to a single versioned folder whose name is not known
        # ahead of time, so the binary is looked for rather than assumed.
        found = self.host.run(
            ["sh", "-lc", f"ls -d {shlex.quote(root)}/*/bin/java 2>/dev/null | head -1"],
            timeout=20,
        )
        path = found.stdout.strip()
        return path if path else ""

    def _version_of(self, java_binary: str) -> int:
        result = self.host.run([java_binary, "-version"], timeout=30)
        text = result.output
        import re

        match = re.search(r'version "(\d+)(?:\.(\d+))?', text)
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
        url, archive_type = get_adoptium_jre_url_for(java_version, "linux", self.machine())
        root = f"{self.jres_dir}/jre-{java_version}"
        archive = f"{root}/download.{archive_type}"

        self.host.path(root).mkdir(parents=True, exist_ok=True)
        if progress:
            progress(0.0, _("Downloading Java {} on {}").format(java_version, self.host.name))

        # Adoptium answers with a redirect to the real file; curl and wget both follow it.
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
        unpack = self.host.run(
            ["tar", "-xzf", archive, "-C", root, "--no-same-owner"],
            timeout=600,
        )
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
        self.host.run(["chmod", "+x", path], timeout=20)
        if progress:
            progress(1.0, _("Java {} ready").format(java_version))
        return path
