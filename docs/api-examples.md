# API examples

Working `curl` examples for the whole lifecycle. Every response below was
produced by running these commands against a seeded database.

```bash
export BASE=http://localhost:8000/api/v1
```

---

## 1. Authenticate

```bash
curl -s $BASE/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"accountant@example.com","password":"<from make seed>"}'
```

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "K8vQ...",
  "token_type": "bearer",
  "expires_at": "2026-09-02T09:09:23Z",
  "expires_in": 1800
}
```

Keep it handy:

```bash
export TOKEN=$(curl -s $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"accountant@example.com","password":"<password>"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
export AUTH="Authorization: Bearer $TOKEN"
```

Who am I:

```bash
curl -s $BASE/auth/me -H "$AUTH"
```

```json
{
  "email": "accountant@example.com",
  "roles": ["Accountant"],
  "permissions": ["accounting:post", "accounting:read", "accounting:reverse",
                  "accounting:void", "accounting:write", "period:close", "reports:read"]
}
```

Rotate a refresh token (the presented one is revoked):

```bash
curl -s $BASE/auth/refresh -H 'Content-Type: application/json' \
  -d '{"refresh_token":"K8vQ..."}'
```

---

## 2. Find the company and period

```bash
export CO=$(curl -s "$BASE/companies" -H "$AUTH" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["id"])')

curl -s "$BASE/fiscal-periods?company_id=$CO&fiscal_year=2026&page_size=12" -H "$AUTH"
```

```json
{
  "items": [
    {"id": "01a06145-...", "period_name": "March 2026", "start_date": "2026-03-01",
     "end_date": "2026-03-31", "status": "OPEN", "is_closed": false}
  ],
  "total": 12, "page": 1, "page_size": 12
}
```

---

## 3. Post a customer invoice

The canonical example: 115.00 received, 100.00 revenue, 15.00 VAT.

```bash
curl -s $BASE/journal-entries -H "$AUTH" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: sales_invoice:INV-9923' \
  -d "{
    \"company_id\": \"$CO\",
    \"entry_date\": \"2026-03-31\",
    \"period_id\": \"$PE\",
    \"currency_code\": \"USD\",
    \"source_type\": \"sales_invoice\",
    \"source_document\": \"INV-9923\",
    \"reference\": \"Customer invoice INV-9923\",
    \"lines\": [
      {\"line_number\": 1, \"account_number\": \"101\", \"debit\": \"115.00\", \"credit\": \"0.00\"},
      {\"line_number\": 2, \"account_number\": \"402\", \"debit\": \"0.00\", \"credit\": \"100.00\"},
      {\"line_number\": 3, \"account_number\": \"205\", \"debit\": \"0.00\", \"credit\": \"15.00\", \"tax_code\": \"VAT15\"}
    ]
  }"
```

`201 Created`:

```json
{
  "id": "01a06145-dbbb-701d-94ef-d56c385c68d3",
  "entry_number": "JE-2026-000001",
  "entry_date": "2026-03-31",
  "currency_code": "USD",
  "source_type": "sales_invoice",
  "source_document": "INV-9923",
  "status": "POSTED",
  "idempotency_key": "sales_invoice:INV-9923",
  "version": 1,
  "posted_at": "2026-09-02T08:39:23.324697Z",
  "total_debits": "115.0000",
  "total_credits": "115.0000",
  "items": [
    {"line_number": 1, "account_number": "101", "account_name": "Bank Account",
     "debit": "115.0000", "credit": "0.0000"},
    {"line_number": 2, "account_number": "402", "account_name": "SaaS Revenue",
     "debit": "0.0000", "credit": "100.0000"},
    {"line_number": 3, "account_number": "205", "account_name": "Sales Tax Payable",
     "debit": "0.0000", "credit": "15.0000", "tax_id": "01a06145-..."}
  ]
}
```

Note every amount is a **string**. Sending `115.00` as a JSON number is a 422 —
see [`accounting-rules.md`](accounting-rules.md#money).

Add `"post_immediately": false` to create a `DRAFT` instead.

---

## 4. Idempotency

Replay the identical request — you get the original entry back, not a second
one:

```bash
# same Idempotency-Key, same body
→ 201  {"id": "01a06145-dbbb-...", "entry_number": "JE-2026-000001"}   # unchanged
```

Reuse the key with a different payload — refused:

```json
{
  "error": "DUPLICATE_IDEMPOTENCY_KEY",
  "message": "This idempotency key was already used with a different payload.",
  "details": {
    "idempotency_key": "sales_invoice:INV-9923",
    "existing_entry_id": "01a06145-dbbb-701d-94ef-d56c385c68d3",
    "existing_entry_number": "JE-2026-000001"
  },
  "request_id": "01a06146-..."
}
```

`409 Conflict`.

---

## 5. Rejections

Unbalanced (`422`):

```json
{
  "error": "UNBALANCED_JOURNAL_ENTRY",
  "message": "Total debits must equal total credits.",
  "details": {"total_debits": "115.0000", "total_credits": "114.0000", "difference": "1.0000"},
  "request_id": "01a06146-16bd-7028-8d08-5b1fa9167b8f"
}
```

Both sides on one line (`422`):

```json
{"error": "INVALID_JOURNAL_LINE",
 "message": "A journal line may not contain both a debit and a credit.",
 "details": {"line_number": 1, "debit": "115.0000", "credit": "115.0000"}}
```

Unknown account (`404`) — the offending line is named:

```json
{"error": "ACCOUNT_NOT_FOUND",
 "message": "Account not found for this company.",
 "details": {"line_number": 2, "account_id": null, "account_number": "999"}}
```

Posting into a non-postable roll-up account (`422`):

```json
{"error": "ACCOUNT_NOT_POSTABLE",
 "message": "Account 100 (Current Assets) is not postable.",
 "details": {"line_number": 1, "account_number": "100"}}
```

---

## 6. Drafts

```bash
# create
curl -s $BASE/journal-entries -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{... , \"post_immediately\": false}"        # → status "DRAFT", entry_number null

# edit (optimistic lock: send the version you read)
curl -s -X PATCH "$BASE/journal-entries/$ID?company_id=$CO" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"reference": "Revised reference", "version": 1}'

# post it
curl -s -X POST "$BASE/journal-entries/$ID/post?company_id=$CO" -H "$AUTH"

# or discard it
curl -s -X DELETE "$BASE/journal-entries/$ID?company_id=$CO" -H "$AUTH"   # 204
```

Patching or deleting a **posted** entry:

```json
{"error": "POSTED_ENTRY_CANNOT_BE_MODIFIED",
 "message": "A posted journal entry is immutable. Post a reversal instead.",
 "details": {"status": "POSTED"}}
```

`409 Conflict`. Same for `DELETE` (`POSTED_ENTRY_CANNOT_BE_DELETED`) and for
`void` (`INVALID_JOURNAL_STATE`).

---

## 7. Reverse a posted entry

```bash
curl -s -X POST "$BASE/journal-entries/$ID/reverse?company_id=$CO" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"reference": "Credit note CN-1"}'
```

`201 Created`:

```json
{
  "original": {"entry_number": "JE-2026-000001", "status": "REVERSED",
               "reversed_by_entry_id": "01a06146-..."},
  "reversal": {"entry_number": "JE-2026-000002", "status": "POSTED",
               "reverses_entry_id": "01a06145-dbbb-...",
               "items": [
                 {"line_number": 1, "account_number": "101", "debit": "0.0000",   "credit": "115.0000"},
                 {"line_number": 2, "account_number": "402", "debit": "100.0000", "credit": "0.0000"},
                 {"line_number": 3, "account_number": "205", "debit": "15.0000",  "credit": "0.0000"}
               ]}
}
```

Pass `reversal_date` to reverse into a later period (e.g. a March entry
reversed in April).

---

## 8. Reports

### Trial balance

```bash
curl -s "$BASE/reports/trial-balance?company_id=$CO&period_id=$PE" -H "$AUTH"
```

```json
{
  "meta": {"company_name": "Demo Company Ltd", "start_date": "2026-03-01",
           "end_date": "2026-03-31", "generated_at": "2026-09-02T08:40:11Z"},
  "rows": [
    {"account_number": "101", "account_name": "Bank Account", "account_type": "ASSET",
     "opening_balance": "0.0000", "total_debits": "115.0000",
     "total_credits": "0.0000", "closing_balance": "115.0000"},
    {"account_number": "205", "account_name": "Sales Tax Payable", "account_type": "LIABILITY",
     "opening_balance": "0.0000", "total_debits": "0.0000",
     "total_credits": "15.0000", "closing_balance": "15.0000"},
    {"account_number": "402", "account_name": "SaaS Revenue", "account_type": "REVENUE",
     "opening_balance": "0.0000", "total_debits": "0.0000",
     "total_credits": "100.0000", "closing_balance": "100.0000"}
  ],
  "total_debits": "115.0000",
  "total_credits": "115.0000",
  "is_balanced": true,
  "difference": "0.0000"
}
```

After the reversal above, the same report shows gross movement preserved and
the net at zero:

```text
 101 Bank Account         dr=115.0000  cr=115.0000  close=0.0000
 205 Sales Tax Payable    dr= 15.0000  cr= 15.0000  close=0.0000
 402 SaaS Revenue         dr=100.0000  cr=100.0000  close=0.0000
 TOTAL dr=230.0000 cr=230.0000 balanced=True
```

### Balance sheet

```bash
curl -s "$BASE/reports/balance-sheet?company_id=$CO&end_date=2026-12-31" -H "$AUTH"
```

```json
{
  "total_assets": "115.0000",
  "total_liabilities": "15.0000",
  "total_equity": "100.0000",
  "retained_earnings_current_period": "100.0000",
  "equation_balanced": true,
  "difference": "0.0000"
}
```

Current-period earnings are folded into equity so the equation holds before a
formal year-end close exists.

### Profit and loss

```bash
curl -s "$BASE/reports/profit-and-loss?company_id=$CO&start_date=2026-01-01&end_date=2026-12-31" -H "$AUTH"
```

### General ledger (one account, running balance)

```bash
curl -s "$BASE/reports/general-ledger?company_id=$CO&account_id=$ACC&start_date=2026-01-01&end_date=2026-12-31" -H "$AUTH"
```

### Ledger projection, with an integrity flag

```bash
curl -s "$BASE/reports/account-balances?company_id=$CO&period_id=$PE" -H "$AUTH"
```

Returns `"projection_matches_journals": true` when `ledger_balances` agrees
with the journals. If it is ever `false`, run
`python -m scripts.rebuild_ledger`.

### CSV export

Any report takes `format=csv`:

```bash
curl -s "$BASE/reports/trial-balance?company_id=$CO&period_id=$PE&format=csv" -H "$AUTH"
```

```csv
account_number,account_name,account_type,currency_code,opening_balance,total_debits,total_credits,closing_balance
101,Bank Account,ASSET,USD,0.0000,115.0000,0.0000,115.0000
205,Sales Tax Payable,LIABILITY,USD,0.0000,0.0000,15.0000,15.0000
402,SaaS Revenue,REVENUE,USD,0.0000,0.0000,100.0000,100.0000
TOTAL,,,,,115.0000,115.0000,
```

---

## 9. Fiscal periods

```bash
# close March
curl -s -X POST "$BASE/fiscal-periods/$PE/close?company_id=$CO" -H "$AUTH"
```

```json
{"period_name": "March 2026", "status": "CLOSED", "is_closed": true,
 "closed_at": "2026-09-02T08:41:02Z", "closed_by": "01a06145-..."}
```

Now posting into it is refused (`409`):

```json
{"error": "FISCAL_PERIOD_CLOSED",
 "message": "Fiscal period March 2026 is CLOSED and cannot accept postings.",
 "details": {"period_id": "01a06145-...", "period_name": "March 2026", "status": "CLOSED"}}
```

Other transitions:

```bash
curl -s -X POST "$BASE/fiscal-periods/$PE/adjusting?company_id=$CO" -H "$AUTH"  # accepts postings
curl -s -X POST "$BASE/fiscal-periods/$PE/lock?company_id=$CO"      -H "$AUTH"  # terminal
curl -s -X POST "$BASE/fiscal-periods/$PE/reopen?company_id=$CO"    -H "$AUTH"  # needs period:reopen
```

An accountant closing a period then trying to reopen it gets `403`:

```json
{"error": "PERMISSION_DENIED",
 "message": "This action requires the 'period:reopen' permission.",
 "details": {"required_permission": "period:reopen"}}
```

Generate a whole year of monthly periods:

```bash
curl -s $BASE/fiscal-periods/generate-year -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"company_id\": \"$CO\", \"fiscal_year\": 2027}"
```

---

## 10. Configuration

```bash
# currencies
curl -s $BASE/currencies -H "$AUTH"
curl -s $BASE/currencies -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"currency_code":"GBP","currency_name":"Pound Sterling","decimal_places":2}'

# accounts
curl -s "$BASE/accounts?company_id=$CO&account_type=REVENUE" -H "$AUTH"
curl -s $BASE/accounts -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"company_id\":\"$CO\",\"account_number\":\"410\",\"account_name\":\"Consulting Revenue\",\"account_type\":\"REVENUE\"}"

# accounts are deactivated, never deleted
curl -s -X POST "$BASE/accounts/$ACC/deactivate?company_id=$CO" -H "$AUTH"

# tax codes
curl -s $BASE/tax-codes -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"company_id\":\"$CO\",\"tax_code\":\"VAT20\",\"tax_name\":\"VAT 20%\",\"tax_rate\":\"20.00\",\"tax_account_id\":\"$TAXACC\",\"effective_from\":\"2026-01-01\"}"
```

---

## 11. Pagination

Every collection supports offset (`page`, `page_size`) and cursor (`cursor`)
pagination:

```bash
curl -s "$BASE/journal-entries?company_id=$CO&page_size=2" -H "$AUTH"
```

```json
{
  "items": [...],
  "total": 5,
  "page": 1,
  "page_size": 2,
  "next_cursor": "AaBhRdu7cB2U79Vjhcac0w",
  "has_more": true
}
```

Follow `next_cursor` for stable iteration under concurrent inserts:

```bash
curl -s "$BASE/journal-entries?company_id=$CO&page_size=2&cursor=AaBhRdu7cB2U79Vjhcac0w" -H "$AUTH"
```

`page_size` is capped at 200; asking for more is a 422.

---

## 12. Operational endpoints

```bash
curl -s http://localhost:8000/livez     # {"status":"alive"}
curl -s http://localhost:8000/readyz    # database + redis check
curl -s http://localhost:8000/metrics   # Prometheus
```

```json
{
  "status": "ready",
  "environment": "local",
  "checks": {
    "database": {"status": "ok", "required": true},
    "redis":    {"status": "ok", "required": false}
  }
}
```

Accounting metrics:

```text
journal_entries_posted_total{company_id="01a0...",source_type="sales_invoice"} 2.0
journal_posting_failures_total{error_code="UNBALANCED_JOURNAL_ENTRY"} 1.0
journal_posting_failures_total{error_code="DUPLICATE_IDEMPOTENCY_KEY"} 1.0
journal_posting_failures_total{error_code="FISCAL_PERIOD_CLOSED"} 1.0
```

Every response carries `X-Request-ID` and `X-Correlation-ID`. Send your own
`X-Correlation-ID` to trace a transaction across ERP modules — it is propagated
into every log line emitted while handling the request.

---

## 13. Posting from another ERP module (in-process)

The HTTP API is one caller. A module in the same deployment uses the command
directly, with no HTTP hop and the same guarantees:

```python
from app.application.journal.commands import (
    ActorContext, CreateJournalCommand, JournalLineCommand,
)
from app.application.journal.posting_service import JournalPostingService

# An education consultancy recording a tuition instalment.
command = CreateJournalCommand(
    company_id=company_id,
    entry_date=payment.received_on,
    currency_code="USD",
    source_type="student_fee",
    source_document=f"ENR-{enrolment.reference}",
    reference=f"Tuition instalment {instalment.number} — {student.name}",
    idempotency_key=f"student_fee:{instalment.id}",
    actor=ActorContext(user_id=user_id),
    lines=[
        JournalLineCommand.of(1, debit="1200.00", account_number="101"),   # Bank
        JournalLineCommand.of(2, credit="1200.00", account_number="410"),  # Service revenue
    ],
)
entry = await JournalPostingService(uow).create(command)
```

The ledger neither knows nor cares that this was tuition. `source_type` and
`source_document` record where it came from; the double entry records what it
did.
