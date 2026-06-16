#!/usr/bin/env python3
"""
update_dividend_calendar.py
─────────────────────────────────────────────────────────────────────────
Builds `output/dividend-calendar.json` — the dividend events calendar the
iOS app's Calendar tab consumes — from the maqasa.com scrape
(`output/dividends.json`, produced by `update_dividends.py`).

maqasa is now the SINGLE source of truth for dividends (the hand-curated
`dividend.xlsx` has been retired). maqasa publishes the ex-date and the
cash amount per share, but NOT the cum / record / payment dates. So this
script:

  - takes each declared payout's ex-date + amount straight from maqasa,
  - derives the cum-date as the trading day immediately before the
    ex-date (Boursa Kuwait trades Sun–Thu; the cum-date is the last day
    you can buy and still receive the dividend),
  - leaves recordDate / paymentDate null (maqasa doesn't carry them),
  - joins `boursa-kuwait-stocks.json` for the company name + sector so the
    Calendar view doesn't need a runtime join.

Only forward-looking events are emitted (ex-date no older than
`LOOKBACK_DAYS` days) so the Calendar tab stays a "what's coming" view
rather than a ten-year archive.

Run cadence: daily after market close, wired into `update-dividends.yml`
right after the maqasa scrape. Re-running daily also advances the
date-window cutoff even on days maqasa itself didn't change.

Usage:
    python3 update_dividend_calendar.py
    python3 update_dividend_calendar.py --dividends output/dividends.json --out output/dividend-calendar.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DIVIDENDS = SCRIPT_DIR / "output" / "dividends.json"
DEFAULT_OUT = SCRIPT_DIR / "output" / "dividend-calendar.json"
STOCKS_FILE = SCRIPT_DIR / "boursa-kuwait-stocks.json"

# How far back an ex-date may be and still appear in the calendar. A small
# grace window keeps very-recently-passed events visible instead of
# vanishing at midnight on their ex-date; everything older is pruned.
LOOKBACK_DAYS = 7


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_iso(s) -> date | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        return None


def prev_trading_day(d: date) -> date:
    """Trading day immediately before `d`. Boursa Kuwait trades Sun–Thu;
    the weekend is Friday (weekday 4) and Saturday (weekday 5). Public
    holidays aren't modelled — this is the cum-date approximation that
    stands in for the value maqasa doesn't publish."""
    d -= timedelta(days=1)
    while d.weekday() in (4, 5):
        d -= timedelta(days=1)
    return d


def load_stock_lookup() -> dict[str, dict]:
    """Map ticker → {nameEn, nameAr, tickerNumber, sector} so the calendar
    can render the company name + sector without a runtime join."""
    if not STOCKS_FILE.exists():
        return {}
    with open(STOCKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {
        s["ticker"]: {
            "nameEn": s.get("nameEn"),
            "nameAr": s.get("nameAr"),
            "tickerNumber": s.get("tickerNumber"),
            "sector": s.get("sector"),
        }
        for s in data["stocks"]
    }


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--dividends", default=str(DEFAULT_DIVIDENDS),
                    help="Path to the maqasa dividends.json")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="Output JSON path")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    dividends_path = Path(args.dividends)
    out_path = Path(args.out)
    if not dividends_path.exists():
        log(f"ERROR: dividends file not found at {dividends_path} — run update_dividends.py first")
        return 1

    stock_lookup = load_stock_lookup()
    if not stock_lookup:
        log(f"WARN: stock lookup empty (couldn't read {STOCKS_FILE.name}); names will be null")

    with open(dividends_path, "r", encoding="utf-8") as f:
        dividends = json.load(f)
    by_ticker = dividends.get("dividendsByTicker", {}) or {}

    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
    events: list[dict] = []
    skipped_no_ex = 0
    pruned_old = 0

    for ticker, records in by_ticker.items():
        meta = stock_lookup.get(ticker, {})
        for rec in records:
            ex = parse_iso(rec.get("exDate"))
            if ex is None:
                skipped_no_ex += 1
                continue
            if ex < cutoff:
                pruned_old += 1
                continue
            events.append({
                "ticker": ticker,
                "nameEn": meta.get("nameEn"),
                "nameAr": meta.get("nameAr"),
                "tickerNumber": meta.get("tickerNumber"),
                "sector": meta.get("sector"),
                "cumDate": prev_trading_day(ex).isoformat(),
                "exDate": ex.isoformat(),
                "recordDate": None,   # maqasa doesn't publish this
                "paymentDate": None,  # maqasa doesn't publish this
                "amountPerShare": rec.get("amountPerShare"),
                "currency": rec.get("currency"),
                "type": rec.get("type"),
            })

    # Ascending by ex-date — the app re-groups for display, but this reads
    # naturally as a forward chronological list.
    events.sort(key=lambda e: (e["exDate"], e["ticker"]))

    log(f"Built {len(events)} calendar events from {len(by_ticker)} tickers "
        f"(cutoff ex-date >= {cutoff.isoformat()})")
    if pruned_old:
        log(f"  Pruned {pruned_old} past events older than the {LOOKBACK_DAYS}-day window")
    if skipped_no_ex:
        log(f"  Skipped {skipped_no_ex} records with no ex-date")

    output = {
        "_meta": {
            "generatedAt": datetime.utcnow().isoformat() + "Z",
            "source": "maqasa.com (derived from dividends.json)",
            "eventCount": len(events),
            "lookbackDays": LOOKBACK_DAYS,
        },
        "events": events,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
