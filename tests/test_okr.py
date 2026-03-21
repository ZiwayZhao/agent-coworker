"""Unit tests for okr.py — OKR engine."""
import sys, os
import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "skills", "agent-fax", "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from okr import (build_okr, handle_okr_propose, update_task_status,
                  get_flat_tasks, get_overall_progress, build_okr_propose)


# ── build_okr ────────────────────────────────────────────

@pytest.mark.unit
def test_build_okr_basic():
    """Researcher+writer skills produce 4 KRs (research, analysis, creation, delivery)."""
    okr = build_okr(
        "Write a report on AI agents",
        "alice", ["web_search", "summarize", "write_report", "translate"],
        "bob", ["research", "analyze", "draft", "review"],
    )
    assert len(okr["key_results"]) == 4
    kr_ids = [kr["kr_id"] for kr in okr["key_results"]]
    assert kr_ids == ["KR1", "KR2", "KR3", "KR4"]


@pytest.mark.unit
def test_build_okr_has_required_fields():
    okr = build_okr("Test goal", "a", ["web_search"], "b", ["summarize"])
    for field in ("okr_id", "goal", "status", "created_at", "key_results"):
        assert field in okr, f"Missing field: {field}"
    assert okr["goal"] == "Test goal"
    assert okr["status"] == "proposed"


@pytest.mark.unit
def test_build_okr_research_only():
    """Only web_search skill -> only 1 KR for research."""
    okr = build_okr("Find info", "a", ["web_search"], "b", [])
    assert len(okr["key_results"]) == 1
    assert okr["key_results"][0]["kr_id"] == "KR1"
    assert "information" in okr["key_results"][0]["description"].lower() or \
           "gather" in okr["key_results"][0]["description"].lower()


@pytest.mark.unit
def test_build_okr_creation_only():
    """Only write_report -> only 1 KR for creation."""
    okr = build_okr("Make a doc", "a", ["write_report"], "b", [])
    assert len(okr["key_results"]) == 1
    assert okr["key_results"][0]["kr_id"] == "KR1"


@pytest.mark.unit
def test_build_okr_no_skills_match_categories():
    """Skills like 'foo','bar' -> generic KR1 with all skills."""
    okr = build_okr("Do something", "a", ["foo"], "b", ["bar"])
    assert len(okr["key_results"]) == 1
    kr = okr["key_results"][0]
    assert kr["kr_id"] == "KR1"
    skills_in_tasks = {t["skill"] for t in kr["tasks"]}
    assert skills_in_tasks == {"foo", "bar"}


@pytest.mark.unit
def test_build_okr_skills_as_dicts():
    """Skills as [{'name': 'web_search', ...}] work the same as strings."""
    okr_str = build_okr("Goal", "a", ["web_search", "summarize"], "b", [])
    okr_dict = build_okr("Goal", "a", [{"name": "web_search"}, {"name": "summarize"}], "b", [])
    assert len(okr_str["key_results"]) == len(okr_dict["key_results"])
    for kr_s, kr_d in zip(okr_str["key_results"], okr_dict["key_results"]):
        assert len(kr_s["tasks"]) == len(kr_d["tasks"])
        for ts, td in zip(kr_s["tasks"], kr_d["tasks"]):
            assert ts["skill"] == td["skill"]
            assert ts["agent"] == td["agent"]


@pytest.mark.unit
def test_build_okr_peer_skills_assigned_to_peer():
    okr = build_okr("Goal", "alice", [], "bob", ["web_search", "summarize"])
    for kr in okr["key_results"]:
        for task in kr["tasks"]:
            assert task["agent"] == "bob"


@pytest.mark.unit
def test_build_okr_my_skills_assigned_to_me():
    okr = build_okr("Goal", "alice", ["web_search", "summarize"], "bob", [])
    for kr in okr["key_results"]:
        for task in kr["tasks"]:
            assert task["agent"] == "alice"


@pytest.mark.unit
def test_build_okr_tasks_have_pending_status():
    okr = build_okr("Goal", "a", ["web_search"], "b", ["summarize"])
    for kr in okr["key_results"]:
        for task in kr["tasks"]:
            assert task["status"] == "pending"
            assert task["duration_ms"] is None
            assert task["result_preview"] is None


# ── handle_okr_propose ───────────────────────────────────

@pytest.mark.unit
def test_handle_okr_propose_accept():
    """Valid proposal where I have all my assigned skills -> okr_accept."""
    msg = {
        "payload": {
            "okr_id": "okr_abc",
            "key_results": [{
                "kr_id": "KR1",
                "tasks": [
                    {"task_id": "t1", "skill": "web_search", "agent": "me", "description": "search"},
                    {"task_id": "t2", "skill": "summarize", "agent": "peer", "description": "sum"},
                ]
            }]
        }
    }
    resp = handle_okr_propose(msg, "me", ["web_search"])
    assert resp["type"] == "okr_accept"
    assert resp["payload"]["okr_id"] == "okr_abc"
    assert resp["payload"]["my_task_count"] == 1


@pytest.mark.unit
def test_handle_okr_propose_reject_missing_skill():
    """Task assigned to me with unknown skill -> okr_reject."""
    msg = {
        "payload": {
            "okr_id": "okr_abc",
            "key_results": [{
                "kr_id": "KR1",
                "tasks": [
                    {"task_id": "t1", "skill": "unknown_skill", "agent": "me", "description": "x"},
                ]
            }]
        }
    }
    resp = handle_okr_propose(msg, "me", ["web_search"])
    assert resp["type"] == "okr_reject"
    assert "unknown_skill" in resp["payload"]["reason"]


@pytest.mark.unit
def test_handle_okr_propose_no_tasks_for_me():
    """All tasks assigned to peer -> okr_accept with 0 my_task_count."""
    msg = {
        "payload": {
            "okr_id": "okr_abc",
            "key_results": [{
                "kr_id": "KR1",
                "tasks": [
                    {"task_id": "t1", "skill": "web_search", "agent": "peer", "description": "x"},
                ]
            }]
        }
    }
    resp = handle_okr_propose(msg, "me", [])
    assert resp["type"] == "okr_accept"
    assert resp["payload"]["my_task_count"] == 0


# ── update_task_status ───────────────────────────────────

def _make_okr_with_tasks(num_tasks=2):
    """Helper: build a minimal OKR with N tasks in one KR."""
    tasks = []
    for i in range(num_tasks):
        tasks.append({
            "task_id": f"t{i+1}",
            "skill": "s",
            "agent": "a",
            "description": "d",
            "status": "pending",
            "duration_ms": None,
            "result_preview": None,
        })
    return {
        "okr_id": "okr_test",
        "goal": "Test",
        "status": "proposed",
        "created_at": "2025-01-01T00:00:00+00:00",
        "key_results": [{
            "kr_id": "KR1",
            "description": "Test KR",
            "metric": "test",
            "progress": 0,
            "status": "pending",
            "tasks": tasks,
        }],
    }


@pytest.mark.unit
def test_update_task_status_completed():
    okr = _make_okr_with_tasks(2)
    update_task_status(okr, "t1", "completed")
    kr = okr["key_results"][0]
    # 1 completed out of 2 = 50%
    assert kr["progress"] == 50
    assert kr["status"] == "in_progress"


@pytest.mark.unit
def test_update_task_status_running_partial_progress():
    """Running task counts as 50% weight."""
    okr = _make_okr_with_tasks(2)
    update_task_status(okr, "t1", "running")
    kr = okr["key_results"][0]
    # (0 + 1*0.5) / 2 * 100 = 25
    assert kr["progress"] == 25
    assert kr["status"] == "in_progress"


@pytest.mark.unit
def test_update_task_status_failed_sets_at_risk():
    okr = _make_okr_with_tasks(2)
    update_task_status(okr, "t1", "failed")
    kr = okr["key_results"][0]
    assert kr["status"] == "at_risk"
    assert okr["status"] == "at_risk"


@pytest.mark.unit
def test_update_task_status_all_completed_propagates():
    """All tasks completed -> KR and OKR both completed."""
    okr = _make_okr_with_tasks(2)
    update_task_status(okr, "t1", "completed")
    update_task_status(okr, "t2", "completed")
    kr = okr["key_results"][0]
    assert kr["progress"] == 100
    assert kr["status"] == "completed"
    assert okr["status"] == "completed"


@pytest.mark.unit
def test_update_task_status_duration_and_preview():
    okr = _make_okr_with_tasks(1)
    update_task_status(okr, "t1", "completed", duration_ms=1234, result_preview="done!")
    task = okr["key_results"][0]["tasks"][0]
    assert task["duration_ms"] == 1234
    assert task["result_preview"] == "done!"


# ── get_overall_progress ─────────────────────────────────

@pytest.mark.unit
def test_get_overall_progress_empty():
    assert get_overall_progress({"key_results": []}) == 0


@pytest.mark.unit
def test_get_overall_progress_partial():
    okr = {
        "key_results": [
            {"progress": 50},
            {"progress": 100},
        ]
    }
    assert get_overall_progress(okr) == 75


# ── get_flat_tasks ───────────────────────────────────────

@pytest.mark.unit
def test_get_flat_tasks_includes_kr_id():
    okr = _make_okr_with_tasks(1)
    tasks = get_flat_tasks(okr)
    assert len(tasks) == 1
    assert tasks[0]["kr_id"] == "KR1"
    assert tasks[0]["kr_description"] == "Test KR"


@pytest.mark.unit
def test_get_flat_tasks_order():
    """Tasks from KR1 come before KR2."""
    okr = _make_okr_with_tasks(1)
    okr["key_results"].append({
        "kr_id": "KR2",
        "description": "Second KR",
        "metric": "m",
        "progress": 0,
        "status": "pending",
        "tasks": [{"task_id": "t_kr2", "skill": "s", "agent": "a",
                    "description": "d", "status": "pending",
                    "duration_ms": None, "result_preview": None}],
    })
    tasks = get_flat_tasks(okr)
    assert len(tasks) == 2
    assert tasks[0]["kr_id"] == "KR1"
    assert tasks[1]["kr_id"] == "KR2"
