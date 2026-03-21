"""AgentFax Session Handler — collaboration session lifecycle messages."""

import logging

logger = logging.getLogger("agentfax.handlers.session")


def register_session_handlers(router, session_manager, executor):
    """Register session-related handlers with the router."""

    def _make_error(error_code, error_message, retryable=False, scope="session"):
        return {"type": "task_error", "payload": {
            "error_code": error_code, "error_message": error_message,
            "retryable": retryable, "scope": scope,
        }}

    @router.handler("session_propose")
    def handle_session_propose(msg, ctx):
        sender = msg.get("sender_id", "unknown")
        payload = msg.get("payload", {})
        proposed_skills = payload.get("proposed_skills", [])
        proposed_trust_tier = payload.get("proposed_trust_tier", 1)
        proposed_max_context_privacy = payload.get("proposed_max_context_privacy", "L1_PUBLIC")
        proposed_max_calls = payload.get("proposed_max_calls", 10)
        ttl_seconds = payload.get("ttl_seconds", 3600)
        remote_session_id = payload.get("session_id", "")

        if ctx.trust_manager:
            from ..security import TrustTier
            peer_tier = ctx.trust_manager.get_trust_tier(sender)
            if peer_tier < TrustTier.KNOWN:
                return _make_error("TRUST_TIER_TOO_LOW",
                    f"Session propose requires KNOWN+ trust", scope="authorization")

        missing = [s for s in proposed_skills if not executor.has_skill(s)]
        if missing:
            return _make_error("SKILL_NOT_FOUND",
                f"Unknown skills: {missing}. Available: {executor.skill_names}", scope="routing")

        session_id = session_manager.create_session(
            peer_id=sender, role="responder", proposed_skills=proposed_skills,
            proposed_trust_tier=proposed_trust_tier,
            proposed_max_context_privacy=proposed_max_context_privacy,
            proposed_max_calls=proposed_max_calls, ttl_seconds=ttl_seconds,
            initiator_id=sender)

        session_manager.accept_session(
            session_id, agreed_skills=proposed_skills, agreed_trust_tier=proposed_trust_tier,
            agreed_max_context_privacy=proposed_max_context_privacy,
            agreed_max_calls=proposed_max_calls)

        return {"type": "session_accept", "payload": {
            "session_id": session_id, "remote_session_id": remote_session_id,
            "agreed_skills": proposed_skills, "agreed_skill_version": "1.0.0",
            "agreed_trust_tier": proposed_trust_tier,
            "agreed_max_context_privacy": proposed_max_context_privacy,
            "agreed_max_calls": proposed_max_calls,
            "agreed_pricing_snapshot": {"model": "free", "amount": 0},
            "expires_at": session_manager.get_session(session_id).get("expires_at", ""),
        }}

    @router.handler("session_accept")
    def handle_session_accept(msg, ctx):
        sender = msg.get("sender_id", "unknown")
        payload = msg.get("payload", {})
        local_session_id = payload.get("remote_session_id", "")
        if not local_session_id:
            return None
        session = session_manager.get_session(local_session_id)
        if not session:
            return None
        if session["peer_id"] != sender:
            return _make_error("SESSION_PEER_MISMATCH",
                f"Session {local_session_id} is with {session['peer_id']}, not {sender}",
                scope="authorization")
        session_manager.accept_session(
            local_session_id, agreed_skills=payload.get("agreed_skills"),
            agreed_skill_version=payload.get("agreed_skill_version", "1.0.0"),
            agreed_schema_hash=payload.get("agreed_schema_hash", ""),
            agreed_trust_tier=payload.get("agreed_trust_tier"),
            agreed_max_context_privacy=payload.get("agreed_max_context_privacy"),
            agreed_max_calls=payload.get("agreed_max_calls"),
            agreed_pricing_snapshot=payload.get("agreed_pricing_snapshot"))
        return None

    @router.handler("session_reject")
    def handle_session_reject(msg, ctx):
        sender = msg.get("sender_id", "unknown")
        payload = msg.get("payload", {})
        local_session_id = payload.get("remote_session_id", "")
        reason = payload.get("reason", "")
        if local_session_id:
            session = session_manager.get_session(local_session_id)
            if session and session["peer_id"] != sender:
                return None
            session_manager.reject_session(local_session_id, reason)
        return None

    @router.handler("session_close")
    def handle_session_close(msg, ctx):
        sender = msg.get("sender_id", "unknown")
        payload = msg.get("payload", {})
        session_id = payload.get("session_id", "")
        reason = payload.get("reason", "")

        if not session_id:
            return _make_error("MISSING_SESSION_ID", "session_close requires session_id")
        session = session_manager.get_session(session_id)
        if not session:
            return _make_error("SESSION_NOT_FOUND", f"Session {session_id} does not exist")
        if session["peer_id"] != sender:
            return _make_error("SESSION_PEER_MISMATCH",
                f"Session {session_id} is with {session['peer_id']}, not {sender}",
                scope="authorization")

        ok = session_manager.close_session(session_id, reason)
        if not ok:
            ok = session_manager.force_close_session(session_id, reason)
        if not ok:
            return _make_error("SESSION_CLOSE_FAILED",
                f"Cannot close session {session_id} (current state: {session['state']})")

        session_manager.complete_session(session_id)

        final = session_manager.get_session(session_id)
        return {"type": "session_close", "payload": {
            "session_id": session_id,
            "status": final["state"] if final else "closed",
            "reason": reason,
        }}
