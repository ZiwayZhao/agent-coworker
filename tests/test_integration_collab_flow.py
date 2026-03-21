"""Integration tests: full collaboration flow using LocalTransport."""

import json
import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"
)
sys.path.insert(0, SCRIPTS_DIR)


@pytest.mark.integration
class TestCollabFlow:
    def test_discover_exchange(self, make_agent):
        """Alpha sends discover to Beta, Beta responds with capabilities."""
        alpha = make_agent(
            "alpha", {"web_search": lambda d: {"results": [], "count": 0}}
        )
        beta = make_agent(
            "beta",
            {"write_report": lambda d: {"report": "test", "word_count": 1}},
        )

        # Alpha discovers Beta
        alpha.send("beta", "discover", {"request": "capabilities"})
        msgs = beta.receive()
        assert len(msgs) == 1
        assert msgs[0]["type"] == "discover"

        # Beta responds with capabilities
        beta_caps = {"agent_id": "beta", "skills": beta.executor.list_skills()}
        beta.send("alpha", "capabilities", beta_caps)
        cap_msgs = alpha.receive()
        assert len(cap_msgs) == 1
        assert cap_msgs[0]["type"] == "capabilities"

    def test_session_propose_accept(self, make_agent):
        """Alpha proposes session, Beta accepts."""
        alpha = make_agent("alpha2", {})
        beta = make_agent("beta2", {})

        # Alpha creates session
        alpha_session_id = alpha.session_manager.create_session(
            peer_id="beta2",
            role="initiator",
            proposed_skills=["echo"],
            proposed_max_calls=5,
            ttl_seconds=3600,
        )
        alpha.send(
            "beta2",
            "session_propose",
            {
                "session_id": alpha_session_id,
                "proposed_skills": ["echo"],
                "proposed_trust_tier": 1,
                "proposed_max_context_privacy": "L1_PUBLIC",
                "proposed_max_calls": 5,
                "ttl_seconds": 3600,
            },
        )

        msgs = beta.receive()
        assert len(msgs) == 1
        assert msgs[0]["type"] == "session_propose"

        beta_session_id = beta.session_manager.create_session(
            peer_id="alpha2",
            role="responder",
            proposed_skills=["echo"],
            proposed_max_calls=5,
            ttl_seconds=3600,
            initiator_id="alpha2",
        )
        beta.session_manager.accept_session(
            beta_session_id,
            agreed_skills=["echo"],
            agreed_trust_tier=1,
            agreed_max_context_privacy="L1_PUBLIC",
            agreed_max_calls=5,
        )
        beta.send("alpha2", "session_accept", {"session_id": beta_session_id})

        acc_msgs = alpha.receive()
        assert len(acc_msgs) == 1
        assert acc_msgs[0]["type"] == "session_accept"

    def test_task_request_response(self, make_agent):
        """Alpha sends task_request, Beta executes and responds."""
        alpha = make_agent("req_alpha", {})
        beta = make_agent(
            "req_beta", {"echo": lambda d: {"echoed": d.get("text", "")}}
        )

        task_id = "task_test_001"
        alpha.send(
            "req_beta",
            "task_request",
            {"task_id": task_id, "skill": "echo", "input": {"text": "hello"}},
        )

        msgs = beta.receive()
        assert len(msgs) == 1
        assert msgs[0]["type"] == "task_request"
        assert msgs[0]["payload"]["task_id"] == task_id

        # Beta executes and responds
        result = beta.executor.execute("echo", {"text": "hello"})
        beta.send(
            "req_alpha",
            "task_response",
            {
                "task_id": task_id,
                "status": "completed",
                "output": result.get("result", {}),
            },
        )

        resp_msgs = alpha.receive()
        assert len(resp_msgs) == 1
        assert resp_msgs[0]["type"] == "task_response"
        assert resp_msgs[0]["payload"]["task_id"] == task_id
        assert resp_msgs[0]["payload"]["status"] == "completed"

    def test_full_collab_flow(self, tmp_data_dir):
        """Run the complete collab flow from agentfax_collab.run_collaboration()."""
        from agentfax_collab import (
            LocalAgent,
            LocalTransport,
            _load_researcher_skills,
            _load_writer_skills,
            run_collaboration,
        )

        transport = LocalTransport()
        alpha_dir = os.path.join(tmp_data_dir, "alpha-full")
        beta_dir = os.path.join(tmp_data_dir, "beta-full")

        alpha = LocalAgent(
            "researcher-alpha", alpha_dir, transport, _load_researcher_skills
        )
        beta = LocalAgent(
            "writer-beta", beta_dir, transport, _load_writer_skills
        )

        # Should complete without errors
        run_collaboration(alpha, beta, "Research AI agents and write a report")

        alpha.close()
        beta.close()

    def test_message_envelope_structure(self, make_agent):
        """Messages have required AgentFax protocol fields."""
        alpha = make_agent("env_alpha", {})
        beta = make_agent("env_beta", {})

        alpha.send("env_beta", "ping", {"message": "hello"})
        msgs = beta.receive()

        assert len(msgs) == 1
        msg = msgs[0]
        assert msg["protocol"] == "agentfax"
        assert msg["version"] == "1.0"
        assert msg["type"] == "ping"
        assert "timestamp" in msg
        assert "correlation_id" in msg
        assert msg["payload"]["message"] == "hello"

    def test_session_close(self, make_agent):
        """Session can be closed and marked completed."""
        alpha = make_agent("close_alpha", {})

        session_id = alpha.session_manager.create_session(
            peer_id="close_beta",
            role="initiator",
            proposed_skills=[],
            proposed_max_calls=1,
            ttl_seconds=3600,
        )
        alpha.session_manager.accept_session(
            session_id,
            agreed_skills=[],
            agreed_trust_tier=1,
            agreed_max_context_privacy="L1_PUBLIC",
            agreed_max_calls=1,
        )
        alpha.session_manager.close_session(session_id, "done")
        alpha.session_manager.complete_session(session_id)

        session = alpha.session_manager.get_session(session_id)
        assert session["state"] == "completed"
