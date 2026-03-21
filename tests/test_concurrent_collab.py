"""Tests for concurrent multi-session collaborations (Sprint 2).

Uses LocalCollabAgent with _SimpleBus (in-memory transport) — no real XMTP.
"""

import os
import sys
import time
import pytest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"
)
sys.path.insert(0, SCRIPTS_DIR)

from collab_orchestrator import LocalCollabAgent, _SimpleBus


def _make_agent(name, tmp_path, bus, skills_dict=None):
    """Helper: create a LocalCollabAgent with optional skills."""
    data_dir = str(tmp_path / name)
    os.makedirs(data_dir, exist_ok=True)

    def load_skills(executor):
        if skills_dict:
            for skill_name, func in skills_dict.items():
                executor.register_skill(skill_name, func,
                                        description=f"Test skill: {skill_name}")

    agent = LocalCollabAgent(name, data_dir, bus=bus, load_skills_fn=load_skills)
    return agent


def _search_skill(d):
    return {"results": [{"title": "Test", "snippet": "Test snippet"}],
            "query": d.get("query", ""), "count": 1}


def _summarize_skill(d):
    return {"summary": "Test summary", "key_points": ["Point 1", "Point 2"]}


def _write_report_skill(d):
    return {"report": "# Test Report\n\nTest content.", "word_count": 10, "sections": 2}


@pytest.mark.integration
class TestConcurrentCollab:

    def test_two_concurrent_collabs(self, tmp_path):
        """Alpha starts two concurrent CollabOrchestrators (to Beta and Gamma).

        Both should complete successfully within the timeout.
        """
        bus = _SimpleBus()

        alpha = _make_agent("alpha", tmp_path, bus,
                             {"web_search": _search_skill, "summarize": _summarize_skill})
        beta = _make_agent("beta", tmp_path, bus,
                            {"write_report": _write_report_skill})
        gamma = _make_agent("gamma", tmp_path, bus,
                             {"write_report": _write_report_skill})

        alpha.start_daemon()
        beta.start_daemon()
        gamma.start_daemon()

        try:
            # Start two concurrent collaborations from alpha
            collab_id_1 = alpha.start_collab("beta", "Research AI and write a report")
            collab_id_2 = alpha.start_collab("gamma", "Research ML and write a report")

            orch1 = alpha.get_orchestrator(collab_id_1)
            orch2 = alpha.get_orchestrator(collab_id_2)

            assert orch1 is not None, "Orchestrator 1 not found"
            assert orch2 is not None, "Orchestrator 2 not found"

            # Wait for both to finish
            status1 = orch1.wait(timeout=15)
            status2 = orch2.wait(timeout=15)

            assert status1 == "completed", \
                f"Collab 1 failed: status={status1}, error={orch1.error}"
            assert status2 == "completed", \
                f"Collab 2 failed: status={status2}, error={orch2.error}"

        finally:
            alpha.stop_daemon()
            beta.stop_daemon()
            gamma.stop_daemon()
            alpha.close()
            beta.close()
            gamma.close()

    def test_message_routing_by_session(self, tmp_path):
        """Messages with session_id route to the correct orchestrator."""
        bus = _SimpleBus()

        alpha = _make_agent("alpha2", tmp_path, bus,
                             {"web_search": _search_skill, "summarize": _summarize_skill})
        beta = _make_agent("beta2", tmp_path, bus,
                            {"write_report": _write_report_skill})
        gamma = _make_agent("gamma2", tmp_path, bus,
                             {"write_report": _write_report_skill})

        alpha.start_daemon()
        beta.start_daemon()
        gamma.start_daemon()

        try:
            collab_id_1 = alpha.start_collab("beta2", "Research topic A")
            collab_id_2 = alpha.start_collab("gamma2", "Research topic B")

            orch1 = alpha.get_orchestrator(collab_id_1)
            orch2 = alpha.get_orchestrator(collab_id_2)

            # Wait for orchestrators to reach session_active phase
            deadline = time.time() + 15
            while time.time() < deadline:
                if orch1.session_id and orch2.session_id:
                    break
                time.sleep(0.1)

            assert orch1.session_id is not None, "Orch1 never got a session"
            assert orch2.session_id is not None, "Orch2 never got a session"
            assert orch1.session_id != orch2.session_id, "Session IDs must differ"

            # collab_ids must be different
            assert collab_id_1 != collab_id_2

            # Verify routing: a message with orch1's session_id should go to orch1
            test_msg = {
                "type": "session_status",
                "payload": {"session_id": orch1.session_id},
                "correlation_id": "",
                "sender_id": "test",
            }
            assert orch1.owns_message(test_msg) is True
            assert orch2.owns_message(test_msg) is False

            # Wait for both to complete
            orch1.wait(timeout=15)
            orch2.wait(timeout=15)

        finally:
            alpha.stop_daemon()
            beta.stop_daemon()
            gamma.stop_daemon()
            alpha.close()
            beta.close()
            gamma.close()

    def test_concurrent_okr_execution(self, tmp_path):
        """Two OKRs run in parallel and both complete."""
        bus = _SimpleBus()

        alpha = _make_agent("alpha3", tmp_path, bus,
                             {"web_search": _search_skill, "summarize": _summarize_skill})
        beta = _make_agent("beta3", tmp_path, bus,
                            {"write_report": _write_report_skill})
        gamma = _make_agent("gamma3", tmp_path, bus,
                             {"write_report": _write_report_skill})

        alpha.start_daemon()
        beta.start_daemon()
        gamma.start_daemon()

        events_1 = []
        events_2 = []

        try:
            collab_id_1 = alpha.start_collab(
                "beta3", "Research distributed systems",
                status_callback=lambda ev, d: events_1.append(ev),
            )
            collab_id_2 = alpha.start_collab(
                "gamma3", "Research cloud computing",
                status_callback=lambda ev, d: events_2.append(ev),
            )

            orch1 = alpha.get_orchestrator(collab_id_1)
            orch2 = alpha.get_orchestrator(collab_id_2)

            status1 = orch1.wait(timeout=20)
            status2 = orch2.wait(timeout=20)

            assert status1 == "completed", \
                f"OKR 1 not completed: {status1}, error={orch1.error}"
            assert status2 == "completed", \
                f"OKR 2 not completed: {status2}, error={orch2.error}"

            # Both should have emitted at least started + completed events
            assert "started" in events_1
            assert "completed" in events_1
            assert "started" in events_2
            assert "completed" in events_2

        finally:
            alpha.stop_daemon()
            beta.stop_daemon()
            gamma.stop_daemon()
            alpha.close()
            beta.close()
            gamma.close()

    def test_session_capacity_respected(self, tmp_path):
        """has_capacity() on session manager reflects concurrent sessions."""
        bus = _SimpleBus()

        alpha = _make_agent("alpha4", tmp_path, bus,
                             {"web_search": _search_skill})
        beta = _make_agent("beta4", tmp_path, bus,
                            {"write_report": _write_report_skill})
        gamma = _make_agent("gamma4", tmp_path, bus,
                             {"write_report": _write_report_skill})

        alpha.start_daemon()
        beta.start_daemon()
        gamma.start_daemon()

        try:
            collab_id_1 = alpha.start_collab("beta4", "Research topic A")
            collab_id_2 = alpha.start_collab("gamma4", "Research topic B")

            orch1 = alpha.get_orchestrator(collab_id_1)
            orch2 = alpha.get_orchestrator(collab_id_2)

            status1 = orch1.wait(timeout=15)
            status2 = orch2.wait(timeout=15)

            assert status1 == "completed", \
                f"Orch1 not completed: {status1}, error={orch1.error}"
            assert status2 == "completed", \
                f"Orch2 not completed: {status2}, error={orch2.error}"

            # After both complete, sessions are in terminal states — capacity freed
            assert alpha.session_mgr.has_capacity(max_sessions=1) is True

        finally:
            alpha.stop_daemon()
            beta.stop_daemon()
            gamma.stop_daemon()
            alpha.close()
            beta.close()
            gamma.close()
