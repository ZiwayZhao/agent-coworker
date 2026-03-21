"""AgentFax Task Handler — handles task_request/response/ack/cancel messages."""

import json
import logging
import time

logger = logging.getLogger("agentfax.handlers.task")


def register_task_handlers(router, task_manager, executor):
    """Register task-related handlers with the router."""

    def _make_error(task_id, skill, error_code, error_message, retryable=False, scope="execution"):
        return {
            "type": "task_error",
            "payload": {
                "task_id": task_id, "skill": skill, "error_code": error_code,
                "error_message": error_message, "retryable": retryable, "scope": scope,
            },
        }

    _dedup_cache = {}
    _dedup_timestamps = {}
    _DEDUP_TTL_SECONDS = 300
    _DEDUP_MAX_ENTRIES = 1000

    def _dedup_key(sender, correlation_id):
        return f"{sender}:{correlation_id}"

    def _dedup_cleanup():
        now = time.time()
        expired = [k for k, ts in _dedup_timestamps.items() if now - ts > _DEDUP_TTL_SECONDS]
        for k in expired:
            _dedup_cache.pop(k, None)
            _dedup_timestamps.pop(k, None)
        while len(_dedup_cache) > _DEDUP_MAX_ENTRIES:
            oldest_key = min(_dedup_timestamps, key=_dedup_timestamps.get)
            _dedup_cache.pop(oldest_key, None)
            _dedup_timestamps.pop(oldest_key, None)

    @router.handler("task_request")
    def handle_task_request(msg, ctx):
        payload = msg.get("payload", {})
        skill = payload.get("skill")
        input_data = payload.get("input")
        task_id = payload.get("task_id") or msg.get("correlation_id", f"task_{int(time.time())}")
        timeout = payload.get("timeout", 300)
        sender = msg.get("sender_id", "unknown")
        sender_wallet = msg.get("_xmtp_sender_wallet")
        correlation_id = msg.get("correlation_id", "")
        session_id = payload.get("session_id")

        if ctx.trust_manager:
            from ..security import TrustTier
            peer_tier = ctx.trust_manager.get_trust_tier(sender)
            skill_def = executor.get_skill(skill) if executor.has_skill(skill) else None
            min_tier = skill_def.min_trust_tier if skill_def else 1
            if peer_tier < min_tier:
                return _make_error(task_id, skill, "TRUST_TIER_TOO_LOW",
                    f"Requires trust tier {min_tier}, you have {peer_tier}",
                    retryable=False, scope="authorization")

        _dedup_cleanup()
        dedup_k = _dedup_key(sender, correlation_id) if correlation_id else None
        if dedup_k and dedup_k in _dedup_cache:
            return _dedup_cache[dedup_k]

        if not executor.has_skill(skill):
            return _make_error(task_id, skill, "SKILL_NOT_FOUND",
                f"Unknown skill: {skill}. Available: {executor.skill_names}",
                retryable=False, scope="routing")

        if session_id and ctx.session_manager:
            ok, err_code, err_msg = ctx.session_manager.validate_task_request(
                session_id, skill, sender)
            if not ok:
                return _make_error(task_id, skill, err_code, err_msg, retryable=False, scope="session")
            if not ctx.session_manager.increment_call_count(session_id):
                return _make_error(task_id, skill, "CALL_LIMIT_EXCEEDED",
                    f"Session {session_id} call limit reached", retryable=False, scope="session")

        is_new = task_manager.receive_task(
            task_id=task_id, skill=skill, input_data=input_data or {},
            peer_wallet=sender_wallet or "", peer_name=sender,
            correlation_id=correlation_id, timeout_seconds=timeout)

        if not is_new:
            existing = task_manager.get_task(task_id)
            if existing and existing.get("state") == "completed":
                return {"type": "task_response", "payload": {
                    "task_id": task_id, "skill": skill, "status": "completed",
                    "output": existing.get("output_data"),
                    "duration_ms": existing.get("duration_ms", 0),
                }}
            return {"type": "task_ack", "payload": {
                "task_id": task_id, "skill": skill, "status": "in_progress"}}

        if session_id:
            task_manager.set_session_id(task_id, session_id)

        task_manager.accept_task(task_id)
        ctx.reply(msg, "task_ack", {
            "task_id": task_id, "skill": skill, "status": "accepted"})

        task_manager.start_task(task_id)
        try:
            exec_result = executor.execute(skill, input_data)
        except Exception as e:
            exec_result = {"success": False, "error": str(e)}

        if exec_result.get("success"):
            duration = exec_result.get("duration_ms", 0)
            task_manager.complete_task(task_id, result=exec_result.get("result"))
            if session_id and ctx.session_manager:
                ctx.session_manager.task_completed(session_id)
            response = {"type": "task_response", "payload": {
                "task_id": task_id, "skill": skill, "status": "completed",
                "output": exec_result.get("result"), "duration_ms": duration,
                "session_id": session_id,
            }}
        else:
            error_msg = exec_result.get("error", "unknown error")
            task_manager.fail_task(task_id, error_msg)
            if session_id and ctx.session_manager:
                ctx.session_manager.task_failed(session_id)
            response = _make_error(task_id, skill, "EXECUTION_FAILED", error_msg,
                                    retryable=False, scope="execution")

        if dedup_k:
            _dedup_cache[dedup_k] = response
            _dedup_timestamps[dedup_k] = time.time()
        return response

    @router.handler("task_ack")
    def handle_task_ack(msg, ctx):
        task = task_manager.get_by_correlation(msg.get("correlation_id", ""))
        if task:
            task_manager.accept_task(task["task_id"])
        return None

    @router.handler("task_reject")
    def handle_task_reject(msg, ctx):
        payload = msg.get("payload", {})
        task = task_manager.get_by_correlation(msg.get("correlation_id", ""))
        if task:
            task_manager.reject_task(task["task_id"], payload.get("reason", "no reason"))
        return None

    @router.handler("task_response")
    def handle_task_response(msg, ctx):
        payload = msg.get("payload", {})
        task = task_manager.get_by_correlation(msg.get("correlation_id", ""))
        if task:
            task_manager.complete_task(task["task_id"], result=payload.get("output"))
        return None

    @router.handler("task_error")
    def handle_task_error(msg, ctx):
        payload = msg.get("payload", {})
        error = payload.get("error_message") or payload.get("error", "unknown")
        task = task_manager.get_by_correlation(msg.get("correlation_id", ""))
        if task:
            task_manager.fail_task(task["task_id"], error)
        return None

    @router.handler("task_progress")
    def handle_task_progress(msg, ctx):
        payload = msg.get("payload", {})
        task = task_manager.get_by_correlation(msg.get("correlation_id", ""))
        if task:
            task_manager.update_progress(task["task_id"], payload.get("percent", 0),
                                          payload.get("status_text", ""))
        return None

    @router.handler("task_cancel")
    def handle_task_cancel(msg, ctx):
        payload = msg.get("payload", {})
        task_id = payload.get("task_id")
        task = task_manager.get_task(task_id)
        if task:
            task_manager.cancel_task(task_id)
        return None
