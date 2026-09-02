"""Immutable accounting value objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.money import ZERO, quantize_storage
from app.domain.accounting.enums import NormalBalance


@dataclass(frozen=True, slots=True)
class Amount:
    """A monetary amount bound to a currency.

    Arithmetic is only defined between amounts of the same currency; mixing
    currencies is a bug, not something to silently coerce.
    """

    value: Decimal
    currency_code: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", quantize_storage(self.value))
        object.__setattr__(self, "currency_code", self.currency_code.upper())

    @classmethod
    def zero(cls, currency_code: str) -> Amount:
        return cls(ZERO, currency_code)

    def _assert_same_currency(self, other: Amount) -> None:
        if self.currency_code != other.currency_code:
            raise ValueError(f"cannot combine {self.currency_code} with {other.currency_code}")

    def __add__(self, other: Amount) -> Amount:
        self._assert_same_currency(other)
        return Amount(self.value + other.value, self.currency_code)

    def __sub__(self, other: Amount) -> Amount:
        self._assert_same_currency(other)
        return Amount(self.value - other.value, self.currency_code)

    def __neg__(self) -> Amount:
        return Amount(-self.value, self.currency_code)

    @property
    def is_zero(self) -> bool:
        return self.value == ZERO

    @property
    def is_positive(self) -> bool:
        return self.value > ZERO

    def __str__(self) -> str:
        return f"{self.value} {self.currency_code}"


@dataclass(frozen=True, slots=True)
class DebitCredit:
    """The two sides of a single journal line.

    Invariant (enforced by `app.domain.accounting.rules`): exactly one side is
    positive and the other is exactly zero.
    """

    debit: Decimal
    credit: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "debit", quantize_storage(self.debit))
        object.__setattr__(self, "credit", quantize_storage(self.credit))

    @property
    def total(self) -> Decimal:
        return self.debit + self.credit

    @property
    def is_debit(self) -> bool:
        return self.debit > ZERO

    def signed(self, normal_balance: NormalBalance) -> Decimal:
        """The line's effect on an account with the given normal balance."""
        movement = self.debit - self.credit
        return movement if normal_balance is NormalBalance.DEBIT else -movement

    def reversed(self) -> DebitCredit:
        """Swap the sides — the whole of reversal accounting in one line."""
        return DebitCredit(debit=self.credit, credit=self.debit)


@dataclass(frozen=True, slots=True)
class DateRange:
    """A closed date interval `[start, end]`, used for fiscal periods."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("start_date must be on or before end_date")

    def overlaps(self, other: DateRange) -> bool:
        return self.start <= other.end and other.start <= self.end

    def contains(self, value: date) -> bool:
        return self.start <= value <= self.end
