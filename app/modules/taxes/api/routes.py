"""Tax code endpoints."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.core.dependencies import ActorDep, UowDep, require
from app.core.pagination import Page, PageParams, page_params
from app.modules.identity.domain.permissions import Permission
from app.modules.taxes.application.schemas import TaxCodeCreate, TaxCodeRead, TaxCodeUpdate
from app.modules.taxes.application.service import TaxCodeService

router = APIRouter(prefix="/tax-codes", tags=["tax-codes"])


@router.get(
    "",
    response_model=Page[TaxCodeRead],
    summary="List tax codes",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def list_tax_codes(
    uow: UowDep,
    company_id: Annotated[UUID, Query()],
    params: Annotated[PageParams, Depends(page_params)],
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[TaxCodeRead]:
    return await TaxCodeService(uow).list(company_id, params, is_active=is_active)


@router.post(
    "",
    response_model=TaxCodeRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tax code",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def create_tax_code(payload: TaxCodeCreate, uow: UowDep, actor: ActorDep) -> TaxCodeRead:
    return await TaxCodeService(uow).create(payload, actor)


@router.get(
    "/{tax_id}",
    response_model=TaxCodeRead,
    summary="Fetch a tax code",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def get_tax_code(
    tax_id: UUID, uow: UowDep, company_id: Annotated[UUID, Query()]
) -> TaxCodeRead:
    return await TaxCodeService(uow).get(company_id, tax_id)


@router.patch(
    "/{tax_id}",
    response_model=TaxCodeRead,
    summary="Update a tax code",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def update_tax_code(
    tax_id: UUID,
    payload: TaxCodeUpdate,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
) -> TaxCodeRead:
    """Prefer closing a rate with `effective_to` and adding a new code over
    editing a rate that has already been used on posted entries."""
    return await TaxCodeService(uow).update(company_id, tax_id, payload, actor)
