"""Accounting enumerations.

These are stored as short strings with database CHECK constraints rather than
native PostgreSQL enum types: adding a value later is a plain constraint swap
instead of a type migration, and the values stay readable in raw SQL.
"""

from __future__ import annotations

from enum import StrEnum


class AccountType(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"

    @property
    def normal_balance(self) -> NormalBalance:
        """The side on which this account type normally carries its balance."""
        if self in (AccountType.ASSET, AccountType.EXPENSE):
            return NormalBalance.DEBIT
        return NormalBalance.CREDIT

    @property
    def is_balance_sheet(self) -> bool:
        return self in (AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY)

    @property
    def is_income_statement(self) -> bool:
        return self in (AccountType.REVENUE, AccountType.EXPENSE)


class NormalBalance(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"

    @property
    def sign(self) -> int:
        """+1 when debits increase the balance, -1 when credits do."""
        return 1 if self is NormalBalance.DEBIT else -1


class FiscalPeriodStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    ADJUSTING = "ADJUSTING"
    LOCKED = "LOCKED"

    @property
    def accepts_postings(self) -> bool:
        return self in (FiscalPeriodStatus.OPEN, FiscalPeriodStatus.ADJUSTING)


class JournalStatus(StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    REVERSED = "REVERSED"
    VOID = "VOID"

    @property
    def is_mutable(self) -> bool:
        return self is JournalStatus.DRAFT

    @property
    def affects_ledger(self) -> bool:
        """Only POSTED and REVERSED entries carry accounting weight.

        A REVERSED entry keeps its original ledger impact — the reversing
        entry cancels it with an opposite posting rather than erasing it.
        """
        return self in (JournalStatus.POSTED, JournalStatus.REVERSED)


class DocumentType(StrEnum):
    JOURNAL_ENTRY = "JOURNAL_ENTRY"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class AuditAction(StrEnum):
    JOURNAL_CREATED = "JOURNAL_CREATED"
    JOURNAL_UPDATED = "JOURNAL_UPDATED"
    JOURNAL_DELETED = "JOURNAL_DELETED"
    JOURNAL_POSTED = "JOURNAL_POSTED"
    JOURNAL_REVERSED = "JOURNAL_REVERSED"
    JOURNAL_VOIDED = "JOURNAL_VOIDED"
    PERIOD_CREATED = "PERIOD_CREATED"
    PERIOD_OPENED = "PERIOD_OPENED"
    PERIOD_CLOSED = "PERIOD_CLOSED"
    PERIOD_LOCKED = "PERIOD_LOCKED"
    PERIOD_REOPENED = "PERIOD_REOPENED"
    PERIOD_ADJUSTING = "PERIOD_ADJUSTING"
    ACCOUNT_CREATED = "ACCOUNT_CREATED"
    ACCOUNT_UPDATED = "ACCOUNT_UPDATED"
    ACCOUNT_ACTIVATED = "ACCOUNT_ACTIVATED"
    ACCOUNT_DEACTIVATED = "ACCOUNT_DEACTIVATED"
    COMPANY_CREATED = "COMPANY_CREATED"
    COMPANY_UPDATED = "COMPANY_UPDATED"
    CURRENCY_CREATED = "CURRENCY_CREATED"
    CURRENCY_UPDATED = "CURRENCY_UPDATED"
    TAX_CODE_CREATED = "TAX_CODE_CREATED"
    TAX_CODE_UPDATED = "TAX_CODE_UPDATED"
    USER_CREATED = "USER_CREATED"
    USER_ROLE_CHANGED = "USER_ROLE_CHANGED"
    USER_LOGIN = "USER_LOGIN"
    USER_LOGOUT = "USER_LOGOUT"
    LEDGER_REBUILT = "LEDGER_REBUILT"
