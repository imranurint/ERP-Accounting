"""The accounting golden rules, as pure functions.

No I/O, no ORM, no framework. Everything here is directly unit-testable and is
the single place each rule is expressed. `JournalPostingService` orchestrates
these; it does not re-implement them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from app.core.money import ZERO, exceeds_currency_precision, within_storage_range
from app.shared.domain.entities import (
    Account,
    Currency,
    FiscalPeriod,
    JournalEntry,
    JournalLine,
    TaxCode,
)
from app.shared.domain.enums import (
    AccountType,
    FiscalPeriodStatus,
    JournalStatus,
    NormalBalance,
)
from app.shared.domain.exceptions import (
    AccountInactiveError,
    AccountNotPostableError,
    CrossCompanyReferenceError,
    CurrencyMismatchError,
    FiscalPeriodClosedError,
    FiscalPeriodNotFoundForDateError,
    InvalidJournalLineError,
    InvalidJournalStateError,
    InvalidPeriodTransitionError,
    MoneyPrecisionError,
    TaxCodeInactiveError,
    UnbalancedJournalEntryError,
)


# ---------------------------------------------------------------------------
# RULE 2 — mutually exclusive, non-negative amounts
# ---------------------------------------------------------------------------
def validate_line_amounts(line: JournalLine) -> None:
    """A line must carry a value on exactly one side."""
    debit, credit = line.debit, line.credit

    if debit < ZERO or credit < ZERO:
        raise InvalidJournalLineError(
            "Debit and credit amounts must not be negative.",
            details={"line_number": line.line_number, "debit": str(debit), "credit": str(credit)},
        )
    if debit == ZERO and credit == ZERO:
        raise InvalidJournalLineError(
            "A journal line must carry a debit or a credit amount.",
            details={"line_number": line.line_number, "debit": str(debit), "credit": str(credit)},
        )
    if debit > ZERO and credit > ZERO:
        raise InvalidJournalLineError(
            "A journal line may not contain both a debit and a credit.",
            details={"line_number": line.line_number, "debit": str(debit), "credit": str(credit)},
        )
    for label, amount in (("debit", debit), ("credit", credit)):
        if not within_storage_range(amount):
            raise InvalidJournalLineError(
                "Amount exceeds the supported monetary range.",
                details={"line_number": line.line_number, label: str(amount)},
            )


def validate_money_precision(line: JournalLine, currency: Currency) -> None:
    """Reject amounts finer than the currency's own scale."""
    for label, amount in (("debit", line.debit), ("credit", line.credit)):
        if exceeds_currency_precision(amount, currency.decimal_places):
            raise MoneyPrecisionError(
                f"{currency.currency_code} allows at most "
                f"{currency.decimal_places} decimal places.",
                details={
                    "line_number": line.line_number,
                    "field": label,
                    "value": str(amount),
                    "decimal_places": currency.decimal_places,
                },
            )


def validate_line_numbers(lines: Sequence[JournalLine]) -> None:
    """Line numbers must be 1..n with no gaps and no duplicates."""
    if not lines:
        raise InvalidJournalLineError("A journal entry must contain at least one line.")
    if len(lines) < 2:
        raise InvalidJournalLineError(
            "Double-entry requires at least two lines.",
            details={"line_count": len(lines)},
        )
    numbers = [line.line_number for line in lines]
    expected = list(range(1, len(numbers) + 1))
    if sorted(numbers) != expected:
        raise InvalidJournalLineError(
            "Line numbers must be sequential starting at 1.",
            details={"received": numbers, "expected": expected},
        )


def validate_line_currency(line: JournalLine, entry_currency_code: str) -> None:
    if line.currency_code.upper() != entry_currency_code.upper():
        raise CurrencyMismatchError(
            "Journal line currency must match the entry currency.",
            details={
                "line_number": line.line_number,
                "line_currency": line.currency_code,
                "entry_currency": entry_currency_code,
            },
        )


# ---------------------------------------------------------------------------
# RULE 1 — a posted entry balances
# ---------------------------------------------------------------------------
def validate_balanced(entry: JournalEntry) -> None:
    if not entry.is_balanced:
        raise UnbalancedJournalEntryError(
            details={
                "total_debits": str(entry.total_debits),
                "total_credits": str(entry.total_credits),
                "difference": str(entry.difference),
            }
        )
    if entry.total_debits == ZERO:
        raise UnbalancedJournalEntryError(
            "A journal entry must move a non-zero amount.",
            details={"total_debits": "0.0000", "total_credits": "0.0000"},
        )


# ---------------------------------------------------------------------------
# RULE 3 — fiscal period validation
# ---------------------------------------------------------------------------
def validate_period_covers_date(period: FiscalPeriod, entry_date: date) -> None:
    if not period.covers(entry_date):
        raise FiscalPeriodNotFoundForDateError(
            "The entry date falls outside the selected fiscal period.",
            details={
                "entry_date": entry_date.isoformat(),
                "period_id": str(period.id),
                "period_name": period.period_name,
                "start_date": period.start_date.isoformat(),
                "end_date": period.end_date.isoformat(),
            },
        )


def validate_period_accepts_posting(period: FiscalPeriod) -> None:
    if not period.status.accepts_postings:
        raise FiscalPeriodClosedError(
            f"Fiscal period {period.period_name} is {period.status} and cannot accept postings.",
            details={
                "period_id": str(period.id),
                "period_name": period.period_name,
                "status": str(period.status),
            },
        )


#: The only fiscal-period transitions the system permits.
_ALLOWED_PERIOD_TRANSITIONS: dict[FiscalPeriodStatus, frozenset[FiscalPeriodStatus]] = {
    FiscalPeriodStatus.OPEN: frozenset({FiscalPeriodStatus.CLOSED, FiscalPeriodStatus.ADJUSTING}),
    FiscalPeriodStatus.ADJUSTING: frozenset({FiscalPeriodStatus.OPEN, FiscalPeriodStatus.CLOSED}),
    FiscalPeriodStatus.CLOSED: frozenset({FiscalPeriodStatus.OPEN, FiscalPeriodStatus.LOCKED}),
    # LOCKED is terminal: a locked period is a filed period.
    FiscalPeriodStatus.LOCKED: frozenset(),
}


def validate_period_transition(current: FiscalPeriodStatus, target: FiscalPeriodStatus) -> None:
    if target not in _ALLOWED_PERIOD_TRANSITIONS[current]:
        raise InvalidPeriodTransitionError(
            f"Cannot move a fiscal period from {current} to {target}.",
            details={
                "current_status": str(current),
                "target_status": str(target),
                "allowed": sorted(str(s) for s in _ALLOWED_PERIOD_TRANSITIONS[current]),
            },
        )


# ---------------------------------------------------------------------------
# Master-data validation
# ---------------------------------------------------------------------------
def validate_account_usable(account: Account, company_id: object, line_number: int) -> None:
    if account.company_id != company_id:
        raise CrossCompanyReferenceError(
            "Account belongs to a different company.",
            details={"line_number": line_number, "account_id": str(account.id)},
        )
    if not account.is_active:
        raise AccountInactiveError(
            f"Account {account.account_number} ({account.account_name}) is inactive.",
            details={
                "line_number": line_number,
                "account_id": str(account.id),
                "account_number": account.account_number,
            },
        )
    if not account.is_postable:
        raise AccountNotPostableError(
            f"Account {account.account_number} ({account.account_name}) is not postable.",
            details={
                "line_number": line_number,
                "account_id": str(account.id),
                "account_number": account.account_number,
            },
        )


def validate_account_currency(account: Account, entry_currency_code: str, line_number: int) -> None:
    """An account pinned to a currency only accepts lines in that currency."""
    if account.currency_code and account.currency_code.upper() != entry_currency_code.upper():
        raise CurrencyMismatchError(
            f"Account {account.account_number} only accepts {account.currency_code}.",
            details={
                "line_number": line_number,
                "account_currency": account.currency_code,
                "entry_currency": entry_currency_code,
            },
        )


def validate_tax_code_usable(
    tax_code: TaxCode, company_id: object, entry_date: date, line_number: int
) -> None:
    if tax_code.company_id != company_id:
        raise CrossCompanyReferenceError(
            "Tax code belongs to a different company.",
            details={"line_number": line_number, "tax_id": str(tax_code.id)},
        )
    if not tax_code.is_active:
        raise TaxCodeInactiveError(
            f"Tax code {tax_code.tax_code} is inactive.",
            details={"line_number": line_number, "tax_code": tax_code.tax_code},
        )
    if not tax_code.is_effective_on(entry_date):
        raise TaxCodeInactiveError(
            f"Tax code {tax_code.tax_code} is not effective on {entry_date.isoformat()}.",
            details={
                "line_number": line_number,
                "tax_code": tax_code.tax_code,
                "effective_from": tax_code.effective_from.isoformat(),
                "effective_to": (
                    tax_code.effective_to.isoformat() if tax_code.effective_to else None
                ),
                "entry_date": entry_date.isoformat(),
            },
        )


# ---------------------------------------------------------------------------
# RULE 5 / journal state machine
# ---------------------------------------------------------------------------
#: DRAFT → POSTED | VOID ; POSTED → REVERSED. REVERSED and VOID are terminal.
_ALLOWED_JOURNAL_TRANSITIONS: dict[JournalStatus, frozenset[JournalStatus]] = {
    JournalStatus.DRAFT: frozenset({JournalStatus.POSTED, JournalStatus.VOID}),
    JournalStatus.POSTED: frozenset({JournalStatus.REVERSED}),
    JournalStatus.REVERSED: frozenset(),
    JournalStatus.VOID: frozenset(),
}


def validate_journal_transition(current: JournalStatus, target: JournalStatus) -> None:
    if target not in _ALLOWED_JOURNAL_TRANSITIONS[current]:
        raise InvalidJournalStateError(
            f"Cannot move a journal entry from {current} to {target}.",
            details={
                "current_status": str(current),
                "target_status": str(target),
                "allowed": sorted(str(s) for s in _ALLOWED_JOURNAL_TRANSITIONS[current]),
            },
        )


def validate_mutable(status: JournalStatus) -> None:
    """Guard for edit/delete: only DRAFT entries may change."""
    from app.shared.domain.exceptions import PostedEntryCannotBeModifiedError

    if status is JournalStatus.DRAFT:
        return
    if status in (JournalStatus.POSTED, JournalStatus.REVERSED):
        raise PostedEntryCannotBeModifiedError(
            details={"status": str(status)},
        )
    raise InvalidJournalStateError(
        f"A {status} journal entry cannot be modified.",
        details={"status": str(status)},
    )


# ---------------------------------------------------------------------------
# Normal balance arithmetic
# ---------------------------------------------------------------------------
def closing_balance(
    opening_balance: Decimal,
    total_debits: Decimal,
    total_credits: Decimal,
    normal_balance: NormalBalance,
) -> Decimal:
    """ASSET/EXPENSE: opening + debits - credits. Otherwise the mirror image."""
    if normal_balance is NormalBalance.DEBIT:
        return opening_balance + total_debits - total_credits
    return opening_balance + total_credits - total_debits


def normal_balance_for(account_type: AccountType) -> NormalBalance:
    return account_type.normal_balance
