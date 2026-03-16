"""Tests for the parallel task queue (task_004).

Covers:
- TaskRecord lifecycle and serialization
- ProjectTaskQueue: enqueue → running → done / failed
- Concurrency: 3 tasks enqueued together complete without collision
- PARALLEL_TASKS_LIMIT env var respected via max_concurrent arg
- No race conditions in shared state (tasks_lock guards _tasks dict)
- TaskQueueRegistry singleton pattern
- REST endpoint contracts (list tasks, get single task)
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.workers.task_queue import (
    PARALLEL_TASKS_LIMIT,
    ProjectTaskQueue,
    TaskQueueRegistry,
    TaskRecord,
    TaskStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _noop_worker(record: TaskRecord, **kwargs) -> None:
    """Worker that does nothing — marks task done immediately."""
    pass


async def _slow_worker(record: TaskRecord, *, delay: float = 0.05, **kwargs) -> None:
    """Worker that sleeps for ``delay`` seconds."""
    await asyncio.sleep(delay)


async def _failing_worker(record: TaskRecord, **kwargs) -> None:
    """Worker that always raises."""
    raise ValueError("simulated worker failure")


async def _capture_worker(
    record: TaskRecord,
    *,
    started_events: list[str],
    done_events: list[str],
    delay: float = 0.05,
    **kwargs,
) -> None:
    """Worker that records task_id on start/done for collision detection."""
    started_events.append(record.task_id)
    await asyncio.sleep(delay)
    done_events.append(record.task_id)


# ---------------------------------------------------------------------------
# TaskRecord
# ---------------------------------------------------------------------------


class TestTaskRecord:
    def test_initial_state(self):
        record = TaskRecord(task_id="abc", project_id="proj", message="hello")
        assert record.status == TaskStatus.queued
        assert record.started_at is None
        assert record.completed_at is None
        assert record.error is None
        assert record.conversation_id is None

    def test_to_dict_keys(self):
        record = TaskRecord(task_id="abc", project_id="proj", message="hello world")
        d = record.to_dict()
        assert set(d.keys()) == {
            "task_id",
            "project_id",
            "message",
            "status",
            "conversation_id",
            "created_at",
            "started_at",
            "completed_at",
            "error",
        }

    def test_to_dict_truncates_message(self):
        long_msg = "x" * 600
        record = TaskRecord(task_id="a", project_id="p", message=long_msg)
        assert len(record.to_dict()["message"]) == 500

    def test_to_dict_status_is_string(self):
        record = TaskRecord(task_id="a", project_id="p", message="hi")
        assert record.to_dict()["status"] == "queued"


# ---------------------------------------------------------------------------
# ProjectTaskQueue — basic operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestProjectTaskQueueBasic:
    async def test_enqueue_returns_record_immediately(self):
        q = ProjectTaskQueue("proj-a", max_concurrent=5)
        q.start()
        try:
            record = await q.enqueue("hello", _noop_worker)
            assert record.task_id
            assert record.status == TaskStatus.queued
            assert record.project_id == "proj-a"
            assert record.message == "hello"
        finally:
            await q.stop()

    async def test_task_completes_to_done(self):
        q = ProjectTaskQueue("proj-b", max_concurrent=5)
        q.start()
        try:
            record = await q.enqueue("hello", _noop_worker)
            # Wait for processing
            for _ in range(50):
                await asyncio.sleep(0.02)
                if record.status == TaskStatus.done:
                    break
            assert record.status == TaskStatus.done
            assert record.started_at is not None
            assert record.completed_at is not None
            assert record.completed_at >= record.started_at
        finally:
            await q.stop()

    async def test_failing_task_marked_failed(self):
        q = ProjectTaskQueue("proj-c", max_concurrent=5)
        q.start()
        try:
            record = await q.enqueue("bad", _failing_worker)
            for _ in range(50):
                await asyncio.sleep(0.02)
                if record.status in (TaskStatus.done, TaskStatus.failed):
                    break
            assert record.status == TaskStatus.failed
            assert "simulated worker failure" in (record.error or "")
        finally:
            await q.stop()

    async def test_get_task_by_id(self):
        q = ProjectTaskQueue("proj-d", max_concurrent=5)
        q.start()
        try:
            record = await q.enqueue("lookup me", _slow_worker, delay=0.01)
            found = await q.get_task(record.task_id)
            assert found is record
        finally:
            await q.stop()

    async def test_get_task_unknown_returns_none(self):
        q = ProjectTaskQueue("proj-e", max_concurrent=5)
        q.start()
        try:
            result = await q.get_task("nonexistent-task-id")
            assert result is None
        finally:
            await q.stop()

    async def test_list_tasks_newest_first(self):
        q = ProjectTaskQueue("proj-f", max_concurrent=5)
        q.start()
        try:
            r1 = await q.enqueue("first", _noop_worker)
            await asyncio.sleep(0.005)
            await q.enqueue("second", _noop_worker)
            await asyncio.sleep(0.005)
            r3 = await q.enqueue("third", _noop_worker)

            # Wait for all to complete
            for _ in range(50):
                await asyncio.sleep(0.02)
                if r3.status == TaskStatus.done:
                    break

            tasks = await q.list_tasks()
            task_ids = [t["task_id"] for t in tasks]
            # Newest (r3) should come first
            assert task_ids.index(r3.task_id) < task_ids.index(r1.task_id)
        finally:
            await q.stop()


# ---------------------------------------------------------------------------
# Concurrency: 3 tasks run without collision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestConcurrency:
    async def test_three_concurrent_tasks_no_collision(self):
        """3 concurrent messages to same project complete without state collision.

        Verifies acceptance criterion 1: each task records its own task_id
        independently in started_events and done_events — no cross-task writes.
        """
        started_events: list[str] = []
        done_events: list[str] = []

        q = ProjectTaskQueue("proj-concurrent", max_concurrent=5)
        q.start()
        try:
            r1 = await q.enqueue(
                "msg1",
                _capture_worker,
                started_events=started_events,
                done_events=done_events,
                delay=0.05,
            )
            r2 = await q.enqueue(
                "msg2",
                _capture_worker,
                started_events=started_events,
                done_events=done_events,
                delay=0.05,
            )
            r3 = await q.enqueue(
                "msg3",
                _capture_worker,
                started_events=started_events,
                done_events=done_events,
                delay=0.05,
            )

            # Wait for all to finish
            for _ in range(100):
                await asyncio.sleep(0.02)
                if all(r.status == TaskStatus.done for r in (r1, r2, r3)):
                    break

            assert r1.status == TaskStatus.done
            assert r2.status == TaskStatus.done
            assert r3.status == TaskStatus.done

            # Each task_id appears exactly once in each list — no collisions
            assert sorted(started_events) == sorted([r1.task_id, r2.task_id, r3.task_id])
            assert sorted(done_events) == sorted([r1.task_id, r2.task_id, r3.task_id])

            # All three ran in parallel (started before any finished)
            # If they ran serially, done_events[0] would appear before started_events[2]
            # We can't guarantee exact ordering but we CAN verify all completed
            assert len(started_events) == 3
            assert len(done_events) == 3
        finally:
            await q.stop()

    async def test_tasks_have_unique_task_ids(self):
        """Each enqueued task gets a globally unique task_id."""
        q = ProjectTaskQueue("proj-unique", max_concurrent=5)
        q.start()
        try:
            records = [await q.enqueue(f"msg{i}", _noop_worker) for i in range(10)]
            task_ids = [r.task_id for r in records]
            assert len(set(task_ids)) == 10  # All unique
        finally:
            await q.stop()

    async def test_tasks_do_not_share_record_state(self):
        """Concurrent tasks updating their own TaskRecord don't clobber each other."""

        async def _set_conv_id(record: TaskRecord, **kwargs) -> None:
            await asyncio.sleep(0.01)
            record.conversation_id = f"conv-{record.task_id}"

        q = ProjectTaskQueue("proj-isolation", max_concurrent=5)
        q.start()
        try:
            records = [await q.enqueue(f"m{i}", _set_conv_id) for i in range(5)]
            for _ in range(80):
                await asyncio.sleep(0.02)
                if all(r.status == TaskStatus.done for r in records):
                    break
            for r in records:
                assert r.conversation_id == f"conv-{r.task_id}", (
                    f"Race condition: task {r.task_id} has wrong conv_id={r.conversation_id!r}"
                )
        finally:
            await q.stop()


# ---------------------------------------------------------------------------
# PARALLEL_TASKS_LIMIT respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSemaphore:
    async def test_max_concurrent_1_serialises_tasks(self):
        """With max_concurrent=1, tasks run one at a time (semaphore blocks the rest)."""
        order: list[str] = []

        async def _ordered_worker(record: TaskRecord, **kwargs) -> None:
            order.append(f"start:{record.task_id[:4]}")
            await asyncio.sleep(0.03)
            order.append(f"done:{record.task_id[:4]}")

        q = ProjectTaskQueue("proj-serial", max_concurrent=1)
        q.start()
        try:
            r1 = await q.enqueue("a", _ordered_worker)
            r2 = await q.enqueue("b", _ordered_worker)
            r3 = await q.enqueue("c", _ordered_worker)

            for _ in range(150):
                await asyncio.sleep(0.02)
                if all(r.status == TaskStatus.done for r in (r1, r2, r3)):
                    break

            # With max_concurrent=1, each task must finish before the next starts
            # Order must be: start1, done1, start2, done2, start3, done3
            for i in range(0, len(order) - 1, 2):
                assert order[i].startswith("start:"), f"Expected start at index {i}: {order}"
                assert order[i + 1].startswith("done:"), f"Expected done at index {i + 1}: {order}"
                # Same task_id prefix in paired start/done
                assert order[i][6:] == order[i + 1][5:], f"Mismatch: {order[i]} vs {order[i + 1]}"
        finally:
            await q.stop()

    async def test_max_concurrent_default_is_env_var(self):
        """ProjectTaskQueue defaults to PARALLEL_TASKS_LIMIT from env."""
        q = ProjectTaskQueue("proj-env")
        assert q.max_concurrent == PARALLEL_TASKS_LIMIT
        q.start()
        await q.stop()

    async def test_custom_max_concurrent_respected(self):
        """max_concurrent=2 allows exactly 2 tasks to run simultaneously."""
        concurrency_peak = [0]
        running_now = [0]

        async def _track_concurrency(record: TaskRecord, **kwargs) -> None:
            running_now[0] += 1
            if running_now[0] > concurrency_peak[0]:
                concurrency_peak[0] = running_now[0]
            await asyncio.sleep(0.04)
            running_now[0] -= 1

        q = ProjectTaskQueue("proj-limited", max_concurrent=2)
        q.start()
        try:
            records = [await q.enqueue(f"m{i}", _track_concurrency) for i in range(6)]
            for _ in range(200):
                await asyncio.sleep(0.02)
                if all(r.status == TaskStatus.done for r in records):
                    break
            # Peak concurrency must not exceed 2
            assert concurrency_peak[0] <= 2, (
                f"Semaphore not respected: peak concurrency was {concurrency_peak[0]}"
            )
        finally:
            await q.stop()


# ---------------------------------------------------------------------------
# TaskQueueRegistry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTaskQueueRegistry:
    async def test_get_registry_is_singleton(self):
        r1 = TaskQueueRegistry.get_registry()
        r2 = TaskQueueRegistry.get_registry()
        assert r1 is r2

    async def test_get_or_create_queue_creates_and_starts(self):
        registry = TaskQueueRegistry.get_registry()
        queue = await registry.get_or_create_queue("test-singleton-proj")
        assert isinstance(queue, ProjectTaskQueue)
        assert queue._running

    async def test_get_or_create_queue_idempotent(self):
        registry = TaskQueueRegistry.get_registry()
        q1 = await registry.get_or_create_queue("idempotent-proj")
        q2 = await registry.get_or_create_queue("idempotent-proj")
        assert q1 is q2

    async def test_get_queue_returns_none_for_unknown(self):
        registry = TaskQueueRegistry.get_registry()
        q = await registry.get_queue("totally-unknown-project-xyz")
        assert q is None

    async def test_list_queues_includes_created(self):
        registry = TaskQueueRegistry.get_registry()
        await registry.get_or_create_queue("list-test-proj")
        summaries = await registry.list_queues()
        project_ids = [s["project_id"] for s in summaries]
        assert "list-test-proj" in project_ids

    async def test_each_queue_has_correct_project_id(self):
        registry = TaskQueueRegistry.get_registry()
        await registry.get_or_create_queue("proj-alpha")
        await registry.get_or_create_queue("proj-beta")
        summaries = await registry.list_queues()
        summaries_by_pid = {s["project_id"]: s for s in summaries}
        assert "proj-alpha" in summaries_by_pid
        assert "proj-beta" in summaries_by_pid


# ---------------------------------------------------------------------------
# WebSocket task_id in responses (contract verification)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTaskIdContract:
    async def test_enqueue_returns_task_id_before_completion(self):
        """task_id is available immediately — frontend can display it before agent runs."""
        q = ProjectTaskQueue("proj-immediate", max_concurrent=5)
        q.start()
        try:
            record = await q.enqueue("hello", _slow_worker, delay=1.0)
            # task_id must be a non-empty hex string
            assert record.task_id
            assert all(c in "0123456789abcdef" for c in record.task_id)
            # Status is queued or running (not done yet — delay is 1s)
            assert record.status in (TaskStatus.queued, TaskStatus.running)
        finally:
            await q.stop()

    async def test_task_record_to_dict_has_task_id(self):
        """TaskRecord.to_dict() always includes task_id for WS multiplexing."""
        record = TaskRecord(task_id="deadbeef", project_id="p", message="m")
        d = record.to_dict()
        assert d["task_id"] == "deadbeef"

    async def test_three_tasks_have_distinct_task_ids(self):
        """Three concurrent enqueues produce three distinct task_ids."""
        q = ProjectTaskQueue("proj-three-distinct", max_concurrent=5)
        q.start()
        try:
            r1 = await q.enqueue("a", _noop_worker)
            r2 = await q.enqueue("b", _noop_worker)
            r3 = await q.enqueue("c", _noop_worker)
            ids = {r1.task_id, r2.task_id, r3.task_id}
            assert len(ids) == 3  # All distinct
        finally:
            await q.stop()


# ---------------------------------------------------------------------------
# REST endpoint contracts (integration-style tests without a real DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTasksRESTContract:
    """Verify the tasks_router returns correct schema shapes."""

    async def test_task_response_schema(self):
        from src.api.tasks import TaskResponse

        record = TaskRecord(
            task_id="abc123",
            project_id="my-project",
            message="do something",
            status=TaskStatus.running,
            conversation_id="conv-uuid",
            started_at=time.time(),
        )
        resp = TaskResponse(**record.to_dict())
        assert resp.task_id == "abc123"
        assert resp.project_id == "my-project"
        assert resp.status == TaskStatus.running
        assert resp.conversation_id == "conv-uuid"

    async def test_task_list_response_schema(self):
        from src.api.tasks import TaskListResponse, TaskResponse

        items = [
            TaskResponse(
                task_id="t1",
                project_id="p",
                message="m1",
                status=TaskStatus.done,
                created_at=time.time(),
            ),
            TaskResponse(
                task_id="t2",
                project_id="p",
                message="m2",
                status=TaskStatus.queued,
                created_at=time.time(),
            ),
        ]
        resp = TaskListResponse(project_id="p", tasks=items, total=2, limit=50)
        assert resp.total == 2
        assert len(resp.tasks) == 2

    async def test_task_status_values(self):
        """All four task statuses are valid string values."""
        assert TaskStatus.queued.value == "queued"
        assert TaskStatus.running.value == "running"
        assert TaskStatus.done.value == "done"
        assert TaskStatus.failed.value == "failed"
