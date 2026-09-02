# Multi-currency

## What V1 does

**V1 implements transaction-currency accounting.** A journal entry is recorded
in one currency; its lines are stored and reported in that currency; balances
are held per `(company, account, period, currency)`.

That is genuinely useful on its own: a company can post USD, EUR and GBP
transactions, and each currency's trial balance ties independently.

## What V1 does not do

> **Foreign exchange revaluation and translation are not implemented.**

Concretely, the following are **out of scope for V1**:

* converting a transaction into the company's base currency at post time;
* storing an exchange rate on a journal line;
* period-end revaluation of foreign-currency monetary balances, and the
  realised/unrealised FX gain or loss entries that produces;
* consolidating multiple currencies into one base-currency financial statement.

If you run a trial balance across a company that has posted in more than one
currency, you get one row per `(account, currency)`. The report does **not**
sum them, because summing 100 USD and 100 EUR into "200" would be wrong, and
producing a meaningful total requires exchange rates the system does not yet
hold. This is a deliberate refusal, not an oversight: silently adding
incompatible currencies is a worse failure than declining to.

`companies.base_currency_code` is stored today and used for reference. It does
not yet drive any conversion.

---

## What the architecture already provides

The point of documenting the limit precisely is that the design must not make
lifting it painful. It does not:

**1. Currency is a first-class dimension, not an afterthought.**
`ledger_balances` is keyed on `(company_id, account_id, period_id,
currency_code)`. Adding base-currency columns does not change that key or
require re-keying a single row.

**2. Amounts already have room.** Every monetary column is `NUMERIC(19,4)`, and
`Decimal` is used end to end. Adding `base_debit` / `base_credit` alongside
`debit` / `credit` is an additive migration.

**3. Accounts can be pinned to a currency.** `chart_of_accounts.currency_code`
is nullable; when set, `rules.validate_account_currency` rejects postings in
any other currency. That is how you model a currency-specific bank account
today, and it is the same hook FX revaluation will use to find monetary
balances to revalue.

**4. Rounding is centralised.** All rounding goes through
`app/core/money.py` with a configurable mode. FX conversion introduces more
rounding decisions; they will be made in one place, not scattered.

**5. Validation is per line, already currency-aware.**
`rules.validate_money_precision` takes the currency and checks its
`decimal_places` — JPY (0) and USD (2) are already handled differently. Rate
precision will slot in beside it.

**6. Reports group by currency already.** `ReportRepository.account_activity`
returns `(account, currency)` rows. Base-currency reporting becomes an
additional aggregation, not a rewrite.

---

## The V2 shape

The intended target, kept here so the V1 schema can be checked against it.

### Journal lines gain base-currency columns

```text
journal_items
  ...
  currency_code            CHAR(3)          -- transaction currency (exists)
  debit                    NUMERIC(19,4)    -- transaction amount (exists)
  credit                   NUMERIC(19,4)    -- transaction amount (exists)

  base_currency_code       CHAR(3)          -- new: the company's base currency
  base_debit               NUMERIC(19,4)    -- new
  base_credit              NUMERIC(19,4)    -- new
  exchange_rate            NUMERIC(19,8)    -- new: transaction → base
  exchange_rate_date       DATE             -- new: which day's rate was used
```

Two invariants would then apply to a posted entry:

```text
SUM(debit)      == SUM(credit)          -- transaction currency (today's Rule 1)
SUM(base_debit) == SUM(base_credit)     -- base currency
```

The second does **not** follow from the first. Rounding each line's conversion
independently can leave the base-currency totals a cent apart, so posting will
need to place a rounding difference on a configured account. That is exactly
the kind of decision that belongs in `JournalPostingService`, behind the same
single door, rather than in each calling module.

### Exchange rates

```text
exchange_rates
  id                UUID
  from_currency     CHAR(3)
  to_currency       CHAR(3)
  rate_date         DATE
  rate              NUMERIC(19,8)
  rate_type         SPOT | AVERAGE | CLOSING
  source            e.g. 'ECB', 'manual'
  UNIQUE (from_currency, to_currency, rate_date, rate_type)
```

Rate lookup would be a repository with an explicit "no rate for this date"
error — never a silent fallback to 1.0, and never an implicit interpolation.

### Ledger balances gain a base column set

```text
ledger_balances
  ...                                       -- transaction-currency columns (exist)
  base_currency_code       CHAR(3)          -- new
  base_opening_balance     NUMERIC(19,4)    -- new
  base_total_debits        NUMERIC(19,4)    -- new
  base_total_credits       NUMERIC(19,4)    -- new
  base_closing_balance     NUMERIC(19,4)    -- new
```

The upsert already increments columns atomically; the base columns increment in
the same statement. `rebuild_ledger` recomputes them from the stored per-line
base amounts — which is why the rate must be stored on the line rather than
looked up at rebuild time. A rebuild has to reproduce history exactly, and a
rate table that has since been corrected would otherwise change the past.

### Revaluation

A period-end job would:

1. select monetary accounts (`is_monetary`, a new flag on the chart) with a
   non-base-currency balance;
2. value each at the period-end closing rate;
3. compute the difference against the stored base-currency balance;
4. post an ordinary journal entry — through `JournalPostingService`, like
   everything else — debiting or crediting an FX gain/loss account, with
   `source_type = "fx_revaluation"`.

Revaluation produces normal double entries. It needs no special ledger path,
which is the whole reason the posting service is the only door.

---

## Migration path

Adding the above is additive:

1. new nullable columns on `journal_items` and `ledger_balances`;
2. a new `exchange_rates` table;
3. backfill: for existing rows, base == transaction (V1 entries were
   single-currency), rate `1.0`, `exchange_rate_date = entry_date`;
4. make the columns `NOT NULL`;
5. extend `JournalPostingService` to resolve a rate and populate them;
6. extend the reports with a base-currency aggregation.

No existing column changes type, no primary key changes, no posted row is
rewritten with different accounting meaning. That property — that lifting the
V1 limitation never rewrites history — is what "the architecture must not make
future multi-currency support difficult" actually has to mean.

---

## Working within V1

* **One currency per entry.** `rules.validate_line_currency` rejects a line
  whose currency differs from the header's. Two currencies in one entry cannot
  balance without a rate, so this is enforced rather than left to chance.
* **Pin accounts that are inherently single-currency.** Set
  `currency_code` on a EUR bank account and mis-posting to it becomes
  `CURRENCY_MISMATCH`.
* **Filter reports by currency.** Pass `currency_code=USD` to get a single-
  currency view that ties on its own.
* **Deactivate rather than delete a currency.** `is_active = false` blocks new
  postings while leaving history readable. Changing `decimal_places` on a
  currency that already has accounting activity is refused outright — it would
  retroactively reinterpret every stored amount.
