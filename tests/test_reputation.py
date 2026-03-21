"""Tests for ReputationManager — interaction tracking, trust tier auto-promotion/demotion."""

import pytest
from unittest.mock import MagicMock

from reputation import ReputationManager, TIER_THRESHOLDS


class TestInteractionRecording:
    """Test recording peer interactions."""

    def test_record_success(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        rm.record_interaction("icy", "task_completed", True, latency_ms=42.0)

        rep = rm.get_reputation("icy")
        assert rep is not None
        assert rep["total_interactions"] == 1
        assert rep["successes"] == 1
        assert rep["failures"] == 0
        assert rep["success_rate"] == 1.0
        rm.close()

    def test_record_failure(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        rm.record_interaction("icy", "task_failed", False)

        rep = rm.get_reputation("icy")
        assert rep["total_interactions"] == 1
        assert rep["successes"] == 0
        assert rep["failures"] == 1
        assert rep["success_rate"] == 0.0
        rm.close()

    def test_multiple_interactions_aggregate(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        rm.record_interaction("icy", "task_completed", True)
        rm.record_interaction("icy", "task_completed", True)
        rm.record_interaction("icy", "task_failed", False)

        rep = rm.get_reputation("icy")
        assert rep["total_interactions"] == 3
        assert rep["successes"] == 2
        assert rep["failures"] == 1
        rm.close()

    def test_latency_tracking(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        rm.record_interaction("icy", "ping_response", True, latency_ms=100.0)
        rm.record_interaction("icy", "ping_response", True, latency_ms=200.0)

        rep = rm.get_reputation("icy")
        assert rep["avg_latency_ms"] is not None
        assert rep["avg_latency_ms"] > 0
        rm.close()

    def test_unknown_peer_returns_none(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        assert rm.get_reputation("nonexistent") is None
        rm.close()

    def test_interaction_history(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        rm.record_interaction("icy", "task_completed", True)
        rm.record_interaction("icy", "ping_response", True)

        history = rm.get_interaction_history("icy")
        assert len(history) == 2
        assert history[0]["interaction_type"] in ("task_completed", "ping_response")
        rm.close()

    def test_multiple_peers(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        rm.record_interaction("icy", "task_completed", True)
        rm.record_interaction("alpha", "task_completed", True)
        rm.record_interaction("beta", "task_failed", False)

        all_reps = rm.get_all_reputations()
        assert len(all_reps) == 3
        rm.close()


class TestTrustTierSuggestion:
    """Test suggest_trust_tier logic."""

    def test_no_interactions_untrusted(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        assert rm.suggest_trust_tier("unknown") == 0
        rm.close()

    def test_few_failures_untrusted(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        rm.record_interaction("bad", "task_failed", False)
        rm.record_interaction("bad", "task_failed", False)
        assert rm.suggest_trust_tier("bad") == 0  # 0% success
        rm.close()

    def test_3_interactions_50pct_becomes_known(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        rm.record_interaction("peer_a", "task_completed", True)
        rm.record_interaction("peer_a", "task_completed", True)
        rm.record_interaction("peer_a", "task_failed", False)
        # 3 interactions, 66% success > 50% threshold
        assert rm.suggest_trust_tier("peer_a") == 1  # KNOWN
        rm.close()

    def test_10_interactions_80pct_becomes_internal(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        for i in range(9):
            rm.record_interaction("peer_b", "task_completed", True)
        rm.record_interaction("peer_b", "task_failed", False)
        # 10 interactions, 90% success > 80% threshold
        assert rm.suggest_trust_tier("peer_b") == 2  # INTERNAL
        rm.close()

    def test_never_auto_promote_to_privileged(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        for i in range(100):
            rm.record_interaction("super_peer", "task_completed", True)
        # Even with 100% success and 100 interactions, max auto is INTERNAL (2)
        assert rm.suggest_trust_tier("super_peer") == 2
        rm.close()

    def test_demotion_when_rate_drops(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        # Build up to INTERNAL
        for i in range(10):
            rm.record_interaction("peer_c", "task_completed", True)
        assert rm.suggest_trust_tier("peer_c") == 2  # INTERNAL

        # Add many failures to drop success rate
        for i in range(20):
            rm.record_interaction("peer_c", "task_failed", False)
        # Now 10/30 = 33% success — below KNOWN threshold (50%)
        assert rm.suggest_trust_tier("peer_c") == 0  # back to UNTRUSTED
        rm.close()


class TestCheckAndUpdateTiers:
    """Test auto-promotion/demotion integration."""

    def _make_mock_sm(self, initial_tiers=None):
        """Create a mock TrustManager."""
        tiers = initial_tiers or {}
        sm = MagicMock()
        sm.get_trust_tier = lambda peer_id: tiers.get(peer_id, 0)

        def set_tier(peer_id, tier):
            tiers[peer_id] = tier
        sm.set_trust_tier = set_tier
        sm._tiers = tiers
        return sm

    def test_promotes_peer(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        sm = self._make_mock_sm()

        # Build reputation for icy
        for i in range(5):
            rm.record_interaction("icy", "task_completed", True)

        changes = rm.check_and_update_tiers(sm)
        assert len(changes) == 1
        assert changes[0]["peer_id"] == "icy"
        assert changes[0]["new"] == "KNOWN"
        rm.close()

    def test_no_change_when_already_correct(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)

        for i in range(5):
            rm.record_interaction("icy", "task_completed", True)

        sm = self._make_mock_sm({"icy": 1})  # Already KNOWN
        changes = rm.check_and_update_tiers(sm)
        assert len(changes) == 0
        rm.close()

    def test_skips_privileged_peers(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        rm.record_interaction("admin", "task_failed", False)

        sm = self._make_mock_sm({"admin": 3})  # PRIVILEGED
        changes = rm.check_and_update_tiers(sm)
        assert len(changes) == 0  # Should not demote PRIVILEGED
        rm.close()

    def test_demotes_peer(self, tmp_data_dir):
        rm = ReputationManager(tmp_data_dir)
        # 3 interactions but 0% success
        for i in range(3):
            rm.record_interaction("bad_peer", "task_failed", False)

        sm = self._make_mock_sm({"bad_peer": 1})  # Currently KNOWN
        changes = rm.check_and_update_tiers(sm)
        assert len(changes) == 1
        assert changes[0]["new"] == "UNTRUSTED"
        rm.close()
