"""Tests for TrustManager — local trust tier management."""

import json
import pytest

from security import TrustManager, TrustTier


class TestTrustTiers:
    """Test trust tier management."""

    def test_default_tier_is_untrusted(self, tmp_data_dir):
        tm = TrustManager(tmp_data_dir)
        assert tm.get_trust_tier("unknown_peer") == TrustTier.UNTRUSTED

    def test_set_trust_tier_in_memory(self, tmp_data_dir):
        tm = TrustManager(tmp_data_dir)
        tm.set_trust_tier("icy", TrustTier.INTERNAL)
        assert tm.get_trust_tier("icy") == TrustTier.INTERNAL

    def test_set_trust_override_persists(self, tmp_data_dir):
        tm = TrustManager(tmp_data_dir)
        tm.set_trust_override("icy", TrustTier.KNOWN)

        # Create new instance — should load from file
        tm2 = TrustManager(tmp_data_dir)
        assert tm2.get_trust_tier("icy") == TrustTier.KNOWN

    def test_remove_trust_override(self, tmp_data_dir):
        tm = TrustManager(tmp_data_dir)
        tm.set_trust_override("icy", TrustTier.INTERNAL)
        assert tm.get_trust_tier("icy") == TrustTier.INTERNAL

        tm.remove_trust_override("icy")
        assert tm.get_trust_tier("icy") == TrustTier.UNTRUSTED

    def test_remove_nonexistent_override_is_safe(self, tmp_data_dir):
        tm = TrustManager(tmp_data_dir)
        tm.remove_trust_override("nobody")  # should not raise

    def test_load_trust_from_file(self, tmp_data_dir):
        data = {"icy": "internal", "bad_bot": "untrusted"}
        with open(f"{tmp_data_dir}/trust.json", "w") as f:
            json.dump(data, f)

        tm = TrustManager(tmp_data_dir)
        assert tm.get_trust_tier("icy") == TrustTier.INTERNAL
        assert tm.get_trust_tier("bad_bot") == TrustTier.UNTRUSTED

    def test_load_trust_from_int_values(self, tmp_data_dir):
        data = {"icy": 2, "peer_a": 1}
        with open(f"{tmp_data_dir}/trust.json", "w") as f:
            json.dump(data, f)

        tm = TrustManager(tmp_data_dir)
        assert tm.get_trust_tier("icy") == TrustTier.INTERNAL
        assert tm.get_trust_tier("peer_a") == TrustTier.KNOWN

    def test_all_tiers_property(self, tmp_data_dir):
        tm = TrustManager(tmp_data_dir)
        tm.set_trust_tier("a", TrustTier.KNOWN)
        tm.set_trust_tier("b", TrustTier.INTERNAL)

        tiers = tm.all_tiers
        assert tiers["a"] == "KNOWN"
        assert tiers["b"] == "INTERNAL"

    def test_corrupt_trust_file_handled(self, tmp_data_dir):
        with open(f"{tmp_data_dir}/trust.json", "w") as f:
            f.write("not json")

        # Should not crash, just warn
        tm = TrustManager(tmp_data_dir)
        assert tm.get_trust_tier("anyone") == TrustTier.UNTRUSTED

    def test_trust_tier_ordering(self):
        assert TrustTier.UNTRUSTED < TrustTier.KNOWN
        assert TrustTier.KNOWN < TrustTier.INTERNAL
        assert TrustTier.INTERNAL < TrustTier.PRIVILEGED
