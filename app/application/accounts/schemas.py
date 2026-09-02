"""Chart of accounts schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, model_validator

from app.application.common import AppModel, CurrencyCode, ORMModel
from app.domain.accounting.enums import AccountType, NormalBalance


class AccountCreate(AppModel):
    company_id: UUID
    account_number: str = Field(min_length=1, max_length=32)
    account_name: str = Field(min_length=1, max_length=200)
    account_type: AccountType
    normal_balance: NormalBalance | None = Field(
        default=None,
        description="Defaults to the account type's natural side. Override only "
        "for contra accounts (e.g. accumulated depreciation).",
    )
    currency_code: CurrencyCode | None = Field(
        default=None, description="Pin the account to a single currency."
    )
    parent_account_id: UUID | None = None
    is_postable: bool = True
    is_active: bool = True
    description: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _default_normal_balance(self) -> AccountCreate:
        if self.normal_balance is None:
            self.normal_balance = self.account_type.normal_balance
        return self


class AccountUpdate(AppModel):
    account_name: str | None = Field(default=None, min_length=1, max_length=200)
    currency_code: CurrencyCode | None = None
    parent_account_id: UUID | None = None
    is_postable: bool | None = None
    description: str | None = Field(default=None, max_length=500)


class AccountRead(ORMModel):
    id: UUID
    company_id: UUID
    account_number: str
    account_name: str
    account_type: AccountType
    normal_balance: NormalBalance
    currency_code: str | None
    parent_account_id: UUID | None
    is_active: bool
    is_postable: bool
    description: str | None
    created_at: datetime
    updated_at: datetime
