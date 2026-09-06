"""Atomic document number allocation.

Journal numbers must be unique per company and year, allocated without a
`SELECT MAX()` and without any application-side counter. A single statement
does it:

    INSERT INTO document_sequences (..., next_number) VALUES (..., 2)
    ON CONFLICT (company_id, document_type, fiscal_year)
    DO UPDATE SET next_number = document_sequences.next_number + 1
    RETURNING next_number - 1

On first use the row is created with `next_number = 2` and 1 is returned. On
every later call PostgreSQL takes a row lock for the UPDATE, increments, and
returns the previous value. Concurrent callers serialise on that row only,
and no two transactions can ever be handed the same number.

Numbering policy: gaps are possible and acceptable. If the surrounding
transaction rolls back, the allocated number is simply never used — the
alternative (reusing numbers) would let two different entries share a number
over time, which is far worse for an audit trail.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.ids import uuid7
from app.shared.domain.enums import DocumentType

_ALLOCATE_SQL = text(
    """
    INSERT INTO document_sequences
        (id, company_id, document_type, fiscal_year, next_number, updated_at)
    VALUES
        (:id, :company_id, :document_type, :fiscal_year, 2, now())
    ON CONFLICT (company_id, document_type, fiscal_year)
    DO UPDATE SET
        next_number = document_sequences.next_number + 1,
        updated_at  = now()
    RETURNING document_sequences.next_number - 1 AS allocated
    """
)


class DocumentSequenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def allocate(
        self,
        company_id: UUID,
        fiscal_year: int,
        document_type: DocumentType = DocumentType.JOURNAL_ENTRY,
    ) -> int:
        """Reserve and return the next sequence number. Caller must be in a txn."""
        result = await self.session.execute(
            _ALLOCATE_SQL,
            {
                "id": uuid7(),
                "company_id": company_id,
                "document_type": document_type.value,
                "fiscal_year": fiscal_year,
            },
        )
        allocated = result.scalar_one()
        return int(allocated)

    async def allocate_journal_number(self, company_id: UUID, fiscal_year: int) -> str:
        """Return a formatted journal number, e.g. `JE-2026-000001`."""
        number = await self.allocate(company_id, fiscal_year, DocumentType.JOURNAL_ENTRY)
        return format_journal_number(fiscal_year, number)

    async def peek(
        self,
        company_id: UUID,
        fiscal_year: int,
        document_type: DocumentType = DocumentType.JOURNAL_ENTRY,
    ) -> int:
        """The number the next allocation would return. Diagnostics only."""
        stmt = text(
            """
            SELECT next_number FROM document_sequences
            WHERE company_id = :company_id
              AND document_type = :document_type
              AND fiscal_year = :fiscal_year
            """
        )
        value = await self.session.scalar(
            stmt,
            {
                "company_id": company_id,
                "document_type": document_type.value,
                "fiscal_year": fiscal_year,
            },
        )
        return int(value) if value is not None else 1


def format_journal_number(fiscal_year: int, number: int) -> str:
    return f"{settings.journal_number_prefix}-{fiscal_year}-{number:06d}"
