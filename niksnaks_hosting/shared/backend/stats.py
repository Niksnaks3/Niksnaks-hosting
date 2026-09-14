"""Live numbers about a running server: CPU, memory, uptime and ticks per second.

Shared by the Performance tab and the web panel so both read the same figures. One
ServerStats per process: psutil's cpu_percent is a delta since the previous call, and
the TPS value is pieced together from console lines, so neither survives being asked
by two readers each holding their own copy.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from niksnaks_hosting.shared.backend.server_process import ServerProcess

_BEHIND_RE = re.compile(r"Running (\d+)ms behind")
_TPS_RE = re.compile(r"TPS.*?(\d+\.?\d*)")


@dataclass
class StatsSample:
    status: str
    pid: int | None
    cpu_percent: float | None  # None when there is nothing to measure (remote host, no psutil)
    rss_mb: float | None
    uptime_s: float | None
    tps: float | None  # None for Bedrock, which never reports it
    ram_mb: int
    players: int
    max_players: int

    def to_dict(self) -> dict:
        return asdict(self)


def host_sample() -> dict | None:
    """CPU and memory use of this computer as a whole, for the panel's dashboard.

    None without psutil. The CPU figure is psutil's delta since the previous call, so
    the first answer after start-up is 0.
    """
    if not HAS_PSUTIL:
        return None
    try:
        memory = psutil.virtual_memory()
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "mem_percent": memory.percent,
            "mem_used_mb": (memory.total - memory.available) / (1024 * 1024),
            "mem_total_mb": memory.total / (1024 * 1024),
            "cpu_count": psutil.cpu_count() or 1,
        }
    except Exception:
        return None


class ServerStats:
    def __init__(self, process: ServerProcess):
        self._process = process
        self._psutil_process = None
        self._tps = 20.0
        self._cpu_count = (psutil.cpu_count() or 1) if HAS_PSUTIL else 1
        # The process outlives any one reader, so this handler is never disconnected.
        process.connect("output-received", self._on_output)

    @property
    def process(self) -> ServerProcess:
        return self._process

    def sample(self) -> StatsSample:
        proc = self._process
        pid = proc.pid if proc.is_running else None
        cpu = rss = uptime = None
        # A remote server's pid belongs to the other machine; psutil would measure a
        # stranger here, or nothing at all.
        if HAS_PSUTIL and pid and not proc.is_remote:
            try:
                if self._psutil_process is None or self._psutil_process.pid != pid:
                    self._psutil_process = psutil.Process(pid)
                cpu = self._psutil_process.cpu_percent(interval=None) / float(self._cpu_count)
                cpu = max(0.0, min(100.0, cpu))
                rss = self._psutil_process.memory_info().rss / (1024 * 1024)
                uptime = time.time() - self._psutil_process.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._psutil_process = None
        if not pid:
            self._psutil_process = None
        return StatsSample(
            status=proc.status,
            pid=pid,
            cpu_percent=cpu,
            rss_mb=rss,
            uptime_s=uptime,
            tps=None if proc.is_bedrock else self._tps,
            ram_mb=proc.ram_mb,
            players=proc.player_count,
            max_players=proc.max_players,
        )

    def _on_output(self, process, text: str) -> None:
        match = _BEHIND_RE.search(text)
        if match:
            tick_time = 50 + int(match.group(1)) / 20
            self._tps = min(20.0, 1000.0 / max(1, tick_time))
            return
        match = _TPS_RE.search(text)
        if match:
            try:
                self._tps = min(20.0, float(match.group(1)))
            except ValueError:
                pass
            return
        if "Done" in text and "For help" in text:
            self._tps = 20.0
