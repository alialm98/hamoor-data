#!/usr/bin/env python3
"""
update_prices.py
─────────────────────────────────────────────────────────────────────────
Daily EOD updater for Hamoor.

Fetches recent Boursa Kuwait closes from Yahoo Finance and merges them
(by date) into the existing per-stock JSON files. Designed to run
weekdays after the Kuwait market close.

Updates two sets of files:
    1. Canonical   hamoor-data/output/prices/{TICKER}.json   (full: d/c/v)
    2. Bundled     Hamoor/Resources/prices/{TICKER}.json     (slim: d/c)

Also rewrites output/stocks.json (inline 90d recent) and output/manifest.json.

Usage:
    python3 update_prices.py                        # rolling 10-day refresh
    python3 update_prices.py --days 30              # wider catchup window
    python3 update_prices.py --from 2026-05-13 --to 2026-05-14   # backfill
    python3 update_prices.py --ticker KFH           # single stock test
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yfinance as yf

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

CANONICAL_PRICES_DIR = SCRIPT_DIR / "output" / "prices"
BUNDLED_PRICES_DIR = REPO_ROOT / "Hamoor" / "Resources" / "prices"
STOCKS_FILE = SCRIPT_DIR / "boursa-kuwait-stocks.json"
STOCKS_OUT = SCRIPT_DIR / "output" / "stocks.json"
MANIFEST_OUT = SCRIPT_DIR / "output" / "manifest.json"

DEFAULT_WINDOW_DAYS = 10
INLINE_RECENT_DAYS = 90
PRICE_DIVISOR = 1000           # Yahoo returns fils → convert to KWD
THROTTLE_SECONDS = 0.4


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_stock_list() -> List[dict]:
    with open(STOCKS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)["stocks"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Daily EOD price updater (incremental)")
    p.add_argument("--from", dest="from_date", help="Start date inclusive YYYY-MM-DD")
    p.add_argument("--to", dest="to_date", help="End date inclusive YYYY-MM-DD")
    p.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS,
                   help=f"Rolling window size when no date range given (default {DEFAULT_WINDOW_DAYS})")
    p.add_argument("--ticker", help="Restrict to one ticker (for testing)")
    return p.parse_args()


def resolve_date_range(args: argparse.Namespace) -> Tuple[date, date]:
    today = date.today()
    if args.from_date or args.to_date:
        start = date.fromisoformat(args.from_date) if args.from_date else today - timedelta(days=args.days)
        end = date.fromisoformat(args.to_date) if args.to_date else today
    else:
        end = today
        start = today - timedelta(days=args.days)
    if start > end:
        raise SystemExit(f"--from ({start}) is after --to ({end})")
    return start, end


def fetch_window(ticker_symbol: str, start: date, end: date) -> Optional[List[dict]]:
    """Fetch [start, end] inclusive. Returns list of {d,c,v} or None on failure."""
    yahoo_ticker = f"{ticker_symbol}.KW"
    try:
        t = yf.Ticker(yahoo_ticker)
        hist = t.history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),  # yfinance end is exclusive
            interval="1d",
            auto_adjust=False,
        )
        if hist.empty:
            return []
        rows = []
        for ts, row in hist.iterrows():
            close = row["Close"]
            if close is None or close != close:
                continue
            volume = row["Volume"]
            vol_int = 0 if (volume is None or volume != volume) else int(volume)
            rows.append({
                "d": ts.date().isoformat(),
                "c": round(float(close) / PRICE_DIVISOR, 4),
                "v": vol_int,
            })
        return rows
    except Exception as e:
        log(f"   [error] {yahoo_ticker}: {str(e)[:120]}")
        return None


def load_canonical(ticker: str) -> dict:
    path = CANONICAL_PRICES_DIR / f"{ticker}.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"ticker": ticker, "currency": "KWD", "firstDate": None,
            "lastDate": None, "rows": 0, "history": []}


def merge_history(existing: List[dict], incoming: List[dict]) -> Tuple[List[dict], int, int]:
    """Returns (merged, added, replaced). Incoming wins on date conflict."""
    by_date = {row["d"]: row for row in existing}
    added = 0
    replaced = 0
    for row in incoming:
        if row["d"] in by_date:
            if by_date[row["d"]] != row:
                replaced += 1
            by_date[row["d"]] = row
        else:
            by_date[row["d"]] = row
            added += 1
    merged = sorted(by_date.values(), key=lambda r: r["d"])
    return merged, added, replaced


def write_canonical(ticker: str, history: List[dict]) -> None:
    CANONICAL_PRICES_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "ticker": ticker,
        "currency": "KWD",
        "firstDate": history[0]["d"] if history else None,
        "lastDate": history[-1]["d"] if history else None,
        "rows": len(history),
        "history": history,
    }
    path = CANONICAL_PRICES_DIR / f"{ticker}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))


def write_bundled(ticker: str, history: List[dict]) -> None:
    """Slim format used by the iOS app bundle: ticker + history (d,c only).

    Only written when the sibling `Hamoor/Resources/prices` directory
    exists — i.e. when this script is running from the local Mac repo
    layout. On the GitHub Actions runner (only the data repo is checked
    out), there's no `Hamoor/` to write into, so we skip silently.
    """
    if not BUNDLED_PRICES_DIR.parent.exists():
        return
    BUNDLED_PRICES_DIR.mkdir(parents=True, exist_ok=True)
    slim = [{"d": r["d"], "c": r["c"]} for r in history]
    out = {"ticker": ticker, "history": slim}
    path = BUNDLED_PRICES_DIR / f"{ticker}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))


def rebuild_stocks_file(stocks: List[dict], price_data: Dict[str, List[dict]]) -> None:
    """Rewrite output/stocks.json with last 90 days inline per stock.

    Stocks with no known close price are OMITTED. The iOS app's Codable
    model has `lastClosePrice` and `lastClosePriceDate` as non-optional,
    so a single null breaks the whole decode. Stocks with no Yahoo
    coverage get filtered out here rather than crashing every device.
    """
    enriched = []
    skipped: List[str] = []
    for stock in stocks:
        ticker = stock["ticker"]
        full = price_data.get(ticker)
        if full is None:
            # Not touched in this run — preserve previous inline data if any.
            existing = _read_existing_stock_row(ticker)
            if existing is not None:
                merged = {**stock, **existing}
                if merged.get("lastClosePrice") is None or merged.get("lastClosePriceDate") is None:
                    skipped.append(ticker)
                    continue
                enriched.append(merged)
                continue
            # No previous data AND nothing fetched this run → skip.
            skipped.append(ticker)
            continue

        recent = full[-INLINE_RECENT_DAYS:]
        last_close = recent[-1]["c"] if recent else None
        last_date = recent[-1]["d"] if recent else None
        if last_close is None or last_date is None:
            skipped.append(ticker)
            continue
        has_history = bool(full)
        enriched.append({
            **stock,
            "lastClosePrice": last_close,
            "lastClosePriceDate": last_date,
            "recentPrices": recent,
            "hasFullHistory": has_history,
        })

    if skipped:
        log(f"Skipped {len(skipped)} stock(s) from stocks.json (no price): {', '.join(skipped)}")

    out = {
        "version": 1,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "currency": "KWD",
        "stockCount": len(enriched),
        "stocks": enriched,
    }
    with open(STOCKS_OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


_existing_stocks_cache: Optional[Dict[str, dict]] = None


def _read_existing_stock_row(ticker: str) -> Optional[dict]:
    """Pull a single ticker's inline fields from the pre-existing stocks.json."""
    global _existing_stocks_cache
    if _existing_stocks_cache is None:
        if not STOCKS_OUT.exists():
            _existing_stocks_cache = {}
        else:
            with open(STOCKS_OUT, "r", encoding="utf-8") as f:
                data = json.load(f)
            _existing_stocks_cache = {
                s["ticker"]: {
                    "lastClosePrice": s.get("lastClosePrice"),
                    "lastClosePriceDate": s.get("lastClosePriceDate"),
                    "recentPrices": s.get("recentPrices", []),
                    "hasFullHistory": s.get("hasFullHistory", False),
                }
                for s in data.get("stocks", [])
                if "ticker" in s
            }
    return _existing_stocks_cache.get(ticker)


def write_manifest(price_data_tickers: List[str]) -> None:
    out = {
        "version": 1,
        "schemaVersion": "1.0.0",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "files": {
            "stocks": {"url": "/api/v1/stocks.json"},
            "prices": {
                "baseUrl": "/api/v1/prices",
                "tickers": sorted(price_data_tickers),
            },
        },
    }
    with open(MANIFEST_OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)


def main() -> int:
    args = parse_args()
    start, end = resolve_date_range(args)
    log(f"Updating prices for window {start} → {end} (inclusive)")

    stocks = load_stock_list()
    if args.ticker:
        stocks = [s for s in stocks if s["ticker"] == args.ticker.upper()]
        if not stocks:
            log(f"Ticker {args.ticker} not found in stock list.")
            return 1

    log(f"Stocks to refresh: {len(stocks)}")

    price_data: Dict[str, List[dict]] = {}
    touched = 0
    added_total = 0
    replaced_total = 0
    empty = []
    failed = []

    for i, stock in enumerate(stocks, 1):
        ticker = stock["ticker"]
        incoming = fetch_window(ticker, start, end)
        if incoming is None:
            failed.append(ticker)
            log(f"[{i}/{len(stocks)}] {ticker}: FAIL")
            time.sleep(THROTTLE_SECONDS)
            continue
        canonical = load_canonical(ticker)
        merged, added, replaced = merge_history(canonical.get("history", []), incoming)
        price_data[ticker] = merged

        if added or replaced:
            write_canonical(ticker, merged)
            write_bundled(ticker, merged)
            touched += 1
            added_total += added
            replaced_total += replaced
            tag = f"+{added}" + (f" ~{replaced}" if replaced else "")
            log(f"[{i}/{len(stocks)}] {ticker}: {tag} (last={merged[-1]['d']})")
        else:
            if not incoming:
                empty.append(ticker)
            log(f"[{i}/{len(stocks)}] {ticker}: no change")
        time.sleep(THROTTLE_SECONDS)

    log("─" * 60)
    log(f"Touched files : {touched}")
    log(f"Rows added    : {added_total}")
    log(f"Rows replaced : {replaced_total}")
    if empty:
        log(f"Empty windows : {len(empty)} ({', '.join(empty[:10])}{'…' if len(empty) > 10 else ''})")
    if failed:
        log(f"Failures      : {len(failed)} ({', '.join(failed)})")

    # Always rebuild aggregate files even on single-ticker runs so stocks.json stays consistent
    log("Rebuilding stocks.json…")
    rebuild_stocks_file(load_stock_list(), price_data)
    log("Rebuilding manifest.json…")
    # tickers in manifest = union of price files present on disk
    all_tickers = sorted(p.stem for p in CANONICAL_PRICES_DIR.glob("*.json"))
    write_manifest(all_tickers)
    log("Done.")
    return 0 if not failed else 0  # don't error-exit on partial failures; logs are enough


if __name__ == "__main__":
    sys.exit(main())
