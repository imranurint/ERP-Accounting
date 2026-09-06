"""Company and currency routes."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, status

from app.core.dependencies import ActorDep, UowDep, require
from app.core.pagination import Page, PageParams, page_params
from app.modules.identity.domain.permissions import Permission
from app.modules.organization.application.companies import CompanyService
from app.modules.organization.application.currencies import CurrencyService
from app.modules.organization.application.schemas import (
    CompanyCreate,
    CompanyRead,
    CompanyUpdate,
    CurrencyCreate,
    CurrencyRead,
    CurrencyUpdate,
)

router = APIRouter(prefix="/companies", tags=["companies"])


@router.post(
    "",
    response_model=CompanyRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a company",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def create_company(payload: CompanyCreate, uow: UowDep, actor: ActorDep) -> CompanyRead:
    return await CompanyService(uow).create(payload, actor)


@router.get(
    "",
    response_model=Page[CompanyRead],
    summary="List companies",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def list_companies(
    uow: UowDep,
    params: Annotated[PageParams, Depends(page_params)],
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[CompanyRead]:
    return await CompanyService(uow).list(params, is_active=is_active)


@router.get(
    "/{company_id}",
    response_model=CompanyRead,
    summary="Fetch a company",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def get_company(company_id: UUID, uow: UowDep) -> CompanyRead:
    return await CompanyService(uow).get(company_id)


@router.patch(
    "/{company_id}",
    response_model=CompanyRead,
    summary="Update a company",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def update_company(
    company_id: UUID, payload: CompanyUpdate, uow: UowDep, actor: ActorDep
) -> CompanyRead:
    """`base_currency_code` is deliberately not updatable: changing it would
    reinterpret every historical balance for the entity."""
    return await CompanyService(uow).update(company_id, payload, actor)


# ---------------------------------------------------------------- currencies
currency_router = APIRouter(prefix="/currencies", tags=["currencies"])

CurrencyPath = Annotated[str, Path(min_length=3, max_length=3)]


@currency_router.get(
    "",
    response_model=list[CurrencyRead],
    summary="List currencies",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def list_currencies(
    uow: UowDep, is_active: Annotated[bool | None, Query()] = None
) -> list[CurrencyRead]:
    return await CurrencyService(uow).list(is_active=is_active)


@currency_router.post(
    "",
    response_model=CurrencyRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a currency",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def create_currency(payload: CurrencyCreate, uow: UowDep, actor: ActorDep) -> CurrencyRead:
    return await CurrencyService(uow).create(payload, actor)


@currency_router.get(
    "/{currency_code}",
    response_model=CurrencyRead,
    summary="Fetch a currency",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def get_currency(currency_code: CurrencyPath, uow: UowDep) -> CurrencyRead:
    return await CurrencyService(uow).get(currency_code)


@currency_router.patch(
    "/{currency_code}",
    response_model=CurrencyRead,
    summary="Update a currency",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def update_currency(
    currency_code: CurrencyPath, payload: CurrencyUpdate, uow: UowDep, actor: ActorDep
) -> CurrencyRead:
    return await CurrencyService(uow).update(currency_code, payload, actor)
