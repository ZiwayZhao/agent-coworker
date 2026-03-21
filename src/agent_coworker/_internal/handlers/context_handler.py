"""AgentFax Context Handlers — privacy-aware context exchange."""

import logging

logger = logging.getLogger("agentfax.handlers.context")


def register_context_handlers(router, context_manager, trust_manager):
    """Register context-related handlers with the router."""

    @router.handler("context_sync")
    def handle_context_sync(msg, ctx):
        payload = msg.get("payload", {})
        sender = msg.get("sender_id", "unknown")
        items = payload.get("items", [])
        if not items:
            return None
        count = context_manager.store_peer_context(
            peer_id=sender, context_items=items,
            correlation_id=msg.get("correlation_id"))
        return {
            "type": "ack",
            "payload": {"correlation_id": msg.get("correlation_id"),
                        "context_items_stored": count},
        }

    @router.handler("context_query")
    def handle_context_query(msg, ctx):
        payload = msg.get("payload", {})
        sender = msg.get("sender_id", "unknown")
        peer_tier = trust_manager.get_trust_tier(sender)
        response_payload = context_manager.build_context_response_payload(
            query=payload, peer_trust_tier=peer_tier)
        return {"type": "context_response", "payload": response_payload}

    @router.handler("context_response")
    def handle_context_response(msg, ctx):
        payload = msg.get("payload", {})
        sender = msg.get("sender_id", "unknown")
        items = payload.get("items", [])
        if items:
            context_manager.store_peer_context(
                peer_id=sender, context_items=items,
                correlation_id=msg.get("correlation_id"))
        return None
