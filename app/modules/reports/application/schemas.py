"""Financial report schemas."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import Field

from app.shared.domain.enums import AccountType, NormalBalance
from app.shared.schemas import AppModel, MoneyStr


class ReportMeta(AppModel):
    """Common header on every report — what was asked, and when."""

    company_id: UUID
    company_name: str
    currency_code: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    period_id: UUID | None = None
    generated_at: datetime


# --------------------------------------------------------------- trial balance
class TrialBalanceRow(AppModel):
    account_id: UUID
    account_number: str
    account_name: str
    account_type: AccountType
    normal_balance: NormalBalance
    currency_code: str
    opening_balance: MoneyStr
    total_debits: MoneyStr
    total_credits: MoneyStr
    closing_balance: MoneyStr


class TrialBalanceReport(AppModel):
    meta: ReportMeta
    rows: list[TrialBalanceRow]
    total_debits: MoneyStr
    total_credits: MoneyStr
    #: True when total debits equal total credits. It always should be; if it
    #: is ever false, the ledger has been written to outside the posting
    #: service and that is an incident.
    is_balanced: bool
    difference: MoneyStr


# -------------------------------------------------------------- general ledger
class GeneralLedgerLine(AppModel):
    entry_id: UUID
    entry_number: str | None
    entry_date: date
    reference: str | None
    source_type: str | None
    source_document: str | None
    line_number: int
    memo: str | None
    debit: MoneyStr
    credit: MoneyStr
    running_balance: MoneyStr


class GeneralLedgerAccount(AppModel):
    account_id: UUID
    account_number: str
    account_name: str
    account_type: AccountType
    normal_balance: NormalBalance
    currency_code: str
    opening_balance: MoneyStr
    total_debits: MoneyStr
    total_credits: MoneyStr
    closing_balance: MoneyStr
    lines: list[GeneralLedgerLine]
    has_more: bool = False


class GeneralLedgerReport(AppModel):
    meta: ReportMeta
    accounts: list[GeneralLedgerAccount]


# ------------------------------------------------------------ account balances
class AccountBalanceRow(AppModel):
    account_id: UUID
    account_number: str
    account_name: str
    account_type: AccountType
    normal_balance: NormalBalance
    period_id: UUID
    currency_code: str
    opening_balance: MoneyStr
    total_debits: MoneyStr
    total_credits: MoneyStr
    closing_balance: MoneyStr


class AccountBalancesReport(AppModel):
    meta: ReportMeta
    rows: list[AccountBalanceRow]
    #: Set when the projection disagrees with the journals — see
    #: `python -m scripts.rebuild_ledger`.
    projection_matches_journals: bool = True


# ----------------------------------------------------------- profit and loss
class ProfitAndLossLine(AppModel):
    account_id: UUID
    account_number: str
    account_name: str
    amount: MoneyStr


class ProfitAndLossReport(AppModel):
    meta: ReportMeta
    revenue: list[ProfitAndLossLine]
    expenses: list[ProfitAndLossLine]
    total_revenue: MoneyStr
    total_expenses: MoneyStr
    net_profit: MoneyStr


# --------------------------------------------------------------- balance sheet
class BalanceSheetLine(AppModel):
    account_id: UUID
    account_number: str
    account_name: str
    amount: MoneyStr


class BalanceSheetReport(AppModel):
    meta: ReportMeta
    assets: list[BalanceSheetLine]
    liabilities: list[BalanceSheetLine]
    equity: list[BalanceSheetLine]
    total_assets: MoneyStr
    total_liabilities: MoneyStr
    total_equity: MoneyStr
    #: Current-period earnings are folded into equity so the statement
    #: balances before a formal year-end close has been run.
    retained_earnings_current_period: MoneyStr
    equation_balanced: bool
    difference: MoneyStr


class ReportQuery(AppModel):
    """Shared report filters."""

    company_id: UUID
    period_id: UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    account_id: UUID | None = None
    currency_code: str | None = None
    include_zero_balances: bool = Field(
        default=False, description="Include accounts with no activity in the window."
    )
