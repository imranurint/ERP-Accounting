"""Reporting queries.

Reports read from `journal_entries` + `journal_items` — the source of truth —
rather than from the `ledger_balances` projection. That guarantees a trial
balance always ties even if the projection were somehow stale, and it makes
the projection's correctness independently verifiable (see
`GET /api/v1/reports/account-balances`, which reads the projection instead).

Every query aggregates in PostgreSQL and returns one row per account. There is
no per-account query anywhere in this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import ZERO
from app.domain.accounting.enums import AccountType, JournalStatus, NormalBalance
from app.infrastructure.database.models.account import Account
from app.infrastructure.database.models.journal import JournalEntry, JournalItem

#: Statuses whose lines carry accounting weight. A REVERSED entry keeps its
#: original impact; the reversing entry cancels it.
LEDGER_STATUSES = (JournalStatus.POSTED, JournalStatus.REVERSED)


@dataclass(frozen=True, slots=True)
class AccountActivityRow:
    account_id: UUID
    account_number: str
    account_name: str
    account_type: AccountType
    normal_balance: NormalBalance
    currency_code: str
    total_debits: Decimal
    total_credits: Decimal

    @property
    def net(self) -> Decimal:
        movement = self.total_debits - self.total_credits
        return movement if self.normal_balance is NormalBalance.DEBIT else -movement


@dataclass(frozen=True, slots=True)
class GeneralLedgerRow:
    entry_id: UUID
    entry_number: str | None
    entry_date: date
    reference: str | None
    source_type: str | None
    source_document: str | None
    line_number: int
    memo: str | None
    debit: Decimal
    credit: Decimal
    currency_code: str


class ReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- shared building block ---------------------------------------------
    def _activity_stmt(
        self,
        company_id: UUID,
        *,
        start_date: date | None,
        end_date: date | None,
        currency_code: str | None,
        account_ids: Sequence[UUID] | None = None,
        account_types: Sequence[AccountType] | None = None,
        period_id: UUID | None = None,
    ) -> Select[tuple[UUID, str, str, AccountType, NormalBalance, str, Decimal, Decimal]]:
        debits = func.coalesce(func.sum(JournalItem.debit), ZERO).label("total_debits")
        credits = func.coalesce(func.sum(JournalItem.credit), ZERO).label("total_credits")

        stmt = (
            select(
                Account.id,
                Account.account_number,
                Account.account_name,
                Account.account_type,
                Account.normal_balance,
                JournalItem.currency_code,
                debits,
                credits,
            )
            .join(JournalItem, JournalItem.account_id == Account.id)
            .join(JournalEntry, JournalEntry.id == JournalItem.entry_id)
            .where(
                JournalEntry.company_id == company_id,
                JournalEntry.status.in_(LEDGER_STATUSES),
            )
            .group_by(
                Account.id,
                Account.account_number,
                Account.account_name,
                Account.account_type,
                Account.normal_balance,
                JournalItem.currency_code,
            )
            .order_by(Account.account_number, JournalItem.currency_code)
        )

        if period_id is not None:
            stmt = stmt.where(JournalEntry.period_id == period_id)
        if start_date is not None:
            stmt = stmt.where(JournalEntry.entry_date >= start_date)
        if end_date is not None:
            stmt = stmt.where(JournalEntry.entry_date <= end_date)
        if currency_code:
            stmt = stmt.where(JournalItem.currency_code == currency_code.upper())
        if account_ids:
            stmt = stmt.where(Account.id.in_(set(account_ids)))
        if account_types:
            stmt = stmt.where(Account.account_type.in_(tuple(account_types)))
        return stmt

    async def account_activity(
        self,
        company_id: UUID,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        currency_code: str | None = None,
        account_ids: Sequence[UUID] | None = None,
        account_types: Sequence[AccountType] | None = None,
        period_id: UUID | None = None,
    ) -> list[AccountActivityRow]:
        """Debit/credit totals per account for a window. One query."""
        stmt = self._activity_stmt(
            company_id,
            start_date=start_date,
            end_date=end_date,
            currency_code=currency_code,
            account_ids=account_ids,
            account_types=account_types,
            period_id=period_id,
        )
        result = await self.session.execute(stmt)
        return [
            AccountActivityRow(
                account_id=row[0],
                account_number=row[1],
                account_name=row[2],
                account_type=row[3],
                normal_balance=row[4],
                currency_code=row[5],
                total_debits=row[6],
                total_credits=row[7],
            )
            for row in result.all()
        ]

    async def opening_balances(
        self,
        company_id: UUID,
        *,
        before: date,
        currency_code: str | None = None,
        account_ids: Sequence[UUID] | None = None,
        account_types: Sequence[AccountType] | None = None,
    ) -> dict[tuple[UUID, str], Decimal]:
        """Cumulative net balance per (account, currency) strictly before a date.

        This is how a report derives an opening balance: by cumulating history,
        never by trusting a stored figure.
        """
        rows = await self.account_activity(
            company_id,
            start_date=None,
            end_date=before - timedelta(days=1),  # `before` itself is excluded
            currency_code=currency_code,
            account_ids=account_ids,
            account_types=account_types,
        )
        return {(row.account_id, row.currency_code): row.net for row in rows}

    async def general_ledger_lines(
        self,
        company_id: UUID,
        account_id: UUID,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        currency_code: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[GeneralLedgerRow]:
        """Posted activity for one account, in accounting order."""
        stmt = (
            select(
                JournalEntry.id,
                JournalEntry.entry_number,
                JournalEntry.entry_date,
                JournalEntry.reference,
                JournalEntry.source_type,
                JournalEntry.source_document,
                JournalItem.line_number,
                JournalItem.memo,
                JournalItem.debit,
                JournalItem.credit,
                JournalItem.currency_code,
            )
            .join(JournalItem, JournalItem.entry_id == JournalEntry.id)
            .where(
                JournalEntry.company_id == company_id,
                JournalItem.account_id == account_id,
                JournalEntry.status.in_(LEDGER_STATUSES),
            )
            .order_by(
                JournalEntry.entry_date,
                JournalEntry.entry_number,
                JournalItem.line_number,
            )
            .limit(limit)
            .offset(offset)
        )
        if start_date is not None:
            stmt = stmt.where(JournalEntry.entry_date >= start_date)
        if end_date is not None:
            stmt = stmt.where(JournalEntry.entry_date <= end_date)
        if currency_code:
            stmt = stmt.where(JournalItem.currency_code == currency_code.upper())

        result = await self.session.execute(stmt)
        return [
            GeneralLedgerRow(
                entry_id=row[0],
                entry_number=row[1],
                entry_date=row[2],
                reference=row[3],
                source_type=row[4],
                source_document=row[5],
                line_number=row[6],
                memo=row[7],
                debit=row[8],
                credit=row[9],
                currency_code=row[10],
            )
            for row in result.all()
        ]

    async def count_general_ledger_lines(
        self,
        company_id: UUID,
        account_id: UUID,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        currency_code: str | None = None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(JournalItem)
            .join(JournalEntry, JournalEntry.id == JournalItem.entry_id)
            .where(
                JournalEntry.company_id == company_id,
                JournalItem.account_id == account_id,
                JournalEntry.status.in_(LEDGER_STATUSES),
            )
        )
        if start_date is not None:
            stmt = stmt.where(JournalEntry.entry_date >= start_date)
        if end_date is not None:
            stmt = stmt.where(JournalEntry.entry_date <= end_date)
        if currency_code:
            stmt = stmt.where(JournalItem.currency_code == currency_code.upper())
        return int(await self.session.scalar(stmt) or 0)

    async def totals(
        self,
        company_id: UUID,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        currency_code: str | None = None,
    ) -> tuple[Decimal, Decimal]:
        """Grand totals of posted debits and credits — the ultimate sanity check."""
        stmt = (
            select(
                func.coalesce(func.sum(JournalItem.debit), ZERO),
                func.coalesce(func.sum(JournalItem.credit), ZERO),
            )
            .join(JournalEntry, JournalEntry.id == JournalItem.entry_id)
            .where(
                JournalEntry.company_id == company_id,
                JournalEntry.status.in_(LEDGER_STATUSES),
            )
        )
        if start_date is not None:
            stmt = stmt.where(JournalEntry.entry_date >= start_date)
        if end_date is not None:
            stmt = stmt.where(JournalEntry.entry_date <= end_date)
        if currency_code:
            stmt = stmt.where(JournalItem.currency_code == currency_code.upper())
        row = (await self.session.execute(stmt)).one()
        return row[0], row[1]
