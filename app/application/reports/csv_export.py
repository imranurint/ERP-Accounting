"""CSV rendering for reports.

Streamed row by row so a multi-year general ledger never has to be
materialised in memory. Amounts are written as plain decimal strings with no
thousands separators — the file is for machines and spreadsheets, not for
display.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from decimal import Decimal

from app.application.reports.schemas import (
    AccountBalancesReport,
    BalanceSheetReport,
    GeneralLedgerReport,
    ProfitAndLossReport,
    TrialBalanceReport,
)


def _money(value: Decimal) -> str:
    return f"{value:f}"


class _Writer:
    """Adapter that lets csv.writer emit into a generator."""

    def __init__(self) -> None:
        self.buffer = io.StringIO()
        self.writer = csv.writer(self.buffer, lineterminator="\n")

    def row(self, values: list[str]) -> str:
        self.buffer.seek(0)
        self.buffer.truncate(0)
        self.writer.writerow(values)
        return self.buffer.getvalue()


def trial_balance_csv(report: TrialBalanceReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(
        [
            "account_number",
            "account_name",
            "account_type",
            "currency_code",
            "opening_balance",
            "total_debits",
            "total_credits",
            "closing_balance",
        ]
    )
    for row in report.rows:
        yield out.row(
            [
                row.account_number,
                row.account_name,
                str(row.account_type),
                row.currency_code,
                _money(row.opening_balance),
                _money(row.total_debits),
                _money(row.total_credits),
                _money(row.closing_balance),
            ]
        )
    yield out.row(
        ["TOTAL", "", "", "", "", _money(report.total_debits), _money(report.total_credits), ""]
    )


def general_ledger_csv(report: GeneralLedgerReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(
        [
            "account_number",
            "account_name",
            "entry_date",
            "entry_number",
            "reference",
            "source_type",
            "source_document",
            "memo",
            "debit",
            "credit",
            "running_balance",
        ]
    )
    for account in report.accounts:
        yield out.row(
            [
                account.account_number,
                account.account_name,
                "",
                "OPENING BALANCE",
                "",
                "",
                "",
                "",
                "",
                "",
                _money(account.opening_balance),
            ]
        )
        for line in account.lines:
            yield out.row(
                [
                    account.account_number,
                    account.account_name,
                    line.entry_date.isoformat(),
                    line.entry_number or "",
                    line.reference or "",
                    line.source_type or "",
                    line.source_document or "",
                    line.memo or "",
                    _money(line.debit),
                    _money(line.credit),
                    _money(line.running_balance),
                ]
            )
        yield out.row(
            [
                account.account_number,
                account.account_name,
                "",
                "CLOSING BALANCE",
                "",
                "",
                "",
                "",
                _money(account.total_debits),
                _money(account.total_credits),
                _money(account.closing_balance),
            ]
        )


def account_balances_csv(report: AccountBalancesReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(
        [
            "account_number",
            "account_name",
            "account_type",
            "currency_code",
            "opening_balance",
            "total_debits",
            "total_credits",
            "closing_balance",
        ]
    )
    for row in report.rows:
        yield out.row(
            [
                row.account_number,
                row.account_name,
                str(row.account_type),
                row.currency_code,
                _money(row.opening_balance),
                _money(row.total_debits),
                _money(row.total_credits),
                _money(row.closing_balance),
            ]
        )


def profit_and_loss_csv(report: ProfitAndLossReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(["section", "account_number", "account_name", "amount"])
    for line in report.revenue:
        yield out.row(["REVENUE", line.account_number, line.account_name, _money(line.amount)])
    yield out.row(["REVENUE", "", "Total revenue", _money(report.total_revenue)])
    for line in report.expenses:
        yield out.row(["EXPENSE", line.account_number, line.account_name, _money(line.amount)])
    yield out.row(["EXPENSE", "", "Total expenses", _money(report.total_expenses)])
    yield out.row(["RESULT", "", "Net profit / (loss)", _money(report.net_profit)])


def balance_sheet_csv(report: BalanceSheetReport) -> Iterator[str]:
    out = _Writer()
    yield out.row(["section", "account_number", "account_name", "amount"])
    for section, lines, total in (
        ("ASSET", report.assets, report.total_assets),
        ("LIABILITY", report.liabilities, report.total_liabilities),
        ("EQUITY", report.equity, report.total_equity),
    ):
        for line in lines:
            yield out.row([section, line.account_number, line.account_name, _money(line.amount)])
        yield out.row([section, "", f"Total {section.lower()}", _money(total)])
    yield out.row(
        [
            "EQUITY",
            "",
            "Current period earnings",
            _money(report.retained_earnings_current_period),
        ]
    )
    yield out.row(["CHECK", "", "Assets - (Liabilities + Equity)", _money(report.difference)])
