"""Rebuild the ledger projection from the journals.

    python -m app.scripts.rebuild_ledger --company-id <uuid>
    python -m app.scripts.rebuild_ledger --all --verify

`ledger_balances` is a cache. `journal_entries` + `journal_items` are the
truth. This script throws the cache away and recomputes it, and the result
must be byte-for-byte what incremental posting would have produced — which is
exactly what `--verify` asserts before committing anything.

It is safe to run against a live system: the whole rebuild for a company
happens in one transaction, so readers see either the old projection or the
new one, never a half-built one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from decimal import Decimal
from uuid import UUID

from app.core.logging import configure_logging, get_logger
from app.core.money import ZERO
from app.domain.accounting.entities import LedgerMovement
from app.domain.accounting.enums import AuditAction, JournalStatus
from app.infrastructure.database.models.journal import JournalEntry
from app.infrastructure.database.session import dispose_engine, session_scope
from app.infrastructure.database.unit_of_work import UnitOfWork

logger = get_logger("rebuild_ledger")

BATCH_SIZE = 500


async def rebuild_company(
    uow: UnitOfWork, company_id: UUID, *, verify: bool, dry_run: bool
) -> tuple[int, int, bool]:
    """Recompute one company's projection.

    Returns `(entries_processed, balance_rows, matched_previous)`.
    """
    previous = {
        (row.account_id, row.period_id, row.currency_code): (
            row.total_debits,
            row.total_credits,
            row.closing_balance,
        )
        for row in await uow.ledger.list_for_company(company_id)
    }

    accounts = {account.id: account for account in await uow.accounts.list_for_company(company_id)}

    # (account_id, period_id, currency_code) -> [debits, credits]
    totals: dict[tuple[UUID, UUID, str], list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])

    processed = 0
    after_id: UUID | None = None
    while True:
        batch: list[JournalEntry] = list(
            await uow.journal.list_posted_for_company(
                company_id, batch_size=BATCH_SIZE, after_id=after_id
            )
        )
        if not batch:
            break
        for entry in batch:
            if entry.status not in (JournalStatus.POSTED, JournalStatus.REVERSED):
                continue  # defensive; the query already filters
            for item in entry.items:
                key = (item.account_id, entry.period_id, item.currency_code.upper())
                totals[key][0] += item.debit
                totals[key][1] += item.credit
            processed += 1
        after_id = batch[-1].id

    movements: list[LedgerMovement] = []
    for (account_id, period_id, currency_code), (debits, credits) in totals.items():
        account = accounts.get(account_id)
        if account is None:  # pragma: no cover - FK makes this unreachable
            raise RuntimeError(f"journal references unknown account {account_id}")
        movements.append(
            LedgerMovement(
                company_id=company_id,
                account_id=account_id,
                period_id=period_id,
                currency_code=currency_code,
                total_debits=debits,
                total_credits=credits,
                normal_balance=account.normal_balance,
            )
        )

    deleted = await uow.ledger.clear_company(company_id)
    await uow.ledger.apply_movements(movements)
    await uow.flush()

    rebuilt = {
        (movement.account_id, movement.period_id, movement.currency_code): (
            movement.total_debits,
            movement.total_credits,
            movement.net,
        )
        for movement in movements
    }
    matched = _compare(previous, rebuilt) if previous else True

    logger.info(
        "rebuild.company_done",
        company_id=str(company_id),
        entries=processed,
        balance_rows=len(movements),
        rows_deleted=deleted,
        matched_previous=matched,
    )

    if verify and previous and not matched:
        raise SystemExit(
            "Rebuilt ledger differs from the stored projection. "
            "Re-run without --verify to overwrite, after investigating."
        )

    if dry_run:
        await uow.rollback()
    else:
        uow.audit.record(
            action=AuditAction.LEDGER_REBUILT,
            entity_type="LedgerBalance",
            entity_id=str(company_id),
            company_id=company_id,
            payload={
                "entries_processed": processed,
                "balance_rows": len(movements),
                "matched_previous": matched,
            },
        )
        await uow.commit()

    return processed, len(movements), matched


def _compare(
    previous: dict[tuple[UUID, UUID, str], tuple[Decimal, Decimal, Decimal]],
    rebuilt: dict[tuple[UUID, UUID, str], tuple[Decimal, Decimal, Decimal]],
) -> bool:
    if set(previous) != set(rebuilt):
        for key in set(previous) ^ set(rebuilt):
            logger.warning("rebuild.key_mismatch", key=[str(part) for part in key])
        return False
    ok = True
    for key, before in previous.items():
        after = rebuilt[key]
        if before != after:
            ok = False
            logger.warning(
                "rebuild.value_mismatch",
                key=[str(part) for part in key],
                before=[str(v) for v in before],
                after=[str(v) for v in after],
            )
    return ok


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild ledger_balances from journals.")
    parser.add_argument("--company-id", type=UUID, help="Rebuild a single company.")
    parser.add_argument("--all", action="store_true", help="Rebuild every company.")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Fail if the rebuild disagrees with the stored projection.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and compare, then roll back without writing.",
    )
    args = parser.parse_args(argv)

    if not args.company_id and not args.all:
        parser.error("pass --company-id <uuid> or --all")

    configure_logging()

    exit_code = 0
    try:
        async with session_scope() as session:
            uow = UnitOfWork(session)
            if args.all:
                companies = await uow.companies.list_all()
                targets = [company.id for company in companies]
            else:
                targets = [args.company_id]

            for company_id in targets:
                entries, rows, matched = await rebuild_company(
                    uow, company_id, verify=args.verify, dry_run=args.dry_run
                )
                print(
                    f"company={company_id} entries={entries} rows={rows} "
                    f"matched_previous={matched}"
                    + (" (dry run, rolled back)" if args.dry_run else "")
                )
                if not matched:
                    exit_code = 2
    finally:
        await dispose_engine()

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
