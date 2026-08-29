import re
from pathlib import Path
from typing import Any

from niksnaks_hosting.shared.remote.paths import is_remote

class ConfigManager:
    def __init__(self, server_dir: Any):
        # A folder on another machine arrives as a RemotePath and has to stay one;
        # rebuilding it as a Path would read this computer's disk at the same
        # spelling, which is how a remote server ends up silently editing nothing.
        self.server_dir = server_dir if is_remote(server_dir) else Path(server_dir)
        self.properties_path = self.server_dir / "server.properties"
        self._lines: list[str] = []
        self._properties: dict[str, str] = {}
        self._loaded = False

    def load(self) -> dict[str, str]:
        self._lines = []
        self._properties = {}

        if not self.properties_path.exists():
            self._loaded = True
            return self._properties

        self._lines = self.properties_path.read_text(encoding="utf-8").splitlines(keepends=True)

        for line in self._lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                match = re.match(r"^([^=]+)=(.*)", stripped)
                if match:
                    key = match.group(1).strip()
                    value = match.group(2).strip()
                    self._properties[key] = value

        self._loaded = True
        return dict(self._properties)

    def try_load(self) -> bool:
        """Load the properties, or say it could not be done. Never raises.

        For a server on another machine the read fails when the machine is not
        answering, which is not the same as the file being empty. Nothing is marked as
        loaded in that case, so a later save() reads it properly first or refuses --
        writing back what little was in memory would be how a server.properties gets
        truncated by a computer having been asleep.
        """
        try:
            self.load()
            return True
        except Exception:
            self._loaded = False
            return False

    def save(self):
        if not self._loaded:
            self.load()

        used_keys = set()
        new_lines = []

        for line in self._lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                match = re.match(r"^([^=]+)=(.*)", stripped)
                if match:
                    key = match.group(1).strip()
                    if key in self._properties:
                        new_lines.append(f"{key}={self._properties[key]}\n")
                        used_keys.add(key)
                    else:
                        new_lines.append(line)
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        for key, value in self._properties.items():
            if key not in used_keys:
                new_lines.append(f"{key}={value}\n")

        self.server_dir.mkdir(parents=True, exist_ok=True)
        self.properties_path.write_text("".join(new_lines), encoding="utf-8")

    def get(self, key: str, default: str = "") -> str:
        if not self._loaded:
            self.load()
        return self._properties.get(key, default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        val = self.get(key, str(default).lower())
        return val.lower() == "true"

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, str(default)))
        except ValueError:
            return default

    def set_value(self, key: str, value) -> None:
        if not self._loaded:
            self.load()
        if isinstance(value, bool):
            self._properties[key] = str(value).lower()
        else:
            self._properties[key] = str(value)

    def set_eula(self, accepted: bool = True):
        eula_path = self.server_dir / "eula.txt"
        self.server_dir.mkdir(parents=True, exist_ok=True)
        eula_path.write_text(
            f"# Accepted by Niksnaks-Hosting\neula={'true' if accepted else 'false'}\n",
            encoding="utf-8",
        )
