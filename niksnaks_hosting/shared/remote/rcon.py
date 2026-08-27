"""Minecraft's own remote console, spoken over the SSH connection that is already open.

A Windows host has no mkfifo, so the trick that gives a Linux server a writable stdin
does not transfer. Minecraft has always had a second way in -- RCON -- and the protocol
is small enough to speak directly rather than take a dependency for it.

The usual reason not to enable RCON is that it opens a TCP port with a password on it.
This app never has to reach it any other way: the socket this class opens is a
direct-tcpip channel on the far machine, so as far as the server is concerned the
connection came from 127.0.0.1. That says nothing about who *else* can reach the port,
though -- RCON is served wherever the server itself is bound, which has to stay open
for players -- so enabling it does put a password-protected port on the network unless
something else closes it. WindowsTaskService._restrict_rcon_to_loopback tries to add
the firewall rule that closes it, and says so in the console when it cannot. This
module has no part in that and cannot enforce it on its own.
"""

from __future__ import annotations

import struct
from typing import Any

from niksnaks_hosting.shared.remote.ssh_client import SshError

# Packet types, from the Source RCON protocol Minecraft implements.
AUTH = 3
AUTH_RESPONSE = 2
EXEC_COMMAND = 2
RESPONSE_VALUE = 0

# A failed login comes back with this in place of the request id.
AUTH_FAILED = -1

# The server answers a command in one packet unless the reply is very long. Anything
# bigger than this is a malformed length field, not a real console line.
MAX_PACKET = 4 * 1024 * 1024

RCON_TIMEOUT = 20.0


class RconError(SshError):
    """The console could not be reached or would not take the command."""


class RconClient:
    """One short-lived RCON session over an SSH channel.

    Deliberately not kept open between commands: a console command is a rare, deliberate
    act, and a connection held open for hours is one more thing to notice dying.
    """

    def __init__(self, channel: Any, password: str) -> None:
        self._channel = channel
        self._password = password
        self._next_id = 1

    # ------------------------------------------------------------------- protocol
    def _pack(self, request_id: int, packet_type: int, body: str) -> bytes:
        payload = struct.pack("<ii", request_id, packet_type) + body.encode("utf-8") + b"\x00\x00"
        return struct.pack("<i", len(payload)) + payload

    def _read_exactly(self, count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            try:
                chunk = self._channel.recv(count - len(data))
            except TimeoutError as e:
                raise RconError(_("The server's console did not answer in time.")) from e
            if not chunk:
                raise RconError(_("The server closed the console connection."))
            data += chunk
        return bytes(data)

    def _read_packet(self) -> tuple[int, int, str]:
        (length,) = struct.unpack("<i", self._read_exactly(4))
        if length < 10 or length > MAX_PACKET:
            raise RconError(_("The server's console sent something this app cannot read."))
        payload = self._read_exactly(length)
        request_id, packet_type = struct.unpack("<ii", payload[:8])
        # Two trailing nulls: one ends the body, one ends the packet.
        body = payload[8:-2].decode("utf-8", errors="replace")
        return request_id, packet_type, body

    def _send(self, packet_type: int, body: str) -> int:
        request_id = self._next_id
        self._next_id += 1
        self._channel.sendall(self._pack(request_id, packet_type, body))
        return request_id

    # ---------------------------------------------------------------------- usage
    def login(self) -> None:
        sent = self._send(AUTH, self._password)
        while True:
            request_id, packet_type, _body = self._read_packet()
            # Minecraft answers with an empty RESPONSE_VALUE first; the verdict is in
            # the AUTH_RESPONSE that follows it.
            if packet_type != AUTH_RESPONSE:
                continue
            if request_id == AUTH_FAILED:
                raise RconError(
                    _("The server's console refused this app's password. Restart the server once.")
                )
            if request_id != sent:
                raise RconError(_("The server's console answered a question nobody asked."))
            return

    def command(self, line: str) -> str:
        sent = self._send(EXEC_COMMAND, line)
        # A second, empty command marks the end of the reply: the server answers packets
        # in order, so its response to this one cannot arrive before the real reply is
        # complete. Without it a multi-packet answer gets truncated to its first part.
        marker = self._send(RESPONSE_VALUE, "")

        parts: list[str] = []
        while True:
            request_id, _packet_type, body = self._read_packet()
            if request_id == marker:
                return "".join(parts)
            if request_id == sent:
                parts.append(body)

    def close(self) -> None:
        try:
            self._channel.close()
        except Exception:
            pass


def send_rcon(host: Any, port: int, password: str, line: str, timeout: float = RCON_TIMEOUT) -> str:
    """Log in, say one thing, and hang up."""
    if not password:
        raise RconError(
            _("This server has no console password set yet. Restart it once and try again.")
        )
    channel = host.connection.open_tunnel("127.0.0.1", int(port), timeout=timeout)
    client = RconClient(channel, password)
    try:
        client.login()
        return client.command(line)
    finally:
        client.close()
