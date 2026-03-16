"""Declarative base and shared ORM mixins for the platform persistence layer.

This module provides:
- ``Base`` — the SQLAlchemy ``DeclarativeBase`` shared by all ORM models.
- ``TimestampMixin`` — adds ``created_at`` / ``updated_at`` columns to any model.

All models should inherit from ``Base`` (and optionally ``TimestampMixin``).
Import ``Base`` from here — never instantiate it yourself.

Design notes
------------
- ``Base`` is the single SQLAlchemy metadata registry.  All tables defined
  by subclasses are registered in ``Base.metadata`` and are therefore picked
  up by Alembic's ``--autogenerate`` (which reads ``target_metadata = Base.metadata``).
- ``TimestampMixin`` uses Python-side defaults (``default=_utcnow``) rather
  than ``server_default`` so that the value is available on the ORM object
  immediately after ``session.flush()`` — no extra DB round-trip needed.
- All ``DateTime`` columns are ``timezone=True`` to enforce UTC storage.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
from _shared_utils import utcnow as _utcnow

# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Declarative base for all ORM models.

    All tables in the platform inherit from this class.  The shared
    ``Base.metadata`` is the single source of truth for Alembic migrations.
    """


# ---------------------------------------------------------------------------
# Timestamp mixin
# ---------------------------------------------------------------------------


class TimestampMixin:
    """Adds ``created_at`` and ``updated_at`` audit columns to ORM models.

    Columns
    -------
    created_at : DateTime(timezone=True)
        UTC timestamp set once at row creation.  Never updated.
    updated_at : DateTime(timezone=True)
        UTC timestamp updated every time the row is modified.

    Usage
    -----
        class MyModel(Base, TimestampMixin):
            __tablename__ = "my_table"
            id: Mapped[str] = mapped_column(primary_key=True)
            # created_at and updated_at are provided by TimestampMixin
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="UTC timestamp when this row was first created.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="UTC timestamp of the last modification to this row.",
    )
