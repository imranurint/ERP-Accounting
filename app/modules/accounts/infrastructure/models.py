"""Chart of accounts."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.shared.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.shared.domain.enums import AccountType, NormalBalance

if TYPE_CHECKING:
    from app.modules.organization.infrastructure.models import Company


class Account(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A ledger account.

    Accounts are never hard deleted once they carry posted transactions —
    doing so would orphan accounting history. Deactivate with `is_active`.
    Parent (roll-up) accounts must be `is_postable = false`.

    `normal_balance` defaults to the account type's natural side but is stored
    independently so that *contra* accounts work: accumulated depreciation is
    an ASSET carrying a CREDIT balance, sales discounts a REVENUE carrying a
    DEBIT balance. Balance arithmetic follows `normal_balance`; report
    grouping follows `account_type`.
    """

    __tablename__ = "chart_of_accounts"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    account_number: Mapped[str] = mapped_column(String(32), nullable=False)
    account_name: Mapped[str] = mapped_column(String(200), nullable=False)
    account_type: Mapped[AccountType] = mapped_column(
        Enum(
            AccountType,
            name="account_type",
            native_enum=False,
            length=16,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    normal_balance: Mapped[NormalBalance] = mapped_column(
        Enum(
            NormalBalance,
            name="normal_balance",
            native_enum=False,
            length=8,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    #: When set, the account only accepts postings in this currency.
    currency_code: Mapped[str | None] = mapped_column(
        String(3), ForeignKey("currencies.currency_code", ondelete="RESTRICT")
    )
    parent_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_postable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(String(500))

    company: Mapped[Company] = relationship(back_populates="accounts", lazy="noload")
    parent: Mapped[Account | None] = relationship(remote_side="Account.id", lazy="noload")

    __table_args__ = (
        UniqueConstraint(
            "company_id", "account_number", name="uq_chart_of_accounts_company_number"
        ),
        CheckConstraint("id <> parent_account_id", name="parent_not_self"),
        CheckConstraint("length(trim(account_number)) > 0", name="account_number_not_blank"),
        Index("ix_chart_of_accounts_company_type", "company_id", "account_type"),
        Index("ix_chart_of_accounts_parent", "parent_account_id"),
    )
