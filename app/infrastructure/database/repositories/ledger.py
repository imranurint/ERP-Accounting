"""Ledger balance projection repository.

The only write path is `apply_movements`, which performs an atomic upsert.
A read-modify-write (`SELECT balance` → `UPDATE balance`) would lose updates
whenever two postings touch the same account concurrently, so it is never
used here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.accounting.entities import LedgerMovement
from app.domain.accounting.enums import NormalBalance
from app.infrastructure.database.models.ledger import LedgerBalance

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
