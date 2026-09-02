"""Currency endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from app.application.currencies.schemas import (
    CurrencyCreate,
    CurrencyRead,
    CurrencyUpdate,
)
from app.application.currencies.service import CurrencyService
from app.core.deps import ActorDep, UowDep, require
from app.domain.identity.permissions import Permission

router = APIRouter(prefix="/currencies", tags=["currencies"])

CurrencyPath = Annotated[str, Path(min_length=3, max_length=3)]


@router.get(
    "",
    response_model=list[CurrencyRead],
    summary="List currencies",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def list_currencies(
    uow: UowDep, is_active: Annotated[bool | None, Query()] = None
) -> list[CurrencyRead]:
    return await CurrencyService(uow).list(is_active=is_active)


@router.post(
    "",
    response_model=CurrencyRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a currency",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def create_currency(payload: CurrencyCreate, uow: UowDep, actor: ActorDep) -> CurrencyRead:
    return await CurrencyService(uow).create(payload, actor)


@router.get(
    "/{currency_code}",
    response_model=CurrencyRead,
    summary="Fetch a currency",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def get_currency(currency_code: CurrencyPath, uow: UowDep) -> CurrencyRead:
    return await CurrencyService(uow).get(currency_code)


@router.patch(
    "/{currency_code}",
    response_model=CurrencyRead,
    summary="Update a currency",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def update_currency(
    currency_code: CurrencyPath, payload: CurrencyUpdate, uow: UowDep, actor: ActorDep
) -> CurrencyRead:
    return await CurrencyService(uow).update(currency_code, payload, actor)
