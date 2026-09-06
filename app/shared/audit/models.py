"""Append-only audit log.

Rows are inserted, never updated or deleted. Sensitive values never reach the
`payload` column — the writer whitelists what it records.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.database.base import Base, UUIDPrimaryKeyMixin
from app.shared.database.types import CreatedAtMixin


class AuditLog(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "audit_logs"

    company_id: Mapped[UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(500))
    request_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_audit_logs_company_created", "company_id", "created_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_user", "user_id"),
    )
