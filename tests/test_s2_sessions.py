#!/usr/bin/env python3
"""Tests for S2: Collaboration sessions — 7-state machine, handlers, task integration."""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"))

from session import SessionManager, SessionState, TERMINAL_STATES, VALID_TRANSITIONS
from executor import TaskExecutor, register_builtin_skills
from security import TrustTier


# ── SessionManager state machine ─────────────────────────────

class TestSessionStateMachine(unittest.TestCase):
    """Test the 7-state session lifecycle."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sm = SessionManager(self.tmpdir)

    def tearDown(self):
        self.sm.close()

    def test_create_session(self):
        sid = self.sm.create_session(
            peer_id="icy",
            proposed_skills=["echo", "reverse"],
            proposed_trust_tier=1,
            proposed_max_calls=5,
        )
        self.assertTrue(sid.startswith("sess_"))
        session = self.sm.get_session(sid)
        self.assertEqual(session["state"], "proposed")
        self.assertEqual(session["peer_id"], "icy")
        self.assertEqual(json.loads(session["proposed_skills"]), ["echo", "reverse"])
        self.assertEqual(session["proposed_max_calls"], 5)
        self.assertEqual(session["role"], "initiator")

    def test_proposed_to_active(self):
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])
        ok = self.sm.accept_session(sid, agreed_skills=["echo"],
                                     agreed_trust_tier=1, agreed_max_calls=10)
        self.assertTrue(ok)
        session = self.sm.get_session(sid)
        self.assertEqual(session["state"], "active")
        self.assertEqual(json.loads(session["agreed_skills"]), ["echo"])
        self.assertEqual(session["agreed_max_calls"], 10)
        self.assertIsNotNone(session["accepted_at"])

    def test_proposed_to_rejected(self):
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])
        ok = self.sm.reject_session(sid, reason="not interested")
        self.assertTrue(ok)
        session = self.sm.get_session(sid)
        self.assertEqual(session["state"], "rejected")
        self.assertEqual(session["close_reason"], "not interested")

    def test_proposed_to_expired(self):
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])
        ok = self.sm.expire_session(sid)
        self.assertTrue(ok)
        self.assertEqual(self.sm.get_session(sid)["state"], "expired")

    def test_active_to_closing(self):
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])
        self.sm.accept_session(sid)
        ok = self.sm.close_session(sid, reason="done")
        self.assertTrue(ok)
        self.assertEqual(self.sm.get_session(sid)["state"], "closing")

    def test_closing_to_completed(self):
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])
        self.sm.accept_session(sid)
        self.sm.close_session(sid)
        ok = self.sm.complete_session(sid)
        self.assertTrue(ok)
        self.assertEqual(self.sm.get_session(sid)["state"], "completed")

    def test_closing_to_closed(self):
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])
        self.sm.accept_session(sid)
        self.sm.close_session(sid)
        ok = self.sm.force_close_session(sid, reason="force")
        self.assertTrue(ok)
        self.assertEqual(self.sm.get_session(sid)["state"], "closed")

    def test_terminal_states_immutable(self):
        """No transitions out of terminal states."""
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])
        self.sm.reject_session(sid)
        # Try to accept after rejection
        ok = self.sm.accept_session(sid)
        self.assertFalse(ok)
        self.assertEqual(self.sm.get_session(sid)["state"], "rejected")

    def test_invalid_transition_rejected(self):
        """Can't go from proposed directly to closing."""
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])
        ok = self.sm.close_session(sid)
        self.assertFalse(ok)
        self.assertEqual(self.sm.get_session(sid)["state"], "proposed")

    def test_accept_defaults_to_proposed_terms(self):
        """If accept doesn't specify terms, use proposed terms."""
        sid = self.sm.create_session(
            peer_id="icy",
            proposed_skills=["echo"],
            proposed_trust_tier=2,
            proposed_max_context_privacy="L2_TRUSTED",
            proposed_max_calls=5,
        )
        self.sm.accept_session(sid)  # No explicit terms
        session = self.sm.get_session(sid)
        self.assertEqual(json.loads(session["agreed_skills"]), ["echo"])
        self.assertEqual(session["agreed_trust_tier"], 2)
        self.assertEqual(session["agreed_max_context_privacy"], "L2_TRUSTED")
        self.assertEqual(session["agreed_max_calls"], 5)


# ── Task tracking within sessions ────────────────────────────

class TestSessionTaskTracking(unittest.TestCase):
    """Test call count, task completion/failure, auto-complete."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sm = SessionManager(self.tmpdir)
        self.sid = self.sm.create_session(
            peer_id="icy", proposed_skills=["echo"],
            proposed_max_calls=3,
        )
        self.sm.accept_session(self.sid, agreed_max_calls=3)

    def tearDown(self):
        self.sm.close()

    def test_increment_call_count(self):
        self.assertTrue(self.sm.increment_call_count(self.sid))
        self.assertTrue(self.sm.increment_call_count(self.sid))
        self.assertTrue(self.sm.increment_call_count(self.sid))
        # 4th call should fail
        self.assertFalse(self.sm.increment_call_count(self.sid))
        session = self.sm.get_session(self.sid)
        self.assertEqual(session["call_count"], 3)

    def test_task_completed_decrements_in_flight(self):
        self.sm.increment_call_count(self.sid)
        session = self.sm.get_session(self.sid)
        self.assertEqual(session["tasks_in_flight"], 1)

        self.sm.task_completed(self.sid)
        session = self.sm.get_session(self.sid)
        self.assertEqual(session["tasks_in_flight"], 0)
        self.assertEqual(session["tasks_completed"], 1)

    def test_task_failed_decrements_in_flight(self):
        self.sm.increment_call_count(self.sid)
        self.sm.task_failed(self.sid)
        session = self.sm.get_session(self.sid)
        self.assertEqual(session["tasks_in_flight"], 0)
        self.assertEqual(session["tasks_failed"], 1)

    def test_auto_complete_on_closing(self):
        """When closing + no tasks in flight → auto-complete."""
        self.sm.increment_call_count(self.sid)
        self.sm.close_session(self.sid)
        self.assertEqual(self.sm.get_session(self.sid)["state"], "closing")

        self.sm.task_completed(self.sid)
        # Should auto-complete
        self.assertEqual(self.sm.get_session(self.sid)["state"], "completed")

    def test_no_auto_complete_with_in_flight(self):
        """Closing + tasks in flight → stays closing."""
        self.sm.increment_call_count(self.sid)
        self.sm.increment_call_count(self.sid)
        self.sm.close_session(self.sid)

        self.sm.task_completed(self.sid)  # 1 still in flight
        self.assertEqual(self.sm.get_session(self.sid)["state"], "closing")

    def test_complete_session_blocked_with_in_flight(self):
        """Can't manually complete if tasks in flight."""
        self.sm.increment_call_count(self.sid)
        self.sm.close_session(self.sid)
        ok = self.sm.complete_session(self.sid)
        self.assertFalse(ok)


# ── Session validation for task_request ──────────────────────

class TestSessionValidation(unittest.TestCase):
    """Test validate_task_request()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sm = SessionManager(self.tmpdir)

    def tearDown(self):
        self.sm.close()

    def _make_active_session(self, peer="icy", skills=None, max_calls=10):
        sid = self.sm.create_session(
            peer_id=peer,
            proposed_skills=skills or ["echo"],
            proposed_max_calls=max_calls,
        )
        self.sm.accept_session(sid, agreed_skills=skills or ["echo"],
                                agreed_max_calls=max_calls)
        return sid

    def test_valid_request(self):
        sid = self._make_active_session()
        ok, code, msg = self.sm.validate_task_request(sid, "echo", "icy")
        self.assertTrue(ok)
        self.assertEqual(code, "")

    def test_session_not_found(self):
        ok, code, msg = self.sm.validate_task_request("nonexistent", "echo", "icy")
        self.assertFalse(ok)
        self.assertEqual(code, "SESSION_NOT_FOUND")

    def test_session_not_active(self):
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])
        # Still in proposed state
        ok, code, msg = self.sm.validate_task_request(sid, "echo", "icy")
        self.assertFalse(ok)
        self.assertEqual(code, "SESSION_NOT_ACTIVE")

    def test_peer_mismatch(self):
        sid = self._make_active_session(peer="icy")
        ok, code, msg = self.sm.validate_task_request(sid, "echo", "hacker")
        self.assertFalse(ok)
        self.assertEqual(code, "SESSION_PEER_MISMATCH")

    def test_skill_not_in_session(self):
        sid = self._make_active_session(skills=["echo"])
        ok, code, msg = self.sm.validate_task_request(sid, "reverse", "icy")
        self.assertFalse(ok)
        self.assertEqual(code, "SKILL_NOT_IN_SESSION")

    def test_call_limit_exceeded(self):
        sid = self._make_active_session(max_calls=1)
        # Use up the call
        self.sm.increment_call_count(sid)
        ok, code, msg = self.sm.validate_task_request(sid, "echo", "icy")
        self.assertFalse(ok)
        self.assertEqual(code, "CALL_LIMIT_EXCEEDED")

    def test_expired_session(self):
        sid = self.sm.create_session(
            peer_id="icy", proposed_skills=["echo"],
            ttl_seconds=-1,  # Already expired
        )
        self.sm.accept_session(sid)
        ok, code, msg = self.sm.validate_task_request(sid, "echo", "icy")
        self.assertFalse(ok)
        self.assertEqual(code, "SESSION_EXPIRED")


# ── Expiry management ────────────────────────────────────────

class TestSessionExpiry(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sm = SessionManager(self.tmpdir)

    def tearDown(self):
        self.sm.close()

    def test_expire_stale_sessions(self):
        # Create expired sessions
        self.sm.create_session(peer_id="a", proposed_skills=["echo"], ttl_seconds=-1)
        self.sm.create_session(peer_id="b", proposed_skills=["echo"], ttl_seconds=-1)
        # Create non-expired session
        self.sm.create_session(peer_id="c", proposed_skills=["echo"], ttl_seconds=3600)

        expired = self.sm.expire_stale_sessions()
        self.assertEqual(expired, 2)

        # c should still be proposed
        sessions = self.sm.list_sessions(state="proposed")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["peer_id"], "c")


# ── Query methods ────────────────────────────────────────────

class TestSessionQueries(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sm = SessionManager(self.tmpdir)

    def tearDown(self):
        self.sm.close()

    def test_get_active_session(self):
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])
        self.sm.accept_session(sid)
        active = self.sm.get_active_session("icy")
        self.assertIsNotNone(active)
        self.assertEqual(active["session_id"], sid)

    def test_get_active_session_none(self):
        self.assertIsNone(self.sm.get_active_session("nobody"))

    def test_list_sessions_filter(self):
        s1 = self.sm.create_session(peer_id="a", proposed_skills=["echo"])
        s2 = self.sm.create_session(peer_id="b", proposed_skills=["echo"])
        self.sm.accept_session(s1)

        active = self.sm.list_sessions(state="active")
        self.assertEqual(len(active), 1)
        proposed = self.sm.list_sessions(state="proposed")
        self.assertEqual(len(proposed), 1)

    def test_count(self):
        self.sm.create_session(peer_id="a", proposed_skills=["echo"])
        self.sm.create_session(peer_id="b", proposed_skills=["echo"])
        self.assertEqual(self.sm.count(), 2)
        self.assertEqual(self.sm.count(state="proposed"), 2)
        self.assertEqual(self.sm.count(state="active"), 0)


# ── Session handler integration ──────────────────────────────

class TestSessionHandler(unittest.TestCase):
    """Test session_handler message dispatch."""

    def setUp(self):
        from router import MessageRouter, RouterContext
        self.tmpdir = tempfile.mkdtemp()
        self.router = MessageRouter()
        self.sm = SessionManager(self.tmpdir)
        self.executor = TaskExecutor()
        register_builtin_skills(self.executor)
        self.trust_manager = MagicMock()
        self.trust_manager.get_trust_tier.return_value = TrustTier.KNOWN

        self.ctx = RouterContext(
            client=MagicMock(),
            trust_manager=self.trust_manager,
            session_manager=self.sm,
        )

        from handlers.session_handler import register_session_handlers
        register_session_handlers(self.router, self.sm, self.executor)

    def tearDown(self):
        self.sm.close()

    def test_session_propose_auto_accepts(self):
        """session_propose creates local session and auto-accepts."""
        msg = {
            "type": "session_propose",
            "sender_id": "icy",
            "payload": {
                "session_id": "remote_sess_1",
                "proposed_skills": ["echo"],
                "proposed_trust_tier": 1,
                "proposed_max_calls": 5,
                "ttl_seconds": 3600,
            },
        }
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "session_accept")
        payload = result["payload"]
        self.assertIn("session_id", payload)
        self.assertEqual(payload["remote_session_id"], "remote_sess_1")
        self.assertEqual(payload["agreed_skills"], ["echo"])
        self.assertEqual(payload["agreed_max_calls"], 5)

        # Verify local session is active
        session = self.sm.get_session(payload["session_id"])
        self.assertEqual(session["state"], "active")
        self.assertEqual(session["role"], "responder")

    def test_session_propose_untrusted_rejected(self):
        """UNTRUSTED peer can't propose sessions."""
        self.trust_manager.get_trust_tier.return_value = TrustTier.UNTRUSTED
        msg = {
            "type": "session_propose",
            "sender_id": "hacker",
            "payload": {
                "proposed_skills": ["echo"],
            },
        }
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_error")
        self.assertEqual(result["payload"]["error_code"], "TRUST_TIER_TOO_LOW")

    def test_session_propose_unknown_skill_rejected(self):
        msg = {
            "type": "session_propose",
            "sender_id": "icy",
            "payload": {
                "proposed_skills": ["nonexistent_skill"],
            },
        }
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_error")
        self.assertEqual(result["payload"]["error_code"], "SKILL_NOT_FOUND")

    def test_session_accept_activates_local(self):
        """session_accept from peer activates our local session."""
        # Create local session as initiator
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])

        msg = {
            "type": "session_accept",
            "sender_id": "icy",
            "payload": {
                "session_id": "remote_sess_2",
                "remote_session_id": sid,
                "agreed_skills": ["echo"],
                "agreed_trust_tier": 1,
                "agreed_max_calls": 10,
            },
        }
        self.router.dispatch(msg, self.ctx)
        session = self.sm.get_session(sid)
        self.assertEqual(session["state"], "active")
        self.assertEqual(session["agreed_max_calls"], 10)

    def test_session_reject_rejects_local(self):
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])
        msg = {
            "type": "session_reject",
            "sender_id": "icy",
            "payload": {
                "remote_session_id": sid,
                "reason": "busy",
            },
        }
        self.router.dispatch(msg, self.ctx)
        self.assertEqual(self.sm.get_session(sid)["state"], "rejected")

    def test_session_close(self):
        # Create and accept
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])
        self.sm.accept_session(sid)

        msg = {
            "type": "session_close",
            "sender_id": "icy",
            "payload": {
                "session_id": sid,
                "reason": "done",
            },
        }
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "session_close")
        # No tasks in flight → should be completed
        session = self.sm.get_session(sid)
        self.assertIn(session["state"], ["completed", "closing", "closed"])


# ── Task handler + session integration ───────────────────────

class TestTaskHandlerSessionIntegration(unittest.TestCase):
    """Test task_request validation with sessions."""

    def setUp(self):
        from router import MessageRouter, RouterContext
        self.tmpdir = tempfile.mkdtemp()
        self.router = MessageRouter()
        self.sm = SessionManager(self.tmpdir)
        self.task_manager = MagicMock()
        self.executor = TaskExecutor()
        register_builtin_skills(self.executor)
        self.trust_manager = MagicMock()
        self.trust_manager.get_trust_tier.return_value = TrustTier.KNOWN

        self.ctx = RouterContext(
            client=MagicMock(),
            trust_manager=self.trust_manager,
            session_manager=self.sm,
        )

        from handlers.task_handler import register_task_handlers
        register_task_handlers(self.router, self.task_manager, self.executor)

    def tearDown(self):
        self.sm.close()

    def _make_active_session(self, peer="icy", skills=None, max_calls=10):
        sid = self.sm.create_session(
            peer_id=peer, proposed_skills=skills or ["echo"],
            proposed_max_calls=max_calls,
        )
        self.sm.accept_session(sid, agreed_skills=skills or ["echo"],
                                agreed_max_calls=max_calls)
        return sid

    def _task_msg(self, skill="echo", session_id=None, sender="icy"):
        self.task_manager.receive_task = MagicMock()
        self.task_manager.accept_task = MagicMock()
        self.task_manager.start_task = MagicMock()
        self.task_manager.complete_task = MagicMock()
        return {
            "type": "task_request",
            "sender_id": sender,
            "correlation_id": f"corr_{time.time()}",
            "payload": {
                "skill": skill,
                "input": {"text": "hello"},
                "task_id": f"t_{time.time()}",
                "session_id": session_id,
            },
        }

    def test_task_with_valid_session(self):
        sid = self._make_active_session()
        msg = self._task_msg(session_id=sid)
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_response")
        self.assertEqual(result["payload"]["session_id"], sid)

    def test_task_with_invalid_session(self):
        msg = self._task_msg(session_id="nonexistent_sess")
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_error")
        self.assertEqual(result["payload"]["error_code"], "SESSION_NOT_FOUND")

    def test_task_with_inactive_session(self):
        sid = self.sm.create_session(peer_id="icy", proposed_skills=["echo"])
        # Don't accept — still proposed
        msg = self._task_msg(session_id=sid)
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_error")
        self.assertEqual(result["payload"]["error_code"], "SESSION_NOT_ACTIVE")

    def test_task_with_wrong_skill(self):
        sid = self._make_active_session(skills=["echo"])
        msg = self._task_msg(skill="reverse", session_id=sid)
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_error")
        self.assertEqual(result["payload"]["error_code"], "SKILL_NOT_IN_SESSION")

    def test_task_with_wrong_peer(self):
        sid = self._make_active_session(peer="icy")
        msg = self._task_msg(session_id=sid, sender="hacker")
        # hacker is KNOWN trust, so passes trust check
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_error")
        self.assertEqual(result["payload"]["error_code"], "SESSION_PEER_MISMATCH")

    def test_task_call_limit_exceeded(self):
        sid = self._make_active_session(max_calls=1)
        # First call succeeds
        msg1 = self._task_msg(session_id=sid)
        r1 = self.router.dispatch(msg1, self.ctx)
        self.assertEqual(r1["type"], "task_response")

        # Second call exceeds limit
        msg2 = self._task_msg(session_id=sid)
        r2 = self.router.dispatch(msg2, self.ctx)
        self.assertEqual(r2["type"], "task_error")
        self.assertEqual(r2["payload"]["error_code"], "CALL_LIMIT_EXCEEDED")

    def test_task_without_session_still_works(self):
        """Backwards compat: no session_id → standalone task."""
        msg = self._task_msg(session_id=None)
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_response")

    def test_session_counter_updates_on_completion(self):
        sid = self._make_active_session()
        msg = self._task_msg(session_id=sid)
        self.router.dispatch(msg, self.ctx)
        session = self.sm.get_session(sid)
        self.assertEqual(session["tasks_completed"], 1)
        self.assertEqual(session["call_count"], 1)


if __name__ == "__main__":
    unittest.main()
