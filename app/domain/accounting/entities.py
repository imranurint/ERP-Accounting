"""Pure domain entities.

These are persistence-free structures the domain rules operate on. ORM models
in `app.infrastructure.database.models` map onto the same concepts but stay in
the infrastructure layer; the domain never imports SQLAlchemy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from app.core.money import ZERO
from app.domain.accounting.enums import (
    AccountType,
    FiscalPeriodStatus,
    JournalStatus,
    NormalBalance,
)
from app.domain.accounting.value_objects import DateRange, DebitCredit


@dataclass(frozen=True, slots=True)
class Company:
    id: UUID
    name: str
    legal_name: str | None
    base_currency_code: str
    timezone: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class Currency:
    currency_code: str
    currency_name: str
    decimal_places: int
    is_active: bool


@dataclass(frozen=True, slots=True)
class Account:
    id: UUID
    company_id: UUID
    account_number: str
    account_name: str
    account_type: AccountType
    normal_balance: NormalBalance
    currency_code: str | None
    parent_account_id: UUID | None
    is_active: bool
    is_postable: bool


@dataclass(frozen=True, slots=True)
class FiscalPeriod:
    id: UUID
    company_id: UUID
    fiscal_year: int
    period_name: str
    start_date: date
    end_date: date
    status: FiscalPeriodStatus
    is_closed: bool

    @property
    def range(self) -> DateRange:
        return DateRange(self.start_date, self.end_date)

    def covers(self, value: date) -> bool:
        return self.range.contains(value)


@dataclass(frozen=True, slots=True)
class TaxCode:
    id: UUID
    company_id: UUID
    tax_code: str
    tax_name: str
    tax_rate: Decimal
    tax_account_id: UUID
    effective_from: date
    effective_to: date | None
    is_active: bool

    def is_effective_on(self, value: date) -> bool:
        if value < self.effective_from:
            return False
        return self.effective_to is None or value <= self.effective_to


@dataclass(slots=True)
class JournalLine:
    """A single double-entry line, before or after persistence."""

    line_number: int
    account_id: UUID
    amounts: DebitCredit
    currency_code: str
    tax_id: UUID | None = None
    memo: str | None = None
    id: UUID | None = None

    @property
    def debit(self) -> Decimal:
        return self.amounts.debit

    @property
    def credit(self) -> Decimal:
        return self.amounts.credit


@dataclass(slots=True)
class JournalEntry:
    """A journal entry aggregate: the header plus its lines.

    This is the *only* structure that may reach the ledger, and only through
    `JournalPostingService`.
    """

    company_id: UUID
    entry_date: date
    period_id: UUID
    currency_code: str
    lines: list[JournalLine] = field(default_factory=list)
    status: JournalStatus = JournalStatus.DRAFT
    id: UUID | None = None
    entry_number: str | None = None
    source_type: str | None = None
    source_document: str | None = None
    reference: str | None = None
    idempotency_key: str | None = None
    idempotency_payload_hash: str | None = None
    version: int = 1
    posted_at: datetime | None = None
    posted_by: UUID | None = None
    reversed_by_entry_id: UUID | None = None

    @property
    def total_debits(self) -> Decimal:
        return sum((line.debit for line in self.lines), start=ZERO)

    @property
    def total_credits(self) -> Decimal:
        return sum((line.credit for line in self.lines), start=ZERO)

    @property
    def difference(self) -> Decimal:
        return self.total_debits - self.total_credits

    @property
    def is_balanced(self) -> bool:
        return self.difference == ZERO


@dataclass(frozen=True, slots=True)
class LedgerMovement:
    """The aggregated effect of one posting on one ledger balance row."""

    company_id: UUID
    account_id: UUID
    period_id: UUID
    currency_code: str
    total_debits: Decimal
    total_credits: Decimal
    normal_balance: NormalBalance

    @property
    def net(self) -> Decimal:
        """Signed movement in the account's normal-balance direction."""
        movement = self.total_debits - self.total_credits
        return movement if self.normal_balance is NormalBalance.DEBIT else -movement
