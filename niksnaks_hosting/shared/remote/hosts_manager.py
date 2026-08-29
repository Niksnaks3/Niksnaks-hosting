from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from niksnaks_hosting.shared.core.events import EventEmitter
from niksnaks_hosting.shared.remote.host import LOCAL_HOST_ID, Host, LocalHost, SshHost
from niksnaks_hosting.shared.remote.ssh_client import SshCredentials, SshError
from niksnaks_hosting.shared.utils.constants import DATA_DIR

HOSTS_FILE = DATA_DIR / "hosts.json"


class HostsManager(EventEmitter):
    """The machines this app can put servers on: always this one, plus any added over SSH.

    Passwords are deliberately not written to hosts.json. A host set up with one is
    asked for it again next launch; a host set up with a key file reconnects on its own.
    """

    def __init__(self, hosts_path: Path = HOSTS_FILE) -> None:
        super().__init__()
        self._path = hosts_path
        self._local = LocalHost()
        self._hosts: dict[str, SshHost] = {}
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------ storage
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        for raw in data.get("hosts", []) if isinstance(data, dict) else []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            credentials = SshCredentials(
                hostname=str(raw.get("hostname", "")),
                port=int(raw.get("port", 22) or 22),
                username=str(raw.get("username", "")),
                key_path=str(raw.get("key_path", "")),
                known_host_key=str(raw.get("known_host_key", "")),
            )
            host = SshHost(
                str(raw["id"]),
                str(raw.get("name", "")),
                credentials,
                # Remembered so the wizard can tell a Windows host from a Linux one
                # before anything has connected. An older file simply has neither, and
                # the host works it out again on first use.
                platform=str(raw.get("platform", "")),
                arch=str(raw.get("arch", "")),
            )
            host.on_platform_detected = self._remember_platform
            self._hosts[host.id] = host

    def _save(self) -> None:
        payload = {
            "hosts": [
                {
                    "id": host.id,
                    "name": host.name,
                    "hostname": host.credentials.hostname,
                    "port": host.credentials.port,
                    "username": host.credentials.username,
                    "key_path": host.credentials.key_path,
                    "platform": host.platform,
                    "arch": host.arch,
                    # Pinned so a swapped-out server is noticed rather than trusted.
                    "known_host_key": host.connection.trusted_key or host.credentials.known_host_key,
                }
                for host in self._hosts.values()
            ]
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    # -------------------------------------------------------------------- access
    @property
    def local(self) -> LocalHost:
        return self._local

    def get(self, host_id: str | None) -> Host | None:
        """The host a server lives on. An empty id has always meant this computer."""
        if not host_id:
            return self._local
        return self._hosts.get(str(host_id))

    def require(self, host_id: str | None) -> Host:
        host = self.get(host_id)
        if host is None:
            raise SshError(_("That server's host is no longer set up in this app."))
        return host

    def all_hosts(self) -> list[Host]:
        return [self._local, *self._hosts.values()]

    def ssh_hosts(self) -> list[SshHost]:
        return list(self._hosts.values())

    def is_remote(self, host_id: str | None) -> bool:
        return bool(host_id) and str(host_id) != LOCAL_HOST_ID

    # ------------------------------------------------------------------- editing
    def add(
        self,
        name: str,
        hostname: str,
        username: str,
        port: int = 22,
        password: str = "",
        key_path: str = "",
        key_passphrase: str = "",
    ) -> SshHost:
        credentials = SshCredentials(
            hostname=hostname.strip(),
            port=int(port or 22),
            username=username.strip(),
            password=password,
            key_path=key_path.strip(),
            key_passphrase=key_passphrase,
        )
        host = SshHost(uuid.uuid4().hex[:12], name.strip() or credentials.label, credentials)
        host.on_platform_detected = self._remember_platform
        with self._lock:
            self._hosts[host.id] = host
            self._save()
        self.emit_on_main_thread("hosts-changed")
        return host

    def remove(self, host_id: str) -> None:
        with self._lock:
            host = self._hosts.pop(str(host_id), None)
            self._save()
        if host:
            host.close()
        self.emit_on_main_thread("hosts-changed")

    def rename(self, host_id: str, name: str) -> None:
        with self._lock:
            host = self._hosts.get(str(host_id))
            if not host:
                return
            host.name = name.strip() or host.credentials.label
            self._save()
        self.emit_on_main_thread("hosts-changed")

    def set_password(self, host_id: str, password: str) -> None:
        """Hold a password for this session only; it never reaches the disk."""
        host = self._hosts.get(str(host_id))
        if host:
            host.credentials.password = password

    def set_key_path(self, host_id: str, key_path: str) -> None:
        """Remember that this host logs in with a key now, so no password is wanted again."""
        with self._lock:
            host = self._hosts.get(str(host_id))
            if not host:
                return
            host.credentials.key_path = key_path
            self._save()
        self.emit_on_main_thread("hosts-changed")

    def needs_password(self, host_id: str) -> bool:
        """Whether this host has nothing to log in with until someone types a password."""
        host = self._hosts.get(str(host_id))
        if not host:
            return False
        return not host.credentials.key_path and not host.credentials.password

    def _remember_platform(self, host: SshHost) -> None:
        """Write down what a host turned out to be, so it is never asked twice."""
        with self._lock:
            if host.id not in self._hosts:
                return
            self._save()

    def platform_of(self, host_id: str | None) -> str:
        """What kind of machine a host is, without connecting to ask.

        Empty until the host has been reached once. Callers that need an answer before
        then have to treat "unknown" as its own case rather than guessing.
        """
        host = self._hosts.get(str(host_id or ""))
        return host.platform if host else ""

    def trust_current_key(self, host_id: str) -> None:
        """Write down the key this host answered with, so a later change is noticed.

        _save() reads the live key off each connection, so there is nothing to pass in
        here -- but a host that has already been removed must not resurrect the file.
        """
        with self._lock:
            if str(host_id) not in self._hosts:
                return
            self._save()

    def close_all(self) -> None:
        for host in list(self._hosts.values()):
            host.close()

    def status(self, host_id: str) -> dict[str, Any]:
        host = self._hosts.get(str(host_id))
        if not host:
            return {"connected": False, "detail": ""}
        return {"connected": host.is_connected, "detail": host.credentials.label}


_shared: HostsManager | None = None
_shared_lock = threading.Lock()


def close_shared_hosts() -> None:
    """Close every SSH connection, if any host was ever resolved this run.

    Quitting should not be the thing that first builds a HostsManager, so this stays
    quiet when nothing ever asked for one.
    """
    with _shared_lock:
        if _shared is not None:
            _shared.close_all()


def get_hosts_manager() -> HostsManager:
    """The one registry of hosts, shared by everything that needs to resolve a host id."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = HostsManager()
        return _shared
