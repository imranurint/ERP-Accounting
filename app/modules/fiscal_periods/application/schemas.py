"""Fiscal period schemas."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import Field, model_validator

from app.shared.domain.enums import FiscalPeriodStatus
from app.shared.schemas import AppModel, ORMModel


class FiscalPeriodCreate(AppModel):
    company_id: UUID
    fiscal_year: int = Field(ge=1900, le=9999)
    period_name: str = Field(min_length=1, max_length=100)
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _check_dates(self) -> FiscalPeriodCreate:
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self


class FiscalPeriodBulkCreate(AppModel):
    """Generate twelve monthly periods for a year in one call."""

    company_id: UUID
    fiscal_year: int = Field(ge=1900, le=9999)
    name_template: str = Field(
        default="{month_name} {year}",
        description="Supports {month_name}, {month}, {year}.",
    )


class FiscalPeriodRead(ORMModel):
    id: UUID
    company_id: UUID
    fiscal_year: int
    period_name: str
    start_date: date
    end_date: date
    status: FiscalPeriodStatus
    is_closed: bool
    closed_at: datetime | None
    closed_by: UUID | None
    created_at: datetime
    updated_at: datetime


class PeriodTransitionRequest(AppModel):
    reason: str | None = Field(default=None, max_length=500)
