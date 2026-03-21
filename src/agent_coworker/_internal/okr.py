"""AgentFax OKR Engine — Goal -> OKR -> Task decomposition and tracking."""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def new_okr_id():
    return f"okr_{uuid.uuid4().hex[:12]}"


def new_kr_id(okr_index, kr_index):
    return f"kr_{okr_index}_{kr_index}"


def build_okr(goal: str, my_name: str, my_skills: list,
              peer_name: str, peer_skills: list) -> dict:
    my_skill_set = {s["name"] if isinstance(s, dict) else s for s in my_skills}
    peer_skill_set = {s["name"] if isinstance(s, dict) else s for s in peer_skills}

    okr_id = new_okr_id()

    research_skills = {"web_search", "search", "research", "crawl", "fetch"}
    analysis_skills = {"summarize", "analyze", "extract", "classify", "evaluate"}
    creation_skills = {"write_report", "write", "compose", "draft", "generate", "create"}
    delivery_skills = {"translate", "format", "publish", "export", "review", "proofread"}

    objective = {
        "okr_id": okr_id, "goal": goal, "status": "proposed",
        "created_at": datetime.now(timezone.utc).isoformat(), "key_results": [],
    }

    kr_index = 0

    for label, skill_set in [
        ("Gather comprehensive information on", research_skills),
        ("Analyze and synthesize findings into key insights", analysis_skills),
        ("Produce a structured deliverable on", creation_skills),
        ("Deliver polished output in required formats", delivery_skills),
    ]:
        matched = my_skill_set & skill_set | peer_skill_set & skill_set
        if matched:
            kr_index += 1
            tasks = []
            for skill in sorted(matched):
                agent = my_name if skill in my_skill_set else peer_name
                tasks.append(_make_task(kr_index, len(tasks) + 1, skill, agent, goal))
            desc = f"{label}: {_short_goal(goal)}" if "on" in label else label
            objective["key_results"].append({
                "kr_id": f"KR{kr_index}", "description": desc,
                "metric": f"{max(3, len(tasks) * 2)}+ items",
                "progress": 0, "status": "pending", "tasks": tasks,
            })

    if not objective["key_results"]:
        all_skills = sorted(my_skill_set | peer_skill_set)
        tasks = []
        for i, skill in enumerate(all_skills):
            agent = my_name if skill in my_skill_set else peer_name
            tasks.append(_make_task(1, i + 1, skill, agent, goal))
        objective["key_results"].append({
            "kr_id": "KR1", "description": f"Complete all tasks for: {_short_goal(goal)}",
            "metric": f"{len(tasks)} tasks completed",
            "progress": 0, "status": "pending", "tasks": tasks,
        })

    return objective


def _make_task(kr_index, task_index, skill, agent, description):
    return {
        "task_id": f"task_{kr_index}_{task_index}_{uuid.uuid4().hex[:6]}",
        "skill": skill, "agent": agent, "description": description,
        "status": "pending", "duration_ms": None, "result_preview": None,
    }


def _short_goal(goal, max_len=60):
    return goal if len(goal) <= max_len else goal[:max_len - 3] + "..."


def update_task_status(okr: dict, task_id: str, status: str,
                       duration_ms: float = None, result_preview: str = None):
    for kr in okr.get("key_results", []):
        for task in kr.get("tasks", []):
            if task["task_id"] == task_id:
                task["status"] = status
                if duration_ms is not None:
                    task["duration_ms"] = duration_ms
                if result_preview:
                    task["result_preview"] = result_preview
                _recalc_kr_progress(kr)
                break
    _recalc_okr_status(okr)
    return okr


def _recalc_kr_progress(kr):
    tasks = kr.get("tasks", [])
    if not tasks:
        return
    completed = sum(1 for t in tasks if t["status"] == "completed")
    running = sum(1 for t in tasks if t["status"] == "running")
    failed = sum(1 for t in tasks if t["status"] == "failed")
    kr["progress"] = round((completed + running * 0.5) / len(tasks) * 100)
    if all(t["status"] == "completed" for t in tasks):
        kr["status"] = "completed"
    elif failed > 0:
        kr["status"] = "at_risk"
    elif running > 0 or completed > 0:
        kr["status"] = "in_progress"
    else:
        kr["status"] = "pending"


def _recalc_okr_status(okr):
    krs = okr.get("key_results", [])
    if not krs:
        return
    if all(kr["status"] == "completed" for kr in krs):
        okr["status"] = "completed"
    elif any(kr["status"] == "at_risk" for kr in krs):
        okr["status"] = "at_risk"
    elif any(kr["status"] == "in_progress" for kr in krs):
        okr["status"] = "in_progress"
    else:
        okr["status"] = "proposed"


def get_overall_progress(okr: dict) -> int:
    krs = okr.get("key_results", [])
    if not krs:
        return 0
    return round(sum(kr.get("progress", 0) for kr in krs) / len(krs))


def get_flat_tasks(okr: dict) -> list:
    tasks = []
    for kr in okr.get("key_results", []):
        for task in kr.get("tasks", []):
            tasks.append({**task, "kr_id": kr["kr_id"], "kr_description": kr["description"]})
    return tasks


def build_okr_propose(okr: dict) -> dict:
    return {
        "okr_id": okr["okr_id"], "goal": okr["goal"],
        "key_results": [{
            "kr_id": kr["kr_id"], "description": kr["description"], "metric": kr["metric"],
            "tasks": [{
                "task_id": t["task_id"], "skill": t["skill"],
                "agent": t["agent"], "description": t["description"],
            } for t in kr["tasks"]],
        } for kr in okr["key_results"]],
    }


def handle_okr_propose(msg: dict, my_name: str, my_skills: list) -> dict:
    payload = msg.get("payload", {})
    okr_id = payload.get("okr_id", "unknown")
    krs = payload.get("key_results", [])
    my_skill_set = {s["name"] if isinstance(s, dict) else s for s in my_skills}

    issues = []
    my_task_count = 0
    for kr in krs:
        for task in kr.get("tasks", []):
            if task["agent"] == my_name:
                my_task_count += 1
                if task["skill"] not in my_skill_set:
                    issues.append(f"I don't have skill '{task['skill']}' (KR: {kr['kr_id']})")

    if issues:
        return {"type": "okr_reject", "payload": {"okr_id": okr_id, "reason": "; ".join(issues)}}

    return {
        "type": "okr_accept",
        "payload": {
            "okr_id": okr_id, "agreed_krs": [kr["kr_id"] for kr in krs],
            "my_task_count": my_task_count,
            "message": f"OKR accepted. I will handle {my_task_count} tasks across {len(krs)} key results.",
        }
    }
