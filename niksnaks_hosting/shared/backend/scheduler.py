from __future__ import annotations

import threading
from datetime import datetime

CHECK_EVERY_S = 20


class DailyScheduler:
    def __init__(self, manager):
        self._manager = manager
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._done_on: dict[str, str] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="daily-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(CHECK_EVERY_S):
            try:
                self.tick(datetime.now())
            except Exception as e:
                print(f"Daily scheduler failed: {e}")

    def tick(self, now: datetime) -> list[str]:
        today, minute = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        started = []
        for info in self._manager.servers:
            if not info.daily_restart and info.daily_backup == "off":
                continue
            if info.daily_time != minute or self._done_on.get(info.id) == today:
                continue
            self._done_on[info.id] = today
            started.append(info.id)
            threading.Thread(target=self._run, args=(info.id,), daemon=True, name=f"daily-{info.id}").start()
        return started

    def _run(self, server_id: str) -> None:
        ok, message = self._manager.run_daily_task(server_id)
        if message:
            self._manager.emit_on_main_thread("daily-task-done", server_id, ok, message)
