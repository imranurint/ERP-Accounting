# ERP Financial Core — General Ledger

A production-ready, double-entry **General Ledger** built to be the financial
source of truth for an ERP platform.

Every module that moves money — Sales, CRM, Accounts Receivable, Purchasing,
Accounts Payable, Inventory, Payroll, HR, POS, e-commerce, or a vertical
application like an education consultancy billing tuition or a manufacturer
issuing invoices — submits its transactions here, through one gate:
`JournalPostingService`.

The ledger itself is deliberately domain-agnostic. It does not know what an
invoice, an enrolment or a work order *is*. It knows accounts, debits, credits,
periods and currencies. Modules describe provenance with `source_type` /
`source_document`, and the GL records the double entry. That is what lets the
same engine serve any vertical without change.

```text
Business transaction            e.g. "student paid tuition", "goods received"
        │
        ▼
Accounting command              CreateJournalCommand
        │
        ▼
Journal Posting Service         the only way into the ledger
        │
        ▼
Validated double entry          SUM(debit) == SUM(credit)
        │
        ▼
One atomic database transaction
        ├── journal_entries      ← the authoritative history
        ├── journal_items        ← the authoritative history
        ├── ledger_balances      ← a rebuildable projection
        ├── audit_logs           ← append-only
        └── outbox_events        ← for other modules to consume
        │
        ▼
      COMMIT
```

---

## Guarantees

| Guarantee | How it is enforced |
| --- | --- |
| Every posted entry balances | `rules.validate_balanced` + the posting pipeline |
| A line is debit **or** credit, never both, never neither | Domain rules **and** a `CHECK` constraint |
| Posted entries are immutable | State machine; corrections are made by reversal |
| Posted entries are never deleted | State machine + `PostedEntryCannotBeDeletedError` |
| Nothing posts into a `CLOSED`/`LOCKED` period | `SELECT ... FOR UPDATE` on the period, then a status check |
| No lost ledger updates under concurrency | `INSERT ... ON CONFLICT DO UPDATE`, never read-modify-write |
| Journal numbers are unique and gap-tolerant | One atomic upsert on `document_sequences` |
| Retries never double-post | `Idempotency-Key` + a partial unique index + a payload hash |
| The ledger can always be re-derived | `python -m app.scripts.rebuild_ledger` |
| Money is never a float | `Decimal` in Python, `NUMERIC(19,4)` in PostgreSQL, strings on the wire |

---

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2.0 (async) · asyncpg · PostgreSQL 16 ·
Alembic · Pydantic v2 · Redis · pytest + Testcontainers · Ruff · mypy (strict) ·
Docker · GitHub Actions · structlog · OpenTelemetry · Prometheus · Argon2 · JWT.

Dependencies are managed with [`uv`](https://docs.astral.sh/uv/). `uv.lock` and
`.python-version` are committed, so local development, CI and the Docker image
all resolve to the same Python 3.12 and the same dependency set — `uv sync
--frozen` is what CI and the image build run.

---

## Quick start

### With Docker (nothing else required)

```bash
cp .env.example .env
# Set a real SECRET_KEY:
python -c "import secrets; print(f'SECRET_KEY={secrets.token_urlsafe(64)}')" >> .env

make docker-up          # postgres + redis + migrations + api
docker compose exec api python -m app.scripts.seed
```

Then open <http://localhost:8000/docs> (Swagger) or
<http://localhost:8000/redoc>.

### Locally

```bash
make install            # uv sync + create .env from the example
# Start PostgreSQL 16 and Redis (docker compose up -d postgres redis works)
make migrate            # alembic upgrade head
make seed               # demo company, chart of accounts, 2026 periods, users
make run                # uvicorn with reload on :8000
```

`make seed` prints generated passwords **once**. Set `SEED_*_PASSWORD` in
`.env` to choose your own. Seeding refuses to run when `APP_ENV=production`.

To create the first administrator on a fresh, unseeded database:

```bash
make create-admin email=admin@yourcompany.com
```

---

## Try it: post a customer invoice

```bash
TOKEN=$(curl -s localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"accountant@example.com","password":"<from make seed>"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl -s localhost:8000/api/v1/journal-entries \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: sales_invoice:INV-9923' \
  -d '{
    "company_id": "<company uuid>",
    "entry_date": "2026-03-31",
    "currency_code": "USD",
    "source_type": "sales_invoice",
    "source_document": "INV-9923",
    "reference": "Customer invoice INV-9923",
    "lines": [
      {"line_number": 1, "account_number": "101", "debit": "115.00", "credit": "0.00"},
      {"line_number": 2, "account_number": "402", "debit": "0.00", "credit": "100.00"},
      {"line_number": 3, "account_number": "205", "debit": "0.00", "credit": "15.00", "tax_code": "VAT15"}
    ]
  }'
```

Result: a `POSTED` entry numbered `JE-2026-000001`, debits `115.00`, credits
`115.00`, with `ledger_balances` updated, an audit record written, and a
`JournalPosted` event queued in the outbox — all in one transaction. Replay the
same request with the same `Idempotency-Key` and you get the same entry back,
not a second one.

More examples, including reversal and reporting, are in
[`docs/api-examples.md`](docs/api-examples.md).

---

## Environment

| Variable | Purpose | Example |
| --- | --- | --- |
| `APP_ENV` | `local`, `test`, `staging`, `production` | `local` |
| `DEBUG` | Verbose behaviour in development | `true` |
| `SECRET_KEY` | JWT signing key — **required**, ≥ 32 chars | *(generate one)* |
| `DATABASE_URL` | Async PostgreSQL DSN | `postgresql+asyncpg://user:pw@host:5432/db` |
| `REDIS_URL` | Rate limiting and caching | `redis://localhost:6379/0` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Access token lifetime | `30` |
| `REFRESH_TOKEN_EXPIRE_DAYS` | Refresh token lifetime | `14` |
| `CORS_ORIGINS` | JSON array of allowed origins | `["http://localhost:3000"]` |
| `TRUSTED_HOSTS` | JSON array; `["*"]` disables the check | `["api.example.com"]` |
| `OPENAPI_ENABLED` | Serve `/docs` and `/redoc` | `false` in production |
| `MONEY_ROUNDING` | Rounding mode | `ROUND_HALF_UP` |
| `RATE_LIMIT_*` | Per-client request limits | `300` / `60` |
| `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT` | Tracing | `false` |
| `SENTRY_DSN` | Optional error reporting | *(empty)* |

The full list with safe defaults is in [`.env.example`](.env.example). Secrets
are never hardcoded and never logged.

---

## Database migrations

```bash
make migrate                      # upgrade to head
make revision m="add bank feeds"  # autogenerate from the models
make downgrade                    # roll back one revision
```

The initial migration creates every table, foreign key, check constraint,
unique constraint and index — including the GiST exclusion constraint that
makes overlapping fiscal periods impossible.

---

## Tests

```bash
make test               # everything, with coverage
make test-unit          # pure domain rules, no database
make test-integration   # posting, ledger, concurrency, rebuild
make test-api           # HTTP: auth, validation, idempotency, reports
```

Integration and API tests run against a **real PostgreSQL 16**, started
automatically via Testcontainers (Docker required). To reuse a running
instance instead:

```bash
USE_EXISTING_DATABASE=true TEST_DATABASE_URL=postgresql+asyncpg://... make test
```

SQLite is not an option here: the schema depends on partial unique indexes,
GiST exclusion constraints, `ON CONFLICT DO UPDATE` and `SELECT ... FOR
UPDATE`. Testing accounting guarantees against a database that cannot express
them would prove nothing.

---

## Operations

```bash
make rebuild-ledger     # recompute ledger_balances from the journals, verifying
python -m app.scripts.rebuild_ledger --company-id <uuid> --dry-run
```

`ledger_balances` is a cache. `journal_entries` + `journal_items` are the
truth. The rebuild is idempotent, transactional, and `--verify` fails loudly if
the recomputed figures disagree with what is stored — which is the check you
want in a nightly job.

### Endpoints

| Path | Purpose |
| --- | --- |
| `/livez` | Process liveness — no dependencies touched |
| `/readyz` | Readiness — fails on a database outage, degrades on Redis |
| `/healthz` | Human-facing summary |
| `/metrics` | Prometheus (HTTP + accounting counters) |
| `/docs`, `/redoc` | OpenAPI, when `OPENAPI_ENABLED=true` |

---

## Code quality

```bash
make format      # ruff format + ruff check --fix
make lint        # ruff, no modifications
make typecheck   # mypy (strict)
make precommit   # install and run all hooks
```

CI runs lint, strict mypy, the full test suite against PostgreSQL and Redis
service containers, and a Docker image build. Images publish to GHCR on `main`.

---

## Documentation

| Document | Contents |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | Modular monolith, clean architecture layers, repositories, Unit of Work, transaction boundaries, UUIDv7, ledger projection, ERP module integration |
| [`docs/accounting-rules.md`](docs/accounting-rules.md) | Double entry, debit/credit rules, immutability, period controls, reversal, rebuilding, idempotency, numbering policy |
| [`docs/journal-state-machine.md`](docs/journal-state-machine.md) | `DRAFT` → `POSTED` → `REVERSED`, `VOID`, and why void is not a delete |
| [`docs/multi-currency.md`](docs/multi-currency.md) | What V1 does, what it deliberately does not, and the FX architecture it leaves room for |
| [`docs/api-examples.md`](docs/api-examples.md) | Working `curl` examples for the whole lifecycle |

---

## Scope of V1

Implemented: double-entry posting, drafts, posting, reversal, void, fiscal
period lifecycle, chart of accounts, tax codes, multi-company, transaction-
currency accounting, ledger projection and rebuild, trial balance, general
ledger, account balances, P&L, balance sheet, CSV export, RBAC, JWT auth with
refresh rotation, idempotency, audit logging, transactional outbox,
observability.

**Not implemented in V1**, by design:

* **Foreign exchange revaluation and translation.** V1 records transaction
  currency only. The schema and services are shaped so base-currency columns
  and exchange rates can be added without restructuring — see
  [`docs/multi-currency.md`](docs/multi-currency.md).
* **Year-end closing entries.** The balance sheet folds current-period earnings
  into equity so the accounting equation holds before a formal close is run.
* **An outbox relay process.** Events are written transactionally; publishing
  them to a broker is a separate concern and deliberately outside the
  accounting transaction.
* **Per-user company scoping.** Permissions are global; `company_id` is an
  explicit parameter. Multi-tenant row scoping is an additive change to
  `deps.get_current_user`.
