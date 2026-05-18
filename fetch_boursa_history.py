#!/usr/bin/env python3
"""
fetch_boursa_history.py
─────────────────────────────────────────────────────────────────────────
Downloads historical daily prices for all Boursa Kuwait stocks from
Yahoo Finance and writes them as JSON files in the Hamoor data layout.

Usage:
    pip install yfinance
    python3 fetch_boursa_history.py            # backfill 5 years for all stocks
    python3 fetch_boursa_history.py --ticker KFH  # single stock test

Output structure:
    output/
    ├── manifest.json
    ├── stocks.json            (metadata + last 90 days inline)
    └── prices/
        ├── KFH.json
        ├── NBK.json
        └── ...

Compatible with Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yfinance as yf

# ---------------------------------------------------------------------------
# Configuration

OUTPUT_DIR = Path("./output")
PRICES_DIR = OUTPUT_DIR / "prices"
HISTORY_PERIOD = "5y"           # change to "max" for full history
INLINE_RECENT_DAYS = 90         # how many days to embed in stocks.json
THROTTLE_SECONDS = 0.5          # pause between requests; Yahoo gets cranky if you hammer it
PRICE_DIVISOR = 1000            # Yahoo returns Boursa Kuwait prices in fils; convert to KWD

STOCKS_FILE = Path("./boursa-kuwait-stocks.json")

# ---------------------------------------------------------------------------


def load_stock_list() -> List[dict]:
    """Load the master stock list with ticker + metadata."""
    if not STOCKS_FILE.exists():
        print(f"[!] {STOCKS_FILE} not found. Place your stock list JSON next to this script.")
        raise SystemExit(1)
    with open(STOCKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["stocks"]


def fetch_history(ticker_symbol: str, period: str = HISTORY_PERIOD) -> Optional[List[dict]]:
    """
    Fetch historical daily prices for a Boursa Kuwait ticker.
    Returns a list of {d, c, v} dicts in chronological order, or None on failure.
    """
    yahoo_ticker = f"{ticker_symbol}.KW"
    try:
        ticker = yf.Ticker(yahoo_ticker)
        hist = ticker.history(period=period, interval="1d", auto_adjust=False)
        if hist.empty:
            print(f"   [empty] {yahoo_ticker} no data on Yahoo")
            return None
        rows = []
        for ts, row in hist.iterrows():
            close = row["Close"]
            # NaN check (NaN is not equal to itself)
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
        print(f"   [error] {yahoo_ticker}: {str(e)[:100]}")
        return None


def write_price_file(ticker: str, history: List[dict]) -> Path:
    """Write a single stock's full price history to prices/{TICKER}.json."""
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "ticker": ticker,
        "currency": "KWD",
        "firstDate": history[0]["d"] if history else None,
        "lastDate": history[-1]["d"] if history else None,
        "rows": len(history),
        "history": history,
    }
    path = PRICES_DIR / f"{ticker}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))  # compact, single-line
    return path


def write_stocks_file(stocks: List[dict], price_data: Dict[str, List[dict]]) -> Path:
    """
    Write the main stocks.json with metadata + last 90 days inline per stock.
    Mobile app fetches this on launch.
    """
    enriched = []
    for stock in stocks:
        ticker = stock["ticker"]
        full_history = price_data.get(ticker, [])
        recent = full_history[-INLINE_RECENT_DAYS:] if full_history else []
        last_close = recent[-1]["c"] if recent else None
        last_date = recent[-1]["d"] if recent else None
        enriched.append({
            **stock,
            "lastClosePrice": last_close,
            "lastClosePriceDate": last_date,
            "recentPrices": recent,
            "hasFullHistory": bool(full_history),
        })

    out = {
        "version": 1,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "currency": "KWD",
        "stockCount": len(enriched),
        "stocks": enriched,
    }
    path = OUTPUT_DIR / "stocks.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return path


def write_manifest(price_data: Dict[str, List[dict]]) -> Path:
    """Write the tiny manifest.json that the app fetches first."""
    out = {
        "version": 1,
        "schemaVersion": "1.0.0",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "files": {
            "stocks": {"url": "/api/v1/stocks.json"},
            "prices": {
                "baseUrl": "/api/v1/prices",
                "tickers": sorted(price_data.keys()),
            },
        },
    }
    path = OUTPUT_DIR / "manifest.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    return path


def main():
    parser = argparse.ArgumentParser(description="Fetch Boursa Kuwait history from Yahoo Finance")
    parser.add_argument("--ticker", help="Fetch only one ticker (for testing)")
    parser.add_argument("--period", default=HISTORY_PERIOD, help="Yahoo Finance period (e.g. 1y, 5y, max)")
    args = parser.parse_args()

    stocks = load_stock_list()
    if args.ticker:
        stocks = [s for s in stocks if s["ticker"] == args.ticker.upper()]
        if not stocks:
            print(f"[!] Ticker {args.ticker} not found in stock list.")
            return

    print(f"Fetching history for {len(stocks)} stocks (period={args.period})...")
    print("-" * 60)

    price_data: Dict[str, List[dict]] = {}
    success = 0
    failed = []

    for i, stock in enumerate(stocks, 1):
        ticker = stock["ticker"]
        print(f"[{i}/{len(stocks)}] {ticker}.KW ... ", end="", flush=True)
        history = fetch_history(ticker, period=args.period)
        if history:
            path = write_price_file(ticker, history)
            price_data[ticker] = history
            print(f"OK {len(history)} rows -> {path.name}")
            success += 1
        else:
            failed.append(ticker)
            print("skipped")
        time.sleep(THROTTLE_SECONDS)

    print("-" * 60)
    print(f"Success: {success}/{len(stocks)}")
    if failed:
        print(f"Failed: {len(failed)}: {', '.join(failed)}")
        print("(These may not exist on Yahoo Finance. Smaller stocks often don't.)")

    if not args.ticker:
        stocks_path = write_stocks_file(stocks, price_data)
        manifest_path = write_manifest(price_data)
        print(f"\nWrote {stocks_path}")
        print(f"Wrote {manifest_path}")
        print(f"Wrote {len(price_data)} per-stock files in {PRICES_DIR}")

    print("\nNext step: git commit && git push from your hamoor-data repo.")


if __name__ == "__main__":
    main()
