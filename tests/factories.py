"""Test data builders.

`factory_boy` shapes the plain attribute dictionaries; persistence goes
through the real session so every constraint in the schema applies. Monetary
amounts are always `Decimal`, never float — the same rule the production code
follows.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import factory
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.modules.accounts.infrastructure.models import Account
from app.modules.fiscal_periods.infrastructure.models import FiscalPeriod
from app.modules.identity.domain.permissions import (
    DEFAULT_ROLE_PERMISSIONS,
    PERMISSION_DESCRIPTIONS,
    Permission,
    Role,
)
from app.modules.identity.infrastructure.models import (
    Permission as PermissionModel,
)
from app.modules.identity.infrastructure.models import (
    Role as RoleModel,
)
from app.modules.identity.infrastructure.models import (
    User,
)
from app.modules.organization.infrastructure.models import Company, Currency
from app.modules.taxes.infrastructure.models import TaxCode
from app.shared.domain.enums import AccountType, FiscalPeriodStatus, NormalBalance

TEST_PASSWORD = "correct-horse-battery-staple-42"

#: The seeded chart used by the tests, matching `scripts.seed`.
ACCOUNTS: list[tuple[str, str, AccountType, NormalBalance, bool]] = [
    ("100", "Current Assets", AccountType.ASSET, NormalBalance.DEBIT, False),
    ("101", "Bank Account", AccountType.ASSET, NormalBalance.DEBIT, True),
    ("120", "Accounts Receivable", AccountType.ASSET, NormalBalance.DEBIT, True),
    ("150", "Inventory", AccountType.ASSET, NormalBalance.DEBIT, True),
    ("205", "Sales Tax Payable", AccountType.LIABILITY, NormalBalance.CREDIT, True),
    ("300", "Owner Equity", AccountType.EQUITY, NormalBalance.CREDIT, True),
    ("402", "SaaS Revenue", AccountType.REVENUE, NormalBalance.CREDIT, True),
    ("500", "Cost of Goods Sold", AccountType.EXPENSE, NormalBalance.DEBIT, True),
    ("600", "Operating Expenses", AccountType.EXPENSE, NormalBalance.DEBIT, True),
]


class CurrencyAttrs(factory.DictFactory):
    currency_code = "USD"
    currency_name = "US Dollar"
    decimal_places = 2
    is_active = True


class CompanyAttrs(factory.DictFactory):
    name = factory.Sequence(lambda n: f"Demo Company {n} Ltd")
    legal_name = "Demo Company Ltd"
    base_currency_code = "USD"
    timezone = "UTC"
    is_active = True


class AccountAttrs(factory.DictFactory):
    account_number = factory.Sequence(lambda n: f"9{n:03d}")
    account_name = factory.Sequence(lambda n: f"Test Account {n}")
    account_type = AccountType.ASSET
    normal_balance = NormalBalance.DEBIT
    is_active = True
    is_postable = True


@dataclass
class DemoData:
    """Everything a test needs to post a realistic journal entry."""

    company: Company
    currencies: dict[str, Currency]
    accounts: dict[str, Account]
    periods: dict[str, FiscalPeriod]
    tax_codes: dict[str, TaxCode]
    users: dict[Role, User] = field(default_factory=dict)
    roles: dict[Role, RoleModel] = field(default_factory=dict)

    @property
    def company_id(self) -> UUID:
        return self.company.id

    def account(self, number: str) -> Account:
        return self.accounts[number]

    def period(self, name: str) -> FiscalPeriod:
        return self.periods[name]

    def user(self, role: Role) -> User:
        return self.users[role]

    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        *,
        year: int = 2026,
        company_name: str = "Demo Company Ltd",
    ) -> DemoData:
        currencies: dict[str, Currency] = {}
        for code, name, places, active in (
            ("USD", "US Dollar", 2, True),
            ("EUR", "Euro", 2, True),
            ("JPY", "Japanese Yen", 0, True),
            ("XXX", "Retired Currency", 2, False),
        ):
            existing = await session.get(Currency, code)
            if existing is None:
                existing = Currency(
                    currency_code=code,
                    currency_name=name,
                    decimal_places=places,
                    is_active=active,
                )
                session.add(existing)
            currencies[code] = existing
        await session.flush()

        company = Company(
            name=company_name,
            legal_name=company_name,
            base_currency_code="USD",
            timezone="UTC",
            is_active=True,
        )
        session.add(company)
        await session.flush()

        accounts: dict[str, Account] = {}
        for number, name, account_type, normal_balance, postable in ACCOUNTS:
            account = Account(
                company_id=company.id,
                account_number=number,
                account_name=name,
                account_type=account_type,
                normal_balance=normal_balance,
                is_active=True,
                is_postable=postable,
            )
            session.add(account)
            accounts[number] = account
        await session.flush()

        periods: dict[str, FiscalPeriod] = {}
        for month in range(1, 13):
            last_day = calendar.monthrange(year, month)[1]
            period = FiscalPeriod(
                company_id=company.id,
                fiscal_year=year,
                period_name=f"{calendar.month_name[month]} {year}",
                start_date=date(year, month, 1),
                end_date=date(year, month, last_day),
                status=FiscalPeriodStatus.OPEN,
                is_closed=False,
            )
            session.add(period)
            periods[calendar.month_name[month]] = period
        await session.flush()

        tax = TaxCode(
            company_id=company.id,
            tax_code="VAT15",
            tax_name="Value Added Tax 15%",
            tax_rate=Decimal("15.0000"),
            tax_account_id=accounts["205"].id,
            effective_from=date(year, 1, 1),
            is_active=True,
        )
        session.add(tax)
        await session.flush()

        roles, users = await _create_users(session)

        await session.commit()
        # Detach: tests share this session with the services under test, and a
        # rollback there would otherwise expire these fixture objects.
        session.expunge_all()

        return cls(
            company=company,
            currencies=currencies,
            accounts=accounts,
            periods=periods,
            tax_codes={"VAT15": tax},
            users=users,
            roles=roles,
        )


async def _create_users(
    session: AsyncSession,
) -> tuple[dict[Role, RoleModel], dict[Role, User]]:
    existing_permissions = {
        row.code: row for row in (await session.execute(select(PermissionModel))).scalars().all()
    }
    permission_models: dict[Permission, PermissionModel] = {}
    for permission in Permission:
        model = existing_permissions.get(str(permission))
        if model is None:
            model = PermissionModel(
                code=str(permission), description=PERMISSION_DESCRIPTIONS[permission]
            )
            session.add(model)
        permission_models[permission] = model
    await session.flush()

    existing_roles = {
        row.name: row for row in (await session.execute(select(RoleModel))).scalars().all()
    }
    roles: dict[Role, RoleModel] = {}
    users: dict[Role, User] = {}
    for role, permissions in DEFAULT_ROLE_PERMISSIONS.items():
        model = existing_roles.get(str(role))
        if model is None:
            model = RoleModel(name=str(role), description=str(role), is_system=True)
            model.permissions = [permission_models[p] for p in sorted(permissions)]
            session.add(model)
        roles[role] = model
    await session.flush()

    existing_users = {
        row.email: row for row in (await session.execute(select(User))).scalars().all()
    }
    for role in Role:
        email = f"{str(role).lower()}@example.com"
        if email in existing_users:
            users[role] = existing_users[email]
            continue
        user = User(
            email=email,
            hashed_password=hash_password(TEST_PASSWORD),
            full_name=f"Demo {role}",
            is_active=True,
            # Deliberately not a superuser: the tests must exercise real
            # permission checks rather than the bypass.
            is_superuser=False,
        )
        user.roles = [roles[role]]
        session.add(user)
        users[role] = user
    await session.flush()

    return roles, users


def invoice_payload(
    company_id: UUID,
    period_id: UUID,
    *,
    entry_date: str = "2026-03-31",
    idempotency_key: str | None = "sales_invoice:INV-9923",
    post_immediately: bool = True,
) -> dict[str, Any]:
    """The canonical customer-invoice example: 115.00 = 100.00 + 15.00 VAT."""
    payload: dict[str, Any] = {
        "company_id": str(company_id),
        "entry_date": entry_date,
        "period_id": str(period_id),
        "currency_code": "USD",
        "source_type": "sales_invoice",
        "source_document": "INV-9923",
        "reference": "Customer invoice INV-9923",
        "post_immediately": post_immediately,
        "lines": [
            {"line_number": 1, "account_number": "101", "debit": "115.00", "credit": "0.00"},
            {"line_number": 2, "account_number": "402", "debit": "0.00", "credit": "100.00"},
            {
                "line_number": 3,
                "account_number": "205",
                "debit": "0.00",
                "credit": "15.00",
                "tax_code": "VAT15",
            },
        ],
    }
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    return payload


# ---------------------------------------------------------------------------
# Mutation helpers.
#
# Fixture objects are detached, so tests change stored state with explicit
# UPDATE statements rather than by mutating a stale instance.
# ---------------------------------------------------------------------------
async def set_account_active(session: AsyncSession, account_id: UUID, *, active: bool) -> None:
    await session.execute(update(Account).where(Account.id == account_id).values(is_active=active))
    await session.commit()


async def set_period_status(
    session: AsyncSession, period_id: UUID, status: FiscalPeriodStatus
) -> None:
    await session.execute(
        update(FiscalPeriod)
        .where(FiscalPeriod.id == period_id)
        .values(
            status=status,
            is_closed=status in (FiscalPeriodStatus.CLOSED, FiscalPeriodStatus.LOCKED),
        )
    )
    await session.commit()
