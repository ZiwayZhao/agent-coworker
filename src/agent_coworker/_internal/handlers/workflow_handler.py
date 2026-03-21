"""AgentFax Workflow Handler — handles workflow_request messages."""

import logging
import time

logger = logging.getLogger("agentfax.handlers.workflow")


def register_workflow_handlers(router, workflow_manager, task_manager, executor):
    """Register workflow-related handlers with the router."""

    @router.handler("workflow_request")
    def handle_workflow_request(msg, ctx):
        payload = msg.get("payload", {})
        workflow_id = payload.get("workflow_id")
        step_info = payload.get("step", {})

        step_id = step_info.get("step_id")
        skill = step_info.get("skill")
        input_data = step_info.get("input", {})
        step_context = step_info.get("context", [])
        timeout = step_info.get("timeout_seconds", 300)

        sender = msg.get("sender_id", "unknown")
        sender_wallet = msg.get("_xmtp_sender_wallet")
        task_id = f"wf_{workflow_id}_{step_id}_{int(time.time())}"

        if not executor.has_skill(skill):
            return {"type": "task_error", "payload": {
                "task_id": task_id, "workflow_id": workflow_id,
                "step_id": step_id, "skill": skill,
                "status": "failed", "error": f"Unknown skill: {skill}",
            }}

        task_manager.receive_task(
            task_id=task_id, skill=skill, input_data=input_data,
            peer_wallet=sender_wallet or "", peer_name=sender,
            correlation_id=msg.get("correlation_id"), timeout_seconds=timeout)
        task_manager.accept_task(task_id)

        exec_input = input_data
        if step_context and isinstance(input_data, dict):
            exec_input = {**input_data, "_context": step_context}

        task_manager.start_task(task_id)
        exec_result = executor.execute(skill, exec_input)

        if exec_result.get("success"):
            duration = exec_result.get("duration_ms", 0)
            task_manager.complete_task(task_id, result=exec_result.get("result"))
            return {"type": "task_response", "payload": {
                "task_id": task_id, "workflow_id": workflow_id,
                "step_id": step_id, "skill": skill,
                "status": "completed", "output": exec_result.get("result"),
                "duration_ms": duration,
            }}
        else:
            error_msg = exec_result.get("error", "unknown error")
            task_manager.fail_task(task_id, error_msg)
            return {"type": "task_error", "payload": {
                "task_id": task_id, "workflow_id": workflow_id,
                "step_id": step_id, "skill": skill,
                "status": "failed", "error": error_msg,
            }}
