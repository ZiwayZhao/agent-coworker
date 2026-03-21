"""Tests for TaskManager — task lifecycle state machine."""

import pytest
from datetime import datetime, timezone, timedelta

from task_manager import TaskManager, TaskState


class TestTaskCreation:
    """Test task creation and basic queries."""

    def test_create_task(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        task_id = tm.create_task("echo", {"text": "hello"}, peer_wallet="0xABC")

        assert task_id.startswith("task_")
        task = tm.get_task(task_id)
        assert task is not None
        assert task["skill"] == "echo"
        assert task["input_data"] == {"text": "hello"}
        assert task["state"] == TaskState.PENDING
        assert task["role"] == "requester"
        assert task["peer_wallet"] == "0xABC"
        tm.close()

    def test_create_task_with_peer_name(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        task_id = tm.create_task("echo", {}, peer_name="icy")
        task = tm.get_task(task_id)
        assert task["peer_name"] == "icy"
        tm.close()

    def test_create_task_generates_correlation_id(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        task_id = tm.create_task("echo", {})
        task = tm.get_task(task_id)
        assert task["correlation_id"] is not None
        assert "task_" in task["correlation_id"]
        tm.close()

    def test_create_task_custom_timeout(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        task_id = tm.create_task("echo", {}, timeout_seconds=60)
        task = tm.get_task(task_id)
        assert task["timeout_seconds"] == 60
        tm.close()

    def test_get_nonexistent_task(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        assert tm.get_task("nonexistent") is None
        tm.close()


class TestTaskLifecycle:
    """Test full requester-side lifecycle: pending → sent → acked → completed."""

    def test_requester_full_lifecycle(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        task_id = tm.create_task("summarize", {"text": "long document..."})

        # pending → sent
        tm.mark_sent(task_id)
        assert tm.get_task(task_id)["state"] == TaskState.SENT

        # sent → acked (executor acknowledged)
        tm.accept_task(task_id)
        assert tm.get_task(task_id)["state"] == TaskState.ACKED

        # acked → in_progress
        tm.start_task(task_id)
        assert tm.get_task(task_id)["state"] == TaskState.IN_PROGRESS

        # in_progress → completed
        tm.complete_task(task_id, result={"summary": "short version"})
        task = tm.get_task(task_id)
        assert task["state"] == TaskState.COMPLETED
        assert task["output_data"] == {"summary": "short version"}
        assert task["progress_pct"] == 100
        assert task["completed_at"] is not None
        tm.close()

    def test_executor_full_lifecycle(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)

        # Receive incoming task
        tm.receive_task(
            task_id="task_remote_001",
            skill="echo",
            input_data={"text": "hello"},
            peer_wallet="0xREQUESTER",
            peer_name="ziway",
            correlation_id="corr_001",
        )

        task = tm.get_task("task_remote_001")
        assert task["role"] == "executor"
        assert task["state"] == TaskState.PENDING

        # Accept
        tm.accept_task("task_remote_001")
        assert tm.get_task("task_remote_001")["state"] == TaskState.ACKED

        # Start
        tm.start_task("task_remote_001")
        assert tm.get_task("task_remote_001")["state"] == TaskState.IN_PROGRESS

        # Progress updates
        tm.update_progress("task_remote_001", 50, "halfway")
        task = tm.get_task("task_remote_001")
        assert task["progress_pct"] == 50
        assert task["progress_text"] == "halfway"

        # Complete
        tm.complete_task("task_remote_001", result={"echo": "hello"})
        assert tm.get_task("task_remote_001")["state"] == TaskState.COMPLETED
        tm.close()


class TestTaskRejection:
    """Test task rejection and failure paths."""

    def test_reject_task(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        tm.receive_task("task_rej", "unknown_skill", {}, "0xPEER")
        tm.reject_task("task_rej", reason="skill not available")

        task = tm.get_task("task_rej")
        assert task["state"] == TaskState.REJECTED
        assert task["error_message"] == "skill not available"
        tm.close()

    def test_fail_task(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        task_id = tm.create_task("buggy", {})
        tm.mark_sent(task_id)
        tm.start_task(task_id)
        tm.fail_task(task_id, error="runtime crash")

        task = tm.get_task(task_id)
        assert task["state"] == TaskState.FAILED
        assert task["error_message"] == "runtime crash"
        assert task["completed_at"] is not None
        tm.close()

    def test_cancel_task(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        task_id = tm.create_task("slow_task", {})
        tm.mark_sent(task_id)
        tm.cancel_task(task_id)

        task = tm.get_task(task_id)
        assert task["state"] == TaskState.CANCELLED
        assert task["completed_at"] is not None
        tm.close()


class TestTaskTimeout:
    """Test timeout detection."""

    def test_timeout_detection(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        task_id = tm.create_task("slow", {}, timeout_seconds=1)
        tm.mark_sent(task_id)

        # Manually backdate created_at
        tm.conn.execute(
            "UPDATE tasks SET created_at = ? WHERE task_id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
                task_id,
            ),
        )
        tm.conn.commit()

        timed_out = tm.check_timeouts()
        assert task_id in timed_out
        assert tm.get_task(task_id)["state"] == TaskState.TIMED_OUT
        tm.close()

    def test_no_timeout_when_within_limit(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        task_id = tm.create_task("fast", {}, timeout_seconds=9999)
        tm.mark_sent(task_id)

        timed_out = tm.check_timeouts()
        assert task_id not in timed_out
        assert tm.get_task(task_id)["state"] == TaskState.SENT
        tm.close()

    def test_completed_tasks_not_timed_out(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        task_id = tm.create_task("done", {}, timeout_seconds=1)
        tm.complete_task(task_id, result={"ok": True})

        # Backdate
        tm.conn.execute(
            "UPDATE tasks SET created_at = ? WHERE task_id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat(),
                task_id,
            ),
        )
        tm.conn.commit()

        timed_out = tm.check_timeouts()
        assert task_id not in timed_out  # already completed
        tm.close()


class TestTaskQueries:
    """Test query and correlation lookups."""

    def test_query_by_state(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        id1 = tm.create_task("a", {})
        id2 = tm.create_task("b", {})
        tm.mark_sent(id1)

        pending = tm.query(state=TaskState.PENDING)
        assert len(pending) == 1
        assert pending[0]["task_id"] == id2

        sent = tm.query(state=TaskState.SENT)
        assert len(sent) == 1
        assert sent[0]["task_id"] == id1
        tm.close()

    def test_query_by_role(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        tm.create_task("a", {})  # requester
        tm.receive_task("remote_1", "b", {}, "0xPEER")  # executor

        requesters = tm.query(role="requester")
        assert len(requesters) == 1

        executors = tm.query(role="executor")
        assert len(executors) == 1
        assert executors[0]["task_id"] == "remote_1"
        tm.close()

    def test_query_by_skill(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        tm.create_task("echo", {"text": "a"})
        tm.create_task("summarize", {"text": "b"})
        tm.create_task("echo", {"text": "c"})

        echo_tasks = tm.query(skill="echo")
        assert len(echo_tasks) == 2
        tm.close()

    def test_get_by_correlation(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        task_id = tm.create_task("echo", {})
        task = tm.get_task(task_id)
        corr_id = task["correlation_id"]

        found = tm.get_by_correlation(corr_id)
        assert found is not None
        assert found["task_id"] == task_id
        tm.close()

    def test_get_by_correlation_not_found(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        assert tm.get_by_correlation("nonexistent") is None
        tm.close()

    def test_query_limit(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        for i in range(10):
            tm.create_task("echo", {"i": i})

        results = tm.query(limit=3)
        assert len(results) == 3
        tm.close()

    def test_receive_duplicate_ignored(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        tm.receive_task("task_dup", "echo", {}, "0xPEER")
        tm.receive_task("task_dup", "echo", {}, "0xPEER")  # duplicate

        # Should only have one
        task = tm.get_task("task_dup")
        assert task is not None
        all_tasks = tm.query()
        assert len(all_tasks) == 1
        tm.close()


class TestTaskDuration:
    """Test duration calculation on completion."""

    def test_duration_calculated(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        task_id = tm.create_task("echo", {})
        tm.start_task(task_id)

        # Backdate started_at by 2 seconds
        tm.conn.execute(
            "UPDATE tasks SET started_at = ? WHERE task_id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
                task_id,
            ),
        )
        tm.conn.commit()

        tm.complete_task(task_id, result={"ok": True})
        task = tm.get_task(task_id)
        assert task["duration_ms"] is not None
        assert task["duration_ms"] >= 1900  # ~2000ms with some tolerance
        tm.close()

    def test_duration_none_without_start(self, tmp_data_dir):
        tm = TaskManager(tmp_data_dir)
        task_id = tm.create_task("echo", {})
        # Complete without starting
        tm.complete_task(task_id, result={"ok": True})
        task = tm.get_task(task_id)
        assert task["duration_ms"] is None
        tm.close()
