"""ORM model registry.

Importing this package registers every table on `Base.metadata`, which is what
Alembic autogenerate and the test bootstrap rely on. Import models from here
rather than from their individual modules.
"""

from __future__ import annotations

from app.infrastructure.database.base import Base
from app.infrastructure.database.models.account import Account
from app.infrastructure.database.models.audit import AuditLog
from app.infrastructure.database.models.fiscal_period import DocumentSequence, FiscalPeriod
from app.infrastructure.database.models.identity import (
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.infrastructure.database.models.journal import JournalEntry, JournalItem
from app.infrastructure.database.models.ledger import LedgerBalance
from app.infrastructure.database.models.organization import Company, Currency
from app.infrastructure.database.models.tax_code import TaxCode
from app.infrastructure.outbox.models import OutboxEvent

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
