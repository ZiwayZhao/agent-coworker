"""Tests for error recovery — timeouts, dedup, malformed messages, persistence."""

import json
import os
import sys
import time

import pytest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"
)
sys.path.insert(0, SCRIPTS_DIR)

from session import SessionManager, SessionState
from store import InboxStore
from agentfax_client import build_message, parse_message


@pytest.mark.unit
def test_timeout_returns_none(tmp_path):
    """expire_stale_sessions() expires sessions past their TTL."""
    data_dir = str(tmp_path / "agentfax")
    os.makedirs(data_dir, exist_ok=True)
    sm = SessionManager(data_dir)

    sid = sm.create_session(
        peer_id="peer_timeout",
        proposed_skills=["echo"],
        ttl_seconds=1,
    )
    # Wait for TTL to elapse
    time.sleep(1.1)

    expired_count = sm.expire_stale_sessions()
    assert expired_count >= 1

    session = sm.get_session(sid)
    assert session["state"] == SessionState.EXPIRED.value
    sm.close()


@pytest.mark.unit
def test_session_ttl_expires(tmp_path):
    """expire_session() directly transitions to EXPIRED."""
    data_dir = str(tmp_path / "agentfax")
    os.makedirs(data_dir, exist_ok=True)
    sm = SessionManager(data_dir)

    sid = sm.create_session(
        peer_id="peer_expire",
        proposed_skills=["echo"],
        ttl_seconds=3600,
    )
    result = sm.expire_session(sid)
    assert result is True

    session = sm.get_session(sid)
    assert session["state"] == SessionState.EXPIRED.value
    sm.close()


@pytest.mark.unit
def test_duplicate_message_dedup(tmp_path):
    """InboxStore deduplicates messages by msg_id (_xmtp_id)."""
    data_dir = str(tmp_path / "agentfax")
    os.makedirs(data_dir, exist_ok=True)
    inbox = InboxStore(data_dir)

    msg = build_message("ping", {"message": "hello"}, sender_id="peer_a")
    msg["_xmtp_id"] = "dedup_test_001"

    saved_first = inbox.save(msg)
    saved_second = inbox.save(msg)

    assert saved_first is True
    assert saved_second is False
    assert inbox.count() == 1
    inbox.close()


@pytest.mark.unit
def test_malformed_message_rejected():
    """parse_message handles malformed input gracefully."""
    # Empty string
    assert parse_message("") is None

    # Valid JSON but missing "protocol" field
    assert parse_message('{"type": "ping"}') is None

    # Valid JSON, wrong protocol
    assert parse_message('{"protocol": "other", "type": "ping"}') is None

    # Valid AgentFax message with different version still parses
    result = parse_message(json.dumps({
        "protocol": "agentfax",
        "version": "99.0",
        "type": "ping",
        "payload": {},
    }))
    assert result is not None
    assert result["version"] == "99.0"

    # Non-JSON garbage
    assert parse_message("not json at all {{{") is None

    # None input
    assert parse_message(None) is None


@pytest.mark.unit
def test_session_state_persists_restart(tmp_path):
    """Sessions persist across SessionManager restarts (SQLite)."""
    data_dir = str(tmp_path / "agentfax")
    os.makedirs(data_dir, exist_ok=True)

    # Create and accept a session
    sm1 = SessionManager(data_dir)
    sid = sm1.create_session(
        peer_id="peer_persist",
        proposed_skills=["echo", "summarize"],
        proposed_trust_tier=2,
        ttl_seconds=3600,
    )
    sm1.accept_session(sid, agreed_skills=["echo", "summarize"])
    session1 = sm1.get_session(sid)
    assert session1["state"] == SessionState.ACTIVE.value
    sm1.close()

    # "Restart" — new SessionManager pointing to the same data_dir
    sm2 = SessionManager(data_dir)
    session2 = sm2.get_session(sid)
    assert session2 is not None
    assert session2["state"] == SessionState.ACTIVE.value
    assert session2["peer_id"] == "peer_persist"
    assert json.loads(session2["agreed_skills"]) == ["echo", "summarize"]
    sm2.close()
