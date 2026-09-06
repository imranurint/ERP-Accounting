"""Journal entry API schemas."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from app.shared.domain.enums import JournalStatus
from app.shared.schemas import AppModel, CurrencyCode, MoneyStr, ORMModel


class JournalLineInput(AppModel):
    """One line of a submitted entry.

    Accounts and tax codes may be identified by id *or* by their business
    code. ERP modules integrating with the GL usually know "402", not a UUID.
    """

    line_number: int = Field(ge=1, description="1-based, sequential, no gaps.")
    account_id: UUID | None = None
    account_number: str | None = Field(default=None, max_length=32)
    debit: MoneyStr = Decimal("0")
    credit: MoneyStr = Decimal("0")
    currency_code: CurrencyCode | None = Field(
        default=None, description="Defaults to the entry currency."
    )
    tax_id: UUID | None = None
    tax_code: str | None = Field(default=None, max_length=32)
    memo: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _require_account_reference(self) -> JournalLineInput:
        if self.account_id is None and not self.account_number:
            raise ValueError("Each line requires either account_id or account_number.")
        if self.tax_id is not None and self.tax_code:
            raise ValueError("Provide either tax_id or tax_code, not both.")
        return self


class JournalEntryCreate(AppModel):
    """Create a journal entry.

    `post_immediately` defaults to true because that is what an integrating
    ERP module wants: submit a business transaction, get back a posted,
    balanced, numbered accounting document. Set it to false to park a draft
    for human review.
    """

    company_id: UUID
    entry_date: date
    period_id: UUID | None = Field(
        default=None,
        description="Optional. When omitted the period covering entry_date is used.",
    )
    currency_code: CurrencyCode
    source_type: str | None = Field(default=None, max_length=64)
    source_document: str | None = Field(default=None, max_length=128)
    reference: str | None = Field(default=None, max_length=255)
    memo: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=255)
    post_immediately: bool = True
    lines: list[JournalLineInput] = Field(min_length=2, max_length=1000)

    def idempotency_payload(self) -> dict[str, Any]:
        """The canonical view used for the idempotency hash.

        Deliberately excludes `post_immediately` and `idempotency_key`: the
        hash answers "is this the same accounting transaction?", not "was the
        request byte-identical".
        """
        return {
            "company_id": self.company_id,
            "entry_date": self.entry_date.isoformat(),
            "currency_code": self.currency_code,
            "source_type": self.source_type,
            "source_document": self.source_document,
            "reference": self.reference,
            "lines": [
                {
                    "line_number": line.line_number,
                    "account_id": line.account_id,
                    "account_number": line.account_number,
                    "debit": line.debit,
                    "credit": line.credit,
                    "currency_code": line.currency_code or self.currency_code,
                    "tax_id": line.tax_id,
                    "tax_code": line.tax_code,
                    "memo": line.memo,
                }
                for line in sorted(self.lines, key=lambda ln: ln.line_number)
            ],
        }


class JournalEntryUpdate(AppModel):
    """Edit a DRAFT entry. Posted entries reject this outright."""

    entry_date: date | None = None
    period_id: UUID | None = None
    currency_code: CurrencyCode | None = None
    source_type: str | None = Field(default=None, max_length=64)
    source_document: str | None = Field(default=None, max_length=128)
    reference: str | None = Field(default=None, max_length=255)
    memo: str | None = Field(default=None, max_length=1000)
    lines: list[JournalLineInput] | None = Field(default=None, min_length=2, max_length=1000)
    version: int | None = Field(
        default=None,
        ge=1,
        description="Optimistic lock. When supplied it must match the stored version.",
    )


class JournalPostRequest(AppModel):
    """Post an existing DRAFT."""

    idempotency_key: str | None = Field(default=None, max_length=255)


class JournalReversalRequest(AppModel):
    reversal_date: date | None = Field(
        default=None, description="Defaults to the original entry date."
    )
    period_id: UUID | None = Field(
        default=None, description="Defaults to the period covering reversal_date."
    )
    reference: str | None = Field(default=None, max_length=255)
    idempotency_key: str | None = Field(default=None, max_length=255)


class JournalVoidRequest(AppModel):
    reason: str | None = Field(default=None, max_length=500)


class JournalItemRead(ORMModel):
    id: UUID
    line_number: int
    account_id: UUID
    account_number: str | None = None
    account_name: str | None = None
    debit: MoneyStr
    credit: MoneyStr
    currency_code: str
    tax_id: UUID | None = None
    memo: str | None = None


class JournalEntryRead(ORMModel):
    id: UUID
    company_id: UUID
    entry_number: str | None
    entry_date: date
    period_id: UUID
    currency_code: str
    source_type: str | None
    source_document: str | None
    reference: str | None
    memo: str | None
    status: JournalStatus
    idempotency_key: str | None
    version: int
    posted_at: datetime | None
    posted_by: UUID | None
    reversed_by_entry_id: UUID | None
    reverses_entry_id: UUID | None
    created_at: datetime
    created_by: UUID | None
    updated_at: datetime
    updated_by: UUID | None
    total_debits: MoneyStr
    total_credits: MoneyStr
    items: list[JournalItemRead] = Field(default_factory=list)


class JournalEntrySummary(ORMModel):
    """List view — no lines, so a page of 200 stays one query plus one count."""

    id: UUID
    entry_number: str | None
    entry_date: date
    period_id: UUID
    currency_code: str
    status: JournalStatus
    reference: str | None
    source_type: str | None
    source_document: str | None
    total_debits: MoneyStr
    total_credits: MoneyStr
    posted_at: datetime | None
    created_at: datetime


class ReversalResult(AppModel):
    original: JournalEntryRead
    reversal: JournalEntryRead
