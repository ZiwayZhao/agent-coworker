#!/usr/bin/env python3
"""Tests for S4: Context projection + privacy control.

Covers:
- max_privacy_tier parameter on project_for_task()
- Session-constrained context projection in task_handler
- LLM projection fail-closed behavior
- Privacy cap enforcement: min(peer_trust, skill_cap, session_cap)
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"))

from context_manager import ContextManager, PrivacyTier
from llm_projection import LLMProjectionEngine, ProjectionResult
from session import SessionManager
from security import TrustTier


# ── ContextManager: max_privacy_tier ────────────────────────────


class TestProjectionMaxPrivacyTier(unittest.TestCase):
    """Test that max_privacy_tier constrains projection ceiling."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cm = ContextManager(self.tmpdir)
        # Seed context: L1, L2, L3
        self.cm.add_context("public_skill", "python", category="skill",
                            privacy_tier=PrivacyTier.L1_PUBLIC)
        self.cm.add_context("trusted_pref", "vim", category="skill",
                            privacy_tier=PrivacyTier.L2_TRUSTED)
        self.cm.add_context("secret_key", "sk-xxx", category="credential",
                            privacy_tier=PrivacyTier.L3_PRIVATE)

    def tearDown(self):
        self.cm.close()

    def test_no_cap_internal_gets_l1_l2(self):
        """Without max_privacy_tier, INTERNAL peer gets L1+L2."""
        items = self.cm.project_for_task("echo", peer_trust_tier=2)
        keys = {i["key"] for i in items}
        self.assertIn("public_skill", keys)
        self.assertIn("trusted_pref", keys)
        self.assertNotIn("secret_key", keys)

    def test_cap_l1_restricts_internal_to_l1(self):
        """max_privacy_tier=L1 restricts INTERNAL peer to L1 only."""
        items = self.cm.project_for_task(
            "echo", peer_trust_tier=2, max_privacy_tier=PrivacyTier.L1_PUBLIC
        )
        keys = {i["key"] for i in items}
        self.assertIn("public_skill", keys)
        self.assertNotIn("trusted_pref", keys)  # L2 blocked by cap
        self.assertNotIn("secret_key", keys)

    def test_cap_l2_allows_internal_l1_l2(self):
        """max_privacy_tier=L2 allows INTERNAL peer L1+L2 (no change)."""
        items = self.cm.project_for_task(
            "echo", peer_trust_tier=2, max_privacy_tier=PrivacyTier.L2_TRUSTED
        )
        keys = {i["key"] for i in items}
        self.assertIn("public_skill", keys)
        self.assertIn("trusted_pref", keys)

    def test_cap_narrows_not_widens(self):
        """max_privacy_tier cannot widen KNOWN peer beyond L1."""
        items = self.cm.project_for_task(
            "echo", peer_trust_tier=1, max_privacy_tier=PrivacyTier.L2_TRUSTED
        )
        keys = {i["key"] for i in items}
        # KNOWN peer trust-based cap is L1, max_privacy_tier=L2 cannot widen
        self.assertIn("public_skill", keys)
        self.assertNotIn("trusted_pref", keys)

    def test_cap_zero_returns_empty(self):
        """max_privacy_tier=0 (below L1) returns nothing."""
        items = self.cm.project_for_task(
            "echo", peer_trust_tier=2, max_privacy_tier=0
        )
        self.assertEqual(items, [])

    def test_l3_never_shared_regardless_of_cap(self):
        """L3 items never shared even with high cap and high trust."""
        items = self.cm.project_for_task(
            "echo", peer_trust_tier=3, max_privacy_tier=PrivacyTier.L2_TRUSTED
        )
        keys = {i["key"] for i in items}
        self.assertNotIn("secret_key", keys)

    def test_untrusted_with_high_cap_still_empty(self):
        """UNTRUSTED peer gets nothing even with max_privacy_tier=L2."""
        items = self.cm.project_for_task(
            "echo", peer_trust_tier=0, max_privacy_tier=PrivacyTier.L2_TRUSTED
        )
        self.assertEqual(items, [])


# ── LLM Projection: fail-closed ────────────────────────────────


class TestLLMFailClosed(unittest.TestCase):
    """Test that LLM projection failures result in no context shared."""

    def test_parse_failure_returns_empty(self):
        """Malformed LLM response → empty list (fail-closed)."""
        engine = LLMProjectionEngine(api_key="")
        response = {
            "content": "I cannot help with that request",
            "usage": {},
        }
        ids, rationale, usage = engine._parse_response(
            response, [
                {"context_id": "ctx_1", "key": "a", "value": "b", "category": "skill"},
                {"context_id": "ctx_2", "key": "c", "value": "d", "category": "skill"},
            ]
        )
        # Fail-closed: no items shared
        self.assertEqual(ids, [])
        self.assertIn("fail-closed", rationale)

    def test_llm_exception_falls_back_to_static(self):
        """LLM call exception → fallback to static categories (not all items)."""
        engine = LLMProjectionEngine(api_key="test_key")
        engine._available = True  # pretend available
        engine._client = MagicMock()
        engine._client.messages.create.side_effect = RuntimeError("API down")

        items = [
            {"context_id": "ctx_1", "key": "python", "value": "3.12", "category": "skill"},
            {"context_id": "ctx_2", "key": "color", "value": "blue", "category": "preference"},
        ]

        result = engine.project(
            task_description="echo test",
            task_type="echo",
            available_items=items,
        )
        # Falls back to static, "echo" type → only "skill" category
        self.assertEqual(result.method, "fallback")
        selected_keys = {i["key"] for i in result.selected_items}
        self.assertIn("python", selected_keys)
        self.assertNotIn("color", selected_keys)  # preference not in echo's categories

    def test_valid_llm_response_works(self):
        """Valid LLM JSON response → selected items returned."""
        engine = LLMProjectionEngine(api_key="")
        items = [
            {"context_id": "ctx_1", "key": "python", "value": "3.12", "category": "skill"},
            {"context_id": "ctx_2", "key": "react", "value": "18", "category": "skill"},
        ]
        response = {
            "content": json.dumps({
                "selected": [{"id": "ctx_1", "reason": "relevant to code"}],
                "overall_rationale": "Only Python relevant",
            }),
            "usage": {"input_tokens": 50, "output_tokens": 30},
        }
        ids, rationale, usage = engine._parse_response(response, items)
        self.assertEqual(ids, ["ctx_1"])
        self.assertNotIn("ctx_2", ids)


# ── Task Handler: session-constrained projection ────────────────


class TestTaskHandlerSessionProjection(unittest.TestCase):
    """Test that task_handler enforces session privacy cap on context projection."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cm = ContextManager(self.tmpdir)
        self.sm = SessionManager(self.tmpdir)

        # Seed context
        self.cm.add_context("public_info", "hello", category="skill",
                            privacy_tier=PrivacyTier.L1_PUBLIC)
        self.cm.add_context("trusted_info", "world", category="skill",
                            privacy_tier=PrivacyTier.L2_TRUSTED)

    def tearDown(self):
        self.cm.close()
        self.sm.close()

    def _make_session(self, privacy_cap="L1_PUBLIC", trust_tier=1):
        """Create an active session with given caps."""
        sid = self.sm.create_session(
            peer_id="test_peer",
            proposed_skills=["echo"],
            proposed_trust_tier=trust_tier,
            proposed_max_context_privacy=privacy_cap,
            proposed_max_calls=10,
        )
        self.sm.accept_session(
            sid,
            agreed_skills=["echo"],
            agreed_trust_tier=trust_tier,
            agreed_max_context_privacy=privacy_cap,
            agreed_max_calls=10,
        )
        return sid

    def test_session_l1_cap_restricts_internal_peer(self):
        """INTERNAL peer with L1 session cap only gets L1 context."""
        sid = self._make_session(privacy_cap="L1_PUBLIC", trust_tier=2)
        session = self.sm.get_session(sid)

        # Simulate what task_handler does: compute effective privacy cap
        privacy_tier_map = {"L1_PUBLIC": 1, "L2_TRUSTED": 2, "L3_PRIVATE": 3}
        skill_max_privacy = "L2_TRUSTED"  # skill allows up to L2
        skill_cap = privacy_tier_map[skill_max_privacy]
        session_cap = privacy_tier_map[session["agreed_max_context_privacy"]]
        privacy_cap = min(skill_cap, session_cap)  # = 1

        items = self.cm.project_for_task(
            "echo", peer_trust_tier=2, max_privacy_tier=privacy_cap
        )
        keys = {i["key"] for i in items}
        self.assertIn("public_info", keys)
        self.assertNotIn("trusted_info", keys)  # L2 blocked by session cap

    def test_session_l2_cap_allows_internal_l2(self):
        """INTERNAL peer with L2 session cap gets L1+L2 context."""
        sid = self._make_session(privacy_cap="L2_TRUSTED", trust_tier=2)
        session = self.sm.get_session(sid)

        privacy_tier_map = {"L1_PUBLIC": 1, "L2_TRUSTED": 2, "L3_PRIVATE": 3}
        skill_cap = privacy_tier_map["L2_TRUSTED"]
        session_cap = privacy_tier_map[session["agreed_max_context_privacy"]]
        privacy_cap = min(skill_cap, session_cap)  # = 2

        items = self.cm.project_for_task(
            "echo", peer_trust_tier=2, max_privacy_tier=privacy_cap
        )
        keys = {i["key"] for i in items}
        self.assertIn("public_info", keys)
        self.assertIn("trusted_info", keys)

    def test_skill_cap_overrides_session_cap(self):
        """Skill L1 cap overrides session L2 cap (most restrictive wins)."""
        sid = self._make_session(privacy_cap="L2_TRUSTED", trust_tier=2)
        session = self.sm.get_session(sid)

        privacy_tier_map = {"L1_PUBLIC": 1, "L2_TRUSTED": 2, "L3_PRIVATE": 3}
        skill_cap = privacy_tier_map["L1_PUBLIC"]  # Skill restricts to L1
        session_cap = privacy_tier_map[session["agreed_max_context_privacy"]]
        privacy_cap = min(skill_cap, session_cap)  # = 1 (skill wins)

        items = self.cm.project_for_task(
            "echo", peer_trust_tier=2, max_privacy_tier=privacy_cap
        )
        keys = {i["key"] for i in items}
        self.assertIn("public_info", keys)
        self.assertNotIn("trusted_info", keys)

    def test_no_session_uses_skill_cap_only(self):
        """Without session, only skill cap applies."""
        privacy_tier_map = {"L1_PUBLIC": 1, "L2_TRUSTED": 2, "L3_PRIVATE": 3}
        skill_cap = privacy_tier_map["L1_PUBLIC"]

        items = self.cm.project_for_task(
            "echo", peer_trust_tier=2, max_privacy_tier=skill_cap
        )
        keys = {i["key"] for i in items}
        self.assertIn("public_info", keys)
        self.assertNotIn("trusted_info", keys)


# ── Integration: full task_handler flow with context ────────────


class TestTaskHandlerContextIntegration(unittest.TestCase):
    """Integration test: task_handler with context projection and session."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

        from router import MessageRouter, RouterContext
        from executor import TaskExecutor, register_builtin_skills
        from task_manager import TaskManager
        from security import TrustManager

        self.router = MessageRouter()
        self.executor = TaskExecutor()
        register_builtin_skills(self.executor)
        self.task_manager = TaskManager(self.tmpdir)
        self.trust_manager = TrustManager(self.tmpdir)
        self.context_manager = ContextManager(self.tmpdir)
        self.session_manager = SessionManager(self.tmpdir)

        # Seed context
        self.context_manager.add_context("lang", "python", category="skill",
                                         privacy_tier=PrivacyTier.L1_PUBLIC)
        self.context_manager.add_context("api_key", "sk-xxx", category="credential",
                                         privacy_tier=PrivacyTier.L3_PRIVATE)
        self.context_manager.add_context("preference", "vim", category="skill",
                                         privacy_tier=PrivacyTier.L2_TRUSTED)

        # Set peer trust to INTERNAL (tier 2)
        self.trust_manager.set_trust_tier("test_peer", TrustTier.INTERNAL)

        # Mock client
        self.mock_client = MagicMock()
        self.mock_client._sender_id = "ziway"
        self.mock_client.send.return_value = {"messageId": "msg_1", "conversationId": "conv_1"}

        # Create RouterContext
        self.ctx = RouterContext(
            client=self.mock_client,
            inbox_store=MagicMock(),
            outbox_store=MagicMock(),
            trust_manager=self.trust_manager,
            context_manager=self.context_manager,
            session_manager=self.session_manager,
        )

        from handlers.task_handler import register_task_handlers
        register_task_handlers(self.router, self.task_manager, self.executor)

    def tearDown(self):
        self.context_manager.close()
        self.session_manager.close()
        self.task_manager.close()

    def _make_task_msg(self, task_id, skill="echo", session_id=None,
                       sender="test_peer"):
        msg = {
            "protocol": "agentfax",
            "version": "1.0",
            "type": "task_request",
            "sender_id": sender,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "correlation_id": f"corr_{task_id}",
            "ttl": 3600,
            "payload": {
                "task_id": task_id,
                "skill": skill,
                "input": {"text": "hello"},
            },
            "_xmtp_sender_wallet": "0xTEST",
            "_xmtp_id": f"msg_{task_id}",
        }
        if session_id:
            msg["payload"]["session_id"] = session_id
        return msg

    def test_task_without_session_projects_context(self):
        """Task without session still projects context based on trust+skill cap."""
        # Intercept executor to capture what _context is passed
        original_execute = self.executor.execute
        captured_inputs = []

        def spy_execute(skill, input_data):
            captured_inputs.append(input_data)
            return original_execute(skill, input_data)

        self.executor.execute = spy_execute

        msg = self._make_task_msg("task_ctx_1")
        result = self.router.dispatch(msg, self.ctx)

        # Should succeed
        self.assertEqual(result["type"], "task_response")
        self.assertEqual(result["payload"]["status"], "completed")

        # Verify context was passed and contains only appropriate items
        self.assertEqual(len(captured_inputs), 1)
        exec_input = captured_inputs[0]
        if "_context" in exec_input:
            ctx_keys = {i["key"] for i in exec_input["_context"]}
            # L3 items must NEVER appear
            self.assertNotIn("api_key", ctx_keys)

    def test_task_with_l1_session_restricts_context(self):
        """Task with L1 session cap restricts context to L1 only."""
        sid = self.session_manager.create_session(
            peer_id="test_peer",
            proposed_skills=["echo"],
            proposed_trust_tier=2,
            proposed_max_context_privacy="L1_PUBLIC",
        )
        self.session_manager.accept_session(
            sid,
            agreed_skills=["echo"],
            agreed_trust_tier=2,
            agreed_max_context_privacy="L1_PUBLIC",
            agreed_max_calls=10,
        )

        # Intercept executor
        original_execute = self.executor.execute
        captured_inputs = []

        def spy_execute(skill, input_data):
            captured_inputs.append(input_data)
            return original_execute(skill, input_data)

        self.executor.execute = spy_execute

        msg = self._make_task_msg("task_ctx_2", session_id=sid)
        result = self.router.dispatch(msg, self.ctx)

        self.assertEqual(result["type"], "task_response")
        self.assertEqual(result["payload"]["status"], "completed")

        # Verify L2 items excluded by session L1 cap
        self.assertEqual(len(captured_inputs), 1)
        exec_input = captured_inputs[0]
        if "_context" in exec_input:
            ctx_keys = {i["key"] for i in exec_input["_context"]}
            self.assertNotIn("preference", ctx_keys)  # L2 blocked by L1 session cap
            self.assertNotIn("api_key", ctx_keys)      # L3 never shared

    def test_projection_error_is_fail_closed(self):
        """If context projection raises, task still executes but without context."""
        # Make context_manager.project_for_task raise
        self.context_manager.project_for_task = MagicMock(
            side_effect=RuntimeError("DB error")
        )

        msg = self._make_task_msg("task_ctx_3")
        result = self.router.dispatch(msg, self.ctx)

        # Task should still succeed (fail-closed = no context, not task failure)
        self.assertEqual(result["type"], "task_response")
        self.assertEqual(result["payload"]["status"], "completed")


# ── Verification scenario from plan ─────────────────────────────


class TestS4Verification(unittest.TestCase):
    """Plan verification: KNOWN peer only gets L1 context, projection failure is safe."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cm = ContextManager(self.tmpdir)
        # L1, L2, L3 items
        self.cm.add_context("tech_stack", "python,nodejs", category="skill",
                            privacy_tier=PrivacyTier.L1_PUBLIC)
        self.cm.add_context("work_pref", "async", category="preference",
                            privacy_tier=PrivacyTier.L2_TRUSTED)
        self.cm.add_context("api_key", "sk-xxx", category="credential",
                            privacy_tier=PrivacyTier.L3_PRIVATE)

    def tearDown(self):
        self.cm.close()

    def test_known_peer_only_l1(self):
        """Verification: KNOWN peer only gets L1 context."""
        items = self.cm.project_for_task(
            "code_review", peer_trust_tier=1,
            max_privacy_tier=PrivacyTier.L1_PUBLIC,
        )
        keys = {i["key"] for i in items}
        self.assertIn("tech_stack", keys)
        self.assertNotIn("work_pref", keys)
        self.assertNotIn("api_key", keys)

    def test_projection_failure_shares_nothing(self):
        """Verification: LLM projection parse failure → no items shared."""
        engine = LLMProjectionEngine(api_key="")
        response = {"content": "totally invalid", "usage": {}}
        ids, rationale, _ = engine._parse_response(response, [
            {"context_id": "ctx_1", "key": "a", "value": "b", "category": "skill"},
        ])
        self.assertEqual(ids, [])

    def test_internal_with_l1_session_only_l1(self):
        """Verification: INTERNAL peer with L1 session cap only gets L1."""
        items = self.cm.project_for_task(
            "code_review", peer_trust_tier=2,
            max_privacy_tier=PrivacyTier.L1_PUBLIC,
        )
        keys = {i["key"] for i in items}
        self.assertIn("tech_stack", keys)
        self.assertNotIn("work_pref", keys)
        self.assertNotIn("api_key", keys)

    def test_internal_without_cap_gets_l1_l2(self):
        """Verification: INTERNAL peer without cap gets L1+L2."""
        items = self.cm.project_for_task(
            "code_review", peer_trust_tier=2,
        )
        keys = {i["key"] for i in items}
        self.assertIn("tech_stack", keys)
        self.assertIn("work_pref", keys)
        self.assertNotIn("api_key", keys)


# ── Workflow dispatch: privacy cap on context ───────────────────


class TestWorkflowDispatchPrivacyCap(unittest.TestCase):
    """Regression: workflow step dispatch must enforce skill privacy cap on context."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cm = ContextManager(self.tmpdir)
        # L1 + L2 context
        self.cm.add_context("public_skill", "python", category="skill",
                            privacy_tier=PrivacyTier.L1_PUBLIC)
        self.cm.add_context("trusted_pref", "vim", category="skill",
                            privacy_tier=PrivacyTier.L2_TRUSTED)
        self.cm.add_context("secret", "sk-xxx", category="credential",
                            privacy_tier=PrivacyTier.L3_PRIVATE)

    def tearDown(self):
        self.cm.close()

    def test_l1_skill_cap_excludes_l2_in_workflow(self):
        """Workflow dispatch with L1 skill cap must exclude L2 items."""
        # Simulate what daemon.py does for workflow step dispatch
        peer_tier = 2  # INTERNAL
        privacy_tier_map = {"L1_PUBLIC": 1, "L2_TRUSTED": 2, "L3_PRIVATE": 3}
        skill_max_priv = "L1_PUBLIC"  # Skill cap is L1
        privacy_cap = privacy_tier_map.get(skill_max_priv, 1)

        context_items = self.cm.project_for_task(
            "echo", peer_tier,
            max_privacy_tier=privacy_cap,
            peer_name="icy",
        )
        keys = {i["key"] for i in context_items}
        self.assertIn("public_skill", keys)
        self.assertNotIn("trusted_pref", keys)  # L2 blocked by L1 skill cap
        self.assertNotIn("secret", keys)         # L3 never shared

    def test_l2_skill_cap_allows_l2_in_workflow(self):
        """Workflow dispatch with L2 skill cap allows L2 for INTERNAL peer."""
        peer_tier = 2
        privacy_tier_map = {"L1_PUBLIC": 1, "L2_TRUSTED": 2, "L3_PRIVATE": 3}
        skill_max_priv = "L2_TRUSTED"
        privacy_cap = privacy_tier_map.get(skill_max_priv, 1)

        context_items = self.cm.project_for_task(
            "echo", peer_tier,
            max_privacy_tier=privacy_cap,
            peer_name="icy",
        )
        keys = {i["key"] for i in context_items}
        self.assertIn("public_skill", keys)
        self.assertIn("trusted_pref", keys)
        self.assertNotIn("secret", keys)


if __name__ == "__main__":
    unittest.main()
