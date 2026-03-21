#!/usr/bin/env python3
"""Tests for S0: Trust enforcement, dedup, unified error model, Skill-as-API."""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"))

from executor import TaskExecutor, SkillDefinition, register_builtin_skills
from security import TrustManager, TrustTier


class TestSkillDefinitionTrust(unittest.TestCase):
    """Test that SkillDefinition carries trust fields."""

    def test_default_trust_tier(self):
        sd = SkillDefinition("test", lambda x: x)
        self.assertEqual(sd.min_trust_tier, 1)
        self.assertEqual(sd.max_context_privacy_tier, "L1_PUBLIC")

    def test_custom_trust_tier(self):
        sd = SkillDefinition("test", lambda x: x, min_trust_tier=2,
                             max_context_privacy_tier="L2_TRUSTED")
        self.assertEqual(sd.min_trust_tier, 2)
        self.assertEqual(sd.max_context_privacy_tier, "L2_TRUSTED")

    def test_to_dict_includes_trust(self):
        sd = SkillDefinition("test", lambda x: x, min_trust_tier=3)
        d = sd.to_dict()
        self.assertIn("min_trust_tier", d)
        self.assertEqual(d["min_trust_tier"], 3)
        self.assertIn("max_context_privacy_tier", d)

    def test_executor_skill_decorator_trust(self):
        ex = TaskExecutor()

        @ex.skill("secret", min_trust_tier=2)
        def secret_handler(data):
            return {"secret": True}

        sd = ex.get_skill("secret")
        self.assertIsNotNone(sd)
        self.assertEqual(sd.min_trust_tier, 2)

    def test_executor_register_skill_trust(self):
        ex = TaskExecutor()
        ex.register_skill("paid", lambda x: x, min_trust_tier=2)
        sd = ex.get_skill("paid")
        self.assertEqual(sd.min_trust_tier, 2)


class TestInstallFromCodeRemoved(unittest.TestCase):
    """Test that remote code installation is removed."""

    def test_no_install_from_code(self):
        ex = TaskExecutor()
        self.assertFalse(hasattr(ex, "install_from_code"))

    def test_no_load_skills_from_dir(self):
        ex = TaskExecutor()
        self.assertFalse(hasattr(ex, "load_skills_from_dir"))

    def test_get_skill_method_exists(self):
        ex = TaskExecutor()
        register_builtin_skills(ex)
        self.assertIsNotNone(ex.get_skill("echo"))
        self.assertIsNone(ex.get_skill("nonexistent"))


class TestTrustEnforcement(unittest.TestCase):
    """Test trust check in task_handler."""

    def setUp(self):
        from router import MessageRouter, RouterContext
        self.router = MessageRouter()
        self.task_manager = MagicMock()
        self.executor = TaskExecutor()
        register_builtin_skills(self.executor)

        # Register a skill requiring INTERNAL trust
        self.executor.register_skill(
            "secret_analysis", lambda x: {"result": "classified"},
            min_trust_tier=2,  # INTERNAL
        )

        self.trust_manager = MagicMock()

        self.ctx = RouterContext(
            client=MagicMock(),
            trust_manager=self.trust_manager,
        )

        from handlers.task_handler import register_task_handlers
        register_task_handlers(self.router, self.task_manager, self.executor)

    def test_untrusted_peer_rejected(self):
        """UNTRUSTED peer trying to call echo (requires KNOWN) → rejected."""
        self.trust_manager.get_trust_tier.return_value = TrustTier.UNTRUSTED

        msg = {
            "type": "task_request",
            "sender_id": "evil_bot",
            "correlation_id": "corr_untrusted_1",
            "payload": {
                "skill": "echo",
                "input": {"text": "hack"},
                "task_id": "t1",
            },
        }

        result = self.router.dispatch(msg, self.ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "task_error")
        self.assertEqual(result["payload"]["error_code"], "TRUST_TIER_TOO_LOW")
        self.assertFalse(result["payload"]["retryable"])
        self.assertEqual(result["payload"]["scope"], "authorization")

    def test_known_peer_allowed_for_echo(self):
        """KNOWN peer calling echo → allowed."""
        self.trust_manager.get_trust_tier.return_value = TrustTier.KNOWN
        self.task_manager.receive_task = MagicMock()
        self.task_manager.accept_task = MagicMock()
        self.task_manager.start_task = MagicMock()
        self.task_manager.complete_task = MagicMock()

        msg = {
            "type": "task_request",
            "sender_id": "friend_bot",
            "correlation_id": "corr_known_1",
            "payload": {
                "skill": "echo",
                "input": {"text": "hello"},
                "task_id": "t2",
            },
        }

        result = self.router.dispatch(msg, self.ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "task_response")
        self.assertEqual(result["payload"]["status"], "completed")

    def test_known_peer_rejected_for_internal_skill(self):
        """KNOWN peer trying to call secret_analysis (requires INTERNAL) → rejected."""
        self.trust_manager.get_trust_tier.return_value = TrustTier.KNOWN

        msg = {
            "type": "task_request",
            "sender_id": "friend_bot",
            "correlation_id": "corr_known_secret_1",
            "payload": {
                "skill": "secret_analysis",
                "input": {},
                "task_id": "t3",
            },
        }

        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_error")
        self.assertEqual(result["payload"]["error_code"], "TRUST_TIER_TOO_LOW")

    def test_internal_peer_allowed_for_internal_skill(self):
        """INTERNAL peer calling secret_analysis → allowed."""
        self.trust_manager.get_trust_tier.return_value = TrustTier.INTERNAL
        self.task_manager.receive_task = MagicMock()
        self.task_manager.accept_task = MagicMock()
        self.task_manager.start_task = MagicMock()
        self.task_manager.complete_task = MagicMock()

        msg = {
            "type": "task_request",
            "sender_id": "trusted_bot",
            "correlation_id": "corr_internal_1",
            "payload": {
                "skill": "secret_analysis",
                "input": {},
                "task_id": "t4",
            },
        }

        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_response")

    def test_unknown_skill_error(self):
        """Request for nonexistent skill → SKILL_NOT_FOUND error."""
        self.trust_manager.get_trust_tier.return_value = TrustTier.KNOWN

        msg = {
            "type": "task_request",
            "sender_id": "friend_bot",
            "correlation_id": "corr_missing_1",
            "payload": {
                "skill": "nonexistent_skill",
                "input": {},
                "task_id": "t5",
            },
        }

        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_error")
        self.assertEqual(result["payload"]["error_code"], "SKILL_NOT_FOUND")


class TestDedup(unittest.TestCase):
    """Test idempotency/dedup in task_handler."""

    def setUp(self):
        from router import MessageRouter, RouterContext
        self.router = MessageRouter()
        self.task_manager = MagicMock()
        self.executor = TaskExecutor()
        register_builtin_skills(self.executor)
        self.trust_manager = MagicMock()
        self.trust_manager.get_trust_tier.return_value = TrustTier.KNOWN

        self.ctx = RouterContext(
            client=MagicMock(),
            trust_manager=self.trust_manager,
        )

        from handlers.task_handler import register_task_handlers
        register_task_handlers(self.router, self.task_manager, self.executor)

    def test_duplicate_request_returns_cached(self):
        """Same sender + correlation_id → cached response, no re-execution."""
        self.task_manager.receive_task = MagicMock()
        self.task_manager.accept_task = MagicMock()
        self.task_manager.start_task = MagicMock()
        self.task_manager.complete_task = MagicMock()

        msg = {
            "type": "task_request",
            "sender_id": "bot_a",
            "correlation_id": "dedup_test_1",
            "payload": {"skill": "echo", "input": {"x": 1}, "task_id": "td1"},
        }

        r1 = self.router.dispatch(msg, self.ctx)
        r2 = self.router.dispatch(msg, self.ctx)

        self.assertEqual(r1, r2)
        # execute was only called once (via receive_task)
        self.assertEqual(self.task_manager.receive_task.call_count, 1)

    def test_different_sender_same_correlation_not_cached(self):
        """Different sender with same correlation_id → separate executions."""
        self.task_manager.receive_task = MagicMock()
        self.task_manager.accept_task = MagicMock()
        self.task_manager.start_task = MagicMock()
        self.task_manager.complete_task = MagicMock()

        msg1 = {
            "type": "task_request",
            "sender_id": "bot_a",
            "correlation_id": "shared_corr_1",
            "payload": {"skill": "echo", "input": {"x": 1}, "task_id": "td2a"},
        }
        msg2 = {
            "type": "task_request",
            "sender_id": "bot_b",
            "correlation_id": "shared_corr_1",
            "payload": {"skill": "echo", "input": {"x": 2}, "task_id": "td2b"},
        }

        r1 = self.router.dispatch(msg1, self.ctx)
        r2 = self.router.dispatch(msg2, self.ctx)

        # Both should execute (different senders)
        self.assertEqual(self.task_manager.receive_task.call_count, 2)


class TestSkillInstallRejected(unittest.TestCase):
    """Test that skill_install is properly rejected."""

    def setUp(self):
        from router import MessageRouter, RouterContext
        self.router = MessageRouter()
        self.executor = TaskExecutor()

        self.ctx = RouterContext(client=MagicMock())

        from handlers.skill_handler import register_skill_handlers
        register_skill_handlers(self.router, self.executor, "/tmp/test")

    def test_skill_install_returns_forbidden(self):
        msg = {
            "type": "skill_install",
            "sender_id": "hacker",
            "payload": {
                "name": "backdoor",
                "code": "def handler(x): import os; os.system('rm -rf /')",
            },
        }
        result = self.router.dispatch(msg, self.ctx)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "skill_install_result")
        self.assertFalse(result["payload"]["success"])
        self.assertEqual(result["payload"]["error_code"], "CODE_TRANSFER_FORBIDDEN")

    def test_skill_card_query_works(self):
        register_builtin_skills(self.executor)
        msg = {
            "type": "skill_card_query",
            "sender_id": "friend",
            "payload": {},
        }
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "skill_card_list")
        self.assertGreater(result["payload"]["count"], 0)

    def test_legacy_skill_query_returns_skill_list_type(self):
        """Legacy skill_query must return type=skill_list for backwards compat."""
        register_builtin_skills(self.executor)
        msg = {
            "type": "skill_query",
            "sender_id": "old_node",
            "payload": {},
        }
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "skill_list")

    def test_skill_card_get(self):
        register_builtin_skills(self.executor)
        msg = {
            "type": "skill_card_get",
            "sender_id": "friend",
            "payload": {"skill_name": "echo"},
        }
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "skill_card")
        card = result["payload"]["card"]
        self.assertEqual(card["skill_name"], "echo")
        self.assertIn("trust_requirements", card)
        self.assertIn("min_trust_tier", card["trust_requirements"])

    def test_skill_card_get_missing(self):
        msg = {
            "type": "skill_card_get",
            "sender_id": "friend",
            "payload": {"skill_name": "nonexistent"},
        }
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_error")
        self.assertEqual(result["payload"]["error_code"], "SKILL_NOT_FOUND")


class TestUnifiedErrorModel(unittest.TestCase):
    """Test that error responses follow unified format."""

    def setUp(self):
        from router import MessageRouter, RouterContext
        self.router = MessageRouter()
        self.task_manager = MagicMock()
        self.executor = TaskExecutor()
        register_builtin_skills(self.executor)
        self.trust_manager = MagicMock()
        self.trust_manager.get_trust_tier.return_value = TrustTier.UNTRUSTED

        self.ctx = RouterContext(
            client=MagicMock(),
            trust_manager=self.trust_manager,
        )

        from handlers.task_handler import register_task_handlers
        register_task_handlers(self.router, self.task_manager, self.executor)

    def test_error_has_all_fields(self):
        msg = {
            "type": "task_request",
            "sender_id": "untrusted",
            "correlation_id": "err_test_1",
            "payload": {"skill": "echo", "input": {}, "task_id": "te1"},
        }
        result = self.router.dispatch(msg, self.ctx)
        p = result["payload"]
        self.assertIn("error_code", p)
        self.assertIn("error_message", p)
        self.assertIn("retryable", p)
        self.assertIn("scope", p)


if __name__ == "__main__":
    unittest.main()
