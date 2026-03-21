"""Tests for SessionManager multi-session query methods (Sprint 2)."""

import os
import sys
import pytest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"
)
sys.path.insert(0, SCRIPTS_DIR)

from session import SessionManager


@pytest.mark.unit
class TestMultiSession:
    """Tests for get_active_sessions, get_sessions_for_peer, has_capacity."""

    def _make_sm(self, tmp_path):
        data_dir = str(tmp_path / "sessions")
        os.makedirs(data_dir, exist_ok=True)
        return SessionManager(data_dir)

    def test_create_multiple_sessions(self, tmp_path):
        """Create 3 sessions for 3 different peers and verify all are stored."""
        sm = self._make_sm(tmp_path)

        sid1 = sm.create_session(peer_id="peer-alpha", role="initiator",
                                  proposed_skills=["web_search"], ttl_seconds=3600)
        sid2 = sm.create_session(peer_id="peer-beta", role="initiator",
                                  proposed_skills=["write_report"], ttl_seconds=3600)
        sid3 = sm.create_session(peer_id="peer-gamma", role="responder",
                                  proposed_skills=["translate"], ttl_seconds=3600)

        assert sm.count() == 3
        assert sm.get_session(sid1) is not None
        assert sm.get_session(sid2) is not None
        assert sm.get_session(sid3) is not None

        sm.close()

    def test_session_isolation(self, tmp_path):
        """Sessions for different peers do not interfere with each other."""
        sm = self._make_sm(tmp_path)

        sid_a = sm.create_session(peer_id="peer-a", role="initiator",
                                   proposed_skills=["skill_a"],
                                   proposed_max_calls=5, ttl_seconds=3600)
        sid_b = sm.create_session(peer_id="peer-b", role="initiator",
                                   proposed_skills=["skill_b"],
                                   proposed_max_calls=10, ttl_seconds=3600)

        # Accept session A
        sm.accept_session(sid_a, agreed_skills=["skill_a"], agreed_max_calls=5)

        # Session B should still be proposed
        sess_a = sm.get_session(sid_a)
        sess_b = sm.get_session(sid_b)

        assert sess_a["state"] == "active"
        assert sess_b["state"] == "proposed"

        # Peer B sessions should not include session A
        peer_b_sessions = sm.get_sessions_for_peer("peer-b")
        peer_b_ids = [s["session_id"] for s in peer_b_sessions]
        assert sid_b in peer_b_ids
        assert sid_a not in peer_b_ids

        sm.close()

    def test_capacity_limit(self, tmp_path):
        """has_capacity() returns False once the limit is reached."""
        sm = self._make_sm(tmp_path)

        # Create sessions up to capacity (max_sessions=3)
        for i in range(3):
            sm.create_session(peer_id=f"peer-{i}", role="initiator",
                               proposed_skills=[], ttl_seconds=3600)

        assert sm.has_capacity(max_sessions=3) is False
        assert sm.has_capacity(max_sessions=4) is True
        assert sm.has_capacity(max_sessions=2) is False

        sm.close()

    def test_capacity_limit_excludes_terminal_sessions(self, tmp_path):
        """Completed/closed sessions do not count against capacity."""
        sm = self._make_sm(tmp_path)

        sid = sm.create_session(peer_id="old-peer", role="initiator",
                                 proposed_skills=[], ttl_seconds=3600)
        sm.accept_session(sid, agreed_skills=[], agreed_max_calls=5)
        sm.close_session(sid, "done")
        sm.complete_session(sid)

        # The completed session should not consume capacity
        assert sm.has_capacity(max_sessions=1) is True

        sm.close()

    def test_get_active_sessions(self, tmp_path):
        """get_active_sessions() returns only sessions in ACTIVE state."""
        sm = self._make_sm(tmp_path)

        sid1 = sm.create_session(peer_id="peer-1", role="initiator",
                                  proposed_skills=[], ttl_seconds=3600)
        sid2 = sm.create_session(peer_id="peer-2", role="initiator",
                                  proposed_skills=[], ttl_seconds=3600)
        sid3 = sm.create_session(peer_id="peer-3", role="initiator",
                                  proposed_skills=[], ttl_seconds=3600)

        # Activate only sessions 1 and 2
        sm.accept_session(sid1, agreed_skills=[], agreed_max_calls=5)
        sm.accept_session(sid2, agreed_skills=[], agreed_max_calls=5)
        # sid3 stays proposed

        active = sm.get_active_sessions()
        active_ids = [s["session_id"] for s in active]

        assert sid1 in active_ids
        assert sid2 in active_ids
        assert sid3 not in active_ids
        assert len(active) == 2

        sm.close()

    def test_get_sessions_for_peer(self, tmp_path):
        """get_sessions_for_peer() returns all sessions (any state) for a peer."""
        sm = self._make_sm(tmp_path)

        # Create two sessions for "peer-x" (different states)
        sid1 = sm.create_session(peer_id="peer-x", role="initiator",
                                  proposed_skills=["search"], ttl_seconds=3600)
        sm.accept_session(sid1, agreed_skills=["search"], agreed_max_calls=5)

        sid2 = sm.create_session(peer_id="peer-x", role="initiator",
                                  proposed_skills=["write"], ttl_seconds=3600)
        # sid2 stays proposed

        # Create one session for a different peer
        sid3 = sm.create_session(peer_id="peer-y", role="initiator",
                                  proposed_skills=[], ttl_seconds=3600)

        peer_x_sessions = sm.get_sessions_for_peer("peer-x")
        peer_x_ids = {s["session_id"] for s in peer_x_sessions}

        assert sid1 in peer_x_ids
        assert sid2 in peer_x_ids
        assert sid3 not in peer_x_ids

        # peer-y sessions should not include peer-x sessions
        peer_y_sessions = sm.get_sessions_for_peer("peer-y")
        peer_y_ids = {s["session_id"] for s in peer_y_sessions}
        assert sid3 in peer_y_ids
        assert sid1 not in peer_y_ids

        sm.close()

    def test_get_active_sessions_empty(self, tmp_path):
        """get_active_sessions() returns empty list when nothing is active."""
        sm = self._make_sm(tmp_path)
        assert sm.get_active_sessions() == []
        sm.close()

    def test_get_sessions_for_peer_unknown(self, tmp_path):
        """get_sessions_for_peer() returns empty list for an unknown peer."""
        sm = self._make_sm(tmp_path)
        assert sm.get_sessions_for_peer("nobody") == []
        sm.close()
