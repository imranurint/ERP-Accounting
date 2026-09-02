"""Transactional outbox table.

An event row is written in the *same* transaction as the accounting change it
describes, so either both land or neither does. Publishing to an external bus
happens later, outside the accounting transaction, and is deliberately not the
GL's problem: a broker outage must never block a journal posting.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Enum, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.accounting.enums import OutboxStatus
from app.infrastructure.database.base import Base, UUIDPrimaryKeyMixin
from app.infrastructure.database.models.mixins import CreatedAtMixin


class OutboxEvent(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "outbox_events"

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(nullable=False)
    company_id: Mapped[UUID | None] = mapped_column()
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(
            OutboxStatus,
            name="outbox_status",
            native_enum=False,
            length=16,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=OutboxStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # The relay only ever scans PENDING rows; keep that index tiny.
        Index(
            "ix_outbox_events_pending",
            "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
    )
