"""Currency application service."""

from __future__ import annotations

from app.core.exceptions import ConflictError, NotFoundError
from app.modules.journal.application.commands import ActorContext
from app.modules.organization.application.schemas import (
    CurrencyCreate,
    CurrencyRead,
    CurrencyUpdate,
)
from app.modules.organization.infrastructure.models import Currency
from app.shared.domain.enums import AuditAction
from app.shared.unit_of_work.unit_of_work import UnitOfWork


class CurrencyService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def create(self, payload: CurrencyCreate, actor: ActorContext) -> CurrencyRead:
        existing = await self.uow.currencies.get_by_code(payload.currency_code)
        if existing is not None:
            raise ConflictError(
                "That currency already exists.",
                code="DUPLICATE_CURRENCY",
                details={"currency_code": payload.currency_code},
            )

        currency = Currency(
            currency_code=payload.currency_code,
            currency_name=payload.currency_name,
            decimal_places=payload.decimal_places,
            is_active=payload.is_active,
        )
        self.uow.currencies.add(currency)

        self.uow.audit.record(
            action=AuditAction.CURRENCY_CREATED,
            entity_type="Currency",
            entity_id=currency.currency_code,
            user_id=actor.user_id,
            payload={
                "currency_code": currency.currency_code,
                "decimal_places": currency.decimal_places,
            },
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()
        return CurrencyRead.model_validate(currency)

    async def get(self, currency_code: str) -> CurrencyRead:
        currency = await self.uow.currencies.get_by_code(currency_code)
        if currency is None:
            raise NotFoundError("Currency not found.", details={"currency_code": currency_code})
        return CurrencyRead.model_validate(currency)

    async def list(self, *, is_active: bool | None = None) -> list[CurrencyRead]:
        rows = await self.uow.currencies.list_all_ordered(is_active=is_active)
        return [CurrencyRead.model_validate(row) for row in rows]

    async def update(
        self, currency_code: str, payload: CurrencyUpdate, actor: ActorContext
    ) -> CurrencyRead:
        currency = await self.uow.currencies.get_by_code(currency_code)
        if currency is None:
            raise NotFoundError("Currency not found.", details={"currency_code": currency_code})

        changes = payload.model_dump(exclude_none=True)
        # Changing scale retroactively would reinterpret every stored amount in
        # this currency. Refuse rather than silently rescale.
        if (
            "decimal_places" in changes
            and changes["decimal_places"] != currency.decimal_places
            and await self._currency_in_use(currency.currency_code)
        ):
            raise ConflictError(
                "Cannot change decimal places for a currency that has accounting activity.",
                code="CURRENCY_IN_USE",
                details={"currency_code": currency.currency_code},
            )

        for field, value in changes.items():
            setattr(currency, field, value)

        self.uow.audit.record(
            action=AuditAction.CURRENCY_UPDATED,
            entity_type="Currency",
            entity_id=currency.currency_code,
            user_id=actor.user_id,
            payload={"changes": changes},
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()
        return CurrencyRead.model_validate(currency)

    async def _currency_in_use(self, currency_code: str) -> bool:
        from sqlalchemy import exists, select

        from app.modules.journal.infrastructure.models import JournalItem

        stmt = select(exists().where(JournalItem.currency_code == currency_code))
        return bool(await self.uow.session.scalar(stmt))
