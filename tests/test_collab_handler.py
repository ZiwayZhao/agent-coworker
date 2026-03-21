"""Unit tests for handlers/collab_handler.py."""
import sys, os, re
import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts")
sys.path.insert(0, SCRIPTS_DIR)

HANDLERS_DIR = os.path.join(SCRIPTS_DIR, "handlers")
sys.path.insert(0, HANDLERS_DIR)

from collab_handler import (generate_collab_id, build_plan_from_skills,
                              validate_plan, handle_collab_propose,
                              handle_collab_accept, handle_collab_reject,
                              handle_collab_counter)


# ── generate_collab_id ───────────────────────────────────

@pytest.mark.unit
def test_generate_collab_id_format():
    cid = generate_collab_id()
    assert cid.startswith("collab_")
    hex_part = cid[len("collab_"):]
    assert len(hex_part) == 12
    assert re.fullmatch(r"[0-9a-f]{12}", hex_part)


@pytest.mark.unit
def test_generate_collab_id_unique():
    assert generate_collab_id() != generate_collab_id()


# ── build_plan_from_skills ───────────────────────────────

@pytest.mark.unit
def test_build_plan_from_skills_order():
    """web_search before summarize before write_report before translate."""
    plan = build_plan_from_skills(
        "Goal", "a", ["translate", "web_search"], "b", ["summarize", "write_report"]
    )
    skills = [s["skill"] for s in plan]
    assert skills.index("web_search") < skills.index("summarize")
    assert skills.index("summarize") < skills.index("write_report")
    assert skills.index("write_report") < skills.index("translate")


@pytest.mark.unit
def test_build_plan_assigns_to_owner():
    plan = build_plan_from_skills("Goal", "a", ["web_search"], "b", ["summarize"])
    by_skill = {s["skill"]: s["agent"] for s in plan}
    assert by_skill["web_search"] == "a"
    assert by_skill["summarize"] == "b"


@pytest.mark.unit
def test_build_plan_initiator_priority():
    """If both have the skill, initiator (my_name) gets it."""
    plan = build_plan_from_skills("Goal", "a", ["web_search"], "b", ["web_search"])
    assert plan[0]["agent"] == "a"


@pytest.mark.unit
def test_build_plan_input_from_goal():
    plan = build_plan_from_skills("Goal", "a", ["web_search"], "b", [])
    assert plan[0]["input_from"] == "$goal"


@pytest.mark.unit
def test_build_plan_input_from_prev():
    """Step N+1 has input_from='step_N'."""
    plan = build_plan_from_skills("Goal", "a", ["web_search", "summarize"], "b", [])
    assert len(plan) >= 2
    assert plan[1]["input_from"] == "step_1"


@pytest.mark.unit
def test_build_plan_skills_as_dicts():
    plan = build_plan_from_skills(
        "Goal", "a", [{"name": "web_search"}], "b", [{"name": "summarize"}]
    )
    skills = {s["skill"] for s in plan}
    assert "web_search" in skills
    assert "summarize" in skills


# ── validate_plan ────────────────────────────────────────

@pytest.mark.unit
def test_validate_plan_valid():
    plan = [{"step": 1, "skill": "web_search", "agent": "me", "input_from": "$goal"}]
    ok, reason = validate_plan(plan, "me", ["web_search"])
    assert ok is True
    assert reason == "Plan is valid"


@pytest.mark.unit
def test_validate_plan_missing_skill():
    plan = [{"step": 1, "skill": "web_search", "agent": "me", "input_from": "$goal"}]
    ok, reason = validate_plan(plan, "me", ["summarize"])
    assert ok is False
    assert "web_search" in reason


@pytest.mark.unit
def test_validate_plan_no_steps_for_me():
    plan = [{"step": 1, "skill": "web_search", "agent": "other", "input_from": "$goal"}]
    ok, _ = validate_plan(plan, "me", [])
    assert ok is True


# ── handle_collab_propose ────────────────────────────────

@pytest.mark.unit
def test_handle_collab_propose_accept():
    msg = {
        "sender_id": "peer",
        "payload": {
            "collab_id": "collab_abc123",
            "goal": "Test",
            "plan": [
                {"step": 1, "skill": "web_search", "agent": "me", "input_from": "$goal"},
            ],
        },
    }
    ctx = {"agent_name": "me", "skills": ["web_search"]}
    resp = handle_collab_propose(msg, ctx)
    assert resp["type"] == "collab_accept"
    assert resp["payload"]["collab_id"] == "collab_abc123"


@pytest.mark.unit
def test_handle_collab_propose_reject():
    msg = {
        "sender_id": "peer",
        "payload": {
            "collab_id": "collab_abc123",
            "goal": "Test",
            "plan": [
                {"step": 1, "skill": "unknown", "agent": "me", "input_from": "$goal"},
            ],
        },
    }
    ctx = {"agent_name": "me", "skills": ["web_search"]}
    resp = handle_collab_propose(msg, ctx)
    assert resp["type"] == "collab_reject"
    assert "unknown" in resp["payload"]["reason"]


@pytest.mark.unit
def test_handle_collab_propose_accept_count():
    """Accepted message mentions correct step count."""
    msg = {
        "sender_id": "peer",
        "payload": {
            "collab_id": "collab_abc123",
            "goal": "Test",
            "plan": [
                {"step": 1, "skill": "web_search", "agent": "me", "input_from": "$goal"},
                {"step": 2, "skill": "summarize", "agent": "me", "input_from": "step_1"},
                {"step": 3, "skill": "write", "agent": "peer", "input_from": "step_2"},
            ],
        },
    }
    ctx = {"agent_name": "me", "skills": ["web_search", "summarize"]}
    resp = handle_collab_propose(msg, ctx)
    assert resp["type"] == "collab_accept"
    assert "2 steps" in resp["payload"]["message"]


# ── handle_collab_accept/reject/counter ──────────────────

@pytest.mark.unit
def test_handle_collab_accept_stores_in_ctx():
    ctx = {"collab_responses": {}}
    msg = {"payload": {"collab_id": "c1", "agreed_plan": [{"step": 1}], "message": "ok"}}
    handle_collab_accept(msg, ctx)
    assert "c1" in ctx["collab_responses"]
    assert ctx["collab_responses"]["c1"]["type"] == "accepted"
    assert ctx["collab_responses"]["c1"]["plan"] == [{"step": 1}]


@pytest.mark.unit
def test_handle_collab_reject_stores_in_ctx():
    ctx = {"collab_responses": {}}
    msg = {"payload": {"collab_id": "c1", "reason": "nope"}}
    handle_collab_reject(msg, ctx)
    assert ctx["collab_responses"]["c1"]["type"] == "rejected"
    assert ctx["collab_responses"]["c1"]["reason"] == "nope"


@pytest.mark.unit
def test_handle_collab_counter_stores_in_ctx():
    ctx = {"collab_responses": {}}
    msg = {"payload": {"collab_id": "c1", "counter_plan": [{"step": 1}], "message": "swap"}}
    handle_collab_counter(msg, ctx)
    assert ctx["collab_responses"]["c1"]["type"] == "counter"
    assert ctx["collab_responses"]["c1"]["plan"] == [{"step": 1}]
    assert ctx["collab_responses"]["c1"]["message"] == "swap"
