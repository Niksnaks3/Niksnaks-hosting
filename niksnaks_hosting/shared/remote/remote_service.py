"""What "keep this server running on that machine" means, without saying how.

Two machines answer it very differently. Linux has systemd user units, which can hold a
FIFO open as the process's stdin and type "stop" into it on the way down. Windows has
Scheduled Tasks, which survive the SSH session but have no stdin at all, so console input
goes over RCON instead.

Everything above this line -- the console view, the player counter, the status dot --
only ever asks the questions in this file, so neither answer leaks upwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# One unit or task per server, named so a person reading `systemctl --user list-units` or
# `schtasks /Query` can tell what it is and the app can find its own again.
UNIT_PREFIX = "niksnaks-"

CONSOLE_LOG = "console.log"

# Long enough for a big modded world to finish writing itself out. A server killed part
# way through that is a corrupted region file, so this errs high on purpose.
STOP_GRACE_SECONDS = 180


def unit_name(server_id: str) -> str:
    """A name that is unique per server and legal as both a filename and a task name."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(server_id))[:40] or "server"
    return f"{UNIT_PREFIX}{safe}"


@dataclass
class UnitState:
    """What the machine says about the server process right now."""

    active: bool
    status: str
    pid: int = 0
    memory_bytes: int = 0
    cpu_seconds: float = 0.0


@runtime_checkable
class RemoteService(Protocol):
    """One server, run on a host in whatever way that host can keep alive.

    SystemdService satisfies this as written; WindowsTaskService is built to it.
    """

    host: Any
    server_id: str
    server_dir: str
    unit: str

    # --------------------------------------------------------------- installation
    def write_unit(self, description: str, exec_args: list[str], ram_mb: int) -> None:
        """Describe the server to the host's service manager, replacing any earlier one."""

    def remove_unit(self) -> None:
        """Withdraw it, so a deleted server leaves nothing pointing at a missing folder."""

    def enable_autostart(self, enabled: bool, description: str = "") -> None:
        """Whether the host should start this server on its own when it boots.

        description is the server's own display name. SystemdService's enable/disable
        never touches the unit file, so it has no use for this; WindowsTaskService has
        to rewrite the whole task definition to change the trigger, and without a name
        to put back it would overwrite the task's real description with its raw id.
        """

    # -------------------------------------------------------------------- control
    def start(self) -> None: ...

    def stop(self) -> None:
        """Ask for a clean shutdown, waiting for the world to be saved."""

    def kill(self) -> None: ...

    def kill_pid(self, pid: int) -> None:
        """Kill one process by id, whoever started it.

        For a server this app adopted rather than launched: it is not behind our unit
        or task, so kill() would be addressed to the wrong thing, but the pid found by
        foreign_process_pid() can still be acted on directly.
        """

    def send_console(self, line: str) -> None:
        """Type a line into the running server's console."""

    def take_warnings(self) -> list[str]:
        """Anything the last write_unit could not do, to be shown once in the console.

        Set-up steps that are worth attempting but not worth failing a start over --
        a firewall rule that needs privileges this account has not got, say -- end up
        here rather than being swallowed, so the user finds out from the server's own
        console instead of not at all.
        """

    # ---------------------------------------------------------------------- state
    def state(self) -> UnitState: ...

    def follow_console(self) -> Any:
        """A paramiko channel streaming the console log as it is written."""

    def read_console_tail(self, lines: int = 1000) -> str: ...

    def foreign_process_pid(self) -> int:
        """A server already running in this folder that this service did not start.

        Two JVMs on one world directory corrupt it. The service manager only knows about
        its own unit, so a server the far machine's own copy of this app launched as a
        plain child process is invisible to state() -- and is exactly the collision worth
        refusing. Returns 0 when the folder is free.
        """


def as_int(value: str | None) -> int:
    """A counter from a service manager, with the ways they spell "unset" folded to zero."""
    try:
        number = int(str(value or "0").strip() or "0")
    except ValueError:
        return 0
    # systemd reports an unset counter as the largest unsigned 64-bit value.
    return 0 if number < 0 or number >= 2**63 else number
