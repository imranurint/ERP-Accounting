"""Company application service."""

from __future__ import annotations

from uuid import UUID

from app.application.companies.schemas import CompanyCreate, CompanyRead, CompanyUpdate
from app.application.journal.commands import ActorContext
from app.core.exceptions import NotFoundError
from app.core.pagination import Page, PageParams
from app.domain.accounting.enums import AuditAction
from app.domain.accounting.exceptions import CurrencyInactiveError
from app.infrastructure.database.models.organization import Company
from app.infrastructure.database.unit_of_work import UnitOfWork


class CompanyService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def create(self, payload: CompanyCreate, actor: ActorContext) -> CompanyRead:
        currency = await self.uow.currencies.get_by_code(payload.base_currency_code)
        if currency is None:
            raise NotFoundError(
                "Base currency not found.",
                details={"currency_code": payload.base_currency_code},
            )
        if not currency.is_active:
            raise CurrencyInactiveError(details={"currency_code": payload.base_currency_code})

        company = Company(
            name=payload.name,
            legal_name=payload.legal_name,
            base_currency_code=currency.currency_code,
            timezone=payload.timezone,
            is_active=True,
        )
        self.uow.companies.add(company)
        await self.uow.flush()

        self.uow.audit.record(
            action=AuditAction.COMPANY_CREATED,
            entity_type="Company",
            entity_id=company.id,
            company_id=company.id,
            user_id=actor.user_id,
            payload={
                "name": company.name,
                "base_currency_code": company.base_currency_code,
            },
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()
        return CompanyRead.model_validate(company)

    async def get(self, company_id: UUID) -> CompanyRead:
        company = await self.uow.companies.get(company_id)
        if company is None:
            raise NotFoundError("Company not found.", details={"company_id": str(company_id)})
        return CompanyRead.model_validate(company)

    async def list(self, params: PageParams, *, is_active: bool | None = None) -> Page[CompanyRead]:
        rows, total = await self.uow.companies.list_paginated(params, is_active=is_active)
        return Page.build([CompanyRead.model_validate(row) for row in rows], params, total=total)

    async def update(
        self, company_id: UUID, payload: CompanyUpdate, actor: ActorContext
    ) -> CompanyRead:
        company = await self.uow.companies.get(company_id)
        if company is None:
            raise NotFoundError("Company not found.", details={"company_id": str(company_id)})

        changes = payload.model_dump(exclude_none=True)
        for field, value in changes.items():
            setattr(company, field, value)

        self.uow.audit.record(
            action=AuditAction.COMPANY_UPDATED,
            entity_type="Company",
            entity_id=company.id,
            company_id=company.id,
            user_id=actor.user_id,
            payload={"changes": changes},
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()
        return CompanyRead.model_validate(company)
