"""Read-side mapping for journal entries.

Kept separate from the write path so the posting service never has to think
about presentation, and so response building stays free of database access.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

from app.core.money import ZERO
from app.modules.accounts.infrastructure.models import Account
from app.modules.journal.application.schemas import (
    JournalEntryRead,
    JournalEntrySummary,
    JournalItemRead,
)
from app.modules.journal.infrastructure.models import JournalEntry


def _totals(entry: JournalEntry) -> tuple[Decimal, Decimal]:
    debits = sum((item.debit for item in entry.items), start=ZERO)
    credits = sum((item.credit for item in entry.items), start=ZERO)
    return debits, credits


def to_read_model(
    entry: JournalEntry, accounts: dict[UUID, Account] | None = None
) -> JournalEntryRead:
    """Full entry view. `accounts` decorates lines with number/name if supplied."""
    debits, credits = _totals(entry)
    lookup = accounts or {}
    return JournalEntryRead(
        id=entry.id,
        company_id=entry.company_id,
        entry_number=entry.entry_number,
        entry_date=entry.entry_date,
        period_id=entry.period_id,
        currency_code=entry.currency_code,
        source_type=entry.source_type,
        source_document=entry.source_document,
        reference=entry.reference,
        memo=entry.memo,
        status=entry.status,
        idempotency_key=entry.idempotency_key,
        version=entry.version,
        posted_at=entry.posted_at,
        posted_by=entry.posted_by,
        reversed_by_entry_id=entry.reversed_by_entry_id,
        reverses_entry_id=entry.reverses_entry_id,
        created_at=entry.created_at,
        created_by=entry.created_by,
        updated_at=entry.updated_at,
        updated_by=entry.updated_by,
        total_debits=debits,
        total_credits=credits,
        items=[
            JournalItemRead(
                id=item.id,
                line_number=item.line_number,
                account_id=item.account_id,
                account_number=(
                    lookup[item.account_id].account_number if item.account_id in lookup else None
                ),
                account_name=(
                    lookup[item.account_id].account_name if item.account_id in lookup else None
                ),
                debit=item.debit,
                credit=item.credit,
                currency_code=item.currency_code,
                tax_id=item.tax_id,
                memo=item.memo,
            )
            for item in sorted(entry.items, key=lambda i: i.line_number)
        ],
    )


def to_summary(
    entry: JournalEntry, totals: tuple[Decimal, Decimal] | None = None
) -> JournalEntrySummary:
    debits, credits = totals if totals is not None else _totals(entry)
    return JournalEntrySummary(
        id=entry.id,
        entry_number=entry.entry_number,
        entry_date=entry.entry_date,
        period_id=entry.period_id,
        currency_code=entry.currency_code,
        status=entry.status,
        reference=entry.reference,
        source_type=entry.source_type,
        source_document=entry.source_document,
        total_debits=debits,
        total_credits=credits,
        posted_at=entry.posted_at,
        created_at=entry.created_at,
    )


def to_summaries(
    entries: Sequence[JournalEntry], totals: dict[UUID, tuple[Decimal, Decimal]]
) -> list[JournalEntrySummary]:
    """Build a list page from pre-aggregated totals — no per-entry query."""
    return [to_summary(entry, totals.get(entry.id, (ZERO, ZERO))) for entry in entries]
