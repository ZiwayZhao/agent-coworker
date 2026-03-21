"""Tests for AgentFax Client — protocol envelope, parsing, expiry, and bridge communication."""

import json
import time
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from agentfax_client import (
    build_message, parse_message, is_expired,
    AgentFaxClient, PROTOCOL_NAME, PROTOCOL_VERSION,
    _read_bridge_port, _bridge_url,
)


# ── Protocol envelope tests ──────────────────────────────────────


class TestBuildMessage:
    """Test build_message() envelope construction."""

    def test_minimal_envelope(self):
        msg = build_message("ping", {"hello": "world"})
        assert msg["protocol"] == PROTOCOL_NAME
        assert msg["version"] == PROTOCOL_VERSION
        assert msg["type"] == "ping"
        assert msg["payload"] == {"hello": "world"}
        assert msg["ttl"] == 3600
        assert "timestamp" in msg

    def test_timestamp_is_utc_iso(self):
        msg = build_message("ping", {})
        ts = datetime.fromisoformat(msg["timestamp"])
        assert ts.tzinfo is not None  # timezone-aware

    def test_sender_id_included(self):
        msg = build_message("ping", {}, sender_id="ziway")
        assert msg["sender_id"] == "ziway"

    def test_sender_id_omitted_when_none(self):
        msg = build_message("ping", {})
        assert "sender_id" not in msg

    def test_correlation_id(self):
        msg = build_message("ping", {}, correlation_id="corr_123")
        assert msg["correlation_id"] == "corr_123"

    def test_custom_ttl(self):
        msg = build_message("ping", {}, ttl=60)
        assert msg["ttl"] == 60

    def test_v11_trace_fields(self):
        msg = build_message(
            "ping", {},
            trace_id="trace_abc",
            span_id="span_123",
            parent_span_id="span_000",
        )
        assert msg["trace_id"] == "trace_abc"
        assert msg["span_id"] == "span_123"
        assert msg["parent_span_id"] == "span_000"

    def test_v11_optional_fields_omitted_when_none(self):
        msg = build_message("ping", {})
        assert "trace_id" not in msg
        assert "span_id" not in msg
        assert "context" not in msg
        assert "trust_required" not in msg
        assert "priority" not in msg

    def test_v11_context_and_priority(self):
        msg = build_message(
            "task_request", {"skill": "echo"},
            context={"key": "value"},
            trust_required="INTERNAL",
            priority="high",
        )
        assert msg["context"] == {"key": "value"}
        assert msg["trust_required"] == "INTERNAL"
        assert msg["priority"] == "high"

    def test_empty_payload(self):
        msg = build_message("ping", {})
        assert msg["payload"] == {}

    def test_complex_payload(self):
        payload = {
            "nested": {"deep": [1, 2, 3]},
            "unicode": "你好世界",
            "bool": True,
            "null": None,
        }
        msg = build_message("task_request", payload)
        assert msg["payload"] == payload


class TestParseMessage:
    """Test parse_message() — raw JSON to AgentFax envelope."""

    def test_valid_agentfax_message(self):
        raw = json.dumps({
            "protocol": "agentfax",
            "version": "1.0",
            "type": "ping",
            "payload": {},
        })
        msg = parse_message(raw)
        assert msg is not None
        assert msg["type"] == "ping"

    def test_non_agentfax_protocol(self):
        raw = json.dumps({"protocol": "other", "type": "test"})
        assert parse_message(raw) is None

    def test_missing_protocol_field(self):
        raw = json.dumps({"type": "ping", "payload": {}})
        assert parse_message(raw) is None

    def test_invalid_json(self):
        assert parse_message("not json at all") is None

    def test_empty_string(self):
        assert parse_message("") is None

    def test_none_input(self):
        assert parse_message(None) is None

    def test_json_array_not_dict(self):
        assert parse_message("[1, 2, 3]") is None

    def test_preserves_all_fields(self):
        raw = json.dumps({
            "protocol": "agentfax",
            "version": "1.0",
            "type": "task_request",
            "payload": {"skill": "echo"},
            "sender_id": "ziway",
            "correlation_id": "corr_1",
            "timestamp": "2026-03-15T00:00:00+00:00",
            "ttl": 7200,
        })
        msg = parse_message(raw)
        assert msg["sender_id"] == "ziway"
        assert msg["correlation_id"] == "corr_1"
        assert msg["ttl"] == 7200


class TestIsExpired:
    """Test is_expired() TTL checking."""

    def test_fresh_message_not_expired(self):
        msg = build_message("ping", {}, ttl=3600)
        assert is_expired(msg) is False

    def test_old_message_expired(self):
        msg = build_message("ping", {}, ttl=1)
        # Fake old timestamp
        msg["timestamp"] = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        assert is_expired(msg) is True

    def test_missing_timestamp_not_expired(self):
        msg = {"type": "ping", "payload": {}}
        assert is_expired(msg) is False

    def test_malformed_timestamp_not_expired(self):
        msg = {"type": "ping", "timestamp": "not-a-date", "ttl": 60}
        assert is_expired(msg) is False

    def test_default_ttl_3600(self):
        msg = {
            "type": "ping",
            "timestamp": (
                datetime.now(timezone.utc) - timedelta(seconds=3000)
            ).isoformat(),
        }
        # No ttl field → default 3600s → 3000s age → not expired
        assert is_expired(msg) is False

    def test_zero_ttl_always_expired(self):
        msg = build_message("ping", {}, ttl=0)
        # Even a fresh message with ttl=0 is expired (age > 0)
        assert is_expired(msg) is True


# ── Bridge communication tests (mocked HTTP) ─────────────────────


class TestBridgePort:
    """Test bridge port file reading."""

    def test_read_port_from_file(self, tmp_data_dir):
        port_file = f"{tmp_data_dir}/bridge_port"
        with open(port_file, "w") as f:
            f.write("3500\n")
        assert _read_bridge_port(tmp_data_dir) == 3500

    def test_missing_port_file_raises(self, tmp_data_dir):
        with pytest.raises(FileNotFoundError):
            _read_bridge_port(tmp_data_dir)

    def test_bridge_url_construction(self, tmp_data_dir):
        with open(f"{tmp_data_dir}/bridge_port", "w") as f:
            f.write("4200")
        url = _bridge_url(tmp_data_dir, "/health")
        assert url == "http://localhost:4200/health"


class TestClientSend:
    """Test AgentFaxClient.send() with mocked bridge."""

    def _make_client(self, tmp_data_dir):
        # Write minimal config
        with open(f"{tmp_data_dir}/config.json", "w") as f:
            json.dump({"peer_id": "test_agent"}, f)
        with open(f"{tmp_data_dir}/bridge_port", "w") as f:
            f.write("3500")
        return AgentFaxClient(tmp_data_dir)

    @patch("agentfax_client._bridge_post")
    def test_send_builds_envelope(self, mock_post, tmp_data_dir):
        mock_post.return_value = {"messageId": "msg_123"}
        client = self._make_client(tmp_data_dir)

        result = client.send("0xABC", "ping", {"hello": "world"})

        assert result["messageId"] == "msg_123"
        # Verify what was posted to bridge
        call_args = mock_post.call_args[0]
        assert call_args[1] == "/send"
        body = call_args[2]
        assert body["to"] == "0xABC"
        envelope = json.loads(body["content"])
        assert envelope["protocol"] == "agentfax"
        assert envelope["type"] == "ping"
        assert envelope["sender_id"] == "test_agent"

    @patch("agentfax_client._bridge_post")
    def test_send_with_correlation_id(self, mock_post, tmp_data_dir):
        mock_post.return_value = {"messageId": "msg_456"}
        client = self._make_client(tmp_data_dir)

        client.send("0xABC", "task_request", {}, correlation_id="corr_xyz")

        body = mock_post.call_args[0][2]
        envelope = json.loads(body["content"])
        assert envelope["correlation_id"] == "corr_xyz"

    @patch("agentfax_client._bridge_post")
    def test_ping_convenience(self, mock_post, tmp_data_dir):
        mock_post.return_value = {"messageId": "msg_789"}
        client = self._make_client(tmp_data_dir)

        client.ping("0xABC")

        body = mock_post.call_args[0][2]
        envelope = json.loads(body["content"])
        assert envelope["type"] == "ping"
        assert "test_agent" in envelope["payload"]["message"]

    @patch("agentfax_client._bridge_post")
    def test_broadcast_sends_to_multiple(self, mock_post, tmp_data_dir):
        mock_post.return_value = {"results": []}
        client = self._make_client(tmp_data_dir)

        client.broadcast(["0xA", "0xB"], "ping", {"msg": "hi"})

        body = mock_post.call_args[0][2]
        assert body["to"] == ["0xA", "0xB"]
        assert mock_post.call_args[0][1] == "/broadcast"


class TestClientReceive:
    """Test AgentFaxClient.receive() with mocked bridge."""

    def _make_client(self, tmp_data_dir):
        with open(f"{tmp_data_dir}/config.json", "w") as f:
            json.dump({"peer_id": "test_agent"}, f)
        with open(f"{tmp_data_dir}/bridge_port", "w") as f:
            f.write("3500")
        return AgentFaxClient(tmp_data_dir)

    @patch("agentfax_client._bridge_get")
    def test_receive_parses_agentfax_messages(self, mock_get, tmp_data_dir):
        envelope = build_message("ping", {"msg": "hello"}, sender_id="icy")
        mock_get.return_value = {
            "messages": [
                {
                    "content": json.dumps(envelope),
                    "contentType": "text",
                    "id": "xmtp_1",
                    "senderInboxId": "inbox_icy",
                    "sentAt": "2026-03-15T00:00:00Z",
                }
            ]
        }
        client = self._make_client(tmp_data_dir)
        messages = client.receive()

        assert len(messages) == 1
        assert messages[0]["type"] == "ping"
        assert messages[0]["sender_id"] == "icy"
        assert messages[0]["_xmtp_id"] == "xmtp_1"

    @patch("agentfax_client._bridge_get")
    def test_receive_filters_non_agentfax(self, mock_get, tmp_data_dir):
        mock_get.return_value = {
            "messages": [
                {"content": "just plain text", "contentType": "text"},
                {"content": json.dumps({"not": "agentfax"}), "contentType": "text"},
            ]
        }
        client = self._make_client(tmp_data_dir)
        messages = client.receive()
        assert len(messages) == 0

    @patch("agentfax_client._bridge_get")
    def test_receive_filters_expired(self, mock_get, tmp_data_dir):
        old_msg = build_message("ping", {}, ttl=1)
        old_msg["timestamp"] = (
            datetime.now(timezone.utc) - timedelta(seconds=100)
        ).isoformat()

        mock_get.return_value = {
            "messages": [
                {"content": json.dumps(old_msg), "contentType": "text"}
            ]
        }
        client = self._make_client(tmp_data_dir)
        messages = client.receive()
        assert len(messages) == 0  # expired, filtered out

    @patch("agentfax_client._bridge_get")
    def test_receive_handles_attachments(self, mock_get, tmp_data_dir):
        mock_get.return_value = {
            "messages": [
                {
                    "contentType": "attachment",
                    "content": "base64data",
                    "attachment": {"filename": "test.pdf"},
                    "sentAt": "2026-03-15T00:00:00Z",
                }
            ]
        }
        client = self._make_client(tmp_data_dir)
        messages = client.receive()

        assert len(messages) == 1
        assert messages[0]["type"] == "attachment_received"
        assert messages[0]["payload"]["attachment"]["filename"] == "test.pdf"

    @patch("agentfax_client._bridge_get")
    def test_receive_empty_inbox(self, mock_get, tmp_data_dir):
        mock_get.return_value = {"messages": []}
        client = self._make_client(tmp_data_dir)
        assert client.receive() == []

    @patch("agentfax_client._bridge_get")
    def test_receive_mixed_valid_and_invalid(self, mock_get, tmp_data_dir):
        valid = build_message("pong", {"msg": "ok"}, sender_id="icy")
        mock_get.return_value = {
            "messages": [
                {"content": json.dumps(valid), "contentType": "text"},
                {"content": "garbage", "contentType": "text"},
                {"content": json.dumps({"protocol": "other"}), "contentType": "text"},
            ]
        }
        client = self._make_client(tmp_data_dir)
        messages = client.receive()
        assert len(messages) == 1
        assert messages[0]["type"] == "pong"


class TestClientSendFile:
    """Test file sending."""

    def _make_client(self, tmp_data_dir):
        with open(f"{tmp_data_dir}/config.json", "w") as f:
            json.dump({"peer_id": "test_agent"}, f)
        with open(f"{tmp_data_dir}/bridge_port", "w") as f:
            f.write("3500")
        return AgentFaxClient(tmp_data_dir)

    @patch("agentfax_client._bridge_post")
    def test_send_file_posts_attachment_and_notification(self, mock_post, tmp_data_dir):
        mock_post.return_value = {"messageId": "msg_file"}
        client = self._make_client(tmp_data_dir)

        # Create test file
        test_file = f"{tmp_data_dir}/test.txt"
        with open(test_file, "w") as f:
            f.write("hello world")

        client.send_file("0xABC", test_file)

        # Should call bridge twice: attachment + protocol notification
        assert mock_post.call_count == 2
        # First call: attachment
        assert mock_post.call_args_list[0][0][1] == "/send-attachment"
        # Second call: protocol notification
        assert mock_post.call_args_list[1][0][1] == "/send"

    def test_send_file_not_found(self, tmp_data_dir):
        client = self._make_client(tmp_data_dir)
        with pytest.raises(FileNotFoundError):
            client.send_file("0xABC", "/nonexistent/file.txt")

    @patch("agentfax_client._bridge_post")
    def test_send_file_too_large(self, mock_post, tmp_data_dir):
        client = self._make_client(tmp_data_dir)
        large_file = f"{tmp_data_dir}/big.bin"
        with open(large_file, "wb") as f:
            f.write(b"x" * 1_100_000)  # > 1MB

        with pytest.raises(ValueError, match="too large"):
            client.send_file("0xABC", large_file)


class TestClientIdentity:
    """Test agent identity loading."""

    def test_loads_from_chain_identity(self, tmp_data_dir):
        with open(f"{tmp_data_dir}/chain_identity.json", "w") as f:
            json.dump({"claw_name": "ziway", "agent_id": 1735}, f)
        with open(f"{tmp_data_dir}/bridge_port", "w") as f:
            f.write("3500")

        client = AgentFaxClient(tmp_data_dir)
        assert client._sender_id == "ziway"

    def test_loads_from_config(self, tmp_data_dir):
        with open(f"{tmp_data_dir}/config.json", "w") as f:
            json.dump({"peer_id": "my_agent"}, f)
        with open(f"{tmp_data_dir}/bridge_port", "w") as f:
            f.write("3500")

        client = AgentFaxClient(tmp_data_dir)
        assert client._sender_id == "my_agent"

    def test_defaults_to_unknown(self, tmp_data_dir):
        with open(f"{tmp_data_dir}/bridge_port", "w") as f:
            f.write("3500")

        client = AgentFaxClient(tmp_data_dir)
        assert client._sender_id == "unknown"
