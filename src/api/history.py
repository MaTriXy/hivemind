"""REST API router for conversation history.

Endpoints
---------
GET  /api/conversations/{project_id}
    List all conversations for a project (paginated).

GET  /api/conversations/{project_id}/{conversation_id}
    Get a single conversation with its full message history.

GET  /api/conversations/{project_id}/{conversation_id}/messages
    Get paginated messages for a conversation.

POST /api/conversations/{project_id}
    Create a new conversation for a project.

GET  /api/memory/{project_id}
    Get all agent memory for a project.

PUT  /api/memory/{project_id}/{key}
    Set a memory key for a project.

DELETE /api/memory/{project_id}/{key}
    Delete a memory key for a project.

These endpoints are registered on the FastAPI app by calling
``app.include_router(history_router)`` in ``dashboard/api.py``.

All errors follow the RFC 7807 Problem Detail format::

    {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "Conversation abc123 not found."
    }
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from _shared_utils import valid_project_id as _valid_project_id
from src.dependencies import get_conversation_store, get_memory_store
from src.storage.conversation_store import ConversationStore
from src.storage.memory_store import MemoryStore

logger = logging.getLogger(__name__)

history_router = APIRouter(tags=["conversations"])

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ConversationSummary(BaseModel):
    """Summary of a conversation (no messages included)."""

    id: str = Field(
        description="Conversation UUID", examples=["550e8400-e29b-41d4-a716-446655440000"]
    )
    project_id: str = Field(description="Parent project ID")
    title: str | None = Field(description="Human-readable title, or null if not set")
    created_at: str | None = Field(description="ISO-8601 UTC creation timestamp")
    last_active_at: str | None = Field(description="ISO-8601 UTC last-activity timestamp")

    model_config = {"from_attributes": True}


class MessageSchema(BaseModel):
    """A single message in a conversation."""

    id: str = Field(description="Message UUID")
    conversation_id: str = Field(description="Parent conversation UUID")
    role: str = Field(description="'user' | 'assistant' | 'system' | 'tool'")
    content: str = Field(description="Full message text")
    timestamp: str | None = Field(description="ISO-8601 UTC timestamp")
    metadata: dict | None = Field(
        default=None, description="Optional metadata (model, tokens, cost, …)"
    )

    model_config = {"from_attributes": True}


class ConversationDetail(BaseModel):
    """A conversation with its full message list."""

    id: str
    project_id: str
    title: str | None
    created_at: str | None
    last_active_at: str | None
    messages: list[MessageSchema] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ConversationListResponse(BaseModel):
    """Paginated list of conversations."""

    conversations: list[ConversationSummary]
    total: int
    limit: int
    offset: int
    project_id: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "conversations": [
                    {
                        "id": "550e8400-e29b-41d4-a716-446655440000",
                        "project_id": "my-project",
                        "title": "Fix login bug",
                        "created_at": "2026-03-11T10:00:00+00:00",
                        "last_active_at": "2026-03-11T10:05:00+00:00",
                    }
                ],
                "total": 1,
                "limit": 50,
                "offset": 0,
                "project_id": "my-project",
            }
        }
    }


class CreateConversationRequest(BaseModel):
    """Request body for creating a new conversation."""

    title: str | None = Field(
        default=None,
        max_length=500,
        description="Optional conversation title.",
        examples=["Fix login bug"],
    )

    model_config = {"json_schema_extra": {"example": {"title": "Fix login bug"}}}


class MemorySetRequest(BaseModel):
    """Request body for setting a memory key."""

    value: Any = Field(
        description="Any JSON-serialisable value. Secrets are forbidden.",
        examples=["Alice", 42, ["Python", "FastAPI"], {"nested": True}],
    )

    model_config = {"json_schema_extra": {"example": {"value": "Alice"}}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _problem(status: int, detail: str) -> JSONResponse:
    _TITLES = {
        400: "Bad Request",
        404: "Not Found",
        409: "Conflict",
        422: "Unprocessable Content",
        500: "Internal Server Error",
    }
    return JSONResponse(
        {
            "type": "about:blank",
            "title": _TITLES.get(status, "Error"),
            "status": status,
            "detail": detail,
        },
        status_code=status,
    )


# ---------------------------------------------------------------------------
# Conversation endpoints
# ---------------------------------------------------------------------------


@history_router.get(
    "/api/conversations/{project_id}",
    response_model=ConversationListResponse,
    summary="List conversations for a project",
    description=(
        "Returns all conversations for the given project, ordered by most-recently-active first. "
        "Supports pagination via ``limit`` and ``offset`` query parameters."
    ),
    responses={
        200: {"description": "Paginated list of conversations"},
        400: {"description": "Invalid project_id format"},
        500: {"description": "Internal server error"},
    },
)
async def list_conversations(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=500, description="Max results per page"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    store: ConversationStore = Depends(get_conversation_store),
) -> ConversationListResponse:
    """List all conversations for a project (most-recently-active first).

    This is the primary endpoint for the multi-project conversation sidebar.
    Each conversation summary includes its UUID, title, and timestamps —
    clients can then fetch full message history via the detail endpoint.
    """
    if not _valid_project_id(project_id):
        return _problem(400, f"Invalid project_id format: {project_id!r}")

    try:
        convs = await store.list_conversations(project_id, limit=limit, offset=offset)
        return ConversationListResponse(
            conversations=[ConversationSummary(**c) for c in convs],
            total=len(convs),
            limit=limit,
            offset=offset,
            project_id=project_id,
        )
    except Exception:
        logger.error("GET /api/conversations/%s failed", project_id, exc_info=True)
        return _problem(500, "Failed to load conversations. Check server logs.")


@history_router.get(
    "/api/conversations/{project_id}/{conversation_id}",
    response_model=ConversationDetail,
    summary="Get a conversation with its message history",
    description="Returns a conversation and its full message list for LLM context replay.",
    responses={
        200: {"description": "Conversation with messages"},
        400: {"description": "Invalid project_id format"},
        404: {"description": "Conversation not found"},
        500: {"description": "Internal server error"},
    },
)
async def get_conversation(
    project_id: str,
    conversation_id: str,
    store: ConversationStore = Depends(get_conversation_store),
) -> ConversationDetail:
    """Return a conversation and its full message history.

    Designed for the reconnect flow: after a WebSocket reconnects, the client
    fetches this endpoint to load the agent's full prior context before sending
    new messages. The message list is ordered chronologically (oldest first).
    """
    if not _valid_project_id(project_id):
        return _problem(400, f"Invalid project_id format: {project_id!r}")

    try:
        # Load conversations to find this one
        convs = await store.list_conversations(project_id, limit=1000, offset=0)
        conv = next((c for c in convs if c["id"] == conversation_id), None)
        if conv is None:
            return _problem(
                404, f"Conversation {conversation_id!r} not found in project {project_id!r}."
            )

        messages = await store.get_conversation_history(conversation_id)
        return ConversationDetail(
            **conv,
            messages=[MessageSchema(**m) for m in messages],
        )
    except Exception:
        logger.error(
            "GET /api/conversations/%s/%s failed",
            project_id,
            conversation_id,
            exc_info=True,
        )
        return _problem(500, "Failed to load conversation history. Check server logs.")


@history_router.get(
    "/api/conversations/{project_id}/{conversation_id}/messages",
    summary="Get paginated messages for a conversation",
    description="Returns messages for a conversation in chronological order.",
    responses={
        200: {"description": "List of messages with pagination metadata"},
        400: {"description": "Invalid project_id format"},
        500: {"description": "Internal server error"},
    },
)
async def get_conversation_messages(
    project_id: str,
    conversation_id: str,
    limit: int = Query(default=100, ge=1, le=1000, description="Max messages per page"),
    store: ConversationStore = Depends(get_conversation_store),
) -> dict:
    """Return paginated messages for a conversation.

    Use ``limit`` to restrict context window size on reconnect.
    """
    if not _valid_project_id(project_id):
        return _problem(400, f"Invalid project_id format: {project_id!r}")

    try:
        messages = await store.get_conversation_history(
            conversation_id, limit=limit if limit < 1000 else None
        )
        return {
            "conversation_id": conversation_id,
            "project_id": project_id,
            "messages": messages,
            "count": len(messages),
        }
    except Exception:
        logger.error(
            "GET /api/conversations/%s/%s/messages failed",
            project_id,
            conversation_id,
            exc_info=True,
        )
        return _problem(500, "Failed to load messages. Check server logs.")


@history_router.post(
    "/api/conversations/{project_id}",
    response_model=ConversationSummary,
    status_code=201,
    summary="Create a new conversation",
    description="Creates a new conversation for the given project.",
    responses={
        201: {"description": "Conversation created"},
        400: {"description": "Invalid project_id format"},
        500: {"description": "Internal server error"},
    },
)
async def create_conversation(
    project_id: str,
    req: CreateConversationRequest,
    store: ConversationStore = Depends(get_conversation_store),
) -> ConversationSummary | JSONResponse:
    """Create a new conversation for a project.

    Returns the new conversation's UUID and metadata. The conversation will
    have no messages until ``append_message`` is called.
    """
    if not _valid_project_id(project_id):
        return _problem(400, f"Invalid project_id format: {project_id!r}")

    try:
        conv_id = await store.create_conversation(project_id, title=req.title)
        convs = await store.list_conversations(project_id, limit=1000, offset=0)
        conv = next((c for c in convs if c["id"] == conv_id), None)
        if conv is None:
            return _problem(500, "Conversation created but could not be retrieved.")
        return ConversationSummary(**conv)
    except Exception:
        logger.error("POST /api/conversations/%s failed", project_id, exc_info=True)
        return _problem(500, "Failed to create conversation. Check server logs.")


# ---------------------------------------------------------------------------
# Memory endpoints
# ---------------------------------------------------------------------------


@history_router.get(
    "/api/memory/{project_id}",
    summary="Get all agent memory for a project",
    description=(
        "Returns the full persistent agent context for a project as a flat key/value dict. "
        "Used by agents on reconnect to restore their working state."
    ),
    responses={
        200: {"description": "Dict of all memory entries"},
        400: {"description": "Invalid project_id format"},
        500: {"description": "Internal server error"},
    },
)
async def get_project_memory(
    project_id: str,
    store: MemoryStore = Depends(get_memory_store),
) -> dict:
    """Return all persisted agent memory for a project.

    Example response::

        {
            "project_id": "my-project",
            "memory": {
                "user.name": "Alice",
                "project.tech_stack": ["Python", "FastAPI"],
                "agent.orchestrator.last_plan": "..."
            },
            "count": 3
        }
    """
    if not _valid_project_id(project_id):
        return _problem(400, f"Invalid project_id format: {project_id!r}")

    try:
        memory = await store.get_all_memory(project_id)
        return {
            "project_id": project_id,
            "memory": memory,
            "count": len(memory),
        }
    except Exception:
        logger.error("GET /api/memory/%s failed", project_id, exc_info=True)
        return _problem(500, "Failed to load memory. Check server logs.")


@history_router.put(
    "/api/memory/{project_id}/{key:path}",
    status_code=200,
    summary="Set a memory key for a project",
    description="Upserts a key/value pair in the project's agent memory. Secrets are forbidden.",
    responses={
        200: {"description": "Memory entry written"},
        400: {"description": "Invalid project_id format or key"},
        500: {"description": "Internal server error"},
    },
)
async def set_project_memory(
    project_id: str,
    key: str,
    req: MemorySetRequest,
    store: MemoryStore = Depends(get_memory_store),
) -> dict:
    """Set (upsert) a memory key for a project.

    Keys must follow dot-notation naming (e.g. ``agent.last_plan``).
    Secrets, API keys, and passwords are explicitly forbidden — the memory
    table is not encrypted and is readable by DB-level access.
    """
    if not _valid_project_id(project_id):
        return _problem(400, f"Invalid project_id format: {project_id!r}")

    try:
        await store.set_memory(project_id, key, req.value)
        return {"ok": True, "project_id": project_id, "key": key}
    except ValueError as e:
        return _problem(400, str(e))
    except Exception:
        logger.error("PUT /api/memory/%s/%s failed", project_id, key, exc_info=True)
        return _problem(500, "Failed to set memory. Check server logs.")


@history_router.delete(
    "/api/memory/{project_id}/{key:path}",
    summary="Delete a memory key for a project",
    description="Removes a key from the project's agent memory. Returns 404 if not found.",
    responses={
        200: {"description": "Memory entry deleted"},
        400: {"description": "Invalid project_id format"},
        404: {"description": "Key not found"},
        500: {"description": "Internal server error"},
    },
)
async def delete_project_memory(
    project_id: str,
    key: str,
    store: MemoryStore = Depends(get_memory_store),
) -> dict:
    """Delete a memory key from a project."""
    if not _valid_project_id(project_id):
        return _problem(400, f"Invalid project_id format: {project_id!r}")

    try:
        deleted = await store.delete_memory(project_id, key)
        if not deleted:
            return _problem(404, f"Memory key {key!r} not found for project {project_id!r}.")
        return {"ok": True, "project_id": project_id, "key": key, "deleted": True}
    except ValueError as e:
        return _problem(400, str(e))
    except Exception:
        logger.error("DELETE /api/memory/%s/%s failed", project_id, key, exc_info=True)
        return _problem(500, "Failed to delete memory. Check server logs.")
