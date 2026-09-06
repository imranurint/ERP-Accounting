"""Fiscal periods and document sequences."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.shared.domain.enums import DocumentType, FiscalPeriodStatus


class FiscalPeriod(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An accounting period.

    Overlap prevention is enforced in the database with a GiST exclusion
    constraint over `(company_id, daterange(start_date, end_date, '[]'))` —
    see the initial Alembic migration. Application-level checks alone lose
    races; the constraint cannot.
    """

    __tablename__ = "fiscal_periods"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_name: Mapped[str] = mapped_column(String(100), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[FiscalPeriodStatus] = mapped_column(
        Enum(
            FiscalPeriodStatus,
            name="fiscal_period_status",
            native_enum=False,
            length=16,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=FiscalPeriodStatus.OPEN,
    )
    #: Denormalised convenience flag kept in step with `status` by the service
    #: layer and guarded by a CHECK constraint so the two cannot diverge.
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "period_name",
            "fiscal_year",
            name="uq_fiscal_periods_company_name_year",
        ),
        CheckConstraint("start_date <= end_date", name="date_order"),
        CheckConstraint("fiscal_year >= 1900 AND fiscal_year <= 9999", name="fiscal_year_range"),
        CheckConstraint(
            "(is_closed = true AND status IN ('CLOSED', 'LOCKED'))"
            " OR (is_closed = false AND status IN ('OPEN', 'ADJUSTING'))",
            name="is_closed_matches_status",
        ),
        Index("ix_fiscal_periods_company_dates", "company_id", "start_date", "end_date"),
        Index("ix_fiscal_periods_company_year", "company_id", "fiscal_year"),
    )


class DocumentSequence(Base, UUIDPrimaryKeyMixin):
    """Per-company, per-year document numbering.

    Numbers are allocated with a single atomic
    `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` inside the posting
    transaction — never `SELECT MAX()`, never in application memory. See
    `app.shared.sequences.service`.
    """

    __tablename__ = "document_sequences"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    document_type: Mapped[DocumentType] = mapped_column(
        Enum(
            DocumentType,
            name="document_type",
            native_enum=False,
            length=32,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    next_number: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "document_type",
            "fiscal_year",
            name="uq_document_sequences_company_type_year",
        ),
        CheckConstraint("next_number >= 1", name="next_number_positive"),
    )
