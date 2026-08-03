import re
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path

import requests

from niksnaks_hosting.shared.utils.constants import JRES_DIR, get_adoptium_jre_download_info, get_required_java_version
from niksnaks_hosting.shared.utils.subprocess_utils import hidden_subprocess_kwargs

class JavaManager:
    def __init__(self):
        self._system_java_version: int | None = None
        self._system_java_checked = False

    def _ensure_system_java_detected(self):
        if self._system_java_checked:
            return
        self._detect_system_java()

    def _detect_system_java(self):
        self._system_java_checked = True
        try:
            result = subprocess.run(
                ["java", "-version"],
                capture_output=True,
                text=True,
                timeout=10,
                **hidden_subprocess_kwargs(),
            )
            output = result.stderr + result.stdout
            match = re.search(r'version "([\d\.]+)', output)
            if not match:
                self._system_java_version = None
                return

            version_text = match.group(1)
            parts = version_text.split(".")
            major = int(parts[0])

            if major == 1 and len(parts) > 1:
                major = int(parts[1])

            self._system_java_version = major
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._system_java_version = None

    @property
    def system_java_version(self) -> int | None:
        self._ensure_system_java_detected()
        return self._system_java_version

    def get_java_path(self, java_version: int) -> str | None:
        managed_path = self._get_managed_java_path(java_version)
        if managed_path:
            return managed_path

        self._ensure_system_java_detected()
        if self._system_java_version and self._system_java_version >= java_version:
            return shutil.which("java")

        return None

    def _get_managed_java_path(self, java_version: int) -> str | None:
        jre_dir = JRES_DIR / f"jre-{java_version}"
        if not jre_dir.exists():
            return None

        exe_name = "java.exe" if sys.platform == "win32" else "java"

        for child in jre_dir.iterdir():
            if child.is_dir():
                java_bin = child / "bin" / exe_name
                if java_bin.exists():
                    return str(java_bin)

        java_bin = jre_dir / "bin" / exe_name
        if java_bin.exists():
            return str(java_bin)

        return None

    def is_java_available(self, java_version: int) -> bool:
        return self.get_java_path(java_version) is not None

    def get_java_for_mc(self, mc_version: str) -> str | None:
        java_ver = get_required_java_version(mc_version)
        return self.get_java_path(java_ver)

    def download_jre(
        self,
        java_version: int,
        progress_callback: Callable[[float, str], None] | None = None,
        done_callback: Callable[[bool, str], None] | None = None,
    ):

        thread = threading.Thread(
            target=self._download_jre_thread, args=(java_version, progress_callback, done_callback), daemon=True
        )
        thread.start()
        return thread

    def _download_jre_thread(self, java_version: int, progress_callback, done_callback):
        try:
            url, archive_type = get_adoptium_jre_download_info(java_version)
            jre_dir = JRES_DIR / f"jre-{java_version}"
            if archive_type == "zip":
                archive_path = JRES_DIR / f"jre-{java_version}.zip"
            else:
                archive_path = JRES_DIR / f"jre-{java_version}.tar.gz"

            if progress_callback:
                progress_callback(0.0, _("Downloading JRE {}...").format(java_version))

            response = requests.get(url, stream=True, timeout=60, allow_redirects=True)
            response.raise_for_status()

            total_size = int(response.headers.get("content-length", 0))
            downloaded = 0

            with open(archive_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0 and progress_callback:
                        frac = downloaded / total_size * 0.7
                        size_mb = downloaded / (1024 * 1024)
                        total_mb = total_size / (1024 * 1024)
                        progress_callback(
                            frac, _("Downloading JRE {}... {:.1f}/{:.1f} MB").format(java_version, size_mb, total_mb)
                        )

            if progress_callback:
                progress_callback(0.75, _("Extracting JRE {}...").format(java_version))

            if jre_dir.exists():
                shutil.rmtree(jre_dir)
            jre_dir.mkdir(parents=True, exist_ok=True)

            if archive_type == "zip":
                with zipfile.ZipFile(archive_path, "r") as archive:
                    archive.extractall(path=jre_dir)
            else:
                with tarfile.open(archive_path, "r:gz") as archive:
                    archive.extractall(path=jre_dir)

            archive_path.unlink(missing_ok=True)

            java_path = self._get_managed_java_path(java_version)
            if java_path:

                if sys.platform != "win32":
                    Path(java_path).chmod(0o755)
                if progress_callback:
                    progress_callback(1.0, _("JRE {} ready").format(java_version))
                if done_callback:
                    done_callback(True, _("JRE {} installed successfully").format(java_version))
            else:
                if done_callback:
                    done_callback(False, _("JRE {} extraction failed: java binary not found").format(java_version))

        except Exception as e:
            if done_callback:
                done_callback(False, _("Failed to download JRE {}: {}").format(java_version, e))

    def download_jre_sync(self, java_version: int, progress_callback=None) -> tuple[bool, str]:
        result = [False, ""]

        def on_done(success, msg):
            result[0] = success
            result[1] = msg

        thread = self.download_jre(java_version, progress_callback, on_done)
        thread.join()
        return tuple(result)
