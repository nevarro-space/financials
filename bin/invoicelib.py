"""Shared plumbing for the vendor invoice parsers (bin/*-invoice.py etc.).

Each vendor module parses its documents into objects with `number` and `date`
attributes, renders each as a journal entry, and uses `generate()` to skip
documents already in the ledger. `main()` is the common command line: print
entries for new documents, report skipped ones on stderr.
"""

import csv
import io
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_FILE = REPO_ROOT / "ledger" / "all.journal"


def hledger(*args: str) -> str:
    return subprocess.run(
        ["hledger", "-f", str(LEDGER_FILE), *args], capture_output=True, text=True, check=True
    ).stdout


def hledger_csv(*args: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(hledger(*args, "-O", "csv"))))


def numbers_in_ledger(desc_query: str, pattern: str) -> set[str]:
    """Document numbers already in the ledger: `pattern`'s first group, over
    the printed transactions matching `desc_query` (e.g. "desc:^SendGrid invoice ")."""
    return set(re.findall(pattern, hledger("print", desc_query)))


def pdf_text(path: Path, *opts: str) -> str:
    return subprocess.run(
        ["pdftotext", *opts, str(path), "-"], capture_output=True, text=True, check=True
    ).stdout


@dataclass
class Result:
    doc: Any
    status: str  # "added" or "exists"
    label: str  # e.g. "invoice INV123", for messages
    entry: str = ""


def generate(
    docs: Iterable[Any],
    seen: set[str],
    render: Callable[[Any], str],
    label: Callable[[Any], str],
) -> list[Result]:
    """One Result per document, in (date, number) order; only "added" ones
    carry an entry. Duplicates within `docs` are added once."""
    seen = set(seen)
    results = []
    for doc in sorted(docs, key=lambda d: (d.date, d.number)):
        if doc.number in seen:
            results.append(Result(doc, "exists", label(doc)))
        else:
            seen.add(doc.number)
            results.append(Result(doc, "added", label(doc), render(doc)))
    return results


def main(generate_from_args: Callable[[list[str]], list[Result]]) -> None:
    results = generate_from_args(sys.argv[1:])
    for r in results:
        if r.status == "exists":
            print(f"skip {r.label}: already in ledger", file=sys.stderr)
    sys.stdout.write("\n".join(r.entry for r in results if r.status == "added"))
