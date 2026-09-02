"""Audit log repository — append only."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import PageParams
from app.domain.accounting.enums import AuditAction
from app.infrastructure.database.models.audit import AuditLog


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def record(
        self,
        *,
        action: AuditAction,
        entity_type: str,
        entity_id: str | UUID | None = None,
        company_id: UUID | None = None,
        user_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
    ) -> AuditLog:
        """Stage an audit row. Written when the surrounding transaction commits.

        Callers pass only whitelisted, non-sensitive fields in `payload`.
        """
        entry = AuditLog(
            company_id=company_id,
            user_id=user_id,
            action=action.value,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            payload=payload or {},
            ip_address=ip_address,
            user_agent=user_agent[:500] if user_agent else None,
            request_id=request_id,
        )
        self.session.add(entry)
        return entry

    async def list_paginated(
        self,
        params: PageParams,
        *,
        company_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        action: str | None = None,
        user_id: UUID | None = None,
        since: datetime | None = None,
    ) -> Sequence[AuditLog]:
        stmt = select(AuditLog)
        if company_id is not None:
            stmt = stmt.where(AuditLog.company_id == company_id)
        if entity_type is not None:
            stmt = stmt.where(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(AuditLog.entity_id == entity_id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if user_id is not None:
            stmt = stmt.where(AuditLog.user_id == user_id)
        if since is not None:
            stmt = stmt.where(AuditLog.created_at >= since)
        stmt = stmt.order_by(AuditLog.created_at.desc()).offset(params.offset).limit(params.limit)
        return (await self.session.execute(stmt)).scalars().all()
