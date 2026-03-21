"""AgentFax Message Router — dispatches incoming messages to handlers."""

import json
import logging
import time
import traceback
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Any

logger = logging.getLogger("agentfax.router")


class RouterContext:
    """Context passed to message handlers."""

    def __init__(self, client, inbox_store=None, outbox_store=None, peer_manager=None,
                 trust_manager=None, reputation_manager=None,
                 context_manager=None, workflow_manager=None,
                 session_manager=None, metering_manager=None,
                 slack_notifier=None):
        self.client = client
        self.inbox_store = inbox_store
        self.outbox_store = outbox_store
        self.peer_manager = peer_manager
        self.trust_manager = trust_manager
        self.reputation_manager = reputation_manager
        self.context_manager = context_manager
        self.workflow_manager = workflow_manager
        self.session_manager = session_manager
        self.metering_manager = metering_manager
        self.slack_notifier = slack_notifier

    def reply(self, original_msg: dict, msg_type: str, payload: dict) -> Optional[dict]:
        sender_wallet = original_msg.get("_xmtp_sender_wallet")
        if not sender_wallet:
            return None
        corr_id = original_msg.get("correlation_id")
        try:
            result = self.client.send(
                to_wallet=sender_wallet, msg_type=msg_type,
                payload=payload, correlation_id=corr_id,
            )
            if self.outbox_store:
                self.outbox_store.record(
                    recipient_wallet=sender_wallet, msg_type=msg_type,
                    payload=payload, bridge_response=result, correlation_id=corr_id,
                )
            return result
        except Exception as e:
            logger.error(f"Reply send failed [{msg_type}]: {e}")
            if self.outbox_store:
                self.outbox_store.record_pending(
                    recipient_wallet=sender_wallet, msg_type=msg_type,
                    payload=payload, correlation_id=corr_id,
                )
            return None


class MessageRouter:
    """Routes incoming AgentFax messages to registered handlers."""

    def __init__(self):
        self._handlers: Dict[str, Callable] = {}
        self._fallback: Optional[Callable] = None
        self._middleware: list = []
        self._stats = {"dispatched": 0, "handled": 0, "unhandled": 0, "errors": 0}

    def handler(self, msg_type: str):
        def decorator(func):
            self._handlers[msg_type] = func
            return func
        return decorator

    def register(self, msg_type: str, func: Callable):
        self._handlers[msg_type] = func

    def set_fallback(self, func: Callable):
        self._fallback = func

    def add_middleware(self, func: Callable):
        self._middleware.append(func)

    def dispatch(self, msg: dict, ctx: RouterContext) -> Optional[dict]:
        self._stats["dispatched"] += 1
        msg_type = msg.get("type", "unknown")
        sender = msg.get("sender_id", "?")

        for mw in self._middleware:
            try:
                if not mw(msg, ctx):
                    return None
            except Exception:
                pass

        handler_func = self._handlers.get(msg_type)
        if not handler_func and self._fallback:
            handler_func = self._fallback

        if not handler_func:
            self._stats["unhandled"] += 1
            return None

        try:
            result = handler_func(msg, ctx)
            self._stats["handled"] += 1
            if isinstance(result, dict) and "type" in result and "payload" in result:
                ctx.reply(msg, result["type"], result["payload"])
            return result
        except Exception as e:
            self._stats["errors"] += 1
            try:
                ctx.reply(msg, "error", {
                    "error": str(e), "original_type": msg_type,
                    "correlation_id": msg.get("correlation_id"),
                })
            except Exception:
                pass
            return None

    def process_inbox(self, client, ctx: RouterContext, clear: bool = True) -> int:
        messages = client.receive(clear=clear)
        count = 0
        for msg in messages:
            if ctx.inbox_store:
                ctx.inbox_store.save(msg)
            if ctx.peer_manager and msg.get("sender_id"):
                ctx.peer_manager.update_seen(
                    sender_id=msg.get("sender_id"),
                    wallet=msg.get("_xmtp_sender_wallet"),
                )
            self.dispatch(msg, ctx)
            count += 1
        return count

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    @property
    def registered_types(self) -> list:
        return list(self._handlers.keys())
