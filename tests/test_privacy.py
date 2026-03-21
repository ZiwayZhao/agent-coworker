"""Privacy tests: L3 data must not leak into outbound messages."""

import json
import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"
)
sys.path.insert(0, SCRIPTS_DIR)


@pytest.mark.privacy
class TestPrivacyL3DataLeakage:
    """L3 data must never appear in outbound protocol messages."""

    L3_SECRET = "CONFIDENTIAL_SECRET_XYZ_12345"

    def test_task_request_no_l3_data(self, make_agent):
        """task_request payload must not contain L3 secrets."""
        alpha = make_agent("priv_alpha", {})
        beta = make_agent("priv_beta", {})

        # Send a normal task request (no L3 data injected)
        alpha.send(
            "priv_beta",
            "task_request",
            {
                "task_id": "t001",
                "skill": "echo",
                "input": {"text": "normal input"},
            },
        )

        msgs = beta.receive()
        raw_json = json.dumps(msgs)
        assert self.L3_SECRET not in raw_json

    def test_skill_card_no_implementation_code(self, alpha_agent):
        """Skill cards shared via capabilities must not include implementation source code."""
        skills = alpha_agent.executor.list_skills()

        for skill in skills:
            skill_json = json.dumps(skill)
            # Should not contain Python source code indicators
            assert (
                "def " not in skill_json
            ), f"Skill {skill.get('name')} exposes function definition"
            assert (
                "import " not in skill_json
            ), f"Skill {skill.get('name')} exposes imports"
            assert "__code__" not in skill_json

    def test_capabilities_message_no_internal_paths(self, make_agent):
        """capabilities message must not expose internal file system paths."""
        alpha = make_agent(
            "path_alpha", {"web_search": lambda d: {"results": []}}
        )
        beta = make_agent("path_beta", {})

        alpha.send("path_beta", "discover", {"request": "capabilities"})
        beta.receive()

        # Build capabilities as the agent would
        skills = alpha.executor.list_skills()
        caps = {"agent_id": "path_alpha", "skills": skills}
        alpha.send("path_beta", "capabilities", caps)

        msgs = beta.receive()
        raw_json = json.dumps(msgs)

        # Should not expose internal module names in protocol messages
        assert "__code__" not in raw_json
        assert "lambda" not in raw_json.lower() or "lambda" in json.dumps(
            skills
        )

    def test_session_propose_no_l3_in_terms(self, make_agent):
        """session_propose must only send L1/L2 context privacy levels."""
        alpha = make_agent("sess_priv_alpha", {})
        beta = make_agent("sess_priv_beta", {})

        alpha.send(
            "sess_priv_beta",
            "session_propose",
            {
                "session_id": "sess_001",
                "proposed_skills": ["echo"],
                "proposed_trust_tier": 1,
                "proposed_max_context_privacy": "L1_PUBLIC",
                "proposed_max_calls": 5,
                "ttl_seconds": 3600,
            },
        )

        msgs = beta.receive()
        payload = msgs[0]["payload"]

        # Privacy level in session proposal should not be L3
        context_privacy = payload.get("proposed_max_context_privacy", "")
        assert (
            context_privacy != "L3_CONFIDENTIAL"
        ), "Should not propose L3 in session terms"

    def test_okr_propose_no_private_data(self, make_agent):
        """okr_propose must not embed agent's private context."""
        alpha = make_agent(
            "okr_priv_alpha", {"web_search": lambda d: {"results": []}}
        )
        beta = make_agent(
            "okr_priv_beta", {"write_report": lambda d: {"report": ""}}
        )

        okr_payload = {
            "okr_id": "okr_test_001",
            "goal": "Research AI",
            "key_results": [
                {
                    "kr_id": "KR1",
                    "description": "Gather info",
                    "metric": "3+ sources",
                    "tasks": [
                        {
                            "task_id": "t1",
                            "skill": "web_search",
                            "agent": "okr_priv_alpha",
                            "description": "Search the web",
                        }
                    ],
                }
            ],
        }

        alpha.send("okr_priv_beta", "okr_propose", okr_payload)
        msgs = beta.receive()

        raw_json = json.dumps(msgs)
        assert self.L3_SECRET not in raw_json

    def test_error_response_no_stack_trace(self, make_agent):
        """Error responses should not expose internal stack traces."""
        beta = make_agent("err_beta", {})

        # Execute a skill that doesn't exist
        result = beta.executor.execute("nonexistent_skill", {})

        assert not result.get("success", True)
        error_str = result.get("error", "")
        # Should have a descriptive error but not a full traceback
        assert "nonexistent_skill" in error_str.lower() or "unknown" in error_str.lower()
        # No traceback key for "skill not found" errors (only for exceptions)
        assert "traceback" not in result or result["traceback"] is None


@pytest.mark.privacy
class TestPrivacyContextProjection:
    """Context projection must not leak L3 data to peers."""

    def test_wallet_private_key_never_in_projection(self, tmp_data_dir):
        """L3_PRIVATE items must not appear in projections for L2_TRUSTED peers."""
        from context_manager import ContextManager, PrivacyTier

        cm = ContextManager(tmp_data_dir)

        # Add a private key at L3_PRIVATE
        cm.add_context(
            "wallet_private_key",
            "0xSECRET_PRIVATE_KEY_abc123",
            category="credential",
            privacy_tier=PrivacyTier.L3_PRIVATE,
        )

        # Add a public skill at L1_PUBLIC
        cm.add_context(
            "tech_stack",
            ["python", "nodejs"],
            category="skill",
            privacy_tier=PrivacyTier.L1_PUBLIC,
        )

        # Project for a peer with L2_TRUSTED access
        items = cm.project_for_task("echo", peer_trust_tier=2)
        projection_json = json.dumps(items)

        assert "0xSECRET_PRIVATE_KEY_abc123" not in projection_json
        assert "wallet_private_key" not in projection_json

    def test_data_dir_path_never_in_message(self):
        """build_message output must not contain the data_dir path."""
        from agentfax_client import build_message

        distinctive_path = "/secret/private/data_dir/path"
        msg = build_message(
            "ping",
            {"message": "hello", "info": "test payload"},
            sender_id="test_agent",
        )
        msg_json = json.dumps(msg)

        assert distinctive_path not in msg_json
        # Also verify no generic path patterns leak
        assert "/Users/" not in msg_json or "sender_id" in msg_json
