# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

Financial reports for Nevarro LLC, a single-member Colorado LLC owned by Sumner Evans. Three report types:

- `by-month/YYYY-MM.md` — monthly reports (equity, revenue events, expenditures)
- `quarterly-reports/YYYY-QN.md` — quarterly summaries (equity, runway)
- `annual-reports/YYYY.md` — annual summaries (revenue, expenses, equity, plans)

## Accounting Method

Two eras coexist in this repo:

- **2023–2025**: `by-month/` and `quarterly-reports/` for these years are
  hand-maintained Markdown, summed with `bin/sum-amount.py` (below).
- **2026 onward**: `by-month/` and `quarterly-reports/` are generated from an
  [hledger](https://hledger.org) plaintext-accounting ledger by
  `bin/generate-report.py`. Don't hand-edit the generated sections of these
  files — edit the ledger and regenerate instead. Annual reports
  (`annual-reports/`) stay hand-written in both eras.

### Ledger layout

- `ledger/all.journal` — entry point (`include`s each year's journal).
- `ledger/2026.journal` — 2026 transactions, `account` declarations, the
  `equity:opening-balances` entry, and periodic budget transactions
  (`~ monthly`, `~ yearly from ...`) used for budget-vs-actual reporting.
- `csv/<source>-YYYY.csv` — raw bank/card exports (Bluevine checking, Chase
  credit card, Fidelity/SPAXX), one file per source per year.
- `rules/<source>.csv.rules` — hledger CSV import rules mapping each
  source's columns and description patterns to ledger accounts.

Key accounts: `assets:bluevine`, `assets:fidelity`, `liabilities:chase-cc`,
`equity:opening-balances`, `equity:member-capital` (owner capital
contributions), `equity:transfers` (clearing account for CC payments — both
the paying account's CSV and Chase's CSV record their half of the same
transfer independently, so each posts against this account; it should net to
$0.00 once both sides are imported), `income:*`, `expenses:*`.

### Workflow

Import new transactions from a freshly downloaded CSV:

```bash
hledger import csv/<source>-2026.csv --rules rules/<source>.csv.rules -f ledger/2026.journal
```

Then regenerate the affected report(s):

```bash
python3 bin/generate-report.py month 2026-07
python3 bin/generate-report.py quarter 2026-Q3
python3 bin/generate-report.py all       # regenerate everything the ledger has data for
```

`generate-report.py` extracts and preserves whatever is already written in a
file's `## Commentary` section before overwriting it, so hand-written prose
(narrative, budget commentary) survives regeneration — only the
ledger-derived sections (Equity, Revenue Events, Expenditures, Runway,
Budget vs. Actual) are recomputed.

### Dev environment

`nix develop` (or `direnv allow`, since `.envrc` has `use flake`) provides
`hledger`, `hledger-web`, and `python3`, and sets `LEDGER_FILE` to
`ledger/all.journal`. `hledger-web` starts a local browser UI for exploring
the ledger.

## Tooling

### `bin/sum-amount.py`

For hand-maintained reports only (2023–2025 by-month/quarterly reports, and
annual reports in any year). Reads Markdown pipe-delimited table rows from
stdin; sums the second-to-last column as a dollar amount.

```bash
# Sum all expenditures in a month report
grep "^|" by-month/2025-01.md | python3 bin/sum-amount.py

# Sum with category breakdown (column 2 = category column, 0-indexed)
grep "^|" file.md | python3 bin/sum-amount.py 2
```

Amount field parsing rules:
- Strips `$`, `,`, leading `-`
- Handles `= $X.XX` suffix (e.g. `30.32€ + $0.95 FTF = $32.86` → uses `$32.86`)
- Leading `-` in field = negative amount
- Categories split on `::` for subcategory grouping (e.g. `Server Costs::Hetzner`)

## Report Conventions

**Expenditure table columns:** `Date | Event | Amount`

**Amount format:** `$X.XX` for positive, `-$X.XX` for negative (credits/redemptions). Foreign currency entries show conversion: `30.32€ + $0.95 FTF = $32.86`.

**Equity table:** two-column layout — Assets on left, Liabilities on right. Equity = Assets − Liabilities.

**Quarterly reports** include a runway calculation: `equity / monthly_burn_rate = months of runway`.

**Annual reports** include expense category breakdown and goals for the next year.
