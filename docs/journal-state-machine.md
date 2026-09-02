# Journal entry state machine

```text
                        ┌─────────┐
              create ──▶│  DRAFT  │
           (post=false) └────┬────┘
                             │
                ┌────────────┴────────────┐
           post │                         │ void
                ▼                         ▼
           ┌────────┐               ┌──────────┐
  create ─▶│ POSTED │               │   VOID   │  terminal
 (default) └───┬────┘               └──────────┘
               │ reverse
               │ (creates a second POSTED entry)
               ▼
          ┌──────────┐
          │ REVERSED │  terminal
          └──────────┘
```

Implemented as data in
[`app/domain/accounting/rules.py`](../app/domain/accounting/rules.py):

```python
_ALLOWED_JOURNAL_TRANSITIONS = {
    JournalStatus.DRAFT:    frozenset({JournalStatus.POSTED, JournalStatus.VOID}),
    JournalStatus.POSTED:   frozenset({JournalStatus.REVERSED}),
    JournalStatus.REVERSED: frozenset(),
    JournalStatus.VOID:     frozenset(),
}
```

Anything not in that table is `INVALID_JOURNAL_STATE` (409). Keeping the
machine as a lookup rather than scattered `if` statements means the whole rule
is visible in one place and testable by parametrising over every pair.

---

## DRAFT

A work-in-progress entry with **no accounting effect**.

| Property | Value |
| --- | --- |
| `entry_number` | `NULL` — numbers are only issued at posting |
| `posted_at` | `NULL` |
| Ledger impact | none |
| Appears in reports | no |
| Editable | yes |
| Deletable | yes |

Can be edited (`PATCH`), deleted (`DELETE`), posted, or voided.

Editing replaces lines wholesale rather than patching them individually — a
partial line patch would make "does this entry balance?" ambiguous mid-edit.
Concurrent edits are caught by the `version` column: send the version you read,
and a mismatch is `CONCURRENT_MODIFICATION` (409).

A draft may sit for weeks, so **every rule is re-evaluated at posting time, not
at creation**. Accounts may have been deactivated, the period closed, tax codes
expired. Posting is the moment that matters, so posting is where the rules bind
(`JournalPostingService._revalidate_stored`).

Drafts are also why a period close does not lock the draft path: creating a
draft never takes the fiscal-period lock, because a draft has no accounting
effect and must not block a close.

---

## POSTED

The entry is part of the general ledger. Immutable.

| Property | Value |
| --- | --- |
| `entry_number` | allocated, e.g. `JE-2026-000001` |
| `posted_at` / `posted_by` | set |
| Ledger impact | applied to `ledger_balances` |
| Appears in reports | yes |
| Editable | **no** — 409 `POSTED_ENTRY_CANNOT_BE_MODIFIED` |
| Deletable | **no** — 409 `POSTED_ENTRY_CANNOT_BE_DELETED` |
| Voidable | **no** — 409 `INVALID_JOURNAL_STATE` |

The database backs this up: a CHECK constraint requires `entry_number` and
`posted_at` to be non-null exactly when the status is `POSTED`/`REVERSED`, and
null when it is `DRAFT`/`VOID`. A row cannot be half-posted.

The only correction is a **reversal**.

---

## REVERSED

The entry was posted, and a counter-entry has been posted against it. Terminal.

| Property | Value |
| --- | --- |
| `reversed_by_entry_id` | points at the reversal |
| Ledger impact | **retained** — the reversal cancels it, nothing is erased |
| Appears in reports | yes, alongside its reversal |

Both entries keep full ledger weight. After reversing a 115.00 invoice, the
bank account shows `total_debits = 115.00`, `total_credits = 115.00`,
`closing_balance = 0.00`. The net is zero; the history is intact. An auditor
can see that something happened and that it was undone — which is exactly what
an erasure would destroy.

An entry can be reversed once. A second attempt is `JOURNAL_ALREADY_REVERSED`.

The reversal entry itself is `POSTED` — it must be, because it changes
balances. It carries `reverses_entry_id` back to the original, so the pair is
navigable in both directions.

---

## VOID

The entry never happened, and never had any accounting effect. Terminal.

| Property | Value |
| --- | --- |
| `entry_number` | `NULL` — a void entry never consumed a number |
| Ledger impact | none, and never had any |
| Appears in reports | no |

**Only a DRAFT can be voided.** This is the distinction that matters most:

> **Void** means *this was never accounting.*
> **Reversal** means *this was accounting, and here is the accounting that undoes it.*

Allowing a posted entry to be voided would let someone remove a transaction
from the ledger with no trace — the single most damaging thing an accounting
system can permit. So the state machine has no `POSTED → VOID` edge at all, and
the API returns 409 for the attempt.

Void exists for the mundane case: a draft that was keyed by mistake and should
be kept for the record rather than deleted.

---

## Choosing between them

| Situation | Do this |
| --- | --- |
| Draft was keyed in error, keep a trace | **Void** it |
| Draft was keyed in error, no trace needed | **Delete** it |
| Posted entry has the wrong amount/account | **Reverse** it, then post a correct one |
| Posted entry was a duplicate | **Reverse** it |
| Posted entry needs a period-end adjustment | Move the period to `ADJUSTING`, post the adjusting entry |
| Posted entry is fine but the period is closed | Reverse into a later open period, if reversal is warranted at all |

---

## What each transition writes

| Transition | Number | Ledger | Audit | Outbox |
| --- | --- | --- | --- | --- |
| → `DRAFT` (create) | — | — | `JOURNAL_CREATED` | — |
| `DRAFT` → `DRAFT` (edit) | — | — | `JOURNAL_UPDATED` | — |
| `DRAFT` → deleted | — | — | `JOURNAL_DELETED` | — |
| `DRAFT` → `POSTED` | allocated | applied | `JOURNAL_POSTED` | `JournalPosted` |
| → `POSTED` (create + post) | allocated | applied | `JOURNAL_POSTED` | `JournalPosted` |
| `DRAFT` → `VOID` | — | — | `JOURNAL_VOIDED` | `JournalVoided` |
| `POSTED` → `REVERSED` | allocated (for the reversal) | applied (the reversal's) | `JOURNAL_REVERSED` + `JOURNAL_POSTED` | `JournalReversed` |

Each row is one atomic transaction. Nothing in it commits without the rest.
