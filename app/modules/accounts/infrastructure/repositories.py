"""Chart of accounts repository."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import exists, select

from app.core.pagination import PageParams
from app.modules.accounts.infrastructure.models import Account
from app.modules.journal.infrastructure.models import JournalEntry, JournalItem
from app.shared.database.repository import BaseRepository
from app.shared.domain.enums import AccountType


class AccountRepository(BaseRepository[Account]):
    model = Account

    async def get_for_company(self, company_id: UUID, account_id: UUID) -> Account | None:
        stmt = select(Account).where(Account.id == account_id, Account.company_id == company_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_number(self, company_id: UUID, account_number: str) -> Account | None:
        stmt = select(Account).where(
            Account.company_id == company_id,
            Account.account_number == account_number,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_many_by_id(
        self, company_id: UUID, account_ids: Sequence[UUID]
    ) -> dict[UUID, Account]:
        """Batch fetch by id — one query per posting, never one per line."""
        if not account_ids:
            return {}
        stmt = select(Account).where(
            Account.company_id == company_id, Account.id.in_(set(account_ids))
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.id: row for row in rows}

    async def get_many_by_number(
        self, company_id: UUID, account_numbers: Sequence[str]
    ) -> dict[str, Account]:
        if not account_numbers:
            return {}
        stmt = select(Account).where(
            Account.company_id == company_id,
            Account.account_number.in_(set(account_numbers)),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.account_number: row for row in rows}

    async def list_paginated(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        account_type: AccountType | None = None,
        is_active: bool | None = None,
        is_postable: bool | None = None,
        search: str | None = None,
    ) -> tuple[Sequence[Account], int]:
        stmt = select(Account).where(Account.company_id == company_id)
        if account_type is not None:
            stmt = stmt.where(Account.account_type == account_type)
        if is_active is not None:
            stmt = stmt.where(Account.is_active == is_active)
        if is_postable is not None:
            stmt = stmt.where(Account.is_postable == is_postable)
        if search:
            pattern = f"%{search.lower()}%"
            stmt = stmt.where(
                Account.account_name.ilike(pattern) | Account.account_number.ilike(pattern)
            )
        total = await self.count(stmt)
        stmt = stmt.order_by(Account.account_number).offset(params.offset).limit(params.limit)
        rows = (await self.session.execute(stmt)).scalars().all()
        return rows, total

    async def list_for_company(self, company_id: UUID) -> Sequence[Account]:
        stmt = (
            select(Account).where(Account.company_id == company_id).order_by(Account.account_number)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def has_posted_transactions(self, account_id: UUID) -> bool:
        """True when the account appears on any entry that touched the ledger.

        Guards deletion: an account with accounting history may only be
        deactivated.
        """
        stmt = select(
            exists().where(
                JournalItem.account_id == account_id,
                JournalItem.entry_id == JournalEntry.id,
                JournalEntry.status.in_(("POSTED", "REVERSED")),
            )
        )
        return bool(await self.session.scalar(stmt))

    async def has_any_transactions(self, account_id: UUID) -> bool:
        stmt = select(exists().where(JournalItem.account_id == account_id))
        return bool(await self.session.scalar(stmt))
