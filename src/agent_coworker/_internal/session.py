"""AgentFax Session Manager — collaboration session lifecycle."""

import enum
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agentfax.session")


class SessionState(enum.Enum):
    """Protocol-layer session states (7 states)."""
    PROPOSED = "proposed"
    ACTIVE = "active"
    CLOSING = "closing"
    COMPLETED = "completed"
    CLOSED = "closed"
    EXPIRED = "expired"
    REJECTED = "rejected"


TERMINAL_STATES = {
    SessionState.COMPLETED,
    SessionState.CLOSED,
    SessionState.EXPIRED,
    SessionState.REJECTED,
}

VALID_TRANSITIONS = {
    SessionState.PROPOSED: {SessionState.ACTIVE, SessionState.REJECTED, SessionState.EXPIRED},
    SessionState.ACTIVE: {SessionState.CLOSING, SessionState.EXPIRED},
    SessionState.CLOSING: {SessionState.COMPLETED, SessionState.CLOSED},
}


class SessionManager:
    """Manages collaboration sessions with SQLite persistence."""

    def __init__(self, data_dir: str):
        self.data_dir = str(Path(data_dir).expanduser())
        db_path = os.path.join(self.data_dir, "agentfax_sessions.db")
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                peer_id TEXT NOT NULL,
                role TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'proposed',
                proposed_skills TEXT,
                proposed_trust_tier INTEGER DEFAULT 1,
                proposed_max_context_privacy TEXT DEFAULT 'L1_PUBLIC',
                proposed_max_calls INTEGER DEFAULT 10,
                ttl_seconds INTEGER DEFAULT 3600,
                agreed_skills TEXT,
                agreed_skill_version TEXT,
                agreed_schema_hash TEXT,
                agreed_trust_tier INTEGER,
                agreed_max_context_privacy TEXT,
                agreed_max_calls INTEGER,
                agreed_pricing_snapshot TEXT,
                call_count INTEGER DEFAULT 0,
                tasks_completed INTEGER DEFAULT 0,
                tasks_failed INTEGER DEFAULT 0,
                tasks_in_flight INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                accepted_at TEXT,
                expires_at TEXT,
                closed_at TEXT,
                completed_at TEXT,
                close_reason TEXT,
                initiator_id TEXT,
                error_message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_peer ON sessions(peer_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_state ON sessions(state);
        """)
        self.conn.commit()

    def create_session(
        self, peer_id: str, role: str = "initiator",
        proposed_skills: list = None, proposed_trust_tier: int = 1,
        proposed_max_context_privacy: str = "L1_PUBLIC",
        proposed_max_calls: int = 10, ttl_seconds: int = 3600,
        initiator_id: str = "",
    ) -> str:
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        expires_at = datetime.fromtimestamp(
            time.time() + ttl_seconds, tz=timezone.utc
        ).isoformat()

        with self._lock:
            self.conn.execute("""
                INSERT INTO sessions
                    (session_id, peer_id, role, state,
                     proposed_skills, proposed_trust_tier,
                     proposed_max_context_privacy, proposed_max_calls,
                     ttl_seconds, created_at, expires_at, initiator_id)
                VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session_id, peer_id, role,
                json.dumps(proposed_skills or []),
                proposed_trust_tier, proposed_max_context_privacy,
                proposed_max_calls, ttl_seconds, now, expires_at, initiator_id,
            ))
            self.conn.commit()
        return session_id

    def accept_session(
        self, session_id: str, agreed_skills: list = None,
        agreed_skill_version: str = "1.0.0", agreed_schema_hash: str = "",
        agreed_trust_tier: int = None, agreed_max_context_privacy: str = None,
        agreed_max_calls: int = None, agreed_pricing_snapshot: dict = None,
    ) -> bool:
        session = self.get_session(session_id)
        if not session:
            return False
        if not self._can_transition(session, SessionState.ACTIVE):
            return False

        now = datetime.now(timezone.utc).isoformat()
        final_skills = agreed_skills or json.loads(session["proposed_skills"] or "[]")
        final_trust = agreed_trust_tier if agreed_trust_tier is not None else session["proposed_trust_tier"]
        final_privacy = agreed_max_context_privacy or session["proposed_max_context_privacy"]
        final_calls = agreed_max_calls if agreed_max_calls is not None else session["proposed_max_calls"]

        with self._lock:
            self.conn.execute("""
                UPDATE sessions SET
                    state = 'active', agreed_skills = ?, agreed_skill_version = ?,
                    agreed_schema_hash = ?, agreed_trust_tier = ?,
                    agreed_max_context_privacy = ?, agreed_max_calls = ?,
                    agreed_pricing_snapshot = ?, accepted_at = ?
                WHERE session_id = ?
            """, (
                json.dumps(final_skills), agreed_skill_version, agreed_schema_hash,
                final_trust, final_privacy, final_calls,
                json.dumps(agreed_pricing_snapshot or {"model": "free", "amount": 0}),
                now, session_id,
            ))
            self.conn.commit()
        return True

    def reject_session(self, session_id: str, reason: str = "") -> bool:
        return self._transition(session_id, SessionState.REJECTED, close_reason=reason)

    def close_session(self, session_id: str, reason: str = "") -> bool:
        return self._transition(session_id, SessionState.CLOSING, close_reason=reason)

    def complete_session(self, session_id: str) -> bool:
        session = self.get_session(session_id)
        if not session:
            return False
        if session["tasks_in_flight"] > 0:
            return False
        return self._transition(session_id, SessionState.COMPLETED)

    def force_close_session(self, session_id: str, reason: str = "") -> bool:
        return self._transition(session_id, SessionState.CLOSED, close_reason=reason)

    def expire_session(self, session_id: str) -> bool:
        return self._transition(session_id, SessionState.EXPIRED)

    def increment_call_count(self, session_id: str) -> bool:
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE sessions SET call_count = call_count + 1, "
                "tasks_in_flight = tasks_in_flight + 1 "
                "WHERE session_id = ? AND state = 'active' "
                "AND (agreed_max_calls IS NULL OR call_count < agreed_max_calls)",
                (session_id,),
            )
            self.conn.commit()
        return cursor.rowcount > 0

    def task_completed(self, session_id: str):
        with self._lock:
            self.conn.execute(
                "UPDATE sessions SET tasks_completed = tasks_completed + 1, "
                "tasks_in_flight = MAX(0, tasks_in_flight - 1) "
                "WHERE session_id = ?", (session_id,),
            )
            self.conn.commit()
        self._check_auto_complete(session_id)

    def task_failed(self, session_id: str):
        with self._lock:
            self.conn.execute(
                "UPDATE sessions SET tasks_failed = tasks_failed + 1, "
                "tasks_in_flight = MAX(0, tasks_in_flight - 1) "
                "WHERE session_id = ?", (session_id,),
            )
            self.conn.commit()
        self._check_auto_complete(session_id)

    def _check_auto_complete(self, session_id: str):
        session = self.get_session(session_id)
        if (session and
            session["state"] == SessionState.CLOSING.value and
            session["tasks_in_flight"] <= 0):
            self.complete_session(session_id)

    def validate_task_request(self, session_id: str, skill: str,
                              sender_id: str) -> tuple:
        session = self.get_session(session_id)
        if not session:
            return (False, "SESSION_NOT_FOUND", f"Session {session_id} does not exist")
        if session["state"] != SessionState.ACTIVE.value:
            return (False, "SESSION_NOT_ACTIVE", f"Session {session_id} is {session['state']}, not active")
        if session["expires_at"]:
            now = datetime.now(timezone.utc).isoformat()
            if now > session["expires_at"]:
                self.expire_session(session_id)
                return (False, "SESSION_EXPIRED", f"Session {session_id} has expired")
        if session["peer_id"] != sender_id:
            return (False, "SESSION_PEER_MISMATCH",
                    f"Session {session_id} is with {session['peer_id']}, not {sender_id}")
        agreed_skills = json.loads(session["agreed_skills"] or "[]")
        if agreed_skills and skill not in agreed_skills:
            return (False, "SKILL_NOT_IN_SESSION",
                    f"Skill '{skill}' not agreed in session. Agreed: {agreed_skills}")
        max_calls = session["agreed_max_calls"]
        if max_calls and session["call_count"] >= max_calls:
            return (False, "CALL_LIMIT_EXCEEDED",
                    f"Session {session_id} call limit reached ({session['call_count']}/{max_calls})")
        return (True, "", "")

    def expire_stale_sessions(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            "SELECT session_id FROM sessions "
            "WHERE state IN ('proposed', 'active') "
            "AND expires_at IS NOT NULL AND expires_at <= ?", (now,),
        )
        expired_ids = [row["session_id"] for row in cursor.fetchall()]
        for sid in expired_ids:
            self.expire_session(sid)
        return len(expired_ids)

    def get_session(self, session_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_active_session(self, peer_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE peer_id = ? AND state = 'active' "
            "ORDER BY created_at DESC LIMIT 1", (peer_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, state: str = None, peer_id: str = None,
                      limit: int = 50) -> List[dict]:
        conditions, params = [], []
        if state:
            conditions.append("state = ?")
            params.append(state)
        if peer_id:
            conditions.append("peer_id = ?")
            params.append(peer_id)
        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM sessions WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self, state: str = None) -> int:
        if state:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE state = ?", (state,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        return row[0]

    def get_active_sessions(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE state = 'active' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_sessions_for_peer(self, peer_id: str) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE peer_id = ? ORDER BY created_at DESC",
            (peer_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def has_capacity(self, max_sessions: int = 10) -> bool:
        active = self.count(state="active")
        proposed = self.count(state="proposed")
        return (active + proposed) < max_sessions

    def _can_transition(self, session: dict, target: SessionState) -> bool:
        current = SessionState(session["state"])
        if current in TERMINAL_STATES:
            return False
        return target in VALID_TRANSITIONS.get(current, set())

    def _transition(self, session_id: str, target: SessionState,
                    close_reason: str = "") -> bool:
        session = self.get_session(session_id)
        if not session:
            return False
        if not self._can_transition(session, target):
            return False

        now = datetime.now(timezone.utc).isoformat()
        updates = {"state": target.value}
        if target in TERMINAL_STATES:
            if target == SessionState.COMPLETED:
                updates["completed_at"] = now
            else:
                updates["closed_at"] = now
        if target == SessionState.CLOSING:
            updates["closed_at"] = now
        if close_reason:
            updates["close_reason"] = close_reason

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [session_id]
        with self._lock:
            self.conn.execute(
                f"UPDATE sessions SET {set_clause} WHERE session_id = ?", values,
            )
            self.conn.commit()
        return True

    def close(self):
        self.conn.close()
