"""Journal and ledger repositories."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.pagination import PageParams
from app.modules.journal.infrastructure.models import JournalEntry, JournalItem, LedgerBalance
from app.shared.database.repository import BaseRepository
from app.shared.domain.entities import LedgerMovement
from app.shared.domain.enums import JournalStatus, NormalBalance


class JournalRepository(BaseRepository[JournalEntry]):
    model = JournalEntry

    async def get_with_items(self, company_id: UUID, entry_id: UUID) -> JournalEntry | None:
        stmt = (
            select(JournalEntry)
            .where(JournalEntry.id == entry_id, JournalEntry.company_id == company_id)
            .options(selectinload(JournalEntry.items))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_with_items(self, company_id: UUID, entry_id: UUID) -> JournalEntry | None:
        """Lock the header row before a state transition.

        `of=JournalEntry` keeps the lock on the header only; the eagerly
        loaded items are read in a second statement and do not need locking
        because a posted entry's lines never change.
        """
        stmt = (
            select(JournalEntry)
            .where(JournalEntry.id == entry_id, JournalEntry.company_id == company_id)
            .with_for_update(of=JournalEntry)
        )
        entry = (await self.session.execute(stmt)).scalar_one_or_none()
        if entry is not None:
            await self.session.refresh(entry, attribute_names=["items"])
        return entry

    async def get_by_idempotency_key(
        self, company_id: UUID, idempotency_key: str
    ) -> JournalEntry | None:
        stmt = (
            select(JournalEntry)
            .where(
                JournalEntry.company_id == company_id,
                JournalEntry.idempotency_key == idempotency_key,
            )
            .options(selectinload(JournalEntry.items))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_number(self, company_id: UUID, entry_number: str) -> JournalEntry | None:
        stmt = (
            select(JournalEntry)
            .where(
                JournalEntry.company_id == company_id,
                JournalEntry.entry_number == entry_number,
            )
            .options(selectinload(JournalEntry.items))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_paginated(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        status: JournalStatus | None = None,
        period_id: UUID | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        source_type: str | None = None,
        source_document: str | None = None,
        reference: str | None = None,
        currency_code: str | None = None,
        include_items: bool = False,
    ) -> tuple[Sequence[JournalEntry], int]:
        stmt = select(JournalEntry).where(JournalEntry.company_id == company_id)
        if status is not None:
            stmt = stmt.where(JournalEntry.status == status)
        if period_id is not None:
            stmt = stmt.where(JournalEntry.period_id == period_id)
        if start_date is not None:
            stmt = stmt.where(JournalEntry.entry_date >= start_date)
        if end_date is not None:
            stmt = stmt.where(JournalEntry.entry_date <= end_date)
        if source_type:
            stmt = stmt.where(JournalEntry.source_type == source_type)
        if source_document:
            stmt = stmt.where(JournalEntry.source_document == source_document)
        if reference:
            stmt = stmt.where(JournalEntry.reference.ilike(f"%{reference}%"))
        if currency_code:
            stmt = stmt.where(JournalEntry.currency_code == currency_code.upper())

        total = await self.count(stmt)

        # `selectinload` (not a join) keeps the page size honest and avoids
        # the row multiplication a join against items would cause.
        if include_items:
            stmt = stmt.options(selectinload(JournalEntry.items))

        cursor = params.cursor_uuid()
        if cursor is not None:
            stmt = stmt.where(JournalEntry.id > cursor).order_by(JournalEntry.id)
        else:
            stmt = stmt.order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc()).offset(
                params.offset
            )

        rows = (await self.session.execute(stmt.limit(params.limit))).scalars().all()
        return rows, total

    async def list_posted_for_company(
        self, company_id: UUID, *, batch_size: int = 1000, after_id: UUID | None = None
    ) -> Sequence[JournalEntry]:
        """Ordered batch of ledger-affecting entries — used by the rebuild."""
        stmt = (
            select(JournalEntry)
            .where(
                JournalEntry.company_id == company_id,
                JournalEntry.status.in_((JournalStatus.POSTED, JournalStatus.REVERSED)),
            )
            .options(selectinload(JournalEntry.items))
            .order_by(JournalEntry.id)
            .limit(batch_size)
        )
        if after_id is not None:
            stmt = stmt.where(JournalEntry.id > after_id)
        return (await self.session.execute(stmt)).scalars().all()

    async def delete_items(self, entry_id: UUID) -> None:
        """Remove a draft's lines before rewriting them on edit."""
        entry = await self.session.get(JournalEntry, entry_id)
        if entry is not None:
            await self.session.refresh(entry, attribute_names=["items"])
            for item in list(entry.items):
                await self.session.delete(item)

    def add_items(self, items: Sequence[JournalItem]) -> None:
        self.session.add_all(list(items))


# ----------------------------------------------------------- ledger balances
_CONFLICT_COLUMNS = ["company_id", "account_id", "period_id", "currency_code"]


class LedgerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def apply_movements(self, movements: Sequence[LedgerMovement]) -> None:
        """Fold a posting's movements into the projection atomically.

        Movements are grouped by normal balance so the whole posting costs at
        most two statements, each a multi-row `INSERT ... ON CONFLICT DO
        UPDATE`. `closing_balance` is recomputed from the stored opening
        balance and the *post-increment* totals, so it can never drift from
        `total_debits` / `total_credits`.
        """
        if not movements:
            return

        for normal_balance in (NormalBalance.DEBIT, NormalBalance.CREDIT):
            group = [m for m in movements if m.normal_balance is normal_balance]
            if group:
                await self._upsert_group(group, normal_balance)

    async def _upsert_group(
        self, movements: Iterable[LedgerMovement], normal_balance: NormalBalance
    ) -> None:
        rows = []
        for movement in movements:
            net = movement.net
            rows.append(
                {
                    "company_id": movement.company_id,
                    "account_id": movement.account_id,
                    "period_id": movement.period_id,
                    "currency_code": movement.currency_code,
                    "opening_balance": Decimal("0.0000"),
                    "total_debits": movement.total_debits,
                    "total_credits": movement.total_credits,
                    "closing_balance": net,
                }
            )

        stmt = pg_insert(LedgerBalance).values(rows)
        new_debits = LedgerBalance.total_debits + stmt.excluded.total_debits
        new_credits = LedgerBalance.total_credits + stmt.excluded.total_credits
        movement_expr = (
            new_debits - new_credits
            if normal_balance is NormalBalance.DEBIT
            else new_credits - new_debits
        )

        stmt = stmt.on_conflict_do_update(
            index_elements=_CONFLICT_COLUMNS,
            set_={
                "total_debits": new_debits,
                "total_credits": new_credits,
                "closing_balance": LedgerBalance.opening_balance + movement_expr,
                "updated_at": text("now()"),
            },
        )
        await self.session.execute(stmt)

    # --- reads -------------------------------------------------------------
    async def get(
        self, company_id: UUID, account_id: UUID, period_id: UUID, currency_code: str
    ) -> LedgerBalance | None:
        stmt = select(LedgerBalance).where(
            LedgerBalance.company_id == company_id,
            LedgerBalance.account_id == account_id,
            LedgerBalance.period_id == period_id,
            LedgerBalance.currency_code == currency_code.upper(),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_period(
        self, company_id: UUID, period_id: UUID, *, currency_code: str | None = None
    ) -> Sequence[LedgerBalance]:
        stmt = select(LedgerBalance).where(
            LedgerBalance.company_id == company_id,
            LedgerBalance.period_id == period_id,
        )
        if currency_code:
            stmt = stmt.where(LedgerBalance.currency_code == currency_code.upper())
        return (await self.session.execute(stmt)).scalars().all()

    async def list_for_company(
        self, company_id: UUID, *, currency_code: str | None = None
    ) -> Sequence[LedgerBalance]:
        stmt = select(LedgerBalance).where(LedgerBalance.company_id == company_id)
        if currency_code:
            stmt = stmt.where(LedgerBalance.currency_code == currency_code.upper())
        return (await self.session.execute(stmt)).scalars().all()

    # --- rebuild support ---------------------------------------------------
    async def clear_company(self, company_id: UUID) -> int:
        """Drop the projection for a company. Only used by the rebuild script."""
        result = await self.session.execute(
            delete(LedgerBalance).where(LedgerBalance.company_id == company_id)
        )
        return int(getattr(result, "rowcount", 0) or 0)
