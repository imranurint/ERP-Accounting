"""Journal tables and the ledger balance projection they feed."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.shared.database.base import (
    Base,
    MoneyNumeric,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from app.shared.domain.enums import JournalStatus

_JOURNAL_STATUS = Enum(
    JournalStatus,
    name="journal_status",
    native_enum=False,
    length=16,
    create_constraint=True,
    values_callable=lambda enum: [member.value for member in enum],
)


class JournalEntry(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "journal_entries"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    #: Allocated from `document_sequences` at post time. NULL while DRAFT.
    entry_number: Mapped[str | None] = mapped_column(String(50))
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_id: Mapped[UUID] = mapped_column(
        ForeignKey("fiscal_periods.id", ondelete="RESTRICT"), nullable=False
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.currency_code", ondelete="RESTRICT"), nullable=False
    )

    #: Provenance from the originating ERP module, e.g.
    #: ("sales_invoice", "INV-9923") or ("student_fee", "ENR-2026-118").
    source_type: Mapped[str | None] = mapped_column(String(64))
    source_document: Mapped[str | None] = mapped_column(String(128))
    reference: Mapped[str | None] = mapped_column(String(255))
    memo: Mapped[str | None] = mapped_column(String(1000))

    status: Mapped[JournalStatus] = mapped_column(
        _JOURNAL_STATUS, nullable=False, default=JournalStatus.DRAFT
    )

    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    idempotency_payload_hash: Mapped[str | None] = mapped_column(String(64))

    #: Optimistic concurrency for DRAFT edits.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    #: Set on the *original* entry when a reversal is posted against it.
    reversed_by_entry_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    #: Set on the *reversal* entry, pointing back at what it reverses.
    reverses_entry_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )

    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    items: Mapped[list[JournalItem]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
        order_by="JournalItem.line_number",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "entry_number", name="uq_journal_entries_company_number"),
        CheckConstraint(
            "(status IN ('POSTED', 'REVERSED') AND entry_number IS NOT NULL)"
            " OR (status IN ('DRAFT', 'VOID') AND entry_number IS NULL)",
            name="entry_number_required_when_posted",
        ),
        CheckConstraint(
            "(status IN ('POSTED', 'REVERSED') AND posted_at IS NOT NULL)"
            " OR (status IN ('DRAFT', 'VOID') AND posted_at IS NULL)",
            name="posted_at_matches_status",
        ),
        CheckConstraint(
            "(idempotency_key IS NULL AND idempotency_payload_hash IS NULL)"
            " OR (idempotency_key IS NOT NULL AND idempotency_payload_hash IS NOT NULL)",
            name="idempotency_pair",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("id <> reversed_by_entry_id", name="not_self_reversed"),
        CheckConstraint("id <> reverses_entry_id", name="not_self_reversal"),
        Index("ix_journal_entries_company_date", "company_id", "entry_date"),
        Index("ix_journal_entries_company_status", "company_id", "status"),
        Index("ix_journal_entries_period", "period_id"),
        Index("ix_journal_entries_source", "company_id", "source_type", "source_document"),
        # Partial unique index: idempotency keys are scoped to a company and
        # only apply when present.
        Index(
            "uq_journal_entries_company_idempotency_key",
            "company_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # Posted entries are the hot path for every report.
        Index(
            "ix_journal_entries_posted",
            "company_id",
            "period_id",
            "entry_date",
            postgresql_where=text("status IN ('POSTED', 'REVERSED')"),
        ),
    )


class JournalItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One side of a double entry.

    The debit/credit invariants are enforced by CHECK constraints as well as
    by `app.shared.domain.rules`. Belt and braces is the right call here:
    the database is the last line of defence for financial data, and a future
    module writing through a different code path must still be unable to
    create a malformed line.
    """

    __tablename__ = "journal_items"

    entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="CASCADE"), nullable=False
    )
    #: Denormalised from the header so ledger queries never need the join.
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    debit: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False, server_default=text("0"))
    credit: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False, server_default=text("0"))
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.currency_code", ondelete="RESTRICT"), nullable=False
    )
    tax_id: Mapped[UUID | None] = mapped_column(ForeignKey("tax_codes.id", ondelete="RESTRICT"))
    memo: Mapped[str | None] = mapped_column(String(500))

    entry: Mapped[JournalEntry] = relationship(back_populates="items", lazy="noload")

    __table_args__ = (
        UniqueConstraint("entry_id", "line_number", name="uq_journal_items_entry_line"),
        CheckConstraint("debit >= 0", name="debit_non_negative"),
        CheckConstraint("credit >= 0", name="credit_non_negative"),
        CheckConstraint("debit + credit > 0", name="amount_present"),
        CheckConstraint(
            "(debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)",
            name="single_sided",
        ),
        CheckConstraint("line_number >= 1", name="line_number_positive"),
        Index("ix_journal_items_entry", "entry_id"),
        Index("ix_journal_items_account", "company_id", "account_id"),
        Index("ix_journal_items_account_only", "account_id"),
    )


# ----------------------- ledger balances (the projection over journal_items)
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
