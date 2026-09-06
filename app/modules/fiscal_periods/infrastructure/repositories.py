"""Fiscal period repository, including the posting lock."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import select

from app.core.pagination import PageParams
from app.modules.fiscal_periods.infrastructure.models import FiscalPeriod
from app.shared.database.repository import BaseRepository
from app.shared.domain.enums import FiscalPeriodStatus


class FiscalPeriodRepository(BaseRepository[FiscalPeriod]):
    model = FiscalPeriod

    async def get_for_company(self, company_id: UUID, period_id: UUID) -> FiscalPeriod | None:
        stmt = select(FiscalPeriod).where(
            FiscalPeriod.id == period_id, FiscalPeriod.company_id == company_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_for_update(self, company_id: UUID, period_id: UUID) -> FiscalPeriod | None:
        """Take a row lock on the period for the duration of the transaction.

        This is what makes "close the period" and "post into the period"
        mutually exclusive: a close cannot slip between our status check and
        our insert, and two concurrent postings serialise only on this one
        row rather than on the whole ledger.
        """
        stmt = (
            select(FiscalPeriod)
            .where(FiscalPeriod.id == period_id, FiscalPeriod.company_id == company_id)
            .with_for_update()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_for_date(self, company_id: UUID, value: date) -> FiscalPeriod | None:
        stmt = select(FiscalPeriod).where(
            FiscalPeriod.company_id == company_id,
            FiscalPeriod.start_date <= value,
            FiscalPeriod.end_date >= value,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_overlapping(
        self,
        company_id: UUID,
        start_date: date,
        end_date: date,
        *,
        exclude_id: UUID | None = None,
    ) -> Sequence[FiscalPeriod]:
        stmt = select(FiscalPeriod).where(
            FiscalPeriod.company_id == company_id,
            FiscalPeriod.start_date <= end_date,
            FiscalPeriod.end_date >= start_date,
        )
        if exclude_id is not None:
            stmt = stmt.where(FiscalPeriod.id != exclude_id)
        return (await self.session.execute(stmt)).scalars().all()

    async def list_paginated(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        fiscal_year: int | None = None,
        status: FiscalPeriodStatus | None = None,
    ) -> tuple[Sequence[FiscalPeriod], int]:
        stmt = select(FiscalPeriod).where(FiscalPeriod.company_id == company_id)
        if fiscal_year is not None:
            stmt = stmt.where(FiscalPeriod.fiscal_year == fiscal_year)
        if status is not None:
            stmt = stmt.where(FiscalPeriod.status == status)
        total = await self.count(stmt)
        stmt = stmt.order_by(FiscalPeriod.start_date).offset(params.offset).limit(params.limit)
        rows = (await self.session.execute(stmt)).scalars().all()
        return rows, total

    async def list_in_range(
        self, company_id: UUID, start_date: date, end_date: date
    ) -> Sequence[FiscalPeriod]:
        stmt = (
            select(FiscalPeriod)
            .where(
                FiscalPeriod.company_id == company_id,
                FiscalPeriod.start_date <= end_date,
                FiscalPeriod.end_date >= start_date,
            )
            .order_by(FiscalPeriod.start_date)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def list_before(self, company_id: UUID, before: date) -> Sequence[FiscalPeriod]:
        """Periods that end strictly before `before` — used for opening balances."""
        stmt = (
            select(FiscalPeriod)
            .where(FiscalPeriod.company_id == company_id, FiscalPeriod.end_date < before)
            .order_by(FiscalPeriod.start_date)
        )
        return (await self.session.execute(stmt)).scalars().all()
