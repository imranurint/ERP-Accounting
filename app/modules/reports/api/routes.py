"""Financial reporting endpoints.

Every report supports `format=json` (default) and `format=csv`. CSV is
streamed, so exporting a full-year general ledger does not buffer the whole
document in memory.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.core.dependencies import UowDep, require
from app.modules.identity.domain.permissions import Permission
from app.modules.reports.application import csv_export
from app.modules.reports.application.schemas import (
    AccountBalancesReport,
    BalanceSheetReport,
    GeneralLedgerReport,
    ProfitAndLossReport,
    ReportQuery,
    TrialBalanceReport,
)
from app.modules.reports.application.service import ReportService

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    dependencies=[Depends(require(Permission.REPORTS_READ))],
)

ReportFormat = Literal["json", "csv"]


def report_query(
    company_id: Annotated[UUID, Query()],
    period_id: Annotated[UUID | None, Query()] = None,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    account_id: Annotated[UUID | None, Query()] = None,
    currency_code: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    include_zero_balances: Annotated[bool, Query()] = False,
) -> ReportQuery:
    return ReportQuery(
        company_id=company_id,
        period_id=period_id,
        start_date=start_date,
        end_date=end_date,
        account_id=account_id,
        currency_code=currency_code.upper() if currency_code else None,
        include_zero_balances=include_zero_balances,
    )


QueryDep = Annotated[ReportQuery, Depends(report_query)]


def _csv_response(rows: Iterator[str], filename: str) -> StreamingResponse:
    return StreamingResponse(
        rows,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
    )


@router.get(
    "/trial-balance",
    # Explicit: the return annotation is a union with StreamingResponse for
    # the CSV variant, which FastAPI cannot turn into a response model.
    response_model=TrialBalanceReport,
    summary="Trial balance",
)
async def trial_balance(
    uow: UowDep,
    query: QueryDep,
    response_format: Annotated[ReportFormat, Query(alias="format")] = "json",
) -> TrialBalanceReport | StreamingResponse:
    """Debits and credits by account. `is_balanced` must always be true."""
    report = await ReportService(uow).trial_balance(query)
    if response_format == "csv":
        return _csv_response(csv_export.trial_balance_csv(report), "trial-balance")
    return report


@router.get(
    "/general-ledger",
    # Explicit: the return annotation is a union with StreamingResponse for
    # the CSV variant, which FastAPI cannot turn into a response model.
    response_model=GeneralLedgerReport,
    summary="General ledger",
)
async def general_ledger(
    uow: UowDep,
    query: QueryDep,
    response_format: Annotated[ReportFormat, Query(alias="format")] = "json",
) -> GeneralLedgerReport | StreamingResponse:
    """Posted activity per account with a running balance."""
    report = await ReportService(uow).general_ledger(query)
    if response_format == "csv":
        return _csv_response(csv_export.general_ledger_csv(report), "general-ledger")
    return report


@router.get(
    "/account-balances",
    # Explicit: the return annotation is a union with StreamingResponse for
    # the CSV variant, which FastAPI cannot turn into a response model.
    response_model=AccountBalancesReport,
    summary="Ledger balance projection",
)
async def account_balances(
    uow: UowDep,
    query: QueryDep,
    response_format: Annotated[ReportFormat, Query(alias="format")] = "json",
) -> AccountBalancesReport | StreamingResponse:
    """Reads `ledger_balances` directly and reports whether it agrees with the
    journals. Requires `period_id`."""
    report = await ReportService(uow).account_balances(query)
    if response_format == "csv":
        return _csv_response(csv_export.account_balances_csv(report), "account-balances")
    return report


@router.get(
    "/profit-and-loss",
    # Explicit: the return annotation is a union with StreamingResponse for
    # the CSV variant, which FastAPI cannot turn into a response model.
    response_model=ProfitAndLossReport,
    summary="Profit and loss",
)
async def profit_and_loss(
    uow: UowDep,
    query: QueryDep,
    response_format: Annotated[ReportFormat, Query(alias="format")] = "json",
) -> ProfitAndLossReport | StreamingResponse:
    report = await ReportService(uow).profit_and_loss(query)
    if response_format == "csv":
        return _csv_response(csv_export.profit_and_loss_csv(report), "profit-and-loss")
    return report


@router.get(
    "/balance-sheet",
    # Explicit: the return annotation is a union with StreamingResponse for
    # the CSV variant, which FastAPI cannot turn into a response model.
    response_model=BalanceSheetReport,
    summary="Balance sheet",
)
async def balance_sheet(
    uow: UowDep,
    query: QueryDep,
    response_format: Annotated[ReportFormat, Query(alias="format")] = "json",
) -> BalanceSheetReport | StreamingResponse:
    """As at `end_date`. `equation_balanced` asserts Assets = Liabilities + Equity."""
    report = await ReportService(uow).balance_sheet(query)
    if response_format == "csv":
        return _csv_response(csv_export.balance_sheet_csv(report), "balance-sheet")
    return report
