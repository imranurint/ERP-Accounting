"""Initial schema: identity, accounting configuration, journals, ledger, audit, outbox.

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-01

Design notes worth keeping with the DDL:

* Money is NUMERIC(19, 4) everywhere. There is no FLOAT column in this schema.
* Enumerations are VARCHAR + CHECK rather than native PostgreSQL enum types,
  so adding a value later is a constraint swap instead of a type migration.
* `journal_items` carries the debit/credit invariants as CHECK constraints.
  The application enforces them too; the database is the last line of defence.
* `fiscal_periods` uses a GiST exclusion constraint to make overlapping
  periods impossible even under concurrent inserts.
* The idempotency uniqueness is a *partial* index, so entries without a key
  are unconstrained.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(19, 4)
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    # Required for the fiscal period overlap exclusion constraint.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # ---------------------------------------------------------------- currencies
    op.create_table(
        "currencies",
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("currency_name", sa.String(100), nullable=False),
        sa.Column("decimal_places", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("currency_code", name="pk_currencies"),
        sa.CheckConstraint(
            "currency_code = upper(currency_code) AND length(currency_code) = 3",
            name="ck_currencies_currency_code_format",
        ),
        sa.CheckConstraint(
            "decimal_places >= 0 AND decimal_places <= 4",
            name="ck_currencies_decimal_places_range",
        ),
    )

    # --------------------------------------------------------------------- users
    op.create_table(
        "users",
        sa.Column("id", UUID, nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_superuser", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email_lower", "users", ["email"])

    op.create_table(
        "roles",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )

    op.create_table(
        "permissions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_permissions"),
        sa.UniqueConstraint("code", name="uq_permissions_code"),
    )

    op.create_table(
        "user_roles",
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("role_id", UUID, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_roles_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name="fk_user_roles_role_id_roles", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role_id", name="pk_user_roles"),
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_id", UUID, nullable=False),
        sa.Column("permission_id", UUID, nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name="fk_role_permissions_role_id_roles", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["permission_id"], ["permissions.id"], name="fk_role_permissions_permission_id_permissions", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name="pk_role_permissions"),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", UUID, nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_refresh_tokens_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"], ["refresh_tokens.id"], name="fk_refresh_tokens_replaced_by_id_refresh_tokens", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_created_at", "refresh_tokens", ["created_at"])
    op.create_index("ix_refresh_tokens_user_active", "refresh_tokens", ["user_id", "revoked_at"])

    # ----------------------------------------------------------------- companies
    op.create_table(
        "companies",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("legal_name", sa.String(255), nullable=True),
        sa.Column("base_currency_code", sa.String(3), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["base_currency_code"], ["currencies.currency_code"], name="fk_companies_base_currency_code_currencies", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_companies"),
    )

    # --------------------------------------------------------- chart of accounts
    op.create_table(
        "chart_of_accounts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("account_number", sa.String(32), nullable=False),
        sa.Column("account_name", sa.String(200), nullable=False),
        sa.Column("account_type", sa.String(16), nullable=False),
        sa.Column("normal_balance", sa.String(8), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=True),
        sa.Column("parent_account_id", UUID, nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_postable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_chart_of_accounts_company_id_companies", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["currency_code"], ["currencies.currency_code"], name="fk_chart_of_accounts_currency_code_currencies", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["parent_account_id"], ["chart_of_accounts.id"], name="fk_chart_of_accounts_parent_account_id_chart_of_accounts", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chart_of_accounts"),
        sa.UniqueConstraint("company_id", "account_number", name="uq_chart_of_accounts_company_number"),
        sa.CheckConstraint("id <> parent_account_id", name="ck_chart_of_accounts_parent_not_self"),
        sa.CheckConstraint("length(trim(account_number)) > 0", name="ck_chart_of_accounts_account_number_not_blank"),
        sa.CheckConstraint(
            "account_type IN ('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE')",
            name="ck_chart_of_accounts_account_type",
        ),
        sa.CheckConstraint("normal_balance IN ('DEBIT', 'CREDIT')", name="ck_chart_of_accounts_normal_balance"),
    )
    op.create_index("ix_chart_of_accounts_company_type", "chart_of_accounts", ["company_id", "account_type"])
    op.create_index("ix_chart_of_accounts_parent", "chart_of_accounts", ["parent_account_id"])

    # ------------------------------------------------------------ fiscal periods
    op.create_table(
        "fiscal_periods",
        sa.Column("id", UUID, nullable=False),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("period_name", sa.String(100), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_fiscal_periods_company_id_companies", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["closed_by"], ["users.id"], name="fk_fiscal_periods_closed_by_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_fiscal_periods"),
        sa.UniqueConstraint("company_id", "period_name", "fiscal_year", name="uq_fiscal_periods_company_name_year"),
        sa.CheckConstraint("start_date <= end_date", name="ck_fiscal_periods_date_order"),
        sa.CheckConstraint("fiscal_year >= 1900 AND fiscal_year <= 9999", name="ck_fiscal_periods_fiscal_year_range"),
        sa.CheckConstraint(
            "status IN ('OPEN', 'CLOSED', 'ADJUSTING', 'LOCKED')",
            name="ck_fiscal_periods_status",
        ),
        sa.CheckConstraint(
            "(is_closed = true AND status IN ('CLOSED', 'LOCKED'))"
            " OR (is_closed = false AND status IN ('OPEN', 'ADJUSTING'))",
            name="ck_fiscal_periods_is_closed_matches_status",
        ),
    )
    op.create_index("ix_fiscal_periods_company_dates", "fiscal_periods", ["company_id", "start_date", "end_date"])
    op.create_index("ix_fiscal_periods_company_year", "fiscal_periods", ["company_id", "fiscal_year"])

    # Two periods of the same company may not cover the same day. An
    # application check alone would lose a concurrent-insert race.
    op.execute(
        """
        ALTER TABLE fiscal_periods
        ADD CONSTRAINT ex_fiscal_periods_no_overlap
        EXCLUDE USING gist (
            company_id WITH =,
            daterange(start_date, end_date, '[]') WITH &&
        )
        """
    )

    # ----------------------------------------------------------------- tax codes
    op.create_table(
        "tax_codes",
        sa.Column("id", UUID, nullable=False),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("tax_code", sa.String(32), nullable=False),
        sa.Column("tax_name", sa.String(200), nullable=False),
        sa.Column("tax_rate", sa.Numeric(8, 4), nullable=False),
        sa.Column("tax_account_id", UUID, nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_tax_codes_company_id_companies", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tax_account_id"], ["chart_of_accounts.id"], name="fk_tax_codes_tax_account_id_chart_of_accounts", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tax_codes"),
        sa.UniqueConstraint("company_id", "tax_code", name="uq_tax_codes_company_code"),
        sa.CheckConstraint("tax_rate >= 0 AND tax_rate <= 100", name="ck_tax_codes_tax_rate_range"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from <= effective_to",
            name="ck_tax_codes_effective_date_order",
        ),
    )
    op.create_index("ix_tax_codes_company_active", "tax_codes", ["company_id", "is_active"])

    # -------------------------------------------------------- document sequences
    op.create_table(
        "document_sequences",
        sa.Column("id", UUID, nullable=False),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("document_type", sa.String(32), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("next_number", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_document_sequences_company_id_companies", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_document_sequences"),
        sa.UniqueConstraint("company_id", "document_type", "fiscal_year", name="uq_document_sequences_company_type_year"),
        sa.CheckConstraint("next_number >= 1", name="ck_document_sequences_next_number_positive"),
    )

    # ----------------------------------------------------------- journal entries
    op.create_table(
        "journal_entries",
        sa.Column("id", UUID, nullable=False),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("entry_number", sa.String(50), nullable=True),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("period_id", UUID, nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=True),
        sa.Column("source_document", sa.String(128), nullable=True),
        sa.Column("reference", sa.String(255), nullable=True),
        sa.Column("memo", sa.String(1000), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("idempotency_payload_hash", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", UUID, nullable=True),
        sa.Column("reversed_by_entry_id", UUID, nullable=True),
        sa.Column("reverses_entry_id", UUID, nullable=True),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_by", UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_journal_entries_company_id_companies", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["period_id"], ["fiscal_periods.id"], name="fk_journal_entries_period_id_fiscal_periods", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["currency_code"], ["currencies.currency_code"], name="fk_journal_entries_currency_code_currencies", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["posted_by"], ["users.id"], name="fk_journal_entries_posted_by_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_journal_entries_created_by_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name="fk_journal_entries_updated_by_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["reversed_by_entry_id"], ["journal_entries.id"], name="fk_journal_entries_reversed_by_entry_id_journal_entries", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reverses_entry_id"], ["journal_entries.id"], name="fk_journal_entries_reverses_entry_id_journal_entries", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_journal_entries"),
        sa.UniqueConstraint("company_id", "entry_number", name="uq_journal_entries_company_number"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'POSTED', 'REVERSED', 'VOID')",
            name="ck_journal_entries_status",
        ),
        sa.CheckConstraint(
            "(status IN ('POSTED', 'REVERSED') AND entry_number IS NOT NULL)"
            " OR (status IN ('DRAFT', 'VOID') AND entry_number IS NULL)",
            name="ck_journal_entries_entry_number_required_when_posted",
        ),
        sa.CheckConstraint(
            "(status IN ('POSTED', 'REVERSED') AND posted_at IS NOT NULL)"
            " OR (status IN ('DRAFT', 'VOID') AND posted_at IS NULL)",
            name="ck_journal_entries_posted_at_matches_status",
        ),
        sa.CheckConstraint(
            "(idempotency_key IS NULL AND idempotency_payload_hash IS NULL)"
            " OR (idempotency_key IS NOT NULL AND idempotency_payload_hash IS NOT NULL)",
            name="ck_journal_entries_idempotency_pair",
        ),
        sa.CheckConstraint("version >= 1", name="ck_journal_entries_version_positive"),
        sa.CheckConstraint("id <> reversed_by_entry_id", name="ck_journal_entries_not_self_reversed"),
        sa.CheckConstraint("id <> reverses_entry_id", name="ck_journal_entries_not_self_reversal"),
    )
    op.create_index("ix_journal_entries_company_date", "journal_entries", ["company_id", "entry_date"])
    op.create_index("ix_journal_entries_company_status", "journal_entries", ["company_id", "status"])
    op.create_index("ix_journal_entries_period", "journal_entries", ["period_id"])
    op.create_index("ix_journal_entries_source", "journal_entries", ["company_id", "source_type", "source_document"])
    op.create_index(
        "uq_journal_entries_company_idempotency_key",
        "journal_entries",
        ["company_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_journal_entries_posted",
        "journal_entries",
        ["company_id", "period_id", "entry_date"],
        postgresql_where=sa.text("status IN ('POSTED', 'REVERSED')"),
    )

    # ------------------------------------------------------------- journal items
    op.create_table(
        "journal_items",
        sa.Column("id", UUID, nullable=False),
        sa.Column("entry_id", UUID, nullable=False),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("debit", MONEY, nullable=False, server_default="0"),
        sa.Column("credit", MONEY, nullable=False, server_default="0"),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("tax_id", UUID, nullable=True),
        sa.Column("memo", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["entry_id"], ["journal_entries.id"], name="fk_journal_items_entry_id_journal_entries", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_journal_items_company_id_companies", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["chart_of_accounts.id"], name="fk_journal_items_account_id_chart_of_accounts", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["currency_code"], ["currencies.currency_code"], name="fk_journal_items_currency_code_currencies", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["tax_id"], ["tax_codes.id"], name="fk_journal_items_tax_id_tax_codes", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_journal_items"),
        sa.UniqueConstraint("entry_id", "line_number", name="uq_journal_items_entry_line"),
        # The double-entry invariants, enforced by the database itself.
        sa.CheckConstraint("debit >= 0", name="ck_journal_items_debit_non_negative"),
        sa.CheckConstraint("credit >= 0", name="ck_journal_items_credit_non_negative"),
        sa.CheckConstraint("debit + credit > 0", name="ck_journal_items_amount_present"),
        sa.CheckConstraint(
            "(debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)",
            name="ck_journal_items_single_sided",
        ),
        sa.CheckConstraint("line_number >= 1", name="ck_journal_items_line_number_positive"),
    )
    op.create_index("ix_journal_items_entry", "journal_items", ["entry_id"])
    op.create_index("ix_journal_items_account", "journal_items", ["company_id", "account_id"])
    op.create_index("ix_journal_items_account_only", "journal_items", ["account_id"])

    # ----------------------------------------------------------- ledger balances
    op.create_table(
        "ledger_balances",
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("period_id", UUID, nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("opening_balance", MONEY, nullable=False, server_default="0"),
        sa.Column("total_debits", MONEY, nullable=False, server_default="0"),
        sa.Column("total_credits", MONEY, nullable=False, server_default="0"),
        sa.Column("closing_balance", MONEY, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_ledger_balances_company_id_companies", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["chart_of_accounts.id"], name="fk_ledger_balances_account_id_chart_of_accounts", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["period_id"], ["fiscal_periods.id"], name="fk_ledger_balances_period_id_fiscal_periods", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["currency_code"], ["currencies.currency_code"], name="fk_ledger_balances_currency_code_currencies", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("company_id", "account_id", "period_id", "currency_code", name="pk_ledger_balances"),
    )
    op.create_index("ix_ledger_balances_company_period", "ledger_balances", ["company_id", "period_id"])
    op.create_index("ix_ledger_balances_account", "ledger_balances", ["account_id"])

    # --------------------------------------------------------------- audit logs
    op.create_table(
        "audit_logs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("company_id", UUID, nullable=True),
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], name="fk_audit_logs_company_id_companies", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_audit_logs_user_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_company_created", "audit_logs", ["company_id", "created_at"])
    op.create_index("ix_audit_logs_entity", "audit_logs", ["entity_type", "entity_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_user", "audit_logs", ["user_id"])

    # ------------------------------------------------------------ outbox events
    op.create_table(
        "outbox_events",
        sa.Column("id", UUID, nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", UUID, nullable=False),
        sa.Column("company_id", UUID, nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'PROCESSED', 'FAILED')",
            name="ck_outbox_events_status",
        ),
    )
    op.create_index("ix_outbox_events_created_at", "outbox_events", ["created_at"])
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index("ix_outbox_events_aggregate", "outbox_events", ["aggregate_type", "aggregate_id"])


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_table("audit_logs")
    op.drop_table("ledger_balances")
    op.drop_table("journal_items")
    op.drop_table("journal_entries")
    op.drop_table("document_sequences")
    op.drop_table("tax_codes")
    op.execute("ALTER TABLE fiscal_periods DROP CONSTRAINT IF EXISTS ex_fiscal_periods_no_overlap")
    op.drop_table("fiscal_periods")
    op.drop_table("chart_of_accounts")
    op.drop_table("companies")
    op.drop_table("refresh_tokens")
    op.drop_table("role_permissions")
    op.drop_table("user_roles")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_table("users")
    op.drop_table("currencies")
