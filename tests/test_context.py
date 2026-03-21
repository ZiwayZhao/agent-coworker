"""Tests for ContextManager — privacy tiers, projection, peer context."""

import pytest
from context_manager import ContextManager, PrivacyTier, TASK_CATEGORY_MAP


class TestContextCRUD:
    """Test adding, querying, updating, deleting context items."""

    def test_add_and_get(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        ctx_id = cm.add_context("lang", "python", category="skill",
                                privacy_tier=PrivacyTier.L1_PUBLIC)
        item = cm.get_context(ctx_id)
        assert item is not None
        assert item["key"] == "lang"
        assert item["value"] == "python"
        assert item["category"] == "skill"
        assert item["privacy_tier"] == PrivacyTier.L1_PUBLIC
        cm.close()

    def test_add_json_value(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        ctx_id = cm.add_context("tech_stack", ["python", "rust", "go"],
                                category="skill")
        item = cm.get_context(ctx_id)
        assert item["value"] == ["python", "rust", "go"]
        cm.close()

    def test_update_context(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        ctx_id = cm.add_context("lang", "python")
        cm.update_context(ctx_id, "rust")
        item = cm.get_context(ctx_id)
        assert item["value"] == "rust"
        assert item["version"] == 2
        cm.close()

    def test_delete_context(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        ctx_id = cm.add_context("temp", "data")
        cm.delete_context(ctx_id)
        assert cm.get_context(ctx_id) is None
        cm.close()

    def test_query_by_category(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        cm.add_context("python", "3.12", category="skill")
        cm.add_context("rust", "1.75", category="skill")
        cm.add_context("dark_mode", True, category="preference")

        skills = cm.query_context(category="skill")
        assert len(skills) == 2
        prefs = cm.query_context(category="preference")
        assert len(prefs) == 1
        cm.close()

    def test_query_by_privacy_max(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        cm.add_context("public_info", "hi", privacy_tier=PrivacyTier.L1_PUBLIC)
        cm.add_context("trusted_info", "secret", privacy_tier=PrivacyTier.L2_TRUSTED)
        cm.add_context("private_info", "very_secret", privacy_tier=PrivacyTier.L3_PRIVATE)

        public_only = cm.query_context(privacy_max=PrivacyTier.L1_PUBLIC)
        assert len(public_only) == 1
        assert public_only[0]["key"] == "public_info"

        up_to_trusted = cm.query_context(privacy_max=PrivacyTier.L2_TRUSTED)
        assert len(up_to_trusted) == 2
        cm.close()

    def test_query_by_tags(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        cm.add_context("python", "3.12", tags=["backend", "scripting"])
        cm.add_context("react", "18", tags=["frontend"])
        cm.add_context("rust", "1.75", tags=["backend", "systems"])

        backend = cm.query_context(tags=["backend"])
        assert len(backend) == 2
        cm.close()


class TestProjection:
    """Test task-relevant context projection."""

    def _setup_contexts(self, cm):
        cm.add_context("python", "3.12", category="skill",
                       privacy_tier=PrivacyTier.L1_PUBLIC)
        cm.add_context("code_style", "functional", category="preference",
                       privacy_tier=PrivacyTier.L2_TRUSTED)
        cm.add_context("api_key", "sk-xxx", category="credential",
                       privacy_tier=PrivacyTier.L3_PRIVATE)
        cm.add_context("project_name", "CoWorker", category="project",
                       privacy_tier=PrivacyTier.L1_PUBLIC)

    def test_untrusted_gets_nothing(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        self._setup_contexts(cm)
        items = cm.project_for_task("code_review", peer_trust_tier=0)
        assert items == []
        cm.close()

    def test_known_gets_l1_only(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        self._setup_contexts(cm)
        items = cm.project_for_task("code_review", peer_trust_tier=1)
        keys = {i["key"] for i in items}
        assert "python" in keys
        assert "project_name" in keys
        assert "code_style" not in keys  # L2
        assert "api_key" not in keys  # L3
        cm.close()

    def test_internal_gets_l1_and_l2(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        self._setup_contexts(cm)
        items = cm.project_for_task("code_review", peer_trust_tier=2)
        keys = {i["key"] for i in items}
        assert "python" in keys
        assert "code_style" in keys  # L2 visible to INTERNAL
        assert "api_key" not in keys  # L3 NEVER shared
        cm.close()

    def test_l3_never_shared_even_privileged(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        self._setup_contexts(cm)
        items = cm.project_for_task("code_review", peer_trust_tier=3)
        keys = {i["key"] for i in items}
        assert "api_key" not in keys  # L3 = NEVER
        cm.close()

    def test_projection_respects_task_categories(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        cm.add_context("python", "3.12", category="skill",
                       privacy_tier=PrivacyTier.L1_PUBLIC)
        cm.add_context("dark_mode", True, category="preference",
                       privacy_tier=PrivacyTier.L1_PUBLIC)
        cm.add_context("other", "data", category="general",
                       privacy_tier=PrivacyTier.L1_PUBLIC)

        # "echo" task type only needs "skill" category
        items = cm.project_for_task("echo", peer_trust_tier=2)
        keys = {i["key"] for i in items}
        assert "python" in keys
        assert "dark_mode" not in keys  # preference not relevant to echo
        cm.close()

    def test_projection_max_items(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        for i in range(20):
            cm.add_context(f"skill_{i}", f"value_{i}", category="skill",
                          privacy_tier=PrivacyTier.L1_PUBLIC)

        items = cm.project_for_task("echo", peer_trust_tier=2, max_items=5)
        assert len(items) <= 5
        cm.close()

    def test_projection_strips_internal_fields(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        cm.add_context("python", "3.12", category="skill",
                       privacy_tier=PrivacyTier.L1_PUBLIC)

        items = cm.project_for_task("echo", peer_trust_tier=1)
        assert len(items) == 1
        # Should only have these fields, not privacy_tier, tags, etc.
        assert set(items[0].keys()) == {"context_id", "key", "value", "category"}
        cm.close()


class TestPeerContext:
    """Test storing and querying context received from peers."""

    def test_store_and_query_peer_context(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        items = [
            {"context_id": "ctx_1", "key": "lang", "value": "rust", "category": "skill"},
            {"context_id": "ctx_2", "key": "os", "value": "linux", "category": "preference"},
        ]
        count = cm.store_peer_context("icy", items, correlation_id="corr_001")
        assert count == 2

        result = cm.query_peer_context(peer_id="icy")
        assert len(result) == 2

        result_skill = cm.query_peer_context(peer_id="icy", category="skill")
        assert len(result_skill) == 1
        assert result_skill[0]["key"] == "lang"
        cm.close()


class TestContextSyncPayload:
    """Test protocol payload builders."""

    def test_sync_payload_respects_privacy(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        cm.add_context("public", "data", privacy_tier=PrivacyTier.L1_PUBLIC)
        cm.add_context("secret", "data", privacy_tier=PrivacyTier.L3_PRIVATE)

        payload = cm.build_context_sync_payload(peer_trust_tier=1)
        keys = {i["key"] for i in payload["items"]}
        assert "public" in keys
        assert "secret" not in keys
        cm.close()

    def test_sync_payload_untrusted_gets_empty(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        cm.add_context("data", "value", privacy_tier=PrivacyTier.L1_PUBLIC)

        payload = cm.build_context_sync_payload(peer_trust_tier=0)
        assert payload["items"] == []
        cm.close()

    def test_response_payload_shows_filtered_count(self, tmp_data_dir):
        cm = ContextManager(tmp_data_dir)
        cm.add_context("pub", "1", privacy_tier=PrivacyTier.L1_PUBLIC)
        cm.add_context("priv", "2", privacy_tier=PrivacyTier.L3_PRIVATE)

        query = {"categories": None, "max_items": 10}
        payload = cm.build_context_response_payload(query, peer_trust_tier=1)
        assert payload["total_available"] == 2
        assert len(payload["items"]) == 1  # only public
        assert payload["filtered_by_trust"] == 1  # 1 filtered out
        cm.close()


class TestCleanup:
    """Test expired context cleanup."""

    def test_cleanup_expired(self, tmp_data_dir):
        from datetime import datetime, timezone, timedelta
        cm = ContextManager(tmp_data_dir)

        # Add one expired, one not expired
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        cm.add_context("old", "data", expires_at=past)
        cm.add_context("new", "data", expires_at=future)

        removed = cm.cleanup_expired()
        assert removed == 1

        # Only "new" should remain
        items = cm.query_context(include_expired=True)
        assert len(items) == 1
        assert items[0]["key"] == "new"
        cm.close()
