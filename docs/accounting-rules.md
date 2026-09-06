# Accounting rules

Every rule below is enforced in code and, where the database can express it,
enforced again in the schema. The application check gives a good error message;
the constraint makes the bad state impossible. For financial data that
redundancy is worth the cost.

Pure rule implementations: [`app/shared/domain/rules.py`](../app/shared/domain/rules.py).
Unit tests: [`tests/unit/test_accounting_rules.py`](../tests/unit/test_accounting_rules.py).

---

## Double entry

Every transaction affects at least two accounts, and total debits always equal
total credits. Debit and credit are not "plus" and "minus" — they are the two
sides of an entry, and which one increases an account depends on the account:

| Account type | Normal balance | Debit | Credit |
| --- | --- | --- | --- |
| ASSET | DEBIT | increases | decreases |
| EXPENSE | DEBIT | increases | decreases |
| LIABILITY | CREDIT | decreases | increases |
| EQUITY | CREDIT | decreases | increases |
| REVENUE | CREDIT | decreases | increases |

Closing balances follow the normal balance:

```text
DEBIT-normal:   closing = opening + total_debits - total_credits
CREDIT-normal:  closing = opening + total_credits - total_debits
```

**Contra accounts.** `normal_balance` is stored per account and defaults to the
type's natural side, but may differ. Accumulated depreciation is an `ASSET`
carrying a `CREDIT` balance; sales discounts are `REVENUE` carrying a `DEBIT`
balance. Balance arithmetic follows `normal_balance`; report grouping follows
`account_type`. Forcing them to agree would make contra accounts impossible,
and every real chart of accounts has them.

---

## Rule 1 — a posted entry balances

```text
SUM(debit) == SUM(credit)
```

Otherwise `UNBALANCED_JOURNAL_ENTRY` (422), with the difference in `details`:

```json
{
  "error": "UNBALANCED_JOURNAL_ENTRY",
  "message": "Total debits must equal total credits.",
  "details": {"total_debits": "115.0000", "total_credits": "114.0000", "difference": "1.0000"}
}
```

An entry whose totals are both zero is also rejected — it balances trivially
but records nothing.

Comparison is on `Decimal`, so `0.10 + 0.20 == 0.30` exactly. In float it would
not, and an entry would fail to balance for reasons no accountant could explain.

---

## Rule 2 — one side per line

A line carries a value on exactly one side:

* `debit >= 0` and `credit >= 0`
* `debit + credit > 0` — not both zero
* `(debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)` — not both non-zero

Enforced by `rules.validate_line_amounts` **and** by four CHECK constraints on
`journal_items`. A line that violates either is `INVALID_JOURNAL_LINE`.

Line numbers must be `1..n` — sequential, no gaps, no duplicates — and an entry
needs at least two lines. Both are checked in the application layer;
`UNIQUE(entry_id, line_number)` catches duplicates in the database.

---

## Money

* `Decimal` in Python. **Never** `float` — `to_decimal()` raises on a float
  argument, and a pre-commit hook rejects `float(amount)`-shaped code.
* `NUMERIC(19,4)` in PostgreSQL. There is no FLOAT column in the schema.
* **Strings on the wire**, in both directions: `"debit": "115.00"`. JSON
  numbers are IEEE-754 doubles in most clients, so accepting one would admit a
  rounding error before validation ever ran. Sending a JSON number is a 422.
* Rounding is configurable (`MONEY_ROUNDING`), default `ROUND_HALF_UP` — the
  convention accountants expect, not banker's rounding.
* Precision is validated against the currency's own `decimal_places`. `"115.005"`
  in USD is rejected rather than silently rounded: a caller sending it has a bug
  worth surfacing. JPY (0 decimals) rejects `"115.50"`.

---

## Rule 3 — fiscal period control

An entry may only post when:

```text
period.start_date <= entry_date <= period.end_date
```

and the period's status accepts postings:

| Status | Accepts postings | Meaning |
| --- | --- | --- |
| `OPEN` | yes | Normal trading |
| `ADJUSTING` | yes | Closed to ordinary traffic, open for adjustments |
| `CLOSED` | **no** | Books closed; reopening is a privileged action |
| `LOCKED` | **no** | Filed. Terminal — nothing reopens it |

Allowed transitions:

```text
OPEN ⇄ ADJUSTING          OPEN → CLOSED          CLOSED → OPEN (reopen)
ADJUSTING → CLOSED        CLOSED → LOCKED        LOCKED → (nothing)
```

`period:close` and `period:reopen` are separate permissions: an accountant can
close a period, but reopening one is an administrative act. Every transition
writes an audit record and an outbox event.

**The lock.** Posting takes `SELECT … FOR UPDATE` on the period row and holds
it until commit. Closing takes the same lock. A close therefore cannot slip
between a posting's status check and its insert — the two serialise on one row,
and only on that row.

Periods cannot overlap: a GiST exclusion constraint on
`(company_id, daterange(start_date, end_date, '[]'))` makes it impossible even
under concurrent inserts, where an application check alone would lose the race.

---

## Rule 4 — atomic posting

```text
BEGIN
  validate command
  validate company, currency          (exist, active)
  lock fiscal period                  SELECT … FOR UPDATE
  validate date in period, status accepts postings
  validate accounts                   (exist, same company, active, postable)
  validate tax codes                  (exist, active, effective on entry_date)
  validate lines, precision, numbering
  compute totals, assert balanced
  resolve idempotency
  allocate entry number               atomic upsert
  INSERT journal_entries
  INSERT journal_items
  UPSERT ledger_balances
  INSERT audit_logs
  INSERT outbox_events
COMMIT
```

Any failure rolls back everything. There is no partial accounting transaction.

Only `POSTED` (and `REVERSED`) entries affect `ledger_balances`. A draft has
zero ledger impact — `tests/integration` asserts the projection stays empty
while a draft exists.

---

## Rule 5 — posted entries are immutable

A posted entry is never updated and never deleted.

| Operation on a POSTED entry | Result |
| --- | --- |
| `PATCH /journal-entries/{id}` | 409 `POSTED_ENTRY_CANNOT_BE_MODIFIED` |
| `DELETE /journal-entries/{id}` | 409 `POSTED_ENTRY_CANNOT_BE_DELETED` |
| `POST /journal-entries/{id}/void` | 409 `INVALID_JOURNAL_STATE` |
| `POST /journal-entries/{id}/reverse` | ✅ the only correction |

Accounts with posted history are never hard deleted either — deactivate them
with `is_active = false`. Deleting one would orphan history that the ledger
must still be able to explain.

---

## Reversal

A reversal is not a deletion. It is a new **posted** entry that mirrors the
original, so the ledger nets to zero while both documents remain visible
forever:

```text
Original  JE-2026-000001              Reversal  JE-2026-000002
  Bank            Dr 115.00             Bank                  Cr 115.00
  SaaS Revenue        Cr 100.00         SaaS Revenue    Dr 100.00
  Sales Tax Payable   Cr  15.00         Sales Tax Payable Dr  15.00
```

Then:

* `original.status = REVERSED`, `original.reversed_by_entry_id = reversal.id`
* `reversal.status = POSTED`, `reversal.reverses_entry_id = original.id`
* both entries audited; a `JournalReversed` event is queued

An entry can only be reversed once — `JOURNAL_ALREADY_REVERSED` otherwise. The
reversal must land in a period that accepts postings, which may differ from the
original's (reverse a March entry into April by passing `reversal_date`).

**Deactivated accounts are still reversible.** If an account was deactivated
after the original posted, the reversal still goes through — otherwise a
mistake could become permanently uncorrectable. Non-postable roll-up accounts
remain rejected.

After reversal the gross movement is preserved (`total_debits` and
`total_credits` both grew) and only the net is zero. That is the point: an
audit trail shows what happened *and* that it was undone.

---

## Journal numbering

Numbers look like `JE-2026-000001` — prefix, fiscal year, six digits.

They are allocated from `document_sequences` with **one statement**:

```sql
INSERT INTO document_sequences (id, company_id, document_type, fiscal_year, next_number, updated_at)
VALUES (:id, :company_id, :type, :year, 2, now())
ON CONFLICT (company_id, document_type, fiscal_year)
DO UPDATE SET next_number = document_sequences.next_number + 1, updated_at = now()
RETURNING document_sequences.next_number - 1
```

Never `SELECT MAX(entry_number)`. Never an in-memory counter. Both lose races;
this cannot, because the `DO UPDATE` takes a row lock and two transactions can
never be handed the same value.

Allocation happens **inside** the posting transaction.

**Gap policy.** Gaps are possible and acceptable. If a posting transaction
rolls back, its number is simply never used. The alternative — reusing numbers
— would let two different entries share a number over the life of the system,
which is far worse for an audit trail than a missing one. Where a jurisdiction
requires strictly gapless numbering, that is a reporting-time reconciliation
concern, not a reason to make allocation lossy.

---

## Idempotency

ERP integrations retry. Queues deliver at least once. Webhooks fire twice. A
double-posted invoice is a real financial error, so retry safety is built in.

Supply a key via the `Idempotency-Key` header or the `idempotency_key` body
field — typically the source document's own key, `sales_invoice:INV-9923`.

```text
same key + same payload      → the original entry is returned (201, not a new entry)
same key + different payload → 409 DUPLICATE_IDEMPOTENCY_KEY
no key                       → no deduplication
```

**The database is the source of truth**: a partial unique index on
`(company_id, idempotency_key) WHERE idempotency_key IS NOT NULL`. Redis may
cache a resolution but never decides one; a cache miss falls through.

The payload hash is a SHA-256 over a *canonical* representation
(`app/core/idempotency.py`): keys sorted, decimals normalised. So `"100"`,
`"100.0"` and `"100.00"` hash identically — the hash asks "is this the same
accounting transaction?", not "were the bytes identical". `post_immediately`
and the key itself are excluded for the same reason.

---

## Rule 6 — the ledger is rebuildable

`journal_entries` + `journal_items` are the truth. `ledger_balances` is a
cache, and any cache must be reconstructible:

```bash
python -m scripts.rebuild_ledger --company-id <uuid>
python -m scripts.rebuild_ledger --all --verify
python -m scripts.rebuild_ledger --all --dry-run
```

The rebuild clears the projection for a company, re-reads every ledger-
affecting entry in batches, recomputes, and writes — all in one transaction, so
readers see either the old projection or the new one. `--verify` fails if the
result disagrees with what was stored; `--dry-run` compares and rolls back.

Because posting and rebuilding run the same arithmetic over the same source
rows, they produce identical results. `tests/integration` asserts exactly that,
and separately asserts that a deliberately corrupted projection is repaired.

---

## Business error codes

| Code | HTTP | Meaning |
| --- | --- | --- |
| `UNBALANCED_JOURNAL_ENTRY` | 422 | Debits ≠ credits |
| `INVALID_JOURNAL_LINE` | 422 | Both sides, neither side, negative, or bad numbering |
| `INVALID_MONEY_PRECISION` | 422 | More decimals than the currency allows |
| `FISCAL_PERIOD_CLOSED` | 409 | Period is CLOSED or LOCKED |
| `FISCAL_PERIOD_NOT_FOUND_FOR_DATE` | 422 | No period covers `entry_date` |
| `FISCAL_PERIOD_OVERLAP` | 409 | Periods would overlap |
| `INVALID_PERIOD_TRANSITION` | 409 | Not an allowed status change |
| `ACCOUNT_NOT_POSTABLE` | 422 | Roll-up/parent account |
| `ACCOUNT_INACTIVE` | 422 | Account deactivated |
| `ACCOUNT_NOT_FOUND` | 404 | Unknown account for this company |
| `ACCOUNT_HAS_TRANSACTIONS` | 409 | Cannot delete; deactivate instead |
| `TAX_CODE_INACTIVE` | 422 | Inactive or outside its effective range |
| `CURRENCY_INACTIVE` | 422 | Currency deactivated |
| `COMPANY_INACTIVE` | 422 | Company deactivated |
| `CURRENCY_MISMATCH` | 422 | Line currency ≠ entry currency, or account is pinned |
| `CROSS_COMPANY_REFERENCE` | 422 | Referenced record belongs to another company |
| `DUPLICATE_IDEMPOTENCY_KEY` | 409 | Same key, different payload |
| `POSTED_ENTRY_CANNOT_BE_MODIFIED` | 409 | Immutability |
| `POSTED_ENTRY_CANNOT_BE_DELETED` | 409 | Immutability |
| `JOURNAL_ALREADY_REVERSED` | 409 | One reversal per entry |
| `INVALID_JOURNAL_STATE` | 409 | Not an allowed state transition |
| `CONCURRENT_MODIFICATION` | 409 | Draft `version` mismatch |

Every error uses the same envelope: `error`, `message`, `details`, `request_id`.

---

## Audit

`audit_logs` is append-only — inserted, never updated, never deleted. Audited
actions include `JOURNAL_POSTED`, `JOURNAL_REVERSED`, `JOURNAL_VOIDED`,
`PERIOD_CLOSED`, `PERIOD_LOCKED`, `PERIOD_REOPENED`, `ACCOUNT_DEACTIVATED`,
`USER_ROLE_CHANGED` and `LEDGER_REBUILT`.

Each row records who, what, when, from where (IP, user agent) and under which
`request_id`, plus a whitelisted JSONB payload. **Never** logged: passwords,
password hashes, raw or hashed refresh tokens, JWTs, secret keys. The structlog
pipeline scrubs those keys too, in case one is ever passed by accident.
