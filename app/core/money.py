"""Monetary primitives.

Rules enforced here, once, for the whole system:

* money is `Decimal`, never `float`;
* storage scale is 4 (`NUMERIC(19,4)`);
* presentation/validation scale is the currency's `decimal_places`;
* rounding mode is configurable, default `ROUND_HALF_UP`.
"""

from __future__ import annotations

from decimal import Decimal, DecimalException, InvalidOperation

from app.core.config import settings

#: Storage scale of every monetary column — NUMERIC(19, 4).
STORAGE_SCALE = 4
STORAGE_PRECISION = 19
STORAGE_QUANT = Decimal("0.0001")

ZERO = Decimal("0.0000")

#: Largest absolute value NUMERIC(19,4) can hold.
MAX_AMOUNT = Decimal("999999999999999.9999")


class MoneyError(ValueError):
    """Raised when a value cannot be interpreted as a monetary amount."""


def to_decimal(value: str | int | Decimal) -> Decimal:
    """Parse an API/monetary input into `Decimal` without ever touching float."""
    if isinstance(value, float):  # pragma: no cover - defensive, typing forbids it
        raise MoneyError("float is not an acceptable monetary type")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, DecimalException, ArithmeticError) as exc:
        raise MoneyError(f"invalid monetary amount: {value!r}") from exc
    if not parsed.is_finite():
        raise MoneyError(f"invalid monetary amount: {value!r}")
    return parsed


def quantize_storage(value: Decimal) -> Decimal:
    """Round to the storage scale (4 dp) using the configured rounding mode."""
    return value.quantize(STORAGE_QUANT, rounding=settings.rounding_mode)


def quantize_currency(value: Decimal, decimal_places: int) -> Decimal:
    """Round to a currency's own scale, then widen back to storage scale."""
    if decimal_places < 0 or decimal_places > STORAGE_SCALE:
        raise MoneyError(f"unsupported currency scale: {decimal_places}")
    quant = Decimal(1).scaleb(-decimal_places)
    return quantize_storage(value.quantize(quant, rounding=settings.rounding_mode))


def exceeds_currency_precision(value: Decimal, decimal_places: int) -> bool:
    """True when `value` carries more decimals than the currency permits.

    We reject rather than silently round: a caller sending "115.005" to a
    2-decimal currency has a bug we must not paper over.
    """
    exponent = value.normalize().as_tuple().exponent
    if not isinstance(exponent, int):  # NaN/Infinity guard
        return True
    return -exponent > decimal_places


def within_storage_range(value: Decimal) -> bool:
    return -MAX_AMOUNT <= value <= MAX_AMOUNT


def format_money(value: Decimal, decimal_places: int = 2) -> str:
    """Serialise money for transport: always a string, always fixed scale."""
    quant = Decimal(1).scaleb(-decimal_places)
    return str(value.quantize(quant, rounding=settings.rounding_mode))
