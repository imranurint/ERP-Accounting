"""Fiscal period endpoints.

Closing, locking and reopening are privileged operations. Each requires an
explicit permission and each writes an audit record.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.core.dependencies import ActorDep, UowDep, require
from app.core.pagination import Page, PageParams, page_params
from app.modules.fiscal_periods.application.schemas import (
    FiscalPeriodBulkCreate,
    FiscalPeriodCreate,
    FiscalPeriodRead,
    PeriodTransitionRequest,
)
from app.modules.fiscal_periods.application.service import FiscalPeriodService
from app.modules.identity.domain.permissions import Permission
from app.shared.domain.enums import FiscalPeriodStatus

router = APIRouter(prefix="/fiscal-periods", tags=["fiscal-periods"])


@router.get(
    "",
    response_model=Page[FiscalPeriodRead],
    summary="List fiscal periods",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def list_fiscal_periods(
    uow: UowDep,
    company_id: Annotated[UUID, Query()],
    params: Annotated[PageParams, Depends(page_params)],
    fiscal_year: Annotated[int | None, Query(ge=1900, le=9999)] = None,
    period_status: Annotated[FiscalPeriodStatus | None, Query(alias="status")] = None,
) -> Page[FiscalPeriodRead]:
    return await FiscalPeriodService(uow).list(
        company_id, params, fiscal_year=fiscal_year, status=period_status
    )


@router.post(
    "",
    response_model=FiscalPeriodRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a fiscal period",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def create_fiscal_period(
    payload: FiscalPeriodCreate, uow: UowDep, actor: ActorDep
) -> FiscalPeriodRead:
    return await FiscalPeriodService(uow).create(payload, actor)


@router.post(
    "/generate-year",
    response_model=list[FiscalPeriodRead],
    status_code=status.HTTP_201_CREATED,
    summary="Generate twelve monthly periods for a fiscal year",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def generate_fiscal_year(
    payload: FiscalPeriodBulkCreate, uow: UowDep, actor: ActorDep
) -> list[FiscalPeriodRead]:
    return await FiscalPeriodService(uow).create_year(payload, actor)


@router.get(
    "/{period_id}",
    response_model=FiscalPeriodRead,
    summary="Fetch a fiscal period",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def get_fiscal_period(
    period_id: UUID, uow: UowDep, company_id: Annotated[UUID, Query()]
) -> FiscalPeriodRead:
    return await FiscalPeriodService(uow).get(company_id, period_id)


@router.post(
    "/{period_id}/open",
    response_model=FiscalPeriodRead,
    summary="Open a fiscal period",
    dependencies=[Depends(require(Permission.PERIOD_REOPEN))],
)
async def open_fiscal_period(
    period_id: UUID,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
    payload: PeriodTransitionRequest | None = None,
) -> FiscalPeriodRead:
    return await FiscalPeriodService(uow).transition(
        company_id,
        period_id,
        FiscalPeriodStatus.OPEN,
        actor,
        reason=payload.reason if payload else None,
    )


@router.post(
    "/{period_id}/close",
    response_model=FiscalPeriodRead,
    summary="Close a fiscal period",
    dependencies=[Depends(require(Permission.PERIOD_CLOSE))],
)
async def close_fiscal_period(
    period_id: UUID,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
    payload: PeriodTransitionRequest | None = None,
) -> FiscalPeriodRead:
    """After this, postings dated in the period are rejected."""
    return await FiscalPeriodService(uow).transition(
        company_id,
        period_id,
        FiscalPeriodStatus.CLOSED,
        actor,
        reason=payload.reason if payload else None,
    )


@router.post(
    "/{period_id}/adjusting",
    response_model=FiscalPeriodRead,
    summary="Move a period into ADJUSTING",
    dependencies=[Depends(require(Permission.PERIOD_CLOSE))],
)
async def set_period_adjusting(
    period_id: UUID,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
    payload: PeriodTransitionRequest | None = None,
) -> FiscalPeriodRead:
    """ADJUSTING accepts postings, so year-end adjustments can be made without
    reopening the period to ordinary traffic."""
    return await FiscalPeriodService(uow).transition(
        company_id,
        period_id,
        FiscalPeriodStatus.ADJUSTING,
        actor,
        reason=payload.reason if payload else None,
    )


@router.post(
    "/{period_id}/lock",
    response_model=FiscalPeriodRead,
    summary="Lock a fiscal period permanently",
    dependencies=[Depends(require(Permission.PERIOD_CLOSE))],
)
async def lock_fiscal_period(
    period_id: UUID,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
    payload: PeriodTransitionRequest | None = None,
) -> FiscalPeriodRead:
    """LOCKED is terminal — a locked period is a filed period."""
    return await FiscalPeriodService(uow).transition(
        company_id,
        period_id,
        FiscalPeriodStatus.LOCKED,
        actor,
        reason=payload.reason if payload else None,
    )


@router.post(
    "/{period_id}/reopen",
    response_model=FiscalPeriodRead,
    summary="Reopen a closed fiscal period",
    dependencies=[Depends(require(Permission.PERIOD_REOPEN))],
)
async def reopen_fiscal_period(
    period_id: UUID,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
    payload: PeriodTransitionRequest | None = None,
) -> FiscalPeriodRead:
    return await FiscalPeriodService(uow).transition(
        company_id,
        period_id,
        FiscalPeriodStatus.OPEN,
        actor,
        reason=payload.reason if payload else None,
    )
