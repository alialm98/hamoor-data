#!/usr/bin/env python3
"""
update_dividends.py
─────────────────────────────────────────────────────────────────────────
Annual dividend backfill for Hamoor.

Scrapes maqasa.com's dividend-distribution page for every stock we track
(boursa-kuwait-stocks.json) and writes a `dividends.json` keyed by ticker.
Each value is a list of `DividendEntry`-compatible records that the Swift
app already knows how to decode.

Designed to run rarely — once per fiscal year after the AGM season (most
Kuwaiti companies finalize their full-year dividends in March/April, with
interim H1 dividends approved in July/August). A GitHub Actions workflow
fires this on a yearly cron + on manual workflow_dispatch.

Strategy:
  1. Fetch the maqasa index once and build a {boursa_code → maqasa_name}
     map. The Boursa Code (= our `tickerNumber`) is the only reliable
     join key — maqasa's company names don't always match ours verbatim.
  2. For each ticker in our list that has a tickerNumber + matching
     maqasa name, request:
        /en/dividend-distribution/?company={NAME}&from=2016&to={current}
     This returns the stock's full history within that range on one page.
  3. Parse every <h5>YEAR</h5> + <div class="year-content"> block.
     Each block has: Cash Distribution (ex-date), Cash% (of 100-fils par),
     Share Distribution (bonus ex-date), Share% (bonus pct), Comments.
  4. Convert each block to up to two DividendEntry records:
       cash      → { type: "cash",  amountPerShare: cash%/1000,  exDate: … }
       bonus     → { type: "stock", amountPerShare: 0,
                     bonusSharesPercent: share%, exDate: … }
     When multiple cash payouts share a year, the chronologically first
     becomes "cash_interim", the last "cash_final" (matches the Swift
     aggregator's expectation in DividendEntry.swift).
  5. Write data/output/dividends.json. The next `update_prices.py` run
     merges this into the stocks.json `dividends` field — that's what the
     app pulls via OTA.

Usage:
    python3 update_dividends.py                    # full scrape
    python3 update_dividends.py --ticker KFH       # one-stock test
    python3 update_dividends.py --from-year 2020   # narrower range
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

SCRIPT_DIR = Path(__file__).resolve().parent
STOCKS_FILE = SCRIPT_DIR / "boursa-kuwait-stocks.json"
OUTPUT_FILE = SCRIPT_DIR / "output" / "dividends.json"

MAQASA_BASE = "https://www.maqasa.com/en/dividend-distribution/"
EARLIEST_YEAR_DEFAULT = 2016
USER_AGENT = "hamoor-data-bot/1.0 (+https://github.com/alialm98/hamoor-data)"
REQUEST_TIMEOUT = 20
THROTTLE_SECONDS = 0.4


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# HTTP

def fetch(url: str, attempts: int = 3) -> str:
    """GET a URL with a polite UA, retrying transient failures."""
    last_err: Optional[Exception] = None
    for i in range(attempts):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                charset = r.headers.get_content_charset() or "utf-8"
                return r.read().decode(charset, errors="replace")
        except (URLError, HTTPError, TimeoutError) as e:
            last_err = e
            time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"fetch failed after {attempts} attempts: {url} → {last_err}")


# ---------------------------------------------------------------------------
# Maqasa name index
#
# The bare /en/dividend-distribution/ index page only renders ~100 stocks.
# Fetching the same path with `?from=YYYY&to=YYYY` returns the much fuller
# historical list (310+ entries), which is what we want for the name
# lookup. We pair each "Boursa Code N" occurrence with the immediately-
# preceding <h3>NAME</h3> rather than trying to span both with one regex —
# that approach used to silently miss ~30% of cards.

_BOURSA_CODE_RE = re.compile(r'Boursa Code\s*(\d+)')
_H3_RE = re.compile(r'<h3[^>]*>([^<]+)</h3>')

_HTML_ENTITY = {"&amp;": "&", "&quot;": '"', "&apos;": "'", "&lt;": "<", "&gt;": ">"}


def _decode_entities(s: str) -> str:
    for k, v in _HTML_ENTITY.items():
        s = s.replace(k, v)
    return s


def build_name_index(from_year: int, to_year: int) -> dict[str, str]:
    """Map maqasa Boursa Code (string) → maqasa company name."""
    log("Fetching maqasa historical index…")
    url = f"{MAQASA_BASE}?from={from_year}&to={to_year}"
    html = fetch(url)

    boursa_iter = list(_BOURSA_CODE_RE.finditer(html))
    h3_iter = list(_H3_RE.finditer(html))

    index: dict[str, str] = {}
    h3_idx = 0
    last_h3 = None
    for bm in boursa_iter:
        while h3_idx < len(h3_iter) and h3_iter[h3_idx].start() < bm.start():
            last_h3 = h3_iter[h3_idx]
            h3_idx += 1
        if last_h3 is None:
            continue
        code = bm.group(1)
        # Don't overwrite — first occurrence wins (the active card).
        # Maqasa sometimes lists the same stock multiple times for
        # different fiscal years; the name is the same either way.
        if code in index:
            continue
        index[code] = _decode_entities(" ".join(last_h3.group(1).split()))

    log(f"  → {len(index)} (Boursa Code → maqasa name) mappings")
    return index


# ---------------------------------------------------------------------------
# Per-stock parse

# Cards are organized: <h3>NAME</h3> ... <h5>YEAR</h5> ... <div class="year-content">FIELDS</div>
# Multiple year-content blocks may sit under one <h5> (interim + final).

_H5_YEAR = re.compile(r'<h5>\s*(\d{4})\s*</h5>')
_YEAR_CONTENT = re.compile(r'<div class="year-content">(.*?)</div>', re.DOTALL)

_FIELD_PATTERNS = {
    "cash_date":   re.compile(r'Cash Distribution:</span>\s*&nbsp;?([^<\n]*?)\s*</p>'),
    "cash_pct":    re.compile(r'Cash%:</span>\s*&nbsp;?([^<\n]*?)\s*</p>'),
    "share_date":  re.compile(r'Share Distribution:</span>\s*&nbsp;?([^<\n]*?)\s*</p>'),
    "share_pct":   re.compile(r'Share%:</span>\s*&nbsp;?([^<\n]*?)\s*</p>'),
    "comments":    re.compile(r'Comments:</span>\s*&nbsp;?([^<\n]*?)\s*</p>'),
}


def _field(block: str, name: str) -> Optional[str]:
    m = _FIELD_PATTERNS[name].search(block)
    if not m:
        return None
    val = m.group(1).strip()
    if val in ("", "-"):
        return None
    return val


def _parse_pct(s: Optional[str]) -> Optional[float]:
    """'17.0%' → 17.0, '17%' → 17.0, None → None."""
    if not s:
        return None
    s = s.strip().rstrip("%").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _iso_date(ddmmyyyy: Optional[str]) -> Optional[str]:
    """'27/03/2016' → '2016-03-27', None → None."""
    if not ddmmyyyy:
        return None
    try:
        d, m, y = ddmmyyyy.split("/")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except (ValueError, AttributeError):
        return None


def parse_stock_history(html: str) -> list[dict]:
    """Parse a per-stock filtered page into a list of payout dicts.

    Each entry: { year, cashDate, cashPct, shareDate, sharePct, comments }
    Multiple entries per year are possible (interim + final).
    """
    payouts: list[dict] = []
    # Walk <h5> markers and collect the year-content blocks under each.
    h5_matches = list(_H5_YEAR.finditer(html))
    for i, m in enumerate(h5_matches):
        year = int(m.group(1))
        end = h5_matches[i + 1].start() if i + 1 < len(h5_matches) else len(html)
        section = html[m.end():end]
        for yc in _YEAR_CONTENT.finditer(section):
            block = yc.group(1)
            payouts.append({
                "year": year,
                "cashDate": _iso_date(_field(block, "cash_date")),
                "cashPct": _parse_pct(_field(block, "cash_pct")),
                "shareDate": _iso_date(_field(block, "share_date")),
                "sharePct": _parse_pct(_field(block, "share_pct")),
                "comments": _field(block, "comments"),
            })
    return payouts


# ---------------------------------------------------------------------------
# DividendEntry conversion

def to_dividend_entries(payouts: list[dict]) -> list[dict]:
    """Convert maqasa payouts → DividendEntry-shaped records (Swift-compatible).

    Each payout can produce up to two entries (cash + bonus shares). When a
    single year has multiple cash payouts, the chronologically earliest
    gets `cash_interim`, the latest `cash_final`, and any middle entries
    stay as `cash` — matches the aggregator note in `DividendEntry.swift`.
    """
    # Group cash payouts per fiscal year to assign interim/final labels
    by_year_cash: dict[int, list[dict]] = defaultdict(list)
    for p in payouts:
        if p["cashPct"] is not None and p["cashPct"] > 0:
            by_year_cash[p["year"]].append(p)
    cash_label: dict[id, str] = {}  # keyed by id(p) so we can label in-place
    for year, items in by_year_cash.items():
        items_sorted = sorted(items, key=lambda p: p.get("cashDate") or "")
        if len(items_sorted) == 1:
            cash_label[id(items_sorted[0])] = "cash"
        else:
            cash_label[id(items_sorted[0])] = "cash_interim"
            cash_label[id(items_sorted[-1])] = "cash_final"
            for mid in items_sorted[1:-1]:
                cash_label[id(mid)] = "cash"

    entries: list[dict] = []
    for p in payouts:
        ex_date = p["cashDate"] or p["shareDate"]
        comments = p["comments"]

        # Cash entry
        if p["cashPct"] is not None and p["cashPct"] > 0:
            # cash% is percent of the 100-fils par value, so amountPerShare
            # in KWD = cash% / 1000  (e.g. 24% → 0.024 KWD/share).
            amount = round(p["cashPct"] / 1000.0, 6)
            entries.append({
                "fiscalYear": p["year"],
                "type": cash_label.get(id(p), "cash"),
                "amountPerShare": amount,
                "currency": "KWD",
                "exDate": p["cashDate"],
                "paymentDate": None,
                "bonusSharesPercent": None,
                "agmDate": None,
            })

        # Bonus shares entry
        if p["sharePct"] is not None and p["sharePct"] > 0:
            entries.append({
                "fiscalYear": p["year"],
                "type": "stock",
                "amountPerShare": 0,
                "currency": "KWD",
                "exDate": p["shareDate"] or p["cashDate"],
                "paymentDate": None,
                # bonusSharesPercent stored as int per DividendEntry's schema
                "bonusSharesPercent": int(round(p["sharePct"])),
                "agmDate": None,
            })

    # Sort newest first for stable output (matches the app's typical display order)
    entries.sort(key=lambda e: (e["fiscalYear"], e.get("exDate") or ""), reverse=True)
    return entries


# ---------------------------------------------------------------------------
# Stock list

def load_our_stocks() -> list[dict]:
    with open(STOCKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["stocks"] if isinstance(data, dict) else data


# ---------------------------------------------------------------------------
# Orchestration

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--ticker", help="Restrict to a single ticker (for testing)")
    ap.add_argument("--from-year", type=int, default=EARLIEST_YEAR_DEFAULT,
                    help=f"Earliest year to request (default {EARLIEST_YEAR_DEFAULT})")
    ap.add_argument("--to-year", type=int, default=datetime.now().year,
                    help="Latest year (defaults to current calendar year)")
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    name_index = build_name_index(args.from_year, args.to_year)
    stocks = load_our_stocks()
    if args.ticker:
        stocks = [s for s in stocks if s["ticker"] == args.ticker.upper()]
        if not stocks:
            log(f"No stock matching --ticker {args.ticker!r}")
            return 1

    results: dict[str, list[dict]] = {}
    matched = 0
    skipped_no_code = 0
    skipped_no_match = 0
    skipped_no_data = 0
    error_count = 0

    for s in stocks:
        ticker = s["ticker"]
        boursa_code = s.get("tickerNumber")
        if not boursa_code:
            skipped_no_code += 1
            log(f"  {ticker:14} skip: no tickerNumber in our data")
            continue

        maqasa_name = name_index.get(str(boursa_code))
        if not maqasa_name:
            skipped_no_match += 1
            log(f"  {ticker:14} skip: no maqasa entry for Boursa Code {boursa_code}")
            continue

        url = (
            f"{MAQASA_BASE}?company={quote_plus(maqasa_name)}"
            f"&from={args.from_year}&to={args.to_year}"
        )

        try:
            html = fetch(url)
        except Exception as e:
            error_count += 1
            log(f"  {ticker:14} fetch error: {e}")
            continue

        payouts = parse_stock_history(html)
        entries = to_dividend_entries(payouts)
        if not entries:
            skipped_no_data += 1
            log(f"  {ticker:14} no payouts found ({maqasa_name})")
        else:
            results[ticker] = entries
            matched += 1
            log(f"  {ticker:14} {len(entries):>3} entries  ({maqasa_name})")

        time.sleep(THROTTLE_SECONDS)

    log("")
    log("Summary:")
    log(f"  Matched + data : {matched}")
    log(f"  Skip no code   : {skipped_no_code}")
    log(f"  Skip no maqasa : {skipped_no_match}")
    log(f"  Skip no data   : {skipped_no_data}")
    log(f"  Fetch errors   : {error_count}")

    output = {
        "_meta": {
            "generatedAt": datetime.utcnow().isoformat() + "Z",
            "source": "maqasa.com/en/dividend-distribution",
            "yearRange": [args.from_year, args.to_year],
            "matched": matched,
        },
        "dividendsByTicker": results,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log(f"Wrote {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
