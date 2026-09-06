"""Tax code application service."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.pagination import Page, PageParams
from app.modules.journal.application.commands import ActorContext
from app.modules.taxes.application.schemas import TaxCodeCreate, TaxCodeRead, TaxCodeUpdate
from app.modules.taxes.infrastructure.models import TaxCode
from app.shared.domain.enums import AuditAction
from app.shared.domain.exceptions import CrossCompanyReferenceError
from app.shared.unit_of_work.unit_of_work import UnitOfWork


class TaxCodeService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def create(self, payload: TaxCodeCreate, actor: ActorContext) -> TaxCodeRead:
        if await self.uow.companies.get(payload.company_id) is None:
            raise NotFoundError(
                "Company not found.", details={"company_id": str(payload.company_id)}
            )
        if await self.uow.tax_codes.get_by_code(payload.company_id, payload.tax_code):
            raise ConflictError(
                "That tax code already exists for this company.",
                code="DUPLICATE_TAX_CODE",
                details={"tax_code": payload.tax_code},
            )

        await self._require_tax_account(payload.company_id, payload.tax_account_id)
        _validate_rate(payload.tax_rate)
        _validate_effective_range(payload.effective_from, payload.effective_to)

        tax = TaxCode(
            company_id=payload.company_id,
            tax_code=payload.tax_code,
            tax_name=payload.tax_name,
            tax_rate=payload.tax_rate,
            tax_account_id=payload.tax_account_id,
            effective_from=payload.effective_from,
            effective_to=payload.effective_to,
            is_active=payload.is_active,
        )
        self.uow.tax_codes.add(tax)
        await self.uow.flush()

        self.uow.audit.record(
            action=AuditAction.TAX_CODE_CREATED,
            entity_type="TaxCode",
            entity_id=tax.id,
            company_id=tax.company_id,
            user_id=actor.user_id,
            payload={
                "tax_code": tax.tax_code,
                "tax_rate": str(tax.tax_rate),
                "tax_account_id": str(tax.tax_account_id),
            },
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()
        return TaxCodeRead.model_validate(tax)

    async def get(self, company_id: UUID, tax_id: UUID) -> TaxCodeRead:
        return TaxCodeRead.model_validate(await self._require(company_id, tax_id))

    async def list(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> Page[TaxCodeRead]:
        rows, total = await self.uow.tax_codes.list_paginated(
            company_id, params, is_active=is_active
        )
        return Page.build([TaxCodeRead.model_validate(row) for row in rows], params, total=total)

    async def update(
        self, company_id: UUID, tax_id: UUID, payload: TaxCodeUpdate, actor: ActorContext
    ) -> TaxCodeRead:
        tax = await self._require(company_id, tax_id)
        changes = payload.model_dump(exclude_unset=True)

        if payload.tax_account_id is not None:
            await self._require_tax_account(company_id, payload.tax_account_id)
        if payload.tax_rate is not None:
            _validate_rate(payload.tax_rate)

        effective_from = payload.effective_from or tax.effective_from
        effective_to = payload.effective_to if "effective_to" in changes else tax.effective_to
        _validate_effective_range(effective_from, effective_to)

        for field, value in changes.items():
            setattr(tax, field, value)

        self.uow.audit.record(
            action=AuditAction.TAX_CODE_UPDATED,
            entity_type="TaxCode",
            entity_id=tax.id,
            company_id=tax.company_id,
            user_id=actor.user_id,
            payload={"changes": {k: str(v) for k, v in changes.items()}},
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()
        return TaxCodeRead.model_validate(tax)

    # ------------------------------------------------------------- internals
    async def _require(self, company_id: UUID, tax_id: UUID) -> TaxCode:
        tax = await self.uow.tax_codes.get_for_company(company_id, tax_id)
        if tax is None:
            raise NotFoundError(
                "Tax code not found.",
                code="TAX_CODE_NOT_FOUND",
                details={"tax_id": str(tax_id)},
            )
        return tax

    async def _require_tax_account(self, company_id: UUID, account_id: UUID) -> None:
        """The tax account must exist, belong to this company and be postable."""
        account = await self.uow.accounts.get(account_id)
        if account is None:
            raise NotFoundError(
                "Tax account not found.",
                code="ACCOUNT_NOT_FOUND",
                details={"tax_account_id": str(account_id)},
            )
        if account.company_id != company_id:
            raise CrossCompanyReferenceError(
                "Tax account belongs to a different company.",
                details={"tax_account_id": str(account_id)},
            )
        if not account.is_postable:
            raise ValidationError(
                "The tax account must be postable.",
                code="ACCOUNT_NOT_POSTABLE",
                details={"tax_account_id": str(account_id)},
            )


def _validate_rate(rate: Decimal) -> None:
    if rate < 0 or rate > 100:
        raise ValidationError(
            "Tax rate must be between 0 and 100.", details={"tax_rate": str(rate)}
        )


def _validate_effective_range(start: date, end: date | None) -> None:
    if end is not None and start > end:
        raise ValidationError(
            "effective_from must be on or before effective_to.",
            details={"effective_from": start.isoformat(), "effective_to": end.isoformat()},
        )
