"""Tax codes.

The GL stores tax *codes* and the account they post to; it does not compute
tax. Calculating tax amounts belongs to the originating module (Sales, AP,
POS, …), which then submits the resulting lines to the posting service. That
keeps jurisdiction-specific tax logic out of the ledger.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.database.base import (
    Base,
    RateNumeric,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class TaxCode(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tax_codes"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    tax_code: Mapped[str] = mapped_column(String(32), nullable=False)
    tax_name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Percentage, e.g. 15.0000 means 15%.
    tax_rate: Mapped[Decimal] = mapped_column(RateNumeric, nullable=False)
    tax_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "tax_code", name="uq_tax_codes_company_code"),
        CheckConstraint("tax_rate >= 0 AND tax_rate <= 100", name="tax_rate_range"),
        CheckConstraint(
            "effective_to IS NULL OR effective_from <= effective_to",
            name="effective_date_order",
        ),
        Index("ix_tax_codes_company_active", "company_id", "is_active"),
    )
