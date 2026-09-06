"""ORM model registry.

Importing this package registers every table on `Base.metadata`, which is what
Alembic autogenerate and the test bootstrap rely on. Import models from here
rather than from their individual modules.
"""

from __future__ import annotations

from app.modules.accounts.infrastructure.models import Account
from app.modules.fiscal_periods.infrastructure.models import DocumentSequence, FiscalPeriod
from app.modules.identity.infrastructure.models import (
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.modules.journal.infrastructure.models import JournalEntry, JournalItem, LedgerBalance
from app.modules.organization.infrastructure.models import Company, Currency
from app.modules.taxes.infrastructure.models import TaxCode
from app.shared.audit.models import AuditLog
from app.shared.database.base import Base
from app.shared.outbox.models import OutboxEvent

__all__ = [
    "Account",
    "AuditLog",
    "Base",
    "Company",
    "Currency",
    "DocumentSequence",
    "FiscalPeriod",
    "JournalEntry",
    "JournalItem",
    "LedgerBalance",
    "OutboxEvent",
    "Permission",
    "RefreshToken",
    "Role",
    "RolePermission",
    "TaxCode",
    "User",
    "UserRole",
]
