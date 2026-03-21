"""Tests for WorkflowManager — DAG validation, step lifecycle, input resolution."""

import pytest
from workflow import WorkflowManager, StepState, WorkflowState


class TestDAGValidation:
    """Test cycle detection and reference validation."""

    def test_valid_linear_dag(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("linear", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
            {"step_id": "b", "skill": "echo", "depends_on": ["a"]},
            {"step_id": "c", "skill": "echo", "depends_on": ["b"]},
        ])
        assert wf_id.startswith("wf_")
        wm.close()

    def test_valid_diamond_dag(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("diamond", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
            {"step_id": "b", "skill": "echo", "depends_on": ["a"]},
            {"step_id": "c", "skill": "echo", "depends_on": ["a"]},
            {"step_id": "d", "skill": "echo", "depends_on": ["b", "c"]},
        ])
        assert wf_id is not None
        wm.close()

    def test_cycle_detected(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        with pytest.raises(ValueError, match="cycle"):
            wm.create_workflow("cyclic", steps=[
                {"step_id": "a", "skill": "echo", "depends_on": ["c"]},
                {"step_id": "b", "skill": "echo", "depends_on": ["a"]},
                {"step_id": "c", "skill": "echo", "depends_on": ["b"]},
            ])
        wm.close()

    def test_unknown_dependency_rejected(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        with pytest.raises(ValueError, match="unknown step"):
            wm.create_workflow("bad_ref", steps=[
                {"step_id": "a", "skill": "echo", "depends_on": ["nonexistent"]},
            ])
        wm.close()

    def test_self_dependency_is_cycle(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        with pytest.raises(ValueError):
            wm.create_workflow("self_ref", steps=[
                {"step_id": "a", "skill": "echo", "depends_on": ["a"]},
            ])
        wm.close()

    def test_single_step_no_deps(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("single", steps=[
            {"step_id": "only", "skill": "echo", "depends_on": []},
        ])
        assert wf_id is not None
        wm.close()


class TestWorkflowLifecycle:
    """Test workflow state transitions."""

    def test_create_and_start(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
            {"step_id": "b", "skill": "echo", "depends_on": ["a"]},
        ])

        wf = wm.get_workflow(wf_id)
        assert wf["state"] == "draft"

        ready = wm.start_workflow(wf_id)
        assert ready == ["a"]

        wf = wm.get_workflow(wf_id)
        assert wf["state"] == "running"
        wm.close()

    def test_cannot_start_non_draft(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
        ])
        wm.start_workflow(wf_id)

        with pytest.raises(ValueError):
            wm.start_workflow(wf_id)  # Already running
        wm.close()

    def test_cancel_workflow(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
        ])
        wm.start_workflow(wf_id)
        wm.cancel_workflow(wf_id)

        wf = wm.get_workflow(wf_id)
        assert wf["state"] == "cancelled"
        assert wf["steps"][0]["state"] == "cancelled"
        wm.close()

    def test_pause_and_resume(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
        ])
        wm.start_workflow(wf_id)
        wm.pause_workflow(wf_id)

        wf = wm.get_workflow(wf_id)
        assert wf["state"] == "paused"

        ready = wm.resume_workflow(wf_id)
        wf = wm.get_workflow(wf_id)
        assert wf["state"] == "running"
        assert "a" in ready
        wm.close()


class TestStepExecution:
    """Test step state transitions and dependency resolution."""

    def test_complete_step_unlocks_dependent(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
            {"step_id": "b", "skill": "echo", "depends_on": ["a"]},
        ])
        wm.start_workflow(wf_id)

        newly_ready = wm.complete_step(wf_id, "a", {"result": "done"})
        assert "b" in newly_ready
        wm.close()

    def test_diamond_dependency(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("diamond", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
            {"step_id": "b", "skill": "echo", "depends_on": ["a"]},
            {"step_id": "c", "skill": "echo", "depends_on": ["a"]},
            {"step_id": "d", "skill": "echo", "depends_on": ["b", "c"]},
        ])
        wm.start_workflow(wf_id)

        # Complete a → b and c become ready
        newly = wm.complete_step(wf_id, "a", {"out": 1})
        assert set(newly) == {"b", "c"}

        # Complete b → d still not ready (waiting for c)
        newly = wm.complete_step(wf_id, "b", {"out": 2})
        assert "d" not in newly

        # Complete c → d now ready
        newly = wm.complete_step(wf_id, "c", {"out": 3})
        assert "d" in newly
        wm.close()

    def test_workflow_auto_completes(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
        ])
        wm.start_workflow(wf_id)
        wm.complete_step(wf_id, "a", {"done": True})

        wf = wm.get_workflow(wf_id)
        assert wf["state"] == "completed"
        wm.close()

    def test_dispatch_step(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
        ])
        wm.start_workflow(wf_id)

        wm.dispatch_step(wf_id, "a", task_id="task_123")
        wf = wm.get_workflow(wf_id)
        step_a = wf["steps"][0]
        assert step_a["state"] == "dispatched"
        assert step_a["task_id"] == "task_123"
        wm.close()

    def test_get_step_by_task(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
        ])
        wm.start_workflow(wf_id)
        wm.dispatch_step(wf_id, "a", task_id="task_xyz")

        step = wm.get_step_by_task("task_xyz")
        assert step is not None
        assert step["step_id"] == "a"
        wm.close()


class TestStepFailure:
    """Test failure handling, retries, cascade."""

    def test_fail_step_cascades_to_dependents(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
            {"step_id": "b", "skill": "echo", "depends_on": ["a"]},
            {"step_id": "c", "skill": "echo", "depends_on": ["b"]},
        ])
        wm.start_workflow(wf_id)

        wm.fail_step(wf_id, "a", "something broke")

        wf = wm.get_workflow(wf_id)
        assert wf["state"] == "failed"
        step_states = {s["step_id"]: s["state"] for s in wf["steps"]}
        assert step_states["a"] == "failed"
        assert step_states["b"] == "skipped"
        assert step_states["c"] == "skipped"
        wm.close()

    def test_retry_on_failure(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": [],
             "retry_count": 2},
        ])
        wm.start_workflow(wf_id)

        # First failure → retry (back to ready)
        wm.fail_step(wf_id, "a", "temp error")
        wf = wm.get_workflow(wf_id)
        assert wf["steps"][0]["state"] == "ready"  # retrying
        assert wf["steps"][0]["retries_used"] == 1

        # Second failure → retry again
        wm.fail_step(wf_id, "a", "temp error again")
        wf = wm.get_workflow(wf_id)
        assert wf["steps"][0]["state"] == "ready"
        assert wf["steps"][0]["retries_used"] == 2

        # Third failure → no more retries, actually fails
        wm.fail_step(wf_id, "a", "permanent error")
        wf = wm.get_workflow(wf_id)
        assert wf["steps"][0]["state"] == "failed"
        assert wf["state"] == "failed"
        wm.close()


class TestInputResolution:
    """Test $ref input template resolution."""

    def test_resolve_simple_ref(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
            {"step_id": "b", "skill": "echo", "depends_on": ["a"],
             "input_template": {"data": "$a.output.result"}},
        ])
        wm.start_workflow(wf_id)
        wm.complete_step(wf_id, "a", {"result": "hello"})

        resolved = wm.resolve_step_input(wf_id, "b")
        assert resolved == {"data": "hello"}
        wm.close()

    def test_resolve_nested_ref(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "scan", "skill": "echo", "depends_on": []},
            {"step_id": "report", "skill": "echo", "depends_on": ["scan"],
             "input_template": {"findings": "$scan.output.analysis.findings"}},
        ])
        wm.start_workflow(wf_id)
        wm.complete_step(wf_id, "scan", {
            "analysis": {"findings": ["bug1", "bug2"], "score": 7}
        })

        resolved = wm.resolve_step_input(wf_id, "report")
        assert resolved == {"findings": ["bug1", "bug2"]}
        wm.close()

    def test_resolve_full_output(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
            {"step_id": "b", "skill": "echo", "depends_on": ["a"],
             "input_template": {"prev": "$a.output"}},
        ])
        wm.start_workflow(wf_id)
        wm.complete_step(wf_id, "a", {"x": 1, "y": 2})

        resolved = wm.resolve_step_input(wf_id, "b")
        assert resolved == {"prev": {"x": 1, "y": 2}}
        wm.close()

    def test_resolve_no_template(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
        ])
        wm.start_workflow(wf_id)
        resolved = wm.resolve_step_input(wf_id, "a")
        assert resolved == {}
        wm.close()

    def test_resolve_mixed_refs_and_literals(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf_id = wm.create_workflow("test", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
            {"step_id": "b", "skill": "echo", "depends_on": ["a"],
             "input_template": {
                 "from_a": "$a.output.val",
                 "static": "hardcoded",
                 "number": 42,
             }},
        ])
        wm.start_workflow(wf_id)
        wm.complete_step(wf_id, "a", {"val": "dynamic"})

        resolved = wm.resolve_step_input(wf_id, "b")
        assert resolved == {
            "from_a": "dynamic",
            "static": "hardcoded",
            "number": 42,
        }
        wm.close()


class TestListAndQuery:
    """Test workflow listing and filtering."""

    def test_list_by_state(self, tmp_data_dir):
        wm = WorkflowManager(tmp_data_dir)
        wf1 = wm.create_workflow("wf1", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
        ])
        wf2 = wm.create_workflow("wf2", steps=[
            {"step_id": "a", "skill": "echo", "depends_on": []},
        ])
        wm.start_workflow(wf2)

        drafts = wm.list_workflows(state="draft")
        assert len(drafts) == 1
        running = wm.list_workflows(state="running")
        assert len(running) == 1
        wm.close()
