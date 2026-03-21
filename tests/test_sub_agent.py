"""Tests for SubAgent (Sprint 3).

Tests spawn/lifecycle/skills of SubAgent using LocalCollabAgent.
"""

import os
import sys
import pytest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"
)
sys.path.insert(0, SCRIPTS_DIR)

from collab_orchestrator import LocalCollabAgent, _SimpleBus
from sub_agent import SubAgent


def _make_parent(name, tmp_path, bus=None, skills_dict=None):
    data_dir = str(tmp_path / name)
    os.makedirs(data_dir, exist_ok=True)

    def load_skills(executor):
        if skills_dict:
            for skill_name, func in skills_dict.items():
                executor.register_skill(skill_name, func,
                                        description=f"Test: {skill_name}")

    return LocalCollabAgent(name, data_dir, bus=bus or _SimpleBus(),
                            load_skills_fn=load_skills)


@pytest.mark.unit
class TestSubAgent:

    def test_sub_agent_inherits_skills(self, tmp_path):
        """Sub-agent shares the same executor instance as its parent."""
        parent = _make_parent("parent", tmp_path,
                              skills_dict={"echo": lambda d: {"echo": d}})
        sub = parent.spawn_sub("collab_001")

        # Same executor object
        assert sub.executor is parent.executor

        # Sub can list the same skills
        sub_skills = [s["name"] for s in sub.executor.list_skills()]
        parent_skills = [s["name"] for s in parent.executor.list_skills()]
        assert sub_skills == parent_skills

        parent.close()

    def test_sub_agent_own_session_db(self, tmp_path):
        """Sub-agent uses a separate SQLite database in a sub-directory."""
        parent = _make_parent("parent2", tmp_path)
        sub = parent.spawn_sub("collab_002")

        # Data directories must differ
        assert sub.data_dir != parent.data_dir

        # Sub's data_dir is a child of parent's data_dir
        assert sub.data_dir.startswith(parent.data_dir)

        # Sub-directory must exist
        assert os.path.isdir(sub.data_dir)

        # Sub session manager works independently
        sid = sub.session_mgr.create_session(
            peer_id="test-peer", role="initiator",
            proposed_skills=[], ttl_seconds=3600,
        )
        sub_session = sub.session_mgr.get_session(sid)
        assert sub_session is not None

        # Parent session manager should NOT see sub's session
        parent_session = parent.session_mgr.get_session(sid)
        assert parent_session is None

        parent.close()
        sub.close()

    def test_sub_agent_name_format(self, tmp_path):
        """Sub-agent name follows the {parent.name}/sub-{suffix} format."""
        parent = _make_parent("researcher-alpha", tmp_path)
        sub = parent.spawn_sub("collab_003")

        assert sub.name.startswith("researcher-alpha/sub-")
        assert len(sub.name) > len("researcher-alpha/sub-")

        # suffix is embedded in the name
        assert sub.name_suffix in sub.name

        parent.close()

    def test_sub_agent_executes_task(self, tmp_path):
        """Sub-agent can execute a skill via the shared executor."""
        parent = _make_parent("parent4", tmp_path,
                              skills_dict={"add": lambda d: {"sum": d["a"] + d["b"]}})
        sub = parent.spawn_sub("collab_004")

        result = sub.executor.execute("add", {"a": 3, "b": 4})
        assert result["success"] is True
        assert result["result"]["sum"] == 7

        parent.close()

    def test_parent_monitors_sub(self, tmp_path):
        """Progress callback fires when sub reports progress."""
        parent = _make_parent("parent5", tmp_path)
        sub = parent.spawn_sub("collab_005")

        received = []

        def my_callback(s, event, data):
            received.append({"sub": s.name, "event": event, "data": data})

        sub.set_progress_callback(my_callback)
        sub.report_progress("test_event", {"value": 42})

        assert len(received) == 1
        assert received[0]["event"] == "test_event"
        assert received[0]["data"]["value"] == 42
        assert received[0]["sub"] == sub.name

        parent.close()

    def test_sub_agent_cleanup(self, tmp_path):
        """terminate_sub() removes the sub-agent from the parent's registry."""
        parent = _make_parent("parent6", tmp_path)

        sub1 = parent.spawn_sub("collab_006a")
        sub2 = parent.spawn_sub("collab_006b")

        assert len(parent.list_subs()) == 2

        # Terminate sub1
        parent.terminate_sub(sub1.name)

        remaining = parent.list_subs()
        remaining_names = [s.name for s in remaining]
        assert len(remaining) == 1
        assert sub1.name not in remaining_names
        assert sub2.name in remaining_names

        # get_sub_status on removed sub returns empty dict
        assert parent.get_sub_status(sub1.name) == {}

        parent.close()

    def test_sub_agent_send_delegates_to_parent(self, tmp_path):
        """Sub-agent send() delegates to the parent's send() method."""
        bus = _SimpleBus()
        parent = _make_parent("parent7", tmp_path, bus=bus)
        # Register a peer on the bus
        bus.register("peer-x")

        sub = parent.spawn_sub("collab_007")

        # Sub sends a message via parent transport
        result = sub.send("peer-x", "ping", {"msg": "hello from sub"})
        assert result.get("ok") is True

        # Message should arrive in peer-x's queue
        msgs = bus.drain("peer-x")
        assert len(msgs) == 1
        assert msgs[0]["type"] == "ping"
        # sender_id from the sub's send goes through parent which uses parent.name
        # (LocalCollabAgent.send sets sender_id=self.name which is parent's name)

        parent.close()

    def test_sub_agent_status_tracking(self, tmp_path):
        """Sub-agent _status dict tracks name, collab_id, and status."""
        parent = _make_parent("parent8", tmp_path)
        sub = parent.spawn_sub("collab_008")

        status = parent.get_sub_status(sub.name)
        assert status["name"] == sub.name
        assert status["collab_id"] == "collab_008"
        assert status["status"] == "idle"

        # After a progress report, last_event should be updated
        sub.report_progress("working_on_task", {"skill": "search"})
        status2 = parent.get_sub_status(sub.name)
        assert status2["last_event"] == "working_on_task"

        parent.close()

    def test_sub_agent_full_collab(self, tmp_path):
        """Sub-agent can run a full CollabOrchestrator flow."""
        bus = _SimpleBus()

        parent = _make_parent("parent9", tmp_path, bus=bus,
                               skills_dict={
                                   "web_search": lambda d: {
                                       "results": [{"title": "T", "snippet": "S"}],
                                       "query": d.get("query", ""),
                                       "count": 1,
                                   },
                                   "summarize": lambda d: {
                                       "summary": "Summary",
                                       "key_points": ["K1"],
                                   },
                               })
        peer = _make_parent("peer9", tmp_path, bus=bus,
                             skills_dict={
                                 "write_report": lambda d: {
                                     "report": "# Report\n\nContent.",
                                     "word_count": 5,
                                     "sections": 2,
                                 }
                             })

        parent.start_daemon()
        peer.start_daemon()

        try:
            sub = parent.spawn_sub("collab_009")
            orch = sub.start_collab("peer9", "Research AI and write report")

            status = orch.wait(timeout=20)
            assert status == "completed", \
                f"Sub-agent collab failed: {status}, error={orch.error}"

        finally:
            parent.stop_daemon()
            peer.stop_daemon()
            parent.close()
            peer.close()
