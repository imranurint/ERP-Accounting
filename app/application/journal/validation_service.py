"""Journal validation.

Steps 2-22 of the posting pipeline live here. The service resolves every
reference the command makes (company, currency, period, accounts, tax codes)
in a bounded number of queries, then hands the resolved objects to the pure
functions in `app.domain.accounting.rules`.

Resolution is batched on purpose: a 500-line payroll journal costs the same
number of round-trips as a 2-line invoice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.application.journal.commands import CreateJournalCommand, JournalLineCommand
from app.core.exceptions import NotFoundError
from app.core.money import quantize_storage
from app.domain.accounting import rules
from app.domain.accounting.entities import Account as AccountEntity
from app.domain.accounting.entities import Currency as CurrencyEntity
from app.domain.accounting.entities import FiscalPeriod as FiscalPeriodEntity
from app.domain.accounting.entities import JournalEntry, JournalLine
from app.domain.accounting.entities import TaxCode as TaxCodeEntity
from app.domain.accounting.exceptions import (
    CompanyInactiveError,
    CurrencyInactiveError,
    FiscalPeriodNotFoundForDateError,
)
from app.domain.accounting.value_objects import DebitCredit
from app.infrastructure.database.models.account import Account
from app.infrastructure.database.models.fiscal_period import FiscalPeriod
from app.infrastructure.database.models.organization import Company, Currency
from app.infrastructure.database.unit_of_work import UnitOfWork


@dataclass(slots=True)
class ValidatedEntry:
    """Everything the posting service needs, already checked."""

    entry: JournalEntry
    company: Company
    currency: Currency
    period: FiscalPeriod
    accounts: dict[UUID, AccountEntity]


def to_account_entity(model: Account) -> AccountEntity:
    return AccountEntity(
        id=model.id,
        company_id=model.company_id,
        account_number=model.account_number,
        account_name=model.account_name,
        account_type=model.account_type,
        normal_balance=model.normal_balance,
        currency_code=model.currency_code,
        parent_account_id=model.parent_account_id,
        is_active=model.is_active,
        is_postable=model.is_postable,
    )


def to_currency_entity(model: Currency) -> CurrencyEntity:
    return CurrencyEntity(
        currency_code=model.currency_code,
        currency_name=model.currency_name,
        decimal_places=model.decimal_places,
        is_active=model.is_active,
    )


def to_period_entity(model: FiscalPeriod) -> FiscalPeriodEntity:
    return FiscalPeriodEntity(
        id=model.id,
        company_id=model.company_id,
        fiscal_year=model.fiscal_year,
        period_name=model.period_name,
        start_date=model.start_date,
        end_date=model.end_date,
        status=model.status,
        is_closed=model.is_closed,
    )


class JournalValidationService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    # ------------------------------------------------------------------ main
    async def validate_create(
        self, command: CreateJournalCommand, *, lock_period: bool
    ) -> ValidatedEntry:
        """Validate a command end to end.

        `lock_period=True` takes `SELECT ... FOR UPDATE` on the fiscal period,
        which the posting path always does and the draft path never does — a
        draft has no accounting effect, so it must not block a period close.
        """
        company = await self.resolve_company(command.company_id)
        currency = await self.resolve_currency(command.currency_code)
        period = await self._resolve_period(
            command.company_id, command.entry_date, command.period_id, lock=lock_period
        )

        period_entity = to_period_entity(period)
        rules.validate_period_covers_date(period_entity, command.entry_date)
        if lock_period:
            rules.validate_period_accepts_posting(period_entity)

        accounts = await self._resolve_accounts(command)
        tax_codes = await self._resolve_tax_codes(command)

        currency_entity = to_currency_entity(currency)
        lines = self._build_lines(command, accounts, tax_codes, currency_entity)

        entry = JournalEntry(
            company_id=command.company_id,
            entry_date=command.entry_date,
            period_id=period.id,
            currency_code=command.currency_code.upper(),
            lines=lines,
            source_type=command.source_type,
            source_document=command.source_document,
            reference=command.reference,
            idempotency_key=command.idempotency_key,
            idempotency_payload_hash=command.idempotency_payload_hash,
        )

        rules.validate_balanced(entry)

        return ValidatedEntry(
            entry=entry,
            company=company,
            currency=currency,
            period=period,
            accounts=dict(accounts),
        )

    # ------------------------------------------------------------- resolution
    async def resolve_company(self, company_id: UUID) -> Company:
        company = await self.uow.companies.get(company_id)
        if company is None:
            raise NotFoundError("Company not found.", details={"company_id": str(company_id)})
        if not company.is_active:
            raise CompanyInactiveError(details={"company_id": str(company_id)})
        return company

    async def resolve_currency(self, currency_code: str) -> Currency:
        currency = await self.uow.currencies.get_by_code(currency_code)
        if currency is None:
            raise NotFoundError("Currency not found.", details={"currency_code": currency_code})
        if not currency.is_active:
            raise CurrencyInactiveError(details={"currency_code": currency_code})
        return currency

    async def _resolve_period(
        self,
        company_id: UUID,
        entry_date: date,
        period_id: UUID | None,
        *,
        lock: bool,
    ) -> FiscalPeriod:
        if period_id is None:
            found = await self.uow.fiscal_periods.find_for_date(company_id, entry_date)
            if found is None:
                raise FiscalPeriodNotFoundForDateError(
                    details={
                        "entry_date": entry_date.isoformat(),
                        "company_id": str(company_id),
                    }
                )
            period_id = found.id

        if lock:
            period = await self.uow.fiscal_periods.lock_for_update(company_id, period_id)
        else:
            period = await self.uow.fiscal_periods.get_for_company(company_id, period_id)

        if period is None:
            raise NotFoundError("Fiscal period not found.", details={"period_id": str(period_id)})
        return period

    async def _resolve_accounts(self, command: CreateJournalCommand) -> dict[UUID, AccountEntity]:
        """Fetch every referenced account in at most two queries."""
        ids = [line.account_id for line in command.lines if line.account_id is not None]
        numbers = [
            line.account_number
            for line in command.lines
            if line.account_id is None and line.account_number
        ]

        by_id = await self.uow.accounts.get_many_by_id(command.company_id, ids)
        by_number = await self.uow.accounts.get_many_by_number(command.company_id, numbers)

        resolved: dict[UUID, AccountEntity] = {}
        for line in command.lines:
            model = (
                by_id.get(line.account_id)
                if line.account_id is not None
                else by_number.get(line.account_number or "")
            )
            if model is None:
                raise NotFoundError(
                    "Account not found for this company.",
                    code="ACCOUNT_NOT_FOUND",
                    details={
                        "line_number": line.line_number,
                        "account_id": str(line.account_id) if line.account_id else None,
                        "account_number": line.account_number,
                    },
                )
            resolved[model.id] = to_account_entity(model)
        return resolved

    async def _resolve_tax_codes(self, command: CreateJournalCommand) -> dict[str, TaxCodeEntity]:
        """Keyed by both id-string and code so line lookup is uniform."""
        ids = [line.tax_id for line in command.lines if line.tax_id is not None]
        codes = [line.tax_code for line in command.lines if line.tax_code]
        if not ids and not codes:
            return {}

        by_id = await self.uow.tax_codes.get_many_by_id(command.company_id, ids)
        by_code = await self.uow.tax_codes.get_many_by_code(
            command.company_id, [code for code in codes if code]
        )

        resolved: dict[str, TaxCodeEntity] = {}
        for model in list(by_id.values()) + list(by_code.values()):
            entity = TaxCodeEntity(
                id=model.id,
                company_id=model.company_id,
                tax_code=model.tax_code,
                tax_name=model.tax_name,
                tax_rate=model.tax_rate,
                tax_account_id=model.tax_account_id,
                effective_from=model.effective_from,
                effective_to=model.effective_to,
                is_active=model.is_active,
            )
            resolved[str(model.id)] = entity
            resolved[model.tax_code] = entity
        return resolved

    # ------------------------------------------------------------------ lines
    def _build_lines(
        self,
        command: CreateJournalCommand,
        accounts: dict[UUID, AccountEntity],
        tax_codes: dict[str, TaxCodeEntity],
        currency: CurrencyEntity,
    ) -> list[JournalLine]:
        by_number = {account.account_number: account for account in accounts.values()}
        lines: list[JournalLine] = []

        for raw in sorted(command.lines, key=lambda ln: ln.line_number):
            account = self._line_account(raw, accounts, by_number)
            tax = self._line_tax_code(raw, tax_codes)

            line = JournalLine(
                line_number=raw.line_number,
                account_id=account.id,
                amounts=DebitCredit(
                    debit=quantize_storage(raw.debit),
                    credit=quantize_storage(raw.credit),
                ),
                currency_code=(raw.currency_code or command.currency_code).upper(),
                tax_id=tax.id if tax else None,
                memo=raw.memo,
            )

            rules.validate_line_amounts(line)
            rules.validate_money_precision(line, currency)
            rules.validate_line_currency(line, command.currency_code)
            rules.validate_account_usable(account, command.company_id, line.line_number)
            rules.validate_account_currency(account, command.currency_code, line.line_number)
            if tax is not None:
                rules.validate_tax_code_usable(
                    tax, command.company_id, command.entry_date, line.line_number
                )
            lines.append(line)

        rules.validate_line_numbers(lines)
        return lines

    @staticmethod
    def _line_account(
        raw: JournalLineCommand,
        accounts: dict[UUID, AccountEntity],
        by_number: dict[str, AccountEntity],
    ) -> AccountEntity:
        if raw.account_id is not None and raw.account_id in accounts:
            return accounts[raw.account_id]
        if raw.account_number and raw.account_number in by_number:
            return by_number[raw.account_number]
        raise NotFoundError(
            "Account not found for this company.",
            code="ACCOUNT_NOT_FOUND",
            details={"line_number": raw.line_number, "account_number": raw.account_number},
        )

    @staticmethod
    def _line_tax_code(
        raw: JournalLineCommand, tax_codes: dict[str, TaxCodeEntity]
    ) -> TaxCodeEntity | None:
        key: str | None = None
        if raw.tax_id is not None:
            key = str(raw.tax_id)
        elif raw.tax_code:
            key = raw.tax_code
        if key is None:
            return None
        tax = tax_codes.get(key)
        if tax is None:
            raise NotFoundError(
                "Tax code not found for this company.",
                code="TAX_CODE_NOT_FOUND",
                details={"line_number": raw.line_number, "tax_code": raw.tax_code},
            )
        return tax
