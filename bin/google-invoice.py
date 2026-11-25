#!/usr/bin/env python3
"""Turn Google Workspace invoice PDFs into itemized hledger transactions.

Usage:
    bin/google-invoice.py ~/Downloads/<invoice number>.pdf ...

Google bills Workspace monthly in arrears: the invoice is dated the last day
of the month and charged on the 1st as `GOOGLE *Workspace_nevarro`. Each
invoice becomes one transaction dated on the invoice, with a posting per
subscription line (expenses:communication:google-workspace) and per sales-tax
line (expenses:taxes:sales:google), against
liabilities:accounts-payable:google. The Chase import rule posts the card
charge to that payable, so it nets to zero.

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

AP = "liabilities:accounts-payable:google"
EXPENSE = "expenses:communication:google-workspace"
TAX = "expenses:taxes:sales:google"


@dataclass
class Line:
    account: str
    amount: Decimal
    comment: str


@dataclass
class Invoice:
    path: Path
    number: str
    date: date
    lines: list[Line] = field(default_factory=list)
    total: Decimal = Decimal(0)


def money(s: str) -> Decimal:
    return Decimal(s.replace(",", "").replace("$", ""))


def pdf_text(path: Path) -> str:
    return subprocess.run(
        ["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True
    ).stdout


def parse(path: Path) -> Invoice:
    text = pdf_text(path)
    num = re.search(r"Invoice number:\s*(\d+)", text)
    # The invoice date sits on the summary's first "Total in USD" line.
    d = re.search(r"(\w{3} \d{1,2}, \d{4})\s+Total in USD", text)
    if not (num and d):
        raise ValueError(f"{path}: not a Google Workspace invoice (missing number or date)")
    inv = Invoice(path, num[1], datetime.strptime(d[1], "%b %d, %Y").date())

    # Detail table (after the "Subscription  Description  Interval ..." header):
    # rows of subscription | description | interval | quantity | amount, then
    # "Subtotal", tax lines and "Total in USD".
    start = re.search(r"^\s*Subscription\s+Description\s+Interval", text, re.M)
    if not start:
        raise ValueError(f"{path}: no detail table")
    for line in text[start.end():].splitlines():
        s = line.strip()
        row = re.fullmatch(
            r"(\S.*?)\s{2,}(\S.*?)\s{2,}(\w{3} \d{1,2} - \w{3} \d{1,2})\s+(\d+)\s+(-?[\d,]+\.\d\d)", s
        )
        tax = re.fullmatch(r"(.*tax.*?)\s{2,}(-?\$[\d,]+\.\d\d)", s, re.I)
        total = re.fullmatch(r"Total in USD\s+(-?\$[\d,]+\.\d\d)", s)
        if row:
            sub, desc, interval, qty, amt = row.groups()
            name = sub.removeprefix("Google Workspace ").strip()
            note = f"{name}, {qty} user{'s' if qty != '1' else ''}, {interval}"
            if desc != "Usage":
                note += f" ({desc})"
            inv.lines.append(Line(EXPENSE, money(amt), note))
        elif tax:
            inv.lines.append(Line(TAX, money(tax[2]), tax[1]))
        elif total:
            inv.total = money(total[1])
            break

    if not inv.lines or sum(l.amount for l in inv.lines) != inv.total:
        raise ValueError(f"{path}: lines {sum(l.amount for l in inv.lines)} != total {inv.total}")
    return inv


def render(inv: Invoice) -> str:
    lines = [f"{inv.date} * Google Workspace invoice {inv.number}"]
    for l in inv.lines:
        lines.append(f"    {l.account}  {l.amount:.2f} USD  ; {l.comment}")
    lines.append(f"    {AP}  {-inv.total:.2f} USD")
    return "\n".join(lines) + "\n"


def invoices_in_ledger() -> set[str]:
    out = subprocess.run(
        ["hledger", "-f", str(LEDGER_FILE), "print", "desc:^Google Workspace invoice "],
        capture_output=True, text=True, check=True,
    ).stdout
    return set(re.findall(r"Google Workspace invoice (\d+)", out))


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
