"""API v1 router composition.

Modules expose routers; this file is the only place that knows the full set.
Adding a module is one import and one `include_router`.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.modules.accounts.api import routes as accounts
from app.modules.fiscal_periods.api import routes as fiscal_periods
from app.modules.identity.api import routes as identity
from app.modules.journal.api import routes as journal
from app.modules.organization.api import routes as organization
from app.modules.reports.api import routes as reports
from app.modules.taxes.api import routes as taxes

api_router = APIRouter()

api_router.include_router(identity.router)
api_router.include_router(organization.router)
api_router.include_router(organization.currency_router)
api_router.include_router(fiscal_periods.router)
api_router.include_router(accounts.router)
api_router.include_router(taxes.router)
api_router.include_router(journal.router)
api_router.include_router(reports.router)
