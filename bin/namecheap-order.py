#!/usr/bin/env python3
"""Turn Namecheap "Order Summary" emails into itemized hledger transactions.

Usage:
    bin/namecheap-order.py order1.eml order2.eml ...

Namecheap has no invoice export or order-history API; the itemized order is
only in the HTML part of the "Namecheap Order Summary (Order# N)" email (also
re-sendable from Profile -> Billing -> Order History -> Resend receipt). Each
order becomes one transaction dated on the order, with postings per domain
(expenses:domains:namecheap:<domain>: the registration/renewal/transfer price
and, separately, its ICANN fee) against liabilities:accounts-payable:namecheap.
The Chase import rule posts the `NAME-CHEAP.COM*` card charge to that
payable, so it nets to zero.

Orders already in the ledger (matched by order number) are skipped and
reported on stderr.

Requires `hledger` on PATH.
"""

import email
import email.policy
import html
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_FILE = REPO_ROOT / "ledger" / "all.journal"

AP = "liabilities:accounts-payable:namecheap"
EXPENSE = "expenses:domains:namecheap"

MONEY = r"\$([\d,]+\.\d\d)"


@dataclass
class Item:
    domain: str
    amount: Decimal
    comment: str


@dataclass
class Order:
    number: str
    transaction: str
    date: date
    items: list[Item] = field(default_factory=list)
    total: Decimal = Decimal(0)


def money(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def html_lines(msg: EmailMessage) -> list[str]:
    """Render the HTML part to one table cell / text run per line; cells end
    with " |"."""
    part = msg.get_body(preferencelist=("html",))
    if part is None:
        raise ValueError("no HTML part")
    t = part.get_content()
    t = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", t)
    t = re.sub(r"(?i)</t[dh]>", " | ", t)
    t = re.sub(r"(?i)<br\s*/?>|</(p|tr|div|h\d|li|table)>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return [l for l in (" ".join(x.split()) for x in t.splitlines()) if l.strip(" |")]


def after(lines: list[str], label: str) -> str:
    i = next(i for i, l in enumerate(lines) if l.rstrip(" |") == label)
    return lines[i + 1].rstrip(" |")


def parse_message(msg: EmailMessage) -> Order:
    lines = html_lines(msg)
    try:
        number = after(lines, "Order Number:")
        txn = after(lines, "Transaction ID:")
        final = money(re.fullmatch(MONEY, after(lines, "Final Cost:"))[1])
    except (StopIteration, TypeError) as e:
        raise ValueError("not a Namecheap order summary") from e
    m = re.search(r"Order Date\s*:\s*(\w{3} \d{1,2}, \d{4})", msg.get_body(preferencelist=("plain",)).get_content())
    order_date = datetime.strptime(m[1], "%b %d, %Y").date()
    order = Order(number, txn, order_date, total=final)

    # Item table: after the "TITLE | QTY | DURATION | PRICE | SUB TOTAL" header,
    # each item is 1-2 title lines (action, then domain), then cells
    # "qty |", "duration |", "$price |", "$subtotal", then extra charges like
    # "ICANN Fee $0.20" / "Setup $0.00" until the next item.
    start = next(i for i, l in enumerate(lines) if l.startswith("SUB TOTAL")) + 1
    end = next(i for i, l in enumerate(lines[start:], start) if l.startswith(("Sub Total", "TOTAL")))
    rows = lines[start:end]
    i, title, domain = 0, [], ""
    while i < len(rows):
        line = rows[i]
        if re.fullmatch(r"\d+ \|", line) and i + 3 < len(rows):
            qty, duration = line.rstrip(" |"), rows[i + 1].rstrip(" |")
            subtotal = money(re.search(MONEY, rows[i + 3])[1])
            name = " ".join(t.rstrip(" |") for t in title)
            doms = [t for t in title if re.fullmatch(r"[\w.-]+\.[a-z]{2,}", t.rstrip(" |"))]
            if doms:
                domain = doms[0].rstrip(" |")
                name = " ".join(t.rstrip(" |") for t in title if t not in doms)
            if subtotal:
                if not domain:
                    raise ValueError(f"order {number}: charge {name!r} isn't tied to a domain")
                desc = name.lower().replace("domain ", "")
                order.items.append(Item(domain, subtotal, f"{desc}, {duration}" + (f" x{qty}" if qty != "1" else "")))
            i, title = i + 4, []
            continue
        fee = re.fullmatch(rf"(.+?) {MONEY}", line)
        if fee and not title:
            if money(fee[2]):
                order.items.append(Item(domain, money(fee[2]), fee[1]))
        else:
            title.append(line)
        i += 1

    if sum(it.amount for it in order.items) != order.total:
        raise ValueError(f"order {number}: items {sum(it.amount for it in order.items)} != total {order.total}")
    return order


def render(o: Order) -> str:
    lines = [f"{o.date} * Namecheap order {o.number}  ; transaction {o.transaction}"]
    for it in o.items:
        lines.append(f"    {EXPENSE}:{it.domain}  {it.amount:.2f} USD  ; {it.comment}")
    lines.append(f"    {AP}  {-o.total:.2f} USD")
    return "\n".join(lines) + "\n"


def orders_in_ledger() -> set[str]:
    out = subprocess.run(
        ["hledger", "-f", str(LEDGER_FILE), "print", "desc:^Namecheap order "],
        capture_output=True, text=True, check=True,
    ).stdout
    return set(re.findall(r"Namecheap order (\d+)", out))


@dataclass
class Result:
    order: Order
    status: str  # "added" or "exists"
    entry: str = ""


def generate(messages: list[EmailMessage]) -> list[Result]:
    seen = orders_in_ledger()
    results = []
    for o in sorted((parse_message(m) for m in messages), key=lambda o: (o.date, o.number)):
        if o.number in seen:
            results.append(Result(o, "exists"))
        else:
            seen.add(o.number)
            results.append(Result(o, "added", render(o)))
    return results


def main() -> None:
    msgs = []
    for p in sys.argv[1:]:
        with open(p, "rb") as f:
            msgs.append(email.message_from_binary_file(f, policy=email.policy.default))
    results = generate(msgs)
    for r in results:
        if r.status == "exists":
            print(f"skip order {r.order.number}: already in ledger", file=sys.stderr)
    sys.stdout.write("\n".join(r.entry for r in results if r.status == "added"))


if __name__ == "__main__":
    main()
