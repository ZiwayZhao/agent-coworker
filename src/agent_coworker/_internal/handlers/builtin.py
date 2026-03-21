"""AgentFax Built-in Handlers — auto-registered by the daemon."""

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("agentfax.handlers")


def register_builtin_handlers(router, data_dir: str):
    """Register all built-in handlers with the router."""
    data_dir = str(Path(data_dir).expanduser())

    @router.handler("ping")
    def handle_ping(msg, ctx):
        sender = msg.get("sender_id", "unknown")
        if ctx.peer_manager:
            ctx.peer_manager.update_seen(sender_id=sender, wallet=msg.get("_xmtp_sender_wallet"))
        return {
            "type": "pong",
            "payload": {
                "message": f"pong from {ctx.client._sender_id}",
                "received_ping_corr": msg.get("correlation_id"),
                "timestamp": time.time(),
            },
        }

    @router.handler("pong")
    def handle_pong(msg, ctx):
        sender = msg.get("sender_id", "unknown")
        latency = None
        payload = msg.get("payload", {})
        ping_corr = payload.get("received_ping_corr", "")
        if ping_corr.startswith("ping_"):
            try:
                ping_ts = float(ping_corr.split("_")[1])
                latency = (time.time() - ping_ts) * 1000
            except (ValueError, IndexError):
                pass
        if ctx.peer_manager:
            ctx.peer_manager.update_seen(sender_id=sender, wallet=msg.get("_xmtp_sender_wallet"),
                                          latency_ms=latency)
        if ctx.reputation_manager:
            ctx.reputation_manager.record_interaction(sender, "ping_response", True, latency_ms=latency)
        return None

    @router.handler("discover")
    def handle_discover(msg, ctx):
        sender = msg.get("sender_id", "unknown")
        caps_file = os.path.join(data_dir, "capabilities.json")
        if os.path.exists(caps_file):
            with open(caps_file) as f:
                capabilities = json.load(f)
        else:
            capabilities = {
                "agent_id": ctx.client._sender_id, "name": ctx.client._sender_id,
                "skills": [], "transport": ["xmtp"], "version": "1.0",
            }
        return {"type": "capabilities", "payload": capabilities}

    @router.handler("capabilities")
    def handle_capabilities(msg, ctx):
        sender = msg.get("sender_id", "unknown")
        payload = msg.get("payload", {})
        if ctx.peer_manager:
            ctx.peer_manager.update_capabilities(
                sender_id=sender, wallet=msg.get("_xmtp_sender_wallet"), capabilities=payload)
        return None

    @router.handler("ack")
    def handle_ack(msg, ctx):
        payload = msg.get("payload", {})
        acked_corr = payload.get("correlation_id")
        if acked_corr and ctx.outbox_store:
            ctx.outbox_store.mark_acked(acked_corr)
        return None

    @router.handler("error")
    def handle_error(msg, ctx):
        payload = msg.get("payload", {})
        sender = msg.get("sender_id", "unknown")
        logger.error(f"Error from {sender}: {payload.get('error', 'unknown')}")
        return None
