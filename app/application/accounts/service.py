"""Chart of accounts application service."""

from __future__ import annotations

from uuid import UUID

from app.application.accounts.schemas import AccountCreate, AccountRead, AccountUpdate
from app.application.journal.commands import ActorContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.pagination import Page, PageParams
from app.domain.accounting.enums import AccountType, AuditAction
from app.domain.accounting.exceptions import (
    CrossCompanyReferenceError,
    CurrencyInactiveError,
)
from app.infrastructure.database.models.account import Account
from app.infrastructure.database.unit_of_work import UnitOfWork


class AccountService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def create(self, payload: AccountCreate, actor: ActorContext) -> AccountRead:
        company = await self.uow.companies.get(payload.company_id)
        if company is None:
            raise NotFoundError(
                "Company not found.", details={"company_id": str(payload.company_id)}
            )

        existing = await self.uow.accounts.get_by_number(payload.company_id, payload.account_number)
        if existing is not None:
            raise ConflictError(
                "That account number already exists for this company.",
                code="DUPLICATE_ACCOUNT_NUMBER",
                details={"account_number": payload.account_number},
            )

        if payload.currency_code:
            currency = await self.uow.currencies.get_by_code(payload.currency_code)
            if currency is None:
                raise NotFoundError(
                    "Currency not found.",
                    details={"currency_code": payload.currency_code},
                )
            if not currency.is_active:
                raise CurrencyInactiveError(details={"currency_code": payload.currency_code})

        parent = await self._resolve_parent(payload.company_id, payload.parent_account_id)

        assert payload.normal_balance is not None  # set by the schema validator
        account = Account(
            company_id=payload.company_id,
            account_number=payload.account_number,
            account_name=payload.account_name,
            account_type=payload.account_type,
            normal_balance=payload.normal_balance,
            currency_code=payload.currency_code,
            parent_account_id=parent.id if parent else None,
            is_active=payload.is_active,
            is_postable=payload.is_postable,
            description=payload.description,
        )
        self.uow.accounts.add(account)
        await self.uow.flush()

        # A parent that has children must not itself receive postings, or the
        # roll-up would double count.
        if parent is not None and parent.is_postable:
            parent.is_postable = False

        self.uow.audit.record(
            action=AuditAction.ACCOUNT_CREATED,
            entity_type="Account",
            entity_id=account.id,
            company_id=account.company_id,
            user_id=actor.user_id,
            payload={
                "account_number": account.account_number,
                "account_name": account.account_name,
                "account_type": str(account.account_type),
                "normal_balance": str(account.normal_balance),
            },
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()
        return AccountRead.model_validate(account)

    async def get(self, company_id: UUID, account_id: UUID) -> AccountRead:
        account = await self._require(company_id, account_id)
        return AccountRead.model_validate(account)

    async def list(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        account_type: AccountType | None = None,
        is_active: bool | None = None,
        is_postable: bool | None = None,
        search: str | None = None,
    ) -> Page[AccountRead]:
        rows, total = await self.uow.accounts.list_paginated(
            company_id,
            params,
            account_type=account_type,
            is_active=is_active,
            is_postable=is_postable,
            search=search,
        )
        return Page.build([AccountRead.model_validate(row) for row in rows], params, total=total)

    async def update(
        self, company_id: UUID, account_id: UUID, payload: AccountUpdate, actor: ActorContext
    ) -> AccountRead:
        account = await self._require(company_id, account_id)
        changes = payload.model_dump(exclude_unset=True)

        if "parent_account_id" in changes:
            parent = await self._resolve_parent(company_id, changes["parent_account_id"])
            if parent is not None and parent.id == account.id:
                raise ValidationError("An account cannot be its own parent.")
            account.parent_account_id = parent.id if parent else None
            changes.pop("parent_account_id")

        if changes.get("is_postable") is True and await self._has_children(account.id):
            raise ConflictError(
                "A parent account cannot be made postable.",
                code="PARENT_ACCOUNT_NOT_POSTABLE",
                details={"account_id": str(account.id)},
            )

        for field, value in changes.items():
            setattr(account, field, value)

        self.uow.audit.record(
            action=AuditAction.ACCOUNT_UPDATED,
            entity_type="Account",
            entity_id=account.id,
            company_id=account.company_id,
            user_id=actor.user_id,
            payload={"changes": {k: str(v) for k, v in changes.items()}},
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()
        return AccountRead.model_validate(account)

    async def set_active(
        self, company_id: UUID, account_id: UUID, *, active: bool, actor: ActorContext
    ) -> AccountRead:
        """Activate or deactivate. Deactivation is the only 'delete' we allow."""
        account = await self._require(company_id, account_id)
        account.is_active = active

        self.uow.audit.record(
            action=(AuditAction.ACCOUNT_ACTIVATED if active else AuditAction.ACCOUNT_DEACTIVATED),
            entity_type="Account",
            entity_id=account.id,
            company_id=account.company_id,
            user_id=actor.user_id,
            payload={
                "account_number": account.account_number,
                "is_active": active,
            },
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()
        return AccountRead.model_validate(account)

    # ------------------------------------------------------------- internals
    async def _require(self, company_id: UUID, account_id: UUID) -> Account:
        account = await self.uow.accounts.get_for_company(company_id, account_id)
        if account is None:
            raise NotFoundError(
                "Account not found.",
                code="ACCOUNT_NOT_FOUND",
                details={"account_id": str(account_id)},
            )
        return account

    async def _resolve_parent(self, company_id: UUID, parent_id: UUID | None) -> Account | None:
        if parent_id is None:
            return None
        parent = await self.uow.accounts.get(parent_id)
        if parent is None:
            raise NotFoundError(
                "Parent account not found.", details={"parent_account_id": str(parent_id)}
            )
        if parent.company_id != company_id:
            raise CrossCompanyReferenceError(
                "Parent account belongs to a different company.",
                details={"parent_account_id": str(parent_id)},
            )
        return parent

    async def _has_children(self, account_id: UUID) -> bool:
        return await self.uow.accounts.exists(Account.parent_account_id == account_id)
