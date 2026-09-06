"""Company and currency repositories."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select

from app.core.pagination import PageParams
from app.modules.organization.infrastructure.models import Company, Currency
from app.shared.database.repository import BaseRepository


class CompanyRepository(BaseRepository[Company]):
    model = Company

    async def get_by_name(self, name: str) -> Company | None:
        stmt = select(Company).where(Company.name == name)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_paginated(
        self, params: PageParams, *, is_active: bool | None = None
    ) -> tuple[Sequence[Company], int]:
        stmt = select(Company)
        if is_active is not None:
            stmt = stmt.where(Company.is_active == is_active)
        total = await self.count(stmt)
        stmt = stmt.order_by(Company.name).offset(params.offset).limit(params.limit)
        rows = (await self.session.execute(stmt)).scalars().all()
        return rows, total

    async def get_active(self, company_id: UUID) -> Company | None:
        stmt = select(Company).where(Company.id == company_id, Company.is_active.is_(True))
        return (await self.session.execute(stmt)).scalar_one_or_none()


class CurrencyRepository(BaseRepository[Currency]):
    model = Currency

    async def get_by_code(self, currency_code: str) -> Currency | None:
        return await self.session.get(Currency, currency_code.upper())

    async def get_many(self, codes: Sequence[str]) -> dict[str, Currency]:
        """Batch fetch — keeps validation of multi-currency payloads O(1) queries."""
        if not codes:
            return {}
        normalized = {code.upper() for code in codes}
        stmt = select(Currency).where(Currency.currency_code.in_(normalized))
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.currency_code: row for row in rows}

    async def list_all_ordered(self, *, is_active: bool | None = None) -> Sequence[Currency]:
        stmt = select(Currency)
        if is_active is not None:
            stmt = stmt.where(Currency.is_active == is_active)
        stmt = stmt.order_by(Currency.currency_code)
        return (await self.session.execute(stmt)).scalars().all()
