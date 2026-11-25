#!/usr/bin/env python3
"""Turn vendor invoice emails in the Nevarro maildir into ledger entries.

Usage (after `mbsync Nevarro` and importing the latest Chase CSV):
    bin/process-mail.py            # process and file away
    bin/process-mail.py --dry-run  # show what would happen, change nothing

1. For each message in INBOX that a handler recognizes, the handler produces
   journal entries (e.g. an itemized invoice against accounts payable), which
   are inserted in date order into the vendor's ledger/YYYY/<vendor>.journal.
2. Reconcilers then add any entries that depend on other imports, e.g. a
   Hetzner payment (EUR->USD conversion) once its card charge is imported.
   These run even when there's no new mail.
3. If `hledger check -s ordereddates` passes, processed messages are moved to
   the Invoices folder (which mbsync syncs back as the Gmail "Invoices" label,
   archiving it out of the inbox). Otherwise the ledger is restored and no
   mail is moved.

A message whose document is already in the ledger is just moved. Messages no
handler recognizes, or that fail sender verification, are left in INBOX.

Handlers:
    hetzner  "Hetzner Online GmbH - Invoice|Cancellation ..." with a PDF
             (see bin/hetzner-invoice.py)
    github   "[GitHub] Payment Receipt for nevarro-space" with a PDF
             (see bin/github-receipt.py)
"""

import argparse
import email
import email.policy
import email.utils
import importlib.util
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_DIR = REPO_ROOT / "ledger"
DEFAULT_MAILDIR = Path.home() / "Mail" / "Nevarro"
# Mail servers whose Authentication-Results we trust: Google Workspace now,
# Migadu for mail received before the move.
TRUSTED_AUTHSERV = re.compile(r"\b(mx\.google\.com|aspmx\d*\.migadu\.com);")


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "bin" / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hetzner = load("hetzner_invoice", "hetzner-invoice.py")
github = load("github_receipt", "github-receipt.py")


@dataclass
class Message:
    path: Path
    msg: EmailMessage

    @property
    def sender(self) -> str:
        """The original sender's address. Mail relayed through a Google Group
        (e.g. admin@nevarro.space) keeps it in X-Original-Sender."""
        hdr = self.msg.get("X-Original-Sender") or self.msg.get("From", "")
        return email.utils.parseaddr(str(hdr))[1].lower()

    def dkim_verified(self, domain: str) -> bool:
        """Whether a trusted mail server verified a DKIM signature from `domain`, either on
        direct delivery (Authentication-Results) or before relaying through a
        group (ARC-Authentication-Results). Guards against spoofed senders."""
        headers = (self.msg.get_all("Authentication-Results") or []) + (
            self.msg.get_all("ARC-Authentication-Results") or []
        )
        pat = re.compile(rf"\bdkim=pass\b[^;]*header\.(?:i|d)=@?(?:[\w-]+\.)*{re.escape(domain)}\b")
        return any(
            TRUSTED_AUTHSERV.search(" ".join(str(h).split()[:2])) and pat.search(" ".join(str(h).split()))
            for h in headers
        )

    @property
    def subject(self) -> str:
        return " ".join(str(self.msg.get("Subject", "")).split())

    def pdfs(self, dest: Path) -> list[Path]:
        out = []
        for part in self.msg.iter_attachments():
            name = Path(part.get_filename() or "").name
            if not name.lower().endswith(".pdf"):
                continue
            path = dest / re.sub(r"[^A-Za-z0-9._-]", "_", name)
            path.write_bytes(part.get_payload(decode=True))
            out.append(path)
        return out


@dataclass
class Outcome:
    message: Message
    status: str  # "added", "exists", or "held" (left in INBOX)
    detail: str
    entries: list[tuple[str, str]]  # (journal name, entry text)


def handle_hetzner(m: Message, tmp: Path) -> Outcome | None:
    if not m.sender.endswith("@hetzner.com") or not re.match(
        r"Hetzner Online GmbH - (Invoice|Cancellation) ", m.subject
    ):
        return None
    if not m.dkim_verified("hetzner.com"):
        return Outcome(m, "held", "DKIM for hetzner.com not verified; check this message by hand", [])
    pdfs = m.pdfs(tmp)
    if not pdfs:
        return None
    results = hetzner.generate(pdfs)
    detail = ", ".join(f"{r.invoice.kind.lower()} {r.invoice.number} {r.status}" for r in results)
    entries = [("hetzner", r.entry) for r in results if r.status == "added"]
    return Outcome(m, "added" if entries else "exists", detail, entries)


def handle_github(m: Message, tmp: Path) -> Outcome | None:
    if m.sender != "noreply@github.com" or not m.subject.startswith("[GitHub] Payment Receipt for nevarro-space"):
        return None
    if not m.dkim_verified("github.com"):
        return Outcome(m, "held", "DKIM for github.com not verified; check this message by hand", [])
    pdfs = m.pdfs(tmp)
    if not pdfs:
        return None
    results = github.generate(pdfs)
    detail = ", ".join(f"receipt {r.receipt.txn_id} {r.status}" for r in results)
    entries = [("github", r.entry) for r in results if r.status == "added"]
    return Outcome(m, "added" if entries else "exists", detail, entries)


HANDLERS = [handle_hetzner, handle_github]

# Called after handler entries are written; each returns (vendor, entry) pairs
# for entries that can now be completed from what's in the ledger.
RECONCILERS = [lambda: [("hetzner", e) for e in hetzner.payments()]]


def read_messages(folder: Path) -> list[Message]:
    msgs = []
    for sub in ("new", "cur"):
        for path in sorted((folder / sub).iterdir()):
            with open(path, "rb") as f:
                msgs.append(Message(path, email.message_from_binary_file(f, policy=email.policy.default)))
    return msgs


def insert_sorted(journal: Path, entry: str) -> None:
    """Insert a transaction into a journal of date-ordered transactions,
    after any transactions on or before its date (keeps ordereddates)."""
    entry = entry.strip("\n")
    date = entry[:10]
    blocks = journal.read_text().strip("\n").split("\n\n") if journal.exists() else []
    i = len(blocks)
    for j, block in enumerate(blocks):
        if re.match(r"\d{4}-\d\d-\d\d", block) and block[:10] > date:
            i = j
            break
    blocks.insert(i, entry)
    journal.write_text("\n\n".join(blocks) + "\n")


def journal_for(vendor: str, entry: str) -> Path:
    year_dir = LEDGER_DIR / entry[:4]
    journal = year_dir / f"{vendor}.journal"
    index = year_dir / "index.journal"
    line = f"include {vendor}.journal"
    if not journal.exists():
        journal.write_text("")
    if line not in index.read_text():
        index.write_text(index.read_text().rstrip("\n") + f"\n{line}\n")
    return journal


def ledger_ok() -> tuple[bool, str]:
    r = subprocess.run(
        ["hledger", "-f", str(LEDGER_DIR / "all.journal"), "check", "-s", "ordereddates"],
        capture_output=True, text=True,
    )
    return r.returncode == 0, r.stderr.strip()


def move_to(m: Message, folder: Path) -> Path:
    """Move a maildir message to another folder. mbsync's ",U=<uid>" must be
    dropped so it's uploaded as a new message there."""
    name = re.sub(r",U=\d+", "", m.path.name)
    dest = folder / m.path.parent.name / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    m.path.rename(dest)
    return dest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--maildir", type=Path, default=DEFAULT_MAILDIR)
    ap.add_argument("--inbox", default="INBOX")
    ap.add_argument("--processed", default="Invoices")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    inbox = args.maildir / args.inbox
    processed = args.maildir / args.processed

    outcomes: list[Outcome] = []
    with tempfile.TemporaryDirectory() as tmp:
        for m in read_messages(inbox):
            for handler in HANDLERS:
                outcome = handler(m, Path(tempfile.mkdtemp(dir=tmp)))
                if outcome:
                    outcomes.append(outcome)
                    break

    for o in outcomes:
        print(f"{o.status:8} {o.message.subject}: {o.detail}")
        for _, entry in o.entries:
            print("\n" + entry.rstrip("\n") + "\n")

    backups: dict[Path, str | None] = {}

    def write(entries: list[tuple[str, str]]) -> None:
        for vendor, entry in entries:
            journal = LEDGER_DIR / entry[:4] / f"{vendor}.journal"
            for p in (journal, journal.parent / "index.journal"):
                backups.setdefault(p, p.read_text() if p.exists() else None)
            insert_sorted(journal_for(vendor, entry), entry)

    if not args.dry_run:
        write([(v, e) for o in outcomes if o.status == "added" for v, e in o.entries])

    # In a dry run, entries for mail not yet written can't be reconciled.
    reconciled = [pair for r in RECONCILERS for pair in r()]
    for _, entry in reconciled:
        print("reconcile\n" + entry.rstrip("\n") + "\n")
    if args.dry_run:
        if not outcomes and not reconciled:
            print("Nothing to process.")
        return
    write(reconciled)

    if backups:
        ok, err = ledger_ok()
        if not ok:
            for p, text in backups.items():
                if text is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_text(text)
            sys.exit(f"hledger check failed; ledger restored, no mail moved:\n{err}")

    moved = 0
    for o in outcomes:
        if o.status in ("added", "exists"):
            dest = move_to(o.message, processed)
            print(f"moved    {o.message.subject} -> {dest.relative_to(args.maildir)}")
            moved += 1
    if not outcomes and not reconciled:
        print("Nothing to process.")
    elif moved:
        print("Run `mbsync Nevarro` to sync the moves; commit the ledger changes.")


if __name__ == "__main__":
    main()
