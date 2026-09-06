"""Journal commands — the stable contract for every caller.

The HTTP layer builds these from Pydantic schemas; a future Sales, Payroll or
POS module builds them directly in-process. Either way the command goes
through `JournalPostingService` and nothing else, which is what keeps "no
module may create a posted transaction outside the posting service" true.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.money import to_decimal


@dataclass(slots=True)
class JournalLineCommand:
    line_number: int
    debit: Decimal
    credit: Decimal
    account_id: UUID | None = None
    account_number: str | None = None
    currency_code: str | None = None
    tax_id: UUID | None = None
    tax_code: str | None = None
    memo: str | None = None

    @classmethod
    def of(
        cls,
        line_number: int,
        *,
        debit: str | Decimal = "0",
        credit: str | Decimal = "0",
        account_id: UUID | None = None,
        account_number: str | None = None,
        currency_code: str | None = None,
        tax_id: UUID | None = None,
        tax_code: str | None = None,
        memo: str | None = None,
    ) -> JournalLineCommand:
        """Convenience constructor for in-process callers and tests."""
        return cls(
            line_number=line_number,
            debit=to_decimal(debit),
            credit=to_decimal(credit),
            account_id=account_id,
            account_number=account_number,
            currency_code=currency_code,
            tax_id=tax_id,
            tax_code=tax_code,
            memo=memo,
        )


@dataclass(slots=True)
class ActorContext:
    """Who is acting, and the request metadata the audit log needs."""

    user_id: UUID | None = None
    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None


@dataclass(slots=True)
class CreateJournalCommand:
    company_id: UUID
    entry_date: date
    currency_code: str
    lines: list[JournalLineCommand]
    period_id: UUID | None = None
    source_type: str | None = None
    source_document: str | None = None
    reference: str | None = None
    memo: str | None = None
    idempotency_key: str | None = None
    idempotency_payload_hash: str | None = None
    post_immediately: bool = True
    actor: ActorContext = field(default_factory=ActorContext)

    def idempotency_payload(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "entry_date": self.entry_date.isoformat(),
            "currency_code": self.currency_code.upper(),
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
                    "currency_code": (line.currency_code or self.currency_code).upper(),
                    "tax_id": line.tax_id,
                    "tax_code": line.tax_code,
                    "memo": line.memo,
                }
                for line in sorted(self.lines, key=lambda ln: ln.line_number)
            ],
        }


@dataclass(slots=True)
class PostJournalCommand:
    """Post an existing DRAFT entry."""

    company_id: UUID
    entry_id: UUID
    idempotency_key: str | None = None
    actor: ActorContext = field(default_factory=ActorContext)


@dataclass(slots=True)
class ReverseJournalCommand:
    company_id: UUID
    entry_id: UUID
    reversal_date: date | None = None
    period_id: UUID | None = None
    reference: str | None = None
    idempotency_key: str | None = None
    actor: ActorContext = field(default_factory=ActorContext)


@dataclass(slots=True)
class VoidJournalCommand:
    company_id: UUID
    entry_id: UUID
    reason: str | None = None
    actor: ActorContext = field(default_factory=ActorContext)
