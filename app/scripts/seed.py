"""Seed a demo company with a working chart of accounts.

    python -m app.scripts.seed
    python -m app.scripts.seed --year 2026 --company "Demo Company Ltd"

Idempotent: re-running will not duplicate anything.

Passwords come from the environment (`SEED_*_PASSWORD`). When unset, secure
random passwords are generated and printed once. Never set these variables in
production, and never commit real ones.

The seeded chart is deliberately generic. This ledger is the financial core
for any vertical — an education consultancy billing tuition, a manufacturer
issuing invoices, a retailer running a POS — because none of that
domain-specific meaning lives in the GL. Modules describe *what* happened via
`source_type` / `source_document`; the GL only records the double entry.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import secrets
import sys
from datetime import date
from decimal import Decimal
from uuid import UUID

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.domain.accounting.enums import AccountType, FiscalPeriodStatus, NormalBalance
from app.domain.identity.permissions import Role
from app.infrastructure.database.models.account import Account
from app.infrastructure.database.models.fiscal_period import FiscalPeriod
from app.infrastructure.database.models.identity import User
from app.infrastructure.database.models.organization import Company, Currency
from app.infrastructure.database.models.tax_code import TaxCode
from app.infrastructure.database.session import dispose_engine, session_scope
from app.infrastructure.database.unit_of_work import UnitOfWork
from app.scripts.create_admin import ensure_roles_and_permissions

logger = get_logger("seed")

CURRENCIES: list[tuple[str, str, int]] = [
    ("USD", "US Dollar", 2),
    ("EUR", "Euro", 2),
    ("GBP", "Pound Sterling", 2),
]

#: (number, name, type, normal balance, postable)
CHART_OF_ACCOUNTS: list[tuple[str, str, AccountType, NormalBalance, bool]] = [
    ("100", "Current Assets", AccountType.ASSET, NormalBalance.DEBIT, False),
    ("101", "Bank Account", AccountType.ASSET, NormalBalance.DEBIT, True),
    ("120", "Accounts Receivable", AccountType.ASSET, NormalBalance.DEBIT, True),
    ("150", "Inventory", AccountType.ASSET, NormalBalance.DEBIT, True),
    ("200", "Current Liabilities", AccountType.LIABILITY, NormalBalance.CREDIT, False),
    ("205", "Sales Tax Payable", AccountType.LIABILITY, NormalBalance.CREDIT, True),
    ("210", "Accounts Payable", AccountType.LIABILITY, NormalBalance.CREDIT, True),
    ("300", "Owner Equity", AccountType.EQUITY, NormalBalance.CREDIT, True),
    ("310", "Retained Earnings", AccountType.EQUITY, NormalBalance.CREDIT, True),
    ("402", "SaaS Revenue", AccountType.REVENUE, NormalBalance.CREDIT, True),
    ("410", "Service Revenue", AccountType.REVENUE, NormalBalance.CREDIT, True),
    ("500", "Cost of Goods Sold", AccountType.EXPENSE, NormalBalance.DEBIT, True),
    ("600", "Operating Expenses", AccountType.EXPENSE, NormalBalance.DEBIT, True),
    ("610", "Payroll Expenses", AccountType.EXPENSE, NormalBalance.DEBIT, True),
]

#: Parent roll-ups, applied after the accounts exist.
ACCOUNT_PARENTS: dict[str, str] = {
    "101": "100",
    "120": "100",
    "150": "100",
    "205": "200",
    "210": "200",
}

SEED_USERS: list[tuple[str, str, Role, str]] = [
    ("admin@example.com", "Demo Administrator", Role.ADMIN, "seed_admin_password"),
    ("accountant@example.com", "Demo Accountant", Role.ACCOUNTANT, "seed_accountant_password"),
    ("auditor@example.com", "Demo Auditor", Role.AUDITOR, "seed_auditor_password"),
    ("viewer@example.com", "Demo Viewer", Role.VIEWER, "seed_viewer_password"),
]


async def seed_currencies(uow: UnitOfWork) -> None:
    for code, name, places in CURRENCIES:
        if await uow.currencies.get_by_code(code) is None:
            uow.currencies.add(
                Currency(
                    currency_code=code,
                    currency_name=name,
                    decimal_places=places,
                    is_active=True,
                )
            )
    await uow.flush()


async def seed_company(uow: UnitOfWork, name: str, currency_code: str) -> Company:
    company = await uow.companies.get_by_name(name)
    if company is None:
        company = Company(
            name=name,
            legal_name=f"{name}",
            base_currency_code=currency_code,
            timezone="UTC",
            is_active=True,
        )
        uow.companies.add(company)
        await uow.flush()
    return company


async def seed_accounts(uow: UnitOfWork, company_id: UUID) -> dict[str, Account]:
    accounts: dict[str, Account] = {}
    for number, name, account_type, normal_balance, postable in CHART_OF_ACCOUNTS:
        account = await uow.accounts.get_by_number(company_id, number)
        if account is None:
            account = Account(
                company_id=company_id,
                account_number=number,
                account_name=name,
                account_type=account_type,
                normal_balance=normal_balance,
                is_active=True,
                is_postable=postable,
            )
            uow.accounts.add(account)
        accounts[number] = account
    await uow.flush()

    for child_number, parent_number in ACCOUNT_PARENTS.items():
        child = accounts[child_number]
        if child.parent_account_id is None:
            child.parent_account_id = accounts[parent_number].id
    await uow.flush()
    return accounts


async def seed_periods(uow: UnitOfWork, company_id: UUID, year: int) -> list[FiscalPeriod]:
    existing = {
        period.period_name
        for period in await uow.fiscal_periods.list_in_range(
            company_id, date(year, 1, 1), date(year, 12, 31)
        )
    }
    created: list[FiscalPeriod] = []
    for month in range(1, 13):
        name = f"{calendar.month_name[month]} {year}"
        if name in existing:
            continue
        last_day = calendar.monthrange(year, month)[1]
        period = FiscalPeriod(
            company_id=company_id,
            fiscal_year=year,
            period_name=name,
            start_date=date(year, month, 1),
            end_date=date(year, month, last_day),
            status=FiscalPeriodStatus.OPEN,
            is_closed=False,
        )
        uow.fiscal_periods.add(period)
        created.append(period)
    await uow.flush()
    return created


async def seed_tax_codes(
    uow: UnitOfWork, company_id: UUID, accounts: dict[str, Account], year: int
) -> None:
    if await uow.tax_codes.get_by_code(company_id, "VAT15") is not None:
        return
    uow.tax_codes.add(
        TaxCode(
            company_id=company_id,
            tax_code="VAT15",
            tax_name="Value Added Tax 15%",
            tax_rate=Decimal("15.0000"),
            tax_account_id=accounts["205"].id,
            effective_from=date(year, 1, 1),
            effective_to=None,
            is_active=True,
        )
    )
    await uow.flush()


async def seed_users(uow: UnitOfWork) -> list[tuple[str, str | None]]:
    """Create the demo users. Returns `(email, generated_password_or_None)`."""
    roles = await ensure_roles_and_permissions(uow)
    created: list[tuple[str, str | None]] = []

    for email, full_name, role, settings_attr in SEED_USERS:
        if await uow.users.get_by_email(email) is not None:
            continue

        configured: str | None = getattr(settings, settings_attr, None)
        password = configured or secrets.token_urlsafe(18)

        user = User(
            email=email,
            hashed_password=hash_password(password),
            full_name=full_name,
            is_active=True,
            is_superuser=role is Role.ADMIN,
        )
        user.roles.append(roles[role])
        uow.users.add(user)
        created.append((email, None if configured else password))

    await uow.flush()
    return created


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed demo accounting data.")
    parser.add_argument("--company", default="Demo Company Ltd")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--year", type=int, default=2026)
    args = parser.parse_args(argv)

    configure_logging()

    if settings.is_production:
        print("Refusing to seed a production environment.", file=sys.stderr)
        return 1

    try:
        async with session_scope() as session:
            uow = UnitOfWork(session)

            await seed_currencies(uow)
            company = await seed_company(uow, args.company, args.currency)
            accounts = await seed_accounts(uow, company.id)
            periods = await seed_periods(uow, company.id, args.year)
            await seed_tax_codes(uow, company.id, accounts, args.year)
            users = await seed_users(uow)

            await uow.commit()

            print(f"Company:   {company.name} ({company.id})")
            print(f"Currency:  {company.base_currency_code}")
            print(f"Accounts:  {len(accounts)}")
            print(f"Periods:   {len(periods)} created for {args.year}")
            print("Tax codes: VAT15 -> 205 Sales Tax Payable")
            if users:
                print("\nUsers created:")
                for email, generated in users:
                    if generated:
                        print(f"  {email}  password: {generated}")
                    else:
                        print(f"  {email}  password: (from environment)")
                print("\nGenerated passwords are shown once. Store them now.")
            else:
                print("Users: already present, unchanged.")
    finally:
        await dispose_engine()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
