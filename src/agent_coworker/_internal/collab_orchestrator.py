"""AgentFax CollabOrchestrator — manages one full collaboration lifecycle."""

import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone

from .client import build_message
from .executor import TaskExecutor
from .session import SessionManager


def _build_task_input(skill: str, goal: str, results: dict, step_index: int = 0) -> dict:
    if step_index == 0 or not results:
        if skill in ("web_search", "search", "research", "crawl", "fetch"):
            return {"query": goal, "max_results": 5}
        return {"text": goal}

    last_result = list(results.values())[-1] if results else {}

    if skill == "summarize":
        return {
            "results": last_result.get("results", []),
            "text": json.dumps(last_result)[:500], "max_words": 150,
        }
    if skill in ("write_report", "write", "compose", "draft", "generate", "create"):
        return {
            "summary": last_result.get("summary", ""),
            "key_points": last_result.get("key_points", []), "topic": goal,
        }
    if skill in ("translate", "format", "publish", "export"):
        return {
            "text": last_result.get("report", last_result.get("text", json.dumps(last_result)[:500])),
            "target_language": "Chinese",
        }
    return last_result


class CollabOrchestrator:
    """Manages one full collaboration lifecycle (initiator role)."""

    def __init__(self, agent, peer_addr: str, goal: str, peer_name: str = None,
                 collab_id: str = None, status_callback=None):
        self.agent = agent
        self.peer_addr = peer_addr
        self.peer_name = peer_name or peer_addr
        self.goal = goal
        self.collab_id = collab_id or f"collab_{uuid.uuid4().hex[:12]}"
        self.status_callback = status_callback or (lambda event, data: None)

        self.session_id: str = None
        self.status: str = "pending"
        self.result: dict = {}
        self.error: str = None

        self._inbox: queue.Queue = queue.Queue()
        self._thread: threading.Thread = None
        self._stop = threading.Event()

    def post_message(self, msg: dict):
        self._inbox.put(msg)

    def owns_message(self, msg: dict) -> bool:
        cid = msg.get("correlation_id", "") or ""
        if cid.startswith(self.collab_id):
            return True
        if self.session_id:
            sid = (msg.get("payload") or {}).get("session_id", "")
            if sid and sid == self.session_id:
                return True
        return False

    def start(self) -> "CollabOrchestrator":
        self._thread = threading.Thread(target=self._run, daemon=True,
                                         name=f"orch-{self.collab_id[:12]}")
        self._thread.start()
        return self

    def wait(self, timeout: float = 120) -> str:
        if self._thread:
            self._thread.join(timeout)
        return self.status

    def cancel(self):
        self._stop.set()
        self.status = "cancelled"

    @property
    def _session_mgr(self) -> SessionManager:
        return getattr(self.agent, "session_mgr", getattr(self.agent, "session_manager", None))

    def _send(self, msg_type: str, payload: dict) -> str:
        cid = f"{self.collab_id}_{msg_type}_{uuid.uuid4().hex[:8]}"
        self.agent.send(self.peer_addr, msg_type, payload, correlation_id=cid)
        return cid

    def _wait_for(self, msg_type: str, timeout: float = 30,
                  correlation_id: str = None) -> dict:
        deadline = time.time() + timeout
        pending = []
        while time.time() < deadline and not self._stop.is_set():
            remaining = max(0.01, deadline - time.time())
            try:
                msg = self._inbox.get(timeout=min(1.0, remaining))
                if msg.get("type") == msg_type:
                    if correlation_id is None or msg.get("correlation_id") == correlation_id:
                        for m in pending:
                            self._inbox.put(m)
                        return msg
                    else:
                        pending.append(msg)
                else:
                    pending.append(msg)
            except queue.Empty:
                pass
        for m in pending:
            self._inbox.put(m)
        return None

    def _report(self, event: str, data: dict = None):
        data = dict(data or {})
        data.update({
            "collab_id": self.collab_id, "status": self.status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        try:
            self.status_callback(event, data)
        except Exception:
            pass

    def _discover(self):
        self._send("discover", {"request": "capabilities"})
        resp = self._wait_for("capabilities", timeout=15)
        if not resp:
            return None
        return resp.get("payload", {}).get("skills", [])

    def _negotiate_okr(self, peer_skills: list):
        from .okr import build_okr, build_okr_propose
        my_skills = getattr(self.agent, "skills", [])
        okr = build_okr(self.goal, self.agent.name, my_skills, self.peer_name, peer_skills)
        payload = build_okr_propose(okr)
        self._send("okr_propose", payload)
        resp = self._wait_for("okr_accept", timeout=20)
        if resp:
            return okr
        rej = self._wait_for("okr_reject", timeout=5)
        if rej:
            self.error = f"OKR rejected: {rej.get('payload', {}).get('reason', 'unknown')}"
        return None

    def _establish_session(self, okr: dict):
        from .okr import get_flat_tasks
        tasks = get_flat_tasks(okr)
        session_skills = list({t["skill"] for t in tasks})
        max_calls = len(tasks) + 2

        sm = self._session_mgr
        sid = sm.create_session(
            peer_id=self.peer_addr, role="initiator",
            proposed_skills=session_skills, proposed_max_calls=max_calls, ttl_seconds=3600,
        )
        self._send("session_propose", {
            "session_id": sid, "proposed_skills": session_skills,
            "proposed_trust_tier": 1, "proposed_max_context_privacy": "L1_PUBLIC",
            "proposed_max_calls": max_calls, "ttl_seconds": 3600,
        })
        resp = self._wait_for("session_accept", timeout=15)
        if resp:
            sm.accept_session(sid, agreed_skills=session_skills, agreed_trust_tier=1,
                              agreed_max_context_privacy="L1_PUBLIC", agreed_max_calls=max_calls)
            return sid
        return None

    def _execute_tasks(self, okr: dict, session_id: str) -> dict:
        from .okr import get_flat_tasks, update_task_status
        tasks = get_flat_tasks(okr)
        results = {}
        for i, task in enumerate(tasks):
            if self._stop.is_set():
                break
            skill = task["skill"]
            agent_name = task["agent"]
            is_local = (agent_name == self.agent.name)
            input_data = _build_task_input(skill, self.goal, results, step_index=i)

            if is_local:
                exec_result = self.agent.executor.execute(skill, input_data)
                if exec_result.get("success"):
                    results[f"step_{i}"] = exec_result["result"]
                    update_task_status(okr, task["task_id"], "completed", exec_result.get("duration_ms"))
                else:
                    update_task_status(okr, task["task_id"], "failed")
            else:
                task_id = f"{self.collab_id}_task_{uuid.uuid4().hex[:8]}"
                cid = f"{self.collab_id}_task_{task_id}"
                self.agent.send(self.peer_addr, "task_request", {
                    "task_id": task_id, "skill": skill, "input": input_data,
                    "session_id": session_id,
                }, correlation_id=cid)
                resp = self._wait_for("task_response", timeout=30)
                if resp:
                    output = resp.get("payload", {}).get("output", {})
                    results[f"step_{i}"] = output
                    update_task_status(okr, task["task_id"], "completed")
                else:
                    update_task_status(okr, task["task_id"], "failed")
            self._report("task_done", {"task_index": i, "skill": skill})
        return results

    def _close_session(self, session_id: str):
        sm = self._session_mgr
        sm.close_session(session_id, "collaboration complete")
        try:
            sm.complete_session(session_id)
        except Exception:
            pass
        self._send("session_close", {"session_id": session_id, "reason": "done"})

    def _run(self):
        self.status = "running"
        self._report("started")
        try:
            peer_skills = self._discover()
            if peer_skills is None:
                self.status = "failed"
                self.error = self.error or "Discover timeout"
                self._report("failed", {"error": self.error})
                return
            self._report("discovered", {"peer_skills": [
                s["name"] if isinstance(s, dict) else s for s in peer_skills
            ]})

            okr = self._negotiate_okr(peer_skills)
            if okr is None:
                self.status = "failed"
                self.error = self.error or "OKR negotiation failed"
                self._report("failed", {"error": self.error})
                return
            self._report("okr_agreed", {"okr_id": okr.get("okr_id")})

            session_id = self._establish_session(okr)
            if session_id is None:
                self.status = "failed"
                self.error = self.error or "Session not established"
                self._report("failed", {"error": self.error})
                return
            self.session_id = session_id
            self._report("session_active", {"session_id": session_id})

            results = self._execute_tasks(okr, session_id)
            self._close_session(session_id)

            self.status = "completed"
            self.result = results
            self._report("completed", {"result_keys": list(results.keys())})
        except Exception as exc:
            self.status = "failed"
            self.error = str(exc)
            self._report("failed", {"error": str(exc)})
