"""Journal application service.

Owns the non-posting operations (read, list, edit a draft, delete a draft) and
delegates every ledger-affecting operation to `JournalPostingService` /
`JournalReversalService`. Routes talk only to this class.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select

from app.core.exceptions import ConcurrencyError, NotFoundError
from app.core.ids import uuid7
from app.core.money import ZERO, quantize_storage
from app.core.pagination import Page, PageParams, encode_cursor
from app.modules.journal.application import queries
from app.modules.journal.application.commands import (
    ActorContext,
    CreateJournalCommand,
    JournalLineCommand,
    PostJournalCommand,
    ReverseJournalCommand,
    VoidJournalCommand,
)
from app.modules.journal.application.posting_service import JournalPostingService
from app.modules.journal.application.reversal_service import JournalReversalService
from app.modules.journal.application.schemas import (
    JournalEntryCreate,
    JournalEntryRead,
    JournalEntrySummary,
    JournalEntryUpdate,
)
from app.modules.journal.infrastructure.models import JournalEntry, JournalItem
from app.shared.domain import rules
from app.shared.domain.enums import AuditAction, JournalStatus
from app.shared.domain.exceptions import PostedEntryCannotBeDeletedError
from app.shared.unit_of_work.unit_of_work import UnitOfWork


class JournalService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow
        self.posting = JournalPostingService(uow)
        self.reversal = JournalReversalService(uow)

    # ----------------------------------------------------------------- write
    async def create(self, payload: JournalEntryCreate, actor: ActorContext) -> JournalEntryRead:
        command = CreateJournalCommand(
            company_id=payload.company_id,
            entry_date=payload.entry_date,
            currency_code=payload.currency_code,
            period_id=payload.period_id,
            source_type=payload.source_type,
            source_document=payload.source_document,
            reference=payload.reference,
            memo=payload.memo,
            idempotency_key=payload.idempotency_key,
            post_immediately=payload.post_immediately,
            actor=actor,
            lines=[
                JournalLineCommand(
                    line_number=line.line_number,
                    debit=line.debit,
                    credit=line.credit,
                    account_id=line.account_id,
                    account_number=line.account_number,
                    currency_code=line.currency_code,
                    tax_id=line.tax_id,
                    tax_code=line.tax_code,
                    memo=line.memo,
                )
                for line in payload.lines
            ],
        )
        entry = await self.posting.create(command)
        return await self._decorate(entry)

    async def post(
        self, company_id: UUID, entry_id: UUID, idempotency_key: str | None, actor: ActorContext
    ) -> JournalEntryRead:
        entry = await self.posting.post(
            PostJournalCommand(
                company_id=company_id,
                entry_id=entry_id,
                idempotency_key=idempotency_key,
                actor=actor,
            )
        )
        return await self._decorate(entry)

    async def void(
        self, company_id: UUID, entry_id: UUID, reason: str | None, actor: ActorContext
    ) -> JournalEntryRead:
        entry = await self.posting.void(
            VoidJournalCommand(company_id=company_id, entry_id=entry_id, reason=reason, actor=actor)
        )
        return await self._decorate(entry)

    async def reverse(
        self,
        company_id: UUID,
        entry_id: UUID,
        *,
        reversal_date: date | None,
        period_id: UUID | None,
        reference: str | None,
        idempotency_key: str | None,
        actor: ActorContext,
    ) -> tuple[JournalEntryRead, JournalEntryRead]:
        original, reversal = await self.reversal.reverse(
            ReverseJournalCommand(
                company_id=company_id,
                entry_id=entry_id,
                reversal_date=reversal_date,
                period_id=period_id,
                reference=reference,
                idempotency_key=idempotency_key,
                actor=actor,
            )
        )
        return await self._decorate(original), await self._decorate(reversal)

    async def update_draft(
        self,
        company_id: UUID,
        entry_id: UUID,
        payload: JournalEntryUpdate,
        actor: ActorContext,
    ) -> JournalEntryRead:
        """Edit a DRAFT. Posted entries are rejected before anything is touched."""
        async with self.uow:
            entry = await self.uow.journal.lock_with_items(company_id, entry_id)
            if entry is None:
                raise NotFoundError("Journal entry not found.", details={"entry_id": str(entry_id)})

            rules.validate_mutable(entry.status)

            if payload.version is not None and payload.version != entry.version:
                raise ConcurrencyError(
                    "This draft was modified by another request.",
                    details={"expected_version": payload.version, "current_version": entry.version},
                )

            await self._apply_draft_changes(entry, payload, actor)

            entry.version += 1
            entry.updated_by = actor.user_id

            self.uow.audit.record(
                action=AuditAction.JOURNAL_UPDATED,
                entity_type="JournalEntry",
                entity_id=entry.id,
                company_id=entry.company_id,
                user_id=actor.user_id,
                payload={
                    "version": entry.version,
                    "fields": sorted(payload.model_dump(exclude_none=True).keys()),
                },
                ip_address=actor.ip_address,
                user_agent=actor.user_agent,
                request_id=actor.request_id,
            )
            await self.uow.commit()

        await self.uow.refresh(entry)
        return await self._decorate(entry)

    async def delete_draft(self, company_id: UUID, entry_id: UUID, actor: ActorContext) -> None:
        async with self.uow:
            entry = await self.uow.journal.lock_with_items(company_id, entry_id)
            if entry is None:
                raise NotFoundError("Journal entry not found.", details={"entry_id": str(entry_id)})
            if entry.status is not JournalStatus.DRAFT:
                raise PostedEntryCannotBeDeletedError(details={"status": str(entry.status)})

            self.uow.audit.record(
                action=AuditAction.JOURNAL_DELETED,
                entity_type="JournalEntry",
                entity_id=entry.id,
                company_id=entry.company_id,
                user_id=actor.user_id,
                payload={
                    "entry_date": entry.entry_date.isoformat(),
                    "reference": entry.reference,
                    "line_count": len(entry.items),
                },
                ip_address=actor.ip_address,
                user_agent=actor.user_agent,
                request_id=actor.request_id,
            )
            await self.uow.journal.delete(entry)
            await self.uow.commit()

    # ------------------------------------------------------------------ read
    async def get(self, company_id: UUID, entry_id: UUID) -> JournalEntryRead:
        entry = await self.uow.journal.get_with_items(company_id, entry_id)
        if entry is None:
            raise NotFoundError("Journal entry not found.", details={"entry_id": str(entry_id)})
        return await self._decorate(entry)

    async def list(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        status: JournalStatus | None = None,
        period_id: UUID | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        source_type: str | None = None,
        source_document: str | None = None,
        reference: str | None = None,
        currency_code: str | None = None,
    ) -> Page[JournalEntrySummary]:
        entries, total = await self.uow.journal.list_paginated(
            company_id,
            params,
            status=status,
            period_id=period_id,
            start_date=start_date,
            end_date=end_date,
            source_type=source_type,
            source_document=source_document,
            reference=reference,
            currency_code=currency_code,
        )
        totals = await self._totals_for(entries)
        items = queries.to_summaries(entries, totals)
        next_cursor = encode_cursor(entries[-1].id) if entries else None
        return Page.build(
            items,
            params,
            total=total,
            next_cursor=next_cursor,
            has_more=len(entries) == params.limit,
        )

    # ------------------------------------------------------------- internals
    async def _totals_for(
        self, entries: Sequence[JournalEntry]
    ) -> dict[UUID, tuple[Decimal, Decimal]]:
        """Aggregate line totals for a whole page in one query (no N+1)."""
        if not entries:
            return {}
        stmt = (
            select(
                JournalItem.entry_id,
                func.coalesce(func.sum(JournalItem.debit), ZERO),
                func.coalesce(func.sum(JournalItem.credit), ZERO),
            )
            .where(JournalItem.entry_id.in_([entry.id for entry in entries]))
            .group_by(JournalItem.entry_id)
        )
        rows = (await self.uow.execute(stmt)).all()
        return {row[0]: (row[1], row[2]) for row in rows}

    async def _decorate(self, entry: JournalEntry) -> JournalEntryRead:
        """Attach account numbers/names to the lines — one extra query."""
        accounts = await self.uow.accounts.get_many_by_id(
            entry.company_id, [item.account_id for item in entry.items]
        )
        return queries.to_read_model(entry, accounts)

    async def _apply_draft_changes(
        self, entry: JournalEntry, payload: JournalEntryUpdate, actor: ActorContext
    ) -> None:
        if payload.entry_date is not None:
            entry.entry_date = payload.entry_date
        if payload.currency_code is not None:
            currency = await self.uow.currencies.get_by_code(payload.currency_code)
            if currency is None:
                raise NotFoundError(
                    "Currency not found.",
                    details={"currency_code": payload.currency_code},
                )
            entry.currency_code = currency.currency_code
        for field in ("source_type", "source_document", "reference", "memo"):
            value = getattr(payload, field)
            if value is not None:
                setattr(entry, field, value)

        if payload.period_id is not None:
            period = await self.uow.fiscal_periods.get_for_company(
                entry.company_id, payload.period_id
            )
            if period is None:
                raise NotFoundError(
                    "Fiscal period not found.",
                    details={"period_id": str(payload.period_id)},
                )
            entry.period_id = period.id

        if payload.lines is None:
            return

        # Replace the lines wholesale: a partial line patch would make the
        # balanced-entry invariant ambiguous.
        for item in list(entry.items):
            await self.uow.session.delete(item)
        await self.uow.flush()

        accounts_by_id = await self.uow.accounts.get_many_by_id(
            entry.company_id,
            [line.account_id for line in payload.lines if line.account_id],
        )
        accounts_by_number = await self.uow.accounts.get_many_by_number(
            entry.company_id,
            [line.account_number for line in payload.lines if line.account_number],
        )

        new_items: list[JournalItem] = []
        for line in sorted(payload.lines, key=lambda ln: ln.line_number):
            account = (
                accounts_by_id.get(line.account_id)
                if line.account_id
                else accounts_by_number.get(line.account_number or "")
            )
            if account is None:
                raise NotFoundError(
                    "Account not found for this company.",
                    code="ACCOUNT_NOT_FOUND",
                    details={
                        "line_number": line.line_number,
                        "account_number": line.account_number,
                    },
                )
            tax_id = line.tax_id
            if tax_id is None and line.tax_code:
                tax = await self.uow.tax_codes.get_by_code(entry.company_id, line.tax_code)
                if tax is None:
                    raise NotFoundError(
                        "Tax code not found for this company.",
                        code="TAX_CODE_NOT_FOUND",
                        details={"tax_code": line.tax_code},
                    )
                tax_id = tax.id

            new_items.append(
                JournalItem(
                    id=uuid7(),
                    entry_id=entry.id,
                    company_id=entry.company_id,
                    line_number=line.line_number,
                    account_id=account.id,
                    debit=quantize_storage(line.debit),
                    credit=quantize_storage(line.credit),
                    currency_code=(line.currency_code or entry.currency_code).upper(),
                    tax_id=tax_id,
                    memo=line.memo,
                )
            )

        self.uow.journal.add_items(new_items)
        await self.uow.flush()
        entry.items = new_items
