#!/usr/bin/env python3
"""Generate by-month/ and quarterly-reports/ Markdown files from the hledger
ledger (ledger/all.journal), overwriting whatever is already there.

Usage:
    bin/generate-report.py month 2026-01
    bin/generate-report.py quarter 2026-Q1

Sections that require human judgment (Commentary prose, next-quarter budget,
the subjective "what's inflating the burn rate" narrative) are intentionally
left out rather than guessed at.
"""

import argparse
import calendar
import csv
import io
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_FILE = REPO_ROOT / "ledger" / "all.journal"

ASSET_LABELS = {
    "assets:bluevine": "Bluevine Checking",
    "assets:fidelity": "Fidelity (SPAXX)",
}
LIABILITY_LABELS = {
    "liabilities:chase-cc": "Chase Credit Card",
}
# account -> (display label, "monthly"|"yearly" — matches the periodic
# transaction interval, so the report can label/format each correctly)
BUDGET_ITEMS = {
    "expenses:hosting:hetzner": ("Hetzner", "monthly"),
    "expenses:comms:sendgrid": ("Twilio Sendgrid", "monthly"),
    "expenses:devtools:github": ("GitHub", "monthly"),
    "expenses:backup:backblaze": ("Backblaze", "monthly"),
    "expenses:monitoring:uptimerobot": ("UptimeRobot", "yearly"),
    "expenses:comms:migadu": ("Migadu", "yearly"),
}


def extract_commentary(path: Path) -> str:
    """Pull any hand-written text out of an existing file's '## Commentary'
    section, so regenerating doesn't clobber notes that aren't tied to a
    specific ledger transaction."""
    if not path.exists():
        return ""
    m = re.search(r"^## Commentary\n(.*?)(?=\n## |\Z)", path.read_text(), re.S | re.M)
    return m.group(1).strip() if m else ""


def hledger(*args: str) -> str:
    result = subprocess.run(
        ["hledger", "-f", str(LEDGER_FILE), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(f"hledger failed: {result.stderr.strip()}")
    return result.stdout


def parse_amount(s: str) -> Decimal:
    return Decimal(s.replace("$", "").replace(",", "") or "0")


def fmt_amount(d: Decimal) -> str:
    sign = "-" if d < 0 else ""
    return f"{sign}${abs(d):,.2f}"


def csv_rows(output: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(output)))


def balances_as_of(end: date, accounts: list[str]) -> dict[str, Decimal]:
    """Balance of each account, historical, as of just before `end`."""
    out = hledger(
        "balance", *accounts, "-e", end.isoformat(), "--historical", "--flat", "-O", "csv"
    )
    balances = {row["account"]: parse_amount(row["balance"]) for row in csv_rows(out)}
    return {a: balances.get(a, Decimal(0)) for a in accounts}


def total_equity(end: date) -> Decimal:
    assets = balances_as_of(end, list(ASSET_LABELS))
    liabilities = balances_as_of(end, list(LIABILITY_LABELS))
    return sum(assets.values()) + sum(liabilities.values())


def register(accounts: list[str], start: date, end: date) -> list[dict]:
    out = hledger(
        "register", *accounts, "-b", start.isoformat(), "-e", end.isoformat(), "-O", "csv"
    )
    return csv_rows(out)


def transactions(accounts: list[str], start: date, end: date) -> list[dict]:
    """Like register(), but keeps the transaction comment (register's CSV
    output has no comment column) and only the postings matching `accounts`."""
    out = hledger(
        "print", *accounts, "-b", start.isoformat(), "-e", end.isoformat(), "-O", "csv"
    )
    rows = csv_rows(out)
    return [r for r in rows if any(r["account"] == a or r["account"].startswith(a + ":") for a in accounts)]


def next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def quarter_bounds(year: int, q: int) -> tuple[date, date]:
    start_month = (q - 1) * 3 + 1
    start = date(year, start_month, 1)
    end = date(year + 1, 1, 1) if q == 4 else date(year, start_month + 3, 1)
    return start, end


def prev_quarter(year: int, q: int) -> tuple[int, int]:
    return (year - 1, 4) if q == 1 else (year, q - 1)


def _table(header_cells: list[str], rows: list[list[str]], aligns: list[str]) -> str:
    """Render a markdown table, sizing each column to its widest cell."""
    widths = [
        max(len(header_cells[i]), *(len(r[i]) for r in rows)) if rows else len(header_cells[i])
        for i in range(len(header_cells))
    ]

    def render_row(cells: list[str]) -> str:
        padded = [
            c.rjust(w) if a == "r" else c.ljust(w) for c, w, a in zip(cells, widths, aligns)
        ]
        return "| " + " | ".join(padded) + " |"

    sep = "| " + " | ".join(
        (":" + "-" * (w - 1) if a == "l" else "-" * (w - 1) + ":")
        for w, a in zip(widths, aligns)
    ) + " |"

    lines = [render_row(header_cells), sep]
    lines += [render_row(r) for r in rows]
    return "\n".join(lines)


def equity_table(end: date) -> str:
    assets = balances_as_of(end, list(ASSET_LABELS))
    # Liabilities are credit-normal (negative) in the ledger; this report
    # shows plain positive dollar amounts owed, like a real balance sheet.
    liabilities = {a: -v for a, v in balances_as_of(end, list(LIABILITY_LABELS)).items()}

    asset_rows = [(ASSET_LABELS[a], assets[a]) for a in ASSET_LABELS if assets[a] != 0]
    liability_rows = [
        (LIABILITY_LABELS[a], liabilities[a]) for a in LIABILITY_LABELS if liabilities[a] != 0
    ]
    asset_total = sum(v for _, v in asset_rows)
    liability_total = sum(v for _, v in liability_rows)

    n = max(len(asset_rows), len(liability_rows))
    asset_rows += [("", None)] * (n - len(asset_rows))
    liability_rows += [("", None)] * (n - len(liability_rows))

    rows = [
        [
            f"**{aname}**" if aname else "",
            fmt_amount(aval) if aval is not None else "",
            f"**{lname}**" if lname else "",
            fmt_amount(lval) if lval is not None else "",
        ]
        for (aname, aval), (lname, lval) in zip(asset_rows, liability_rows)
    ]
    rows.append(["**TOTAL**", f"**{fmt_amount(asset_total)}**", "**TOTAL**", f"**{fmt_amount(liability_total)}**"])

    return _table(
        ["**Assets**", "", "**Liabilities**", ""],
        rows,
        ["l", "r", "l", "r"],
    )


def event_text(r: dict) -> str:
    """Description, with any transaction comment folded in as a note."""
    comment = " ".join(r.get("comment", "").split())  # collapse embedded newlines
    return f"{r['description']} ({comment})" if comment else r["description"]


def budget_actual_vs_goal(period: str) -> dict[str, tuple[Decimal, Decimal]]:
    """{account: (actual, budget goal)} for the given hledger period string,
    for just the accounts that have a budget goal declared. An annual item's
    goal is $0 in every quarter except the one it's actually due in."""
    out = hledger(
        "balance", *BUDGET_ITEMS, "-p", period, "--budget", "--flat", "-O", "csv"
    )
    rows = csv_rows(out)
    # hledger normalizes the period column's header (e.g. "2026Q2") rather
    # than echoing back our -p string verbatim, so find it by elimination.
    period_col = next(c for c in rows[0].keys() if c not in ("Account", "budget")) if rows else None
    result = {}
    for row in rows:
        if row["Account"] not in BUDGET_ITEMS:
            continue
        actual = parse_amount(row[period_col])
        goal = parse_amount(row["budget"]) if row["budget"] else Decimal(0)
        result[row["Account"]] = (actual, goal)
    return result


def budget_vs_actual_table(year: int, q: int) -> str:
    data = budget_actual_vs_goal(f"{year}q{q}")
    rows = []
    for account, (label, _interval) in BUDGET_ITEMS.items():
        actual, goal = data.get(account, (Decimal(0), Decimal(0)))
        if actual == 0 and goal == 0:
            continue  # not due, and nothing happened, this quarter
        pct = f"{actual / goal * 100:.0f}%" if goal else "—"
        rows.append([label, fmt_amount(actual), fmt_amount(goal), pct])
    total_actual = sum(a for a, _ in data.values())
    total_goal = sum(g for _, g in data.values())
    total_pct = f"{total_actual / total_goal * 100:.0f}%" if total_goal else "—"
    rows.append(["**TOTAL**", f"**{fmt_amount(total_actual)}**", f"**{fmt_amount(total_goal)}**", f"**{total_pct}**"])
    return _table(["**Line Item**", "**Actual**", "**Budget**", "**%**"], rows, ["l", "r", "r", "l"])


def next_quarter_budget_table(year: int, q: int) -> tuple[int, int, str]:
    ny, nq = (year + 1, 1) if q == 4 else (year, q + 1)
    data = budget_actual_vs_goal(f"{ny}q{nq}")
    rows = []
    total = Decimal(0)
    for account, (label, interval) in BUDGET_ITEMS.items():
        _, goal = data.get(account, (Decimal(0), Decimal(0)))
        if goal == 0:
            continue  # not due next quarter (e.g. an annual renewal elsewhere)
        note = f"~{fmt_amount(goal / 3)}/mo" if interval == "monthly" else "annual"
        rows.append([f"{label} ({note})", fmt_amount(goal)])
        total += goal
    rows.append(["**TOTAL**", f"**{fmt_amount(total)}**"])
    table = _table(["**Line Item**", "**Amount**"], rows, ["l", "r"])
    return ny, nq, table


def events_table(rows: list[dict]) -> str:
    if not rows:
        return "None for this month."
    amounts = [
        -parse_amount(r["amount"]) if r["account"].startswith(("income:", "equity:")) else parse_amount(r["amount"])
        for r in rows
    ]
    total = sum(amounts)

    table_rows = [
        [str(date.fromisoformat(r["date"]).day), event_text(r), fmt_amount(a)]
        for r, a in zip(rows, amounts)
    ]
    table_rows.append(["", "**TOTAL**", f"**{fmt_amount(total)}**"])

    return _table(["**Date**", "**Event**", "**Amount**"], table_rows, ["l", "l", "r"])


def generate_month(year: int, month: int, commentary: str = "") -> str:
    start = date(year, month, 1)
    end = next_month(start)

    revenue_rows = transactions(["income", "equity:member-capital"], start, end)
    expense_rows = transactions(["expenses"], start, end)

    month_name = calendar.month_name[month]
    equity = total_equity(end)
    commentary_block = f"{commentary}\n\n" if commentary else ""

    return f"""# {month_name} {year}

## Commentary

{commentary_block}## Equity

At the end of the month, the total equity of Nevarro LLC was **{fmt_amount(equity)}**.

{equity_table(end)}

## Revenue Events

{events_table(revenue_rows)}

## Expenditures

{events_table(expense_rows)}
"""


def generate_quarter(year: int, q: int, commentary: str = "") -> str:
    start, end = quarter_bounds(year, q)
    py, pq = prev_quarter(year, q)
    _, prev_end = quarter_bounds(py, pq)

    contrib_rows = register(["equity:member-capital"], start, end)
    contributions = -sum(parse_amount(r["amount"]) for r in contrib_rows)

    interest_rows = register(["income:interest"], start, end)
    interest = -sum(parse_amount(r["amount"]) for r in interest_rows)

    expense_rows = register(["expenses"], start, end)
    expenses = sum(parse_amount(r["amount"]) for r in expense_rows)

    equity = total_equity(end)
    prev_equity = total_equity(prev_end)
    delta = equity - prev_equity
    direction = "up" if delta >= 0 else "down"

    months_in_quarter = 3
    avg_burn = expenses / months_in_quarter
    runway_months = (equity / avg_burn) if avg_burn else Decimal(0)
    commentary_block = f"{commentary}\n\n" if commentary else ""

    ny, nq, next_budget_table = next_quarter_budget_table(year, q)

    return f"""# Quarterly Report for Q{q} {year}

## Commentary

{commentary_block}## Equity

A total of {fmt_amount(contributions)} in capital contributions and {fmt_amount(interest)} in SPAXX interest were
received, and {fmt_amount(expenses)} of expenses were incurred.

| **Summary**              |           |
| :----------------------- | --------: |
| **Capital Contribution** | {fmt_amount(contributions):>9} |
| **Interest (SPAXX)**     | {fmt_amount(interest):>9} |
| **Expenses**             | {fmt_amount(expenses):>9} |
| **Equity**               | {fmt_amount(equity):>9} |

The total equity is **{direction} {fmt_amount(abs(delta))}** since last quarter.

At the end of the quarter, the total equity of Nevarro LLC was **{fmt_amount(equity)}**.

The equity breakdown is as follows:

{equity_table(end)}

## Runway Calculation

This quarter, total burn was {fmt_amount(expenses)} for an average of {fmt_amount(avg_burn)}/mo. At this
burn rate, Nevarro LLC has {runway_months:.1f} months of runway.

## Budget vs. Actual

{budget_vs_actual_table(year, q)}

## {ny} Q{nq} Budget

{next_budget_table}
"""


def write_month(year: int, month: int) -> None:
    out_path = REPO_ROOT / "by-month" / f"{year:04d}-{month:02d}.md"
    commentary = extract_commentary(out_path)
    out_path.write_text(generate_month(year, month, commentary))
    print(f"wrote {out_path}")


def write_quarter(year: int, q: int) -> None:
    out_path = REPO_ROOT / "quarterly-reports" / f"{year:04d}-Q{q}.md"
    commentary = extract_commentary(out_path)
    out_path.write_text(generate_quarter(year, q, commentary))
    print(f"wrote {out_path}")


def data_range() -> tuple[date, date]:
    """Earliest and latest transaction date, ignoring the opening-balances
    entry (which exists only to seed a starting point, not real activity)."""
    rows = csv_rows(hledger("print", "-O", "csv"))
    dates = [
        date.fromisoformat(r["date"]) for r in rows if r["description"] != "Opening Balances"
    ]
    return min(dates), max(dates)


def months_in_range(start: date, end: date) -> list[tuple[int, int]]:
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def next_day(d: date) -> date:
    return date.fromordinal(d.toordinal() + 1)


def quarters_in_range(start: date, end: date) -> list[tuple[int, int]]:
    """Only quarters whose *end* date is covered by the data — a quarter
    still in progress would misreport its monthly-average burn rate."""
    seen = {((y, (m - 1) // 3 + 1)) for y, m in months_in_range(start, end)}
    return sorted(yq for yq in seen if quarter_bounds(*yq)[1] <= next_day(end))


def generate_all() -> None:
    start, end = data_range()
    for y, m in months_in_range(start, end):
        write_month(y, m)
    for y, q in quarters_in_range(start, end):
        write_quarter(y, q)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="kind")

    p_month = sub.add_parser("month", help="Generate by-month/YYYY-MM.md")
    p_month.add_argument("period", help="YYYY-MM")

    p_quarter = sub.add_parser("quarter", help="Generate quarterly-reports/YYYY-QN.md")
    p_quarter.add_argument("period", help="YYYY-QN")

    sub.add_parser("all", help="Generate every month/quarter the ledger has data for (default)")

    args = parser.parse_args()

    if args.kind == "month":
        year_s, month_s = args.period.split("-")
        write_month(int(year_s), int(month_s))
    elif args.kind == "quarter":
        year_s, q_s = args.period.split("-Q")
        write_quarter(int(year_s), int(q_s))
    else:
        generate_all()


if __name__ == "__main__":
    main()
