# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

Financial reports for Nevarro LLC, a single-member Colorado LLC owned by Sumner Evans. Three report types:

- `by-month/YYYY-MM.md` — monthly reports (equity, revenue events, expenditures)
- `quarterly-reports/YYYY-QN.md` — quarterly summaries (equity, runway)
- `annual-reports/YYYY.md` — annual summaries (revenue, expenses, equity, plans)

## Tooling

### `bin/sum-amount.py`

Reads Markdown pipe-delimited table rows from stdin; sums the second-to-last column as a dollar amount.

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
