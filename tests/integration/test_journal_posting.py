"""Integration tests for the posting pipeline, against real PostgreSQL.

These cover the required scenarios end to end: the invoice example, ledger
updates, period locking, idempotency, immutability, reversal and rebuild.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.application.journal.commands import (
    ActorContext,
    CreateJournalCommand,
    JournalLineCommand,
    PostJournalCommand,
    ReverseJournalCommand,
    VoidJournalCommand,
)
from app.application.journal.posting_service import JournalPostingService
from app.application.journal.reversal_service import JournalReversalService
from app.application.journal.schemas import JournalEntryUpdate
from app.application.journal.service import JournalService
from app.application.reports.schemas import ReportQuery
from app.application.reports.service import ReportService
from app.domain.accounting.enums import FiscalPeriodStatus, JournalStatus
from app.domain.accounting.exceptions import (
    DuplicateIdempotencyKeyError,
    FiscalPeriodClosedError,
    InvalidJournalLineError,
    JournalAlreadyReversedError,
    PostedEntryCannotBeDeletedError,
    PostedEntryCannotBeModifiedError,
    UnbalancedJournalEntryError,
)
from app.infrastructure.database.models.audit import AuditLog
from app.infrastructure.database.models.journal import JournalEntry
from app.infrastructure.outbox.models import OutboxEvent
from tests.factories import DemoData, set_account_active, set_period_status

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MARCH_31 = date(2026, 3, 31)
ACTOR = ActorContext(request_id="test-request", ip_address="127.0.0.1", user_agent="pytest")


def invoice_command(
    demo: DemoData,
    *,
    idempotency_key: str | None = "sales_invoice:INV-9923",
    post: bool = True,
    entry_date: date = MARCH_31,
) -> CreateJournalCommand:
    """The canonical customer invoice: Dr Bank 115 / Cr Revenue 100 + Tax 15."""
    return CreateJournalCommand(
        company_id=demo.company_id,
        entry_date=entry_date,
        currency_code="USD",
        period_id=demo.period("March").id,
        source_type="sales_invoice",
        source_document="INV-9923",
        reference="Customer invoice INV-9923",
        idempotency_key=idempotency_key,
        post_immediately=post,
        actor=ACTOR,
        lines=[
            JournalLineCommand.of(1, debit="115.00", account_number="101"),
            JournalLineCommand.of(2, credit="100.00", account_number="402"),
            JournalLineCommand.of(3, credit="15.00", account_number="205", tax_code="VAT15"),
        ],
    )


# ---------------------------------------------------------------------------
class TestInvoicePosting:
    async def test_entry_is_posted_numbered_and_balanced(self, uow, demo: DemoData) -> None:
        entry = await JournalPostingService(uow).create(invoice_command(demo))

        assert entry.status is JournalStatus.POSTED
        assert entry.entry_number == "JE-2026-000001"
        assert entry.posted_at is not None
        assert len(entry.items) == 3

        debits = sum(item.debit for item in entry.items)
        credits = sum(item.credit for item in entry.items)
        assert debits == Decimal("115.0000")
        assert credits == Decimal("115.0000")
        assert debits == credits

    async def test_lines_carry_the_expected_amounts(self, uow, demo: DemoData) -> None:
        entry = await JournalPostingService(uow).create(invoice_command(demo))
        by_account = {item.account_id: item for item in entry.items}

        assert by_account[demo.account("101").id].debit == Decimal("115.0000")
        assert by_account[demo.account("402").id].credit == Decimal("100.0000")
        assert by_account[demo.account("205").id].credit == Decimal("15.0000")
        assert by_account[demo.account("205").id].tax_id == demo.tax_codes["VAT15"].id

    async def test_ledger_balances_are_updated(self, uow, demo: DemoData) -> None:
        await JournalPostingService(uow).create(invoice_command(demo))
        period_id = demo.period("March").id

        bank = await uow.ledger.get(demo.company_id, demo.account("101").id, period_id, "USD")
        assert bank is not None
        assert bank.total_debits == Decimal("115.0000")
        assert bank.total_credits == Decimal("0.0000")
        # ASSET is debit-normal: closing = debits - credits.
        assert bank.closing_balance == Decimal("115.0000")

        revenue = await uow.ledger.get(demo.company_id, demo.account("402").id, period_id, "USD")
        assert revenue is not None
        # REVENUE is credit-normal: closing = credits - debits.
        assert revenue.closing_balance == Decimal("100.0000")

        tax = await uow.ledger.get(demo.company_id, demo.account("205").id, period_id, "USD")
        assert tax is not None
        assert tax.closing_balance == Decimal("15.0000")

    async def test_trial_balance_ties(self, uow, demo: DemoData) -> None:
        await JournalPostingService(uow).create(invoice_command(demo))

        report = await ReportService(uow).trial_balance(
            ReportQuery(company_id=demo.company_id, period_id=demo.period("March").id)
        )
        assert report.is_balanced
        assert report.total_debits == report.total_credits == Decimal("115.0000")
        assert report.difference == Decimal("0.0000")

    async def test_audit_log_and_outbox_event_are_written_in_the_same_transaction(
        self, uow, demo: DemoData
    ) -> None:
        entry = await JournalPostingService(uow).create(invoice_command(demo))

        audits = (
            (await uow.session.execute(select(AuditLog).where(AuditLog.entity_id == str(entry.id))))
            .scalars()
            .all()
        )
        assert [row.action for row in audits] == ["JOURNAL_POSTED"]
        assert audits[0].payload["total_debits"] == "115.0000"
        assert "password" not in str(audits[0].payload).lower()

        events = (
            (
                await uow.session.execute(
                    select(OutboxEvent).where(OutboxEvent.aggregate_id == entry.id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.event_type for row in events] == ["JournalPosted"]
        assert events[0].payload["entry_number"] == "JE-2026-000001"

    async def test_entry_numbers_increment_per_company_and_year(self, uow, demo: DemoData) -> None:
        service = JournalPostingService(uow)
        numbers = []
        for index in range(3):
            command = invoice_command(demo, idempotency_key=f"invoice-{index}")
            command.source_document = f"INV-{index}"
            entry = await service.create(command)
            numbers.append(entry.entry_number)

        assert numbers == ["JE-2026-000001", "JE-2026-000002", "JE-2026-000003"]


# ---------------------------------------------------------------------------
class TestValidationRejections:
    async def test_unbalanced_entry_is_rejected(self, uow, demo: DemoData) -> None:
        command = invoice_command(demo, idempotency_key=None)
        command.lines[2].credit = Decimal("14.00")  # 115 != 114

        with pytest.raises(UnbalancedJournalEntryError) as exc:
            await JournalPostingService(uow).create(command)
        assert exc.value.details["difference"] == "1.0000"

        assert await _entry_count(uow, demo) == 0

    async def test_line_with_both_debit_and_credit_is_rejected(self, uow, demo: DemoData) -> None:
        command = invoice_command(demo, idempotency_key=None)
        command.lines[0].credit = Decimal("115.00")

        with pytest.raises(InvalidJournalLineError):
            await JournalPostingService(uow).create(command)
        assert await _entry_count(uow, demo) == 0

    async def test_line_with_no_amount_is_rejected(self, uow, demo: DemoData) -> None:
        command = invoice_command(demo, idempotency_key=None)
        command.lines[0].debit = Decimal("0")
        command.lines[1].credit = Decimal("0")

        with pytest.raises(InvalidJournalLineError):
            await JournalPostingService(uow).create(command)

    async def test_posting_to_a_non_postable_account_is_rejected(self, uow, demo: DemoData) -> None:
        from app.domain.accounting.exceptions import AccountNotPostableError

        command = invoice_command(demo, idempotency_key=None)
        command.lines[0].account_number = "100"  # roll-up parent

        with pytest.raises(AccountNotPostableError):
            await JournalPostingService(uow).create(command)

    async def test_posting_to_an_inactive_account_is_rejected(self, uow, demo: DemoData) -> None:
        from app.domain.accounting.exceptions import AccountInactiveError

        await set_account_active(uow.session, demo.account("101").id, active=False)

        with pytest.raises(AccountInactiveError):
            await JournalPostingService(uow).create(invoice_command(demo, idempotency_key=None))

    async def test_entry_date_outside_the_period_is_rejected(self, uow, demo: DemoData) -> None:
        from app.domain.accounting.exceptions import FiscalPeriodNotFoundForDateError

        command = invoice_command(demo, idempotency_key=None, entry_date=date(2026, 4, 1))
        # period_id still points at March.
        with pytest.raises(FiscalPeriodNotFoundForDateError):
            await JournalPostingService(uow).create(command)


# ---------------------------------------------------------------------------
class TestFiscalPeriodEnforcement:
    @pytest.mark.parametrize("status", [FiscalPeriodStatus.CLOSED, FiscalPeriodStatus.LOCKED])
    async def test_closed_period_blocks_posting(
        self, uow, demo: DemoData, status: FiscalPeriodStatus
    ) -> None:
        await set_period_status(uow.session, demo.period("March").id, status)

        with pytest.raises(FiscalPeriodClosedError) as exc:
            await JournalPostingService(uow).create(invoice_command(demo, idempotency_key=None))
        assert exc.value.code == "FISCAL_PERIOD_CLOSED"
        assert await _entry_count(uow, demo) == 0

    async def test_adjusting_period_accepts_posting(self, uow, demo: DemoData) -> None:
        await set_period_status(uow.session, demo.period("March").id, FiscalPeriodStatus.ADJUSTING)

        entry = await JournalPostingService(uow).create(invoice_command(demo, idempotency_key=None))
        assert entry.status is JournalStatus.POSTED

    async def test_closing_a_period_after_posting_leaves_the_entry_intact(
        self, uow, demo: DemoData
    ) -> None:
        entry = await JournalPostingService(uow).create(invoice_command(demo))

        await set_period_status(uow.session, demo.period("March").id, FiscalPeriodStatus.CLOSED)

        stored = await uow.journal.get_with_items(demo.company_id, entry.id)
        assert stored is not None
        assert stored.status is JournalStatus.POSTED


# ---------------------------------------------------------------------------
class TestIdempotency:
    async def test_same_key_and_payload_returns_the_original_entry(
        self, uow, demo: DemoData
    ) -> None:
        service = JournalPostingService(uow)
        first = await service.create(invoice_command(demo))
        second = await service.create(invoice_command(demo))

        assert first.id == second.id
        assert first.entry_number == second.entry_number
        assert await _entry_count(uow, demo) == 1

    async def test_replay_does_not_double_the_ledger(self, uow, demo: DemoData) -> None:
        service = JournalPostingService(uow)
        await service.create(invoice_command(demo))
        await service.create(invoice_command(demo))

        bank = await uow.ledger.get(
            demo.company_id, demo.account("101").id, demo.period("March").id, "USD"
        )
        assert bank is not None
        assert bank.total_debits == Decimal("115.0000")

    async def test_same_key_different_payload_conflicts(self, uow, demo: DemoData) -> None:
        service = JournalPostingService(uow)
        await service.create(invoice_command(demo))

        altered = invoice_command(demo)
        altered.lines[0].debit = Decimal("230.00")
        altered.lines[1].credit = Decimal("200.00")
        altered.lines[2].credit = Decimal("30.00")

        with pytest.raises(DuplicateIdempotencyKeyError) as exc:
            await service.create(altered)
        assert exc.value.status_code == 409
        assert exc.value.code == "DUPLICATE_IDEMPOTENCY_KEY"
        assert await _entry_count(uow, demo) == 1

    async def test_amount_spelling_does_not_break_idempotency(self, uow, demo: DemoData) -> None:
        service = JournalPostingService(uow)
        first = await service.create(invoice_command(demo))

        restated = invoice_command(demo)
        restated.lines[0].debit = Decimal("115.0000")
        restated.lines[1].credit = Decimal("100")
        second = await service.create(restated)

        assert first.id == second.id

    async def test_entries_without_a_key_are_not_deduplicated(self, uow, demo: DemoData) -> None:
        service = JournalPostingService(uow)
        first = await service.create(invoice_command(demo, idempotency_key=None))
        second = await service.create(invoice_command(demo, idempotency_key=None))

        assert first.id != second.id
        assert await _entry_count(uow, demo) == 2


# ---------------------------------------------------------------------------
class TestImmutability:
    async def test_posted_entry_cannot_be_edited(self, uow, demo: DemoData) -> None:
        entry = await JournalPostingService(uow).create(invoice_command(demo))
        # Read the id up front: the rejected edit rolls its transaction back,
        # which expires ORM instances loaded in this shared test session.
        entry_id = entry.id

        with pytest.raises(PostedEntryCannotBeModifiedError) as exc:
            await JournalService(uow).update_draft(
                demo.company_id,
                entry_id,
                JournalEntryUpdate(reference="tampered"),
                ACTOR,
            )
        assert exc.value.code == "POSTED_ENTRY_CANNOT_BE_MODIFIED"

        stored = await uow.journal.get_with_items(demo.company_id, entry_id)
        assert stored is not None
        assert stored.reference == "Customer invoice INV-9923"

    async def test_posted_entry_cannot_be_deleted(self, uow, demo: DemoData) -> None:
        entry = await JournalPostingService(uow).create(invoice_command(demo))
        entry_id = entry.id

        with pytest.raises(PostedEntryCannotBeDeletedError):
            await JournalService(uow).delete_draft(demo.company_id, entry_id, ACTOR)

        assert await _entry_count(uow, demo) == 1

    async def test_posted_entry_cannot_be_voided(self, uow, demo: DemoData) -> None:
        from app.domain.accounting.exceptions import InvalidJournalStateError

        entry = await JournalPostingService(uow).create(invoice_command(demo))
        with pytest.raises(InvalidJournalStateError):
            await JournalPostingService(uow).void(
                VoidJournalCommand(company_id=demo.company_id, entry_id=entry.id, actor=ACTOR)
            )


# ---------------------------------------------------------------------------
class TestDraftLifecycle:
    async def test_draft_has_no_ledger_impact(self, uow, demo: DemoData) -> None:
        entry = await JournalPostingService(uow).create(
            invoice_command(demo, post=False, idempotency_key=None)
        )
        assert entry.status is JournalStatus.DRAFT
        assert entry.entry_number is None

        balances = await uow.ledger.list_for_period(demo.company_id, demo.period("March").id)
        assert balances == []

    async def test_draft_can_be_edited_then_posted(self, uow, demo: DemoData) -> None:
        draft = await JournalPostingService(uow).create(
            invoice_command(demo, post=False, idempotency_key=None)
        )
        draft_id, draft_version = draft.id, draft.version

        updated = await JournalService(uow).update_draft(
            demo.company_id,
            draft_id,
            JournalEntryUpdate(reference="Corrected reference", version=draft_version),
            ACTOR,
        )
        assert updated.reference == "Corrected reference"
        assert updated.version == draft_version + 1

        posted = await JournalPostingService(uow).post(
            PostJournalCommand(company_id=demo.company_id, entry_id=draft_id, actor=ACTOR)
        )
        assert posted.status is JournalStatus.POSTED
        assert posted.entry_number == "JE-2026-000001"

    async def test_draft_can_be_deleted(self, uow, demo: DemoData) -> None:
        draft = await JournalPostingService(uow).create(
            invoice_command(demo, post=False, idempotency_key=None)
        )
        await JournalService(uow).delete_draft(demo.company_id, draft.id, ACTOR)
        assert await _entry_count(uow, demo) == 0

    async def test_draft_can_be_voided(self, uow, demo: DemoData) -> None:
        draft = await JournalPostingService(uow).create(
            invoice_command(demo, post=False, idempotency_key=None)
        )
        voided = await JournalPostingService(uow).void(
            VoidJournalCommand(
                company_id=demo.company_id, entry_id=draft.id, reason="keyed twice", actor=ACTOR
            )
        )
        assert voided.status is JournalStatus.VOID
        assert voided.entry_number is None

        balances = await uow.ledger.list_for_period(demo.company_id, demo.period("March").id)
        assert balances == []

    async def test_posting_a_draft_into_a_closed_period_is_rejected(
        self, uow, demo: DemoData
    ) -> None:
        draft = await JournalPostingService(uow).create(
            invoice_command(demo, post=False, idempotency_key=None)
        )
        draft_id = draft.id
        await set_period_status(uow.session, demo.period("March").id, FiscalPeriodStatus.CLOSED)

        with pytest.raises(FiscalPeriodClosedError):
            await JournalPostingService(uow).post(
                PostJournalCommand(company_id=demo.company_id, entry_id=draft_id, actor=ACTOR)
            )

        stored = await uow.journal.get_with_items(demo.company_id, draft_id)
        assert stored is not None
        assert stored.status is JournalStatus.DRAFT
        assert stored.entry_number is None


# ---------------------------------------------------------------------------
class TestReversal:
    async def test_reversal_mirrors_every_line(self, uow, demo: DemoData) -> None:
        original = await JournalPostingService(uow).create(invoice_command(demo))

        _, reversal = await JournalReversalService(uow).reverse(
            ReverseJournalCommand(company_id=demo.company_id, entry_id=original.id, actor=ACTOR)
        )

        by_account = {item.account_id: item for item in reversal.items}
        assert by_account[demo.account("101").id].credit == Decimal("115.0000")
        assert by_account[demo.account("402").id].debit == Decimal("100.0000")
        assert by_account[demo.account("205").id].debit == Decimal("15.0000")

    async def test_reversal_is_posted_and_linked_both_ways(self, uow, demo: DemoData) -> None:
        original = await JournalPostingService(uow).create(invoice_command(demo))
        updated, reversal = await JournalReversalService(uow).reverse(
            ReverseJournalCommand(company_id=demo.company_id, entry_id=original.id, actor=ACTOR)
        )

        assert reversal.status is JournalStatus.POSTED
        assert reversal.entry_number == "JE-2026-000002"
        assert updated.status is JournalStatus.REVERSED
        assert updated.reversed_by_entry_id == reversal.id
        assert reversal.reverses_entry_id == original.id

    async def test_ledger_nets_to_zero_after_reversal(self, uow, demo: DemoData) -> None:
        original = await JournalPostingService(uow).create(invoice_command(demo))
        await JournalReversalService(uow).reverse(
            ReverseJournalCommand(company_id=demo.company_id, entry_id=original.id, actor=ACTOR)
        )

        period_id = demo.period("March").id
        for number in ("101", "402", "205"):
            balance = await uow.ledger.get(
                demo.company_id, demo.account(number).id, period_id, "USD"
            )
            assert balance is not None
            assert balance.closing_balance == Decimal("0.0000"), number
            # The gross movement is preserved — reversal cancels, never erases.
            assert balance.total_debits > Decimal("0.0000")
            assert balance.total_credits > Decimal("0.0000")

    async def test_trial_balance_still_ties_after_reversal(self, uow, demo: DemoData) -> None:
        original = await JournalPostingService(uow).create(invoice_command(demo))
        await JournalReversalService(uow).reverse(
            ReverseJournalCommand(company_id=demo.company_id, entry_id=original.id, actor=ACTOR)
        )
        report = await ReportService(uow).trial_balance(
            ReportQuery(company_id=demo.company_id, period_id=demo.period("March").id)
        )
        assert report.is_balanced
        assert report.total_debits == Decimal("230.0000")
        assert report.total_credits == Decimal("230.0000")

    async def test_double_reversal_is_rejected(self, uow, demo: DemoData) -> None:
        original = await JournalPostingService(uow).create(invoice_command(demo))
        service = JournalReversalService(uow)
        await service.reverse(
            ReverseJournalCommand(company_id=demo.company_id, entry_id=original.id, actor=ACTOR)
        )
        with pytest.raises(JournalAlreadyReversedError):
            await service.reverse(
                ReverseJournalCommand(company_id=demo.company_id, entry_id=original.id, actor=ACTOR)
            )

    async def test_reversal_writes_audit_records_for_both_entries(
        self, uow, demo: DemoData
    ) -> None:
        original = await JournalPostingService(uow).create(invoice_command(demo))
        await JournalReversalService(uow).reverse(
            ReverseJournalCommand(company_id=demo.company_id, entry_id=original.id, actor=ACTOR)
        )
        actions = (
            (await uow.session.execute(select(AuditLog.action).order_by(AuditLog.created_at)))
            .scalars()
            .all()
        )
        assert "JOURNAL_REVERSED" in actions
        assert actions.count("JOURNAL_POSTED") == 2


# ---------------------------------------------------------------------------
class TestLedgerRebuild:
    async def test_rebuild_reproduces_the_same_balances(self, uow, demo: DemoData) -> None:
        from app.scripts.rebuild_ledger import rebuild_company

        service = JournalPostingService(uow)
        for index in range(5):
            command = invoice_command(demo, idempotency_key=f"invoice-{index}")
            command.source_document = f"INV-{index}"
            await service.create(command)

        before = {
            (row.account_id, row.period_id, row.currency_code): (
                row.total_debits,
                row.total_credits,
                row.closing_balance,
            )
            for row in await uow.ledger.list_for_company(demo.company_id)
        }
        assert before  # sanity: there is something to rebuild

        _, rows, matched = await rebuild_company(uow, demo.company_id, verify=True, dry_run=False)
        assert matched
        assert rows == len(before)

        after = {
            (row.account_id, row.period_id, row.currency_code): (
                row.total_debits,
                row.total_credits,
                row.closing_balance,
            )
            for row in await uow.ledger.list_for_company(demo.company_id)
        }
        assert after == before

    async def test_rebuild_repairs_a_corrupted_projection(self, uow, demo: DemoData) -> None:
        from app.scripts.rebuild_ledger import rebuild_company

        await JournalPostingService(uow).create(invoice_command(demo))

        # Simulate drift: something wrote to the projection out of band.
        bank = await uow.ledger.get(
            demo.company_id, demo.account("101").id, demo.period("March").id, "USD"
        )
        assert bank is not None
        bank.total_debits = Decimal("999.0000")
        bank.closing_balance = Decimal("999.0000")
        await uow.session.commit()

        await rebuild_company(uow, demo.company_id, verify=False, dry_run=False)

        repaired = await uow.ledger.get(
            demo.company_id, demo.account("101").id, demo.period("March").id, "USD"
        )
        assert repaired is not None
        assert repaired.total_debits == Decimal("115.0000")
        assert repaired.closing_balance == Decimal("115.0000")

    async def test_rebuild_includes_reversed_entries(self, uow, demo: DemoData) -> None:
        from app.scripts.rebuild_ledger import rebuild_company

        original = await JournalPostingService(uow).create(invoice_command(demo))
        await JournalReversalService(uow).reverse(
            ReverseJournalCommand(company_id=demo.company_id, entry_id=original.id, actor=ACTOR)
        )

        _, _, matched = await rebuild_company(uow, demo.company_id, verify=True, dry_run=False)
        assert matched

        bank = await uow.ledger.get(
            demo.company_id, demo.account("101").id, demo.period("March").id, "USD"
        )
        assert bank is not None
        assert bank.closing_balance == Decimal("0.0000")
        assert bank.total_debits == Decimal("115.0000")
        assert bank.total_credits == Decimal("115.0000")


# ---------------------------------------------------------------------------
async def _entry_count(uow, demo: DemoData) -> int:
    from sqlalchemy import func

    result = await uow.session.execute(
        select(func.count())
        .select_from(JournalEntry)
        .where(JournalEntry.company_id == demo.company_id)
    )
    return int(result.scalar_one())
