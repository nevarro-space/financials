#!/usr/bin/env python3
"""Turn Hetzner invoice PDFs into itemized hledger transactions.

Usage:
    bin/hetzner-invoice.py ~/Downloads/Hetzner_*.pdf >> ledger/2026/hetzner.journal

Each invoice becomes one transaction with a posting per line item and one for
tax (in EUR), balanced against liabilities:accounts-payable:hetzner in USD via
a pair of equity:conversion postings. The USD amount comes from the matching
card charge already in the ledger: the first `Hetzner` posting to the payable,
dated 0-7 days after the invoice, that no other invoice has claimed. The Chase
import rule posts those charges to the payable, so it nets to zero.

Cancellations and the invoices that replace them are settled with account
credit rather than a card charge, so they are booked in EUR against the
payable and cancel each other out.

Invoices already in the ledger (matched by invoice number) are skipped, as are
invoices with no matching card charge yet; both are reported on stderr.

Requires `pdftotext` (poppler-utils) and `hledger` on PATH.
"""

import csv
import io
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_FILE = REPO_ROOT / "ledger" / "all.journal"

AP = "liabilities:accounts-payable:hetzner"
EXPENSE = "expenses:hosting:hetzner"
TAX = "expenses:taxes:vat:hetzner"
ROUNDING = "equity:rounding"
CONVERSION = "equity:conversion"

EUR = r"€\s*(-?[\d,]+\.\d+)"


@dataclass
class Item:
    amount: Decimal
    comment: str


@dataclass
class Invoice:
    path: Path
    kind: str  # "Invoice" or "Cancellation"
    number: str
    date: date
    period: str = ""
    cancels: str = ""
    items: list[Item] = field(default_factory=list)
    tax: Decimal = Decimal(0)
    tax_rate: str = ""
    total: Decimal = Decimal(0)


def eur(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def pdf_text(path: Path) -> str:
    return subprocess.run(
        ["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True
    ).stdout


def parse(path: Path) -> Invoice:
    text = pdf_text(path)
    m = re.search(r"^(Invoice|Cancellation) (\w+)(?: for invoice (\w+))?\s*$", text, re.M)
    if not m:
        raise ValueError(f"{path}: no invoice/cancellation header")
    d = re.search(r"Invoice date:\s*(\d\d/\d\d/\d{4})", text)
    inv = Invoice(
        path=path,
        kind=m[1],
        number=m[2],
        cancels=m[3] or "",
        date=datetime.strptime(d[1], "%d/%m/%Y").date(),
    )

    # Tax summary: the first line after the "Tax code ... Tax rate" header
    # that has a rate. Its last two amounts are always tax and gross; the
    # columns before them vary by layout (net, and from 2026-11 also taxable).
    summary = text[text.index("Tax code"):] if "Tax code" in text else ""
    tm = re.search(r"^.*?([\d.]+\s?%)(.*)$", summary, re.M)
    if not tm:
        raise ValueError(f"{path}: no tax summary")
    amounts = re.findall(EUR, tm[2])
    if len(amounts) < 3:
        raise ValueError(f"{path}: unexpected tax summary line: {tm[0].strip()}")
    inv.tax_rate = tm[1].replace(" ", "")
    inv.tax = eur(amounts[-2])
    due = re.search(rf"Amount due:\s*{EUR}", text)
    inv.total = eur(due[1]) if due else eur(amounts[-1])

    if "Product Description" in text:
        parse_old_items(text, inv)
    else:
        parse_new_items(text, inv)
    if not inv.items:
        raise ValueError(f"{path}: no line items")
    return inv


def parse_new_items(text: str, inv: Invoice) -> None:
    """Layouts used from 2024-10:

    2024-10 to 2026-10: Pos | count Product | Unit | Quantity | Unit price | Price
    2026-11 onward:     Pos | Nx Product | Unit | Quantity | Unit price | Net | Tax | Gross
                        (tax-code markers like "O1" may be glued onto amounts)
    """
    period = re.search(r'^\s*Project "[^"]*"\s+\(?(\d\d/\d{4})\)?', text, re.M)
    inv.period = period[1] if period else ""
    row = re.compile(
        rf"^\s+\d+\s+(\d+)\s+(.+?)\s{{2,}}(\S+)\s+([\d.]+)\s+{EUR}\s+{EUR}\s*$", re.M
    )
    rows = row.findall(text)
    if not rows:
        per_line_tax = re.compile(
            rf"^\s+\d+\s+(\d+)x\s+(.+?)\s{{2,}}(\S+)\s+([\d.]+)\s+{EUR}\s+{EUR}"
            rf"\s+{EUR}(?:O\d)?\s+{EUR}(?:O\d)?\s*$",
            re.M,
        )
        rows = [r[:6] for r in per_line_tax.findall(text)]
    for count, product, unit, qty, unit_price, price in rows:
        amount = eur(price)
        if amount == 0:
            continue  # e.g. included traffic
        unit = unit.replace("Months", "mo").replace("GB-months", "GB-mo")
        comment = f"{count} {product.strip()}, {qty} {unit} @ €{unit_price}"
        inv.items.append(Item(amount, comment))


def parse_old_items(text: str, inv: Invoice) -> None:
    """Layout used through 2024-09. The tax-code footnote digit is glued onto
    each price (€ 1.50001 = €1.5000, tax code 1)."""
    period = re.search(r"\((\d\d/\d\d/\d{4}) - (\d\d/\d\d/\d{4})\)", text)
    if period:
        inv.period = datetime.strptime(period[1], "%d/%m/%Y").strftime("%m/%Y")
    row = re.compile(r"^\s+\d+\s+(.+?)\s{2,}([\d.]+)\s+€\s*([\d.]+)\s+€\s*(\d+\.\d{4})\d\s*$", re.M)
    for desc, qty, unit_price, price in row.findall(text):
        amount = eur(price)
        if amount == 0:
            continue
        comment = f"{' '.join(desc.split())}, {qty} @ €{unit_price}"
        inv.items.append(Item(amount, comment))


def hledger_csv(*args: str) -> list[dict]:
    out = subprocess.run(
        ["hledger", "-f", str(LEDGER_FILE), *args, "-O", "csv"],
        capture_output=True, text=True, check=True,
    ).stdout
    return list(csv.DictReader(io.StringIO(out)))


def existing_invoice_numbers() -> set[str]:
    out = subprocess.run(
        ["hledger", "-f", str(LEDGER_FILE), "print", f"acct:^{AP}$"],
        capture_output=True, text=True, check=True,
    ).stdout
    return set(re.findall(r"Hetzner (?:Invoice|Cancellation) (\w+)", out))


def card_charges() -> list[tuple[date, Decimal]]:
    """USD postings to the payable from card charges (description Hetzner)."""
    rows = hledger_csv("register", f"acct:^{AP}$", "cur:USD", "desc:hetzner")
    charges = []
    for r in rows:
        amt = Decimal(r["amount"].replace("USD", "").replace(",", "").strip())
        if amt > 0 and not r["description"].startswith("Hetzner Invoice"):
            charges.append((date.fromisoformat(r["date"]), amt))
    return charges


def claimed_charges() -> list[tuple[date, Decimal]]:
    """USD amounts already used by invoice transactions in the ledger."""
    rows = hledger_csv("register", f"acct:^{AP}$", "cur:USD", "desc:^Hetzner Invoice")
    return [
        (date.fromisoformat(r["date"]), -Decimal(r["amount"].replace("USD", "").replace(",", "").strip()))
        for r in rows
    ]


def fmt(amount: Decimal, places: int = 2) -> str:
    q = Decimal(1).scaleb(-max(places, -amount.as_tuple().exponent))
    return f"{amount.quantize(q):,}"


def render(inv: Invoice, usd: Decimal | None) -> str:
    sign = -1 if inv.kind == "Cancellation" else 1
    title = f"Hetzner {inv.kind} {inv.number}"
    if inv.cancels:
        title += f" for invoice {inv.cancels}"
    lines = [f"{inv.date} * {title}  ; period {inv.period}" if inv.period else f"{inv.date} * {title}"]
    for it in inv.items:
        lines.append(f"    {EXPENSE}  {fmt(sign * it.amount)} EUR  ; {it.comment}")
    lines.append(f"    {TAX}  {fmt(sign * inv.tax)} EUR  ; {inv.tax_rate}")
    rounding = inv.total - (sum(i.amount for i in inv.items) + inv.tax)
    if rounding:
        lines.append(f"    {ROUNDING}  {fmt(sign * rounding)} EUR")
    if usd is None:
        lines.append(f"    {AP}  {fmt(-sign * inv.total)} EUR")
    else:
        lines.append(f"    {CONVERSION}  {fmt(-inv.total)} EUR")
        lines.append(f"    {CONVERSION}  {fmt(usd)} USD")
        lines.append(f"    {AP}  {fmt(-usd)} USD")
    return "\n".join(lines) + "\n"


def main() -> None:
    invoices = sorted((parse(Path(p)) for p in sys.argv[1:]), key=lambda i: (i.date, i.number))
    by_number = {i.number: i for i in invoices}
    cancelled = {i.cancels for i in invoices if i.kind == "Cancellation"}
    # A replacement is an invoice for the same period as a cancelled one,
    # issued after it, and settled by the cancellation's credit.
    cancelled_periods = {by_number[n].period for n in cancelled if n in by_number}
    seen = existing_invoice_numbers()

    available = card_charges()
    for c in claimed_charges():
        if c in available:
            available.remove(c)

    out = []
    for inv in invoices:
        if inv.number in seen:
            print(f"skip {inv.number}: already in ledger", file=sys.stderr)
            continue
        credit_settled = inv.kind == "Cancellation" or (
            inv.period in cancelled_periods and inv.number not in cancelled
        )
        usd = None
        if not credit_settled:
            window = [c for c in available if inv.date <= c[0] <= inv.date + timedelta(days=7)]
            if not window:
                print(f"skip {inv.number} ({inv.date}): no matching card charge yet", file=sys.stderr)
                continue
            charge = min(window)
            available.remove(charge)
            usd = charge[1]
        out.append(render(inv, usd))
    sys.stdout.write("\n".join(out))


if __name__ == "__main__":
    main()
