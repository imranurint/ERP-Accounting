"""Fiscal period application service.

Every state change locks the period row first, so a close cannot race a
posting: the posting service holds the same lock for the length of its
transaction.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime
from uuid import UUID

from app.application.fiscal_periods.schemas import (
    FiscalPeriodBulkCreate,
    FiscalPeriodCreate,
    FiscalPeriodRead,
)
from app.application.journal.commands import ActorContext
from app.core.exceptions import NotFoundError
from app.core.pagination import Page, PageParams
from app.domain.accounting import rules
from app.domain.accounting.enums import AuditAction, FiscalPeriodStatus
from app.domain.accounting.events import FiscalPeriodStatusChanged
from app.domain.accounting.exceptions import FiscalPeriodOverlapError
from app.infrastructure.database.models.fiscal_period import FiscalPeriod
from app.infrastructure.database.unit_of_work import UnitOfWork

#: Which audit action corresponds to each target status.
_TRANSITION_AUDIT: dict[FiscalPeriodStatus, AuditAction] = {
    FiscalPeriodStatus.OPEN: AuditAction.PERIOD_OPENED,
    FiscalPeriodStatus.CLOSED: AuditAction.PERIOD_CLOSED,
    FiscalPeriodStatus.LOCKED: AuditAction.PERIOD_LOCKED,
    FiscalPeriodStatus.ADJUSTING: AuditAction.PERIOD_ADJUSTING,
}


class FiscalPeriodService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def create(self, payload: FiscalPeriodCreate, actor: ActorContext) -> FiscalPeriodRead:
        await self._require_company(payload.company_id)
        await self._reject_overlap(payload.company_id, payload.start_date, payload.end_date)

        period = FiscalPeriod(
            company_id=payload.company_id,
            fiscal_year=payload.fiscal_year,
            period_name=payload.period_name,
            start_date=payload.start_date,
            end_date=payload.end_date,
            status=FiscalPeriodStatus.OPEN,
            is_closed=False,
        )
        self.uow.fiscal_periods.add(period)
        await self.uow.flush()

        self._audit(period, AuditAction.PERIOD_CREATED, actor, {})
        await self.uow.commit()
        return FiscalPeriodRead.model_validate(period)

    async def create_year(
        self, payload: FiscalPeriodBulkCreate, actor: ActorContext
    ) -> list[FiscalPeriodRead]:
        """Create the twelve calendar months of a fiscal year."""
        await self._require_company(payload.company_id)
        created: list[FiscalPeriod] = []

        for month in range(1, 13):
            last_day = calendar.monthrange(payload.fiscal_year, month)[1]
            start = date(payload.fiscal_year, month, 1)
            end = date(payload.fiscal_year, month, last_day)
            await self._reject_overlap(payload.company_id, start, end)

            period = FiscalPeriod(
                company_id=payload.company_id,
                fiscal_year=payload.fiscal_year,
                period_name=payload.name_template.format(
                    month_name=calendar.month_name[month],
                    month=f"{month:02d}",
                    year=payload.fiscal_year,
                ),
                start_date=start,
                end_date=end,
                status=FiscalPeriodStatus.OPEN,
                is_closed=False,
            )
            self.uow.fiscal_periods.add(period)
            created.append(period)

        await self.uow.flush()
        for period in created:
            self._audit(period, AuditAction.PERIOD_CREATED, actor, {"bulk": True})
        await self.uow.commit()
        return [FiscalPeriodRead.model_validate(period) for period in created]

    async def get(self, company_id: UUID, period_id: UUID) -> FiscalPeriodRead:
        period = await self._require(company_id, period_id)
        return FiscalPeriodRead.model_validate(period)

    async def list(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        fiscal_year: int | None = None,
        status: FiscalPeriodStatus | None = None,
    ) -> Page[FiscalPeriodRead]:
        rows, total = await self.uow.fiscal_periods.list_paginated(
            company_id, params, fiscal_year=fiscal_year, status=status
        )
        return Page.build(
            [FiscalPeriodRead.model_validate(row) for row in rows], params, total=total
        )

    async def transition(
        self,
        company_id: UUID,
        period_id: UUID,
        target: FiscalPeriodStatus,
        actor: ActorContext,
        *,
        reason: str | None = None,
    ) -> FiscalPeriodRead:
        """Move a period to a new status, under the same lock posting uses."""
        async with self.uow:
            period = await self.uow.fiscal_periods.lock_for_update(company_id, period_id)
            if period is None:
                raise NotFoundError(
                    "Fiscal period not found.", details={"period_id": str(period_id)}
                )

            previous = period.status
            rules.validate_period_transition(previous, target)

            period.status = target
            period.is_closed = target in (
                FiscalPeriodStatus.CLOSED,
                FiscalPeriodStatus.LOCKED,
            )
            if target is FiscalPeriodStatus.CLOSED:
                period.closed_at = datetime.now(UTC)
                period.closed_by = actor.user_id
            elif target is FiscalPeriodStatus.OPEN:
                period.closed_at = None
                period.closed_by = None

            action = (
                AuditAction.PERIOD_REOPENED
                if target is FiscalPeriodStatus.OPEN and previous is FiscalPeriodStatus.CLOSED
                else _TRANSITION_AUDIT[target]
            )
            self._audit(
                period,
                action,
                actor,
                {"previous_status": str(previous), "new_status": str(target), "reason": reason},
            )
            self.uow.outbox.enqueue(
                FiscalPeriodStatusChanged(
                    aggregate_id=period.id,
                    company_id=period.company_id,
                    period_name=period.period_name,
                    previous_status=str(previous),
                    new_status=str(target),
                    changed_by=actor.user_id,
                )
            )
            await self.uow.commit()

        return FiscalPeriodRead.model_validate(period)

    # ------------------------------------------------------------- internals
    async def _require(self, company_id: UUID, period_id: UUID) -> FiscalPeriod:
        period = await self.uow.fiscal_periods.get_for_company(company_id, period_id)
        if period is None:
            raise NotFoundError("Fiscal period not found.", details={"period_id": str(period_id)})
        return period

    async def _require_company(self, company_id: UUID) -> None:
        if await self.uow.companies.get(company_id) is None:
            raise NotFoundError("Company not found.", details={"company_id": str(company_id)})

    async def _reject_overlap(self, company_id: UUID, start_date: date, end_date: date) -> None:
        """Application-level guard; the GiST exclusion constraint is the backstop."""
        overlapping = await self.uow.fiscal_periods.find_overlapping(
            company_id, start_date, end_date
        )
        if overlapping:
            raise FiscalPeriodOverlapError(
                details={
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "conflicts": [
                        {
                            "period_id": str(period.id),
                            "period_name": period.period_name,
                            "start_date": period.start_date.isoformat(),
                            "end_date": period.end_date.isoformat(),
                        }
                        for period in overlapping
                    ],
                }
            )

    def _audit(
        self,
        period: FiscalPeriod,
        action: AuditAction,
        actor: ActorContext,
        extra: dict[str, object],
    ) -> None:
        self.uow.audit.record(
            action=action,
            entity_type="FiscalPeriod",
            entity_id=period.id,
            company_id=period.company_id,
            user_id=actor.user_id,
            payload={
                "period_name": period.period_name,
                "fiscal_year": period.fiscal_year,
                "start_date": period.start_date.isoformat(),
                "end_date": period.end_date.isoformat(),
                "status": str(period.status),
                **extra,
            },
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
