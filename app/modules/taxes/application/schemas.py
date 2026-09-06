"""Tax code schemas."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import Field

from app.shared.schemas import AppModel, ORMModel, RateStr


class TaxCodeCreate(AppModel):
    company_id: UUID
    tax_code: str = Field(min_length=1, max_length=32)
    tax_name: str = Field(min_length=1, max_length=200)
    #: Percentage as a string, e.g. "15.00" for 15%.
    tax_rate: RateStr
    tax_account_id: UUID
    effective_from: date
    effective_to: date | None = None
    is_active: bool = True


class TaxCodeUpdate(AppModel):
    tax_name: str | None = Field(default=None, min_length=1, max_length=200)
    tax_rate: RateStr | None = None
    tax_account_id: UUID | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    is_active: bool | None = None


class TaxCodeRead(ORMModel):
    id: UUID
    company_id: UUID
    tax_code: str
    tax_name: str
    tax_rate: RateStr
    tax_account_id: UUID
    effective_from: date
    effective_to: date | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
