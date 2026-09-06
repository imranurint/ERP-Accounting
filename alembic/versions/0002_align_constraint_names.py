"""Align CHECK constraints with the models.

The initial migration passed already-qualified names to `sa.CheckConstraint`,
e.g. ``name="ck_chart_of_accounts_parent_not_self"``. The metadata naming
convention (``ck_%(table_name)s_%(constraint_name)s``) then prefixed them a
second time, so the database ended up with
``ck_chart_of_accounts_ck_chart_of_accounts_parent_not_self`` while the models
declare ``ck_chart_of_accounts_parent_not_self``. Three names overflowed
PostgreSQL's 63-character identifier limit and were truncated with a hash
suffix.

Every constraint is present and enforces the correct expression — this is a
labelling defect, not a missing-protection one. But it left `alembic check`
permanently red, which meant it could never warn about a *real* divergence, and
any future ``DROP CONSTRAINT`` written against the model name would fail.

``ALTER TABLE ... RENAME CONSTRAINT`` is a catalogue-only operation: no table
rewrite, no revalidation of existing rows, no data touched. It takes a brief
ACCESS EXCLUSIVE lock per table.

This migration also adds one constraint that was simply **missing**. The model
declares ``document_type`` as ``Enum(DocumentType, create_constraint=True)``,
but the initial migration created the column as a bare ``VARCHAR(32)``, so
``document_sequences.document_type`` has been accepting any string while every
other enum column in the schema is constrained. The permanently-red
``alembic check`` is what kept that hidden.

Revision ID: 0002_align_constraint_names
Revises: 0001_initial
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_align_constraint_names"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, name the database currently has, name the models declare)
RENAMES: tuple[tuple[str, str, str], ...] = (
    (
        "chart_of_accounts",
        "ck_chart_of_accounts_ck_chart_of_accounts_account_numbe_95f3",
        "ck_chart_of_accounts_account_number_not_blank",
    ),
    (
        "chart_of_accounts",
        "ck_chart_of_accounts_ck_chart_of_accounts_account_type",
        "ck_chart_of_accounts_account_type",
    ),
    (
        "chart_of_accounts",
        "ck_chart_of_accounts_ck_chart_of_accounts_normal_balance",
        "ck_chart_of_accounts_normal_balance",
    ),
    (
        "chart_of_accounts",
        "ck_chart_of_accounts_ck_chart_of_accounts_parent_not_self",
        "ck_chart_of_accounts_parent_not_self",
    ),
    (
        "currencies",
        "ck_currencies_ck_currencies_currency_code_format",
        "ck_currencies_currency_code_format",
    ),
    (
        "currencies",
        "ck_currencies_ck_currencies_decimal_places_range",
        "ck_currencies_decimal_places_range",
    ),
    (
        "document_sequences",
        "ck_document_sequences_ck_document_sequences_next_number_e5f7",
        "ck_document_sequences_next_number_positive",
    ),
    (
        "fiscal_periods",
        "ck_fiscal_periods_ck_fiscal_periods_date_order",
        "ck_fiscal_periods_date_order",
    ),
    (
        "fiscal_periods",
        "ck_fiscal_periods_ck_fiscal_periods_fiscal_year_range",
        "ck_fiscal_periods_fiscal_year_range",
    ),
    (
        "fiscal_periods",
        "ck_fiscal_periods_ck_fiscal_periods_is_closed_matches_status",
        "ck_fiscal_periods_is_closed_matches_status",
    ),
    (
        "fiscal_periods",
        "ck_fiscal_periods_ck_fiscal_periods_status",
        "ck_fiscal_periods_fiscal_period_status",
    ),
    (
        "journal_entries",
        "ck_journal_entries_ck_journal_entries_entry_number_requ_7727",
        "ck_journal_entries_entry_number_required_when_posted",
    ),
    (
        "journal_entries",
        "ck_journal_entries_ck_journal_entries_idempotency_pair",
        "ck_journal_entries_idempotency_pair",
    ),
    (
        "journal_entries",
        "ck_journal_entries_ck_journal_entries_not_self_reversal",
        "ck_journal_entries_not_self_reversal",
    ),
    (
        "journal_entries",
        "ck_journal_entries_ck_journal_entries_not_self_reversed",
        "ck_journal_entries_not_self_reversed",
    ),
    (
        "journal_entries",
        "ck_journal_entries_ck_journal_entries_posted_at_matches_status",
        "ck_journal_entries_posted_at_matches_status",
    ),
    (
        "journal_entries",
        "ck_journal_entries_ck_journal_entries_status",
        "ck_journal_entries_journal_status",
    ),
    (
        "journal_entries",
        "ck_journal_entries_ck_journal_entries_version_positive",
        "ck_journal_entries_version_positive",
    ),
    (
        "journal_items",
        "ck_journal_items_ck_journal_items_amount_present",
        "ck_journal_items_amount_present",
    ),
    (
        "journal_items",
        "ck_journal_items_ck_journal_items_credit_non_negative",
        "ck_journal_items_credit_non_negative",
    ),
    (
        "journal_items",
        "ck_journal_items_ck_journal_items_debit_non_negative",
        "ck_journal_items_debit_non_negative",
    ),
    (
        "journal_items",
        "ck_journal_items_ck_journal_items_line_number_positive",
        "ck_journal_items_line_number_positive",
    ),
    (
        "journal_items",
        "ck_journal_items_ck_journal_items_single_sided",
        "ck_journal_items_single_sided",
    ),
    ("outbox_events", "ck_outbox_events_ck_outbox_events_status", "ck_outbox_events_outbox_status"),
    (
        "tax_codes",
        "ck_tax_codes_ck_tax_codes_effective_date_order",
        "ck_tax_codes_effective_date_order",
    ),
    ("tax_codes", "ck_tax_codes_ck_tax_codes_tax_rate_range", "ck_tax_codes_tax_rate_range"),
)


def _rename(table: str, old: str, new: str) -> None:
    """Rename only if `old` is actually present.

    A database created after the models were corrected may already carry the
    right name, so this stays safe to run against either state.
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{old}'
                  AND conrelid = '{table}'::regclass
            ) THEN
                ALTER TABLE {table} RENAME CONSTRAINT "{old}" TO "{new}";
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    for table, old, new in RENAMES:
        _rename(table, old, new)

    # The CHECK the initial migration failed to create. `NOT VALID` then
    # `VALIDATE` keeps the ACCESS EXCLUSIVE lock to a moment even on a large
    # table: the second step takes only a SHARE UPDATE EXCLUSIVE lock and does
    # not block writes.
    op.execute(
        """
        ALTER TABLE document_sequences
            ADD CONSTRAINT ck_document_sequences_document_type
            CHECK (document_type IN ('JOURNAL_ENTRY')) NOT VALID
        """
    )
    op.execute(
        "ALTER TABLE document_sequences VALIDATE CONSTRAINT ck_document_sequences_document_type"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE document_sequences DROP CONSTRAINT IF EXISTS "
        "ck_document_sequences_document_type"
    )
    for table, old, new in RENAMES:
        _rename(table, new, old)
