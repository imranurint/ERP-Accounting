"""Tax code repository."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select

from app.core.pagination import PageParams
from app.infrastructure.database.models.tax_code import TaxCode
from app.infrastructure.database.repositories.base import BaseRepository


class TaxCodeRepository(BaseRepository[TaxCode]):
    model = TaxCode

    async def get_for_company(self, company_id: UUID, tax_id: UUID) -> TaxCode | None:
        stmt = select(TaxCode).where(TaxCode.id == tax_id, TaxCode.company_id == company_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_code(self, company_id: UUID, tax_code: str) -> TaxCode | None:
        stmt = select(TaxCode).where(TaxCode.company_id == company_id, TaxCode.tax_code == tax_code)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_many_by_id(
        self, company_id: UUID, tax_ids: Sequence[UUID]
    ) -> dict[UUID, TaxCode]:
        if not tax_ids:
            return {}
        stmt = select(TaxCode).where(TaxCode.company_id == company_id, TaxCode.id.in_(set(tax_ids)))
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.id: row for row in rows}

    async def get_many_by_code(self, company_id: UUID, codes: Sequence[str]) -> dict[str, TaxCode]:
        if not codes:
            return {}
        stmt = select(TaxCode).where(
            TaxCode.company_id == company_id, TaxCode.tax_code.in_(set(codes))
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.tax_code: row for row in rows}

    async def list_paginated(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> tuple[Sequence[TaxCode], int]:
        stmt = select(TaxCode).where(TaxCode.company_id == company_id)
        if is_active is not None:
            stmt = stmt.where(TaxCode.is_active == is_active)
        total = await self.count(stmt)
        stmt = stmt.order_by(TaxCode.tax_code).offset(params.offset).limit(params.limit)
        rows = (await self.session.execute(stmt)).scalars().all()
        return rows, total
