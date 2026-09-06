"""Company and currency master data."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.shared.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.modules.accounts.infrastructure.models import Account


class Currency(Base, TimestampMixin):
    """ISO-4217 currency.

    The three-letter code is the natural primary key: it is stable, globally
    agreed, and readable in raw SQL. Surrogate ids buy nothing here.
    """

    __tablename__ = "currencies"

    currency_code: Mapped[str] = mapped_column(String(3), primary_key=True)
    currency_name: Mapped[str] = mapped_column(String(100), nullable=False)
    decimal_places: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint(
            "currency_code = upper(currency_code) AND length(currency_code) = 3",
            name="currency_code_format",
        ),
        CheckConstraint(
            "decimal_places >= 0 AND decimal_places <= 4",
            name="decimal_places_range",
        ),
    )


class Company(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A legal entity that keeps its own set of books."""

    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(255))
    base_currency_code: Mapped[str] = mapped_column(
        String(3),
        ForeignKey("currencies.currency_code", ondelete="RESTRICT"),
        nullable=False,
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    base_currency: Mapped[Currency] = relationship(lazy="joined")
    accounts: Mapped[list[Account]] = relationship(
        back_populates="company", lazy="noload", viewonly=True
    )
