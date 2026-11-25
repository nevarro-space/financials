# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

Financials for Nevarro LLC, a single-member Colorado LLC owned by Sumner Evans.

- `ledger/` — the [hledger](https://hledger.org) plaintext-accounting ledger.
  This is the source of truth for every number; query it with hledger rather
  than keeping derived reports.
- `annual-reports/YYYY.md` — hand-written annual summaries (revenue, expenses,
  equity, goal review, plans for next year).

There are no monthly or quarterly report files. Period commentary lives in the
ledger (see below); the numbers come from hledger.

## Ledger layout

- `ledger/all.journal` — entry point (`include`s each year's `index.journal`).
- `ledger/YYYY/index.journal` — includes that year's files, plus the
  `Opening Balances` entry on Jan 1 and the `clopen:`-tagged closing entry on
  Dec 31.
- `ledger/YYYY/accounts.journal` — `account` declarations for the year.
- `ledger/YYYY/commodities.journal` — `commodity 1,000.00 USD` (and EUR where
  needed).
- `ledger/YYYY/<source>.journal` — imported transactions, one file per source
  (`bluevine`, `chase-ink-unlimited`, `fidelity`, ...).
- `ledger/YYYY/commentary.journal` — period commentary (see below).
- `ledger/2026/budget.journal` — periodic budget transactions (`~ monthly`,
  `~ yearly from ...`) for `hledger balance --budget`.
- `rules/<source>.csv.rules` — hledger CSV import rules; each includes
  `rules/global.rules` (shared description → account mappings) first.

Key accounts: `assets:cash:bluevine`, `assets:cash:fidelity`,
`liabilities:credit-cards:chase:ink-unlimited`,
`equity:opening/closing balances`, `equity:member:sumner` (owner capital
contributions), `equity:transfers` (clearing account for CC payments — both
the paying account's CSV and Chase's CSV record their half of the same
transfer independently, so each posts against this account; it should net to
$0.00 once both sides are imported), `revenue:*`, `expenses:*`.

Unmatched imports land in `revenue:unknown` (inflows, set in
`rules/global.rules`) or `expenses:unknown` (outflows, hledger's default).
Both are deliberately left undeclared so the `hledger check -s` pre-commit
hook fails until they are re-categorized by hand.

## Commentary

Notes about a single charge go in that transaction's comment
(`2026-01-14 * NAME-CHEAP.COM* CSBRIF  ; mentalmodels.education`).

Notes about a month or quarter go in `ledger/YYYY/commentary.journal` as a
transaction with **no postings**, dated on the last day of the period and
tagged `commentary:`:

```journal
2026-03-31 Q1 2026  ; commentary:
    ; I made a capital contribution of $1,000.00 to fund the business ...
```

With no postings these never affect balances or show up in `bal`/`reg`.
Read them with `hledger print tag:commentary -p 2026q1`. Avoid writing
`word:` anywhere in the text — hledger parses it as a tag (`hledger tags`
should list only `clopen`, `commentary`, and `date`). Query by tag name plus
a period; `tag:commentary=<value>` matching is unreliable here.

## Workflow

Import new transactions from a freshly downloaded CSV:

```bash
hledger import <export>.csv --rules rules/<source>.csv.rules -f ledger/2026/<source>.journal
```

Useful queries in place of the old reports:

```bash
hledger bs -e 2026-10-01                  # equity at a point in time
hledger is -M -p 2026                     # revenue/expenses by month
hledger bal --budget -Q -p 2026           # budget vs. actual by quarter
hledger print tag:commentary              # all period commentary
hledger print tag:commentary -p 2026q3    # commentary for a period
```

Runway is `equity / average monthly burn`; take the two inputs from `bs` and
`is`.

### Hetzner invoices (EUR)

Hetzner bills in EUR. Each invoice is an itemized transaction in
`ledger/YYYY/hetzner.journal` (one posting per server/IP/volume plus tax, in
EUR), converted to USD with a pair of `equity:conversion` postings and
balanced against `liabilities:accounts-payable:hetzner` in USD. The Chase
import rule posts each `Hetzner Online GmbH` card charge to that payable, so
it nets to $0.00 once both sides are in; a nonzero balance means a missing
invoice or charge. The foreign-transaction fee stays its own transaction.

After importing the Chase CSV, generate any new invoices from the PDFs
(emailed by Hetzner, or downloaded from accounts.hetzner.com/invoice):

```bash
bin/hetzner-invoice.py ~/Downloads/Hetzner_*.pdf >> ledger/2026/hetzner.journal
hledger bal liabilities:accounts-payable:hetzner -H   # should be empty (0)
```

The script skips invoices already in the ledger and ones whose card charge
isn't imported yet. Invoices through 2024-03-01 are older hand-written entries
in the 2023 style (conversion on the card charge with `@@`).

### Prices

`ledger/prices/EUR.journal` holds daily ECB EUR/USD rates, fetched by
`bin/pull-prices.sh` (pricehist) and updated nightly by the
`.github/workflows/pull-prices.yml` action. Use them to report EUR amounts in
USD: `hledger bal expenses --value=then,USD` (rate on each posting's date) or
`-X USD` (rate at period end). `equity:conversion` valued in USD is the
difference between what the card actually charged and the ECB rate, i.e. the
card network's FX spread.

## Dev environment

`nix develop` (or `direnv allow`, since `.envrc` has `use flake`) provides
`hledger`, `hledger-web`, `pre-commit`, `pricehist`, `poppler-utils`
(`pdftotext`), and `python3`, and sets `LEDGER_FILE`
to `ledger/all.journal`. `hledger-web` starts a local browser UI for exploring
the ledger.

Pre-commit runs `hledger-fmt` (from `juev/hledger-lsp`, installed by
pre-commit via Go) and `hledger check -s ordereddates`.

## Annual Report Conventions

Annual reports are commentary only: narrative, a review of last year's goals,
and plans/budget targets for next year. Don't add tables of numbers the ledger
can produce (revenue, expenses by category, equity); get those from hledger
instead:

```bash
hledger is -p 2025 --flat                                   # revenue/expenses for the year
hledger -f ledger/2025/index.journal bs not:tag:clopen      # equity at year end
```

Query a single year's `index.journal` for year-end equity: `not:tag:clopen`
drops that year's Dec 31 closing entry, and using only that year's file avoids
double-counting earlier years (whose closing entries would also be excluded).
Hetzner charges in 2023–2024 are recorded in EUR (invoices are EUR; the USD
cost is on the `@@` price when the payable is settled), so those years' Hetzner
expense totals show in EUR.
