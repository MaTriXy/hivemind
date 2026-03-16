"""REST endpoints for task status polling.

Exposes:
    GET  /api/projects/{project_id}/tasks               — list all tasks
    GET  /api/projects/{project_id}/tasks/{task_id}     — single task status
    GET  /api/tasks/queues                               — admin queue summary

These endpoints enable the frontend to poll task lifecycle (queued → running
→ done | failed) and to retrieve the conversation_id for each task so it can
load the transcript from the conversation-history API.

All responses use the same ``TaskResponse`` schema so the client has a single
contract regardless of which endpoint it calls.

API contract
------------
Task status object::

    {
      "task_id":         "a3f8c2d1...",      # hex UUID
      "project_id":      "my-api",
      "message":         "fix the login bug",  # first 500 chars
      "status":          "queued" | "running" | "done" | "failed",
      "conversation_id": "uuid..." | null,
      "created_at":      1717000000.0,        # Unix timestamp
      "started_at":      1717000001.5 | null,
      "completed_at":    1717000030.0 | null,
      "error":           "..." | null         # only when failed
    }
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from _shared_utils import valid_project_id as _valid_project_id
from src.workers.task_queue import TaskQueueRegistry, TaskStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


def _problem(status: int, detail: str) -> JSONResponse:
    titles = {
        400: "Bad Request",
        404: "Not Found",
        503: "Service Unavailable",
    }
    return JSONResponse(
        {
            "type": "about:blank",
            "title": titles.get(status, "Error"),
            "status": status,
            "detail": detail,
        },
        status_code=status,
    )


class TaskResponse(BaseModel):
    """Serialized state of one task — returned by all task endpoints."""

    model_config = {"populate_by_name": True}

    task_id: str = Field(
        description="Hex UUID identifying this task.",
        examples=["a3f8c2d1e5b04"],
    )
    project_id: str = Field(description="Project this task belongs to.")
    message: str = Field(description="User message (truncated to 500 chars).")
    status: TaskStatus = Field(description="Current lifecycle stage.")
    conversation_id: str | None = Field(
        default=None,
        description="UUID of the isolated DB conversation for this task.",
    )
    created_at: float = Field(description="Unix timestamp when the task was enqueued.")
    started_at: float | None = Field(
        default=None,
        description="Unix timestamp when execution began.",
    )
    completed_at: float | None = Field(
        default=None,
        description="Unix timestamp when the task finished (done or failed).",
    )
    error: str | None = Field(
        default=None,
        description="Error message if status == failed.",
    )


class TaskListResponse(BaseModel):
    """Paginated list of tasks for a project."""

    project_id: str
    tasks: list[TaskResponse]
    total: int
    limit: int


class QueueSummary(BaseModel):
    """Admin summary of all in-memory task queues."""

    queues: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

tasks_router = APIRouter(prefix="/api/projects/{project_id}", tags=["tasks"])


@tasks_router.get(
    "/tasks",
    response_model=TaskListResponse,
    summary="List all tasks for a project",
    responses={
        200: {"description": "Paginated task list, newest first."},
        400: {"description": "Invalid project_id format."},
    },
)
async def list_project_tasks(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200, description="Max tasks to return."),
) -> TaskListResponse | JSONResponse:
    """Return up to ``limit`` tasks for ``project_id``, ordered newest-first.

    Tasks that have not yet been enqueued return an empty list (not 404).
    The ``status`` field indicates whether a task is queued, running, done,
    or failed.  The ``conversation_id`` field is non-null once the worker has
    created an isolated conversation — use it to fetch the transcript via
    ``GET /api/conversations/{project_id}/{conversation_id}/messages``.
    """
    if not _valid_project_id(project_id):
        return _problem(400, f"Invalid project_id format: {project_id!r}.")

    registry = TaskQueueRegistry.get_registry()
    queue = await registry.get_queue(project_id)

    if queue is None:
        # No tasks have ever been enqueued for this project — return empty list.
        return TaskListResponse(
            project_id=project_id,
            tasks=[],
            total=0,
            limit=limit,
        )

    task_dicts = await queue.list_tasks(limit=limit)
    tasks = [TaskResponse(**td) for td in task_dicts]

    return TaskListResponse(
        project_id=project_id,
        tasks=tasks,
        total=len(tasks),
        limit=limit,
    )


@tasks_router.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    summary="Get a single task by task_id",
    responses={
        200: {"description": "Task found."},
        400: {"description": "Invalid project_id or task_id format."},
        404: {"description": "Task not found."},
    },
)
async def get_project_task(
    project_id: str,
    task_id: str,
) -> TaskResponse | JSONResponse:
    """Return the current status of a single task.

    Poll this endpoint after enqueuing a message to track progress.  A
    ``status`` of ``done`` or ``failed`` means the task has finished.
    """
    if not _valid_project_id(project_id):
        return _problem(400, f"Invalid project_id format: {project_id!r}.")
    if not task_id or len(task_id) > 64 or not task_id.isalnum():
        return _problem(400, "Invalid task_id format.")

    registry = TaskQueueRegistry.get_registry()
    queue = await registry.get_queue(project_id)
    if queue is None:
        return _problem(
            404,
            f"No tasks found for project {project_id!r}.",
        )

    record = await queue.get_task(task_id)
    if record is None:
        return _problem(404, f"Task {task_id!r} not found for project {project_id!r}.")

    return TaskResponse(**record.to_dict())


# ---------------------------------------------------------------------------
# Admin endpoint (no project_id prefix)
# ---------------------------------------------------------------------------

admin_tasks_router = APIRouter(prefix="/api/tasks", tags=["tasks-admin"])


@admin_tasks_router.get(
    "/queues",
    response_model=QueueSummary,
    summary="List all active task queues (admin)",
)
async def list_all_queues() -> QueueSummary:
    """Return a summary of all in-memory task queues across all projects.

    Useful for monitoring how many tasks are queued or running globally.
    ``running_count`` is the number of tasks currently occupying semaphore
    slots; ``queue_depth`` is the number waiting to start.
    """
    registry = TaskQueueRegistry.get_registry()
    queues = await registry.list_queues()
    return QueueSummary(queues=queues)
