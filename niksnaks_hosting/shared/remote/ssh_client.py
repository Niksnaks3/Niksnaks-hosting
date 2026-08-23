from __future__ import annotations

import shlex
import threading
import time
from dataclasses import dataclass
from typing import Any

# Servers on the other end of a slow link still have to answer; these are generous
# enough for a home connection and short enough that a dead host is noticed.
CONNECT_TIMEOUT = 20.0
COMMAND_TIMEOUT = 120.0
KEEPALIVE_SECONDS = 20

# How often an in-flight command is checked for new output or for having finished.
POLL_SECONDS = 0.02


class SshError(RuntimeError):
    """Anything that stopped the app talking to a host, phrased for a person."""


class HostKeyChanged(SshError):
    """The host answered with a different key than the one that was trusted before."""


@dataclass
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    @property
    def output(self) -> str:
        """Whatever the command had to say, wherever it said it."""
        return (self.stdout + self.stderr).strip()

    def check(self, what: str) -> CommandResult:
        if not self.ok:
            detail = self.output or f"exit status {self.exit_status}"
            raise SshError(f"{what}: {detail}")
        return self


@dataclass
class SshCredentials:
    """How to reach one host. The password, if any, is never written to disk."""

    hostname: str
    port: int = 22
    username: str = ""
    password: str = ""
    key_path: str = ""
    key_passphrase: str = ""
    # Base64 of the host key the app trusted the first time it connected.
    known_host_key: str = ""

    @property
    def label(self) -> str:
        who = f"{self.username}@" if self.username else ""
        where = self.hostname if self.port == 22 else f"{self.hostname}:{self.port}"
        return f"{who}{where}"


class SshConnection:
    """One SSH connection, shared by every part of the app that talks to a host.

    paramiko's Transport multiplexes channels, so commands and file transfers can run
    at the same time over the single TCP connection. The lock guards only connect and
    reconnect; once a channel is open it belongs to whoever asked for it.
    """

    def __init__(self, credentials: SshCredentials) -> None:
        self.credentials = credentials
        self._client: Any = None
        self._sftp: Any = None
        self._lock = threading.RLock()
        self._trusted_key: str = credentials.known_host_key

    # ------------------------------------------------------------------ lifetime
    @property
    def is_connected(self) -> bool:
        client = self._client
        if client is None:
            return False
        transport = client.get_transport()
        return bool(transport and transport.is_active())

    def connect(self) -> None:
        with self._lock:
            if self.is_connected:
                return
            self._connect_locked()

    def _connect_locked(self) -> None:
        paramiko = _import_paramiko()
        creds = self.credentials

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_RecordingHostKeyPolicy(self))

        pkey = None
        if creds.key_path:
            pkey = _load_private_key(creds.key_path, creds.key_passphrase)

        try:
            client.connect(
                hostname=creds.hostname,
                port=int(creds.port or 22),
                username=creds.username or None,
                password=creds.password or None,
                pkey=pkey,
                timeout=CONNECT_TIMEOUT,
                auth_timeout=CONNECT_TIMEOUT,
                banner_timeout=CONNECT_TIMEOUT,
                # An agent or ~/.ssh key is a fine way in when nothing was configured,
                # but never a silent substitute for a key the user did name.
                allow_agent=not creds.key_path,
                look_for_keys=not creds.key_path and not creds.password,
            )
        except HostKeyChanged:
            raise
        except Exception as e:
            raise SshError(_describe_connect_failure(e, creds)) from e

        transport = client.get_transport()
        if transport is not None:
            # Idle SSH connections get dropped by NAT and by sshd's own ClientAlive
            # timers. A server left running overnight should still be reachable.
            transport.set_keepalive(KEEPALIVE_SECONDS)

        self._client = client
        self._sftp = None

    def close(self) -> None:
        with self._lock:
            sftp, client = self._sftp, self._client
            self._sftp = self._client = None
        for closable in (sftp, client):
            try:
                if closable is not None:
                    closable.close()
            except Exception:
                pass

    def _ensure(self) -> Any:
        with self._lock:
            if not self.is_connected:
                self._connect_locked()
            return self._client

    # ------------------------------------------------------------------ commands
    def run(self, command: str, timeout: float = COMMAND_TIMEOUT) -> CommandResult:
        """Run a shell command and collect what it printed.

        This waits for the command's exit status rather than for its output to reach
        end-of-file. Reading to EOF looks simpler and hangs the moment a command leaves
        anything running in the background: the child inherits the channel's output and
        holds it open long after the command itself finished. Starting a service is
        exactly that shape, so the difference is not hypothetical.
        """
        client = self._ensure()
        try:
            channel = client.get_transport().open_session()
        except Exception as e:
            raise SshError(f"Could not run a command on {self.credentials.label}: {e}") from e

        out = bytearray()
        err = bytearray()
        try:
            channel.settimeout(POLL_SECONDS)
            channel.exec_command(command)
            deadline = time.monotonic() + timeout

            while True:
                drained = False
                while channel.recv_ready():
                    out += channel.recv(65536)
                    drained = True
                while channel.recv_stderr_ready():
                    err += channel.recv_stderr(65536)
                    drained = True

                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() > deadline:
                    raise SshError(
                        f"A command on {self.credentials.label} did not finish within "
                        f"{int(timeout)} seconds: {command[:80]}"
                    )
                if not drained:
                    time.sleep(POLL_SECONDS)

            status = channel.recv_exit_status()
        except SshError:
            raise
        except Exception as e:
            raise SshError(f"Could not run a command on {self.credentials.label}: {e}") from e
        finally:
            try:
                channel.close()
            except Exception:
                pass

        return CommandResult(
            status,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )

    def run_args(self, args: list[str], timeout: float = COMMAND_TIMEOUT) -> CommandResult:
        return self.run(shlex.join(args), timeout=timeout)

    def open_stream(self, command: str) -> Any:
        """Start a command and hand back its channel, for output that arrives over time."""
        client = self._ensure()
        try:
            transport = client.get_transport()
            channel = transport.open_session()
            channel.set_combine_stderr(True)
            channel.exec_command(command)
            return channel
        except Exception as e:
            raise SshError(f"Could not start a command on {self.credentials.label}: {e}") from e

    # ---------------------------------------------------------------------- sftp
    def sftp(self) -> Any:
        with self._lock:
            if self._sftp is not None and self.is_connected:
                return self._sftp
            client = self._ensure()
            try:
                self._sftp = client.open_sftp()
            except Exception as e:
                raise SshError(f"Could not open a file session on {self.credentials.label}: {e}") from e
            return self._sftp

    # ----------------------------------------------------------------- host keys
    @property
    def trusted_key(self) -> str:
        return self._trusted_key

    def _offered_key(self, key: Any) -> None:
        """Called during the handshake with whatever key the host presented."""
        offered = key.get_base64()
        if self._trusted_key and offered != self._trusted_key:
            raise HostKeyChanged(
                f"{self.credentials.hostname} answered with a different host key than the one "
                "this app trusted before. Either the server was rebuilt, or something is "
                "impersonating it. Remove the host and add it again only if you expected this."
            )
        self._trusted_key = offered


class _RecordingHostKeyPolicy:
    """Trust on first use, then hold the host to the key it gave.

    paramiko's AutoAddPolicy would accept a changed key without a word. This keeps the
    first key seen and refuses anything else, which is the part of host-key checking
    that actually catches an interception.
    """

    def __init__(self, connection: SshConnection) -> None:
        self._connection = connection

    def missing_host_key(self, _client: Any, _hostname: str, key: Any) -> None:
        self._connection._offered_key(key)


def _import_paramiko() -> Any:
    try:
        import paramiko
    except ImportError as e:
        raise SshError(
            "SSH support needs the paramiko library, which is not installed in this build."
        ) from e
    return paramiko


def _load_private_key(key_path: str, passphrase: str = "") -> Any:
    paramiko = _import_paramiko()
    errors: list[str] = []
    # paramiko cannot tell key types apart from the file alone, so each is tried in turn.
    for key_class in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey, paramiko.DSSKey):
        try:
            return key_class.from_private_key_file(key_path, password=passphrase or None)
        except Exception as e:  # noqa: PERF203 - the type of failure is the useful part
            errors.append(f"{key_class.__name__}: {e}")
            if "password" in str(e).lower() or "passphrase" in str(e).lower():
                raise SshError(
                    f"The key at {key_path} is protected by a passphrase. Enter it with the key."
                ) from e
    raise SshError(f"Could not read the private key at {key_path}. Tried: {'; '.join(errors)}")


def _describe_connect_failure(error: Exception, creds: SshCredentials) -> str:
    text = str(error) or error.__class__.__name__
    lowered = text.lower()
    where = creds.label
    if "authentication" in lowered:
        return (
            f"{where} refused the login. Check the username, and the password or key you gave it."
        )
    if "timed out" in lowered or "timeout" in lowered:
        return f"{where} did not answer in time. Check the address, the port, and any firewall."
    if "refused" in lowered:
        return f"Nothing is listening for SSH on {where}."
    if "name or service not known" in lowered or "getaddrinfo" in lowered:
        return f"Could not look up the address {creds.hostname}."
    return f"Could not connect to {where}: {text}"
