#!/usr/bin/env bash
# Fetch daily EUR/USD reference rates from the ECB into ledger/prices/EUR.journal
# so EUR amounts (Hetzner invoices) can be valued in USD with `-X USD`.
#
# Adapted from sumnerevans/financials. Nevarro never holds EUR in an asset
# account (it only appears on Hetzner invoices and the Hetzner payable), so the
# start date is the first EUR posting anywhere in the ledger rather than the
# first date it was held.

set -euo pipefail

cd "$(dirname "$0")/.."

OUTFILE="ledger/prices/EUR.journal"
# pricehist's -e is inclusive, and today's rate may not be published yet.
YESTERDAY="$(date -d yesterday +%Y-%m-%d)"

if [ -s "$OUTFILE" ]; then
    last="$(sort -k2,2 "$OUTFILE" | tail -1 | awk '{print $2}')"
    start="$(date -d "$last +1 day" +%Y-%m-%d)"
else
    start="$(hledger register cur:^EUR\$ -O csv | sed -n 2p | cut -d, -f2 | tr -d '"')"
    if [ -z "$start" ]; then
        echo "No EUR postings in the ledger; nothing to fetch"
        exit 0
    fi
fi

if [[ "$start" > "$YESTERDAY" ]]; then
    echo "EUR prices up to date (through $YESTERDAY)"
    exit 0
fi

echo "Fetching EUR prices from $start to $YESTERDAY"
pricehist fetch -o ledger ecb EUR/USD -s "$start" -e "$YESTERDAY" \
    --fmt-time '' --fmt-quote USD --fmt-symbol rightspace >>"$OUTFILE"
sort -k2,2 -o "$OUTFILE" "$OUTFILE"
