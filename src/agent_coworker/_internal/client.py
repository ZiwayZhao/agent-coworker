#!/usr/bin/env python3
"""
AgentFax Client — protocol builder, parser, and XMTP bridge interface.

This is the core library for AgentFax. It handles:
  1. Building AgentFax protocol envelopes
  2. Parsing incoming messages
  3. Sending/receiving via the local XMTP bridge
"""

import base64
import json
import mimetypes
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any


PROTOCOL_NAME = "coworker"
PROTOCOL_VERSION = "1.0"


# ── Bridge communication ───────────────────────────────────────────

def _read_bridge_port(data_dir: str) -> int:
    """Read the XMTP bridge port from data directory."""
    port_file = Path(data_dir).expanduser() / "bridge_port"
    if not port_file.exists():
        raise FileNotFoundError(
            f"No bridge_port file at {port_file}. Is the XMTP bridge running?"
        )
    return int(port_file.read_text().strip())


def _bridge_url(data_dir: str, endpoint: str) -> str:
    port = _read_bridge_port(data_dir)
    return f"http://localhost:{port}{endpoint}"


def _bridge_get(data_dir: str, endpoint: str, params: dict = None) -> dict:
    url = _bridge_url(data_dir, endpoint)
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url += f"?{qs}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _bridge_post(data_dir: str, endpoint: str, body: dict) -> dict:
    url = _bridge_url(data_dir, endpoint)
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


# ── Protocol envelope ──────────────────────────────────────────────

def build_message(
    msg_type: str,
    payload: dict,
    sender_id: str = None,
    correlation_id: str = None,
    ttl: int = 3600,
    trace_id: str = None,
    span_id: str = None,
    parent_span_id: str = None,
    context: dict = None,
    trust_required: str = None,
    priority: str = None,
) -> dict:
    """Build an AgentFax protocol envelope."""
    msg = {
        "protocol": PROTOCOL_NAME,
        "version": PROTOCOL_VERSION,
        "type": msg_type,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ttl": ttl,
    }
    if sender_id:
        msg["sender_id"] = sender_id
    if correlation_id:
        msg["correlation_id"] = correlation_id
    if trace_id:
        msg["trace_id"] = trace_id
    if span_id:
        msg["span_id"] = span_id
    if parent_span_id:
        msg["parent_span_id"] = parent_span_id
    if context:
        msg["context"] = context
    if trust_required:
        msg["trust_required"] = trust_required
    if priority:
        msg["priority"] = priority

    return msg


def parse_message(raw_content: str) -> Optional[dict]:
    """Parse a raw XMTP message into an AgentFax envelope."""
    try:
        msg = json.loads(raw_content)
        if isinstance(msg, dict) and msg.get("protocol") == PROTOCOL_NAME:
            return msg
        return None
    except (json.JSONDecodeError, TypeError):
        return None


def is_expired(msg: dict) -> bool:
    """Check if a message has exceeded its TTL."""
    try:
        ts = datetime.fromisoformat(msg["timestamp"])
        ttl = msg.get("ttl", 3600)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > ttl
    except (KeyError, ValueError):
        return False


# ── High-level client ──────────────────────────────────────────────

class AgentFaxClient:
    """High-level AgentFax client wrapping the XMTP bridge."""

    def __init__(self, data_dir: str, transport=None):
        self.data_dir = str(Path(data_dir).expanduser())
        self._sender_id = self._load_sender_id()
        self._transport = transport

    def _load_sender_id(self) -> str:
        """Load agent name from config or chain identity."""
        for fname in ("chain_identity.json", "config.json"):
            fpath = os.path.join(self.data_dir, fname)
            if os.path.exists(fpath):
                with open(fpath) as f:
                    data = json.load(f)
                name = data.get("claw_name") or data.get("name") or data.get("peer_id")
                if name:
                    return name
        return "unknown"

    def _load_wallet_address(self) -> str:
        """Load wallet address from config."""
        for fname in ("config.json", "wallet.json"):
            fpath = os.path.join(self.data_dir, fname)
            if os.path.exists(fpath):
                with open(fpath) as f:
                    data = json.load(f)
                addr = data.get("wallet") or data.get("address")
                if addr:
                    return addr
        return ""

    def health(self) -> dict:
        """Check XMTP bridge health."""
        if self._transport:
            return self._transport.health()
        return _bridge_get(self.data_dir, "/health")

    def can_message(self, wallet_address: str) -> bool:
        """Check if a wallet address is reachable via XMTP."""
        result = _bridge_get(self.data_dir, "/can-message", {"address": wallet_address})
        return result.get("canMessage", False)

    def get_inbox_id(self) -> str:
        """Get this agent's XMTP inbox ID from the bridge."""
        result = _bridge_get(self.data_dir, "/inbox-id")
        return result.get("inboxId", "")

    def send(
        self,
        to_wallet: str,
        msg_type: str,
        payload: dict,
        correlation_id: str = None,
        ttl: int = 3600,
    ) -> dict:
        """Send a message to a wallet address or inbox ID.

        The bridge accepts both formats:
        - Wallet address (0x...): routes via Ethereum identifier
        - Inbox ID (hex string): routes via XMTP inbox ID directly
        """
        envelope = build_message(
            msg_type=msg_type,
            payload=payload,
            sender_id=self._sender_id,
            correlation_id=correlation_id,
            ttl=ttl,
        )
        # Include sender wallet so receiver can reply
        envelope["sender_wallet"] = self._load_wallet_address()
        if self._transport:
            return self._transport.send(to_wallet, json.dumps(envelope))
        return _bridge_post(self.data_dir, "/send", {
            "to": to_wallet,
            "content": json.dumps(envelope),
        })

    def send_file(
        self,
        to_wallet: str,
        file_path: str,
        msg_type: str = "file_transfer",
        correlation_id: str = None,
    ) -> dict:
        """Send a file as an inline XMTP attachment (< 1MB)."""
        fpath = Path(file_path).expanduser()
        if not fpath.exists():
            raise FileNotFoundError(f"File not found: {fpath}")

        file_bytes = fpath.read_bytes()
        if len(file_bytes) > 1_000_000:
            raise ValueError(
                f"File too large ({len(file_bytes)} bytes). "
                "Use send_remote_attachment for files > 1MB."
            )

        mime_type = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
        content_b64 = base64.b64encode(file_bytes).decode("ascii")

        result = _bridge_post(self.data_dir, "/send-attachment", {
            "to": to_wallet,
            "filename": fpath.name,
            "mimeType": mime_type,
            "content": content_b64,
        })

        envelope = build_message(
            msg_type=msg_type,
            payload={
                "filename": fpath.name,
                "mimeType": mime_type,
                "size": len(file_bytes),
                "message": f"File sent: {fpath.name}",
            },
            sender_id=self._sender_id,
            correlation_id=correlation_id or f"file_{int(time.time())}",
        )
        _bridge_post(self.data_dir, "/send", {
            "to": to_wallet,
            "content": json.dumps(envelope),
        })

        return result

    def send_image(self, to_wallet: str, image_path: str) -> dict:
        """Convenience method to send an image file."""
        return self.send_file(
            to_wallet, image_path,
            msg_type="image_transfer",
            correlation_id=f"img_{int(time.time())}",
        )

    def broadcast(
        self,
        wallets: List[str],
        msg_type: str,
        payload: dict,
        correlation_id: str = None,
        ttl: int = 3600,
    ) -> dict:
        """Send the same AgentFax message to multiple recipients."""
        envelope = build_message(
            msg_type=msg_type,
            payload=payload,
            sender_id=self._sender_id,
            correlation_id=correlation_id or f"bcast_{int(time.time())}",
            ttl=ttl,
        )
        return _bridge_post(self.data_dir, "/broadcast", {
            "to": wallets,
            "content": json.dumps(envelope),
        })

    def prewarm(self, target: str) -> dict:
        """Pre-warm a DM conversation to reduce first-call latency.

        Call this after discover succeeds to warm the return path.
        """
        try:
            return _bridge_post(self.data_dir, "/prewarm", {"to": target})
        except Exception:
            return {"status": "failed"}

    def receive(self, since: str = None, clear: bool = False) -> List[dict]:
        """Receive AgentFax messages from the bridge inbox."""
        if self._transport:
            raw_results = self._transport.receive()
            messages = []
            for wrapper in raw_results:
                for raw in wrapper.get("messages", []):
                    parsed = parse_message(raw.get("content", ""))
                    if parsed and not is_expired(parsed):
                        parsed["_xmtp_id"] = raw.get("id")
                        parsed["_xmtp_sent_at"] = raw.get("sentAt")
                        messages.append(parsed)
            return messages

        params = {}
        if since:
            params["since"] = since
        if clear:
            params["clear"] = "1"

        result = _bridge_get(self.data_dir, "/inbox", params if params else None)
        messages = []
        for raw in result.get("messages", []):
            content_type = raw.get("contentType", "text")

            if content_type in ("attachment", "remoteAttachment"):
                entry = {
                    "protocol": PROTOCOL_NAME,
                    "version": PROTOCOL_VERSION,
                    "type": "attachment_received",
                    "payload": {
                        "content_type": content_type,
                        "content": raw.get("content", ""),
                        "attachment": raw.get("attachment"),
                    },
                    "timestamp": raw.get("sentAt", datetime.now(timezone.utc).isoformat()),
                    "ttl": 86400,
                    "_xmtp_id": raw.get("id"),
                    "_xmtp_sender": raw.get("senderInboxId"),
                    "_xmtp_sent_at": raw.get("sentAt"),
                    "_xmtp_received_at": raw.get("receivedAt"),
                }
                messages.append(entry)
                continue

            parsed = parse_message(raw.get("content", ""))
            if parsed and not is_expired(parsed):
                parsed["_xmtp_id"] = raw.get("id")
                parsed["_xmtp_sender"] = raw.get("senderInboxId")
                parsed["_xmtp_sent_at"] = raw.get("sentAt")
                parsed["_xmtp_received_at"] = raw.get("receivedAt")
                parsed["_xmtp_conversation_id"] = raw.get("conversationId", "")
                parsed["_xmtp_is_group"] = raw.get("isGroup", False)
                messages.append(parsed)
        return messages

    # ── Group Chat Methods ──────────────────────────────────────────

    def create_group(
        self,
        member_wallets: List[str],
        name: str = "CoWorker Group",
        description: str = "",
    ) -> dict:
        """Create an XMTP group conversation with multiple members.

        Returns dict with groupId, name, members, memberCount.
        """
        if self._transport:
            raise NotImplementedError("Group chat not yet supported on custom transports")
        return _bridge_post(self.data_dir, "/create-group", {
            "members": member_wallets,
            "name": name,
            "description": description,
        })

    def group_send(
        self,
        group_id: str,
        msg_type: str,
        payload: dict,
        correlation_id: str = None,
        ttl: int = 3600,
    ) -> dict:
        """Send a CoWorker protocol message to an XMTP group."""
        envelope = build_message(
            msg_type=msg_type,
            payload=payload,
            sender_id=self._sender_id,
            correlation_id=correlation_id,
            ttl=ttl,
        )
        envelope["sender_wallet"] = self._load_wallet_address()
        return _bridge_post(self.data_dir, "/group-send", {
            "groupId": group_id,
            "content": json.dumps(envelope),
        })

    def group_add_member(self, group_id: str, wallet: str) -> dict:
        """Add a member to an existing XMTP group."""
        return _bridge_post(self.data_dir, "/group-add-member", {
            "groupId": group_id,
            "member": wallet,
        })

    def group_remove_member(self, group_id: str, wallet: str) -> dict:
        """Remove a member from an XMTP group."""
        return _bridge_post(self.data_dir, "/group-remove-member", {
            "groupId": group_id,
            "member": wallet,
        })

    def list_groups(self) -> List[dict]:
        """List all XMTP groups this agent is a member of."""
        result = _bridge_get(self.data_dir, "/groups")
        return result.get("groups", [])

    def ping(self, to_wallet: str) -> dict:
        """Send a ping and return the bridge response."""
        return self.send(to_wallet, "ping", {
            "message": f"ping from {self._sender_id}"
        }, correlation_id=f"ping_{int(time.time())}")

    def pong(self, to_wallet: str, correlation_id: str) -> dict:
        """Send a pong response."""
        return self.send(to_wallet, "pong", {
            "message": f"pong from {self._sender_id}",
            "received_correlation_id": correlation_id,
        }, correlation_id=f"pong_{int(time.time())}")
