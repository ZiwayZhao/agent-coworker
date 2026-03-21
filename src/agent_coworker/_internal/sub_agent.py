"""AgentFax SubAgent — a lightweight agent spawned by a parent."""

import os
import queue
import threading
import uuid
from datetime import datetime, timezone

from .session import SessionManager


class SubAgent:
    """A sub-agent spawned by a parent agent to handle one collaboration."""

    def __init__(self, parent_agent, collab_id: str, name_suffix: str):
        self.parent = parent_agent
        self.collab_id = collab_id
        self.name_suffix = name_suffix
        self.name = f"{parent_agent.name}/sub-{name_suffix}"
        self.executor = parent_agent.executor

        sub_dir = os.path.join(parent_agent.data_dir, f"sub-{name_suffix}")
        os.makedirs(sub_dir, exist_ok=True)
        self.data_dir = sub_dir
        self.session_mgr = SessionManager(sub_dir)

        self._inbox: queue.Queue = queue.Queue()
        self._status: dict = {
            "name": self.name, "collab_id": collab_id,
            "status": "idle", "tasks_done": 0, "last_event": None,
        }
        self._stop = threading.Event()
        self._progress_callback = None

    def post_message(self, msg: dict):
        self._inbox.put(msg)

    def send(self, to: str, msg_type: str, payload: dict,
             correlation_id: str = None) -> dict:
        return self.parent.send(to, msg_type, payload, correlation_id=correlation_id)

    def set_progress_callback(self, fn):
        self._progress_callback = fn

    def report_progress(self, event: str, data: dict = None):
        self._status["last_event"] = event
        if data:
            self._status.update(data)
        if self._progress_callback:
            try:
                self._progress_callback(self, event, data or {})
            except Exception:
                pass

    def start_collab(self, peer_name: str, goal: str,
                     status_callback=None) -> "CollabOrchestrator":
        from .collab_orchestrator import CollabOrchestrator

        def _wrapped_callback(event: str, data: dict):
            self._status["status"] = "working" if event not in ("completed", "failed") else event
            if event == "task_done":
                self._status["tasks_done"] = self._status.get("tasks_done", 0) + 1
            self.report_progress(event, data)
            if status_callback:
                status_callback(event, data)

        self._status["status"] = "working"
        orch = CollabOrchestrator(self, peer_name, goal, status_callback=_wrapped_callback)
        with self.parent._orch_lock:
            self.parent._orchestrators[orch.collab_id] = orch
        orch.start()
        return orch

    def close(self):
        self._stop.set()
        self._status["status"] = "terminated"
        try:
            self.session_mgr.close()
        except Exception:
            pass
