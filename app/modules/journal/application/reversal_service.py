"""Reversal — the only way to correct a posted entry.

A reversal is not a deletion and not a void. It is a *new posted entry* that
mirrors the original, so the ledger ends up net-zero while both documents
remain visible in the audit trail forever. That is what makes the history
defensible to an auditor.

    Original                        Reversal
    Bank            Dr 115          Bank                Cr 115
    Revenue             Cr 100      Revenue         Dr 100
    Tax Payable         Cr  15      Tax Payable     Dr  15

Both entries stay POSTED-weight; the original's status becomes REVERSED and
the two are linked in both directions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.core.exceptions import NotFoundError
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.modules.fiscal_periods.infrastructure.models import FiscalPeriod
from app.modules.journal.application.commands import ReverseJournalCommand
from app.modules.journal.application.posting_service import _to_domain_entry
from app.modules.journal.application.validation_service import (
    JournalValidationService,
    to_account_entity,
    to_period_entity,
)
from app.modules.journal.infrastructure.models import JournalEntry, JournalItem
from app.shared.domain import rules
from app.shared.domain import services as domain_services
from app.shared.domain.entities import JournalEntry as JournalEntryEntity
from app.shared.domain.enums import AuditAction, JournalStatus
from app.shared.domain.events import JournalReversed
from app.shared.domain.exceptions import (
    FiscalPeriodNotFoundForDateError,
    JournalAlreadyReversedError,
)
from app.shared.observability import metrics
from app.shared.unit_of_work.unit_of_work import UnitOfWork

logger = get_logger(__name__)


class JournalReversalService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow
        self.validator = JournalValidationService(uow)

    async def reverse(self, command: ReverseJournalCommand) -> tuple[JournalEntry, JournalEntry]:
        """Reverse a posted entry. Returns `(original, reversal)`.

        Everything happens in one transaction: if the reversal cannot be
        posted, the original stays untouched.
        """
        async with self.uow:
            original = await self.uow.journal.lock_with_items(command.company_id, command.entry_id)
            if original is None:
                raise NotFoundError(
                    "Journal entry not found.",
                    details={"entry_id": str(command.entry_id)},
                )

            if original.reversed_by_entry_id is not None:
                raise JournalAlreadyReversedError(
                    details={
                        "entry_id": str(original.id),
                        "entry_number": original.entry_number,
                        "reversed_by_entry_id": str(original.reversed_by_entry_id),
                    }
                )
            rules.validate_journal_transition(original.status, JournalStatus.REVERSED)

            reversal_date = command.reversal_date or original.entry_date
            period = await self._resolve_reversal_period(command, reversal_date)

            period_entity = to_period_entity(period)
            rules.validate_period_covers_date(period_entity, reversal_date)
            rules.validate_period_accepts_posting(period_entity)

            domain_original = _to_domain_entry(original)
            domain_reversal = domain_services.build_reversal_entry(
                domain_original,
                reversal_date=reversal_date,
                period_id=period.id,
                reference=command.reference,
            )
            rules.validate_balanced(domain_reversal)

            accounts_models = await self.uow.accounts.get_many_by_id(
                command.company_id, [line.account_id for line in domain_reversal.lines]
            )
            accounts = {
                account_id: to_account_entity(model)
                for account_id, model in accounts_models.items()
            }
            for line in domain_reversal.lines:
                account = accounts.get(line.account_id)
                if account is None:
                    raise NotFoundError(
                        "Account referenced by the original entry no longer exists.",
                        code="ACCOUNT_NOT_FOUND",
                        details={"line_number": line.line_number},
                    )
                rules.validate_line_amounts(line)
                # A reversal must be postable even if the account has since been
                # deactivated — otherwise a mistake could become uncorrectable.
                # Non-postable (roll-up) accounts are still rejected.
                rules.validate_account_currency(
                    account, domain_reversal.currency_code, line.line_number
                )

            reversal = await self._insert_reversal(
                command, original, domain_reversal, period.fiscal_year
            )

            movements = domain_services.aggregate_ledger_movements(domain_reversal, accounts)
            await self.uow.ledger.apply_movements(movements)

            original.status = JournalStatus.REVERSED
            original.reversed_by_entry_id = reversal.id
            original.updated_by = command.actor.user_id
            original.version += 1

            self._audit(original, reversal, command)
            self.uow.outbox.enqueue(
                JournalReversed(
                    aggregate_id=original.id,
                    company_id=original.company_id,
                    entry_number=original.entry_number or "",
                    reversal_entry_id=reversal.id,
                    reversal_entry_number=reversal.entry_number or "",
                    reversed_by=command.actor.user_id,
                )
            )

            await self.uow.commit()

        metrics.journal_entries_reversed_total.inc()
        metrics.journal_entries_posted_total.labels(
            str(reversal.company_id), reversal.source_type or "reversal"
        ).inc()
        logger.info(
            "journal.reversed",
            original_entry_id=str(original.id),
            original_entry_number=original.entry_number,
            reversal_entry_id=str(reversal.id),
            reversal_entry_number=reversal.entry_number,
        )

        await self.uow.refresh(original)
        await self.uow.refresh(reversal)
        return original, reversal

    # ------------------------------------------------------------- internals
    async def _resolve_reversal_period(
        self, command: ReverseJournalCommand, reversal_date: date
    ) -> FiscalPeriod:
        if command.period_id is not None:
            period = await self.uow.fiscal_periods.lock_for_update(
                command.company_id, command.period_id
            )
            if period is None:
                raise NotFoundError(
                    "Fiscal period not found.",
                    details={"period_id": str(command.period_id)},
                )
            return period

        found = await self.uow.fiscal_periods.find_for_date(command.company_id, reversal_date)
        if found is None:
            raise FiscalPeriodNotFoundForDateError(
                details={"reversal_date": reversal_date.isoformat()}
            )
        locked = await self.uow.fiscal_periods.lock_for_update(command.company_id, found.id)
        if locked is None:  # pragma: no cover - the row was just read
            raise NotFoundError("Fiscal period not found.")
        return locked

    async def _insert_reversal(
        self,
        command: ReverseJournalCommand,
        original: JournalEntry,
        domain_reversal: JournalEntryEntity,
        fiscal_year: int,
    ) -> JournalEntry:
        reversal_id = uuid7()
        entry_number = await self.uow.sequences.allocate_journal_number(
            command.company_id, fiscal_year
        )

        reversal = JournalEntry(
            id=reversal_id,
            company_id=command.company_id,
            entry_number=entry_number,
            entry_date=domain_reversal.entry_date,
            period_id=domain_reversal.period_id,
            currency_code=domain_reversal.currency_code,
            source_type=original.source_type,
            source_document=original.source_document,
            reference=domain_reversal.reference,
            memo=f"Reversal of {original.entry_number}",
            status=JournalStatus.POSTED,
            idempotency_key=command.idempotency_key,
            idempotency_payload_hash=(
                f"reversal:{original.id}" if command.idempotency_key else None
            ),
            version=1,
            posted_at=datetime.now(UTC),
            posted_by=command.actor.user_id,
            reverses_entry_id=original.id,
            created_by=command.actor.user_id,
            updated_by=command.actor.user_id,
        )
        self.uow.journal.add(reversal)
        self.uow.journal.add_items(
            [
                JournalItem(
                    id=uuid7(),
                    entry_id=reversal_id,
                    company_id=command.company_id,
                    line_number=line.line_number,
                    account_id=line.account_id,
                    debit=line.debit,
                    credit=line.credit,
                    currency_code=line.currency_code,
                    tax_id=line.tax_id,
                    memo=line.memo,
                )
                for line in domain_reversal.lines
            ]
        )
        await self.uow.flush()
        return reversal

    def _audit(
        self,
        original: JournalEntry,
        reversal: JournalEntry,
        command: ReverseJournalCommand,
    ) -> None:
        payload = {
            "original_entry_id": str(original.id),
            "original_entry_number": original.entry_number,
            "reversal_entry_id": str(reversal.id),
            "reversal_entry_number": reversal.entry_number,
            "reversal_date": reversal.entry_date.isoformat(),
        }
        self.uow.audit.record(
            action=AuditAction.JOURNAL_REVERSED,
            entity_type="JournalEntry",
            entity_id=original.id,
            company_id=original.company_id,
            user_id=command.actor.user_id,
            payload=payload,
            ip_address=command.actor.ip_address,
            user_agent=command.actor.user_agent,
            request_id=command.actor.request_id,
        )
        self.uow.audit.record(
            action=AuditAction.JOURNAL_POSTED,
            entity_type="JournalEntry",
            entity_id=reversal.id,
            company_id=reversal.company_id,
            user_id=command.actor.user_id,
            payload=payload | {"kind": "reversal"},
            ip_address=command.actor.ip_address,
            user_agent=command.actor.user_agent,
            request_id=command.actor.request_id,
        )
