#!/usr/bin/env python3
"""Tests for S3: Reliable transport + metering — usage receipts, outbox retry, task schema."""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts"))

from metering import MeteringManager
from store import OutboxStore
from task_manager import TaskManager
from router import MessageRouter, RouterContext
from executor import TaskExecutor, register_builtin_skills
from security import TrustTier


# ── MeteringManager ──────────────────────────────────────────

class TestMeteringManager(unittest.TestCase):
    """Test usage receipt creation and queries."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mm = MeteringManager(self.tmpdir)

    def tearDown(self):
        self.mm.close()

    def test_create_receipt(self):
        rid = self.mm.create_receipt(
            task_id="task_001",
            caller="icy",
            provider="ziway",
            skill_name="echo",
            status="completed",
            duration_ms=150,
            input_size_bytes=64,
            output_size_bytes=64,
        )
        self.assertTrue(rid.startswith("rcpt_"))
        receipt = self.mm.get_receipt(rid)
        self.assertEqual(receipt["task_id"], "task_001")
        self.assertEqual(receipt["caller"], "icy")
        self.assertEqual(receipt["provider"], "ziway")
        self.assertEqual(receipt["skill_name"], "echo")
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["duration_ms"], 150)
        self.assertEqual(receipt["input_size_bytes"], 64)
        self.assertEqual(receipt["output_size_bytes"], 64)
        self.assertEqual(receipt["pricing_model"], "free")
        self.assertEqual(receipt["amount"], 0)

    def test_create_receipt_with_session(self):
        rid = self.mm.create_receipt(
            task_id="task_002",
            caller="icy",
            provider="ziway",
            skill_name="reverse",
            status="completed",
            session_id="sess_abc",
            skill_version="1.0.0",
            duration_ms=200,
        )
        receipt = self.mm.get_receipt(rid)
        self.assertEqual(receipt["session_id"], "sess_abc")
        self.assertEqual(receipt["skill_version"], "1.0.0")

    def test_create_receipt_failed(self):
        rid = self.mm.create_receipt(
            task_id="task_003",
            caller="icy",
            provider="ziway",
            skill_name="echo",
            status="failed",
            input_size_bytes=128,
        )
        receipt = self.mm.get_receipt(rid)
        self.assertEqual(receipt["status"], "failed")
        self.assertIsNone(receipt["duration_ms"])

    def test_get_by_task(self):
        self.mm.create_receipt(
            task_id="task_004", caller="a", provider="b",
            skill_name="echo", status="completed",
        )
        receipt = self.mm.get_by_task("task_004")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["task_id"], "task_004")

    def test_get_by_task_not_found(self):
        self.assertIsNone(self.mm.get_by_task("nonexistent"))

    def test_list_receipts_no_filter(self):
        for i in range(3):
            self.mm.create_receipt(
                task_id=f"task_{i}", caller="a", provider="b",
                skill_name="echo", status="completed",
            )
        results = self.mm.list_receipts()
        self.assertEqual(len(results), 3)

    def test_list_receipts_by_session(self):
        self.mm.create_receipt(
            task_id="t1", caller="a", provider="b",
            skill_name="echo", status="completed", session_id="sess_1",
        )
        self.mm.create_receipt(
            task_id="t2", caller="a", provider="b",
            skill_name="echo", status="completed", session_id="sess_2",
        )
        results = self.mm.list_receipts(session_id="sess_1")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["task_id"], "t1")

    def test_list_receipts_by_caller(self):
        self.mm.create_receipt(
            task_id="t1", caller="icy", provider="ziway",
            skill_name="echo", status="completed",
        )
        self.mm.create_receipt(
            task_id="t2", caller="bob", provider="ziway",
            skill_name="echo", status="completed",
        )
        results = self.mm.list_receipts(caller="icy")
        self.assertEqual(len(results), 1)

    def test_list_receipts_by_status(self):
        self.mm.create_receipt(
            task_id="t1", caller="a", provider="b",
            skill_name="echo", status="completed",
        )
        self.mm.create_receipt(
            task_id="t2", caller="a", provider="b",
            skill_name="echo", status="failed",
        )
        results = self.mm.list_receipts(status="failed")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["task_id"], "t2")

    def test_count(self):
        self.assertEqual(self.mm.count(), 0)
        self.mm.create_receipt(
            task_id="t1", caller="a", provider="b",
            skill_name="echo", status="completed",
        )
        self.assertEqual(self.mm.count(), 1)
        self.assertEqual(self.mm.count("completed"), 1)
        self.assertEqual(self.mm.count("failed"), 0)


class TestMeteringSessionSummary(unittest.TestCase):
    """Test session-level usage summaries."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mm = MeteringManager(self.tmpdir)

    def tearDown(self):
        self.mm.close()

    def test_session_summary_basic(self):
        self.mm.create_receipt(
            task_id="t1", caller="icy", provider="ziway",
            skill_name="echo", status="completed",
            session_id="sess_1", duration_ms=100,
            input_size_bytes=50, output_size_bytes=50,
        )
        self.mm.create_receipt(
            task_id="t2", caller="icy", provider="ziway",
            skill_name="reverse", status="completed",
            session_id="sess_1", duration_ms=200,
            input_size_bytes=100, output_size_bytes=80,
        )
        self.mm.create_receipt(
            task_id="t3", caller="icy", provider="ziway",
            skill_name="echo", status="failed",
            session_id="sess_1", input_size_bytes=30,
        )

        summary = self.mm.get_session_summary("sess_1")
        self.assertEqual(summary["total_calls"], 3)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["total_duration_ms"], 300)
        self.assertEqual(summary["total_input_bytes"], 180)
        self.assertEqual(summary["total_output_bytes"], 130)
        self.assertIn("echo", summary["skills_used"])
        self.assertIn("reverse", summary["skills_used"])

    def test_session_summary_empty(self):
        summary = self.mm.get_session_summary("nonexistent")
        self.assertEqual(summary["total_calls"], 0)
        self.assertEqual(summary["completed"], 0)

    def test_peer_summary(self):
        self.mm.create_receipt(
            task_id="t1", caller="icy", provider="ziway",
            skill_name="echo", status="completed", duration_ms=100,
        )
        self.mm.create_receipt(
            task_id="t2", caller="icy", provider="ziway",
            skill_name="echo", status="failed",
        )
        summary = self.mm.get_peer_summary("icy", role="caller")
        self.assertEqual(summary["total_calls"], 2)
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["failed"], 1)


# ── OutboxStore retry ────────────────────────────────────────

class TestOutboxRetry(unittest.TestCase):
    """Test outbox retry mechanics."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.outbox = OutboxStore(self.tmpdir)

    def tearDown(self):
        self.outbox.close()

    def test_record_pending(self):
        row_id = self.outbox.record_pending(
            recipient_wallet="0xabc",
            msg_type="task_response",
            payload={"task_id": "t1", "output": "hello"},
            correlation_id="corr_1",
        )
        self.assertIsInstance(row_id, int)

        # Should show up in retryable (next_retry_at is 5s in future,
        # but we can check the status is pending)
        rows = self.outbox.query(status="pending")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["msg_type"], "task_response")
        self.assertEqual(rows[0]["recipient_wallet"], "0xabc")

    def test_get_retryable_respects_time(self):
        row_id = self.outbox.record_pending(
            recipient_wallet="0xabc",
            msg_type="test",
            payload={"foo": "bar"},
        )
        # next_retry_at is 5 seconds in future, so nothing should be retryable now
        retryable = self.outbox.get_retryable()
        self.assertEqual(len(retryable), 0)

        # Manually set next_retry_at to the past
        self.outbox.conn.execute(
            "UPDATE sent_messages SET next_retry_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), row_id),
        )
        self.outbox.conn.commit()

        retryable = self.outbox.get_retryable()
        self.assertEqual(len(retryable), 1)

    def test_mark_retry_sent(self):
        row_id = self.outbox.record_pending(
            recipient_wallet="0xabc",
            msg_type="test",
            payload={},
        )
        # Must claim first (pending → retrying)
        self.outbox.conn.execute(
            "UPDATE sent_messages SET status = 'retrying' WHERE id = ?", (row_id,)
        )
        self.outbox.conn.commit()
        self.outbox.mark_retry_sent(row_id, {
            "messageId": "msg_123",
            "conversationId": "conv_456",
        })
        rows = self.outbox.query(status="sent")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], "msg_123")

    def test_mark_retry_failed_with_backoff(self):
        row_id = self.outbox.record_pending(
            recipient_wallet="0xabc",
            msg_type="test",
            payload={},
            max_retries=3,
        )
        # Simulate claim (pending → retrying)
        self.outbox.conn.execute(
            "UPDATE sent_messages SET status = 'retrying' WHERE id = ?", (row_id,)
        )
        self.outbox.conn.commit()
        # First failure
        self.outbox.mark_retry_failed(row_id, "connection refused")
        row = self.outbox.conn.execute(
            "SELECT * FROM sent_messages WHERE id = ?", (row_id,)
        ).fetchone()
        self.assertEqual(row["retry_count"], 1)
        self.assertEqual(row["status"], "pending")  # Back to pending for next retry
        self.assertEqual(row["last_error"], "connection refused")

    def test_mark_retry_failed_gives_up(self):
        row_id = self.outbox.record_pending(
            recipient_wallet="0xabc",
            msg_type="test",
            payload={},
            max_retries=2,
        )
        # Simulate two retry cycles: claim → fail → claim → fail
        self.outbox.conn.execute(
            "UPDATE sent_messages SET status = 'retrying' WHERE id = ?", (row_id,)
        )
        self.outbox.conn.commit()
        self.outbox.mark_retry_failed(row_id, "error 1")
        # Second cycle
        self.outbox.conn.execute(
            "UPDATE sent_messages SET status = 'retrying' WHERE id = ?", (row_id,)
        )
        self.outbox.conn.commit()
        self.outbox.mark_retry_failed(row_id, "error 2")
        row = self.outbox.conn.execute(
            "SELECT * FROM sent_messages WHERE id = ?", (row_id,)
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["retry_count"], 2)

    def test_mark_acked_clears_pending(self):
        """Ack should work on pending messages too (e.g. retried before ack)."""
        row_id = self.outbox.record_pending(
            recipient_wallet="0xabc",
            msg_type="test",
            payload={},
            correlation_id="corr_x",
        )
        self.outbox.mark_acked("corr_x")
        row = self.outbox.conn.execute(
            "SELECT * FROM sent_messages WHERE id = ?", (row_id,)
        ).fetchone()
        self.assertEqual(row["status"], "acked")

    def test_mark_acked_clears_retrying(self):
        """Ack should supersede retrying status (late ack during retry)."""
        row_id = self.outbox.record_pending(
            recipient_wallet="0xabc",
            msg_type="test",
            payload={},
            correlation_id="corr_y",
        )
        self.outbox.conn.execute(
            "UPDATE sent_messages SET status = 'retrying' WHERE id = ?", (row_id,)
        )
        self.outbox.conn.commit()
        self.outbox.mark_acked("corr_y")
        row = self.outbox.conn.execute(
            "SELECT * FROM sent_messages WHERE id = ?", (row_id,)
        ).fetchone()
        self.assertEqual(row["status"], "acked")

    def test_recover_stale_retrying(self):
        """Stale retrying rows should be recovered to pending."""
        row_id = self.outbox.record_pending(
            recipient_wallet="0xabc",
            msg_type="test",
            payload={},
        )
        # Simulate claimed but worker crashed — set status=retrying, sent_at in past
        past = datetime.fromtimestamp(
            time.time() - 120, tz=timezone.utc
        ).isoformat()
        self.outbox.conn.execute(
            "UPDATE sent_messages SET status = 'retrying', sent_at = ? WHERE id = ?",
            (past, row_id),
        )
        self.outbox.conn.commit()

        recovered = self.outbox.recover_stale_retrying(stale_seconds=60)
        self.assertEqual(recovered, 1)
        row = self.outbox.conn.execute(
            "SELECT * FROM sent_messages WHERE id = ?", (row_id,)
        ).fetchone()
        self.assertEqual(row["status"], "pending")


# ── TaskManager S3 columns ───────────────────────────────────

class TestTaskManagerS3(unittest.TestCase):
    """Test new session_id, skill_version, receipt_id columns."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tm = TaskManager(self.tmpdir)

    def tearDown(self):
        self.tm.close()

    def test_set_session_id(self):
        self.tm.receive_task(
            task_id="t1", skill="echo",
            input_data={}, peer_wallet="0xabc",
        )
        self.tm.set_session_id("t1", "sess_abc")
        task = self.tm.get_task("t1")
        self.assertEqual(task["session_id"], "sess_abc")

    def test_set_receipt_id(self):
        self.tm.receive_task(
            task_id="t2", skill="echo",
            input_data={}, peer_wallet="0xabc",
        )
        self.tm.set_receipt_id("t2", "rcpt_123")
        task = self.tm.get_task("t2")
        self.assertEqual(task["receipt_id"], "rcpt_123")

    def test_new_columns_default_null(self):
        self.tm.receive_task(
            task_id="t3", skill="echo",
            input_data={}, peer_wallet="0xabc",
        )
        task = self.tm.get_task("t3")
        self.assertIsNone(task["session_id"])
        self.assertIsNone(task["skill_version"])
        self.assertIsNone(task["receipt_id"])


# ── Task handler metering integration ────────────────────────

class TestTaskHandlerMetering(unittest.TestCase):
    """Test that task_handler creates usage receipts on complete/fail."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.router = MessageRouter()
        self.tm = TaskManager(self.tmpdir)
        self.executor = TaskExecutor()
        register_builtin_skills(self.executor)
        self.mm = MeteringManager(self.tmpdir)

        # Mock client
        self.client = MagicMock()
        self.client._sender_id = "ziway"

        # Mock trust manager — allow everything
        self.trust_manager = MagicMock()
        self.trust_manager.get_trust_tier.return_value = 2

        from handlers.task_handler import register_task_handlers
        register_task_handlers(self.router, self.tm, self.executor)

        self.ctx = RouterContext(
            client=self.client,
            trust_manager=self.trust_manager,
            metering_manager=self.mm,
        )

    def tearDown(self):
        self.mm.close()
        self.tm.close()

    def _make_task_request(self, skill, input_data, task_id=None, session_id=None):
        payload = {"skill": skill, "input": input_data}
        if task_id:
            payload["task_id"] = task_id
        if session_id:
            payload["session_id"] = session_id
        return {
            "type": "task_request",
            "payload": payload,
            "sender_id": "icy",
            "_xmtp_sender_wallet": "0xicy",
            "correlation_id": f"corr_{task_id or 'auto'}",
        }

    def test_successful_task_creates_receipt(self):
        msg = self._make_task_request("echo", {"text": "hello"}, task_id="mt_001")
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_response")
        self.assertEqual(result["payload"]["status"], "completed")

        # Check usage info in response
        self.assertIn("usage", result["payload"])
        usage = result["payload"]["usage"]
        self.assertTrue(usage["receipt_id"].startswith("rcpt_"))
        self.assertGreaterEqual(usage["input_size_bytes"], 1)
        self.assertGreaterEqual(usage["output_size_bytes"], 1)

        # Check receipt in DB
        receipt = self.mm.get_by_task("mt_001")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["caller"], "icy")
        self.assertEqual(receipt["provider"], "ziway")
        self.assertEqual(receipt["skill_name"], "echo")
        self.assertEqual(receipt["status"], "completed")

        # Check task has receipt_id
        task = self.tm.get_task("mt_001")
        self.assertEqual(task["receipt_id"], receipt["receipt_id"])

    def test_failed_task_creates_receipt(self):
        # Use a skill that will fail — nonexistent skill won't create receipt
        # (it fails before execution). Instead, mock executor to fail.
        self.executor.register_skill(
            name="fail_skill",
            func=lambda inp: (_ for _ in ()).throw(ValueError("boom")),
            description="always fails",
        )
        msg = self._make_task_request("fail_skill", {}, task_id="mt_002")
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_error")

        receipt = self.mm.get_by_task("mt_002")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["status"], "failed")

    def test_task_with_session_sets_session_id(self):
        msg = self._make_task_request(
            "echo", {"text": "hi"}, task_id="mt_003", session_id="sess_x"
        )
        # No session_manager → session check skipped but session_id still recorded
        self.router.dispatch(msg, self.ctx)

        task = self.tm.get_task("mt_003")
        self.assertEqual(task["session_id"], "sess_x")

        receipt = self.mm.get_by_task("mt_003")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["session_id"], "sess_x")

    def test_receipt_not_created_when_no_metering_manager(self):
        ctx_no_meter = RouterContext(
            client=self.client,
            trust_manager=self.trust_manager,
        )
        msg = self._make_task_request("echo", {"text": "hi"}, task_id="mt_004")
        result = self.router.dispatch(msg, ctx_no_meter)
        self.assertEqual(result["type"], "task_response")
        # No usage in response
        self.assertNotIn("usage", result["payload"])

    def test_skill_not_found_no_receipt(self):
        """If skill doesn't exist, no receipt is created (pre-execution error)."""
        msg = self._make_task_request("nonexistent_skill", {}, task_id="mt_005")
        result = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result["type"], "task_error")
        self.assertEqual(result["payload"]["error_code"], "SKILL_NOT_FOUND")

        receipt = self.mm.get_by_task("mt_005")
        self.assertIsNone(receipt)

    def test_dedup_returns_cached_with_usage(self):
        """Duplicate request returns cached response (with usage info)."""
        msg = self._make_task_request("echo", {"text": "hi"}, task_id="mt_006")
        result1 = self.router.dispatch(msg, self.ctx)
        result2 = self.router.dispatch(msg, self.ctx)
        # Both should have same receipt
        self.assertEqual(
            result1["payload"].get("usage", {}).get("receipt_id"),
            result2["payload"].get("usage", {}).get("receipt_id"),
        )

    def test_persistent_dedup_after_cache_miss(self):
        """If dedup cache is empty but task_id exists in DB, don't re-execute."""
        msg = self._make_task_request("echo", {"text": "persistent"}, task_id="mt_007")
        # First execution
        result1 = self.router.dispatch(msg, self.ctx)
        self.assertEqual(result1["type"], "task_response")

        # Simulate dedup cache loss by using a fresh router+handler
        from handlers.task_handler import register_task_handlers
        router2 = MessageRouter()
        register_task_handlers(router2, self.tm, self.executor)

        # Same task_id → should return stored result, not re-execute
        result2 = router2.dispatch(msg, self.ctx)
        self.assertEqual(result2["type"], "task_response")
        self.assertEqual(result2["payload"]["status"], "completed")

        # Only one receipt should exist (no double-billing)
        receipt = self.mm.get_by_task("mt_007")
        self.assertIsNotNone(receipt)
        receipts = self.mm.list_receipts()
        task_007_receipts = [r for r in receipts if r["task_id"] == "mt_007"]
        self.assertEqual(len(task_007_receipts), 1)


# ── Outbox + daemon retry integration ────────────────────────

class TestDaemonRetryIntegration(unittest.TestCase):
    """Test daemon._retry_pending_sends logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.outbox = OutboxStore(self.tmpdir)

    def tearDown(self):
        self.outbox.close()

    def test_retry_flow_full_cycle(self):
        """Simulate: record pending → atomic claim → retry succeeds."""
        row_id = self.outbox.record_pending(
            recipient_wallet="0xpeer",
            msg_type="task_response",
            payload={"task_id": "t1", "output": "result"},
            correlation_id="corr_1",
        )
        # Make it retryable now
        self.outbox.conn.execute(
            "UPDATE sent_messages SET next_retry_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), row_id),
        )
        self.outbox.conn.commit()

        # get_retryable atomically claims (pending → retrying)
        retryable = self.outbox.get_retryable()
        self.assertEqual(len(retryable), 1)
        self.assertEqual(retryable[0]["correlation_id"], "corr_1")
        self.assertEqual(retryable[0]["status"], "retrying")

        # Simulate successful retry
        self.outbox.mark_retry_sent(row_id, {"messageId": "new_msg_id"})

        # No more retryable
        retryable = self.outbox.get_retryable()
        self.assertEqual(len(retryable), 0)

        # Should be in sent status
        sent = self.outbox.query(status="sent")
        self.assertEqual(len(sent), 1)

    def test_atomic_claim_prevents_double_retry(self):
        """Two get_retryable calls shouldn't return the same row."""
        row_id = self.outbox.record_pending(
            recipient_wallet="0xpeer",
            msg_type="test",
            payload={},
        )
        self.outbox.conn.execute(
            "UPDATE sent_messages SET next_retry_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), row_id),
        )
        self.outbox.conn.commit()

        # First claim
        batch1 = self.outbox.get_retryable()
        self.assertEqual(len(batch1), 1)
        # Second claim — nothing left
        batch2 = self.outbox.get_retryable()
        self.assertEqual(len(batch2), 0)


if __name__ == "__main__":
    unittest.main()
