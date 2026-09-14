"""Who may log into the web panel, and which servers each of them may touch.

Users live in web_users.json in the data folder, one scrypt hash each; the admin
makes them in the GTK app's Settings. Sessions are only in memory: restart the app
and everyone logs in again, which is a feature for something that hands out console
access.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from niksnaks_hosting.shared.utils.constants import DATA_DIR

USERS_FILE = DATA_DIR / "web_users.json"
SESSION_LIFETIME_S = 30 * 24 * 3600
LOGIN_FAILURES_ALLOWED = 10
LOGIN_LOCKOUT_S = 60

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)


@dataclass
class WebUser:
    username: str
    salt: str
    hash: str
    servers: list[str] = field(default_factory=list)
    admin: bool = False

    def may_access(self, server_id: str) -> bool:
        return self.admin or server_id in self.servers

    def to_public(self) -> dict:
        return {"username": self.username, "admin": self.admin, "servers": list(self.servers)}


class UserStore:
    def __init__(self, path: Path = USERS_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._users: dict[str, WebUser] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        for name, item in (raw.get("users") or {}).items():
            if not isinstance(item, dict) or not item.get("salt") or not item.get("hash"):
                continue
            self._users[str(name)] = WebUser(
                username=str(name),
                salt=str(item["salt"]),
                hash=str(item["hash"]),
                servers=[str(s) for s in item.get("servers", []) if s],
                admin=bool(item.get("admin", False)),
            )

    def _save(self) -> None:
        payload = {
            "users": {
                u.username: {"salt": u.salt, "hash": u.hash, "servers": u.servers, "admin": u.admin}
                for u in self._users.values()
            }
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    @staticmethod
    def valid_username(name: str) -> bool:
        return 1 <= len(name) <= 32 and all(c.isalnum() or c in "._-" for c in name)

    def users(self) -> list[WebUser]:
        with self._lock:
            return sorted(self._users.values(), key=lambda u: u.username.lower())

    def get(self, username: str) -> WebUser | None:
        with self._lock:
            return self._users.get(username)

    def set_user(self, username: str, password: str | None, servers: list[str], admin: bool) -> WebUser:
        """Create the user, or update it; *password* None keeps the one it has."""
        with self._lock:
            existing = self._users.get(username)
            if password is None and existing is None:
                raise ValueError("A new user needs a password.")
            if password is not None:
                salt = secrets.token_bytes(16)
                salt_hex, hash_hex = salt.hex(), _hash_password(password, salt).hex()
            else:
                salt_hex, hash_hex = existing.salt, existing.hash
            user = WebUser(username, salt_hex, hash_hex, sorted(set(servers)), admin)
            self._users[username] = user
            self._save()
            return user

    def remove(self, username: str) -> bool:
        with self._lock:
            if self._users.pop(username, None) is None:
                return False
            self._save()
            return True

    def verify(self, username: str, password: str) -> WebUser | None:
        user = self.get(username)
        if not user:
            # Same cost as a real check, so a missing name is not faster than a wrong password.
            _hash_password(password, b"0" * 16)
            return None
        try:
            expected = bytes.fromhex(user.hash)
            got = _hash_password(password, bytes.fromhex(user.salt))
        except ValueError:
            return None
        return user if hmac.compare_digest(expected, got) else None


class SessionStore:
    def __init__(self, users: UserStore):
        self._users = users
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[str, float]] = {}  # token -> (username, expires)
        self._failures: dict[str, tuple[int, float]] = {}  # ip -> (count, locked_until)

    def login(self, username: str, password: str, client_ip: str) -> str | None:
        """A session token, or None. Ten wrong tries from one address earn a minute's wait."""
        now = time.time()
        with self._lock:
            count, locked_until = self._failures.get(client_ip, (0, 0.0))
            if locked_until > now:
                return None
        user = self._users.verify(username, password)
        with self._lock:
            if not user:
                count += 1
                locked = now + LOGIN_LOCKOUT_S if count >= LOGIN_FAILURES_ALLOWED else 0.0
                if locked:
                    count = 0
                self._failures[client_ip] = (count, locked)
                return None
            self._failures.pop(client_ip, None)
            token = secrets.token_urlsafe(32)
            self._sessions[token] = (user.username, now + SESSION_LIFETIME_S)
            return token

    def user_for(self, token: str | None) -> WebUser | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            entry = self._sessions.get(token)
            if not entry:
                return None
            username, expires = entry
            if expires < now:
                self._sessions.pop(token, None)
                return None
            self._sessions[token] = (username, now + SESSION_LIFETIME_S)
        user = self._users.get(username)
        if user is None:
            self.logout(token)
        return user

    def logout(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def logout_user(self, username: str) -> None:
        """Every session of one user, for when the admin removes or changes them."""
        with self._lock:
            for token, (name, _exp) in list(self._sessions.items()):
                if name == username:
                    del self._sessions[token]
