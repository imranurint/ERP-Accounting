"""Company schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.application.common import AppModel, CurrencyCode, ORMModel


class CompanyCreate(AppModel):
    name: str = Field(min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, max_length=255)
    base_currency_code: CurrencyCode = Field(min_length=3, max_length=3)
    timezone: str = Field(default="UTC", max_length=64)


class CompanyUpdate(AppModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, max_length=255)
    timezone: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None


class CompanyRead(ORMModel):
    id: UUID
    name: str
    legal_name: str | None
    base_currency_code: str
    timezone: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
