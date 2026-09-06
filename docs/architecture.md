# Architecture

## The decision: a modular monolith

This is one deployable, one database, one transaction manager — with hard
internal boundaries.

An ERP's General Ledger is defined by a property microservices make expensive:
**a business transaction and its accounting effect must commit together or not
at all**. Posting an invoice writes a journal header, its lines, ledger
balances, an audit record and an outbox event. Split those across services and
that atomic write becomes a distributed saga with compensating transactions —
an enormous amount of machinery to reach a guarantee a single `BEGIN … COMMIT`
already gives for free.

So: modular monolith. Boundaries are enforced in code (module ownership,
dependency direction, a single posting gateway) rather than by the network. If
one module later needs to scale independently, the boundary it must be cut
along is already drawn, and the transactional outbox already publishes the
events it would need.

---

## Layers

Dependencies point inward. The domain knows nothing about anything else.

```text
┌────────────────────────────────────────────────────────────┐
│ api/          FastAPI routers                              │
│               authenticate · authorise · parse · delegate  │
│               no business logic, ever                      │
├────────────────────────────────────────────────────────────┤
│ application/  use-case services, commands, DTOs            │
│               orchestration, transaction boundaries        │
├────────────────────────────────────────────────────────────┤
│ domain/       entities, value objects, rules, events       │
│               pure Python — no SQLAlchemy, no FastAPI      │
├────────────────────────────────────────────────────────────┤
│ infrastructure/  ORM models and repositories               │
└────────────────────────────────────────────────────────────┘
```

Those layers repeat *inside each module*, so a module is a vertical slice
rather than a quarter of four horizontal ones:

```text
app/
├── main.py · api.py · health.py
├── core/            config · security · money · ids · pagination
│                    exceptions · middleware · dependencies
├── shared/
│   ├── domain/      the ledger kernel — entities, enums, rules,
│   │                value objects, events, exceptions
│   └── …            database · unit_of_work · audit · outbox · sequences
│                    cache · idempotency · observability
└── modules/         each: application/ infrastructure/ api/
    ├── identity/        users, roles, permissions, refresh tokens
    ├── organization/    companies and currencies
    ├── accounts/        the chart of accounts
    ├── fiscal_periods/  periods and their state machine
    ├── taxes/           tax codes
    ├── journal/         journal entries, items, the ledger projection
    └── reports/         trial balance, P&L, balance sheet, general ledger
```

This is the same layout as the sibling [`../Core`](../../Core) and
[`../HRM`](../../HRM) modules, so a developer moving between the three ERP
services finds the same file in the same place.

**Why the domain is a shared kernel, not one per module.** In Core and HRM each
module owns its own `domain/`, because leave rules genuinely never touch
payroll rules. A general ledger is not like that. Posting a single journal
entry validates the entry's balance, the account's postability, the period's
status and the tax code's validity **in one breath**:

```python
validate_balanced(entry)
validate_account_usable(account, company_id, line_number)
validate_period_accepts_posting(period)
validate_tax_code_usable(tax_code, ...)
```

Those four live together in `app/shared/domain/rules.py` because they *are* one
rule set — the double-entry bookkeeping model — and splitting them across four
modules would scatter one cohesive surface and make `journal` import the
private domain of three siblings. The ledger is a single bounded context, so it
gets a single domain package that every module reads.

**Why the domain is framework-free.** `app/shared/domain/rules.py` contains
every accounting invariant as a pure function. It imports no ORM and touches no
I/O, so the entire rule set is unit-testable in milliseconds
(`tests/unit/test_accounting_rules.py` runs with no database at all), and the
rules can be read as accounting rather than as SQLAlchemy.

**Why routes are thin.** A route does exactly five things: authenticate,
authorise, parse, call an application service, return. Anything else creates a
second place accounting decisions get made — and the one guarantee this system
sells is that there is only one.

---

## The posting gateway

```text
Sales · CRM · AR · Purchasing · AP · Inventory · Payroll · HR · POS · e-commerce
                                  │
                                  ▼
                       Accounting application layer
                                  │
                                  ▼
                       JournalPostingService          ← the only door
                                  │
                                  ▼
                          General Ledger
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
      ledger_balances         reports             audit_logs
       (projection)          (derived)           (append-only)
```

No module may update `ledger_balances`. No module may insert a `POSTED`
journal entry. Both rules are enforced structurally:

* `LedgerRepository.apply_movements` is called from exactly two places, both
  inside `app/modules/journal/application/` (posting and reversal);
* `journal_entries` has a CHECK constraint requiring `entry_number` and
  `posted_at` on any `POSTED`/`REVERSED` row, and numbers only come from
  `DocumentSequenceRepository` inside the posting transaction.

A module integrating with the GL builds a `CreateJournalCommand` (see
`app/modules/journal/application/commands.py`) and calls `create()`. That command is
the stable contract — the HTTP layer is just one caller of it, and an
in-process Payroll module calls the same code path with no HTTP hop.

---

## Repository pattern and Unit of Work

**Repositories** own SQL. Nothing above `infrastructure/` imports SQLAlchemy in
its signatures. Repositories never commit — deciding when a transaction ends is
not a query's business.

**The Unit of Work** (`infrastructure/database/unit_of_work.py`) is the
transaction boundary and carries the repositories that share its session:

```python
async with self.uow:                      # rolls back on any exception
    validated = await self.validator.validate_create(command, lock_period=True)
    entry     = await self._insert_entry(validated, command, post=True)
    await self._apply_ledger(validated, entry)
    self._audit_posted(entry, validated, command.actor)
    self._enqueue_posted_event(entry, validated)
    await self.uow.commit()               # one COMMIT, or none
```

One UoW == one database transaction == one accounting operation. There is no
code path that commits a partial accounting transaction, because there is no
code path that commits twice.

The UoW also translates database constraint violations into domain errors
(`_CONSTRAINT_ERRORS`). A race that slips past an application check still
surfaces as `DUPLICATE_IDEMPOTENCY_KEY`, not as a 500.

---

## Transaction boundaries and concurrency

Isolation is PostgreSQL's default, **READ COMMITTED**. Correctness comes from
targeted locks and atomic writes, not from a blanket SERIALIZABLE that would
serialise unrelated postings and buy nothing the locks don't already provide.

| Hazard | Mechanism |
| --- | --- |
| A period closes mid-posting | `SELECT … FOR UPDATE` on the `fiscal_periods` row, held for the whole transaction. A close takes the same lock. |
| Two postings allocate the same number | One statement: `INSERT … ON CONFLICT DO UPDATE SET next_number = next_number + 1 RETURNING next_number - 1`. |
| Concurrent postings to the same account lose an update | `INSERT … ON CONFLICT DO UPDATE SET total_debits = total_debits + EXCLUDED.total_debits`. Never `SELECT` then `UPDATE`. |
| Two drafts edited at once | `version` column, optimistic check on PATCH. |
| Overlapping fiscal periods | GiST exclusion constraint on `(company_id, daterange(start_date, end_date, '[]'))`. |
| A retried request double-posts | Partial unique index on `(company_id, idempotency_key)` plus a payload hash. |
| Two relay workers grab the same event | `SELECT … FOR UPDATE SKIP LOCKED`. |

`tests/integration/test_concurrency_and_reports.py` posts twenty entries
concurrently on twenty connections and asserts twenty distinct numbers and an
exact ledger total. That test fails under a read-modify-write ledger update or
a `SELECT MAX()` numbering scheme, which is why it exists.

Ledger movements are sorted by `(account_id, currency_code)` before the upsert,
so concurrent postings acquire ledger row locks in the same order and cannot
deadlock against each other.

---

## Identifier strategy: UUIDv7

Every entity uses a **UUIDv7** primary key, generated by the application
(`app/core/ids.py`). Currency keeps `CHAR(3)` — its natural key is stable,
globally agreed and readable in raw SQL.

Why UUIDv7 rather than a bigserial or UUIDv4:

* **Public identifiers can be exposed safely.** Sequential integers leak
  volume ("we are customer #4") and invite enumeration. Databases sequences are
  never exposed by this API.
* **They are time ordered.** Unlike UUIDv4, v7 embeds a millisecond timestamp
  in its high bits, so index inserts stay local instead of scattering across
  the B-tree. That matters for `journal_items`, the table that grows fastest.
* **They can be generated before the insert.** The posting service builds the
  entire object graph — entry, lines, ledger movements, outbox event — in
  memory with known ids, then writes it in one flush per table. With
  server-generated ids it would need a round trip before it could write the
  lines.
* **They are free to merge across systems.** When a Payroll module eventually
  runs elsewhere, its ids will not collide with the GL's.

The generator adds a 12-bit monotonic counter so values are strictly
increasing within a process even inside a single millisecond, and never regress
if the clock steps backwards.

`app/core/pagination.py` exploits the ordering: a cursor is just the last row's
id, and `WHERE id > :cursor ORDER BY id` is both correct under concurrent
inserts and index-friendly.

---

## The ledger projection

The authoritative accounting history is exactly two tables:

```text
journal_entries → journal_items
```

`ledger_balances` is a **cache** — a per-`(company, account, period, currency)`
rollup, maintained inside the posting transaction so it is never stale, and
fully re-derivable:

```bash
python -m scripts.rebuild_ledger --all --verify
```

The rebuild reads every `POSTED`/`REVERSED` entry, recomputes, and compares.
`--verify` fails loudly on any disagreement — the right behaviour for a nightly
job.

**V1 semantics.** A row holds the *movement within one fiscal period*.
`opening_balance` stays `0.0000`, and opening figures are derived at report
time by cumulating prior periods (`ReportRepository.opening_balances`). This is
deliberate: it keeps the stored value derivation-free, which is what makes an
incremental posting and a from-scratch rebuild produce byte-identical results.
A stored opening balance would depend on the order periods were closed in, and
the two paths would drift.

**Reports read the journals, not the projection.** `trial-balance`,
`general-ledger`, `profit-and-loss` and `balance-sheet` aggregate
`journal_items` directly, so a trial balance always ties even if the projection
were somehow damaged. `account-balances` deliberately reads the projection
instead and reports `projection_matches_journals` — making the two independent
views comparable is the cheapest possible integrity check.

---

## Integrating a future ERP module

A module never writes accounting tables. It builds a command:

```python
from app.application.journal.commands import (
    ActorContext, CreateJournalCommand, JournalLineCommand,
)
from app.application.journal.posting_service import JournalPostingService

command = CreateJournalCommand(
    company_id=company_id,
    entry_date=invoice.issued_on,
    currency_code="USD",
    source_type="sales_invoice",          # provenance, not accounting meaning
    source_document=invoice.number,
    reference=f"Customer invoice {invoice.number}",
    idempotency_key=f"sales_invoice:{invoice.number}",
    actor=ActorContext(user_id=user_id),
    lines=[
        JournalLineCommand.of(1, debit="115.00", account_number="101"),
        JournalLineCommand.of(2, credit="100.00", account_number="402"),
        JournalLineCommand.of(3, credit="15.00", account_number="205",
                              tax_code="VAT15"),
    ],
)
entry = await JournalPostingService(uow).create(command)
```

Three properties make this work for **any** vertical:

1. **The GL has no domain vocabulary.** It does not model invoices, enrolments,
   shipments or payslips. `source_type` / `source_document` record where a
   transaction came from; the double entry records what it did. An education
   consultancy posts `("student_fee", "ENR-2026-118")`, a manufacturer posts
   `("goods_receipt", "GRN-4471")`, and the ledger treats them identically.
2. **The idempotency key is the module's own document key.** Retries, at-least-
   once queues and duplicate webhooks all become safe without the module
   tracking what it has already posted.
3. **Accounts are addressed by number.** Modules configure "revenue account
   402", not a UUID they would have to look up.

Where tax lives: the GL stores tax *codes* and the account they post to; it
does not compute tax. Jurisdiction-specific calculation belongs to the
originating module, which submits the resulting lines. Keeping tax engines out
of the ledger is what lets one ledger serve many jurisdictions.

---

## Assumptions and deliberate limits

* **Permissions are global, not per-company.** `company_id` is an explicit
  parameter on every endpoint. Adding per-user company scoping is an additive
  change to `deps.get_current_user` and a `WHERE` clause in the repositories;
  it is not a schema change.
* **Transaction currency only.** See [`multi-currency.md`](multi-currency.md).
  FX revaluation and translation are not implemented; the schema is shaped so
  they can be added without restructuring.
* **No year-end close.** The balance sheet folds current-period earnings into
  equity so the accounting equation holds before a formal close exists.
* **The outbox has no relay.** Events are written transactionally; publishing
  them is a separate process, deliberately outside the accounting transaction —
  a broker outage must never block a posting.
* **Redis is never authoritative.** It carries rate-limit counters and caches.
  Every caller tolerates it being down; readiness degrades rather than fails.
* **`POST /journal-entries` posts by default** (`post_immediately: true`).
  Integrating modules want a posted document back from one call. Pass `false`
  to park a draft for human review.
