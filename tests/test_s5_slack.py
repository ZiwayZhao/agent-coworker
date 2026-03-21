"""Tests for S5: Slack integration + productization."""

import json
import os
import pytest
from unittest.mock import patch, MagicMock

from slack_notifier import (
    SlackNotifier,
    _header_block, _section_block, _fields_block, _context_block,
    _trust_tier_label, _privacy_label, _truncate, _sanitize_error,
    build_skill_card_blocks, build_session_timeline_blocks,
)


# ── Block Kit builder tests ──────────────────────────────────────


class TestBlockBuilders:
    """Test Slack Block Kit helper functions."""

    def test_header_block(self):
        b = _header_block("Hello World")
        assert b["type"] == "header"
        assert b["text"]["type"] == "plain_text"
        assert b["text"]["text"] == "Hello World"

    def test_header_block_truncates(self):
        b = _header_block("x" * 200)
        assert len(b["text"]["text"]) <= 150

    def test_section_block(self):
        b = _section_block("*bold text*")
        assert b["type"] == "section"
        assert b["text"]["type"] == "mrkdwn"
        assert "*bold text*" in b["text"]["text"]

    def test_fields_block(self):
        b = _fields_block(["*A:* 1", "*B:* 2"])
        assert b["type"] == "section"
        assert len(b["fields"]) == 2
        assert b["fields"][0]["type"] == "mrkdwn"

    def test_context_block(self):
        b = _context_block(["item1", "item2"])
        assert b["type"] == "context"
        assert len(b["elements"]) == 2

    def test_trust_tier_label(self):
        assert "UNTRUSTED" in _trust_tier_label(0)
        assert "KNOWN" in _trust_tier_label(1)
        assert "INTERNAL" in _trust_tier_label(2)
        assert "PRIVILEGED" in _trust_tier_label(3)
        assert "SYSTEM" in _trust_tier_label(4)

    def test_privacy_label(self):
        assert "Public" in _privacy_label("L1_PUBLIC")
        assert "Trusted" in _privacy_label("L2_TRUSTED")
        assert "Private" in _privacy_label("L3_PRIVATE")

    def test_truncate(self):
        assert _truncate("short") == "short"
        long_text = "x" * 200
        result = _truncate(long_text, 100)
        assert len(result) <= 103  # 100 + "..."
        assert result.endswith("...")

    def test_sanitize_error_redacts_api_keys(self):
        assert "REDACTED" in _sanitize_error("Failed: sk-abc1234567890xyz")

    def test_sanitize_error_redacts_tokens(self):
        assert "REDACTED" in _sanitize_error("Auth: xoxb-12345-abcdef")

    def test_sanitize_error_redacts_paths(self):
        assert "REDACTED" in _sanitize_error("File not found: /Users/ziway/secret.txt")

    def test_sanitize_error_preserves_safe_text(self):
        safe = "Skill echo not found"
        assert _sanitize_error(safe) == safe

    def test_sanitize_error_truncates(self):
        result = _sanitize_error("x" * 500)
        assert len(result) <= 303


class TestBuildSkillCardBlocks:
    """Test Skill Card → Slack blocks conversion."""

    def test_minimal_card(self):
        card = {
            "skill_name": "echo",
            "version": "1.0.0",
            "description": "Echo input back",
        }
        blocks = build_skill_card_blocks(card)
        assert len(blocks) >= 1
        # Should contain skill name somewhere
        text = json.dumps(blocks)
        assert "echo" in text

    def test_full_card(self):
        card = {
            "skill_name": "word_count",
            "version": "1.0.0",
            "description": "Count words in text",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "min_trust_tier": 1,
            "max_context_privacy_tier": "L2_TRUSTED",
            "tags": ["text", "nlp"],
        }
        blocks = build_skill_card_blocks(card)
        text = json.dumps(blocks)
        assert "word_count" in text


class TestBuildSessionTimelineBlocks:
    """Test session → timeline Slack blocks."""

    def test_active_session(self):
        session = {
            "session_id": "sess_123",
            "peer_id": "icy",
            "state": "active",
            "agreed_trust_tier": 2,
            "agreed_max_context_privacy": "L2_TRUSTED",
            "agreed_max_calls": 10,
            "call_count": 3,
            "tasks_completed": 2,
            "tasks_failed": 0,
            "created_at": "2026-03-17T10:00:00Z",
            "accepted_at": "2026-03-17T10:00:01Z",
        }
        blocks = build_session_timeline_blocks(session)
        text = json.dumps(blocks)
        assert "icy" in text
        assert "active" in text.lower()

    def test_closed_session(self):
        session = {
            "session_id": "sess_456",
            "peer_id": "remote_agent",
            "state": "completed",
            "agreed_trust_tier": 1,
            "agreed_max_context_privacy": "L1_PUBLIC",
            "agreed_max_calls": 5,
            "call_count": 5,
            "tasks_completed": 5,
            "tasks_failed": 0,
            "created_at": "2026-03-17T10:00:00Z",
            "accepted_at": "2026-03-17T10:00:01Z",
            "closed_at": "2026-03-17T11:00:00Z",
        }
        blocks = build_session_timeline_blocks(session)
        text = json.dumps(blocks)
        assert "completed" in text.lower()


# ── SlackNotifier configuration tests ────────────────────────────


class TestSlackNotifierConfig:
    """Test SlackNotifier initialization and configuration."""

    def test_disabled_without_config(self, tmp_data_dir):
        notifier = SlackNotifier(tmp_data_dir)
        assert notifier.enabled is False
        assert notifier.stats == {"sent": 0, "errors": 0}

    def test_enabled_with_webhook_config(self, tmp_data_dir):
        config = {"webhook_url": "https://hooks.slack.com/test"}
        with open(os.path.join(tmp_data_dir, "slack_config.json"), "w") as f:
            json.dump(config, f)
        notifier = SlackNotifier(tmp_data_dir)
        assert notifier.enabled is True

    def test_enabled_with_env_var(self, tmp_data_dir):
        with patch.dict(os.environ, {"AGENTFAX_SLACK_WEBHOOK": "https://hooks.slack.com/env"}):
            notifier = SlackNotifier(tmp_data_dir)
            assert notifier.enabled is True

    def test_env_overrides_config(self, tmp_data_dir):
        config = {"webhook_url": "https://hooks.slack.com/file"}
        with open(os.path.join(tmp_data_dir, "slack_config.json"), "w") as f:
            json.dump(config, f)
        with patch.dict(os.environ, {"AGENTFAX_SLACK_WEBHOOK": "https://hooks.slack.com/env"}):
            notifier = SlackNotifier(tmp_data_dir)
            assert notifier._config["webhook_url"] == "https://hooks.slack.com/env"

    def test_custom_notify_events(self, tmp_data_dir):
        config = {
            "webhook_url": "https://hooks.slack.com/test",
            "notify_events": ["session"],
        }
        with open(os.path.join(tmp_data_dir, "slack_config.json"), "w") as f:
            json.dump(config, f)
        notifier = SlackNotifier(tmp_data_dir)
        assert notifier._should_notify("session") is True
        assert notifier._should_notify("task") is False

    def test_bad_config_file(self, tmp_data_dir):
        with open(os.path.join(tmp_data_dir, "slack_config.json"), "w") as f:
            f.write("not json")
        notifier = SlackNotifier(tmp_data_dir)
        assert notifier.enabled is False

    def test_close_is_safe(self, tmp_data_dir):
        notifier = SlackNotifier(tmp_data_dir)
        notifier.close()  # Should not raise


# ── Notification method tests (mocked HTTP) ──────────────────────


class TestNotifications:
    """Test notification methods with mocked HTTP."""

    @pytest.fixture
    def notifier(self, tmp_data_dir):
        config = {
            "webhook_url": "https://hooks.slack.com/test",
            "notify_events": ["session", "task", "trust", "workflow"],
        }
        with open(os.path.join(tmp_data_dir, "slack_config.json"), "w") as f:
            json.dump(config, f)
        return SlackNotifier(tmp_data_dir)

    @patch("slack_notifier.urllib.request.urlopen")
    def test_notify_session_proposed(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        notifier.notify_session_proposed(
            peer_id="icy", skills=["echo", "reverse"],
            trust_tier=1, session_id="sess_001",
        )
        assert mock_urlopen.called
        assert notifier.stats["sent"] == 1

    @patch("slack_notifier.urllib.request.urlopen")
    def test_notify_session_accepted(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        session = {
            "session_id": "sess_001", "peer_id": "icy",
            "agreed_skills": ["echo"], "agreed_trust_tier": 2,
            "agreed_max_context_privacy": "L2_TRUSTED",
            "agreed_max_calls": 10,
        }
        notifier.notify_session_accepted(session)
        assert mock_urlopen.called

    @patch("slack_notifier.urllib.request.urlopen")
    def test_notify_session_rejected(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        notifier.notify_session_rejected(
            peer_id="bad_peer", reason="untrusted", session_id="sess_002",
        )
        assert mock_urlopen.called

    @patch("slack_notifier.urllib.request.urlopen")
    def test_notify_task_accepted(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        notifier.notify_task_accepted(task_id="task_001", skill="echo", sender="icy")
        assert mock_urlopen.called

    @patch("slack_notifier.urllib.request.urlopen")
    def test_notify_task_completed(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        notifier.notify_task_completed(
            task_id="task_001", skill="echo", sender="icy", duration_ms=42.0,
        )
        assert mock_urlopen.called

    @patch("slack_notifier.urllib.request.urlopen")
    def test_notify_task_failed(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        notifier.notify_task_failed(
            task_id="task_001", skill="echo", sender="icy",
            error_code="EXECUTION_FAILED", error_message="kaboom",
        )
        assert mock_urlopen.called

    @patch("slack_notifier.urllib.request.urlopen")
    def test_notify_trust_change(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        notifier.notify_trust_change(peer_id="icy", old_tier=0, new_tier=1)
        assert mock_urlopen.called

    @patch("slack_notifier.urllib.request.urlopen")
    def test_notify_workflow_started(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        notifier.notify_workflow_started(
            workflow_id="wf_001", name="test_flow", total_steps=3,
        )
        assert mock_urlopen.called

    @patch("slack_notifier.urllib.request.urlopen")
    def test_notify_skill_card(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        notifier.notify_skill_card(
            card={"skill_name": "echo", "version": "1.0.0", "description": "test"},
            context="Skill registered",
        )
        assert mock_urlopen.called


class TestNotificationFiltering:
    """Test event filtering and disabled state."""

    def test_disabled_notifier_skips_all(self, tmp_data_dir):
        notifier = SlackNotifier(tmp_data_dir)
        assert notifier.enabled is False
        # Should not raise, just silently skip
        notifier.notify_task_accepted("t", "s", "p")
        notifier.notify_session_proposed("p", [], 0, "s")
        assert notifier.stats["sent"] == 0

    def test_filtered_event_skips(self, tmp_data_dir):
        config = {
            "webhook_url": "https://hooks.slack.com/test",
            "notify_events": ["session"],  # task disabled
        }
        with open(os.path.join(tmp_data_dir, "slack_config.json"), "w") as f:
            json.dump(config, f)
        notifier = SlackNotifier(tmp_data_dir)

        with patch("slack_notifier.urllib.request.urlopen") as mock:
            notifier.notify_task_accepted("t", "s", "p")
            assert not mock.called  # task events disabled

    @patch("slack_notifier.urllib.request.urlopen")
    def test_http_error_increments_error_count(self, mock_urlopen, tmp_data_dir):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("timeout")

        config = {"webhook_url": "https://hooks.slack.com/test"}
        with open(os.path.join(tmp_data_dir, "slack_config.json"), "w") as f:
            json.dump(config, f)
        notifier = SlackNotifier(tmp_data_dir)
        notifier.notify_task_accepted("t", "s", "p")
        assert notifier.stats["errors"] == 1
        assert notifier.stats["sent"] == 0


class TestBotTokenAPI:
    """Test bot token (chat.postMessage) path."""

    @patch("slack_notifier.urllib.request.urlopen")
    def test_uses_api_when_token_and_channel(self, mock_urlopen, tmp_data_dir):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"ok":true}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        config = {
            "bot_token": "xoxb-test-token",
            "channel": "#test",
            "notify_events": ["task"],
        }
        with open(os.path.join(tmp_data_dir, "slack_config.json"), "w") as f:
            json.dump(config, f)
        notifier = SlackNotifier(tmp_data_dir)
        notifier.notify_task_accepted("t", "echo", "icy")

        # Verify the request was to Slack API, not webhook
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "slack.com/api" in req.full_url
        assert req.get_header("Authorization") == "Bearer xoxb-test-token"


# ── Handler integration tests ────────────────────────────────────


class TestHandlerSlackWiring:
    """Test that handlers correctly call SlackNotifier methods."""

    @pytest.fixture
    def mock_ctx(self):
        """Build a RouterContext-like mock with slack_notifier."""
        ctx = MagicMock()
        ctx.slack_notifier = MagicMock()
        ctx.trust_manager = MagicMock()
        ctx.trust_manager.get_trust_tier.return_value = 2
        ctx.reputation_manager = MagicMock()
        ctx.context_manager = None  # Disable context projection for simplicity
        ctx.session_manager = None
        ctx.metering_manager = None
        ctx.workflow_manager = None
        ctx.client = MagicMock()
        return ctx

    def test_task_handler_notifies_on_accept_complete_fail(self, mock_ctx):
        from router import MessageRouter
        from task_manager import TaskManager
        from executor import TaskExecutor, register_builtin_skills
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            router = MessageRouter()
            tm = TaskManager(td)
            executor = TaskExecutor()
            register_builtin_skills(executor)

            from handlers.task_handler import register_task_handlers
            register_task_handlers(router, tm, executor)

            # Simulate successful task
            msg = {
                "type": "task_request",
                "sender_id": "icy",
                "payload": {
                    "skill": "echo",
                    "input": {"text": "hi"},
                    "task_id": "t_slack_1",
                },
                "correlation_id": "c_slack_1",
                "_xmtp_sender_wallet": "0xICY",
            }
            response = router.dispatch(msg, mock_ctx)
            assert response["type"] == "task_response"
            assert response["payload"]["status"] == "completed"

            # Verify slack_notifier was called for accept + completed
            mock_ctx.slack_notifier.notify_task_accepted.assert_called_once()
            mock_ctx.slack_notifier.notify_task_completed.assert_called_once()

    def test_task_handler_notifies_on_failure(self, mock_ctx):
        from router import MessageRouter
        from task_manager import TaskManager
        from executor import TaskExecutor

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            router = MessageRouter()
            tm = TaskManager(td)
            executor = TaskExecutor()
            # Register a skill that always fails
            def _fail(inp):
                raise RuntimeError("boom")
            executor.register_skill(
                name="fail_skill",
                func=_fail,
                description="always fails",
            )

            from handlers.task_handler import register_task_handlers
            register_task_handlers(router, tm, executor)

            msg = {
                "type": "task_request",
                "sender_id": "icy",
                "payload": {
                    "skill": "fail_skill",
                    "input": {},
                    "task_id": "t_slack_fail",
                },
                "correlation_id": "c_slack_fail",
                "_xmtp_sender_wallet": "0xICY",
            }
            response = router.dispatch(msg, mock_ctx)
            assert response["type"] == "task_error"
            mock_ctx.slack_notifier.notify_task_failed.assert_called_once()

    def test_session_handler_notifies_on_propose(self, mock_ctx):
        from router import MessageRouter
        from session import SessionManager
        from executor import TaskExecutor, register_builtin_skills

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            router = MessageRouter()
            sm = SessionManager(td)
            executor = TaskExecutor()
            register_builtin_skills(executor)

            from handlers.session_handler import register_session_handlers
            register_session_handlers(router, sm, executor)

            msg = {
                "type": "session_propose",
                "sender_id": "icy",
                "payload": {
                    "proposed_skills": ["echo"],
                    "proposed_trust_tier": 1,
                    "proposed_max_context_privacy": "L1_PUBLIC",
                    "proposed_max_calls": 10,
                    "ttl_seconds": 3600,
                    "session_id": "remote_sess_001",
                },
                "_xmtp_sender_wallet": "0xICY",
                "correlation_id": "c_sess_1",
            }
            response = router.dispatch(msg, mock_ctx)
            assert response["type"] == "session_accept"

            # Both proposed and accepted notifications
            mock_ctx.slack_notifier.notify_session_proposed.assert_called_once()
            mock_ctx.slack_notifier.notify_session_accepted.assert_called_once()
