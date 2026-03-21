"""Tests for LLM Projection Engine — both LLM and fallback paths."""

import json
import pytest
from unittest.mock import MagicMock, patch

from llm_projection import LLMProjectionEngine, ProjectionResult


SAMPLE_ITEMS = [
    {"context_id": "ctx_1", "key": "python", "value": "3.12", "category": "skill"},
    {"context_id": "ctx_2", "key": "rust", "value": "1.75", "category": "skill"},
    {"context_id": "ctx_3", "key": "dark_mode", "value": True, "category": "preference"},
    {"context_id": "ctx_4", "key": "project_name", "value": "CoWorker", "category": "project"},
    {"context_id": "ctx_5", "key": "coding_style", "value": "functional", "category": "preference"},
]


class TestFallbackProjection:
    """Test fallback (no LLM) behavior."""

    def test_fallback_without_api_key(self):
        engine = LLMProjectionEngine(api_key="")
        assert engine.is_available is False

        result = engine.project(
            task_description="review code",
            task_type="code_review",
            available_items=SAMPLE_ITEMS,
        )
        assert result.method == "fallback"
        # code_review → skill, project, preference
        assert len(result.selected_items) > 0

    def test_fallback_selects_correct_categories(self):
        engine = LLMProjectionEngine(api_key="")
        result = engine.project(
            task_description="echo test",
            task_type="echo",
            available_items=SAMPLE_ITEMS,
        )
        # echo → only "skill" category
        categories = {item["category"] for item in result.selected_items}
        assert categories == {"skill"}

    def test_fallback_default_task_type(self):
        engine = LLMProjectionEngine(api_key="")
        result = engine.project(
            task_description="unknown task",
            task_type="never_heard_of_this",
            available_items=SAMPLE_ITEMS,
        )
        # default → skill, general
        assert result.method == "fallback"

    def test_empty_items(self):
        engine = LLMProjectionEngine(api_key="")
        result = engine.project(
            task_description="test",
            task_type="echo",
            available_items=[],
        )
        assert result.selected_items == []
        assert result.method == "empty"


class TestLLMResponseParsing:
    """Test JSON extraction and response parsing."""

    def test_extract_clean_json(self):
        engine = LLMProjectionEngine(api_key="")
        data = engine._extract_json('{"selected": [], "overall_rationale": "test"}')
        assert data["overall_rationale"] == "test"

    def test_extract_json_from_code_block(self):
        engine = LLMProjectionEngine(api_key="")
        text = '```json\n{"selected": [{"id": "ctx_1", "reason": "relevant"}], "overall_rationale": "ok"}\n```'
        data = engine._extract_json(text)
        assert len(data["selected"]) == 1

    def test_extract_json_with_surrounding_text(self):
        engine = LLMProjectionEngine(api_key="")
        text = 'Here is my analysis:\n{"selected": [], "overall_rationale": "none"}\nDone.'
        data = engine._extract_json(text)
        assert data["selected"] == []

    def test_invalid_json_raises(self):
        engine = LLMProjectionEngine(api_key="")
        with pytest.raises(json.JSONDecodeError):
            engine._extract_json("this is not json at all")

    def test_parse_response_filters_invalid_ids(self):
        engine = LLMProjectionEngine(api_key="")
        response = {
            "content": json.dumps({
                "selected": [
                    {"id": "ctx_1", "reason": "valid"},
                    {"id": "ctx_FAKE", "reason": "invalid id"},
                ],
                "overall_rationale": "test",
            }),
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        ids, rationale, usage = engine._parse_response(response, SAMPLE_ITEMS)
        assert "ctx_1" in ids
        assert "ctx_FAKE" not in ids

    def test_parse_response_handles_malformed(self):
        engine = LLMProjectionEngine(api_key="")
        response = {
            "content": "I cannot help with that",
            "usage": {},
        }
        # S4: fail-closed — malformed response returns empty (not all items)
        ids, rationale, usage = engine._parse_response(response, SAMPLE_ITEMS)
        assert len(ids) == 0
        assert "fail-closed" in rationale


class TestItemFormatting:
    """Test prompt formatting."""

    def test_format_items(self):
        engine = LLMProjectionEngine(api_key="")
        formatted = engine._format_items_for_prompt(SAMPLE_ITEMS[:2])
        assert "ctx_1" in formatted
        assert "python" in formatted
        assert "skill" in formatted

    def test_long_values_truncated(self):
        engine = LLMProjectionEngine(api_key="")
        items = [{"context_id": "ctx_long", "key": "data",
                  "value": "x" * 200, "category": "general"}]
        formatted = engine._format_items_for_prompt(items)
        assert "..." in formatted
        assert len(formatted) < 300


class TestContextManagerIntegration:
    """Test LLM engine integration with ContextManager."""

    def test_project_uses_llm_when_set(self, tmp_data_dir):
        from context_manager import ContextManager, PrivacyTier

        cm = ContextManager(tmp_data_dir)
        cm.add_context("python", "3.12", category="skill",
                       privacy_tier=PrivacyTier.L1_PUBLIC)
        cm.add_context("react", "18", category="skill",
                       privacy_tier=PrivacyTier.L1_PUBLIC)
        cm.add_context("secret", "hidden", category="credential",
                       privacy_tier=PrivacyTier.L3_PRIVATE)

        # Create a mock LLM engine that selects only python
        mock_engine = MagicMock()
        mock_engine.project.return_value = ProjectionResult(
            selected_items=[
                {"context_id": "any", "key": "python", "value": "3.12", "category": "skill"}
            ],
            rationale="Python is relevant to code review",
            method="llm",
        )

        cm.set_llm_engine(mock_engine)

        result = cm.project_for_task(
            "code_review", peer_trust_tier=1,
            task_description="review Python code",
        )

        # LLM engine should have been called
        mock_engine.project.assert_called_once()
        # Should get the mock's selection
        assert len(result) == 1
        # L3 secret should NOT have been passed to LLM
        call_items = mock_engine.project.call_args[1]["available_items"]
        item_keys = {i["key"] for i in call_items}
        assert "secret" not in item_keys
        cm.close()

    def test_project_falls_back_without_engine(self, tmp_data_dir):
        from context_manager import ContextManager, PrivacyTier

        cm = ContextManager(tmp_data_dir)
        cm.add_context("python", "3.12", category="skill",
                       privacy_tier=PrivacyTier.L1_PUBLIC)

        # No engine set → should use fallback
        result = cm.project_for_task("echo", peer_trust_tier=1)
        assert len(result) == 1  # "skill" category matches "echo"
        cm.close()

    def test_projection_audit_logged(self, tmp_data_dir):
        from context_manager import ContextManager, PrivacyTier

        cm = ContextManager(tmp_data_dir)
        cm.add_context("python", "3.12", category="skill",
                       privacy_tier=PrivacyTier.L1_PUBLIC)

        cm.project_for_task("echo", peer_trust_tier=1)

        # Check audit log
        cur = cm.conn.cursor()
        cur.execute("SELECT * FROM context_projections")
        rows = cur.fetchall()
        assert len(rows) == 1
        cm.close()
