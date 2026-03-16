"""ConversationStore — persistent SSOT for conversation history.

All messages are persisted to the ``conversations`` and ``messages`` tables
in platform.db. This store is the single source of truth for conversation
history.

Design decisions:
- Projects are auto-provisioned: if a project row doesn't exist yet,
  we create a minimal stub so the FK constraint is satisfied.
- Conversations are project-scoped and identified by UUID. Each project has
  at least one "default" conversation created on first message.
- Messages are append-only (no edits). Full history is reconstructed by
  fetching all messages ordered by timestamp.
- All DB calls use async SQLAlchemy — zero blocking I/O in async context.
- ``_ensure_project`` is dialect-agnostic (works on both SQLite and PostgreSQL)
  via the shared helper in ``src/storage/_store_utils``.

Public interface::

    store = ConversationStore(session_factory)

    # Create or resume a conversation
    conv_id = await store.create_conversation(project_id, title="Fix login bug")

    # Append user/assistant messages
    await store.append_message(conv_id, role="user", content="Hello")
    await store.append_message(conv_id, role="assistant", content="Hi!", metadata={"model": "claude-3-5-sonnet"})

    # Reload full history on reconnect
    messages = await store.get_conversation_history(conv_id)

    # List all conversations for a project (most recent first)
    convs = await store.list_conversations(project_id)

    # Get or create the default conversation for a project
    conv_id = await store.get_or_create_default_conversation(project_id)
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models import Conversation, Message
from src.storage._store_utils import _ensure_project, _utcnow

logger = logging.getLogger(__name__)


class ConversationStore:
    """Async service for reading and writing conversation history.

    Injected via FastAPI ``Depends()`` — see ``src/dependencies.py``.

    Args:
        session_factory: An ``async_sessionmaker[AsyncSession]`` produced by
            ``get_session_factory()`` from ``src.db.database``.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    # ─────────────────────────────────────────────────────────────────────────
    # Public: Conversations
    # ─────────────────────────────────────────────────────────────────────────

    async def create_conversation(
        self,
        project_id: str,
        title: str | None = None,
    ) -> str:
        """Create a new conversation and return its UUID.

        Args:
            project_id: The project this conversation belongs to.
            title:      Optional human-readable title. If omitted, callers may
                        set it later via ``set_conversation_title()``.

        Returns:
            The new conversation's UUID string.

        Raises:
            Exception: On any DB error (logged with full traceback).
        """
        async with self._factory() as db:
            try:
                await _ensure_project(db, project_id)
                conv = Conversation(
                    project_id=project_id,
                    title=title,
                    created_at=_utcnow(),
                    last_active_at=_utcnow(),
                )
                db.add(conv)
                await db.flush()  # populate conv.id before commit
                conv_id = conv.id
                await db.commit()
                logger.info(
                    "ConversationStore: created conversation %s for project %s (title=%r)",
                    conv_id,
                    project_id,
                    title,
                )
                return conv_id
            except Exception:
                logger.error(
                    "ConversationStore.create_conversation failed for project %s",
                    project_id,
                    exc_info=True,
                )
                await db.rollback()
                raise

    async def get_or_create_default_conversation(self, project_id: str) -> str:
        """Return the most-recently-active conversation for a project.

        If no conversation exists, creates one with title ``"default"``.
        This is the main entry point for WebSocket sessions that need a
        ``conversation_id`` without the caller specifying one.

        Args:
            project_id: The project to look up or create a conversation for.

        Returns:
            UUID of the conversation.
        """
        async with self._factory() as db:
            try:
                await _ensure_project(db, project_id)
                stmt = (
                    select(Conversation)
                    .where(Conversation.project_id == project_id)
                    .order_by(Conversation.last_active_at.desc())
                    .limit(1)
                )
                result = await db.execute(stmt)
                conv = result.scalar_one_or_none()
                if conv is not None:
                    await db.commit()
                    return conv.id

                # No conversation yet — create the default one
                conv = Conversation(
                    project_id=project_id,
                    title="default",
                    created_at=_utcnow(),
                    last_active_at=_utcnow(),
                )
                db.add(conv)
                await db.flush()
                conv_id = conv.id
                await db.commit()
                logger.info(
                    "ConversationStore: created default conversation %s for project %s",
                    conv_id,
                    project_id,
                )
                return conv_id
            except Exception:
                logger.error(
                    "ConversationStore.get_or_create_default_conversation failed for project %s",
                    project_id,
                    exc_info=True,
                )
                await db.rollback()
                raise

    async def set_conversation_title(
        self,
        conversation_id: str,
        title: str,
    ) -> None:
        """Update the title of an existing conversation.

        Args:
            conversation_id: UUID of the conversation to update.
            title:           New human-readable title.
        """
        async with self._factory() as db:
            try:
                stmt = (
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(title=title, last_active_at=_utcnow())
                )
                await db.execute(stmt)
                await db.commit()
            except Exception:
                logger.error(
                    "ConversationStore.set_conversation_title failed for conv %s",
                    conversation_id,
                    exc_info=True,
                )
                await db.rollback()
                raise

    async def list_conversations(
        self,
        project_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return conversations for a project, most-recently-active first.

        Args:
            project_id: Filter by this project.
            limit:      Max number of results (clamped to 1–500 by caller).
            offset:     Pagination offset.

        Returns:
            List of dicts with keys: id, project_id, title, created_at,
            last_active_at.
        """
        async with self._factory() as db:
            try:
                stmt = (
                    select(Conversation)
                    .where(Conversation.project_id == project_id)
                    .order_by(Conversation.last_active_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
                result = await db.execute(stmt)
                convs = result.scalars().all()
                return [_conv_to_dict(c) for c in convs]
            except Exception:
                logger.error(
                    "ConversationStore.list_conversations failed for project %s",
                    project_id,
                    exc_info=True,
                )
                raise

    # ─────────────────────────────────────────────────────────────────────────
    # Public: Messages
    # ─────────────────────────────────────────────────────────────────────────

    async def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Persist a message to the conversation and update last_active_at.

        Args:
            conversation_id: UUID of the target conversation.
            role:            ``"user"`` | ``"assistant"`` | ``"system"`` | ``"tool"``.
            content:         Full message text.
            metadata:        Optional metadata blob (model, tokens, cost, etc.).

        Returns:
            UUID of the newly created message.

        Raises:
            ValueError: If role is not a recognised value.
            Exception:  On DB errors (logged with full traceback).
        """
        _VALID_ROLES = {"user", "assistant", "system", "tool"}
        if role not in _VALID_ROLES:
            raise ValueError(f"Invalid role {role!r}. Must be one of {_VALID_ROLES}.")

        # Sanitize surrogate characters that SQLite/aiosqlite cannot encode
        if content and isinstance(content, str):
            content = content.encode("utf-8", errors="replace").decode("utf-8")

        async with self._factory() as db:
            try:
                msg = Message(
                    conversation_id=conversation_id,
                    role=role,
                    content=content,
                    timestamp=_utcnow(),
                    metadata_json=metadata,
                )
                db.add(msg)
                # Update last_active_at on the parent conversation
                await db.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(last_active_at=_utcnow())
                )
                await db.flush()
                msg_id = msg.id
                await db.commit()
                logger.debug(
                    "ConversationStore: appended %s message %s to conv %s",
                    role,
                    msg_id,
                    conversation_id,
                )
                return msg_id
            except Exception:
                logger.error(
                    "ConversationStore.append_message failed (conv=%s role=%s)",
                    conversation_id,
                    role,
                    exc_info=True,
                )
                await db.rollback()
                raise

    async def get_conversation_history(
        self,
        conversation_id: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return all messages in a conversation, ordered by timestamp ASC.

        Designed for context-window reconstruction on agent reconnect.
        Returns the full, untruncated history by default; pass ``limit``
        to retrieve only the N most recent messages.

        Args:
            conversation_id: UUID of the conversation.
            limit:           If set, return only the last N messages.

        Returns:
            List of dicts with keys: id, conversation_id, role, content,
            timestamp, metadata_json.
        """
        async with self._factory() as db:
            try:
                stmt = (
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.timestamp.asc())
                )
                if limit is not None:
                    # We still order ASC but take the tail — use a subquery approach
                    # or simply fetch all and slice (conversations are typically small).
                    # For very long conversations use proper DESC + LIMIT + reverse.
                    inner = (
                        select(Message)
                        .where(Message.conversation_id == conversation_id)
                        .order_by(Message.timestamp.desc())
                        .limit(limit)
                        .subquery()
                    )
                    from sqlalchemy.orm import aliased

                    msg_alias = aliased(Message, inner)
                    stmt = select(msg_alias).order_by(msg_alias.timestamp.asc())

                result = await db.execute(stmt)
                messages = result.scalars().all()
                return [_msg_to_dict(m) for m in messages]
            except Exception:
                logger.error(
                    "ConversationStore.get_conversation_history failed for conv %s",
                    conversation_id,
                    exc_info=True,
                )
                raise


# ─────────────────────────────────────────────────────────────────────────────
# Serialisation helpers (keep ORM objects out of API layer)
# ─────────────────────────────────────────────────────────────────────────────


def _conv_to_dict(c: Conversation) -> dict[str, Any]:
    return {
        "id": c.id,
        "project_id": c.project_id,
        "title": c.title,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "last_active_at": c.last_active_at.isoformat() if c.last_active_at else None,
    }


def _msg_to_dict(m: Message) -> dict[str, Any]:
    return {
        "id": m.id,
        "conversation_id": m.conversation_id,
        "role": m.role,
        "content": m.content,
        "timestamp": m.timestamp.isoformat() if m.timestamp else None,
        "metadata": m.metadata_json,
    }
