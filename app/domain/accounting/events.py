"""Domain events.

Events are written to `outbox_events` inside the same database transaction as
the accounting change that produced them (transactional outbox). A separate
relay — deliberately outside the accounting transaction — is responsible for
publishing them to whatever bus future ERP modules subscribe to.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Base event. `event_type` is the contract subscribers bind to."""

    event_type: ClassVar[str] = "DomainEvent"
    aggregate_type: ClassVar[str] = "Unknown"

    aggregate_id: UUID
    company_id: UUID

    def payload(self) -> dict[str, Any]:
        data = dict(asdict(self))
        return dict(_jsonable(data))


@dataclass(frozen=True, slots=True)
class JournalPosted(DomainEvent):
    event_type: ClassVar[str] = "JournalPosted"
    aggregate_type: ClassVar[str] = "JournalEntry"

    entry_number: str = ""
    entry_date: date | None = None
    currency_code: str = ""
    total_debits: Decimal = Decimal("0.0000")
    total_credits: Decimal = Decimal("0.0000")
    source_type: str | None = None
    source_document: str | None = None
    period_id: UUID | None = None
    posted_by: UUID | None = None
    lines: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class JournalReversed(DomainEvent):
    event_type: ClassVar[str] = "JournalReversed"
    aggregate_type: ClassVar[str] = "JournalEntry"

    entry_number: str = ""
    reversal_entry_id: UUID | None = None
    reversal_entry_number: str = ""
    reversed_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class JournalVoided(DomainEvent):
    event_type: ClassVar[str] = "JournalVoided"
    aggregate_type: ClassVar[str] = "JournalEntry"

    entry_number: str | None = None
    voided_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class FiscalPeriodStatusChanged(DomainEvent):
    event_type: ClassVar[str] = "FiscalPeriodStatusChanged"
    aggregate_type: ClassVar[str] = "FiscalPeriod"

    period_name: str = ""
    previous_status: str = ""
    new_status: str = ""
    changed_by: UUID | None = None
