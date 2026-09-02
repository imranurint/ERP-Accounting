"""Unit tests for the accounting golden rules.

Pure functions, no database. Every amount is a `Decimal`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.ids import uuid7
from app.domain.accounting import rules, services
from app.domain.accounting.entities import (
    Account,
    Currency,
    FiscalPeriod,
    JournalEntry,
    JournalLine,
    TaxCode,
)
from app.domain.accounting.enums import (
    AccountType,
    FiscalPeriodStatus,
    JournalStatus,
    NormalBalance,
)
from app.domain.accounting.exceptions import (
    AccountInactiveError,
    AccountNotPostableError,
    CrossCompanyReferenceError,
    CurrencyMismatchError,
    FiscalPeriodClosedError,
    FiscalPeriodNotFoundForDateError,
    InvalidJournalLineError,
    InvalidJournalStateError,
    InvalidPeriodTransitionError,
    JournalAlreadyReversedError,
    MoneyPrecisionError,
    PostedEntryCannotBeModifiedError,
    TaxCodeInactiveError,
    UnbalancedJournalEntryError,
)
from app.domain.accounting.value_objects import DebitCredit

pytestmark = pytest.mark.unit

COMPANY_ID = uuid7()
USD = Currency("USD", "US Dollar", 2, True)
JPY = Currency("JPY", "Japanese Yen", 0, True)


def make_account(
    *,
    number: str = "101",
    account_type: AccountType = AccountType.ASSET,
    normal_balance: NormalBalance | None = None,
    active: bool = True,
    postable: bool = True,
    currency: str | None = None,
    company_id: object = COMPANY_ID,
) -> Account:
    return Account(
        id=uuid7(),
        company_id=company_id,  # type: ignore[arg-type]
        account_number=number,
        account_name=f"Account {number}",
        account_type=account_type,
        normal_balance=normal_balance or account_type.normal_balance,
        currency_code=currency,
        parent_account_id=None,
        is_active=active,
        is_postable=postable,
    )


def make_line(
    line_number: int = 1,
    debit: str = "0",
    credit: str = "0",
    *,
    account_id: object = None,
    currency: str = "USD",
) -> JournalLine:
    return JournalLine(
        line_number=line_number,
        account_id=account_id or uuid7(),  # type: ignore[arg-type]
        amounts=DebitCredit(Decimal(debit), Decimal(credit)),
        currency_code=currency,
    )


def make_entry(lines: list[JournalLine]) -> JournalEntry:
    return JournalEntry(
        company_id=COMPANY_ID,
        entry_date=date(2026, 3, 31),
        period_id=uuid7(),
        currency_code="USD",
        lines=lines,
    )


# ---------------------------------------------------------------------------
# RULE 2 — debit/credit exclusivity
# ---------------------------------------------------------------------------
class TestLineAmounts:
    def test_debit_only_line_is_valid(self) -> None:
        rules.validate_line_amounts(make_line(1, debit="115.00"))

    def test_credit_only_line_is_valid(self) -> None:
        rules.validate_line_amounts(make_line(1, credit="115.00"))

    def test_both_sides_populated_is_rejected(self) -> None:
        with pytest.raises(InvalidJournalLineError) as exc:
            rules.validate_line_amounts(make_line(1, debit="100.00", credit="100.00"))
        assert exc.value.code == "INVALID_JOURNAL_LINE"
        assert exc.value.details["line_number"] == 1

    def test_both_sides_zero_is_rejected(self) -> None:
        with pytest.raises(InvalidJournalLineError):
            rules.validate_line_amounts(make_line(1))

    def test_negative_amount_is_rejected(self) -> None:
        with pytest.raises(InvalidJournalLineError):
            rules.validate_line_amounts(make_line(1, debit="-10.00"))

    def test_amount_beyond_storage_range_is_rejected(self) -> None:
        with pytest.raises(InvalidJournalLineError):
            rules.validate_line_amounts(make_line(1, debit="9999999999999999.0000"))


# ---------------------------------------------------------------------------
# Money precision
# ---------------------------------------------------------------------------
class TestMoneyPrecision:
    def test_two_decimals_allowed_for_usd(self) -> None:
        rules.validate_money_precision(make_line(1, debit="115.00"), USD)

    def test_three_decimals_rejected_for_usd(self) -> None:
        with pytest.raises(MoneyPrecisionError) as exc:
            rules.validate_money_precision(make_line(1, debit="115.005"), USD)
        assert exc.value.details["decimal_places"] == 2

    def test_zero_decimal_currency_rejects_fractions(self) -> None:
        with pytest.raises(MoneyPrecisionError):
            rules.validate_money_precision(make_line(1, debit="115.50"), JPY)

    def test_zero_decimal_currency_accepts_integers(self) -> None:
        rules.validate_money_precision(make_line(1, debit="11500"), JPY)


# ---------------------------------------------------------------------------
# Line numbering
# ---------------------------------------------------------------------------
class TestLineNumbers:
    def test_sequential_numbers_accepted(self) -> None:
        rules.validate_line_numbers([make_line(1, debit="1"), make_line(2, credit="1")])

    def test_gap_is_rejected(self) -> None:
        with pytest.raises(InvalidJournalLineError) as exc:
            rules.validate_line_numbers([make_line(1, debit="1"), make_line(3, credit="1")])
        assert exc.value.details["expected"] == [1, 2]

    def test_duplicate_is_rejected(self) -> None:
        with pytest.raises(InvalidJournalLineError):
            rules.validate_line_numbers([make_line(1, debit="1"), make_line(1, credit="1")])

    def test_single_line_is_rejected(self) -> None:
        with pytest.raises(InvalidJournalLineError):
            rules.validate_line_numbers([make_line(1, debit="1")])

    def test_empty_is_rejected(self) -> None:
        with pytest.raises(InvalidJournalLineError):
            rules.validate_line_numbers([])


# ---------------------------------------------------------------------------
# RULE 1 — balance
# ---------------------------------------------------------------------------
class TestBalance:
    def test_balanced_invoice_passes(self) -> None:
        entry = make_entry(
            [
                make_line(1, debit="115.00"),
                make_line(2, credit="100.00"),
                make_line(3, credit="15.00"),
            ]
        )
        assert entry.total_debits == Decimal("115.0000")
        assert entry.total_credits == Decimal("115.0000")
        rules.validate_balanced(entry)

    def test_unbalanced_entry_reports_the_difference(self) -> None:
        entry = make_entry([make_line(1, debit="115.00"), make_line(2, credit="100.00")])
        with pytest.raises(UnbalancedJournalEntryError) as exc:
            rules.validate_balanced(entry)
        assert exc.value.code == "UNBALANCED_JOURNAL_ENTRY"
        assert exc.value.details["difference"] == "15.0000"

    def test_zero_value_entry_is_rejected(self) -> None:
        entry = make_entry([make_line(1, debit="0"), make_line(2, credit="0")])
        with pytest.raises(UnbalancedJournalEntryError):
            rules.validate_balanced(entry)

    def test_rounding_artefacts_do_not_sneak_through(self) -> None:
        # 0.1 + 0.2 == 0.3 exactly in Decimal; it would not in float.
        entry = make_entry(
            [
                make_line(1, debit="0.10"),
                make_line(2, debit="0.20"),
                make_line(3, credit="0.30"),
            ]
        )
        rules.validate_balanced(entry)


# ---------------------------------------------------------------------------
# RULE 3 — fiscal periods
# ---------------------------------------------------------------------------
def make_period(
    status: FiscalPeriodStatus = FiscalPeriodStatus.OPEN,
) -> FiscalPeriod:
    return FiscalPeriod(
        id=uuid7(),
        company_id=COMPANY_ID,
        fiscal_year=2026,
        period_name="March 2026",
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
        status=status,
        is_closed=status in (FiscalPeriodStatus.CLOSED, FiscalPeriodStatus.LOCKED),
    )


class TestFiscalPeriodRules:
    @pytest.mark.parametrize("day", [1, 15, 31])
    def test_dates_inside_the_period_are_accepted(self, day: int) -> None:
        rules.validate_period_covers_date(make_period(), date(2026, 3, day))

    @pytest.mark.parametrize("value", [date(2026, 2, 28), date(2026, 4, 1), date(2025, 3, 15)])
    def test_dates_outside_the_period_are_rejected(self, value: date) -> None:
        with pytest.raises(FiscalPeriodNotFoundForDateError):
            rules.validate_period_covers_date(make_period(), value)

    @pytest.mark.parametrize("status", [FiscalPeriodStatus.OPEN, FiscalPeriodStatus.ADJUSTING])
    def test_open_and_adjusting_accept_postings(self, status: FiscalPeriodStatus) -> None:
        rules.validate_period_accepts_posting(make_period(status))

    @pytest.mark.parametrize("status", [FiscalPeriodStatus.CLOSED, FiscalPeriodStatus.LOCKED])
    def test_closed_and_locked_reject_postings(self, status: FiscalPeriodStatus) -> None:
        with pytest.raises(FiscalPeriodClosedError) as exc:
            rules.validate_period_accepts_posting(make_period(status))
        assert exc.value.code == "FISCAL_PERIOD_CLOSED"

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (FiscalPeriodStatus.OPEN, FiscalPeriodStatus.CLOSED),
            (FiscalPeriodStatus.OPEN, FiscalPeriodStatus.ADJUSTING),
            (FiscalPeriodStatus.CLOSED, FiscalPeriodStatus.OPEN),
            (FiscalPeriodStatus.CLOSED, FiscalPeriodStatus.LOCKED),
            (FiscalPeriodStatus.ADJUSTING, FiscalPeriodStatus.CLOSED),
        ],
    )
    def test_allowed_transitions(
        self, current: FiscalPeriodStatus, target: FiscalPeriodStatus
    ) -> None:
        rules.validate_period_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (FiscalPeriodStatus.LOCKED, FiscalPeriodStatus.OPEN),
            (FiscalPeriodStatus.LOCKED, FiscalPeriodStatus.CLOSED),
            (FiscalPeriodStatus.OPEN, FiscalPeriodStatus.LOCKED),
        ],
    )
    def test_forbidden_transitions(
        self, current: FiscalPeriodStatus, target: FiscalPeriodStatus
    ) -> None:
        with pytest.raises(InvalidPeriodTransitionError):
            rules.validate_period_transition(current, target)


# ---------------------------------------------------------------------------
# Account and tax validation
# ---------------------------------------------------------------------------
class TestAccountRules:
    def test_usable_account_passes(self) -> None:
        rules.validate_account_usable(make_account(), COMPANY_ID, 1)

    def test_inactive_account_is_rejected(self) -> None:
        with pytest.raises(AccountInactiveError) as exc:
            rules.validate_account_usable(make_account(active=False), COMPANY_ID, 1)
        assert exc.value.code == "ACCOUNT_INACTIVE"

    def test_non_postable_account_is_rejected(self) -> None:
        with pytest.raises(AccountNotPostableError) as exc:
            rules.validate_account_usable(make_account(postable=False), COMPANY_ID, 2)
        assert exc.value.code == "ACCOUNT_NOT_POSTABLE"
        assert exc.value.details["line_number"] == 2

    def test_account_from_another_company_is_rejected(self) -> None:
        with pytest.raises(CrossCompanyReferenceError):
            rules.validate_account_usable(make_account(company_id=uuid4()), COMPANY_ID, 1)

    def test_currency_pinned_account_rejects_other_currencies(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            rules.validate_account_currency(make_account(currency="EUR"), "USD", 1)

    def test_unpinned_account_accepts_any_currency(self) -> None:
        rules.validate_account_currency(make_account(), "EUR", 1)


class TestTaxCodeRules:
    def _tax(self, *, active: bool = True, ends: date | None = None) -> TaxCode:
        return TaxCode(
            id=uuid7(),
            company_id=COMPANY_ID,
            tax_code="VAT15",
            tax_name="VAT 15%",
            tax_rate=Decimal("15.0000"),
            tax_account_id=uuid7(),
            effective_from=date(2026, 1, 1),
            effective_to=ends,
            is_active=active,
        )

    def test_effective_tax_code_passes(self) -> None:
        rules.validate_tax_code_usable(self._tax(), COMPANY_ID, date(2026, 3, 31), 3)

    def test_inactive_tax_code_is_rejected(self) -> None:
        with pytest.raises(TaxCodeInactiveError):
            rules.validate_tax_code_usable(
                self._tax(active=False), COMPANY_ID, date(2026, 3, 31), 3
            )

    def test_expired_tax_code_is_rejected(self) -> None:
        with pytest.raises(TaxCodeInactiveError):
            rules.validate_tax_code_usable(
                self._tax(ends=date(2026, 2, 28)), COMPANY_ID, date(2026, 3, 31), 3
            )

    def test_tax_code_before_effective_date_is_rejected(self) -> None:
        with pytest.raises(TaxCodeInactiveError):
            rules.validate_tax_code_usable(self._tax(), COMPANY_ID, date(2025, 12, 31), 3)


# ---------------------------------------------------------------------------
# RULE 5 / journal state machine
# ---------------------------------------------------------------------------
class TestJournalStateMachine:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (JournalStatus.DRAFT, JournalStatus.POSTED),
            (JournalStatus.DRAFT, JournalStatus.VOID),
            (JournalStatus.POSTED, JournalStatus.REVERSED),
        ],
    )
    def test_allowed_transitions(self, current: JournalStatus, target: JournalStatus) -> None:
        rules.validate_journal_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (JournalStatus.POSTED, JournalStatus.VOID),
            (JournalStatus.POSTED, JournalStatus.DRAFT),
            (JournalStatus.REVERSED, JournalStatus.POSTED),
            (JournalStatus.VOID, JournalStatus.POSTED),
            (JournalStatus.REVERSED, JournalStatus.REVERSED),
        ],
    )
    def test_forbidden_transitions(self, current: JournalStatus, target: JournalStatus) -> None:
        with pytest.raises(InvalidJournalStateError):
            rules.validate_journal_transition(current, target)

    def test_posted_entries_are_not_mutable(self) -> None:
        with pytest.raises(PostedEntryCannotBeModifiedError) as exc:
            rules.validate_mutable(JournalStatus.POSTED)
        assert exc.value.code == "POSTED_ENTRY_CANNOT_BE_MODIFIED"

    def test_draft_entries_are_mutable(self) -> None:
        rules.validate_mutable(JournalStatus.DRAFT)

    def test_void_entries_are_not_mutable(self) -> None:
        with pytest.raises(InvalidJournalStateError):
            rules.validate_mutable(JournalStatus.VOID)


# ---------------------------------------------------------------------------
# Normal balance arithmetic
# ---------------------------------------------------------------------------
class TestNormalBalance:
    @pytest.mark.parametrize(
        ("account_type", "expected"),
        [
            (AccountType.ASSET, NormalBalance.DEBIT),
            (AccountType.EXPENSE, NormalBalance.DEBIT),
            (AccountType.LIABILITY, NormalBalance.CREDIT),
            (AccountType.EQUITY, NormalBalance.CREDIT),
            (AccountType.REVENUE, NormalBalance.CREDIT),
        ],
    )
    def test_default_normal_balance_per_type(
        self, account_type: AccountType, expected: NormalBalance
    ) -> None:
        assert rules.normal_balance_for(account_type) is expected

    def test_debit_normal_closing_balance(self) -> None:
        # opening 100 + debits 50 - credits 20 = 130
        assert rules.closing_balance(
            Decimal("100.00"), Decimal("50.00"), Decimal("20.00"), NormalBalance.DEBIT
        ) == Decimal("130.00")

    def test_credit_normal_closing_balance(self) -> None:
        # opening 100 + credits 20 - debits 50 = 70
        assert rules.closing_balance(
            Decimal("100.00"), Decimal("50.00"), Decimal("20.00"), NormalBalance.CREDIT
        ) == Decimal("70.00")


# ---------------------------------------------------------------------------
# Reversal construction
# ---------------------------------------------------------------------------
class TestReversal:
    def _posted_invoice(self) -> JournalEntry:
        entry = make_entry(
            [
                make_line(1, debit="115.00"),
                make_line(2, credit="100.00"),
                make_line(3, credit="15.00"),
            ]
        )
        entry.status = JournalStatus.POSTED
        entry.id = uuid7()
        entry.entry_number = "JE-2026-000001"
        return entry

    def test_every_line_is_mirrored(self) -> None:
        original = self._posted_invoice()
        reversal = services.build_reversal_entry(
            original, reversal_date=date(2026, 3, 31), period_id=original.period_id
        )

        assert [line.debit for line in reversal.lines] == [
            Decimal("0.0000"),
            Decimal("100.0000"),
            Decimal("15.0000"),
        ]
        assert [line.credit for line in reversal.lines] == [
            Decimal("115.0000"),
            Decimal("0.0000"),
            Decimal("0.0000"),
        ]

    def test_reversal_balances_and_nets_to_zero(self) -> None:
        original = self._posted_invoice()
        reversal = services.build_reversal_entry(
            original, reversal_date=date(2026, 3, 31), period_id=original.period_id
        )
        rules.validate_balanced(reversal)
        assert reversal.total_debits == original.total_credits
        assert reversal.total_credits == original.total_debits

    def test_reversal_starts_as_draft_and_keeps_provenance(self) -> None:
        original = self._posted_invoice()
        original.source_document = "INV-9923"
        reversal = services.build_reversal_entry(
            original, reversal_date=date(2026, 4, 1), period_id=uuid7()
        )
        assert reversal.status is JournalStatus.DRAFT
        assert reversal.source_document == "INV-9923"
        assert "JE-2026-000001" in (reversal.reference or "")

    def test_double_reversal_is_rejected(self) -> None:
        original = self._posted_invoice()
        original.reversed_by_entry_id = uuid7()
        with pytest.raises(JournalAlreadyReversedError):
            services.build_reversal_entry(
                original, reversal_date=date(2026, 3, 31), period_id=original.period_id
            )

    def test_draft_cannot_be_reversed(self) -> None:
        entry = make_entry([make_line(1, debit="1"), make_line(2, credit="1")])
        entry.id = uuid7()
        with pytest.raises(InvalidJournalStateError):
            services.build_reversal_entry(
                entry, reversal_date=date(2026, 3, 31), period_id=entry.period_id
            )


# ---------------------------------------------------------------------------
# Ledger movement aggregation
# ---------------------------------------------------------------------------
class TestLedgerMovements:
    def test_lines_on_the_same_account_collapse_into_one_movement(self) -> None:
        bank = make_account(number="101")
        revenue = make_account(number="402", account_type=AccountType.REVENUE)

        entry = make_entry(
            [
                make_line(1, debit="100.00", account_id=bank.id),
                make_line(2, debit="15.00", account_id=bank.id),
                make_line(3, credit="115.00", account_id=revenue.id),
            ]
        )
        movements = services.aggregate_ledger_movements(entry, {bank.id: bank, revenue.id: revenue})

        assert len(movements) == 2
        bank_movement = next(m for m in movements if m.account_id == bank.id)
        assert bank_movement.total_debits == Decimal("115.0000")
        assert bank_movement.net == Decimal("115.0000")

        revenue_movement = next(m for m in movements if m.account_id == revenue.id)
        # Credit-normal: a credit increases the balance.
        assert revenue_movement.net == Decimal("115.0000")

    def test_movement_order_is_deterministic(self) -> None:
        accounts = {}
        lines = []
        for index in range(5):
            account = make_account(number=f"10{index}")
            accounts[account.id] = account
            lines.append(make_line(index + 1, debit="10.00", account_id=account.id))
        offset = make_account(number="402", account_type=AccountType.REVENUE)
        accounts[offset.id] = offset
        lines.append(make_line(6, credit="50.00", account_id=offset.id))

        entry = make_entry(lines)
        first = services.aggregate_ledger_movements(entry, accounts)
        second = services.aggregate_ledger_movements(entry, accounts)
        assert [m.account_id for m in first] == [m.account_id for m in second]
