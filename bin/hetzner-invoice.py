#!/usr/bin/env python3
"""Turn Hetzner invoice PDFs into itemized hledger transactions.

Usage:
    bin/hetzner-invoice.py ~/Downloads/Hetzner_*.pdf   # print new invoice entries
    bin/hetzner-invoice.py --payments                  # print new payment entries

Each invoice becomes one transaction with a posting per line item and one for
tax, in EUR, against liabilities:accounts-payable:hetzner. It's booked as soon
as the invoice arrives, so the payable shows what's owed in EUR.

When the card charge for an invoice shows up (the Chase import rule posts
`Hetzner` charges to the payable in USD), `--payments` produces a payment
transaction dated on the charge that converts the invoice's EUR to the charged
USD with a pair of equity:conversion postings. After that the payable nets to
zero in both currencies. A charge matches the earliest unpaid invoice dated
0-10 days before it.

Cancellations and the invoices that replace them are settled with account
credit rather than a card charge, so they get no payment; they cancel each
other out in EUR.

Invoices already in the ledger (matched by invoice number) are skipped and
reported on stderr.

Requires `pdftotext` (poppler-utils) and `hledger` on PATH.
"""

import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import invoicelib as lib  # noqa: E402

AP = "liabilities:accounts-payable:hetzner"
EXPENSE = "expenses:hosting:hetzner"
TAX = "expenses:taxes:sales:hetzner"
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



def parse(path: Path) -> Invoice:
    text = lib.pdf_text(path, "-layout")
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


def amount(s: str) -> Decimal:
    return Decimal(re.sub(r"[A-Z, ]", "", s))


def fmt(amount: Decimal, places: int = 2) -> str:
    q = Decimal(1).scaleb(-max(places, -amount.as_tuple().exponent))
    return f"{amount.quantize(q):,}"


def render(inv: Invoice) -> str:
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
    lines.append(f"    {AP}  {fmt(-sign * inv.total)} EUR")
    return "\n".join(lines) + "\n"


def render_payment(number: str, charged: date, eur_total: Decimal, usd: Decimal) -> str:
    return (
        f"{charged} * Hetzner payment for invoice {number}\n"
        f"    {AP}  {fmt(eur_total)} EUR\n"
        f"    {CONVERSION}  {fmt(-eur_total)} EUR\n"
        f"    {CONVERSION}  {fmt(usd)} USD\n"
        f"    {AP}  {fmt(-usd)} USD\n"
    )


@dataclass
class LedgerDoc:
    number: str
    kind: str
    date: date
    period: str
    cancels: str


def ledger_docs() -> dict[str, LedgerDoc]:
    """Hetzner invoices/cancellations already in the ledger, by number."""
    out = lib.hledger("print", "desc:^Hetzner (Invoice|Cancellation) ")
    docs = {}
    for m in re.finditer(
        r"^(\d{4}-\d\d-\d\d) \S* ?Hetzner (Invoice|Cancellation) (\w+)"
        r"(?: for invoice (\w+))?\s*(?:;\s*period (\S+))?",
        out, re.M,
    ):
        docs[m[3]] = LedgerDoc(m[3], m[2], date.fromisoformat(m[1]), m[5] or "", m[4] or "")
    return docs


def credit_settled_invoices(docs: dict[str, LedgerDoc]) -> set[str]:
    """Replacement invoices: same period as a cancelled invoice, settled by
    the cancellation's account credit rather than a card charge."""
    cancelled = {d.cancels for d in docs.values() if d.kind == "Cancellation"}
    periods = {docs[n].period for n in cancelled if n in docs}
    return {
        d.number for d in docs.values()
        if d.kind == "Invoice" and d.period in periods and d.number not in cancelled
    }


def generate(paths: list[Path]) -> list[lib.Result]:
    """Parse the given PDFs and return one Result per document, in date order.
    Only "added" results carry a journal entry."""
    in_ledger = ledger_docs()
    results = []
    for inv in sorted((parse(p) for p in paths), key=lambda i: (i.date, i.number)):
        if inv.number in in_ledger:
            results.append(lib.Result(inv, "exists", f"{inv.kind.lower()} {inv.number}"))
        else:
            results.append(lib.Result(inv, "added", f"{inv.kind.lower()} {inv.number}", render(inv)))
    return results


def payments() -> list[str]:
    """Payment entries for invoices in the ledger whose card charge has been
    imported but not yet matched."""
    docs = ledger_docs()
    skip = credit_settled_invoices(docs)
    paid = {
        m[1] for r in lib.hledger_csv("register", f"acct:^{AP}$", "desc:^Hetzner payment for invoice")
        if (m := re.search(r"invoice (\w+)", r["description"]))
    }
    owed = {}  # invoice number -> EUR total, from the invoice's payable posting
    for r in lib.hledger_csv("register", f"acct:^{AP}$", "cur:EUR", "desc:^Hetzner Invoice "):
        m = re.match(r"Hetzner Invoice (\w+)", r["description"])
        if m:
            owed[m[1]] = -amount(r["amount"])
    unpaid = sorted(
        (d for n, d in docs.items() if d.kind == "Invoice" and n in owed and n not in paid and n not in skip),
        key=lambda d: (d.date, d.number),
    )

    charges = [
        (date.fromisoformat(r["date"]), amount(r["amount"]))
        for r in lib.hledger_csv("register", f"acct:^{AP}$", "cur:USD", "desc:hetzner")
        if not re.match(r"Hetzner (payment|Invoice|Cancellation) ", r["description"])
        and amount(r["amount"]) > 0
    ]
    for r in lib.hledger_csv("register", f"acct:^{AP}$", "cur:USD", "desc:^Hetzner payment for invoice"):
        c = (date.fromisoformat(r["date"]), -amount(r["amount"]))
        if c in charges:
            charges.remove(c)

    out = []
    for d in unpaid:
        window = sorted(c for c in charges if d.date <= c[0] <= d.date + timedelta(days=10))
        if not window:
            continue
        charge = window[0]
        charges.remove(charge)
        out.append(render_payment(d.number, charge[0], owed[d.number], charge[1]))
    return out


def main() -> None:
    if sys.argv[1:] == ["--payments"]:
        sys.stdout.write("\n".join(payments()))
        return
    lib.main(lambda args: generate([Path(a) for a in args]))


if __name__ == "__main__":
    main()
