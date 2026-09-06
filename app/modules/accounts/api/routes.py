"""Chart of accounts endpoints."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.core.dependencies import ActorDep, UowDep, require
from app.core.pagination import Page, PageParams, page_params
from app.modules.accounts.application.schemas import AccountCreate, AccountRead, AccountUpdate
from app.modules.accounts.application.service import AccountService
from app.modules.identity.domain.permissions import Permission
from app.shared.domain.enums import AccountType

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get(
    "",
    response_model=Page[AccountRead],
    summary="List accounts",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def list_accounts(
    uow: UowDep,
    company_id: Annotated[UUID, Query()],
    params: Annotated[PageParams, Depends(page_params)],
    account_type: Annotated[AccountType | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    is_postable: Annotated[bool | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[AccountRead]:
    return await AccountService(uow).list(
        company_id,
        params,
        account_type=account_type,
        is_active=is_active,
        is_postable=is_postable,
        search=search,
    )


@router.post(
    "",
    response_model=AccountRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def create_account(payload: AccountCreate, uow: UowDep, actor: ActorDep) -> AccountRead:
    return await AccountService(uow).create(payload, actor)


@router.get(
    "/{account_id}",
    response_model=AccountRead,
    summary="Fetch an account",
    dependencies=[Depends(require(Permission.ACCOUNTING_READ))],
)
async def get_account(
    account_id: UUID, uow: UowDep, company_id: Annotated[UUID, Query()]
) -> AccountRead:
    return await AccountService(uow).get(company_id, account_id)


@router.patch(
    "/{account_id}",
    response_model=AccountRead,
    summary="Update an account",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def update_account(
    account_id: UUID,
    payload: AccountUpdate,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
) -> AccountRead:
    """`account_number`, `account_type` and `normal_balance` are immutable —
    changing them would silently reinterpret existing postings."""
    return await AccountService(uow).update(company_id, account_id, payload, actor)


@router.post(
    "/{account_id}/activate",
    response_model=AccountRead,
    summary="Activate an account",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def activate_account(
    account_id: UUID,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
) -> AccountRead:
    return await AccountService(uow).set_active(company_id, account_id, active=True, actor=actor)


@router.post(
    "/{account_id}/deactivate",
    response_model=AccountRead,
    summary="Deactivate an account",
    dependencies=[Depends(require(Permission.CONFIG_MANAGE))],
)
async def deactivate_account(
    account_id: UUID,
    uow: UowDep,
    actor: ActorDep,
    company_id: Annotated[UUID, Query()],
) -> AccountRead:
    """Deactivation replaces deletion: accounts with posted history are never
    removed, so the ledger can always be explained."""
    return await AccountService(uow).set_active(company_id, account_id, active=False, actor=actor)
