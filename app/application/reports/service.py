"""Financial reporting.

All figures are derived from posted journal activity. Reports never mutate
anything and never trust a cached balance except in
`account_balances`, which exists precisely to expose the projection so it can
be compared against the journals.

Window resolution: a `period_id` wins; otherwise `start_date`/`end_date` are
used; a missing `end_date` means "up to today".
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.application.reports.schemas import (
    AccountBalanceRow,
    AccountBalancesReport,
    BalanceSheetLine,
    BalanceSheetReport,
    GeneralLedgerAccount,
    GeneralLedgerLine,
    GeneralLedgerReport,
    ProfitAndLossLine,
    ProfitAndLossReport,
    ReportMeta,
    ReportQuery,
    TrialBalanceReport,
    TrialBalanceRow,
)
from app.core.exceptions import NotFoundError, ValidationError
from app.core.money import ZERO
from app.domain.accounting.enums import AccountType, NormalBalance
from app.infrastructure.database.models.organization import Company
from app.infrastructure.database.repositories.reports import AccountActivityRow
from app.infrastructure.database.unit_of_work import UnitOfWork

MAX_GL_LINES_PER_ACCOUNT = 1000


class ReportService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    # ------------------------------------------------------------ trial balance
    async def trial_balance(self, query: ReportQuery) -> TrialBalanceReport:
        company, start, end = await self._resolve_window(query)

        activity = await self.uow.reports.account_activity(
            query.company_id,
            start_date=start,
            end_date=end,
            currency_code=query.currency_code,
            account_ids=[query.account_id] if query.account_id else None,
        )
        openings = (
            await self.uow.reports.opening_balances(
                query.company_id,
                before=start,
                currency_code=query.currency_code,
                account_ids=[query.account_id] if query.account_id else None,
            )
            if start is not None
            else {}
        )

        rows: list[TrialBalanceRow] = []
        total_debits = ZERO
        total_credits = ZERO

        for row in activity:
            opening = openings.get((row.account_id, row.currency_code), ZERO)
            if not query.include_zero_balances and _is_empty(row, opening):
                continue
            rows.append(
                TrialBalanceRow(
                    account_id=row.account_id,
                    account_number=row.account_number,
                    account_name=row.account_name,
                    account_type=row.account_type,
                    normal_balance=row.normal_balance,
                    currency_code=row.currency_code,
                    opening_balance=opening,
                    total_debits=row.total_debits,
                    total_credits=row.total_credits,
                    closing_balance=opening + row.net,
                )
            )
            total_debits += row.total_debits
            total_credits += row.total_credits

        difference = total_debits - total_credits
        return TrialBalanceReport(
            meta=self._meta(company, query, start, end),
            rows=rows,
            total_debits=total_debits,
            total_credits=total_credits,
            is_balanced=difference == ZERO,
            difference=difference,
        )

    # ----------------------------------------------------------- general ledger
    async def general_ledger(self, query: ReportQuery) -> GeneralLedgerReport:
        company, start, end = await self._resolve_window(query)

        activity = await self.uow.reports.account_activity(
            query.company_id,
            start_date=start,
            end_date=end,
            currency_code=query.currency_code,
            account_ids=[query.account_id] if query.account_id else None,
        )
        openings = (
            await self.uow.reports.opening_balances(
                query.company_id,
                before=start,
                currency_code=query.currency_code,
                account_ids=[query.account_id] if query.account_id else None,
            )
            if start is not None
            else {}
        )

        accounts: list[GeneralLedgerAccount] = []
        for row in activity:
            opening = openings.get((row.account_id, row.currency_code), ZERO)
            lines = await self.uow.reports.general_ledger_lines(
                query.company_id,
                row.account_id,
                start_date=start,
                end_date=end,
                currency_code=row.currency_code,
                limit=MAX_GL_LINES_PER_ACCOUNT + 1,
            )
            has_more = len(lines) > MAX_GL_LINES_PER_ACCOUNT
            lines = lines[:MAX_GL_LINES_PER_ACCOUNT]

            running = opening
            rendered: list[GeneralLedgerLine] = []
            for line in lines:
                movement = line.debit - line.credit
                running += movement if row.normal_balance is NormalBalance.DEBIT else -movement
                rendered.append(
                    GeneralLedgerLine(
                        entry_id=line.entry_id,
                        entry_number=line.entry_number,
                        entry_date=line.entry_date,
                        reference=line.reference,
                        source_type=line.source_type,
                        source_document=line.source_document,
                        line_number=line.line_number,
                        memo=line.memo,
                        debit=line.debit,
                        credit=line.credit,
                        running_balance=running,
                    )
                )

            accounts.append(
                GeneralLedgerAccount(
                    account_id=row.account_id,
                    account_number=row.account_number,
                    account_name=row.account_name,
                    account_type=row.account_type,
                    normal_balance=row.normal_balance,
                    currency_code=row.currency_code,
                    opening_balance=opening,
                    total_debits=row.total_debits,
                    total_credits=row.total_credits,
                    closing_balance=opening + row.net,
                    lines=rendered,
                    has_more=has_more,
                )
            )

        return GeneralLedgerReport(meta=self._meta(company, query, start, end), accounts=accounts)

    # ---------------------------------------------------------- account balances
    async def account_balances(self, query: ReportQuery) -> AccountBalancesReport:
        """Read the `ledger_balances` projection directly.

        Comparing this against `trial-balance` (which reads the journals) is
        the cheapest possible integrity check on the projection.
        """
        company, start, end = await self._resolve_window(query)
        if query.period_id is None:
            raise ValidationError(
                "account-balances requires period_id.",
                details={"parameter": "period_id"},
            )

        balances = await self.uow.ledger.list_for_period(
            query.company_id, query.period_id, currency_code=query.currency_code
        )
        accounts = await self.uow.accounts.get_many_by_id(
            query.company_id, [balance.account_id for balance in balances]
        )

        rows = [
            AccountBalanceRow(
                account_id=balance.account_id,
                account_number=accounts[balance.account_id].account_number,
                account_name=accounts[balance.account_id].account_name,
                account_type=accounts[balance.account_id].account_type,
                normal_balance=accounts[balance.account_id].normal_balance,
                period_id=balance.period_id,
                currency_code=balance.currency_code,
                opening_balance=balance.opening_balance,
                total_debits=balance.total_debits,
                total_credits=balance.total_credits,
                closing_balance=balance.closing_balance,
            )
            for balance in balances
            if balance.account_id in accounts
            and (query.account_id is None or balance.account_id == query.account_id)
        ]
        rows.sort(key=lambda row: (row.account_number, row.currency_code))

        journal_rows = await self.uow.reports.account_activity(
            query.company_id,
            start_date=None,
            end_date=None,
            currency_code=query.currency_code,
            period_id=query.period_id,
        )
        journal_totals = {
            (row.account_id, row.currency_code): (row.total_debits, row.total_credits)
            for row in journal_rows
        }
        matches = all(
            journal_totals.get((row.account_id, row.currency_code))
            == (row.total_debits, row.total_credits)
            for row in rows
        ) and len(journal_totals) == len(rows)

        return AccountBalancesReport(
            meta=self._meta(company, query, start, end),
            rows=rows,
            projection_matches_journals=matches,
        )

    # ----------------------------------------------------------- profit and loss
    async def profit_and_loss(self, query: ReportQuery) -> ProfitAndLossReport:
        company, start, end = await self._resolve_window(query)

        activity = await self.uow.reports.account_activity(
            query.company_id,
            start_date=start,
            end_date=end,
            currency_code=query.currency_code,
            account_types=[AccountType.REVENUE, AccountType.EXPENSE],
        )

        revenue: list[ProfitAndLossLine] = []
        expenses: list[ProfitAndLossLine] = []
        total_revenue = ZERO
        total_expenses = ZERO

        for row in activity:
            amount = row.net
            if amount == ZERO and not query.include_zero_balances:
                continue
            line = ProfitAndLossLine(
                account_id=row.account_id,
                account_number=row.account_number,
                account_name=row.account_name,
                amount=amount,
            )
            if row.account_type is AccountType.REVENUE:
                revenue.append(line)
                total_revenue += amount
            else:
                expenses.append(line)
                total_expenses += amount

        return ProfitAndLossReport(
            meta=self._meta(company, query, start, end),
            revenue=revenue,
            expenses=expenses,
            total_revenue=total_revenue,
            total_expenses=total_expenses,
            net_profit=total_revenue - total_expenses,
        )

    # ------------------------------------------------------------- balance sheet
    async def balance_sheet(self, query: ReportQuery) -> BalanceSheetReport:
        """As at `end_date`, cumulative from the beginning of the ledger."""
        company, _, end = await self._resolve_window(query)
        as_at = end or datetime.now(UTC).date()

        activity = await self.uow.reports.account_activity(
            query.company_id,
            start_date=None,
            end_date=as_at,
            currency_code=query.currency_code,
        )

        assets: list[BalanceSheetLine] = []
        liabilities: list[BalanceSheetLine] = []
        equity: list[BalanceSheetLine] = []
        totals = {
            AccountType.ASSET: ZERO,
            AccountType.LIABILITY: ZERO,
            AccountType.EQUITY: ZERO,
            AccountType.REVENUE: ZERO,
            AccountType.EXPENSE: ZERO,
        }

        for row in activity:
            amount = row.net
            totals[row.account_type] += amount
            if amount == ZERO and not query.include_zero_balances:
                continue
            line = BalanceSheetLine(
                account_id=row.account_id,
                account_number=row.account_number,
                account_name=row.account_name,
                amount=amount,
            )
            if row.account_type is AccountType.ASSET:
                assets.append(line)
            elif row.account_type is AccountType.LIABILITY:
                liabilities.append(line)
            elif row.account_type is AccountType.EQUITY:
                equity.append(line)

        # Revenue and expenses have not been closed to equity yet, so fold the
        # period result in explicitly. Without this the equation would appear
        # unbalanced for any company that has traded since its last year-end.
        current_earnings = totals[AccountType.REVENUE] - totals[AccountType.EXPENSE]
        total_assets = totals[AccountType.ASSET]
        total_liabilities = totals[AccountType.LIABILITY]
        total_equity = totals[AccountType.EQUITY] + current_earnings
        difference = total_assets - (total_liabilities + total_equity)

        return BalanceSheetReport(
            meta=self._meta(company, query, None, as_at),
            assets=assets,
            liabilities=liabilities,
            equity=equity,
            total_assets=total_assets,
            total_liabilities=total_liabilities,
            total_equity=total_equity,
            retained_earnings_current_period=current_earnings,
            equation_balanced=difference == ZERO,
            difference=difference,
        )

    # -------------------------------------------------------------- internals
    async def _resolve_window(self, query: ReportQuery) -> tuple[Company, date | None, date | None]:
        company = await self.uow.companies.get(query.company_id)
        if company is None:
            raise NotFoundError("Company not found.", details={"company_id": str(query.company_id)})

        if query.period_id is not None:
            period = await self.uow.fiscal_periods.get_for_company(
                query.company_id, query.period_id
            )
            if period is None:
                raise NotFoundError(
                    "Fiscal period not found.",
                    details={"period_id": str(query.period_id)},
                )
            return company, period.start_date, period.end_date

        if (
            query.start_date is not None
            and query.end_date is not None
            and query.start_date > query.end_date
        ):
            raise ValidationError(
                "start_date must be on or before end_date.",
                details={
                    "start_date": query.start_date.isoformat(),
                    "end_date": query.end_date.isoformat(),
                },
            )
        return company, query.start_date, query.end_date

    @staticmethod
    def _meta(
        company: Company, query: ReportQuery, start: date | None, end: date | None
    ) -> ReportMeta:
        return ReportMeta(
            company_id=company.id,
            company_name=company.name,
            currency_code=query.currency_code,
            start_date=start,
            end_date=end,
            period_id=query.period_id,
            generated_at=datetime.now(UTC),
        )


def _is_empty(row: AccountActivityRow, opening: Decimal) -> bool:
    return row.total_debits == ZERO and row.total_credits == ZERO and opening == ZERO
