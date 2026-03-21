"""Stress tests — high volume sessions, messages, and large payloads."""

import json
import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"
)
sys.path.insert(0, SCRIPTS_DIR)

from session import SessionManager, SessionState
from store import InboxStore
from task_manager import TaskManager
from agentfax_client import build_message


@pytest.mark.stress
def test_50_concurrent_sessions(tmp_path):
    """Create 50 sessions, accept all, verify all are active."""
    data_dir = str(tmp_path / "agentfax")
    os.makedirs(data_dir, exist_ok=True)
    sm = SessionManager(data_dir)

    session_ids = []
    for i in range(50):
        sid = sm.create_session(
            peer_id=f"peer_{i}",
            proposed_skills=["echo"],
            proposed_trust_tier=1,
            ttl_seconds=3600,
        )
        session_ids.append(sid)

    # Accept all sessions
    for sid in session_ids:
        ok = sm.accept_session(sid, agreed_skills=["echo"])
        assert ok, f"Failed to accept session {sid}"

    # Verify all are active
    active = sm.list_sessions(state="active", limit=100)
    assert len(active) == 50

    for session in active:
        assert session["state"] == SessionState.ACTIVE.value

    sm.close()


@pytest.mark.stress
def test_1000_messages(tmp_path):
    """Store 1000 messages in InboxStore, verify count."""
    data_dir = str(tmp_path / "agentfax")
    os.makedirs(data_dir, exist_ok=True)
    inbox = InboxStore(data_dir)

    for i in range(1000):
        msg = build_message(
            "ping",
            {"message": f"hello_{i}"},
            sender_id=f"peer_{i % 10}",
        )
        msg["_xmtp_id"] = f"msg_stress_{i:04d}"
        saved = inbox.save(msg)
        assert saved is True, f"Message {i} was not saved (duplicate?)"

    assert inbox.count() == 1000

    # Query a subset
    from_peer_0 = inbox.query(sender_id="peer_0")
    assert len(from_peer_0) == 100

    inbox.close()


@pytest.mark.stress
def test_large_payload(tmp_path):
    """TaskManager handles 100KB input and output payloads."""
    data_dir = str(tmp_path / "agentfax")
    os.makedirs(data_dir, exist_ok=True)
    tm = TaskManager(data_dir)

    large_input = {"data": "x" * 102400}
    large_result = {"output": "y" * 102400}

    task_id = tm.create_task(
        skill="big_task",
        input_data=large_input,
        peer_wallet="0xLARGE",
        timeout_seconds=600,
    )

    tm.accept_task(task_id)
    tm.start_task(task_id)
    tm.complete_task(task_id, result=large_result)

    task = tm.get_task(task_id)
    assert task["state"] == "completed"
    assert task["input_data"] == large_input
    assert task["output_data"] == large_result
    assert len(task["input_data"]["data"]) == 102400
    assert len(task["output_data"]["output"]) == 102400

    tm.close()
