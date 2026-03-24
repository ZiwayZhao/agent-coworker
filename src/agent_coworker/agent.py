"""Agent — the main entry point for building a coworker agent.

Each agent:
  1. Registers skills (Python functions)
  2. Connects to XMTP network via local bridge
  3. Listens for incoming messages (task_request, discover, etc.)
  4. Executes skills and sends results back via XMTP
  5. Serves a local HTTP dashboard (React frontend + API)
"""

import base64
import json
import mimetypes
import os
import signal
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs


COWORKER_DIR = Path.home() / ".coworker"


class Group:
    """A CoWorker group — multi-party communication over XMTP MLS.

    Usage:
        group = agent.create_group("Team Alpha", ["0xAAA...", "0xBBB..."])
        group.send("Hello everyone!")
        result = group.call("0xBBB...", "translate", {"text": "hello", "to_lang": "zh"})
        group.add_member("0xCCC...")
    """

    def __init__(self, agent: "Agent", group_id: str, name: str, members: list):
        self._agent = agent
        self.group_id = group_id
        self.name = name
        self.members = members  # wallet addresses
        self._response_box = agent._response_box

    def send(self, text: str) -> dict:
        """Send a chat message to the group (all members see it)."""
        return self._agent.client.group_send(self.group_id, "group_message", {
            "text": text,
            "sender": self._agent.name,
            "sender_wallet": self._agent.wallet,
        })

    def broadcast_skills(self, min_peer_tier: int = 1) -> dict:
        """Broadcast this agent's skill manifest to the group.

        Respects both visibility config and trust tier filtering.
        Since group broadcast goes to all members, we filter by the
        minimum expected peer tier (default KNOWN=1) to avoid leaking
        higher-tier skills to lower-tier group members.
        """
        skills = self._agent.executor.list_skills_for_tier(
            min_peer_tier, exposed_set=self._agent._exposed_set)
        return self._agent.client.group_send(self.group_id, "group_capabilities", {
            "name": self._agent.name,
            "wallet": self._agent.wallet,
            "skills": skills,
        })

    def call(self, target_wallet: str, skill_name: str, input_data: dict,
             timeout: float = 30.0) -> dict:
        """Call a skill on a specific group member via the group channel.

        The request is sent to the group (visible to all), and the target
        agent responds with the result (also visible to all).
        """
        corr_id = _uid()
        task_id = _uid()

        self._agent.client.group_send(self.group_id, "group_task_request", {
            "skill": skill_name,
            "input": input_data,
            "target_wallet": target_wallet,
            "requester": self._agent.name,
            "requester_wallet": self._agent.wallet,
        }, correlation_id=corr_id)

        start = time.time()
        while time.time() - start < timeout:
            time.sleep(0.5)
            if corr_id in self._response_box:
                resp = self._response_box.pop(corr_id)
                payload = resp.get("payload", {})
                return payload

        return {"success": False, "error": f"timeout ({timeout}s)"}

    def discover(self, timeout: float = 20.0) -> dict:
        """Discover all group members' skills.

        Sends a group_discover, waits for group_capabilities from each member.
        Returns {wallet: {name, skills}} for each member that responded.
        """
        corr_id = _uid()
        self._agent.client.group_send(self.group_id, "group_discover", {
            "name": self._agent.name,
            "wallet": self._agent.wallet,
        }, correlation_id=corr_id)

        # Collect capabilities from members
        discovered = {}
        start = time.time()
        expected = len(self.members)

        while time.time() - start < timeout and len(discovered) < expected:
            time.sleep(0.5)
            # Check response_box for group_capabilities with matching corr_id
            to_remove = []
            for key, resp in list(self._response_box.items()):
                if resp.get("type") == "group_capabilities":
                    payload = resp.get("payload", {})
                    w = payload.get("wallet", "")
                    if w and w != self._agent.wallet:
                        discovered[w] = {
                            "name": payload.get("name", w[:12]),
                            "skills": payload.get("skills", []),
                        }
                        to_remove.append(key)
            for key in to_remove:
                self._response_box.pop(key, None)

        return discovered

    def add_member(self, wallet: str) -> dict:
        """Add a member to the group."""
        result = self._agent.client.group_add_member(self.group_id, wallet)
        if result.get("status") == "added":
            self.members.append(wallet)
        return result

    def remove_member(self, wallet: str) -> dict:
        """Remove a member from the group."""
        result = self._agent.client.group_remove_member(self.group_id, wallet)
        if result.get("status") == "removed":
            self.members = [m for m in self.members if m != wallet]
        return result

    def __repr__(self):
        return f"Group(id={self.group_id[:12]}..., name={self.name!r}, members={len(self.members)})"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_TRUST_LABELS = ["untrusted", "known", "internal", "privileged"]

def _trust_tier_label(tier: int) -> str:
    """Convert numeric trust tier to string label."""
    return _TRUST_LABELS[min(tier, len(_TRUST_LABELS) - 1)]


def _uid() -> str:
    return str(uuid.uuid4())[:8]


def _msg_id() -> str:
    """Full UUID4 for message IDs — unique, not time-sortable.
    Incremental fetch relies on deque arrival order, not ID ordering."""
    return f"msg_{uuid.uuid4().hex}"


class Agent:
    """A coworker protocol agent.

    Usage:
        agent = Agent("my-bot")

        @agent.skill("echo", description="Echo input back")
        def echo(text: str) -> dict:
            return {"output": text}

        agent.serve()  # Starts XMTP listener + local monitor
    """

    def __init__(self, name: str, data_dir: str | None = None,
                 auto_accept_trust: bool = True,
                 max_auto_accept_tier: int = 1):
        self.name = name
        if data_dir is None:
            self.data_dir = str(COWORKER_DIR)
        else:
            self.data_dir = str(Path(data_dir).expanduser())

        self._executor = None
        self._client = None
        self._trust_manager = None
        self._running = False
        self._monitor_port = None
        self._start_time = time.time()
        self._auto_accept_trust = auto_accept_trust
        self._max_auto_accept_tier = max_auto_accept_tier

        # In-memory tracking stores
        self._activity: deque = deque(maxlen=500)
        self._tasks: list = []
        self._sessions: list = []
        self._metering: list = []
        self._message_counts: dict = {}
        self._collab_status: dict | None = None
        self._response_box: dict = {}

        # Group chat stores (independent of activity)
        self._groups: dict = {}            # group_id → {id, name, members, xmtp_group_id, created_at, ...}
        self._group_messages: deque = deque(maxlen=2000)  # all group messages, ordered by arrival
        self._client_msg_ids: set = set()  # (group_id, client_message_id) tuples for send idempotency

        # DM conversation stores
        import threading as _th
        self._dm_lock = _th.Lock()
        self._dm_messages: deque = deque(maxlen=5000)       # all DM messages, ordered
        self._dm_conversations: dict = {}                    # conv_id → summary
        self._dm_corr_index: dict = {}                       # correlation_id → context
        self._dm_msg_ids: set = set()                        # dedup set

        # Skill visibility
        self._exposed_set: set | None = None  # None = no filtering (all exposed)
        self._visibility_config = None
        self._expose_skills_override: list | str | None = None  # runtime override from serve()

    @property
    def executor(self):
        if self._executor is None:
            from ._internal.executor import TaskExecutor
            self._executor = TaskExecutor()
        return self._executor

    @property
    def client(self):
        if self._client is None:
            from ._internal.client import AgentFaxClient
            self._client = AgentFaxClient(self.data_dir)
        return self._client

    @property
    def trust_manager(self):
        if self._trust_manager is None:
            from ._internal.security import TrustManager
            self._trust_manager = TrustManager(
                self.data_dir,
                auto_accept_trust=self._auto_accept_trust,
                max_auto_accept_tier=self._max_auto_accept_tier,
            )
        return self._trust_manager

    @property
    def wallet(self) -> str:
        config_path = os.path.join(self.data_dir, "config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    return json.load(f).get("wallet", "")
            except (json.JSONDecodeError, IOError):
                pass
        return ""

    def skill(self, name: str, description: str = "", **kwargs):
        """Decorator to register a skill on this agent."""
        return self.executor.skill(name, description=description, **kwargs)

    # ── Activity Tracking ─────────────────────────────────────

    def _log_activity(self, atype: str, title: str, description: str = "",
                      peer: str = "", status: str = ""):
        self._activity.appendleft({
            "id": _uid(),
            "type": atype,
            "title": title,
            "description": description,
            "timestamp": _now_iso(),
            "peer": peer,
            "status": status,
        })

    def _log_task(self, task_id: str, skill: str, peer_name: str,
                  peer_wallet: str, role: str, state: str,
                  input_data=None, output_data=None,
                  error_message=None, duration_ms=None, session_id=None):
        task = {
            "task_id": task_id,
            "skill": skill,
            "state": state,
            "peer_name": peer_name,
            "peer_wallet": peer_wallet,
            "input_data": input_data,
            "output_data": output_data,
            "error_message": error_message,
            "duration_ms": duration_ms,
            "created_at": _now_iso(),
            "completed_at": _now_iso() if state in ("completed", "failed") else None,
            "role": role,
            "session_id": session_id,
        }
        self._tasks.append(task)
        # Keep last 200 tasks
        if len(self._tasks) > 200:
            self._tasks = self._tasks[-200:]
        return task

    def _log_metering(self, task_id: str, caller: str, provider: str,
                      skill_name: str, status: str, duration_ms: float,
                      input_data=None, output_data=None, session_id=None):
        input_size = len(json.dumps(input_data or {}, default=str))
        output_size = len(json.dumps(output_data or {}, default=str))
        receipt = {
            "receipt_id": _uid(),
            "task_id": task_id,
            "caller": caller,
            "provider": provider,
            "skill_name": skill_name,
            "skill_version": "1.0",
            "status": status,
            "duration_ms": round(duration_ms, 1),
            "input_size_bytes": input_size,
            "output_size_bytes": output_size,
            "session_id": session_id,
            "created_at": _now_iso(),
        }
        self._metering.append(receipt)
        if len(self._metering) > 200:
            self._metering = self._metering[-200:]

    def _count_message(self, msg_type: str):
        self._message_counts[msg_type] = self._message_counts.get(msg_type, 0) + 1

    # ── DM Conversation Storage ────────────────────────────

    def _dm_conv_id(self, peer_wallet: str) -> str:
        return f"dm:{peer_wallet.lower()}"

    def _store_dm_message(self, peer_wallet: str, peer_name: str,
                          direction: str, msg_type: str,
                          content: str, payload: dict = None,
                          correlation_id: str = "",
                          phase: str = "misc", skill: str = "",
                          collab_id: str = "", step_index: int = -1,
                          delivery_status: str = "sent") -> dict:
        """Store a DM message and update conversation summary.

        Args:
            direction: "outbound" or "inbound"
            phase: "discover" / "trust" / "plan" / "execute" / "report" / "misc"
        """
        conv_id = self._dm_conv_id(peer_wallet)
        msg_id = _msg_id()

        # Dedup
        dedup_key = f"{correlation_id}:{msg_type}:{direction}"
        with self._dm_lock:
            if dedup_key in self._dm_msg_ids:
                # Return existing message
                for m in reversed(self._dm_messages):
                    if m.get("_dedup") == dedup_key:
                        return m
                return {}
            self._dm_msg_ids.add(dedup_key)
            # Cap dedup set
            if len(self._dm_msg_ids) > 10000:
                # Just clear old half
                self._dm_msg_ids = set(list(self._dm_msg_ids)[-5000:])

            if direction == "outbound":
                sender_w, sender_n = self.wallet, self.name
                recip_w, recip_n = peer_wallet, peer_name
            else:
                sender_w, sender_n = peer_wallet, peer_name
                recip_w, recip_n = self.wallet, self.name

            msg = {
                "id": msg_id,
                "_dedup": dedup_key,
                "conversation_id": conv_id,
                "conversation_kind": "dm",
                "peer_wallet": peer_wallet,
                "peer_name": peer_name,
                "direction": direction,
                "sender_wallet": sender_w,
                "sender_name": sender_n,
                "recipient_wallet": recip_w,
                "recipient_name": recip_n,
                "msg_type": msg_type,
                "phase": phase,
                "correlation_id": correlation_id,
                "collab_id": collab_id,
                "step_index": step_index,
                "skill": skill,
                "content": content,
                "content_type": "application/json",
                "payload": payload or {},
                "payload_preview": content[:120] if content else "",
                "created_at": _now_iso(),
                "server_received_at": _now_iso(),
                "delivery_status": delivery_status,
            }
            self._dm_messages.append(msg)

            # Update conversation summary
            now = _now_iso()
            if conv_id not in self._dm_conversations:
                self._dm_conversations[conv_id] = {
                    "id": conv_id,
                    "kind": "dm",
                    "peer_wallet": peer_wallet,
                    "peer_name": peer_name,
                    "trust_tier": _trust_tier_label(
                        self.trust_manager.get_trust_tier(peer_wallet)),
                    "created_at": now,
                    "updated_at": now,
                    "last_message_id": msg_id,
                    "last_message_at": now,
                    "last_message": {"content": content[:80], "msg_type": msg_type},
                    "unread_count": 0,
                    "collab_active": bool(collab_id),
                    "message_count": 1,
                }
            else:
                conv = self._dm_conversations[conv_id]
                conv["updated_at"] = now
                conv["last_message_id"] = msg_id
                conv["last_message_at"] = now
                conv["last_message"] = {"content": content[:80], "msg_type": msg_type}
                conv["message_count"] = conv.get("message_count", 0) + 1
                conv["trust_tier"] = _trust_tier_label(
                    self.trust_manager.get_trust_tier(peer_wallet))
                if collab_id:
                    conv["collab_active"] = True

        return msg

    def _track_corr(self, correlation_id: str, peer_wallet: str, peer_name: str,
                    phase: str = "misc", collab_id: str = "",
                    step_index: int = -1, skill: str = "",
                    request_type: str = ""):
        """Track a correlation_id so we can match inbound responses."""
        with self._dm_lock:
            self._dm_corr_index[correlation_id] = {
                "conversation_id": self._dm_conv_id(peer_wallet),
                "peer_wallet": peer_wallet,
                "peer_name": peer_name,
                "phase": phase,
                "collab_id": collab_id,
                "step_index": step_index,
                "skill": skill,
                "request_type": request_type,
            }

    def _record_tracked_response(self, msg: dict) -> bool:
        """If an inbound response matches a tracked correlation_id, store it.

        Returns True if the message was recorded.
        """
        corr_id = msg.get("correlation_id", "")
        if not corr_id:
            return False
        with self._dm_lock:
            ctx = self._dm_corr_index.get(corr_id)
        if not ctx:
            return False

        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})

        # Build human-readable content
        if msg_type == "capabilities":
            skills = payload.get("skills", [])
            names = [s["name"] if isinstance(s, dict) else s for s in skills]
            content = f"Skills: {', '.join(names)}" if names else "No skills"
        elif msg_type == "task_response":
            result = payload.get("result", {})
            content = f"Result: {json.dumps(result, ensure_ascii=False)[:200]}"
        elif msg_type == "task_error":
            content = f"Error: {payload.get('error', '?')}"
        elif msg_type == "plan_accept":
            content = "Plan accepted"
        elif msg_type == "trust_grant":
            content = f"Trust granted: tier {payload.get('tier', '?')}"
        elif msg_type == "trust_deny":
            content = f"Trust denied: {payload.get('reason', '?')}"
        else:
            content = f"{msg_type}: {json.dumps(payload, ensure_ascii=False)[:150]}"

        self._store_dm_message(
            peer_wallet=ctx["peer_wallet"],
            peer_name=ctx["peer_name"],
            direction="inbound",
            msg_type=msg_type,
            content=content,
            payload=payload,
            correlation_id=corr_id,
            phase=ctx.get("phase", "misc"),
            skill=ctx.get("skill", ""),
            collab_id=ctx.get("collab_id", ""),
            step_index=ctx.get("step_index", -1),
            delivery_status="received",
        )
        return True

    # ── Group Message Storage ────────────────────────────

    def _store_group_message(self, group_id: str, sender_wallet: str,
                              sender_name: str, content: str,
                              content_type: str = "text/plain",
                              delivery_status: str = "received",
                              client_message_id: str = "") -> dict:
        """Store a group message and update group metadata.

        delivery_status: "sent" for outbound (we sent it), "received" for inbound.
        client_message_id: only meaningful for outbound (idempotency key).
        """
        msg = {
            "id": _msg_id(),
            "client_message_id": client_message_id or "",  # empty for inbound
            "group_id": group_id,
            "sender_wallet": sender_wallet,
            "sender_name": sender_name,
            "sender_trust_tier": (
                "self" if sender_wallet == self.wallet
                else _trust_tier_label(self.trust_manager.get_trust_tier(sender_wallet))
            ),
            "content": content,
            "content_type": content_type,
            "created_at": _now_iso(),
            "server_received_at": _now_iso(),
            "delivery_status": delivery_status,
        }
        self._group_messages.append(msg)

        # Update group metadata
        if group_id in self._groups:
            self._groups[group_id]["last_message_id"] = msg["id"]
            self._groups[group_id]["last_message_at"] = msg["created_at"]
            self._groups[group_id]["updated_at"] = msg["created_at"]

        return msg

    # ── XMTP Message Handling ────────────────────────────────

    def _handle_message(self, msg: dict):
        """Route an incoming CoWorker protocol message.

        Privacy enforcement:
        1. Correlated responses are delivered to response_box without trust check
           (validated by correlation_id, not trust tier).
        2. All other messages go through the trust gate — the sender's trust
           tier must meet the minimum for the message type (default-deny).
        3. discover returns only skills visible at the sender's trust tier.
        """
        from ._internal.security import RESPONSE_MSG_TYPES

        msg_type = msg.get("type", "")
        sender_wallet = msg.get("sender_wallet", "") or msg.get("_xmtp_sender", "")
        sender_name = msg.get("sender_id", sender_wallet[:12] + "..." if sender_wallet else "unknown")
        correlation_id = msg.get("correlation_id", "")
        self._count_message(msg_type)

        # ── Fast path: correlated responses go straight to response_box ──
        if msg_type in RESPONSE_MSG_TYPES and correlation_id in self._response_box:
            # Already have a response for this correlation — skip duplicate
            return
        if msg_type in RESPONSE_MSG_TYPES:
            # Record tracked response into DM conversation before delivering
            self._record_tracked_response(msg)
            # Check if this matches an async task (only for task_response/task_error)
            if correlation_id and msg_type in ("task_response", "task_error"):
                if self._handle_async_response(
                        correlation_id, msg.get("payload", {}),
                        sender_wallet=sender_wallet, msg_type=msg_type):
                    print(f"  ← {msg_type} [async] (corr: {correlation_id[:8] if correlation_id else '?'})")
                # Also put in response_box in case sync call() is also waiting
            self._response_box[correlation_id] = msg
            print(f"  ← {msg_type} (corr: {correlation_id[:8] if correlation_id else '?'})")
            return

        # ── Group trust bootstrap: same group → auto KNOWN ──
        # Group membership opens the message channel; skill ACL still guards execution.
        from ._internal.security import TrustTier
        if msg.get("_xmtp_is_group") and sender_wallet:
            if self.trust_manager.get_trust_tier(sender_wallet) < TrustTier.KNOWN:
                self.trust_manager.set_trust_override(sender_wallet, TrustTier.KNOWN)
                print(f"  ✓ Auto-trusted {sender_wallet[:12]}... → KNOWN (same group)")

        # ── Trust gate: check sender's permission for this message type ──
        if not self.trust_manager.is_message_allowed(sender_wallet, msg_type):
            rejection = self.trust_manager.get_rejection_info(sender_wallet, msg_type)
            print(f"  ✗ Blocked {msg_type} from {sender_wallet[:12]}... "
                  f"(tier {rejection['peer_tier']} < required {rejection['required_tier']})")
            self._log_activity("security", f"Blocked {msg_type} from {sender_name}",
                             rejection.get("message", ""), peer=sender_name, status="blocked")
            # Optionally notify sender
            self.client.send(sender_wallet, "error", {
                "code": "TRUST_TIER_TOO_LOW",
                "message": rejection.get("message", ""),
            }, correlation_id=correlation_id)
            return

        # ── Route by message type ──

        if msg_type == "discover":
            # Return only skills visible at sender's trust tier
            peer_tier = self.trust_manager.get_trust_tier(sender_wallet)
            visible_skills = self.executor.list_skills_for_tier(peer_tier, exposed_set=self._exposed_set)
            self.client.send(sender_wallet, "capabilities", {
                "name": self.name,
                "skills": visible_skills,
            }, correlation_id=correlation_id)
            # Record inbound discover + outbound capabilities in DM
            skill_names_str = ", ".join(s["name"] if isinstance(s, dict) else s for s in visible_skills)
            self._store_dm_message(
                peer_wallet=sender_wallet, peer_name=sender_name,
                direction="inbound", msg_type="discover",
                content=f"Skill discovery request",
                correlation_id=correlation_id, phase="discover",
                delivery_status="received")
            self._store_dm_message(
                peer_wallet=sender_wallet, peer_name=sender_name,
                direction="outbound", msg_type="capabilities",
                content=f"Skills: {skill_names_str}" if skill_names_str else "No skills",
                payload={"skills": [s["name"] if isinstance(s, dict) else s for s in visible_skills]},
                correlation_id=correlation_id, phase="discover")
            self._log_activity("message", f"Discover from {sender_name}",
                             f"Sent {len(visible_skills)} skills (tier {peer_tier})",
                             peer=sender_name)
            print(f"  ← discover from {sender_wallet[:12]}... → sent {len(visible_skills)} skills (tier {peer_tier})")

        elif msg_type == "trust_request":
            payload = msg.get("payload", {})
            requested_tier = payload.get("requested_tier", 1)
            reason = payload.get("reason", "")
            result = self.trust_manager.handle_trust_request(
                sender_wallet, requested_tier, reason)
            resp_type = "trust_grant" if result.get("granted") else "trust_deny"
            self.client.send(sender_wallet, resp_type, result,
                           correlation_id=correlation_id)
            self._log_activity("security",
                             f"Trust {'granted' if result.get('granted') else 'denied'}: {sender_name}",
                             f"Requested tier {requested_tier}, reason: {reason or 'none'}",
                             peer=sender_name,
                             status="granted" if result.get("granted") else "denied")
            print(f"  ← trust_request from {sender_wallet[:12]}... → {resp_type}")

        elif msg_type == "task_request":
            payload = msg.get("payload", {})
            skill_name = payload.get("skill", "")
            input_data = payload.get("input", {})
            task_id = _uid()

            # Check visibility: hidden skills return unknown_skill (no existence leak)
            if self._exposed_set is not None and skill_name not in self._exposed_set:
                self.client.send(sender_wallet, "task_error", {
                    "success": False,
                    "error": f"Unknown skill: {skill_name}",
                }, correlation_id=correlation_id)
                import logging
                logging.getLogger("coworker.visibility").info(
                    "skill_hidden name=%s caller=%s", skill_name, sender_wallet[:12])
                print(f"  ✗ task_request for hidden skill '{skill_name}' → unknown_skill")
                return

            # Check skill-level trust: peer must have sufficient tier for this skill
            skill_def = self.executor.get_skill(skill_name)
            if skill_def:
                peer_tier = self.trust_manager.get_trust_tier(sender_wallet)
                if peer_tier < (skill_def.min_trust_tier if skill_def.min_trust_tier is not None else 1):
                    self.client.send(sender_wallet, "task_error", {
                        "success": False,
                        "error": f"Unknown skill: {skill_name}",
                    }, correlation_id=correlation_id)
                    import logging
                    logging.getLogger("coworker.visibility").info(
                        "skill_tier_blocked name=%s peer_tier=%s required=%s",
                        skill_name, peer_tier, skill_def.min_trust_tier)
                    print(f"  ✗ Blocked task_request for {skill_name} (tier {peer_tier} < {skill_def.min_trust_tier})")
                    return

            print(f"  ← task_request: {skill_name} from {sender_wallet[:12]}...")
            # Record inbound task_request
            self._store_dm_message(
                peer_wallet=sender_wallet, peer_name=sender_name,
                direction="inbound", msg_type="task_request",
                content=f"Call {skill_name}({json.dumps(input_data, ensure_ascii=False)[:100]})",
                payload={"skill": skill_name, "input": input_data},
                correlation_id=correlation_id, phase="execute",
                skill=skill_name, delivery_status="received")

            start_t = time.time()
            result = self.executor.execute(skill_name, input_data)
            duration = (time.time() - start_t) * 1000

            success = result.get("success", False)
            resp_type = "task_response" if success else "task_error"
            self.client.send(sender_wallet, resp_type, result,
                           correlation_id=correlation_id)
            # Record outbound task_response
            result_preview = json.dumps(result.get("result", {}), ensure_ascii=False)[:150]
            self._store_dm_message(
                peer_wallet=sender_wallet, peer_name=sender_name,
                direction="outbound", msg_type=resp_type,
                content=f"{'Result' if success else 'Error'}: {result_preview}",
                payload=result,
                correlation_id=correlation_id, phase="execute",
                skill=skill_name)

            # Track
            self._log_task(task_id, skill_name, sender_name, sender_wallet,
                         "executor", "completed" if success else "failed",
                         input_data=input_data,
                         output_data=result.get("result"),
                         error_message=result.get("error"),
                         duration_ms=duration)
            self._log_metering(task_id, sender_name, self.name, skill_name,
                             "completed" if success else "failed", duration,
                             input_data=input_data, output_data=result.get("result"))
            self._log_activity("task",
                             f"{'Executed' if success else 'Failed'}: {skill_name}",
                             f"Called by {sender_name}, {duration:.0f}ms",
                             peer=sender_name,
                             status="completed" if success else "failed")
            status = "✓" if success else "✗"
            print(f"  → {resp_type} {status}")

        elif msg_type == "ping":
            self.client.send(sender_wallet, "pong", {
                "message": f"pong from {self.name}",
            }, correlation_id=correlation_id)
            self._log_activity("message", f"Ping from {sender_name}",
                             "Replied with pong", peer=sender_name)
            print(f"  ← ping from {sender_wallet[:12]}... → pong")

        elif msg_type == "plan_propose":
            payload = msg.get("payload", {})
            goal = payload.get("goal", "?")
            steps = payload.get("steps", [])
            my_steps = [s for s in steps if s.get("agent_wallet") == self.wallet]
            print(f"  ← plan_propose: \"{goal}\" ({len(steps)} steps, {len(my_steps)} mine)")
            self.client.send(sender_wallet, "plan_accept", {
                "goal": goal, "accepted": True,
            }, correlation_id=correlation_id)
            self._log_activity("session", f"Plan accepted: \"{goal[:40]}\"",
                             f"{len(steps)} steps, {len(my_steps)} assigned to me",
                             peer=sender_name, status="accepted")

            # Create session record
            session_id = _uid()
            self._sessions.append({
                "session_id": session_id,
                "peer_id": sender_name,
                "role": "responder",
                "state": "active",
                "proposed_skills": [s.get("skill") for s in steps],
                "agreed_skills": [s.get("skill") for s in my_steps],
                "agreed_trust_tier": self.trust_manager.get_trust_tier(sender_wallet),
                "agreed_max_context_privacy": "L1_PUBLIC",
                "agreed_max_calls": len(steps),
                "call_count": 0,
                "tasks_completed": 0,
                "tasks_failed": 0,
                "created_at": _now_iso(),
                "accepted_at": _now_iso(),
                "closed_at": None,
                "expires_at": _now_iso(),
            })
            print(f"  → plan_accept ✓")

        elif msg_type == "skill_card_query":
            # Return detailed skill info, filtered by visibility + trust tier
            payload = msg.get("payload", {})
            skill_name = payload.get("skill", "")
            # Unified check: visibility + trust — all failures return same error
            skill_visible = (self._exposed_set is None or skill_name in self._exposed_set)
            peer_tier = self.trust_manager.get_trust_tier(sender_wallet)
            skill_def = self.executor.get_skill(skill_name)
            if skill_visible and skill_def and peer_tier >= (skill_def.min_trust_tier or 1):
                self.client.send(sender_wallet, "skill_card", skill_def.to_dict(),
                               correlation_id=correlation_id)
            else:
                # Uniform error — no existence/visibility leak
                self.client.send(sender_wallet, "error", {
                    "code": "SKILL_NOT_FOUND",
                    "message": f"Unknown skill: {skill_name}",
                }, correlation_id=correlation_id)

        elif msg_type == "group_discover":
            # Group member wants to know everyone's capabilities
            payload = msg.get("payload", {})
            requester_wallet = payload.get("wallet", sender_wallet)
            peer_tier = self.trust_manager.get_trust_tier(requester_wallet)
            visible_skills = self.executor.list_skills_for_tier(peer_tier, exposed_set=self._exposed_set)
            # Reply via group (so everyone sees)
            group_id = msg.get("_group_id", "")
            if group_id:
                self.client.group_send(group_id, "group_capabilities", {
                    "name": self.name,
                    "wallet": self.wallet,
                    "skills": visible_skills,
                }, correlation_id=correlation_id)
            else:
                # Fallback to DM
                self.client.send(sender_wallet, "group_capabilities", {
                    "name": self.name,
                    "wallet": self.wallet,
                    "skills": visible_skills,
                }, correlation_id=correlation_id)
            print(f"  ← group_discover from {sender_wallet[:12]}... → sent {len(visible_skills)} skills")

        elif msg_type == "group_task_request":
            # Someone in the group wants to call one of our skills
            payload = msg.get("payload", {})
            target_wallet = payload.get("target_wallet", "")
            if target_wallet and target_wallet != self.wallet:
                return  # Not for us

            skill_name = payload.get("skill", "")
            input_data = payload.get("input", {})
            requester_wallet = payload.get("requester_wallet", sender_wallet)
            task_id = _uid()

            # Unified authorization: visibility + trust tier
            # All rejection reasons return the same "Unknown skill" to prevent enumeration
            err_payload = {"success": False, "error": f"Unknown skill: {skill_name}"}

            # Layer 1: visibility check
            if self._exposed_set is not None and skill_name not in self._exposed_set:
                import logging
                logging.getLogger("coworker.visibility").info(
                    "skill_hidden name=%s caller=%s (group)", skill_name, requester_wallet[:12])
                group_id = msg.get("_group_id", "")
                if group_id:
                    self.client.group_send(group_id, "group_task_error", err_payload,
                                          correlation_id=correlation_id)
                else:
                    self.client.send(sender_wallet, "group_task_error", err_payload,
                                    correlation_id=correlation_id)
                return

            # Layer 2: trust tier check
            peer_tier = self.trust_manager.get_trust_tier(requester_wallet)
            skill_def = self.executor.get_skill(skill_name)
            if skill_def:
                min_tier = skill_def.min_trust_tier if skill_def.min_trust_tier is not None else 1
                if peer_tier < min_tier:
                    import logging
                    logging.getLogger("coworker.visibility").info(
                        "skill_tier_blocked name=%s peer_tier=%s required=%s (group)",
                        skill_name, peer_tier, min_tier)
                    group_id = msg.get("_group_id", "")
                    if group_id:
                        self.client.group_send(group_id, "group_task_error", err_payload,
                                              correlation_id=correlation_id)
                    else:
                        self.client.send(sender_wallet, "group_task_error", err_payload,
                                        correlation_id=correlation_id)
                    return

            print(f"  ← group_task_request: {skill_name} from {sender_wallet[:12]}...")
            start_t = time.time()
            result = self.executor.execute(skill_name, input_data)
            duration = (time.time() - start_t) * 1000
            success = result.get("success", False)
            resp_type = "group_task_response" if success else "group_task_error"

            group_id = msg.get("_group_id", "")
            if group_id:
                self.client.group_send(group_id, resp_type, result,
                                      correlation_id=correlation_id)
            else:
                self.client.send(sender_wallet, resp_type, result,
                                correlation_id=correlation_id)

            self._log_task(task_id, skill_name, sender_name, sender_wallet,
                         "executor", "completed" if success else "failed",
                         input_data=input_data, output_data=result.get("result"),
                         duration_ms=duration)
            print(f"  → {resp_type} {'✓' if success else '✗'}")

        elif msg_type == "group_message":
            # Chat message in a group — store and log it
            payload = msg.get("payload", {})
            text = payload.get("text", "")
            sender_name_msg = payload.get("sender", sender_name)
            group_id = msg.get("_group_id", "")

            # Auto-register group if we don't know it yet
            if group_id and group_id not in self._groups:
                self._groups[group_id] = {
                    "id": group_id, "xmtp_group_id": group_id,
                    "name": f"Group {group_id[:8]}", "members": [],
                    "member_count": 0, "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                    "last_message_id": None, "last_message_at": None,
                }

            # Skip self-echoed messages (we already stored them when sending)
            if sender_wallet != self.wallet:
                self._store_group_message(
                    group_id=group_id, sender_wallet=sender_wallet,
                    sender_name=sender_name_msg, content=text,
                )
            print(f"  ← group_message from {sender_name_msg}: {text[:80]}")
            self._log_activity("message", f"Group message from {sender_name_msg}",
                             text[:120], peer=sender_name_msg)

        else:
            print(f"  ← unknown message type: {msg_type}")

    def _poll_loop(self, interval: float = 1.5):
        """Background thread: poll XMTP bridge inbox and handle messages."""
        while self._running:
            try:
                messages = self.client.receive(clear=True)
                for msg in messages:
                    try:
                        # Attach group context if this came from a group conversation
                        if msg.get("_xmtp_is_group"):
                            msg["_group_id"] = msg.get("_xmtp_conversation_id", "")
                        self._handle_message(msg)
                    except Exception as e:
                        print(f"  ⚠ Error handling message: {e}")
            except FileNotFoundError:
                pass
            except Exception as e:
                print(f"  ⚠ Poll error: {e}")
            time.sleep(interval)

    # ── Group Operations ───────────────────────────────────────

    def create_group(self, name: str, members: list[str]) -> Group:
        """Create an XMTP group with the given members.

        Args:
            name: Human-readable group name
            members: List of wallet addresses to add

        Returns:
            Group object for sending messages and calling skills
        """
        result = self.client.create_group(members, name=name)
        group_id = result.get("groupId", "")
        if not group_id:
            raise RuntimeError(f"Failed to create group: {result}")

        # Group membership implies KNOWN trust — open the message channel
        from ._internal.security import TrustTier
        for w in members:
            if self.trust_manager.get_trust_tier(w) < TrustTier.KNOWN:
                self.trust_manager.set_trust_override(w, TrustTier.KNOWN)

        all_members = list(members) + [self.wallet]
        group = Group(self, group_id, name, all_members)

        # Register in group store
        self._groups[group_id] = {
            "id": group_id,
            "xmtp_group_id": group_id,
            "name": name,
            "members": [
                {"wallet": w, "name": w[:12] + "...",
                 "trust_tier": _trust_tier_label(self.trust_manager.get_trust_tier(w))}
                for w in members
            ] + [{"wallet": self.wallet, "name": self.name, "trust_tier": "self"}],
            "member_count": len(members) + 1,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "last_message_id": None,
            "last_message_at": None,
        }

        self._log_activity("group", f"Created group: {name}",
                         f"{len(members)} members", status="created")
        print(f"  ✓ Created group: {name} ({group_id[:12]}..., {len(members)} members)")
        return group

    def get_group(self, group_id: str, name: str = "", members: list[str] = None) -> Group:
        """Get a Group object for an existing XMTP group."""
        return Group(self, group_id, name or "group", members or [])

    # ── Remote Operations ────────────────────────────────────

    def connect(self, wallet_address: str, retries: int = 3) -> dict:
        """Connect to a peer via XMTP. Sends discover, waits for capabilities.

        Retries up to `retries` times if the first attempt times out, because
        XMTP network propagation can be unpredictable (20-40s on dev for first DM).
        """
        for attempt in range(1, retries + 1):
            corr_id = _uid()
            if attempt == 1:
                print(f"  Discovering {wallet_address[:12]}... (first contact may take 30-60s)")
            else:
                print(f"  Retrying discover ({attempt}/{retries})...")

            self.client.send(wallet_address, "discover", {
                "name": self.name, "wallet": self.wallet,
            }, correlation_id=corr_id)
            # Record outbound discover
            _peer_name_tmp = f"peer-{wallet_address[2:8]}"
            self._store_dm_message(
                peer_wallet=wallet_address, peer_name=_peer_name_tmp,
                direction="outbound", msg_type="discover",
                content=f"Discovering skills...",
                correlation_id=corr_id, phase="discover")
            self._track_corr(corr_id, wallet_address, _peer_name_tmp,
                           phase="discover", request_type="discover")

            wait_secs = 30 if attempt == 1 else 25  # first DM needs longer
            for _ in range(wait_secs):
                time.sleep(1)
                if corr_id in self._response_box:
                    resp = self._response_box.pop(corr_id)
                    payload = resp.get("payload", {})
                    peer_name = payload.get("name", f"peer-{wallet_address[2:8]}")
                    skills = payload.get("skills", [])

                    peers = self._load_peers()
                    peers[peer_name] = {
                        "wallet": wallet_address,
                        "skills": skills,
                        "connected_at": _now_iso(),
                    }
                    self._save_peers(peers)

                    skill_names = [s["name"] if isinstance(s, dict) else s for s in skills]

                    # Auto-trust handshake: always request KNOWN tier so we can
                    # call skills that require task_request (tier >= 1).
                    # Even if we got some tier-0 skills, task_request itself needs KNOWN.
                    if True:  # always request trust upgrade
                        print(f"  ℹ Peer returned 0 skills (UNTRUSTED) — requesting trust...")
                        trust_corr = _uid()
                        self.client.send(wallet_address, "trust_request", {
                            "requested_tier": 1,
                            "reason": f"{self.name} wants to collaborate",
                        }, correlation_id=trust_corr)

                        # Wait for trust_grant/trust_deny
                        for _ in range(15):
                            time.sleep(1)
                            if trust_corr in self._response_box:
                                trust_resp = self._response_box.pop(trust_corr)
                                trust_type = trust_resp.get("type", "")
                                if trust_type == "trust_grant":
                                    print(f"  ✓ Trust granted! Re-discovering skills...")
                                    # Re-discover with new trust level
                                    rediscover_corr = _uid()
                                    self.client.send(wallet_address, "discover", {
                                        "name": self.name, "wallet": self.wallet,
                                    }, correlation_id=rediscover_corr)
                                    for _ in range(15):
                                        time.sleep(1)
                                        if rediscover_corr in self._response_box:
                                            resp2 = self._response_box.pop(rediscover_corr)
                                            payload2 = resp2.get("payload", {})
                                            skills = payload2.get("skills", [])
                                            peer_name = payload2.get("name", peer_name)
                                            peers[peer_name]["skills"] = skills
                                            self._save_peers(peers)
                                            skill_names = [s["name"] if isinstance(s, dict) else s for s in skills]
                                            break
                                elif trust_type == "trust_deny":
                                    print(f"  ✗ Trust denied by peer")
                                break

                    print(f"  ✓ Discovered: {peer_name} ({len(skills)} skills: {', '.join(skill_names)})")
                    self._log_activity("session", f"Connected to {peer_name}",
                                     f"Discovered {len(skills)} skills: {', '.join(skill_names)}",
                                     peer=peer_name, status="connected")
                    # Pre-warm return DM conversation in background
                    # This reduces first task_request latency significantly
                    def _prewarm():
                        try:
                            self.client.prewarm(wallet_address)
                        except Exception:
                            pass
                    threading.Thread(target=_prewarm, daemon=True).start()
                    return peers[peer_name]

        print(f"  ✗ No response from {wallet_address[:12]}... (timeout after {retries} attempts)")
        return {"error": "timeout", "wallet": wallet_address}

    def call(self, wallet_address: str, skill_name: str, input_data: dict,
             timeout: float = 30.0, retries: int = 1) -> dict:
        """Call a skill on a remote agent via XMTP.

        If the first attempt times out, retries up to `retries` times.
        XMTP dev network can have 2-30s propagation delay.
        """
        task_id = _uid()
        last_error = ""

        for attempt in range(1, retries + 1):
            corr_id = _uid()
            if attempt > 1:
                print(f"    (retry {attempt}/{retries})")

            self.client.send(wallet_address, "task_request", {
                "skill": skill_name, "input": input_data,
            }, correlation_id=corr_id)
            # Record outbound task_request
            _peer = wallet_address[:12] + "..."
            _collab = getattr(self, '_current_collab_id', "")
            _step = getattr(self, '_current_step_index', -1)
            self._store_dm_message(
                peer_wallet=wallet_address, peer_name=_peer,
                direction="outbound", msg_type="task_request",
                content=f"Call {skill_name}({json.dumps(input_data, ensure_ascii=False)[:100]})",
                payload={"skill": skill_name, "input": input_data},
                correlation_id=corr_id, phase="execute",
                skill=skill_name, collab_id=_collab, step_index=_step)
            self._track_corr(corr_id, wallet_address, _peer,
                           phase="execute", skill=skill_name,
                           collab_id=_collab, step_index=_step,
                           request_type="task_request")

            start = time.time()
            while time.time() - start < timeout:
                time.sleep(0.5)
                if corr_id in self._response_box:
                    resp = self._response_box.pop(corr_id)
                    payload = resp.get("payload", {})
                    duration = (time.time() - start) * 1000
                    success = payload.get("success", False)

                    # Track
                    peer_name = wallet_address[:12] + "..."
                    self._log_task(task_id, skill_name, peer_name, wallet_address,
                                 "requester", "completed" if success else "failed",
                                 input_data=input_data,
                                 output_data=payload.get("result"),
                                 error_message=payload.get("error"),
                                 duration_ms=duration)
                    self._log_metering(task_id, self.name, peer_name, skill_name,
                                     "completed" if success else "failed", duration,
                                     input_data=input_data, output_data=payload.get("result"))
                    self._count_message("task_request")
                    self._log_activity("task",
                                     f"Remote call: {skill_name}",
                                     f"{'Success' if success else 'Failed'} ({duration:.0f}ms)",
                                     peer=peer_name,
                                     status="completed" if success else "failed")
                    return payload

            last_error = f"timeout ({timeout}s)"

        return {"success": False, "error": last_error}

    # ── Async Task Delegation ────────────────────────────────

    def _ensure_task_queue(self):
        """Lazy-init the async task queue."""
        if not hasattr(self, '_task_queue') or self._task_queue is None:
            from ._internal.task_queue import AsyncTaskQueue
            self._task_queue = AsyncTaskQueue(self.data_dir)

    def request(self, wallet_address: str, skill_name: str, input_data: dict,
                task_id: str | None = None) -> str:
        """Send an async task request. Returns immediately with task_id.

        The request is sent via XMTP (store-and-forward). If the peer is
        offline, XMTP queues the message. When the peer comes online,
        their agent processes it and sends back a response, which is
        picked up by our poll loop and stored locally.

        Args:
            wallet_address: Peer's inbox ID or wallet address.
            skill_name: Name of the skill to call.
            input_data: Input parameters for the skill.
            task_id: Optional idempotency key. Auto-generated if not provided.

        Returns:
            task_id string. Use get_result(task_id) to check status later.
        """
        self._ensure_task_queue()
        from ._internal.task_queue import AsyncTask

        if task_id is None:
            task_id = str(uuid.uuid4())  # Full UUID for async tasks (not _uid() which is 8 chars)

        # Idempotency: if task already exists, just return the ID
        if self._task_queue.exists(task_id):
            print(f"  ℹ Task {task_id[:8]} already exists (idempotent)")
            return task_id

        corr_id = str(uuid.uuid4())  # Full UUID for correlation

        # Create and persist task locally
        task = AsyncTask(
            task_id=task_id,
            peer_inbox=wallet_address,
            skill=skill_name,
            input_data=input_data,
            peer_name=wallet_address[:12] + "...",
            correlation_id=corr_id,
        )
        if not self._task_queue.save(task):
            return task_id  # Saved failed, but return ID (caller can retry)

        # Send via XMTP (store-and-forward)
        try:
            self.client.send(wallet_address, "task_request", {
                "skill": skill_name,
                "input": input_data,
                "async": True,
                "task_id": task_id,
            }, correlation_id=corr_id)
        except Exception as e:
            # Mark task as failed if send fails
            self._task_queue.fail(task_id, f"Send failed: {e}")
            print(f"  ✗ Failed to send request: {e}")
            return task_id

        # Track DM
        self._store_dm_message(
            peer_wallet=wallet_address,
            peer_name=task.peer_name,
            direction="outbound",
            msg_type="task_request",
            content=f"[async] Call {skill_name}({json.dumps(input_data, ensure_ascii=False)[:80]})",
            payload={"skill": skill_name, "input": input_data, "task_id": task_id},
            correlation_id=corr_id,
            phase="execute",
            skill=skill_name,
        )

        self._log_activity("task",
                         f"Async request: {skill_name}",
                         f"Sent to {task.peer_name}, task_id={task_id[:8]}",
                         peer=task.peer_name)

        print(f"  → Async request sent: {skill_name} → {task.peer_name}")
        print(f"    Task ID: {task_id[:8]}...")
        print(f"    Check status: agent.get_result('{task_id[:8]}...')")
        return task_id

    def get_result(self, task_id: str) -> dict:
        """Check the status/result of an async task.

        Returns:
            {
                "task_id": "...",
                "state": "queued|succeeded|failed|expired",
                "skill": "translate",
                "result": {...} or None,
                "error": "..." or None,
                "created_at": "...",
                "peer_name": "...",
            }
        """
        self._ensure_task_queue()
        task = self._task_queue.load(task_id)
        if task is None:
            return {"task_id": task_id, "state": "not_found", "error": "Unknown task ID"}
        return task.to_dict()

    def list_async_tasks(self, state: str | None = None, limit: int = 20) -> list:
        """List async tasks, optionally filtered by state."""
        self._ensure_task_queue()
        return [t.to_dict() for t in self._task_queue.list_tasks(state=state, limit=limit)]

    def _handle_async_response(self, correlation_id: str, payload: dict,
                               sender_wallet: str = "", msg_type: str = ""):
        """Called when a task_response/task_error arrives that may match an async task.

        Validates: correlation_id + sender must match the original request.
        """
        self._ensure_task_queue()
        task = self._task_queue.find_by_correlation(correlation_id)
        if task is None:
            return False

        # Validate sender matches original target
        # Note: peer_inbox may be inbox ID while sender_wallet may be wallet address
        # Both identify the same agent, so we check if either matches
        if sender_wallet and task.peer_inbox:
            sender_short = sender_wallet.lower().replace("0x", "")[:12]
            peer_short = task.peer_inbox.lower().replace("0x", "")[:12]
            # Skip validation if formats differ (inbox ID vs wallet address)
            # The correlation_id match is already a strong enough guarantee
            if sender_short == peer_short:
                pass  # Match
            elif len(task.peer_inbox) == 64 and sender_wallet.startswith("0x"):
                pass  # inbox ID vs wallet — different format, allow
            elif task.peer_inbox.startswith("0x") and len(sender_wallet) == 64:
                pass  # wallet vs inbox ID — different format, allow
            elif sender_wallet == task.peer_inbox:
                pass  # Exact match
            else:
                import logging
                logging.getLogger("coworker.task_queue").warning(
                    "Async response sender mismatch: expected=%s got=%s task=%s",
                    task.peer_inbox[:12], sender_wallet[:12], task.task_id[:8])
                return False

        success = payload.get("success", False)
        if success:
            self._task_queue.complete(task.task_id, payload.get("result", {}))
            print(f"  ✓ Async task completed: {task.skill} (task={task.task_id[:8]})")
        else:
            error = payload.get("error", payload.get("error_message", "unknown error"))
            self._task_queue.fail(task.task_id, error)
            print(f"  ✗ Async task failed: {task.skill} (task={task.task_id[:8]})")

        self._log_activity("task",
                         f"Async {'completed' if success else 'failed'}: {task.skill}",
                         f"task_id={task.task_id[:8]}, peer={task.peer_name}",
                         peer=task.peer_name,
                         status="completed" if success else "failed")
        return True

    def collaborate(self, wallet_address: str, goal: str, timeout: float = 120.0) -> dict:
        """Auto-collaborate with a remote agent toward a shared goal."""

        # Generate collab_id for DM thread tracking
        collab_id = _uid()
        self._current_collab_id = collab_id
        self._current_step_index = -1

        # Set collab status for UI
        self._collab_status = {
            "goal": goal, "status": "discovering",
            "result": None, "steps": [], "agents": None, "session": None, "cost": None,
        }

        # ── Create/find collaboration group for live observation ──
        collab_group_id = None
        for gid, g in self._groups.items():
            member_wallets = [m.get("wallet", "") for m in g.get("members", [])]
            if wallet_address in member_wallets:
                collab_group_id = gid
                break

        def _collab_msg(sender_wallet, sender_name, content):
            """Post a message to the collaboration group for frontend observation."""
            if collab_group_id:
                self._store_group_message(
                    collab_group_id, sender_wallet, sender_name,
                    content, delivery_status="sent" if sender_wallet == self.wallet else "received",
                )

        # 1. Discover peer skills
        print(f"\n  ── Collaboration: \"{goal}\" ──\n")
        print(f"  Step 1: Discovering peer skills...")
        peer = self.connect(wallet_address)
        if "error" in peer:
            self._collab_status["status"] = "failed"
            self._collab_status["result"] = f"Cannot discover peer: {peer['error']}"
            return {"success": False, "error": f"Cannot discover peer: {peer['error']}"}

        peer_skills = peer.get("skills", [])
        peer_skill_names = [s["name"] if isinstance(s, dict) else s for s in peer_skills]
        peer_name_early = peer.get("name", wallet_address[:12])

        # If no group exists, create one for this collaboration
        if not collab_group_id:
            collab_group_id = _uid()
            self._groups[collab_group_id] = {
                "id": collab_group_id, "xmtp_group_id": collab_group_id,
                "name": f"{self.name} x {peer_name_early}",
                "members": [
                    {"wallet": self.wallet, "name": self.name, "trust_tier": "self"},
                    {"wallet": wallet_address, "name": peer_name_early,
                     "trust_tier": _trust_tier_label(self.trust_manager.get_trust_tier(wallet_address))},
                ],
                "member_count": 2, "created_at": _now_iso(), "updated_at": _now_iso(),
                "last_message_id": None, "last_message_at": None,
            }

        _collab_msg(self.wallet, self.name,
                    f"[Collaboration] Goal: {goal}")
        _collab_msg(self.wallet, self.name,
                    f"[Discovery] Found {len(peer_skill_names)} skills on {peer_name_early}: {', '.join(peer_skill_names)}")
        my_skill_names = self.executor.skill_names
        peer_name = None
        for pn, pdata in self._load_peers().items():
            if pdata.get("wallet") == wallet_address:
                peer_name = pn
                break
        peer_name = peer_name or wallet_address[:12]

        # Update collab status with agents info
        self._collab_status["agents"] = {
            "alpha": {"name": self.name, "wallet": self.wallet,
                     "online": True, "skills": my_skill_names},
            "beta": {"name": peer_name, "wallet": wallet_address,
                    "online": True, "skills": peer_skill_names},
        }

        print(f"  My skills:   {', '.join(my_skill_names)}")
        print(f"  Peer skills: {', '.join(peer_skill_names)}")

        # 2. Build plan — use skill schemas to construct proper input
        def _build_skill_input(skill_info, goal_text: str) -> dict:
            """Build input dict for a skill based on its input_schema.

            If the skill has an input_schema, populate each declared field
            with the goal text so the skill can use whichever parameter it
            expects. Falls back to common parameter names if no schema.
            """
            schema = {}
            if isinstance(skill_info, dict):
                schema = skill_info.get("input_schema", {})
            if schema:
                # Populate every declared field with the goal text
                return {k: goal_text for k in schema}
            # Fallback: cover common parameter names
            return {"goal": goal_text, "text": goal_text,
                    "query": goal_text, "data": goal_text,
                    "topic": goal_text}

        # Build lookup from skill name → full skill info dict
        # Only use exposed skills in collaboration
        all_skills = self.executor.list_skills()
        if self._exposed_set is not None:
            all_skills = [s for s in all_skills if s["name"] in self._exposed_set]
        my_skill_infos = {s["name"]: s for s in all_skills}
        peer_skill_infos = {}
        for s in peer_skills:
            if isinstance(s, dict):
                peer_skill_infos[s.get("name", "")] = s
            else:
                peer_skill_infos[s] = {"name": s}

        steps = []
        for s in my_skill_names:
            steps.append({
                "skill": s, "agent": self.name,
                "agent_wallet": self.wallet,
                "input": _build_skill_input(my_skill_infos.get(s, {}), goal),
            })
        for s in peer_skill_names:
            steps.append({
                "skill": s, "agent": peer_name,
                "agent_wallet": wallet_address,
                "input": _build_skill_input(peer_skill_infos.get(s, {}), goal),
            })

        if not steps:
            self._collab_status["status"] = "failed"
            return {"success": False, "error": "No skills available on either side"}

        # Build OKR-style view for UI
        collab_steps_ui = []
        for i, step in enumerate(steps):
            collab_steps_ui.append({
                "index": i, "skill": step["skill"],
                "agent": step["agent"], "status": "pending",
                "duration_ms": None, "result_preview": None,
            })
        self._collab_status["steps"] = collab_steps_ui
        self._collab_status["status"] = "proposing"

        # Create session
        session_id = _uid()
        session = {
            "id": session_id, "state": "proposed",
            "privacy": "L1_PUBLIC", "trust": "KNOWN",
            "calls_used": 0, "calls_max": len(steps),
        }
        self._collab_status["session"] = session
        self._sessions.append({
            "session_id": session_id, "peer_id": peer_name,
            "role": "initiator", "state": "proposed",
            "proposed_skills": [s["skill"] for s in steps],
            "agreed_skills": [], "agreed_trust_tier": 1,
            "agreed_max_context_privacy": "L1_PUBLIC",
            "agreed_max_calls": len(steps), "call_count": 0,
            "tasks_completed": 0, "tasks_failed": 0,
            "created_at": _now_iso(), "accepted_at": None,
            "closed_at": None, "expires_at": _now_iso(),
        })

        # Post OKR to group
        step_summary = ", ".join(f"{s['skill']}({s['agent']})" for s in steps)
        _collab_msg(self.wallet, self.name,
                    f"[Plan] {len(steps)} steps: {step_summary}")

        # 3. Propose plan to peer
        print(f"\n  Step 2: Proposing plan ({len(steps)} steps)...")
        plan_corr = _uid()
        self.client.send(wallet_address, "plan_propose", {
            "goal": goal, "steps": steps,
            "initiator": self.name, "initiator_wallet": self.wallet,
        }, correlation_id=plan_corr)
        # Record plan proposal DM
        self._store_dm_message(
            peer_wallet=wallet_address, peer_name=peer_name_early,
            direction="outbound", msg_type="plan_propose",
            content=f"Plan: {len(steps)} steps — {step_summary}",
            payload={"goal": goal, "steps": [s["skill"] for s in steps]},
            correlation_id=plan_corr, phase="plan",
            collab_id=collab_id)
        self._track_corr(plan_corr, wallet_address, peer_name_early,
                        phase="plan", collab_id=collab_id,
                        request_type="plan_propose")

        accepted = False
        for _ in range(20):
            time.sleep(1)
            if plan_corr in self._response_box:
                resp = self._response_box.pop(plan_corr)
                if resp.get("type") == "plan_accept":
                    accepted = True
                    session["state"] = "active"
                    self._collab_status["status"] = "executing"
                    # Update session in list
                    for s in self._sessions:
                        if s["session_id"] == session_id:
                            s["state"] = "active"
                            s["accepted_at"] = _now_iso()
                    print(f"  ✓ Peer accepted the plan")
                    _collab_msg(wallet_address, peer_name_early,
                                f"[Accept] Plan accepted — ready to execute")
                    break
                elif resp.get("type") == "plan_reject":
                    reason = resp.get("payload", {}).get("reason", "unknown")
                    self._collab_status["status"] = "rejected"
                    return {"success": False, "error": f"Peer rejected plan: {reason}"}

        if not accepted:
            self._collab_status["status"] = "timeout"
            return {"success": False, "error": "Peer did not respond to plan (timeout)"}

        # Initialize cost tracking
        cost_alpha = 0.0
        cost_beta = 0.0

        # 4. Execute steps
        print(f"\n  Step 3: Executing plan...\n")
        results = []
        for i, step in enumerate(steps):
            sk = step["skill"]
            agent_name = step["agent"]
            is_local = step["agent_wallet"] == self.wallet
            collab_steps_ui[i]["status"] = "in_progress"
            self._current_step_index = i  # for call() DM tracking

            print(f"  [{i+1}/{len(steps)}] {sk} ({agent_name}) ...", end=" ", flush=True)

            # Post step-start to group
            executor_wallet = self.wallet if is_local else wallet_address
            _collab_msg(executor_wallet, agent_name,
                        f"[Step {i+1}/{len(steps)}] Executing: {sk}")

            start_t = time.time()
            if is_local:
                result = self.executor.execute(sk, step["input"])
            else:
                result = self.call(wallet_address, sk, step["input"], timeout=30, retries=2)

            duration = (time.time() - start_t) * 1000
            success = result.get("success", False)

            # Post step-result to group
            output = result.get("result", result.get("output", ""))
            preview = str(output)[:150] if output else "done"
            status_icon = "✓" if success else "✗"
            _collab_msg(executor_wallet, agent_name,
                        f"[Step {i+1}/{len(steps)}] {status_icon} {sk} — {round(duration)}ms\n{preview}")
            step["result"] = result
            step["status"] = "done" if success else "failed"
            results.append(result)

            # Update UI step
            collab_steps_ui[i]["status"] = "completed" if success else "failed"
            collab_steps_ui[i]["duration_ms"] = round(duration, 1)
            output = result.get("result", result.get("output", ""))
            collab_steps_ui[i]["result_preview"] = str(output)[:120] if output else None

            # Track cost
            if is_local:
                cost_alpha += duration
            else:
                cost_beta += duration

            # Update session counters
            session["calls_used"] = i + 1
            for s in self._sessions:
                if s["session_id"] == session_id:
                    s["call_count"] = i + 1
                    if success:
                        s["tasks_completed"] += 1
                    else:
                        s["tasks_failed"] += 1

            print("✓" if success else "✗")

        # 5. Summary
        all_ok = all(r.get("success") for r in results)
        self._collab_status["status"] = "completed" if all_ok else "partial"
        self._collab_status["cost"] = {
            "alpha": {"total_ms": round(cost_alpha, 1)},
            "beta": {"total_ms": round(cost_beta, 1)},
        }
        self._collab_status["result"] = "All steps completed successfully" if all_ok else \
            f"{sum(1 for r in results if r.get('success'))}/{len(results)} steps succeeded"

        # Build OKR
        completed = sum(1 for r in results if r.get("success"))
        self._collab_status["okr"] = {
            "okr_id": session_id,
            "overall_progress": round(completed / len(results) * 100) if results else 0,
            "key_results": [
                {
                    "kr_id": f"kr_{i}",
                    "description": f"Execute {s['skill']} ({s['agent']})",
                    "metric": s["skill"],
                    "progress": 100 if collab_steps_ui[i]["status"] == "completed" else 0,
                    "status": collab_steps_ui[i]["status"],
                    "task_count": 1,
                    "completed_count": 1 if collab_steps_ui[i]["status"] == "completed" else 0,
                }
                for i, s in enumerate(steps)
            ],
        }

        # Mark session completed
        session["state"] = "completed"
        for s in self._sessions:
            if s["session_id"] == session_id:
                s["state"] = "completed"
                s["closed_at"] = _now_iso()

        self._log_activity("workflow", f"Collaboration {'completed' if all_ok else 'partial'}: \"{goal[:40]}\"",
                         f"{completed}/{len(results)} steps succeeded",
                         peer=peer_name,
                         status="completed" if all_ok else "partial")

        n_ok = sum(1 for r in results if r.get('success'))
        print(f"\n  ── Result: {'SUCCESS' if all_ok else 'PARTIAL'} ({n_ok}/{len(results)} steps) ──\n")

        # Post completion to group
        _collab_msg(self.wallet, self.name,
                    f"[Complete] {'SUCCESS' if all_ok else 'PARTIAL'} — {n_ok}/{len(results)} steps succeeded")

        # ── OKR-complete trust downgrade ──
        if all_ok and hasattr(self, '_trust'):
            prev = self._trust.get_trust_tier(wallet_address)
            new = self._trust.downgrade_after_okr(wallet_address)
            if new < prev:
                print(f"  ⤵ Trust auto-downgraded: {wallet_address[:16]}… "
                      f"{prev}→{new} (OKR completed)")
                from ._internal.security import TrustTier
                _collab_msg(self.wallet, self.name,
                            f"[Trust] Auto-downgraded: {peer_name_early} "
                            f"{TrustTier(prev).name}→{TrustTier(new).name} (OKR completed)")

        # Record collaboration complete in DM
        self._store_dm_message(
            peer_wallet=wallet_address, peer_name=peer_name_early,
            direction="outbound", msg_type="collab_complete",
            content=f"Collaboration {'completed' if all_ok else 'partial'}: {n_ok}/{len(results)} steps",
            phase="report", collab_id=collab_id)
        # Cleanup
        self._current_collab_id = ""
        self._current_step_index = -1
        # Mark conversation collab as inactive
        conv_id = self._dm_conv_id(wallet_address)
        with self._dm_lock:
            if conv_id in self._dm_conversations:
                self._dm_conversations[conv_id]["collab_active"] = False

        return {
            "goal": goal, "success": all_ok,
            "steps": steps, "results": results,
            "peer": wallet_address,
        }

    # ── Peer Management ──────────────────────────────────────

    def _load_peers(self) -> dict:
        path = Path(self.data_dir) / "peers.json"
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {}

    def _save_peers(self, peers: dict):
        os.makedirs(self.data_dir, exist_ok=True)
        with open(Path(self.data_dir) / "peers.json", "w") as f:
            json.dump(peers, f, indent=2)

    # ── API Data Builders ────────────────────────────────────

    def _build_stats(self) -> dict:
        peers = self._load_peers()
        task_states = {}
        for t in self._tasks:
            st = t.get("state", "unknown")
            task_states[st] = task_states.get(st, 0) + 1
        return {
            "agent": {
                "agent_id": self.name,
                "wallet": self.wallet,
                "display_name": self.name,
                "agent_number": hash(self.name) % 10000,
            },
            "inbox_count": self._message_counts.get("task_request", 0) +
                          self._message_counts.get("discover", 0),
            "outbox_count": self._message_counts.get("task_response", 0) +
                          self._message_counts.get("capabilities", 0),
            "task_count": len(self._tasks),
            "peer_count": len(peers),
            "active_sessions": sum(1 for s in self._sessions if s.get("state") == "active"),
            "running_workflows": 0,
            "message_types": dict(self._message_counts),
            "task_states": task_states,
            "uptime_seconds": round(time.time() - self._start_time),
        }

    def _build_peers(self) -> list:
        peers = self._load_peers()
        result = []
        for name, data in peers.items():
            skills = data.get("skills", [])
            skill_names = [s["name"] if isinstance(s, dict) else s for s in skills]
            result.append({
                "name": name,
                "wallet": data.get("wallet", ""),
                "last_seen": data.get("connected_at", _now_iso()),
                "is_online": True,
                "trust_tier": self.trust_manager.get_trust_tier(data.get("wallet", "")),
                "latency_ms": None,
                "skills": skill_names,
            })
        return result

    def _build_tasks(self, state=None, skill=None, limit=50) -> list:
        tasks = list(reversed(self._tasks))
        if state:
            tasks = [t for t in tasks if t.get("state") == state]
        if skill:
            tasks = [t for t in tasks if t.get("skill") == skill]
        return tasks[:limit]

    # ── Skill Visibility ─────────────────────────────────────

    def _init_skill_visibility(self, expose_skills_param):
        """Initialize skill visibility: first-run guide, config load, or override."""
        from ._internal.skill_visibility import (
            SkillVisibilityConfig, compute_effective_exposed,
            run_first_time_guide, print_new_skill_reminder,
        )

        registered = self.executor.skill_names
        config = SkillVisibilityConfig(self.data_dir, agent_id=self.name)
        self._visibility_config = config

        # Runtime override (code parameter) — takes full precedence, API POST cannot overwrite
        self._expose_skills_override = expose_skills_param
        if expose_skills_param is not None:
            if expose_skills_param == "all":
                self._exposed_set = set(registered)
                print(f"  Skills: all {len(registered)} exposed (runtime override)")
            else:
                self._exposed_set = compute_effective_exposed(
                    registered, config, expose_skills_override=list(expose_skills_param))
                print(f"  Skills: {len(self._exposed_set)}/{len(registered)} exposed (runtime override)")
            return

        # Load persistent config
        config.load()
        merge = config.merge_discovered_skills(registered)

        if not merge["has_config"]:
            # First run — interactive guide
            is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
            if is_tty and registered:
                self._exposed_set = run_first_time_guide(registered, config)
            elif registered:
                # Non-TTY: safe default (all hidden)
                print("  [warning] Non-interactive terminal detected.")
                print("  [warning] All skills remain hidden until configured.")
                print("  [warning] Run `coworker skills configure` in a TTY to review.")
                config.set_all_hidden(registered)
                if not config.save():
                    print("  [warning] Could not persist config. Hidden for this session only.")
                self._exposed_set = set()
            else:
                self._exposed_set = set()
        else:
            # Existing config — load and check for new skills
            if merge["new_skills"]:
                print_new_skill_reminder(merge["new_skills"])
                config.save()  # persist new pending_review entries

            self._exposed_set = compute_effective_exposed(registered, config)
            exposed_count = len(self._exposed_set)
            total = len(registered)
            if exposed_count == total:
                print(f"  Skills: all {total} exposed")
            else:
                hidden = total - exposed_count
                print(f"  Skills: {exposed_count}/{total} exposed, {hidden} hidden")

    def _update_visibility(self, skill_name: str, state: str) -> bool:
        """Update a skill's visibility at runtime. Returns True on success.

        If serve(expose_skills=...) runtime override is active, config is
        persisted for future runs but does NOT change the current exposed set.
        """
        if self._visibility_config is None:
            return False
        # Validate skill exists
        if skill_name not in self.executor.skill_names:
            return False
        self._visibility_config.set_state(skill_name, state)
        if not self._visibility_config.save():
            return False
        # Hot-update exposed set only if no runtime override
        if self._expose_skills_override is None:
            registered = self.executor.skill_names
            from ._internal.skill_visibility import compute_effective_exposed
            self._exposed_set = compute_effective_exposed(
                registered, self._visibility_config)
        return True

    # ── Serve ────────────────────────────────────────────────

    def serve(self, monitor_port: int = 8090, expose_skills: list | str | None = None):
        """Start the agent: XMTP listener + local HTTP dashboard.

        This is the main entry point. It:
        1. Runs skill visibility check (first-run guide or config load)
        2. Starts (or connects to) the XMTP bridge
        3. Begins polling for incoming messages
        4. Starts a local HTTP server with full API + React frontend
        5. Blocks until Ctrl+C

        Args:
            monitor_port: Port for local HTTP dashboard.
            expose_skills: Override which skills are exposed to peers.
                - None: use persistent config (or first-run guide)
                - "all": expose all registered skills
                - list of names: only these skills are exposed
        """
        self._running = True
        self._monitor_port = monitor_port
        self._start_time = time.time()
        os.makedirs(self.data_dir, exist_ok=True)

        # 0. Skill visibility
        self._init_skill_visibility(expose_skills)

        # 1. Ensure bridge is running
        from ._internal.bridge import check_running, start as start_bridge
        bridge = check_running(Path(self.data_dir))
        if not bridge:
            print("  Starting XMTP bridge...")
            bridge = start_bridge(Path(self.data_dir))
            if "error" in bridge:
                print(f"  ⚠ Bridge error: {bridge['error']}")
                print("  Running in offline mode (no XMTP)")
            else:
                print(f"  ✓ XMTP connected: {bridge.get('address', '?')[:16]}...")
        else:
            print(f"  ✓ XMTP already running on port {bridge['port']}")

        # Sync real XMTP address to config.json (bridge derives it from private key)
        xmtp_addr = bridge.get("address", "") if isinstance(bridge, dict) else ""
        if xmtp_addr and xmtp_addr != self.wallet:
            config_path = os.path.join(self.data_dir, "config.json")
            try:
                with open(config_path) as f:
                    cfg = json.load(f)
                cfg["wallet"] = xmtp_addr
                with open(config_path, "w") as f:
                    json.dump(cfg, f, indent=2)
                print(f"  ✓ Synced wallet address: {xmtp_addr[:16]}...")
            except (json.JSONDecodeError, IOError, OSError):
                pass  # Non-fatal: wallet property still works with old address

        # 2. Start XMTP poll loop
        poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        poll_thread.start()

        # 3. Locate frontend static files
        frontend_dir = self._find_frontend_dir()

        # 4. Build HTTP monitor + API server
        agent = self
        fe_dir = frontend_dir

        class MonitorHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args): pass

            def do_HEAD(self):
                """Support HEAD requests (browsers/health checks send these)."""
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()

            def _json(self, data, status=200):
                body = json.dumps(data, default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _parse_qs(self):
                parsed = urlparse(self.path)
                return parse_qs(parsed.query)

            def _serve_file(self, filepath):
                """Serve a static file."""
                try:
                    with open(filepath, "rb") as f:
                        content = f.read()
                    mime = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(content)))
                    if mime in ("application/javascript", "text/css"):
                        self.send_header("Cache-Control", "public, max-age=31536000")
                    self.end_headers()
                    self.wfile.write(content)
                except FileNotFoundError:
                    self._json({"error": "not found"}, 404)

            def do_OPTIONS(self):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def do_PATCH(self):
                p = self.path.split("?")[0].rstrip("/")
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len) if content_len > 0 else b""

                # PATCH /api/peers/<wallet>/trust
                parts = p.split("/")
                if (len(parts) == 5 and parts[1] == "api" and parts[2] == "peers"
                        and parts[4] == "trust"):
                    wallet = parts[3]
                    try:
                        data = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        self._json({"error": "invalid JSON"}, 400)
                        return
                    tier = data.get("trust_tier")
                    if tier is None or int(tier) not in (0, 1, 2, 3):
                        self._json({"error": "trust_tier must be 0-3"}, 400)
                        return
                    from ._internal.security import TrustTier
                    agent.trust_manager.set_trust_override(wallet, int(tier))
                    label = TrustTier(int(tier)).name.lower()
                    print(f"  ✓ Trust set: {wallet[:12]}... → {label}")
                    self._json({"ok": True, "wallet": wallet, "trust_tier": int(tier),
                                "trust_label": label})
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                p = self.path.split("?")[0].rstrip("/")
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len) if content_len > 0 else b""

                # Trigger collaboration
                if p == "/api/collaborate":
                    try:
                        data = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        data = {}
                    wallet = data.get("wallet", "")
                    goal = data.get("goal", "Collaborate on a shared task")
                    if not wallet:
                        self._json({"error": "wallet address required"}, 400)
                        return
                    self._json({"status": "started", "goal": goal, "wallet": wallet})
                    # Run in background thread
                    def _run():
                        agent.collaborate(wallet, goal)
                    threading.Thread(target=_run, daemon=True).start()
                    return

                # Trigger remote skill call
                if p == "/api/call":
                    try:
                        data = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        data = {}
                    wallet = data.get("wallet", "")
                    skill = data.get("skill", "")
                    input_data = data.get("input", {})
                    if not wallet or not skill:
                        self._json({"error": "wallet and skill required"}, 400)
                        return
                    self._json({"status": "started", "skill": skill})
                    def _run_call():
                        result = agent.call(wallet, skill, input_data)
                        print(f"  Remote call result: {result}")
                    threading.Thread(target=_run_call, daemon=True).start()
                    return

                # Session actions
                if p.startswith("/api/sessions/") and p.count("/") >= 4:
                    parts = p.split("/")
                    if len(parts) >= 5:
                        session_id = parts[3]
                        action = parts[4]
                        for s in agent._sessions:
                            if s["session_id"] == session_id:
                                if action == "accept":
                                    s["state"] = "active"
                                    s["accepted_at"] = _now_iso()
                                elif action == "reject":
                                    s["state"] = "rejected"
                                elif action == "close":
                                    s["state"] = "closed"
                                    s["closed_at"] = _now_iso()
                                self._json({"ok": True})
                                return
                    self._json({"error": "session not found"}, 404)

                # Create group
                elif p == "/api/groups":
                    try:
                        data = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        data = {}
                    members = data.get("members", [])
                    name = data.get("name", "")
                    if not members:
                        self._json({"error": "members required"}, 400)
                        return
                    try:
                        group = agent.create_group(name or "Unnamed", members)
                        self._json({
                            "group_id": group.group_id,
                            "name": group.name,
                            "member_count": len(members) + 1,
                        })
                    except Exception as e:
                        self._json({"error": str(e)}, 500)
                    return

                # Send message to group
                elif p.startswith("/api/groups/") and p.endswith("/send"):
                    parts = p.split("/")
                    group_id = parts[3]
                    try:
                        data = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        data = {}

                    content = data.get("content", "")
                    client_msg_id = data.get("client_message_id", "")
                    content_type = data.get("content_type", "text/plain")

                    if not content:
                        self._json({"error": "content required"}, 400)
                        return

                    if group_id not in agent._groups:
                        self._json({"error": "group not found"}, 404)
                        return

                    # Idempotency check — scoped per (group_id, client_message_id)
                    idem_key = (group_id, client_msg_id) if client_msg_id else None
                    if idem_key and idem_key in agent._client_msg_ids:
                        # Return existing message if still in deque
                        for m in reversed(agent._group_messages):
                            if m["client_message_id"] == client_msg_id and m["group_id"] == group_id:
                                self._json({"message": m})
                                return
                        # ID tracked but message expired from deque — return ack without re-sending
                        self._json({"message": {
                            "id": "", "client_message_id": client_msg_id,
                            "group_id": group_id, "delivery_status": "sent",
                            "content": content, "sender_wallet": agent.wallet,
                            "sender_name": agent.name, "note": "already_sent",
                        }})
                        return

                    # Store locally
                    msg = agent._store_group_message(
                        group_id=group_id,
                        sender_wallet=agent.wallet,
                        sender_name=agent.name,
                        content=content,
                        content_type=content_type,
                        delivery_status="pending",
                        client_message_id=client_msg_id,
                    )
                    if idem_key:
                        agent._client_msg_ids.add(idem_key)
                        # Cap idempotency set to prevent unbounded growth
                        if len(agent._client_msg_ids) > 5000:
                            excess = len(agent._client_msg_ids) - 4000
                            for _ in range(excess):
                                agent._client_msg_ids.pop()

                    # Send to XMTP group
                    try:
                        agent.client.group_send(group_id, "group_message", {
                            "text": content,
                            "sender": agent.name,
                            "sender_wallet": agent.wallet,
                        })
                        msg["delivery_status"] = "sent"
                    except Exception as e:
                        msg["delivery_status"] = "failed"
                        # Remove idem_key so client can retry with same client_message_id
                        if idem_key:
                            agent._client_msg_ids.discard(idem_key)

                    self._json({"message": msg})

                # Async task request via API
                elif p == "/api/async-tasks":
                    try:
                        data = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        data = {}
                    wallet = data.get("wallet", "")
                    skill = data.get("skill", "")
                    input_data = data.get("input", {})
                    if not wallet or not skill:
                        self._json({"error": "wallet and skill required"}, 400)
                        return
                    task_id = agent.request(wallet, skill, input_data)
                    self._json({"task_id": task_id, "state": "queued"})
                    return

                # Skill visibility toggle
                elif p == "/api/skills/visibility":
                    try:
                        data = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        data = {}
                    skill_name = data.get("skill", "")
                    state = data.get("state", "")
                    if not skill_name or state not in ("exposed", "hidden"):
                        self._json({"error": "skill and state (exposed/hidden) required"}, 400)
                        return
                    if skill_name not in agent.executor.skill_names:
                        self._json({"error": f"skill '{skill_name}' not registered"}, 404)
                        return
                    runtime_override = agent._expose_skills_override is not None
                    if agent._update_visibility(skill_name, state):
                        resp = {"ok": True, "skill": skill_name, "state": state}
                        if runtime_override:
                            resp["warning"] = "runtime override active; change saved for next restart"
                        self._json(resp)
                    else:
                        self._json({"error": "failed to update visibility"}, 500)

                else:
                    self._json({"error": "not found"}, 404)

            def do_PATCH(self):
                p = self.path.split("?")[0].rstrip("/")
                if p == "/api/settings/context-policy":
                    self._json({"ok": True})
                elif p.startswith("/api/peers/") and p.endswith("/trust"):
                    # Legacy POST path — redirect to PATCH logic
                    try:
                        data = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        data = {}
                    tier = data.get("trust_tier")
                    if tier is not None and int(tier) in (0, 1, 2, 3):
                        from ._internal.security import TrustTier
                        wallet = p.split("/")[3]
                        agent.trust_manager.set_trust_override(wallet, int(tier))
                        self._json({"ok": True, "wallet": wallet, "trust_tier": int(tier)})
                    else:
                        self._json({"ok": True})
                else:
                    self._json({"error": "not found"}, 404)

            def do_GET(self):
                p = self.path.split("?")[0].rstrip("/") or "/"
                qs = self._parse_qs()

                # ── API Endpoints ──
                if p == "/api/health":
                    self._json({"status": "ok", "name": agent.name})

                elif p == "/api/stats":
                    self._json(agent._build_stats())

                elif p == "/api/agent/profile":
                    self._json({
                        "agent_id": agent.name,
                        "wallet": agent.wallet,
                        "display_name": agent.name,
                        "agent_number": hash(agent.name) % 10000,
                    })

                elif p == "/api/discover":
                    # Respect visibility: only show exposed skills
                    all_skills = agent.executor.list_skills()
                    if agent._exposed_set is not None:
                        visible = [s for s in all_skills if s["name"] in agent._exposed_set]
                    else:
                        visible = all_skills
                    self._json({
                        "name": agent.name, "wallet": agent.wallet,
                        "skills": visible, "version": "0.4.1",
                    })

                elif p == "/api/invite":
                    # Only include exposed skills in invite code
                    all_sk = agent.executor.list_skills()
                    if agent._exposed_set is not None:
                        exposed_sk = [s for s in all_sk if s["name"] in agent._exposed_set]
                    else:
                        exposed_sk = all_sk
                    invite_data = {
                        "name": agent.name,
                        "wallet": agent.wallet,
                        "skills": [s["name"] for s in exposed_sk],
                        "v": "0.4.1",
                    }
                    code = base64.b64encode(json.dumps(invite_data, separators=(",", ":")).encode()).decode()
                    # Short code: first 4 chars of wallet + agent name
                    short = f"{agent.name}-{agent.wallet[2:6]}".lower()
                    self._json({
                        "invite_code": code,
                        "short_code": short,
                        "wallet": agent.wallet,
                        "name": agent.name,
                        "cli_command": f"coworker connect {code}",
                    })

                elif p == "/api/conversations":
                    # Unified conversation list: DM + Group
                    try:
                        convos = []
                        for conv in list(agent._dm_conversations.values()):
                            convos.append(conv)
                        for gid, grp in list(agent._groups.items()):
                            group_msgs = [m for m in agent._group_messages if m["group_id"] == gid]
                            last_msg = group_msgs[-1] if group_msgs else None
                            convos.append({
                                "id": f"group:{gid}",
                                "kind": "group",
                                "peer_wallet": "",
                                "peer_name": grp.get("name", "Group"),
                                "trust_tier": "",
                                "created_at": grp.get("created_at", ""),
                                "updated_at": last_msg["created_at"] if last_msg else grp.get("created_at", ""),
                                "last_message_id": last_msg["id"] if last_msg else "",
                                "last_message_at": last_msg["created_at"] if last_msg else "",
                                "last_message": {"content": last_msg["content"][:80], "msg_type": "group_message"} if last_msg else None,
                                "unread_count": 0,
                                "collab_active": False,
                                "message_count": len(group_msgs),
                                "member_count": len(grp.get("members", [])) + 1,
                            })
                        convos.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
                        self._json({"conversations": convos, "server_time": _now_iso()})
                    except Exception as exc:
                        self._json({"error": str(exc), "conversations": []}, 500)
                    return

                elif p == "/api/peers":
                    self._json(agent._build_peers())

                elif p.startswith("/api/peers/") and p.endswith("/detail"):
                    peer_id = p.split("/")[3]
                    peers = agent._build_peers()
                    peer = next((pp for pp in peers if pp["name"] == peer_id), None)
                    if peer:
                        peer["reputation"] = {
                            "peer_id": peer_id,
                            "total_interactions": len([t for t in agent._tasks
                                                     if t.get("peer_name", "").startswith(peer_id)]),
                            "successes": 0, "failures": 0, "success_rate": 1.0,
                            "avg_latency_ms": None, "first_seen": peer["last_seen"],
                            "last_seen": peer["last_seen"], "current_tier": 1,
                        }
                        peer["skill_cards"] = [
                            {"skill_name": s, "description": s}
                            for s in peer.get("skills", [])
                        ]
                        self._json(peer)
                    else:
                        self._json({"error": "peer not found"}, 404)

                elif p == "/api/tasks":
                    state = qs.get("state", [None])[0]
                    skill = qs.get("skill", [None])[0]
                    limit = int(qs.get("limit", ["50"])[0])
                    self._json({"tasks": agent._build_tasks(state, skill, limit)})

                elif p == "/api/messages":
                    # Unified message fetch by conversation_id
                    conv_id = qs.get("conversation_id", [None])[0]
                    after = qs.get("after", [None])[0]
                    limit = int(qs.get("limit", ["50"])[0])

                    if not conv_id:
                        # Legacy: return all DM messages
                        msgs = list(agent._dm_messages)[-limit:]
                        self._json({"messages": msgs, "total": len(agent._dm_messages)})
                        return

                    if conv_id.startswith("dm:"):
                        # DM messages
                        msgs = [m for m in agent._dm_messages if m["conversation_id"] == conv_id]
                    elif conv_id.startswith("group:"):
                        gid = conv_id[6:]
                        msgs = [m for m in agent._group_messages if m["group_id"] == gid]
                    else:
                        self._json({"error": "invalid conversation_id"}, 400)
                        return

                    # Apply cursor
                    if after:
                        found_idx = -1
                        for i, m in enumerate(msgs):
                            if m["id"] == after:
                                found_idx = i
                                break
                        if found_idx >= 0:
                            msgs = msgs[found_idx + 1:]
                        else:
                            msgs = []

                    has_more = len(msgs) > limit
                    msgs = msgs[:limit]
                    last_id = msgs[-1]["id"] if msgs else after

                    self._json({
                        "conversation_id": conv_id,
                        "messages": msgs,
                        "paging": {
                            "after": after,
                            "limit": limit,
                            "returned": len(msgs),
                            "has_more": has_more,
                            "last_message_id": last_id,
                        },
                        "total": len(msgs),
                        "server_time": _now_iso(),
                    })

                elif p == "/api/activity":
                    limit = int(qs.get("limit", ["20"])[0])
                    items = list(agent._activity)[:limit]
                    self._json(items)

                elif p == "/api/sessions":
                    state = qs.get("state", [None])[0]
                    sessions = agent._sessions
                    if state:
                        sessions = [s for s in sessions if s.get("state") == state]
                    self._json(sessions)

                elif p == "/api/workflows":
                    # Map collab status to a workflow if available
                    workflows = []
                    cs = agent._collab_status
                    if cs:
                        wf_steps = []
                        for step in cs.get("steps", []):
                            wf_steps.append({
                                "step_id": f"step_{step.get('index', 0)}",
                                "skill": step.get("skill", ""),
                                "target_peer": step.get("agent", ""),
                                "state": step.get("status", "pending"),
                                "depends_on": [],
                                "input_template": None,
                                "resolved_input": None,
                                "output": step.get("result_preview"),
                                "task_id": None,
                                "error_message": None,
                                "dispatched_at": _now_iso(),
                                "completed_at": _now_iso() if step.get("status") in ("completed", "failed") else None,
                            })
                        state_map = {
                            "discovering": "running", "proposing": "running",
                            "executing": "running", "completed": "completed",
                            "partial": "completed", "failed": "failed",
                        }
                        workflows.append({
                            "workflow_id": cs.get("session", {}).get("id", _uid()),
                            "name": cs.get("goal", "Collaboration"),
                            "state": state_map.get(cs.get("status", ""), "draft"),
                            "steps": wf_steps,
                            "created_at": _now_iso(),
                            "started_at": _now_iso(),
                            "completed_at": _now_iso() if cs.get("status") in ("completed", "partial") else None,
                            "timeout_seconds": 120,
                            "initiator_id": agent.name,
                            "error_message": None,
                        })
                    self._json(workflows)

                elif p == "/api/skills/visibility":
                    all_skills = agent.executor.list_skills()
                    result = []
                    for s in all_skills:
                        name = s["name"]
                        is_exposed = (agent._exposed_set is None or name in agent._exposed_set)
                        pending = False
                        if agent._visibility_config:
                            pending = agent._visibility_config.is_pending_review(name)
                        result.append({
                            "name": name,
                            "description": s.get("description", ""),
                            "min_trust_tier": s.get("min_trust_tier", 1),
                            "state": "exposed" if is_exposed else "hidden",
                            "pending_review": pending,
                        })
                    self._json({"skills": result})

                elif p == "/api/skill-cards":
                    # Owner dashboard view — shows all skills with visibility info
                    # This is a local-only endpoint for the agent owner's management
                    cards = []
                    for s in agent.executor.list_skills():
                        cards.append({
                            "skill_name": s["name"],
                            "skill_version": "1.0",
                            "description": s.get("description", ""),
                            "min_trust_tier": s.get("min_trust_tier", 1),
                            "max_context_privacy_tier": s.get("max_context_privacy_tier", "L1_PUBLIC"),
                            "tags": [],
                            "provider": {
                                "agent_id": agent.name,
                                "wallet": agent.wallet,
                                "display_name": agent.name,
                            },
                        })
                    self._json(cards)

                elif p == "/api/async-tasks":
                    state_filter = qs.get("state", [None])[0]
                    limit = int(qs.get("limit", ["20"])[0])
                    tasks = agent.list_async_tasks(state=state_filter, limit=limit)
                    self._json({"tasks": tasks, "total": len(tasks)})

                elif p.startswith("/api/async-tasks/") and not p.endswith("/"):
                    tid = p.split("/")[-1]
                    result = agent.get_result(tid)
                    self._json(result)

                elif p == "/api/metering/receipts":
                    limit = int(qs.get("limit", ["50"])[0])
                    self._json(list(reversed(agent._metering))[:limit])

                elif p == "/api/settings/context-policy":
                    self._json({
                        "autonomy_level": 1,
                        "category_policies": {},
                    })

                elif p == "/api/collab/status":
                    if agent._collab_status:
                        self._json(agent._collab_status)
                    else:
                        self._json({
                            "goal": "", "status": "idle",
                            "result": None, "steps": [],
                        })

                # ── Group Chat API ──

                elif p == "/api/groups":
                    groups_list = []
                    # Build message ID → message index for O(1) lookup
                    msg_by_id = {m["id"]: m for m in agent._group_messages}
                    for gid, gdata in agent._groups.items():
                        lm_id = gdata.get("last_message_id")
                        groups_list.append({
                            **gdata,
                            "last_message": msg_by_id.get(lm_id) if lm_id else None,
                            "unread_count": 0,
                        })
                    # Sort by last activity
                    groups_list.sort(key=lambda g: g.get("updated_at", ""), reverse=True)
                    self._json({
                        "groups": groups_list,
                        "next_cursor": None,
                        "server_time": _now_iso(),
                    })

                elif p.startswith("/api/groups/") and p.endswith("/messages"):
                    # GET /api/groups/:id/messages?after=<msg_id>&limit=50
                    parts = p.split("/")
                    group_id = parts[3]  # /api/groups/<id>/messages
                    after = qs.get("after", [None])[0]
                    limit = int(qs.get("limit", ["50"])[0])

                    # Filter messages for this group
                    group_msgs = [m for m in agent._group_messages if m["group_id"] == group_id]

                    # Incremental: only messages after the given ID
                    if after:
                        found_idx = -1
                        for i, m in enumerate(group_msgs):
                            if m["id"] == after:
                                found_idx = i
                                break
                        if found_idx >= 0:
                            group_msgs = group_msgs[found_idx + 1:]
                        else:
                            # after ID not found (expired from deque or invalid)
                            # Return empty — client should reset cursor
                            group_msgs = []

                    has_more = len(group_msgs) > limit
                    group_msgs = group_msgs[:limit]
                    last_id = group_msgs[-1]["id"] if group_msgs else after

                    self._json({
                        "group_id": group_id,
                        "messages": group_msgs,
                        "paging": {
                            "after": after,
                            "limit": limit,
                            "returned": len(group_msgs),
                            "has_more": has_more,
                            "last_message_id": last_id,
                        },
                        "server_time": _now_iso(),
                    })

                elif p.startswith("/api/groups/") and p.endswith("/detail"):
                    # GET /api/groups/:id/detail
                    parts = p.split("/")
                    group_id = parts[3]
                    gdata = agent._groups.get(group_id)
                    if gdata:
                        self._json({
                            "group_id": gdata["id"],
                            "name": gdata.get("name", ""),
                            "members": gdata.get("members", []),
                            "created_at": gdata.get("created_at", ""),
                            "server_time": _now_iso(),
                        })
                    else:
                        self._json({"error": "group not found"}, 404)

                # ── Static Frontend Files ──
                elif fe_dir:
                    # Map URL to file
                    if p == "/":
                        self._serve_file(os.path.join(fe_dir, "index.html"))
                    else:
                        filepath = os.path.join(fe_dir, p.lstrip("/"))
                        if os.path.isfile(filepath):
                            self._serve_file(filepath)
                        else:
                            # SPA fallback — serve index.html for all routes
                            self._serve_file(os.path.join(fe_dir, "index.html"))

                else:
                    # No frontend, serve basic HTML (only show exposed skills)
                    skills = agent.executor.list_skills()
                    if agent._exposed_set is not None:
                        skills = [s for s in skills if s["name"] in agent._exposed_set]
                    sk = "".join(f"<li><b>{s['name']}</b> — {s['description']}</li>" for s in skills)
                    peers = agent._load_peers()
                    pk = "".join(f"<li>{n} ({p.get('wallet','')[:12]}...)</li>" for n,p in peers.items())
                    html = f"""<!DOCTYPE html><html><head><title>{agent.name}</title>
<style>body{{font-family:system-ui;max-width:640px;margin:40px auto;padding:0 20px;color:#172B4D}}
code{{background:#F4F5F7;padding:2px 6px;border-radius:2px;font-size:13px}}</style></head>
<body><h1>{agent.name} · CoWorker Agent</h1>
<p><code>{agent.wallet}</code></p>
<h3>Skills</h3><ul>{sk or '<li>none</li>'}</ul>
<h3>Peers</h3><ul>{pk or '<li>none</li>'}</ul>
<p style="color:#8993A4;font-size:12px">CoWorker Protocol v0.4.0 · No frontend build found.
Run <code>cd frontend && npm run build</code> to enable the dashboard.</p>
</body></html>"""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    b = html.encode()
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)

        try:
            server = HTTPServer(("127.0.0.1", monitor_port), MonitorHandler)
            server.daemon_threads = True
        except OSError:
            server = None
            print(f"  ⚠ Monitor port {monitor_port} in use, skipping")

        # Print startup info
        skills = self.executor.skill_names
        print(f"\n  CoWorker Agent: {self.name}")
        print(f"  Wallet:         {self.wallet}")
        print(f"  Skills:         {', '.join(skills) if skills else '(none)'}")
        if server:
            print(f"  Dashboard:      http://localhost:{monitor_port}")
        if frontend_dir:
            print(f"  Frontend:       {frontend_dir}")
        print(f"  Listening for XMTP messages...")
        print(f"  Press Ctrl+C to stop\n")

        self._log_activity("session", f"Agent {self.name} started",
                         f"Skills: {', '.join(skills)}", status="online")

        def _stop(sig, frame):
            self._running = False
            print("\n  Shutting down...")
            if server:
                threading.Thread(target=server.shutdown).start()

        import threading as _threading
        if _threading.current_thread() is _threading.main_thread():
            signal.signal(signal.SIGINT, _stop)
            signal.signal(signal.SIGTERM, _stop)

        if server:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        else:
            try:
                while self._running:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass

        self._running = False
        print("  Agent stopped.")

    def _find_frontend_dir(self) -> str | None:
        """Find the frontend dist directory.

        Search order:
          1. COWORKER_FRONTEND_DIR env var (explicit override)
          2. Relative to SDK package: sdk/../frontend/dist (dev layout)
          3. Inside SDK package: coworker/_internal/frontend (pip-installed)
          4. data_dir/frontend (user-provided)
          5. Current working directory: ./frontend/dist
        """
        candidates = []

        # Explicit override via env var
        env_dir = os.environ.get("COWORKER_FRONTEND_DIR")
        if env_dir:
            candidates.append(Path(env_dir))

        # Relative to SDK package (monorepo dev layout: sdk/src/coworker/agent.py → frontend/dist)
        candidates.append(Path(__file__).parent.parent.parent.parent / "frontend" / "dist")

        # Bundled inside pip package
        candidates.append(Path(__file__).parent / "_internal" / "frontend")

        # data_dir/frontend
        candidates.append(Path(self.data_dir) / "frontend")

        # CWD fallback
        candidates.append(Path.cwd() / "frontend" / "dist")

        for candidate in candidates:
            index = candidate / "index.html"
            if index.exists():
                return str(candidate)
        return None
