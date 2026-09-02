"""JournalPostingService — the one and only gateway to the general ledger.

Nothing else in this system, and nothing in any future ERP module, may insert
a POSTED journal entry or touch `ledger_balances`. Every accounting effect in
the platform flows through `post()` or `create()`.

The full pipeline, in order:

     1. validate the command payload            (Pydantic / command dataclass)
     2-5. company + currency exist and are active
     6-7. fiscal period exists, SELECT ... FOR UPDATE
     8-9. entry_date inside the period; period status accepts postings
    10.  journal status permits the transition
    11-14. accounts exist, belong to the company, are active and postable
    15-16. tax codes exist and are effective
    17-19. line rules, money precision, sequential line numbers
    20-22. totals computed; debits == credits
    23-26. canonical payload hash; idempotency resolved
    27.  allocate the journal number atomically
    28-29. insert header and lines
    30.  fold movements into ledger_balances with an atomic UPSERT
    31.  write the audit log
    32.  enqueue the JournalPosted outbox event
    33.  COMMIT
    34.  return the posted entry

Any failure at any step rolls the entire transaction back. There is no code
path that commits a partial accounting transaction.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from app.application.journal.commands import (
    ActorContext,
    CreateJournalCommand,
    PostJournalCommand,
    VoidJournalCommand,
)
from app.application.journal.validation_service import (
    JournalValidationService,
    ValidatedEntry,
    to_account_entity,
    to_currency_entity,
    to_period_entity,
)
from app.core.exceptions import AppError, NotFoundError
from app.core.idempotency import payload_hash
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.domain.accounting import rules
from app.domain.accounting import services as domain_services
from app.domain.accounting.entities import JournalEntry as JournalEntryEntity
from app.domain.accounting.enums import AuditAction, JournalStatus
from app.domain.accounting.events import JournalPosted, JournalVoided
from app.domain.accounting.exceptions import DuplicateIdempotencyKeyError
from app.infrastructure.database.models.journal import JournalEntry, JournalItem
from app.infrastructure.database.unit_of_work import UnitOfWork
from app.infrastructure.observability import metrics

logger = get_logger(__name__)


class JournalPostingService:
    """The authoritative accounting posting gatekeeper."""

    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow
        self.validator = JournalValidationService(uow)

    # ---------------------------------------------------------------- create
    async def create(self, command: CreateJournalCommand) -> JournalEntry:
        """Create an entry, posting it immediately unless asked not to.

        The whole thing is one transaction. A draft creation and a posting
        differ only in whether the period is locked and the ledger is touched.
        """
        started = time.perf_counter()

        if command.idempotency_key and command.idempotency_payload_hash is None:
            command.idempotency_payload_hash = payload_hash(command.idempotency_payload())

        try:
            async with self.uow:
                existing = await self._resolve_idempotency(command)
                if existing is not None:
                    logger.info(
                        "journal.idempotent_replay",
                        entry_id=str(existing.id),
                        entry_number=existing.entry_number,
                        idempotency_key=command.idempotency_key,
                    )
                    return existing

                validated = await self.validator.validate_create(
                    command, lock_period=command.post_immediately
                )

                entry = await self._insert_entry(
                    validated,
                    command,
                    post=command.post_immediately,
                )

                if command.post_immediately:
                    await self._apply_ledger(validated, entry)
                    self._audit_posted(entry, validated, command.actor)
                    self._enqueue_posted_event(entry, validated)
                else:
                    self._audit_created(entry, command.actor)

                await self.uow.commit()
        except AppError as exc:
            metrics.journal_posting_failures_total.labels(exc.code).inc()
            raise

        if command.post_immediately:
            self._record_posted_metrics(entry, started)

        await self.uow.refresh(entry)
        return entry

    # ------------------------------------------------------------------ post
    async def post(self, command: PostJournalCommand) -> JournalEntry:
        """Post an existing DRAFT entry."""
        started = time.perf_counter()

        try:
            async with self.uow:
                entry = await self.uow.journal.lock_with_items(command.company_id, command.entry_id)
                if entry is None:
                    raise NotFoundError(
                        "Journal entry not found.",
                        details={"entry_id": str(command.entry_id)},
                    )

                rules.validate_journal_transition(entry.status, JournalStatus.POSTED)

                validated = await self._revalidate_stored(entry)

                fiscal_year = validated.period.fiscal_year
                entry.entry_number = await self.uow.sequences.allocate_journal_number(
                    command.company_id, fiscal_year
                )
                entry.status = JournalStatus.POSTED
                entry.posted_at = datetime.now(UTC)
                entry.posted_by = command.actor.user_id
                entry.updated_by = command.actor.user_id
                entry.version += 1
                if command.idempotency_key and not entry.idempotency_key:
                    entry.idempotency_key = command.idempotency_key
                    entry.idempotency_payload_hash = payload_hash(_stored_entry_payload(entry))

                await self.uow.flush()

                await self._apply_ledger(validated, entry)
                self._audit_posted(entry, validated, command.actor)
                self._enqueue_posted_event(entry, validated)

                await self.uow.commit()
        except AppError as exc:
            metrics.journal_posting_failures_total.labels(exc.code).inc()
            raise

        self._record_posted_metrics(entry, started)
        await self.uow.refresh(entry)
        return entry

    # ------------------------------------------------------------------ void
    async def void(self, command: VoidJournalCommand) -> JournalEntry:
        """Void a DRAFT entry.

        A posted entry can never be voided — voiding erases, and erasing
        posted accounting is exactly what an audit trail must prevent. Use a
        reversal instead. The state machine enforces this.
        """
        async with self.uow:
            entry = await self.uow.journal.lock_with_items(command.company_id, command.entry_id)
            if entry is None:
                raise NotFoundError(
                    "Journal entry not found.", details={"entry_id": str(command.entry_id)}
                )

            rules.validate_journal_transition(entry.status, JournalStatus.VOID)

            entry.status = JournalStatus.VOID
            entry.updated_by = command.actor.user_id
            entry.version += 1

            self.uow.audit.record(
                action=AuditAction.JOURNAL_VOIDED,
                entity_type="JournalEntry",
                entity_id=entry.id,
                company_id=entry.company_id,
                user_id=command.actor.user_id,
                payload={"reason": command.reason, "entry_date": entry.entry_date.isoformat()},
                ip_address=command.actor.ip_address,
                user_agent=command.actor.user_agent,
                request_id=command.actor.request_id,
            )
            self.uow.outbox.enqueue(
                JournalVoided(
                    aggregate_id=entry.id,
                    company_id=entry.company_id,
                    entry_number=entry.entry_number,
                    voided_by=command.actor.user_id,
                )
            )
            await self.uow.commit()

        await self.uow.refresh(entry)
        return entry

    # -------------------------------------------------------------- internals
    async def _resolve_idempotency(self, command: CreateJournalCommand) -> JournalEntry | None:
        """Same key + same payload → return the original. Different → 409.

        The database is authoritative. A Redis cache in front of this would
        only ever skip a lookup, never decide the outcome.
        """
        if not command.idempotency_key:
            return None

        existing = await self.uow.journal.get_by_idempotency_key(
            command.company_id, command.idempotency_key
        )
        if existing is None:
            return None

        if existing.idempotency_payload_hash != command.idempotency_payload_hash:
            raise DuplicateIdempotencyKeyError(
                "This idempotency key was already used with a different payload.",
                details={
                    "idempotency_key": command.idempotency_key,
                    "existing_entry_id": str(existing.id),
                    "existing_entry_number": existing.entry_number,
                },
            )
        return existing

    async def _insert_entry(
        self,
        validated: ValidatedEntry,
        command: CreateJournalCommand,
        *,
        post: bool,
    ) -> JournalEntry:
        domain_entry = validated.entry
        entry_id = uuid7()

        entry_number: str | None = None
        posted_at: datetime | None = None
        if post:
            # Allocated inside this transaction; a rollback simply leaves a gap.
            entry_number = await self.uow.sequences.allocate_journal_number(
                command.company_id, validated.period.fiscal_year
            )
            posted_at = datetime.now(UTC)

        entry = JournalEntry(
            id=entry_id,
            company_id=command.company_id,
            entry_number=entry_number,
            entry_date=command.entry_date,
            period_id=validated.period.id,
            currency_code=domain_entry.currency_code,
            source_type=command.source_type,
            source_document=command.source_document,
            reference=command.reference,
            memo=command.memo,
            status=JournalStatus.POSTED if post else JournalStatus.DRAFT,
            idempotency_key=command.idempotency_key,
            idempotency_payload_hash=command.idempotency_payload_hash,
            version=1,
            posted_at=posted_at,
            posted_by=command.actor.user_id if post else None,
            created_by=command.actor.user_id,
            updated_by=command.actor.user_id,
        )
        self.uow.journal.add(entry)

        self.uow.journal.add_items(
            [
                JournalItem(
                    id=uuid7(),
                    entry_id=entry_id,
                    company_id=command.company_id,
                    line_number=line.line_number,
                    account_id=line.account_id,
                    debit=line.debit,
                    credit=line.credit,
                    currency_code=line.currency_code,
                    tax_id=line.tax_id,
                    memo=line.memo,
                )
                for line in domain_entry.lines
            ]
        )
        await self.uow.flush()
        return entry

    async def _revalidate_stored(self, entry: JournalEntry) -> ValidatedEntry:
        """Re-run every rule against a stored draft before posting it.

        A draft may have been sitting for weeks: its accounts could have been
        deactivated, its period closed, its tax codes expired. Posting is the
        moment that matters, so posting is where the rules are enforced.
        """
        period = await self.uow.fiscal_periods.lock_for_update(entry.company_id, entry.period_id)
        if period is None:
            raise NotFoundError(
                "Fiscal period not found.", details={"period_id": str(entry.period_id)}
            )
        period_entity = to_period_entity(period)
        rules.validate_period_covers_date(period_entity, entry.entry_date)
        rules.validate_period_accepts_posting(period_entity)

        company = await self.validator.resolve_company(entry.company_id)
        currency = await self.validator.resolve_currency(entry.currency_code)
        currency_entity = to_currency_entity(currency)

        account_models = await self.uow.accounts.get_many_by_id(
            entry.company_id, [item.account_id for item in entry.items]
        )
        accounts = {
            account_id: to_account_entity(model) for account_id, model in account_models.items()
        }

        domain_entry = _to_domain_entry(entry)
        for line in domain_entry.lines:
            account = accounts.get(line.account_id)
            if account is None:
                raise NotFoundError(
                    "Account referenced by this entry no longer exists.",
                    code="ACCOUNT_NOT_FOUND",
                    details={"line_number": line.line_number},
                )
            rules.validate_line_amounts(line)
            rules.validate_money_precision(line, currency_entity)
            rules.validate_line_currency(line, entry.currency_code)
            rules.validate_account_usable(account, entry.company_id, line.line_number)
            rules.validate_account_currency(account, entry.currency_code, line.line_number)

        rules.validate_line_numbers(domain_entry.lines)
        rules.validate_balanced(domain_entry)

        return ValidatedEntry(
            entry=domain_entry,
            company=company,
            currency=currency,
            period=period,
            accounts=accounts,
        )

    async def _apply_ledger(self, validated: ValidatedEntry, entry: JournalEntry) -> None:
        """Fold the entry into the ledger projection — atomically, in-transaction."""
        movements = domain_services.aggregate_ledger_movements(validated.entry, validated.accounts)
        await self.uow.ledger.apply_movements(movements)
        logger.info(
            "journal.ledger_applied",
            entry_id=str(entry.id),
            entry_number=entry.entry_number,
            movements=len(movements),
        )

    def _audit_posted(
        self, entry: JournalEntry, validated: ValidatedEntry, actor: ActorContext
    ) -> None:
        self.uow.audit.record(
            action=AuditAction.JOURNAL_POSTED,
            entity_type="JournalEntry",
            entity_id=entry.id,
            company_id=entry.company_id,
            user_id=actor.user_id,
            payload={
                "entry_number": entry.entry_number,
                "entry_date": entry.entry_date.isoformat(),
                "period_id": str(entry.period_id),
                "currency_code": entry.currency_code,
                "total_debits": str(validated.entry.total_debits),
                "total_credits": str(validated.entry.total_credits),
                "line_count": len(validated.entry.lines),
                "source_type": entry.source_type,
                "source_document": entry.source_document,
            },
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )

    def _audit_created(self, entry: JournalEntry, actor: ActorContext) -> None:
        self.uow.audit.record(
            action=AuditAction.JOURNAL_CREATED,
            entity_type="JournalEntry",
            entity_id=entry.id,
            company_id=entry.company_id,
            user_id=actor.user_id,
            payload={
                "status": str(entry.status),
                "entry_date": entry.entry_date.isoformat(),
                "currency_code": entry.currency_code,
            },
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )

    def _enqueue_posted_event(self, entry: JournalEntry, validated: ValidatedEntry) -> None:
        self.uow.outbox.enqueue(
            JournalPosted(
                aggregate_id=entry.id,
                company_id=entry.company_id,
                entry_number=entry.entry_number or "",
                entry_date=entry.entry_date,
                currency_code=entry.currency_code,
                total_debits=validated.entry.total_debits,
                total_credits=validated.entry.total_credits,
                source_type=entry.source_type,
                source_document=entry.source_document,
                period_id=entry.period_id,
                posted_by=entry.posted_by,
                lines=[
                    {
                        "line_number": line.line_number,
                        "account_id": str(line.account_id),
                        "debit": str(line.debit),
                        "credit": str(line.credit),
                        "currency_code": line.currency_code,
                    }
                    for line in validated.entry.lines
                ],
            )
        )

    @staticmethod
    def _record_posted_metrics(entry: JournalEntry, started: float) -> None:
        metrics.journal_posting_duration_seconds.observe(time.perf_counter() - started)
        metrics.journal_entries_posted_total.labels(
            str(entry.company_id), entry.source_type or "manual"
        ).inc()
        logger.info(
            "journal.posted",
            entry_id=str(entry.id),
            entry_number=entry.entry_number,
            company_id=str(entry.company_id),
            source_type=entry.source_type,
        )


# ---------------------------------------------------------------------------
# Conversions between the ORM model and the pure domain aggregate
# ---------------------------------------------------------------------------
def _to_domain_entry(entry: JournalEntry) -> JournalEntryEntity:
    from app.domain.accounting.entities import JournalLine
    from app.domain.accounting.value_objects import DebitCredit

    return JournalEntryEntity(
        company_id=entry.company_id,
        entry_date=entry.entry_date,
        period_id=entry.period_id,
        currency_code=entry.currency_code,
        lines=[
            JournalLine(
                line_number=item.line_number,
                account_id=item.account_id,
                amounts=DebitCredit(debit=item.debit, credit=item.credit),
                currency_code=item.currency_code,
                tax_id=item.tax_id,
                memo=item.memo,
                id=item.id,
            )
            for item in sorted(entry.items, key=lambda i: i.line_number)
        ],
        status=entry.status,
        id=entry.id,
        entry_number=entry.entry_number,
        source_type=entry.source_type,
        source_document=entry.source_document,
        reference=entry.reference,
        idempotency_key=entry.idempotency_key,
        idempotency_payload_hash=entry.idempotency_payload_hash,
        version=entry.version,
        posted_at=entry.posted_at,
        posted_by=entry.posted_by,
        reversed_by_entry_id=entry.reversed_by_entry_id,
    )


def _stored_entry_payload(entry: JournalEntry) -> dict[str, object]:
    """Canonical payload of a stored entry, for late idempotency binding."""
    return {
        "company_id": entry.company_id,
        "entry_date": entry.entry_date.isoformat(),
        "currency_code": entry.currency_code,
        "source_type": entry.source_type,
        "source_document": entry.source_document,
        "reference": entry.reference,
        "lines": [
            {
                "line_number": item.line_number,
                "account_id": item.account_id,
                "account_number": None,
                "debit": item.debit,
                "credit": item.credit,
                "currency_code": item.currency_code,
                "tax_id": item.tax_id,
                "tax_code": None,
                "memo": item.memo,
            }
            for item in sorted(entry.items, key=lambda i: i.line_number)
        ],
    }
