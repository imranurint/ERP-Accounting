"""Concurrency guarantees and report correctness."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.journal.commands import (
    ActorContext,
    CreateJournalCommand,
    JournalLineCommand,
)
from app.application.journal.posting_service import JournalPostingService
from app.application.reports.schemas import ReportQuery
from app.application.reports.service import ReportService
from app.domain.accounting.enums import AccountType
from app.infrastructure.database.models.journal import JournalEntry
from app.infrastructure.database.unit_of_work import UnitOfWork
from tests.factories import DemoData

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ACTOR = ActorContext(request_id="concurrency-test")


def simple_command(
    demo: DemoData,
    *,
    amount: str = "100.00",
    key: str | None = None,
    entry_date: date = date(2026, 3, 15),
    period_name: str = "March",
    debit_account: str = "101",
    credit_account: str = "402",
) -> CreateJournalCommand:
    return CreateJournalCommand(
        company_id=demo.company_id,
        entry_date=entry_date,
        currency_code="USD",
        period_id=demo.period(period_name).id,
        source_type="test",
        idempotency_key=key,
        actor=ACTOR,
        lines=[
            JournalLineCommand.of(1, debit=amount, account_number=debit_account),
            JournalLineCommand.of(2, credit=amount, account_number=credit_account),
        ],
    )


class TestConcurrentPosting:
    async def test_parallel_postings_produce_distinct_numbers_and_exact_balances(
        self, engine, demo: DemoData
    ) -> None:
        """Twenty concurrent postings, each on its own connection.

        This is the test that would fail under a read-modify-write ledger
        update or a `SELECT MAX()` numbering scheme.
        """
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )

        async def post_one(index: int) -> str | None:
            async with factory() as session:
                uow = UnitOfWork(session)
                entry = await JournalPostingService(uow).create(
                    simple_command(demo, amount="10.00", key=f"concurrent-{index}")
                )
                return entry.entry_number

        numbers = await asyncio.gather(*(post_one(i) for i in range(20)))

        assert len(set(numbers)) == 20, "journal numbers must be unique"

        async with factory() as session:
            uow = UnitOfWork(session)
            bank = await uow.ledger.get(
                demo.company_id, demo.account("101").id, demo.period("March").id, "USD"
            )
            assert bank is not None
            # 20 x 10.00 — no lost updates.
            assert bank.total_debits == Decimal("200.0000")
            assert bank.closing_balance == Decimal("200.0000")

            count = await session.scalar(
                select(func.count())
                .select_from(JournalEntry)
                .where(JournalEntry.company_id == demo.company_id)
            )
            assert count == 20

    async def test_concurrent_replays_of_one_key_create_exactly_one_entry(
        self, engine, demo: DemoData
    ) -> None:
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )

        async def post_same() -> None:
            async with factory() as session:
                uow = UnitOfWork(session)
                # A loser of the unique-index race is an acceptable outcome;
                # what matters is that only one entry exists afterwards.
                with contextlib.suppress(Exception):
                    await JournalPostingService(uow).create(simple_command(demo, key="the-one-key"))

        await asyncio.gather(*(post_same() for _ in range(8)))

        async with factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(JournalEntry)
                .where(JournalEntry.company_id == demo.company_id)
            )
            assert count == 1


class TestReports:
    async def _post_trading_activity(self, uow: UnitOfWork, demo: DemoData) -> None:
        service = JournalPostingService(uow)
        # Owner injects capital: Dr Bank 1000 / Cr Equity 1000
        await service.create(
            simple_command(
                demo,
                amount="1000.00",
                key="capital",
                entry_date=date(2026, 1, 5),
                period_name="January",
                debit_account="101",
                credit_account="300",
            )
        )
        # Sale: Dr Bank 500 / Cr Revenue 500
        await service.create(
            simple_command(
                demo,
                amount="500.00",
                key="sale",
                entry_date=date(2026, 3, 10),
                debit_account="101",
                credit_account="402",
            )
        )
        # Expense: Dr Operating Expenses 200 / Cr Bank 200
        await service.create(
            simple_command(
                demo,
                amount="200.00",
                key="expense",
                entry_date=date(2026, 3, 20),
                debit_account="600",
                credit_account="101",
            )
        )

    async def test_profit_and_loss(self, uow, demo: DemoData) -> None:
        await self._post_trading_activity(uow, demo)

        report = await ReportService(uow).profit_and_loss(
            ReportQuery(
                company_id=demo.company_id,
                start_date=date(2026, 1, 1),
                end_date=date(2026, 12, 31),
            )
        )
        assert report.total_revenue == Decimal("500.0000")
        assert report.total_expenses == Decimal("200.0000")
        assert report.net_profit == Decimal("300.0000")
        assert {line.account_number for line in report.revenue} == {"402"}
        assert {line.account_number for line in report.expenses} == {"600"}

    async def test_balance_sheet_equation_holds(self, uow, demo: DemoData) -> None:
        await self._post_trading_activity(uow, demo)

        report = await ReportService(uow).balance_sheet(
            ReportQuery(company_id=demo.company_id, end_date=date(2026, 12, 31))
        )
        # Bank: 1000 + 500 - 200 = 1300
        assert report.total_assets == Decimal("1300.0000")
        assert report.total_liabilities == Decimal("0.0000")
        # Equity 1000 + current earnings 300
        assert report.total_equity == Decimal("1300.0000")
        assert report.retained_earnings_current_period == Decimal("300.0000")
        assert report.equation_balanced
        assert report.difference == Decimal("0.0000")

    async def test_trial_balance_across_the_whole_year_ties(self, uow, demo: DemoData) -> None:
        await self._post_trading_activity(uow, demo)

        report = await ReportService(uow).trial_balance(
            ReportQuery(
                company_id=demo.company_id,
                start_date=date(2026, 1, 1),
                end_date=date(2026, 12, 31),
            )
        )
        assert report.is_balanced
        assert report.total_debits == Decimal("1700.0000")
        assert report.total_credits == Decimal("1700.0000")

    async def test_opening_balance_carries_into_a_later_period(self, uow, demo: DemoData) -> None:
        await self._post_trading_activity(uow, demo)

        march = await ReportService(uow).trial_balance(
            ReportQuery(company_id=demo.company_id, period_id=demo.period("March").id)
        )
        bank = next(row for row in march.rows if row.account_number == "101")
        # January's 1000 is the opening balance for March.
        assert bank.opening_balance == Decimal("1000.0000")
        assert bank.total_debits == Decimal("500.0000")
        assert bank.total_credits == Decimal("200.0000")
        assert bank.closing_balance == Decimal("1300.0000")

    async def test_general_ledger_running_balance(self, uow, demo: DemoData) -> None:
        await self._post_trading_activity(uow, demo)

        report = await ReportService(uow).general_ledger(
            ReportQuery(
                company_id=demo.company_id,
                account_id=demo.account("101").id,
                start_date=date(2026, 1, 1),
                end_date=date(2026, 12, 31),
            )
        )
        assert len(report.accounts) == 1
        account = report.accounts[0]
        assert [line.running_balance for line in account.lines] == [
            Decimal("1000.0000"),
            Decimal("1500.0000"),
            Decimal("1300.0000"),
        ]
        assert account.closing_balance == Decimal("1300.0000")

    async def test_account_balances_projection_agrees_with_journals(
        self, uow, demo: DemoData
    ) -> None:
        await self._post_trading_activity(uow, demo)

        report = await ReportService(uow).account_balances(
            ReportQuery(company_id=demo.company_id, period_id=demo.period("March").id)
        )
        assert report.projection_matches_journals
        bank = next(row for row in report.rows if row.account_number == "101")
        assert bank.total_debits == Decimal("500.0000")
        assert bank.total_credits == Decimal("200.0000")

    async def test_drafts_are_excluded_from_every_report(self, uow, demo: DemoData) -> None:
        command = simple_command(demo, amount="9999.00", key="draft-only")
        command.post_immediately = False
        await JournalPostingService(uow).create(command)

        report = await ReportService(uow).trial_balance(
            ReportQuery(company_id=demo.company_id, period_id=demo.period("March").id)
        )
        assert report.rows == []
        assert report.total_debits == Decimal("0.0000")

    async def test_report_only_covers_the_requested_company(
        self, uow, demo: DemoData, session
    ) -> None:
        await self._post_trading_activity(uow, demo)
        other = await DemoData.create(session, company_name="Other Company Ltd")

        report = await ReportService(uow).trial_balance(
            ReportQuery(
                company_id=other.company_id,
                start_date=date(2026, 1, 1),
                end_date=date(2026, 12, 31),
            )
        )
        assert report.rows == []


class TestAccountTypeSemantics:
    async def test_credit_normal_accounts_increase_on_credits(self, uow, demo: DemoData) -> None:
        await JournalPostingService(uow).create(
            simple_command(demo, amount="250.00", key="revenue-check")
        )
        report = await ReportService(uow).trial_balance(
            ReportQuery(company_id=demo.company_id, period_id=demo.period("March").id)
        )
        revenue = next(row for row in report.rows if row.account_number == "402")
        assert revenue.account_type is AccountType.REVENUE
        assert revenue.total_credits == Decimal("250.0000")
        assert revenue.closing_balance == Decimal("250.0000")
