"""Currency schemas."""

from __future__ import annotations

from pydantic import Field

from app.application.common import AppModel, CurrencyCode, ORMModel


class CurrencyCreate(AppModel):
    currency_code: CurrencyCode = Field(min_length=3, max_length=3)
    currency_name: str = Field(min_length=1, max_length=100)
    decimal_places: int = Field(default=2, ge=0, le=4)
    is_active: bool = True


class CurrencyUpdate(AppModel):
    currency_name: str | None = Field(default=None, min_length=1, max_length=100)
    decimal_places: int | None = Field(default=None, ge=0, le=4)
    is_active: bool | None = None


class CurrencyRead(ORMModel):
    currency_code: str
    currency_name: str
    decimal_places: int
    is_active: bool
