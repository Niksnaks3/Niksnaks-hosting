"""Console lines and status changes, fanned out to browsers as Server-Sent Events.

The backend emits its signals on the GTK main thread. A handler here must therefore
do nothing but drop a line in a queue and return; the HTTP thread serving the browser
takes it from there. A browser that has fallen far behind loses old lines rather than
stalling the app.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterator

from niksnaks_hosting.shared.backend.server_manager import ServerManager

QUEUE_LINES = 500
KEEPALIVE_S = 15.0


class EventHub:
    def __init__(self, manager: ServerManager):
        self._manager = manager
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[queue.Queue]] = {}
        self._handlers: dict[str, tuple[object, list[int]]] = {}  # server_id -> (process, handler ids)
        manager.connect("server-changed", lambda *_a: self._broadcast_all({"event": "server-changed"}))
        manager.connect("server-removed", lambda *_a: self._broadcast_all({"event": "server-changed"}))
        manager.connect("server-added", lambda *_a: self._broadcast_all({"event": "server-changed"}))

    def subscribe(self, server_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=QUEUE_LINES)
        with self._lock:
            self._subscribers.setdefault(server_id, []).append(q)
            self._attach(server_id)
        return q

    def unsubscribe(self, server_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(server_id, [])
            if q in subs:
                subs.remove(q)
            if not subs:
                self._subscribers.pop(server_id, None)
                self._detach(server_id)

    def _attach(self, server_id: str) -> None:
        process = self._manager.get_process(server_id)
        if process is None:
            return
        held = self._handlers.get(server_id)
        if held and held[0] is process:
            return
        if held:
            self._detach(server_id)
        def on_output(_p, text):
            self._push(server_id, {"event": "output", "text": text})

        def on_status(_p, status):
            self._push(server_id, {"event": "status", "status": status})

        def on_players(_p, count, maximum):
            self._push(server_id, {"event": "players", "players": count, "max": maximum})

        ids = [
            process.connect("output-received", on_output),
            process.connect("status-changed", on_status),
            process.connect("players-changed", on_players),
        ]
        self._handlers[server_id] = (process, ids)

    def _detach(self, server_id: str) -> None:
        held = self._handlers.pop(server_id, None)
        if not held:
            return
        process, ids = held
        for handler_id in ids:
            try:
                process.disconnect(handler_id)
            except Exception:
                pass

    def _push(self, server_id: str, payload: dict) -> None:
        line = json.dumps(payload)
        with self._lock:
            subs = list(self._subscribers.get(server_id, []))
        for q in subs:
            _offer(q, line)

    def _broadcast_all(self, payload: dict) -> None:
        line = json.dumps(payload)
        with self._lock:
            queues = [q for subs in self._subscribers.values() for q in subs]
        for q in queues:
            _offer(q, line)

    def stream(self, server_id: str, stop: threading.Event) -> Iterator[bytes]:
        """SSE frames for one browser; yields a comment every 15 s so proxies keep the line open."""
        q = self.subscribe(server_id)
        try:
            yield b"retry: 3000\n\n"
            while not stop.is_set():
                try:
                    line = q.get(timeout=KEEPALIVE_S)
                except queue.Empty:
                    yield b": keepalive\n\n"
                    continue
                yield f"data: {line}\n\n".encode()
        finally:
            self.unsubscribe(server_id, q)


def _offer(q: queue.Queue, line: str) -> None:
    try:
        q.put_nowait(line)
    except queue.Full:
        try:
            q.get_nowait()
            q.put_nowait(line)
        except (queue.Empty, queue.Full):
            pass
