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
hledger bal --budget -Q -p 2026 expenses  # budget vs. actual by quarter
hledger print tag:commentary              # all period commentary
hledger print tag:commentary -p 2026q3    # commentary for a period
```

Runway is `equity / average monthly burn`; take the two inputs from `bs` and
`is`.

### Hetzner invoices (EUR)

Hetzner bills in EUR. Each document gets two transactions in
`ledger/YYYY/hetzner.journal`:

1. **Invoice**, dated on the invoice and booked as soon as it arrives: one
   posting per server/IP/volume plus tax, in EUR, against
   `liabilities:accounts-payable:hetzner`. Until it's paid, the payable shows
   the EUR owed.
2. **Payment**, dated on the card charge (`Hetzner payment for invoice N`):
   clears the EUR from the payable and converts it to the USD actually
   charged with a pair of `equity:conversion` postings. The Chase import rule
   posts each `Hetzner Online GmbH` charge to the payable in USD, which this
   offsets.

So `hledger bal liabilities:accounts-payable:hetzner -H` is EUR for unpaid
invoices, USD for charges with no invoice, and empty when everything matches.
The foreign-transaction fee stays its own transaction. Cancellations and their
replacement invoices are settled by Hetzner account credit, so they have no
payment and offset each other in EUR.

`bin/process-mail.py` (below) does both steps. To work from PDFs directly
(e.g. downloaded from accounts.hetzner.com/invoice):

```bash
bin/hetzner-invoice.py ~/Downloads/Hetzner_*.pdf >> ledger/2026/hetzner.journal
bin/hetzner-invoice.py --payments   # after the Chase import; insert in date order
```

Invoices through 2024-03-01 are older hand-written entries in the 2023 style
(conversion on the card charge with `@@`).

### GitHub Sponsors receipts

Nevarro sponsors maintainers from the `nevarro-space` GitHub org (arp242 $5/mo
since 2024-02-28, neovim $10/mo since 2025-03-03). GitHub adds a 6% fee for
org sponsorships and bills them together as one `GITHUB` card charge. Each
receipt is a transaction in `ledger/YYYY/github.journal` with a posting per
sponsorship (`expenses:donations:github:<login>`) and per fee
(`expenses:fees:github-sponsors`) against `liabilities:accounts-payable:github`;
the Chase import rule posts the card charge to that payable.

`bin/github-receipt.py` parses the receipt PDFs (`process-mail.py` runs it
on the "[GitHub] Payment Receipt for nevarro-space" emails). Charges before
2026-08 have no receipt; those entries are tagged `reconstructed:` and split
from the card amount using the sponsorship tiers. GitHub's GraphQL
`totalSponsorshipAmountAsSponsorInCents` does not give per-period amounts that
match the charges, so don't use it as a source.

### SendGrid invoices

SendGrid (Twilio) invoices are dated the 1st of each month and charged a few
days later as `TWILIO SENDGRID`. Each invoice is a transaction in
`ledger/YYYY/sendgrid.journal` with a posting per plan
(`expenses:communication:sendgrid:essentials` for Essentials 50K,
`...:marketing-campaigns` for Marketing Campaigns Basic 5K since 2026-01) plus
any overage or sales tax (`expenses:taxes:sales:sendgrid`), against
`liabilities:accounts-payable:sendgrid`; the Chase import rule posts the card
charge to that payable. `bin/sendgrid-invoice.py` parses the invoice PDFs
(`process-mail.py` runs it on "You have a new invoice from SendGrid" emails).
Charges since 2024 without an invoice in the mailbox are tagged
`reconstructed:` and split by plan price. SendGrid announced in 2026-11 that
it's merging into Twilio, so the sender or invoice format may change.

### Namecheap orders

Namecheap has no invoice export or order-history API; the itemized order is
only in the HTML part of the "Namecheap Order Summary (Order# N)" email
(re-sendable from Profile → Billing → Order History → Resend receipt). Each
order is a transaction in `ledger/YYYY/namecheap.journal` with, per domain, the
registration/renewal/transfer price and its ICANN fee under
`expenses:domains:namecheap:<domain>`, against
`liabilities:accounts-payable:namecheap`; the Chase import rule posts the
`NAME-CHEAP.COM*` charge to that payable. `bin/namecheap-order.py` parses saved
`.eml` files; `process-mail.py` handles the emails. Any USD balance on the
payable is a card charge whose receipt hasn't been booked yet. Itemized from 2026-06 on; earlier charges stay as single
`expenses:domains:namecheap` postings (domains noted in the comment).

### Google Workspace invoices

Google bills Workspace monthly in arrears: the invoice is dated the last day
of the month and charged on the 1st as `GOOGLE *Workspace_nevarro`. Each
invoice is a transaction in `ledger/YYYY/google.journal` with the subscription
(`expenses:communication:google-workspace`) and sales tax
(`expenses:taxes:sales:google`) against `liabilities:accounts-payable:google`;
the Chase import rule posts the card charge to that payable.
`bin/google-invoice.py` parses the invoice PDFs (`process-mail.py` runs it on
"Google Workspace: Your invoice is available" emails). June and August 2026
have no invoice and are tagged `reconstructed:`.

### Sales tax

Vendors' US sales tax is booked per vendor under `expenses:taxes:sales:<vendor>`
(Hetzner, Google, and SendGrid if it ever charges any), so
`hledger bal expenses:taxes:sales` is the total paid. Hetzner's is in EUR.

### Invoice mail

`bin/process-mail.py` reads `~/Mail/Nevarro/INBOX` (the mbsync maildir), turns
recognized vendor invoice emails into ledger entries (inserted in date order
into `ledger/YYYY/<vendor>.journal`), runs `hledger check -s ordereddates`, and
moves processed messages to the `Invoices` folder (the Gmail "Invoices" label),
which archives them out of the inbox on the next `mbsync`. If the check fails,
the ledger is restored and nothing is moved.

```bash
mbsync Nevarro
hledger import ...                  # latest Chase CSV first
bin/process-mail.py --dry-run       # preview
bin/process-mail.py
mbsync Nevarro                      # sync the moves
```

Each run also adds payments for any Hetzner invoices whose card charge has
since been imported, even with no new mail. Messages stay in INBOX only when
no handler recognizes them or they fail sender verification: the sender is
taken from `X-Original-Sender` (mail relayed by the admin@nevarro.space group)
or `From`, and must have a DKIM pass for the vendor's domain recorded by Google
or Migadu. Vendors so far: Hetzner, GitHub, SendGrid, Namecheap, Google.
Changed journals are formatted with the repo's `hledger-fmt` pre-commit hook
before the check, so commits don't trip it.

To add a vendor: write `bin/<vendor>-invoice.py` with `parse()` (documents
with `number` and `date`) and `render()`, using `bin/invoicelib.py` for the
shared `generate()`/`main()` plumbing; add a `Vendor` row to `VENDORS` in
`process-mail.py` (senders, DKIM domain, subject prefixes); and a
`RECONCILERS` entry if entries depend on other imports (like Hetzner's
payments). New subaccounts under the prefixes in `AUTO_DECLARE` (per domain,
per GitHub maintainer) are declared in that year's `accounts.journal`
automatically.

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
