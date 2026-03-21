"""AgentFax Persistent Store — SQLite-backed inbox/outbox."""

import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger("agentfax.store")


class InboxStore:
    """Persistent storage for received AgentFax messages."""

    def __init__(self, data_dir: str):
        self.data_dir = str(Path(data_dir).expanduser())
        db_path = os.path.join(self.data_dir, "agentfax_inbox.db")
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                sender_inbox_id TEXT,
                sender_id TEXT,
                conversation_id TEXT,
                content_type TEXT DEFAULT 'text',
                raw_content TEXT,
                msg_type TEXT,
                payload TEXT,
                correlation_id TEXT,
                sent_at TEXT,
                received_at TEXT,
                status TEXT DEFAULT 'new',
                processed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
            CREATE INDEX IF NOT EXISTS idx_messages_msg_type ON messages(msg_type);
            CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender_id);
            CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages(received_at);
        """)
        self.conn.commit()

    def save(self, msg: dict) -> bool:
        msg_id = msg.get("_xmtp_id") or f"local_{datetime.now(timezone.utc).timestamp()}"
        existing = self.conn.execute("SELECT id FROM messages WHERE id = ?", (msg_id,)).fetchone()
        if existing:
            return False
        self.conn.execute("""
            INSERT INTO messages (id, sender_inbox_id, sender_id, conversation_id,
                 content_type, raw_content, msg_type, payload,
                 correlation_id, sent_at, received_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
        """, (
            msg_id, msg.get("_xmtp_sender"), msg.get("sender_id"),
            msg.get("_xmtp_conversation_id"),
            msg.get("payload", {}).get("content_type", "text"),
            json.dumps(msg), msg.get("type"),
            json.dumps(msg.get("payload", {})), msg.get("correlation_id"),
            msg.get("_xmtp_sent_at"),
            msg.get("_xmtp_received_at") or datetime.now(timezone.utc).isoformat(),
        ))
        self.conn.commit()
        return True

    def query(self, status: str = None, msg_type: str = None, sender_id: str = None,
              since: str = None, limit: int = 100) -> List[dict]:
        conditions, params = [], []
        if status:
            conditions.append("status = ?"); params.append(status)
        if msg_type:
            conditions.append("msg_type = ?"); params.append(msg_type)
        if sender_id:
            conditions.append("sender_id = ?"); params.append(sender_id)
        if since:
            conditions.append("received_at > ?"); params.append(since)
        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY received_at DESC LIMIT ?", params
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def mark_status(self, msg_id: str, status: str):
        now = datetime.now(timezone.utc).isoformat()
        processed_at = now if status in ("processed", "failed") else None
        self.conn.execute(
            "UPDATE messages SET status = ?, processed_at = ? WHERE id = ?",
            (status, processed_at, msg_id))
        self.conn.commit()

    def mark_processed(self, msg_id: str):
        self.mark_status(msg_id, "processed")

    def count(self, status: str = None) -> int:
        if status:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE status = ?", (status,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return row[0]

    def get_by_correlation(self, correlation_id: str) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE correlation_id = ? ORDER BY received_at",
            (correlation_id,)).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        for field in ("raw_content", "payload"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def close(self):
        self.conn.close()


class OutboxStore:
    """Persistent storage for sent AgentFax messages."""

    def __init__(self, data_dir: str):
        self.data_dir = str(Path(data_dir).expanduser())
        db_path = os.path.join(self.data_dir, "agentfax_outbox.db")
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_wallet TEXT NOT NULL,
                msg_type TEXT,
                payload TEXT,
                correlation_id TEXT,
                message_id TEXT,
                conversation_id TEXT,
                sent_at TEXT,
                status TEXT DEFAULT 'sent',
                acked_at TEXT,
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                next_retry_at TEXT,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sent_status ON sent_messages(status);
            CREATE INDEX IF NOT EXISTS idx_sent_correlation ON sent_messages(correlation_id);
            CREATE INDEX IF NOT EXISTS idx_sent_retry ON sent_messages(next_retry_at);
        """)
        self.conn.commit()

    def record(self, recipient_wallet: str, msg_type: str, payload: dict,
               bridge_response: dict, correlation_id: str = None):
        self.conn.execute("""
            INSERT INTO sent_messages (recipient_wallet, msg_type, payload, correlation_id,
                 message_id, conversation_id, sent_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'sent')
        """, (
            recipient_wallet, msg_type, json.dumps(payload), correlation_id,
            bridge_response.get("messageId"), bridge_response.get("conversationId"),
            datetime.now(timezone.utc).isoformat(),
        ))
        self.conn.commit()

    def record_pending(self, recipient_wallet: str, msg_type: str, payload: dict,
                       correlation_id: str = None, max_retries: int = 3) -> int:
        now = datetime.now(timezone.utc).isoformat()
        next_retry = datetime.fromtimestamp(time.time() + 5, tz=timezone.utc).isoformat()
        cursor = self.conn.execute("""
            INSERT INTO sent_messages (recipient_wallet, msg_type, payload, correlation_id,
                 sent_at, status, retry_count, max_retries, next_retry_at)
            VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
        """, (recipient_wallet, msg_type, json.dumps(payload), correlation_id,
              now, max_retries, next_retry))
        self.conn.commit()
        return cursor.lastrowid

    def get_retryable(self, limit: int = 10) -> List[dict]:
        now = datetime.now(timezone.utc).isoformat()
        claim_token = f"claim_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "UPDATE sent_messages SET status = 'retrying', "
            "last_error = COALESCE(last_error, '') || ? "
            "WHERE id IN ("
            "  SELECT id FROM sent_messages "
            "  WHERE status = 'pending' AND next_retry_at <= ? "
            "  ORDER BY next_retry_at ASC LIMIT ?)",
            (claim_token, now, limit))
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT * FROM sent_messages WHERE status = 'retrying' AND last_error LIKE ? "
            "ORDER BY next_retry_at ASC LIMIT ?",
            (f"%{claim_token}%", limit)).fetchall()
        for row in rows:
            clean_error = (row["last_error"] or "").replace(claim_token, "")
            self.conn.execute("UPDATE sent_messages SET last_error = ? WHERE id = ?",
                              (clean_error if clean_error else None, row["id"]))
        self.conn.commit()
        results = []
        for row in rows:
            d = dict(row)
            if d.get("last_error"):
                d["last_error"] = d["last_error"].replace(claim_token, "") or None
            if d.get("payload"):
                try:
                    d["payload"] = json.loads(d["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results

    def mark_retry_sent(self, row_id: int, bridge_response: dict):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE sent_messages SET status = 'sent', message_id = ?, "
            "conversation_id = ?, sent_at = ? WHERE id = ? AND status = 'retrying'",
            (bridge_response.get("messageId"), bridge_response.get("conversationId"),
             now, row_id))
        self.conn.commit()

    def mark_retry_failed(self, row_id: int, error: str):
        row = self.conn.execute(
            "SELECT retry_count, max_retries, status FROM sent_messages WHERE id = ?",
            (row_id,)).fetchone()
        if not row or row["status"] != "retrying":
            return
        new_count = (row["retry_count"] or 0) + 1
        max_retries = row["max_retries"] or 3
        if new_count >= max_retries:
            self.conn.execute(
                "UPDATE sent_messages SET status = 'failed', retry_count = ?, last_error = ? "
                "WHERE id = ? AND status = 'retrying'", (new_count, error, row_id))
        else:
            delay = 5 * (3 ** new_count)
            next_retry = datetime.fromtimestamp(time.time() + delay, tz=timezone.utc).isoformat()
            self.conn.execute(
                "UPDATE sent_messages SET status = 'pending', retry_count = ?, "
                "next_retry_at = ?, last_error = ? WHERE id = ? AND status = 'retrying'",
                (new_count, next_retry, error, row_id))
        self.conn.commit()

    def mark_acked(self, correlation_id: str):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE sent_messages SET status = 'acked', acked_at = ? "
            "WHERE correlation_id = ? AND status IN ('sent', 'pending', 'retrying')",
            (now, correlation_id))
        self.conn.commit()

    def recover_stale_retrying(self, stale_seconds: int = 60) -> int:
        cutoff = datetime.fromtimestamp(time.time() - stale_seconds, tz=timezone.utc).isoformat()
        cursor = self.conn.execute(
            "UPDATE sent_messages SET status = 'pending' WHERE status = 'retrying' AND sent_at <= ?",
            (cutoff,))
        self.conn.commit()
        return cursor.rowcount

    def query(self, status: str = None, limit: int = 50) -> List[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM sent_messages WHERE status = ? ORDER BY sent_at DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM sent_messages ORDER BY sent_at DESC LIMIT ?", (limit,)).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            if d.get("payload"):
                try:
                    d["payload"] = json.loads(d["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results

    def count(self, status: str = None) -> int:
        if status:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM sent_messages WHERE status = ?", (status,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM sent_messages").fetchone()
        return row[0]

    def close(self):
        self.conn.close()
