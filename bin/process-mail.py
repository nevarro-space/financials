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

Vendors (sender, subjects, parser) are listed in VENDORS below.
"""

import argparse
import email
import email.policy
import email.utils
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

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
sendgrid = load("sendgrid_invoice", "sendgrid-invoice.py")
namecheap = load("namecheap_order", "namecheap-order.py")
google = load("google_invoice", "google-invoice.py")


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


@dataclass
class Vendor:
    """How to recognize and process one vendor's mail.

    `senders` are original sender addresses; one starting with "@" matches any
    address at that domain. `subjects` are subject prefixes of mail carrying a
    document; `file_only` prefixes are mail that's just filed (e.g. payment
    confirmations the card import already covers). `source` is what the
    module's generate() takes: the PDF attachments, or the message itself.
    """

    journal: str
    module: Any
    senders: tuple[str, ...]
    dkim: str
    subjects: tuple[str, ...]
    source: str = "pdf"  # or "message"
    file_only: tuple[str, ...] = ()

    def sent(self, m: Message) -> bool:
        return any(m.sender.endswith(s) if s.startswith("@") else m.sender == s for s in self.senders)


VENDORS = [
    Vendor("hetzner", hetzner, ("@hetzner.com",), "hetzner.com",
           ("Hetzner Online GmbH - Invoice ", "Hetzner Online GmbH - Cancellation ")),
    Vendor("github", github, ("noreply@github.com",), "github.com",
           ("[GitHub] Payment Receipt for nevarro-space",)),
    Vendor("sendgrid", sendgrid, ("noreply@sendgrid.com",), "sendgrid.com",
           ("You have a new invoice from SendGrid",),
           file_only=("Your payment to SendGrid was successful",)),
    Vendor("namecheap", namecheap, ("support@namecheap.com",), "namecheap.com",
           ("Namecheap Order Summary",), source="message"),
    Vendor("google", google, ("payments-noreply@google.com",), "google.com",
           ("Google Workspace: Your invoice is available",)),
]


def handle(v: Vendor, m: Message, tmp: Path) -> Outcome | None:
    document = m.subject.startswith(v.subjects)
    filing = m.subject.startswith(v.file_only) if v.file_only else False
    if not v.sent(m) or not (document or filing):
        return None
    if not m.dkim_verified(v.dkim):
        return Outcome(m, "held", f"DKIM for {v.dkim} not verified; check this message by hand", [])
    if filing:
        return Outcome(m, "exists", "nothing to record (covered by the card import)", [])
    if v.source == "message":
        results = v.module.generate([m.msg])
    else:
        pdfs = m.pdfs(tmp)
        if not pdfs:
            return None
        results = v.module.generate(pdfs)
    detail = ", ".join(f"{r.label} {r.status}" for r in results)
    entries = [(v.journal, r.entry) for r in results if r.status == "added"]
    return Outcome(m, "added" if entries else "exists", detail, entries)


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
    text = journal.read_text().strip("\n") if journal.exists() else ""
    blocks = text.split("\n\n") if text else []
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


# New subaccounts under these prefixes (one per domain, maintainer, ...) are
# declared automatically in that year's accounts.journal; anything else that's
# undeclared still fails the strict check.
AUTO_DECLARE = ("expenses:domains:namecheap:", "expenses:donations:github:")


def declare_new_accounts(entries: list[tuple[str, str]], backups: dict[Path, str | None]) -> list[str]:
    declared = []
    for _, entry in entries:
        year = entry[:4]
        accounts_file = LEDGER_DIR / year / "accounts.journal"
        for acct in re.findall(r"^\s+(\S+(?: \S+)*?)\s{2,}", entry, re.M):
            if not acct.startswith(AUTO_DECLARE):
                continue
            text = accounts_file.read_text()
            line = f"account {acct}"
            if line in text.splitlines():
                continue
            backups.setdefault(accounts_file, text)
            # Insert after the last declaration sharing the vendor prefix, or
            # at the end of the file.
            prefix = next(p for p in AUTO_DECLARE if acct.startswith(p)).rsplit(":", 2)[0]
            lines = text.rstrip("\n").split("\n")
            idx = max((i for i, l in enumerate(lines) if l.startswith(f"account {prefix}")), default=len(lines) - 1)
            lines.insert(idx + 1, line)
            accounts_file.write_text("\n".join(lines) + "\n")
            declared.append(f"{year}: {acct}")
    return declared


def format_journals(paths: list[Path]) -> None:
    """Run the repo's hledger-fmt pre-commit hook on the files we changed, so
    they're already formatted the way the commit hook wants them."""
    if not paths or not shutil.which("pre-commit"):
        return
    subprocess.run(
        ["pre-commit", "run", "hledger-fmt", "--files", *(str(p) for p in paths)],
        cwd=REPO_ROOT, capture_output=True,
    )


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
            for v in VENDORS:
                outcome = handle(v, m, Path(tempfile.mkdtemp(dir=tmp)))
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
    added = [(v, e) for o in outcomes if o.status == "added" for v, e in o.entries] + reconciled
    for d in declare_new_accounts(added, backups):
        print(f"declared {d}")

    format_journals([p for p in backups if p.exists()])

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
