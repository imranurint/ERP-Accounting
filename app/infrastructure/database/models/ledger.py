"""Ledger balance projection.

`ledger_balances` is a *cache*, not the source of truth. It exists so that a
trial balance does not have to aggregate every journal item ever written. It
is updated only inside the journal posting transaction, with an atomic
`INSERT ... ON CONFLICT DO UPDATE`, and it can always be rebuilt from
`journal_entries` + `journal_items` via `python -m app.scripts.rebuild_ledger`.

V1 semantics: a row holds the *movement within one fiscal period*.
`opening_balance` stays 0 and opening figures are derived at report time by
cumulating prior periods. Keeping the stored value derivation-free is what
makes the rebuild bit-for-bit identical to incremental posting.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base, MoneyNumeric


class LedgerBalance(Base):
    __tablename__ = "ledger_balances"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), primary_key=True
    )
    period_id: Mapped[UUID] = mapped_column(
        ForeignKey("fiscal_periods.id", ondelete="RESTRICT"), primary_key=True
    )
    currency_code: Mapped[str] = mapped_column(
        String(3),
        ForeignKey("currencies.currency_code", ondelete="RESTRICT"),
        primary_key=True,
    )

    opening_balance: Mapped[Decimal] = mapped_column(
        MoneyNumeric, nullable=False, server_default=text("0")
    )
    total_debits: Mapped[Decimal] = mapped_column(
        MoneyNumeric, nullable=False, server_default=text("0")
    )
    total_credits: Mapped[Decimal] = mapped_column(
        MoneyNumeric, nullable=False, server_default=text("0")
    )
    closing_balance: Mapped[Decimal] = mapped_column(
        MoneyNumeric, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        Index("ix_ledger_balances_company_period", "company_id", "period_id"),
        Index("ix_ledger_balances_account", "account_id"),
    )
