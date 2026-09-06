"""Shared Pydantic building blocks for the application layer.

The important one is `MoneyStr`: monetary values cross the API boundary as
*strings*, in both directions. JSON numbers are IEEE-754 doubles in most
clients, so accepting `115.00` as a number would let a rounding error into the
ledger before validation ever ran.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

from app.core.money import to_decimal


def _parse_money(value: Any) -> Decimal:
    if isinstance(value, float):
        raise ValueError(
            'Monetary amounts must be sent as strings (e.g. "115.00"), not JSON numbers.'
        )
    if isinstance(value, Decimal):
        return value
    return to_decimal(value)


#: Decimal on the inside, string on the wire.
MoneyStr = Annotated[
    Decimal,
    BeforeValidator(_parse_money),
    PlainSerializer(lambda v: f"{v:f}", return_type=str, when_used="json"),
]

#: Percentage rates follow the same rule.
RateStr = MoneyStr

CurrencyCode = Annotated[str, BeforeValidator(lambda v: str(v).strip().upper())]


class AppModel(BaseModel):
    """Base for request/response models."""

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        extra="forbid",
    )


class ORMModel(AppModel):
    """Base for models read directly off ORM instances."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class ErrorResponse(AppModel):
    """The single error shape every failure uses. Documented in OpenAPI."""

    error: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


def isoformat(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
