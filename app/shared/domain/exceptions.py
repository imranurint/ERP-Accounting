"""Accounting domain errors.

Each carries the stable machine-readable code that appears in API responses.
Clients — including future ERP modules — branch on `error`, never on prose.
"""

from __future__ import annotations

from app.core.exceptions import AppError


class AccountingError(AppError):
    """Base class for accounting rule violations."""

    code = "ACCOUNTING_ERROR"
    status_code = 422


# --- Journal structure ----------------------------------------------------
class UnbalancedJournalEntryError(AccountingError):
    code = "UNBALANCED_JOURNAL_ENTRY"
    message = "Total debits must equal total credits."


class InvalidJournalLineError(AccountingError):
    code = "INVALID_JOURNAL_LINE"
    message = "A journal line violates the debit/credit rules."


class InvalidJournalStateError(AccountingError):
    code = "INVALID_JOURNAL_STATE"
    status_code = 409
    message = "The journal entry is not in a state that allows this operation."


class PostedEntryCannotBeModifiedError(AccountingError):
    code = "POSTED_ENTRY_CANNOT_BE_MODIFIED"
    status_code = 409
    message = "A posted journal entry is immutable. Post a reversal instead."


class PostedEntryCannotBeDeletedError(AccountingError):
    code = "POSTED_ENTRY_CANNOT_BE_DELETED"
    status_code = 409
    message = "A posted journal entry cannot be deleted. Post a reversal instead."


class JournalAlreadyReversedError(AccountingError):
    code = "JOURNAL_ALREADY_REVERSED"
    status_code = 409
    message = "This journal entry has already been reversed."


# --- Fiscal periods -------------------------------------------------------
class FiscalPeriodClosedError(AccountingError):
    code = "FISCAL_PERIOD_CLOSED"
    status_code = 409
    message = "The fiscal period is closed or locked and cannot accept postings."


class FiscalPeriodNotFoundForDateError(AccountingError):
    code = "FISCAL_PERIOD_NOT_FOUND_FOR_DATE"
    status_code = 422
    message = "No fiscal period covers the supplied entry date."


class FiscalPeriodOverlapError(AccountingError):
    code = "FISCAL_PERIOD_OVERLAP"
    status_code = 409
    message = "Fiscal periods for a company must not overlap."


class InvalidPeriodTransitionError(AccountingError):
    code = "INVALID_PERIOD_TRANSITION"
    status_code = 409
    message = "That fiscal period state transition is not allowed."


# --- Master data ----------------------------------------------------------
class AccountNotPostableError(AccountingError):
    code = "ACCOUNT_NOT_POSTABLE"
    message = "This account does not accept journal lines."


class AccountInactiveError(AccountingError):
    code = "ACCOUNT_INACTIVE"
    message = "This account is inactive and cannot be used."


class AccountHasTransactionsError(AccountingError):
    code = "ACCOUNT_HAS_TRANSACTIONS"
    status_code = 409
    message = "An account with posted transactions cannot be deleted. Deactivate it instead."


class TaxCodeInactiveError(AccountingError):
    code = "TAX_CODE_INACTIVE"
    message = "This tax code is inactive or outside its effective date range."


class CurrencyInactiveError(AccountingError):
    code = "CURRENCY_INACTIVE"
    message = "This currency is inactive."


class CompanyInactiveError(AccountingError):
    code = "COMPANY_INACTIVE"
    message = "This company is inactive."


class CurrencyMismatchError(AccountingError):
    code = "CURRENCY_MISMATCH"
    message = "A journal line currency does not match the entry currency."


class CrossCompanyReferenceError(AccountingError):
    code = "CROSS_COMPANY_REFERENCE"
    message = "The referenced record belongs to a different company."


class MoneyPrecisionError(AccountingError):
    code = "INVALID_MONEY_PRECISION"
    message = "The amount has more decimal places than the currency allows."


class DuplicateIdempotencyKeyError(AccountingError):
    code = "DUPLICATE_IDEMPOTENCY_KEY"
    status_code = 409
    message = "This idempotency key was already used with a different payload."
