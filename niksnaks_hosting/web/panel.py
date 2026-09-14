"""Starting and stopping the web panel's HTTP server inside the app."""

from __future__ import annotations

import socket
import threading
import time
from http.server import ThreadingHTTPServer

from niksnaks_hosting.shared.backend.server_manager import ServerManager
from niksnaks_hosting.web.api import JOB_TTL_S, Job, PanelHandler
from niksnaks_hosting.web.auth import SessionStore, UserStore
from niksnaks_hosting.web.stream import EventHub


class _Server(ThreadingHTTPServer):
    daemon_threads = True  # a browser still holding an event stream must not delay quitting
    allow_reuse_address = True
    panel: WebPanel


class WebPanel:
    def __init__(self, manager: ServerManager):
        self.manager = manager
        self.users = UserStore()
        self.sessions = SessionStore(self.users)
        self.hub = EventHub(manager)
        self.stopping = threading.Event()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._jobs: dict[str, Job] = {}
        self._jobs_lock = threading.Lock()
        self.port = 0

    @property
    def is_running(self) -> bool:
        return self._server is not None

    def start(self, port: int) -> tuple[bool, str]:
        """Listen on every interface. Says why when the port cannot be taken."""
        self.stop()
        try:
            server = _Server(("", int(port)), PanelHandler)
        except OSError as exc:
            return False, _("Port {} could not be opened: {}").format(port, exc.strerror or exc)
        server.panel = self
        self.stopping.clear()
        self._server = server
        self.port = int(port)
        self._thread = threading.Thread(target=server.serve_forever, daemon=True, name="web-panel")
        self._thread.start()
        return True, ""

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        self.stopping.set()
        server.shutdown()
        server.server_close()
        self._thread = None

    def url(self) -> str:
        return f"http://{lan_ip()}:{self.port}"

    def new_job(self) -> Job:
        job = Job()
        with self._jobs_lock:
            now = time.time()
            for jid, old in list(self._jobs.items()):
                if old.finished_at and now - old.finished_at > JOB_TTL_S:
                    del self._jobs[jid]
            self._jobs[job.id] = job
        return job

    def job(self, job_id: str) -> Job | None:
        with self._jobs_lock:
            return self._jobs.get(job_id)


def lan_ip() -> str:
    """The address other machines on the network reach this one by, as best we can tell."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))  # no packet is sent; this only picks the outbound interface
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    finally:
        if sock:
            sock.close()
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"
