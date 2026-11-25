#!/usr/bin/env python3
"""Turn SendGrid (Twilio) invoice PDFs into itemized hledger transactions.

Usage:
    bin/sendgrid-invoice.py ~/Downloads/INV*_32104293_*.pdf

Each invoice becomes one transaction dated on the invoice, with a posting per
nonzero charge (plan or overage) under expenses:communication:sendgrid:<plan>
and one for sales tax if any, against liabilities:accounts-payable:sendgrid.
The Chase import rule posts the `TWILIO SENDGRID` card charge to that payable,
so it nets to zero.

Invoices already in the ledger (matched by invoice number) are skipped and
reported on stderr.

Requires `pdftotext` (poppler-utils) and `hledger` on PATH.
"""

import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_FILE = REPO_ROOT / "ledger" / "all.journal"

AP = "liabilities:accounts-payable:sendgrid"
EXPENSE = "expenses:communication:sendgrid"
TAX = "expenses:taxes:sales"

# Charge name prefix -> subaccount of EXPENSE.
PLANS = [
    ("Marketing Campaigns", "marketing-campaigns"),
    ("Essentials", "essentials"),
]

MONEY = r"\$([\d,]+\.\d\d)"


@dataclass
class Charge:
    name: str
    period: str
    amount: Decimal
    tax: Decimal


@dataclass
class Invoice:
    path: Path
    number: str
    date: date
    charges: list[Charge] = field(default_factory=list)
    total: Decimal = Decimal(0)


def money(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def pdf_text(path: Path, *opts: str) -> str:
    return subprocess.run(
        ["pdftotext", *opts, str(path), "-"], capture_output=True, text=True, check=True
    ).stdout


def parse(path: Path) -> Invoice:
    layout = pdf_text(path, "-layout")
    num = re.search(r"Invoice Number:\s*(INV\d+)", layout)
    d = re.search(r"Invoice Date:\s*(\d\d-\w{3}-\d{4})", layout)
    total = re.search(rf"Total Including Taxes in USD\s+{MONEY}", layout)
    if not (num and d and total):
        raise ValueError(f"{path}: not a SendGrid invoice (missing number, date or total)")
    inv = Invoice(path, num[1], datetime.strptime(d[1], "%d-%b-%Y").date(), total=money(total[1]))

    # In -raw mode each charge row is: name lines, description lines, a
    # "DD-Mon-YYYY -" / "DD-Mon-YYYY" service period, then
    # "[quantity] $subtotal $tax $total" (quantity was added in 2026-11).
    raw = pdf_text(path, "-raw")
    section = raw[raw.index("CHARGE SUMMARY"):raw.index("TAX SUMMARY")]
    lines = section.splitlines()[2:]  # skip the title and column headers
    row: list[str] = []
    for line in lines:
        m = re.fullmatch(rf"(?:\d+ )?{MONEY} {MONEY} {MONEY}", line.strip())
        if not m:
            row.append(line.strip())
            continue
        dates = [i for i, l in enumerate(row) if re.match(r"\d\d-\w{3}-\d{4}", l)]
        desc_start = next(
            (i for i, l in enumerate(row) if re.match(r"(Includes |\S.* includes \d)", l)), dates[0]
        )
        name = " ".join(row[:desc_start])
        period = " ".join(row[dates[0]:]) if dates else ""
        inv.charges.append(Charge(name, period, money(m[1]), money(m[2])))
        row = []

    if sum(c.amount + c.tax for c in inv.charges) != inv.total:
        raise ValueError(f"{path}: charges {sum(c.amount + c.tax for c in inv.charges)} != total {inv.total}")
    return inv


def account(name: str) -> str:
    for prefix, sub in PLANS:
        if name.startswith(prefix):
            return f"{EXPENSE}:{sub}"
    raise ValueError(f"unknown SendGrid charge {name!r}; add it to PLANS")


def render(inv: Invoice) -> str:
    lines = [f"{inv.date} * SendGrid invoice {inv.number}"]
    for c in inv.charges:
        if c.amount:
            lines.append(f"    {account(c.name)}  {c.amount:.2f} USD  ; {c.name}, {c.period}")
    tax = sum(c.tax for c in inv.charges)
    if tax:
        lines.append(f"    {TAX}  {tax:.2f} USD  ; SendGrid")
    lines.append(f"    {AP}  {-inv.total:.2f} USD")
    return "\n".join(lines) + "\n"


def invoices_in_ledger() -> set[str]:
    out = subprocess.run(
        ["hledger", "-f", str(LEDGER_FILE), "print", "desc:^SendGrid invoice "],
        capture_output=True, text=True, check=True,
    ).stdout
    return set(re.findall(r"SendGrid invoice (INV\d+)", out))


@dataclass
class Result:
    invoice: Invoice
    status: str  # "added" or "exists"
    entry: str = ""


def generate(paths: list[Path]) -> list[Result]:
    seen = invoices_in_ledger()
    results = []
    for inv in sorted((parse(p) for p in paths), key=lambda i: (i.date, i.number)):
        if inv.number in seen:
            results.append(Result(inv, "exists"))
        else:
            seen.add(inv.number)
            results.append(Result(inv, "added", render(inv)))
    return results


def main() -> None:
    results = generate([Path(p) for p in sys.argv[1:]])
    for r in results:
        if r.status == "exists":
            print(f"skip {r.invoice.number}: already in ledger", file=sys.stderr)
    sys.stdout.write("\n".join(r.entry for r in results if r.status == "added"))


if __name__ == "__main__":
    main()
