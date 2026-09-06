"""Unit of Work.

One UoW == one database transaction == one accounting operation. The posting
service opens a UoW, does everything (validate, allocate the number, insert
the header and lines, upsert ledger balances, write the audit log, enqueue the
outbox event) and commits once. Any failure rolls the whole thing back — there
is no code path that can leave a half-posted entry behind.

Repositories are attached here so services depend on the UoW rather than on a
session, keeping SQLAlchemy out of the application layer's signature.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.modules.accounts.infrastructure.repositories import AccountRepository
from app.modules.fiscal_periods.infrastructure.repositories import FiscalPeriodRepository
from app.modules.identity.infrastructure.repositories import (
    PermissionRepository,
    RefreshTokenRepository,
    RoleRepository,
    UserRepository,
)
from app.modules.journal.infrastructure.repositories import JournalRepository, LedgerRepository
from app.modules.organization.infrastructure.repositories import (
    CompanyRepository,
    CurrencyRepository,
)
from app.modules.reports.infrastructure.repositories import ReportRepository
from app.modules.taxes.infrastructure.repositories import TaxCodeRepository
from app.shared.audit.service import AuditRepository
from app.shared.outbox.service import OutboxRepository
from app.shared.sequences.service import DocumentSequenceRepository


class UnitOfWork:
    """Transaction scope plus the repositories that share it."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

        self.companies = CompanyRepository(session)
        self.currencies = CurrencyRepository(session)
        self.accounts = AccountRepository(session)
        self.fiscal_periods = FiscalPeriodRepository(session)
        self.tax_codes = TaxCodeRepository(session)
        self.journal = JournalRepository(session)
        self.ledger = LedgerRepository(session)
        self.sequences = DocumentSequenceRepository(session)
        self.audit = AuditRepository(session)
        self.outbox = OutboxRepository(session)
        self.reports = ReportRepository(session)
        self.users = UserRepository(session)
        self.roles = RoleRepository(session)
        self.permissions = PermissionRepository(session)
        self.refresh_tokens = RefreshTokenRepository(session)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # An unhandled exception must never leave a partially applied
        # accounting change visible to another transaction.
        if exc_type is not None:
            await self.rollback()

    async def commit(self) -> None:
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise translate_integrity_error(exc) from exc

    async def rollback(self) -> None:
        await self.session.rollback()

    async def flush(self) -> None:
        """Send pending INSERT/UPDATEs so constraints fire before we continue."""
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def execute(self, statement: Any, params: Any = None) -> Any:
        return await self.session.execute(statement, params)

    async def refresh(self, instance: Any, attribute_names: list[str] | None = None) -> None:
        """Reload an instance from the database.

        Called with no `attribute_names` after every write, because columns
        with a server default or `onupdate` (`created_at`, `updated_at`) are
        left expired by the flush. Touching one lazily later would attempt
        blocking IO from async code and raise `MissingGreenlet`.
        """
        await self.session.refresh(instance, attribute_names=attribute_names)


#: Database constraint name → the domain error it really means. Translating
#: here means a race that slips past an application check still surfaces as a
#: precise business error instead of a 500.
_CONSTRAINT_ERRORS: dict[str, tuple[str, str]] = {
    "uq_journal_entries_company_number": (
        "DUPLICATE_ENTRY_NUMBER",
        "That journal entry number already exists for this company.",
    ),
    "uq_journal_entries_company_idempotency_key": (
        "DUPLICATE_IDEMPOTENCY_KEY",
        "This idempotency key is already in use for this company.",
    ),
    "uq_chart_of_accounts_company_number": (
        "DUPLICATE_ACCOUNT_NUMBER",
        "That account number already exists for this company.",
    ),
    "uq_tax_codes_company_code": (
        "DUPLICATE_TAX_CODE",
        "That tax code already exists for this company.",
    ),
    "uq_fiscal_periods_company_name_year": (
        "DUPLICATE_FISCAL_PERIOD",
        "A fiscal period with that name already exists for this year.",
    ),
    "ex_fiscal_periods_no_overlap": (
        "FISCAL_PERIOD_OVERLAP",
        "Fiscal periods for a company must not overlap.",
    ),
    "uq_journal_items_entry_line": (
        "INVALID_JOURNAL_LINE",
        "Duplicate line number within the journal entry.",
    ),
    "ck_journal_items_single_sided": (
        "INVALID_JOURNAL_LINE",
        "A journal line may not contain both a debit and a credit.",
    ),
    "ck_journal_items_amount_present": (
        "INVALID_JOURNAL_LINE",
        "A journal line must carry a debit or a credit amount.",
    ),
    "uq_users_email": (
        "EMAIL_ALREADY_REGISTERED",
        "An account with this email address already exists.",
    ),
}


def translate_integrity_error(exc: IntegrityError) -> ConflictError:
    detail = str(getattr(exc.orig, "args", ("",))[0] if exc.orig else exc)
    for constraint, (code, message) in _CONSTRAINT_ERRORS.items():
        if constraint in detail:
            return ConflictError(message, code=code, details={"constraint": constraint})
    return ConflictError(
        "The operation violated a database constraint.",
        code="CONSTRAINT_VIOLATION",
        details={"constraint": None},
    )
