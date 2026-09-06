"""Journal entry endpoints.

Routes authenticate, authorise, parse and delegate. Every accounting decision
is made by `JournalService` / `JournalPostingService`.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.dependencies import ActorDep, UowDep, get_idempotency_key, require
from app.core.pagination import Page, PageParams, page_params
from app.modules.identity.domain.permissions import Permission
from app.modules.journal.application.schemas import (
    JournalEntryCreate,
    JournalEntryRead,
    JournalEntrySummary,
    JournalEntryUpdate,
    JournalPostRequest,
    JournalReversalRequest,
    JournalVoidRequest,
    ReversalResult,
)
from app.modules.journal.application.service import JournalService
from app.shared.domain.enums import JournalStatus

router = APIRouter(prefix="/journal-entries", tags=["journal-entries"])


@router.post(
    "",
    response_model=JournalEntryRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create (and by default post) a journal entry",
    dependencies=[Depends(require(Permission.ACCOUNTING_WRITE))],
)
async def create_journal_entry(
    payload: JournalEntryCreate,
    uow: UowDep,
    actor: ActorDep,
    response: Response,
    header_key: Annotated[str | None, Depends(get_idempotency_key)] = None,
) -> JournalEntryRead:
    """Submit a business transaction to the general ledger.

    Supply `Idempotency-Key` (header or body) to make retries safe: the same
    key with the same payload returns the original entry; the same key with a
    different payload is a 409.
    """
    if payload.idempotency_key is None and header_key:
        payload = payload.model_copy(update={"idempotency_key": header_key})

    entry = await JournalService(uow).create(payload, actor)
    if entry.status is JournalStatus.POSTED:
        response.headers["Location"] = f"/api/v1/journal-entries/{entry.id}"
    return entry


@router.get(
    "",
    response_model=Page[JournalEntrySummary],
    summary="List journal entries",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def list_journal_entries(
    uow: UowDep,
    company_id: Annotated[UUID, Query()],
    params: Annotated[PageParams, Depends(page_params)],
    entry_status: Annotated[JournalStatus | None, Query(alias="status")] = None,
    period_id: Annotated[UUID | None, Query()] = None,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    source_type: Annotated[str | None, Query()] = None,
    source_document: Annotated[str | None, Query()] = None,
    reference: Annotated[str | None, Query()] = None,
    currency_code: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
) -> Page[JournalEntrySummary]:
    return await JournalService(uow).list(
        company_id,
        params,
        status=entry_status,
        period_id=period_id,
        start_date=start_date,
        end_date=end_date,
        source_type=source_type,
        source_document=source_document,
        reference=reference,
        currency_code=currency_code,
    )


@router.get(
    "/{entry_id}",
    response_model=JournalEntryRead,
    summary="Fetch a journal entry with its lines",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def get_journal_entry(
    entry_id: UUID,
    uow: UowDep,
    company_id: Annotated[UUID, Query()],
) -> JournalEntryRead:
    return await JournalService(uow).get(company_id, entry_id)


@router.patch(
    "/{entry_id}",
    response_model=JournalEntryRead,
    summary="Edit a DRAFT journal entry",
    dependencies=[Depends(require(Permission.ACCOUNTING_WRITE))],
)
async def update_journal_entry(
    entry_id: UUID,
    payload: JournalEntryUpdate,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
) -> JournalEntryRead:
    """Posted entries are immutable — this returns 409 for anything but a DRAFT."""
    return await JournalService(uow).update_draft(company_id, entry_id, payload, actor)


@router.delete(
    "/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a DRAFT journal entry",
    dependencies=[Depends(require(Permission.ACCOUNTING_WRITE))],
)
async def delete_journal_entry(
    entry_id: UUID,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
) -> None:
    await JournalService(uow).delete_draft(company_id, entry_id, actor)


@router.post(
    "/{entry_id}/post",
    response_model=JournalEntryRead,
    summary="Post a DRAFT journal entry to the general ledger",
    dependencies=[Depends(require(Permission.ACCOUNTING_POST))],
)
async def post_journal_entry(
    entry_id: UUID,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
    payload: JournalPostRequest | None = None,
    header_key: Annotated[str | None, Depends(get_idempotency_key)] = None,
) -> JournalEntryRead:
    idempotency_key = (payload.idempotency_key if payload else None) or header_key
    return await JournalService(uow).post(company_id, entry_id, idempotency_key, actor)


@router.post(
    "/{entry_id}/reverse",
    response_model=ReversalResult,
    status_code=status.HTTP_201_CREATED,
    summary="Reverse a POSTED journal entry",
    dependencies=[Depends(require(Permission.ACCOUNTING_REVERSE))],
)
async def reverse_journal_entry(
    entry_id: UUID,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
    payload: JournalReversalRequest | None = None,
    header_key: Annotated[str | None, Depends(get_idempotency_key)] = None,
) -> ReversalResult:
    """Create and post the mirror-image counter-entry."""
    request = payload or JournalReversalRequest()
    original, reversal = await JournalService(uow).reverse(
        company_id,
        entry_id,
        reversal_date=request.reversal_date,
        period_id=request.period_id,
        reference=request.reference,
        idempotency_key=request.idempotency_key or header_key,
        actor=actor,
    )
    return ReversalResult(original=original, reversal=reversal)


@router.post(
    "/{entry_id}/void",
    response_model=JournalEntryRead,
    summary="Void a DRAFT journal entry",
    dependencies=[Depends(require(Permission.ACCOUNTING_VOID))],
)
async def void_journal_entry(
    entry_id: UUID,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
    payload: JournalVoidRequest | None = None,
) -> JournalEntryRead:
    """Only DRAFT entries can be voided. Correct a posted entry by reversing it."""
    return await JournalService(uow).void(
        company_id, entry_id, payload.reason if payload else None, actor
    )
