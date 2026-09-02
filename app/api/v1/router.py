"""API v1 router composition."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    accounts,
    auth,
    companies,
    currencies,
    fiscal_periods,
    journal_entries,
    reports,
    tax_codes,
)

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(companies.router)
api_router.include_router(currencies.router)
api_router.include_router(fiscal_periods.router)
api_router.include_router(accounts.router)
api_router.include_router(tax_codes.router)
api_router.include_router(journal_entries.router)
api_router.include_router(reports.router)
