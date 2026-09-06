"""Pure domain services: aggregation and reversal construction.

These functions take validated domain objects and return more domain objects.
They never touch the database — the application layer persists their output
inside the posting transaction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from app.core.money import ZERO
from app.shared.domain.entities import (
    Account,
    JournalEntry,
    JournalLine,
    LedgerMovement,
)
from app.shared.domain.enums import JournalStatus, NormalBalance
from app.shared.domain.exceptions import InvalidJournalStateError, JournalAlreadyReversedError
from app.shared.domain.value_objects import DebitCredit


def aggregate_ledger_movements(
    entry: JournalEntry,
    accounts: Mapping[UUID, Account],
) -> list[LedgerMovement]:
    """Collapse an entry's lines into one movement per (account, currency).

    Two lines hitting the same account produce a single upsert, which keeps
    the ledger write cheap and its result independent of line ordering.
    """
    buckets: dict[tuple[UUID, str], list[Decimal]] = {}

    for line in entry.lines:
        key = (line.account_id, line.currency_code.upper())
        totals = buckets.setdefault(key, [ZERO, ZERO])
        totals[0] += line.debit
        totals[1] += line.credit

    movements: list[LedgerMovement] = []
    for (account_id, currency_code), (debits, credits) in buckets.items():
        account = accounts[account_id]
        movements.append(
            LedgerMovement(
                company_id=entry.company_id,
                account_id=account_id,
                period_id=entry.period_id,
                currency_code=currency_code,
                total_debits=debits,
                total_credits=credits,
                normal_balance=account.normal_balance,
            )
        )
    # Deterministic order keeps concurrent postings from deadlocking on the
    # ledger primary key.
    movements.sort(key=lambda m: (str(m.account_id), m.currency_code))
    return movements


def build_reversal_lines(lines: Sequence[JournalLine]) -> list[JournalLine]:
    """Mirror every line: debits become credits and credits become debits."""
    return [
        JournalLine(
            line_number=index,
            account_id=line.account_id,
            amounts=line.amounts.reversed(),
            currency_code=line.currency_code,
            tax_id=line.tax_id,
            memo=line.memo,
        )
        for index, line in enumerate(sorted(lines, key=lambda ln: ln.line_number), start=1)
    ]


def build_reversal_entry(
    original: JournalEntry,
    *,
    reversal_date: date,
    period_id: UUID,
    reference: str | None = None,
) -> JournalEntry:
    """Construct (but do not persist) the counter-entry for a posted entry."""
    if original.status is JournalStatus.REVERSED or original.reversed_by_entry_id is not None:
        raise JournalAlreadyReversedError(
            details={
                "entry_id": str(original.id),
                "entry_number": original.entry_number,
                "reversed_by_entry_id": (
                    str(original.reversed_by_entry_id) if original.reversed_by_entry_id else None
                ),
            }
        )
    if original.status is not JournalStatus.POSTED:
        raise InvalidJournalStateError(
            "Only a POSTED journal entry can be reversed.",
            details={"entry_id": str(original.id), "status": str(original.status)},
        )

    default_reference = f"Reversal of {original.entry_number or original.id}"
    return JournalEntry(
        company_id=original.company_id,
        entry_date=reversal_date,
        period_id=period_id,
        currency_code=original.currency_code,
        lines=build_reversal_lines(original.lines),
        status=JournalStatus.DRAFT,
        source_type=original.source_type,
        source_document=original.source_document,
        reference=reference or default_reference,
    )


def signed_balance(
    total_debits: Decimal, total_credits: Decimal, normal_balance: NormalBalance
) -> Decimal:
    """Movement expressed in the account's normal-balance direction."""
    movement = total_debits - total_credits
    return movement if normal_balance is NormalBalance.DEBIT else -movement


def zero_out(value: Decimal) -> Decimal:
    """Collapse `-0.0000` to `0.0000` so equality checks read naturally."""
    return value if value != ZERO else ZERO


def build_line_amounts(debit: Decimal, credit: Decimal) -> DebitCredit:
    return DebitCredit(debit=debit, credit=credit)
