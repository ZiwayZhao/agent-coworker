"""Tests for InboxStore and OutboxStore — message persistence."""

import pytest
from store import InboxStore, OutboxStore


class TestInboxStore:
    """Test inbox message storage and querying."""

    def test_save_and_query(self, tmp_data_dir, sample_message):
        inbox = InboxStore(tmp_data_dir)
        saved = inbox.save(sample_message)
        assert saved is True

        msgs = inbox.query()
        assert len(msgs) == 1
        assert msgs[0]["msg_type"] == "ping"
        assert msgs[0]["sender_id"] == "test_peer"
        inbox.close()

    def test_duplicate_rejected(self, tmp_data_dir, sample_message):
        inbox = InboxStore(tmp_data_dir)
        inbox.save(sample_message)
        saved_again = inbox.save(sample_message)
        assert saved_again is False
        assert inbox.count() == 1
        inbox.close()

    def test_query_by_status(self, tmp_data_dir, sample_message):
        inbox = InboxStore(tmp_data_dir)
        inbox.save(sample_message)

        new_msgs = inbox.query(status="new")
        assert len(new_msgs) == 1

        processed = inbox.query(status="processed")
        assert len(processed) == 0
        inbox.close()

    def test_mark_processed(self, tmp_data_dir, sample_message):
        inbox = InboxStore(tmp_data_dir)
        inbox.save(sample_message)

        msg_id = sample_message["_xmtp_id"]
        inbox.mark_processed(msg_id)

        new_msgs = inbox.query(status="new")
        assert len(new_msgs) == 0

        processed = inbox.query(status="processed")
        assert len(processed) == 1
        assert processed[0]["processed_at"] is not None
        inbox.close()

    def test_query_by_type(self, tmp_data_dir, make_message):
        inbox = InboxStore(tmp_data_dir)
        inbox.save(make_message(msg_type="ping", correlation_id="c1"))
        inbox.save(make_message(msg_type="task_request", correlation_id="c2"))

        pings = inbox.query(msg_type="ping")
        assert len(pings) == 1
        inbox.close()

    def test_query_by_sender(self, tmp_data_dir, make_message):
        inbox = InboxStore(tmp_data_dir)
        inbox.save(make_message(sender_id="alice", correlation_id="c1"))
        inbox.save(make_message(sender_id="bob", correlation_id="c2"))

        alice_msgs = inbox.query(sender_id="alice")
        assert len(alice_msgs) == 1
        inbox.close()

    def test_get_by_correlation(self, tmp_data_dir, make_message):
        inbox = InboxStore(tmp_data_dir)
        inbox.save(make_message(correlation_id="corr_xyz"))
        inbox.save(make_message(correlation_id="corr_abc"))

        result = inbox.get_by_correlation("corr_xyz")
        assert len(result) == 1
        inbox.close()

    def test_count(self, tmp_data_dir, make_message):
        inbox = InboxStore(tmp_data_dir)
        # Each message needs a unique _xmtp_id (used as primary key)
        inbox.save(make_message(sender_id="a", correlation_id="c1"))
        inbox.save(make_message(sender_id="b", correlation_id="c2"))
        inbox.save(make_message(sender_id="c", correlation_id="c3"))

        assert inbox.count() == 3
        assert inbox.count(status="new") == 3
        inbox.close()


class TestOutboxStore:
    """Test outbox message storage."""

    def test_record_and_query(self, tmp_data_dir):
        outbox = OutboxStore(tmp_data_dir)
        outbox.record(
            recipient_wallet="0xABC",
            msg_type="pong",
            payload={"message": "pong!"},
            bridge_response={"messageId": "mid_1", "conversationId": "cid_1"},
            correlation_id="corr_001",
        )

        msgs = outbox.query()
        assert len(msgs) == 1
        assert msgs[0]["msg_type"] == "pong"
        assert msgs[0]["recipient_wallet"] == "0xABC"
        assert msgs[0]["status"] == "sent"
        outbox.close()

    def test_mark_acked(self, tmp_data_dir):
        outbox = OutboxStore(tmp_data_dir)
        outbox.record(
            recipient_wallet="0xABC",
            msg_type="task_request",
            payload={"skill": "echo"},
            bridge_response={"messageId": "mid_2", "conversationId": "cid_2"},
            correlation_id="corr_task",
        )

        outbox.mark_acked("corr_task")
        msgs = outbox.query(status="acked")
        assert len(msgs) == 1
        assert msgs[0]["acked_at"] is not None
        outbox.close()

    def test_count(self, tmp_data_dir):
        outbox = OutboxStore(tmp_data_dir)
        for i in range(3):
            outbox.record(
                recipient_wallet=f"0x{i}",
                msg_type="ping",
                payload={},
                bridge_response={"messageId": f"m{i}", "conversationId": f"c{i}"},
            )

        assert outbox.count() == 3
        assert outbox.count(status="sent") == 3
        outbox.close()
