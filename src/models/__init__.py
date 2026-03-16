"""ORM model package — canonical re-exports.

All application code should import models from this package rather than
directly from ``src.db.models``.  This gives us a stable public API and
lets us restructure the underlying module layout without breaking callers.

    from src.models import Base, User, Project, Conversation, Message, AgentAction, Memory
    from src.models import MessageRole, AgentActionType, TaskStatus
"""

from src.models.base import Base, TimestampMixin
from src.models.schema_registry import (
    ALL_MODELS,
    TABLE_NAMES,
    AgentAction,
    AgentActionType,
    Conversation,
    Memory,
    Message,
    MessageRole,
    Project,
    TaskStatus,
    User,
)

__all__ = [
    # Registry
    "ALL_MODELS",
    "TABLE_NAMES",
    "AgentAction",
    "AgentActionType",
    # Base
    "Base",
    "Conversation",
    "Memory",
    "Message",
    # Enums
    "MessageRole",
    "Project",
    "TaskStatus",
    "TimestampMixin",
    # Models
    "User",
]
