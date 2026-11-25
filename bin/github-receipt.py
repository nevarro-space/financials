#!/usr/bin/env python3
"""Turn GitHub Sponsors receipt PDFs into itemized hledger transactions.

Usage:
    bin/github-receipt.py ~/Downloads/github-nevarro-space-receipt-*.pdf

Nevarro sponsors maintainers from the nevarro-space org, and GitHub charges
organizations a fee on top of each sponsorship. Each receipt becomes one
transaction with a posting per sponsorship (expenses:donations:github:<login>)
and per fee (expenses:fees:github-sponsors), against
liabilities:accounts-payable:github. The Chase import rule posts the `GITHUB`
card charge to that payable, so it nets to zero.

Receipts already in the ledger (matched by transaction ID) are skipped and
reported on stderr.

Requires `pdftotext` (poppler-utils) and `hledger` on PATH.
"""

import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_FILE = REPO_ROOT / "ledger" / "all.journal"

AP = "liabilities:accounts-payable:github"
DONATION = "expenses:donations:github"
FEE = "expenses:fees:github-sponsors"


@dataclass
class Line:
    login: str
    amount: Decimal
    comment: str
    fee: bool = False


@dataclass
class Receipt:
    path: Path
    txn_id: str
    date: date
    lines: list[Line] = field(default_factory=list)
    total: Decimal = Decimal(0)


def pdf_text(path: Path) -> str:
    return subprocess.run(
        ["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True
    ).stdout


def parse(path: Path) -> Receipt:
    text = pdf_text(path)
    d = re.search(r"^\s*Date\s+(\d{4}-\d\d-\d\d)", text, re.M)
    tid = re.search(r"^\s*Transaction ID\s+(\S+)", text, re.M)
    total = re.search(r"^\s*Total\s+\$([\d,]+\.\d\d)", text, re.M)
    if not (d and tid and total):
        raise ValueError(f"{path}: not a GitHub receipt (missing date, transaction ID or total)")
    r = Receipt(path, tid[1], date.fromisoformat(d[1]), total=Decimal(total[1].replace(",", "")))

    # "Sponsorships   arp242 - $5 a month" followed by continuation lines,
    # including "arp242 - $5 a month - fee ($0.30)".
    block = re.search(r"^\s*Sponsorships\s+(.*?)^\s*Sponsorship Amount", text, re.S | re.M)
    if not block:
        raise ValueError(f"{path}: no sponsorships section")
    for raw in block[1].splitlines():
        line = raw.strip()
        if not line:
            continue
        fee = re.fullmatch(r"(\S+) - (\$[\d.,]+ a (?:month|year)) - fee \(\$([\d.,]+)\)", line)
        spon = re.fullmatch(r"(\S+) - \$([\d.,]+) a (month|year)", line)
        if fee:
            r.lines.append(Line(fee[1], Decimal(fee[3].replace(",", "")), f"fee on {fee[2]}", fee=True))
        elif spon:
            r.lines.append(Line(spon[1], Decimal(spon[2].replace(",", "")), f"${spon[2]} a {spon[3]}"))
        else:
            raise ValueError(f"{path}: unrecognized sponsorship line: {line!r}")

    if sum(l.amount for l in r.lines) != r.total:
        raise ValueError(f"{path}: line items {sum(l.amount for l in r.lines)} != total {r.total}")
    return r


def render(r: Receipt) -> str:
    lines = [f"{r.date} * GitHub Sponsors receipt {r.txn_id}"]
    for l in r.lines:
        account = FEE if l.fee else f"{DONATION}:{l.login}"
        comment = f"{l.login}, {l.comment}" if l.fee else l.comment
        lines.append(f"    {account}  {l.amount:.2f} USD  ; {comment}")
    lines.append(f"    {AP}  {-r.total:.2f} USD")
    return "\n".join(lines) + "\n"


def receipts_in_ledger() -> set[str]:
    out = subprocess.run(
        ["hledger", "-f", str(LEDGER_FILE), "print", "desc:^GitHub Sponsors receipt "],
        capture_output=True, text=True, check=True,
    ).stdout
    return set(re.findall(r"GitHub Sponsors receipt (\S+)", out))


@dataclass
class Result:
    receipt: Receipt
    status: str  # "added" or "exists"
    entry: str = ""


def generate(paths: list[Path]) -> list[Result]:
    seen = receipts_in_ledger()
    results = []
    for r in sorted((parse(p) for p in paths), key=lambda r: (r.date, r.txn_id)):
        if r.txn_id in seen:
            results.append(Result(r, "exists"))
        else:
            seen.add(r.txn_id)
            results.append(Result(r, "added", render(r)))
    return results


def main() -> None:
    results = generate([Path(p) for p in sys.argv[1:]])
    for r in results:
        if r.status == "exists":
            print(f"skip {r.receipt.txn_id}: already in ledger", file=sys.stderr)
    sys.stdout.write("\n".join(r.entry for r in results if r.status == "added"))


if __name__ == "__main__":
    main()
